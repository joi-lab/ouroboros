"""#1347 A: keys the engine animates to the live edge lead to the present."""
import json

import pytest

from tests.test_chat_history_paging_browser import _open, _open_project, _screenshot, _FRAMES, _write
from tests.test_history_continuity_browser import _FIRST_VISIBLE, _OFFSET, _bookmark_place
from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

_AT_REST = """feed => new Promise((done, fail) => {
    const root = document.querySelector(feed), limit = 30000, deadline = performance.now() + limit, seen = [];
    let last = root.scrollTop, still = 0;
    const tick = () => {
        still = root.scrollTop === last ? still + 1 : 0;
        last = root.scrollTop;
        seen.push(last);
        if (still >= 3) done();
        else if (performance.now() > deadline) fail(new Error(`${feed} never rested for 3 frames in ${limit}ms `
            + `over ${seen.length} frames; last scrollTop ${seen.slice(-8).join(', ')} of `
            + `${root.scrollHeight - root.clientHeight}`));
        else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
})"""


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
def test_keys_that_read_to_the_live_edge_follow_new_replies(direct_server_with_data, browser_engine, tmp_path):
    """#1347 A: End or PageDown that the engine animates to the bottom leads to the present,
    though WebKit ends every step of an animated page; an upward key stops following."""
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project
    from tests.ui_chat_viewport_smoke import _emit_ws_frame

    root = direct_server_with_data['data_dir']
    project = create_project(root, 'keyboard-room', name='Keyboard reading')
    cid = project['chat_id']
    _write(root / 'logs/chat.jsonl', [
        {'direction': 'in', 'chat_id': cid, 'ts': f'2026-09-01T10:{index:02d}:00Z',
         'client_message_id': f'key-{index}', 'text': f'Keyboard context {index:02d}'} for index in range(60)])
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            _open(page, direct_server_with_data['url'])
            feed = _open_project(page, project)
            root_feed = page.locator(feed)
            remaining = 'n => n.scrollHeight - n.scrollTop - n.clientHeight'
            steps = {}

            def press(key):
                before = root_feed.evaluate('n => n.scrollTop')
                page.keyboard.press(key)
                page.wait_for_function('([feed, before]) => document.querySelector(feed).scrollTop !== before',
                                       arg=[feed, before])
                page.evaluate(_AT_REST, feed)
                return root_feed.evaluate(remaining)

            def reply(key):
                first = root_feed.evaluate(_FIRST_VISIBLE)
                _emit_ws_frame(page, {'type': 'chat', 'role': 'user', 'chat_id': cid, 'content': f'A new reply {key}',
                                      'ts': '2026-09-01T11:30:00Z', 'client_message_id': key, 'sender_session_id': 'other-tab'})
                page.locator(f'{feed} [data-client-message-id="{key}"]').wait_for(state='attached')
                page.evaluate(_FRAMES)
                kept = page.locator(f'{feed} [data-history-id="{first["id"]}"]').evaluate(_OFFSET)
                return {'gap': root_feed.evaluate(remaining), 'moved': kept - first['offset']}

            # Click the text being read (focus stays on the document): keys then scroll this feed.
            _bookmark_place(page, page.locator(f'{feed} [data-client-message-id="key-20"]'), 40)
            page.locator(f'{feed} [data-client-message-id="key-20"] .message').click()
            steps['reading'] = reply('key-reading')
            assert steps['reading']['gap'] > 20 and abs(steps['reading']['moved']) <= 2, steps
            steps['end'] = press('End')
            steps['end-reply'] = reply('key-end')
            assert steps['end'] <= 2 and steps['end-reply']['gap'] <= 2, ('End to the bottom follows', steps)
            steps['page-up'] = press('PageUp')
            steps['page-down'] = press('PageDown')
            assert steps['page-up'] > 48 >= steps['page-down'], steps
            steps['page-down-reply'] = reply('key-page-down')
            _screenshot(page, tmp_path, f'keys-follow-{browser_engine}')
            assert steps['page-down-reply']['gap'] <= 2, ('an animated PageDown to the bottom follows', steps)
            steps['page-up-again'] = press('PageUp')
            steps['away-reply'] = reply('key-away')
            _screenshot(page, tmp_path, f'keys-away-{browser_engine}')
            (tmp_path / f'keys-follow-{browser_engine}.json').write_text(json.dumps(steps))
            assert steps['away-reply']['gap'] > 20 and abs(steps['away-reply']['moved']) <= 2, (
                'an upward key stops following', steps)
            assert page.evaluate('() => window.__historyReads.every(read => !read.cursor)'), 'keys at the edge page nothing'
        finally:
            browser.close()
