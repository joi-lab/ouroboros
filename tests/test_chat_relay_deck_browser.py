"""Relay notes in real Project rooms, using the isolated dirty-candidate server.

Representative authored fixtures cover the long notes and mixed sender runs in
our frozen relay scenes, without depending on a private owner's history export.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from ouroboros import dialogue_store as ds
from ouroboros.chat_references import reference_from_row
from ouroboros.projects_registry import create_project
from tests._shared import producer_evidence_dir
from tests.test_chat_history_paging_browser import _FRAMES, _open, _open_project
from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

QUOTE = "\n".join([
    "Please keep the existing environment and check the dependency versions before changing the setup.",
    "The Project room should explain what was verified and where the remaining uncertainty comes from.",
    "Include the exact browser sizes, check the phone layout, and keep the note readable in both themes.",
    "The final handoff needs the relevant logs, screenshots and the commands used to reproduce the checks.",
    "Do not publish until I have read the result and compared it with the approved design.",
])
NOTE = "\n\n".join([
    "The receiving work should verify the candidate against the retained baseline. The dependency check completed; the browser comparison still needs both engines.",
    "Use the existing Project room and keep its saved history. The owner asked for the full quoted instructions to remain available alongside this handoff.",
    "Check the narrow layout with a long sender name and a note containing multiple paragraphs. Open the deck, expand this note, then fold it back without losing the row's address or Reply and Copy actions.",
    "Record the exact checks and preserve the evidence in the candidate's QA folder. Return a concise report after all isolated processes have stopped.",
])


def seed_relay_notes(root):
    room = create_project(root, 'relay-receiver', name='Relay notes', origin='owner_ui')
    sender = create_project(root, 'relay-sender', name='Environment checks and dependency verification', origin='owner_ui')
    other = create_project(root, 'relay-other', name='Release follow-up', origin='owner_ui')
    start = datetime(2026, 10, 22, 11, tzinfo=timezone.utc)
    index = 0

    def say(text, **extra):
        nonlocal index
        index += 1
        row = {'chat_id': room['chat_id'], 'ts': (start + timedelta(minutes=index)).isoformat(),
               'direction': 'out', 'text': text, **extra}
        ds.append(root, 'chat', row, feed=True)
        return row

    def note(key, text, sender_facts=None, *, quotes=(), summary=''):
        relay = {'relay_id': key, 'quotes': list(quotes), 'summary': summary}
        if sender_facts:
            relay['sender'] = sender_facts
        return say(text, type='relay_entry', client_message_id=key, relay=relay)

    source = {'chat_id': 1, 'label': 'Main', 'ts': start.isoformat()}
    quote = {'text': QUOTE, 'source': source}
    note('lone', NOTE, {'name': 'Main', 'chat_id': 1}, summary='The environment check is ready for the next work.')
    say('Please keep the results together in this room.', direction='in')
    first = note('quoted', NOTE, sender, quotes=[quote, {'text': 'Keep the attached log with the handoff.',
                 'source': source, 'attachments': [{'name': 'dependency-check.txt'}]}],
                 summary='The owner asked for both engines and the full verification record.')
    note('main', 'The runtime check is complete; the candidate can proceed to visual verification.', {'name': 'Main', 'chat_id': 1})
    note('legacy', 'An older work left this note before sender rooms were recorded.', {'task_id': 'old-unbound'}, quotes=[quote])
    front = note('front', NOTE, other, summary='The next work can compare the browser results and finish the handoff.')
    # A real reply reference exercises the room's reveal path to a tucked relay row.
    say('Return to the earlier verification instructions.', reply_to=reference_from_row(first))
    return room, first, front


def _snapshot(page, root, name, feed, facts):
    dest = producer_evidence_dir(root, 'relay-note-deck')
    page.screenshot(path=str(dest / f'{name}.png'), animations='disabled', scale='css')
    measured = page.locator(feed).evaluate("""feed => {
        const composer = feed.closest('.chat-instance-panel').querySelector('.chat-input-area');
        return { viewport: [innerWidth, innerHeight], pageWidth: document.documentElement.scrollWidth,
            feedWidth: feed.clientWidth, feedScrollWidth: feed.scrollWidth,
            cards: [...feed.querySelectorAll('.chat-relay-note')].filter(n => n.getClientRects().length).map(n => {
                const r = n.getBoundingClientRect();
                return {text: n.innerText, x: r.x, width: r.width, raw: n.dataset.raw,
                    controls: [...n.querySelectorAll('button')].filter(b => b.getClientRects().length).map(b => ({
                        text: b.textContent, label: b.getAttribute('aria-label'), width: b.getBoundingClientRect().width}))};
            }), composerTop: composer?.getBoundingClientRect().top };
    }""")
    assert measured['pageWidth'] <= measured['viewport'][0], measured
    assert measured['feedScrollWidth'] <= measured['feedWidth'] + 1, measured
    facts[name] = measured
    (dest / f'{name}.json').write_text(json.dumps(measured, indent=2))


@pytest.mark.parametrize('browser_engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('viewport', [(1100, 750), (390, 844)], ids=['desktop', 'phone'])
def test_relay_notes_deck_and_expansion(direct_server_with_data, browser_engine, viewport, tmp_path):
    from playwright.sync_api import sync_playwright

    room, first, front = seed_relay_notes(direct_server_with_data['data_dir'])
    feed = f'#pchat-{room["id"]}-messages'
    prefix = f'{browser_engine}-{viewport[0]}'
    errors, facts = [], {}
    with sync_playwright() as pw:
        browser = getattr(pw, browser_engine).launch(headless=True)
        page = browser.new_page(viewport={'width': viewport[0], 'height': viewport[1]}, device_scale_factor=2)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append({'text': message.text, 'location': message.location}) if message.type == 'error' else None)
        page.on('response', lambda response: facts.setdefault('http_errors', []).append({'url': response.url, 'status': response.status}) if response.status >= 400 else None)
        try:
            response = page.request.post(direct_server_with_data['url'] + '/api/ui/preferences',
                                         data={'project_panel_width': 580, 'sidebar_width': 306})
            assert response.ok
            _open(page, direct_server_with_data['url'])
            _open_project(page, room)
            cards = page.locator(f'{feed} .chat-relay-note')
            assert cards.count() == 5
            latest = cards.nth(4)
            latest.locator('[data-intent="open-project"]').wait_for()
            assert latest.locator('.chat-relay-words-inside').inner_text() == 'Your words in 2 earlier notes'
            assert cards.nth(1).get_attribute('data-deck-tucked') == '1'
            assert cards.nth(0).get_attribute('data-deck') is None
            assert page.locator(f'{feed} .chat-relay-tile, {feed} .chat-clamp-toggle').count() == 0
            dest = producer_evidence_dir(tmp_path, 'relay-note-deck')
            latest.screenshot(path=str(dest / f'{prefix}-card@2x.png'), animations='disabled')
            for theme in ('dark', 'light'):
                page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
                latest.scroll_into_view_if_needed()
                page.evaluate(_FRAMES)
                _snapshot(page, tmp_path, f'{prefix}-{theme}-collapsed', feed, facts)
            page.evaluate("document.documentElement.dataset.theme = 'dark'")
            latest.locator('.chat-relay-act').focus()
            page.keyboard.press('Enter')
            assert latest.locator('.chat-relay-more').evaluate('n => n === document.activeElement')
            earlier = cards.nth(1)
            earlier.evaluate("n => { const f = n.closest('.chat-messages'); f.scrollTop += n.getBoundingClientRect().top - f.getBoundingClientRect().top - 110; }")
            page.evaluate(_FRAMES)
            assert earlier.get_attribute('data-deck') == 'open'
            assert latest.locator('.chat-relay-words-inside').count() == 0
            assert cards.nth(3).locator('[data-intent="open-project"]').count() == 0
            assert cards.nth(3).locator('.chat-relay-from').inner_text() == 'Another work'
            _snapshot(page, tmp_path, f'{prefix}-dark-fanned', feed, facts)
            excerpt = earlier.locator('.chat-quote-text').first
            assert len(excerpt.inner_text()) < len(QUOTE)
            more = earlier.locator('.chat-relay-more')
            more.focus()
            page.keyboard.press('Enter')
            assert more.inner_text() == 'less'
            assert more.get_attribute('aria-expanded') == 'true'
            assert more.get_attribute('aria-label').startswith('Collapse note from ')
            assert more.evaluate('n => n === document.activeElement')
            assert excerpt.inner_text() == QUOTE
            assert earlier.locator('.chat-quote-text').nth(1).inner_text() == 'Keep the attached log with the handoff.'
            assert earlier.locator('.chat-quote-files').inner_text() == 'Attached: dependency-check.txt'
            assert earlier.locator('.message').inner_text() == NOTE
            assert earlier.locator('.chat-message-reply').is_visible()
            assert earlier.locator('.chat-message-copy').is_visible()
            for theme in ('dark', 'light'):
                page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
                earlier.evaluate("n => { const f = n.closest('.chat-messages'); f.scrollTop += n.getBoundingClientRect().top - f.getBoundingClientRect().top - 110; }")
                page.evaluate(_FRAMES)
                _snapshot(page, tmp_path, f'{prefix}-{theme}-more-start', feed, facts)
                more.scroll_into_view_if_needed()
                page.evaluate(_FRAMES)
                _snapshot(page, tmp_path, f'{prefix}-{theme}-more-end', feed, facts)
            more.click()
            assert earlier.get_attribute('data-raw') == '0'
            assert more.inner_text() == 'more'
            assert len(excerpt.inner_text()) < len(QUOTE)
            earlier.locator('.chat-relay-hide').click()
            assert latest.locator('.chat-relay-act').evaluate('n => n === document.activeElement')
            # Exact row navigation unfolds the same row before focusing it.
            page.locator(f'{feed} .chat-quote-main').click()
            page.wait_for_function("selector => { const n = document.querySelector(selector); return n?.dataset.deck === 'open' && n === document.activeElement; }", timeout=5000,
                                   arg=f'{feed} .chat-relay-note.is-quote-target')
            # The sender reference really opens the source Project; no synthetic click handler.
            earlier.locator('[data-intent="open-project"]').click()
            page.locator('#pchat-relay-sender-messages').wait_for(state='visible')
            # The isolated gateway has no native Claudexor maintenance bridge. Preserve its
            # expected 503 in evidence; all other console, script and HTTP errors fail this check.
            maintenance = '/api/claudexor/maintenance/harnesses'
            unexpected = [error for error in errors if not (
                isinstance(error, dict) and urlsplit(error['location'].get('url', '')).path == maintenance
                and '503' in error['text'])]
            assert not unexpected, unexpected
            assert not [error for error in facts.get('http_errors', []) if not (
                urlsplit(error['url']).path == maintenance and error['status'] == 503)], facts
        finally:
            dest = producer_evidence_dir(tmp_path, 'relay-note-deck')
            page.screenshot(path=str(dest / f'{prefix}-last.png'), animations='disabled')
            (dest / f'{prefix}-last.html').write_text(page.content())
            (dest / f'{prefix}-errors.json').write_text(json.dumps(errors))
            (dest / f'{prefix}-facts.json').write_text(json.dumps(facts, indent=2))
            browser.close()
