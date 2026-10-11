"""A real late panel through outbox/history, Reviews hydration and source download."""
import asyncio
import hashlib
import json
import os
import queue
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from tests.test_acceptance_late_consumers import delivered, _caller, _request, _source, until
from tests.test_acceptance_late_consumers import late as late, fresh_sends as fresh_sends
from tests.test_subscription_setup_browser import subscription_ui as subscription_ui

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.usefixtures('late')
def test_historical_supplement_live_reload_reconnect_and_full_source(tmp_path, monkeypatch, request):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ouroboros.gateway import tasks
    from ouroboros.gateway.history import make_chat_history_endpoint
    from ouroboros.task_results import load_task_result
    from ouroboros.review_operation import _LIVE
    from supervisor import events_chat_delivery, message_bus
    from supervisor.terminal_delivery import pending_deliveries

    f = delivered(tmp_path, monkeypatch)
    ctx = _caller(f)
    ctx.event_queue = queue.Queue()
    _request(f, ctx, _source(ctx))
    until(lambda: not _LIVE)
    panel = load_task_result(f.root, f.tid)['review_projection']['panels'][0]
    note = panel['late_settlement']['note']
    headline = note.splitlines()[0]
    monkeypatch.setattr(message_bus, 'DATA_DIR', f.root)
    monkeypatch.setattr(message_bus, 'publish_event', lambda *_a, **_k: None)
    bridge = message_bus.LocalChatBridge({})
    monkeypatch.setattr(message_bus, 'get_bridge', lambda: bridge)
    monkeypatch.setattr(message_bus, 'load_state', lambda: {'owner_id': 7})
    frames = []
    bridge._broadcast_fn = frames.append
    sender = SimpleNamespace(DRIVE_ROOT=f.root, task_registry={}, RUNNING={},
        send_with_budget=message_bus.send_with_budget, append_jsonl=lambda *_a: None)
    for event in pending_deliveries(f.root):
        events_chat_delivery._handle_send_message(event, sender)
        events_chat_delivery._handle_send_message(event, sender)  # repeated notification/replay
    live = [row for row in frames if row.get('type') == 'chat']
    assert len(live) == 1
    response = asyncio.run(make_chat_history_endpoint(f.root)(SimpleNamespace(query_params={'chat_id': '7'})))
    rows = json.loads(response.body)['messages']
    assert len([row for row in rows if row.get('card_row') == 'reviews']) == 1
    # A real task-progress row creates the card; placed historical rows never do.
    rows.append({'task_id': f.tid, 'is_progress': True, 'text': 'Original task completed.', 'ts': '2026-09-26T01:00:00Z'})
    app = Starlette(routes=[Route('/api/tasks/{task_id}', tasks.api_task_get),
        Route('/api/tasks/{task_id}/artifacts/{name}', tasks.api_task_artifact)])
    app.state.drive_root = f.root
    ui = request.getfixturevalue('subscription_ui')
    page = ui['page']
    output = Path(os.environ.get('OUROBOROS_UI_EVIDENCE_DIR') or tmp_path)
    output.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as client:
        def api(route):
            url = urlsplit(route.request.url)
            result = client.get(url.path + ('?' + url.query if url.query else ''))
            route.fulfill(status=result.status_code, headers={'content-type': result.headers.get('content-type', '')}, body=result.content)
        page.route('**/api/tasks/**', api)
        page.route('**/api/chat/history?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'messages': rows})))
        page.route('**/late-review', lambda route: route.fulfill(content_type='text/html', body='''<!doctype html><html><head>
            <link rel="stylesheet" href="/static/ui.css"><link rel="stylesheet" href="/static/style.css"></head>
            <body><main id="content"></main></body></html>'''))
        page.goto(ui['url'] + '/late-review')
        page.evaluate('''async () => {
            const { createChatInstance } = await import('/static/modules/chat.js');
            const handlers = new Map();
            window.lateChat = createChatInstance({
                ws: { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); }, isConnected: () => true, send() {} },
                state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 }, updateUnreadBadge() {},
                stateSnapshots: { begin: () => ({generation: 1, requestedAt: Date.now()}), gate() { return Promise.resolve(this.begin()); }, isCurrent: () => true, apply() {} },
                chatId: 7, mountEl: document.querySelector('#content'), asPanel: true,
            });
            window.lateEvent = (type, row) => handlers.get(type)?.(row);
            await lateChat.refreshHistory({revision: 1});
        }''')
        try:
            for row in live:
                page.evaluate('row => lateEvent("chat", row)', row)
            card = page.locator(f'.chat-live-card[data-task-id="{f.tid}"]')
            if card.get_attribute('data-expanded') != '1':
                card.locator('[data-live-summary-button]').click()
            section = card.locator('[data-review-section-toggle]')
            section.click()
            group = card.locator('[data-review-group-toggle]')
            group.click()
            card.locator('[data-review-attempt-toggle]').click()
            assert card.locator('[data-review-attempt]').count() == 1
            assert page.locator('.chat-live-line').filter(has_text=headline).count() == 1
            link = card.get_by_role('link', name='Download full applied review')
            raw = page.evaluate('async url => { const r = await fetch(url); if (!r.ok) throw Error(r.status); return r.text(); }', link.get_attribute('href'))
            assert hashlib.sha256(raw.encode()).hexdigest() == panel['applied_source_ref']['sha256']
            applied = json.loads(raw)
            assert applied['request']['subject'] == f.event['text']
            assert applied['late_settlement']['note'] == note and len(applied['actors']) == 3
            page.screenshot(path=str(output / 'late-review-live.png'), full_page=True)
            page.evaluate('() => lateChat.refreshHistory({revision: 2})')
            assert card.locator('[data-review-attempt]').count() == 1
            assert page.locator('.chat-live-line').filter(has_text=headline).count() == 1
            page.evaluate('() => lateEvent("open", {previouslyConnected: true})')
            page.get_by_text('♻️ Reconnected', exact=True).wait_for()
            assert card.locator('[data-review-attempt]').count() == 1
            assert page.locator('.chat-live-line').filter(has_text=headline).count() == 1
            assert link.is_visible()
            page.screenshot(path=str(output / 'late-review-reconnect.png'), full_page=True)
        finally:
            page.evaluate('() => lateChat.destroy()')
    (output / 'late-review-projection.json').write_text(json.dumps({'live': live, 'history': rows, 'panel': panel}, indent=2))
