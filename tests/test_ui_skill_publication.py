"""Real Skills geometry, Hub submission history, replacement confirmations and local
receipt clearing against an isolated server (catalog and mutation POSTs stubbed in
the browser; listings, receipts and Clear are the real backend)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ouroboros.marketplace.provenance import read_publication_record, write_publication_record, merge_state_record
from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state, save_enabled
from tests.test_ui_smoke_playwright import direct_server_with_data as _direct_server_with_data

pytestmark = pytest.mark.ui_browser
direct_server_with_data = _direct_server_with_data


def _skill(data: Path, name: str, *, reviewed=True, grant=False, version='2.0.0', bucket='external') -> Path:
    payload = data / 'skills' / bucket / name
    (payload / 'scripts').mkdir(parents=True)
    if bucket == 'ouroboroshub':
        (payload / '.ouroboroshub.json').write_text(json.dumps(
            {'schema_version': 1, 'source': 'ouroboroshub', 'slug': name, 'sanitized_name': name}))
    (payload / 'SKILL.md').write_text(
        f'---\nname: {name}\ndescription: Publication layout fixture\nversion: "{version}"\n'
        'type: script\nruntime: python3\nscripts:\n  - name: check.py\n    description: Fixture\n'
        + ('env_from_settings: [OPENROUTER_API_KEY]\n' if grant else '') + '---\n# Fixture\n')
    (payload / 'scripts/check.py').write_text("print('fixture')\n")
    if reviewed:
        save_review_state(data, name, SkillReviewState(status='pass', content_hash=compute_content_hash(payload)))
    save_enabled(data, name, False)
    return payload


def _screenshot(page, name):
    directory = os.environ.get('OUROBOROS_PUBLICATION_SCREENSHOTS')
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / name))


def test_skills_card_geometry_tracks_card_width_and_keeps_grant_on_card(direct_server_with_data):
    from playwright.sync_api import sync_playwright, expect

    data, url = direct_server_with_data['data_dir'], direct_server_with_data['url']
    names = ['publication_pending_with_a_long_but_valid_skill_name',
             'publication_grant_access_with_a_very_long_skill_name',
             'publication_ready_with_a_long_but_valid_skill_name']
    _skill(data, names[0], reviewed=False)
    _skill(data, names[1], grant=True)
    _skill(data, names[2])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 719, 'height': 900})
            page.route('**/api/marketplace/ouroboroshub/catalog*', lambda route: route.fulfill(json={'results': []}))
            page.goto(url, wait_until='domcontentloaded')
            page.click('[data-nav-page="skills"]')
            page.wait_for_selector(f'.skills-card[data-skill="{names[1]}"]')
            measurements = []
            for viewport, sidebar in [(719, 280), (1100, 560), (1440, 280)]:
                page.set_viewport_size({'width': viewport, 'height': 900})
                page.evaluate("width => document.getElementById('app').style.setProperty('--sidebar-width', `${width}px`)", sidebar)
                for name in names:
                    card = page.locator(f'.skills-card[data-skill="{name}"]')
                    card.scroll_into_view_if_needed()
                    facts = card.evaluate('''card => {
                        const rect = card.getBoundingClientRect();
                        const title = card.querySelector('.skills-card-title').getBoundingClientRect();
                        const actions = [...card.querySelectorAll('.skills-card-toggle > button, .skills-card-menu-trigger')]
                            .map(button => ({x:button.getBoundingClientRect().x, right:button.getBoundingClientRect().right}));
                        return {card:rect.width, title:title.width, x:rect.x, right:rect.right, actions,
                            direction:getComputedStyle(card.querySelector('.skills-card-head')).flexDirection,
                            sidebar:document.getElementById('primary-sidebar').getBoundingClientRect().width};
                    }''')
                    assert abs(facts['sidebar'] - sidebar) <= 2, facts
                    assert facts['title'] >= 180, facts
                    assert all(action['x'] >= facts['x'] and action['right'] <= facts['right'] for action in facts['actions']), facts
                    assert facts['direction'] == ('column' if viewport != 1440 else 'row'), facts
                    measurements.append({'viewport': viewport, 'name': name, **facts})
                grant = page.locator(f'.skills-card[data-skill="{names[1]}"]')
                grant.scroll_into_view_if_needed()
                expect(grant.get_by_role('button', name='Grant access', exact=True)).to_be_visible()
                _screenshot(page, f'skills-width-{viewport}-sidebar-{sidebar}.png')
            grant.get_by_role('button', name='Grant access', exact=True).click()
            expect(page.locator('.confirm-dialog')).to_contain_text(f'Grant access to {names[1]}')
            _screenshot(page, 'grant-access-dialog.png')
            page.locator('.confirm-dialog [data-confirm-cancel]').last.click()
            if directory := os.environ.get('OUROBOROS_PUBLICATION_SCREENSHOTS'):
                (Path(directory) / 'geometry.json').write_text(json.dumps(measurements, indent=2))
        finally:
            browser.close()


def _receipt(data: Path, name: str, version: str, content_hash: str, number: int = 60, url: str = ''):
    record = {'slug': name, 'version': version, 'content_hash': content_hash,
              'repository': 'hub/project', 'pr_number': number,
              'pr_url': url or f'https://github.com/hub/project/pull/{number}', 'published_at': '2026-09-06T00:00:00Z'}
    write_publication_record(data, name, record)
    return record


def _catalog(*rows):
    return {'results': [{'slug': name, 'sanitized_name': name, 'display_name': name, 'latest_version': version,
                         'summary': 'Controlled catalog entry', 'description': 'Controlled catalog entry',
                         'identity_conflict': False} for name, version in rows]}


def _wait_for_posts(page, posts, count):
    """Mutation routes resolve while Playwright processes events; wait for the recorded count."""
    for _ in range(200):
        if len(posts) >= count:
            break
        page.wait_for_timeout(25)
    assert len(posts) == count, posts


def _stub_mutations(page, posts):
    """Record Hub mutation POSTs and answer them locally: no catalog download or payload replacement."""
    def handle(route):
        request = route.request
        if request.method != 'POST':
            route.continue_()
            return
        posts.append({'url': request.url, 'body': request.post_data_json})
        route.fulfill(json={'ok': True, 'sanitized_name': request.url.rstrip('/').rsplit('/', 1)[-1]})
    page.route('**/api/marketplace/ouroboroshub/update/**', handle)
    page.route('**/api/marketplace/ouroboroshub/install', handle)


@pytest.mark.parametrize('bucket', ['external', 'ouroboroshub'])
def test_clear_submission_changes_real_receipt_and_never_gates_the_hub_action(direct_server_with_data, bucket):
    from playwright.sync_api import sync_playwright, expect

    data, url = direct_server_with_data['data_dir'], direct_server_with_data['url']
    name = 'publication_waiting_fixture'
    payload = _skill(data, name, bucket=bucket)
    original_hash = compute_content_hash(payload)
    first = _receipt(data, name, '2.0.0', original_hash, 7)
    merge_state_record(data, name, 'ouroboroshub.json', {'future': {'keep': True}})
    next_action = 'adopt' if bucket == 'external' else 'update'
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1100, 'height': 900})
            external, posts = [], []
            page.route('https://github.com/**', lambda route: (external.append(route.request.url), route.abort()))
            # The catalog serves an OLDER version than the submission: a pending-looking receipt.
            page.route('**/api/marketplace/ouroboroshub/catalog*', lambda route: route.fulfill(json=_catalog((name, '1.0.0'))))
            _stub_mutations(page, posts)
            page.goto(url, wait_until='domcontentloaded')
            page.click('[data-nav-page="skills"]')
            own = page.locator(f'.skills-card[data-skill="{name}"]')
            expect(own.locator('.skills-details')).to_contain_text('Submitted v2.0.0 · PR #7')
            expect(own.locator('.skills-card-title')).not_to_contain_text('Submitted')
            page.click('.skills-tab[data-tab="ouroboroshub"]')
            card = page.locator(f'#oh-results [data-slug="{name}"]')
            # #1314: the catalog copy is available while the receipt exists.
            expect(card.locator(f'[data-oh-action="{next_action}"]')).to_be_enabled()
            expect(card).not_to_contain_text('Submitted PR')
            history = card.locator('details[data-oh-history]')
            expect(history).not_to_have_attribute('open', '')
            history.locator('summary').click()
            expect(history).to_contain_text('Submitted v2.0.0 · PR #7')
            assert history.locator('a').get_attribute('href') == 'https://github.com/hub/project/pull/7'
            hint = history.locator('.marketplace-secondary-actions > .muted')
            assert hint.evaluate("node => getComputedStyle(node).fontSize === getComputedStyle(document.documentElement).getPropertyValue('--type-meta').trim()")
            _screenshot(page, f'submission-history-{bucket}.png')
            # The visible old button cannot clear a concurrent newer publication.
            newer = _receipt(data, name, '2.0.0', original_hash, 8)
            clear = page.locator(f'[data-oh-clear-publication="{name}"]')
            clear.click()
            expect(page.locator('#oh-status')).to_contain_text('publication_changed')
            assert read_publication_record(data, name)[0] == newer != first
            page.click('[data-oh-search]')
            # An opened history stays open across the refresh re-render.
            expect(history).to_have_attribute('open', '')
            expect(history).to_contain_text('PR #8')
            clear.click()
            expect(page.locator('#oh-status')).to_contain_text('local submission record cleared')
            assert read_publication_record(data, name) == (None, None)
            assert json.loads((data / f'state/skills/{name}/ouroboroshub.json').read_text())['future'] == {'keep': True}
            expect(card.locator(f'[data-oh-action="{next_action}"]')).to_be_enabled()
            expect(card.locator('details[data-oh-history]')).to_have_count(0)
            expect(page.locator('.confirm-dialog')).to_have_count(0)
            _screenshot(page, f'submission-cleared-{bucket}.png')
            page.click('.skills-tab[data-tab="installed"]')
            expect(own).not_to_contain_text('Submitted')
            assert compute_content_hash(payload) == original_hash
            # Edited since submission: the history says the files differ; Clear stays explicit.
            _receipt(data, name, '2.0.0', original_hash, 9)
            (payload / 'scripts/check.py').write_text("print('edited payload stays')\n")
            edited_hash = compute_content_hash(payload)
            page.click('.skills-tab[data-tab="ouroboroshub"]')
            card.locator('details[data-oh-history] summary').click()
            expect(card).to_contain_text('Local files differ from the submitted copy')
            clear.click()
            expect(page.locator('#oh-status')).to_contain_text('local submission record cleared')
            assert read_publication_record(data, name) == (None, None)
            assert compute_content_hash(payload) == edited_hash
            assert not external and not posts
        finally:
            browser.close()


@pytest.mark.parametrize('engine,width', [('chromium', 1100), ('webkit', 390)])
def test_hub_replacement_confirms_in_both_tabs_and_history_stays_quiet(direct_server_with_data, engine, width):
    from playwright.sync_api import sync_playwright, expect

    data, url = direct_server_with_data['data_dir'], direct_server_with_data['url']
    lens = _skill(data, 'lens_fixture', version='1.1.2')
    _receipt(data, 'lens_fixture', '1.1.2', compute_content_hash(lens), 60)
    pending = _skill(data, 'pending_fixture', version='0.4.0')
    _receipt(data, 'pending_fixture', '0.4.0', compute_content_hash(pending), 61)
    _skill(data, 'hub_fixture', version='0.2.11', bucket='ouroboroshub')
    _receipt(data, 'hub_fixture', '0.2.11', 'e' * 64, 62, url='javascript:alert(1)')
    _skill(data, 'same_fixture', version='1.0.0', bucket='ouroboroshub')
    fresh = _skill(data, 'fresh_fixture', version='0.1.0')
    _receipt(data, 'fresh_fixture', '0.1.0', compute_content_hash(fresh), 63)
    catalog = _catalog(('lens_fixture', '1.1.3'), ('pending_fixture', '0.3.0'),
                       ('hub_fixture', '0.2.12'), ('same_fixture', '1.0.0'))
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': width, 'height': 900})
            external, posts = [], []
            page.route('https://github.com/**', lambda route: (external.append(route.request.url), route.abort()))
            page.route('**/api/marketplace/ouroboroshub/catalog*', lambda route: route.fulfill(json=catalog))
            _stub_mutations(page, posts)
            # The one client route: a phone-width sidebar lives in a closed drawer.
            page.goto(f'{url}/#skills', wait_until='domcontentloaded')
            page.wait_for_selector('.skills-card[data-skill="lens_fixture"]')
            page.click('.skills-tab[data-tab="ouroboroshub"]')
            results = page.locator('#oh-results')
            settled = '4 official skills · 1 local submission not in the catalog'
            expect(page.locator('#oh-status')).to_have_text(settled)
            card = lambda name: results.locator(f'[data-slug="{name}"]')
            # Both mismatch directions offer the catalog copy.
            expect(card('lens_fixture').locator('[data-oh-action="adopt"]')).to_have_text('Use Hub version v1.1.3')
            expect(card('pending_fixture').locator('[data-oh-action="adopt"]')).to_have_text('Use Hub version v0.3.0')
            expect(card('hub_fixture').locator('[data-oh-action="update"]')).to_have_text('Update v0.2.12')
            expect(card('fresh_fixture')).to_contain_text('Not in the Hub catalog')
            expect(card('fresh_fixture').locator('.skills-badge', has_text='official')).to_have_count(0)
            expect(card('lens_fixture').locator('.skills-badge', has_text='official')).to_have_count(1)
            expect(card('fresh_fixture').locator('[data-oh-action]')).to_have_count(0)
            expect(results).not_to_contain_text('Submitted PR')
            overflow = page.evaluate("() => { const s = document.querySelector('.skills-scroll'); return s.scrollWidth - s.clientWidth; }")
            assert overflow <= 1, overflow
            _screenshot(page, f'hub-tab-{engine}-{width}.png')

            # Hub-tab Update: Escape and Cancel post nothing; Confirm posts once.
            update = card('hub_fixture').locator('[data-oh-action="update"]')
            update.click()
            dialog = page.locator('.confirm-dialog')
            expect(dialog).to_contain_text('Replace the local files of hub_fixture, including any local edits, with the current OuroborosHub copy?')
            expect(dialog).to_contain_text('may need access granted again')
            dialog.locator('summary').click()
            expect(dialog).to_contain_text('Last seen in Hub')
            expect(dialog).to_contain_text('v0.2.12')
            _screenshot(page, f'hub-update-confirm-{engine}-{width}.png')
            page.keyboard.press('Escape')
            expect(dialog).to_have_count(0)
            update.click()
            dialog.locator('.marketplace-modal-actions [data-confirm-cancel]').click()
            expect(dialog).to_have_count(0)
            assert posts == []
            update.click()
            dialog.locator('[data-confirm-ok]').click()
            _wait_for_posts(page, posts, 1)
            assert [post['url'].rsplit('/api/', 1)[1] for post in posts] == ['marketplace/ouroboroshub/update/hub_fixture']
            # The action's own refresh re-reads the listing after the enabled button re-renders.
            expect(page.locator('#oh-status')).to_have_text(settled)
            expect(update).to_be_enabled()

            # Use Hub version: the dialog names replacement; the CAS carries the bytes shown.
            shown_hash = compute_content_hash(lens)
            adopt = card('lens_fixture').locator('[data-oh-action="adopt"]')
            adopt.click()
            expect(dialog).to_contain_text('Use Hub version of lens_fixture')
            expect(dialog).to_contain_text('Replace the local copy (external, v1.1.2), including any local edits, with the current OuroborosHub copy?')
            expect(dialog).not_to_contain_text('grants and review history are kept')
            _screenshot(page, f'hub-adopt-confirm-{engine}-{width}.png')
            dialog.locator('.marketplace-modal-actions [data-confirm-cancel]').click()
            assert len(posts) == 1
            adopt.click()
            expect(dialog).to_contain_text('Use Hub version of lens_fixture')
            # Edited while this confirmation is open: a refresh that re-reads the new bytes does not retarget it.
            (lens / 'scripts/check.py').write_text("print('edited before adopt')\n")
            page.evaluate("() => document.getElementById('skills-pane-ouroboroshub')._ouroboroshubRefresh()")
            expect(card('lens_fixture')).to_contain_text('Local files differ from the submitted copy')
            dialog.locator('[data-confirm-ok]').click()
            _wait_for_posts(page, posts, 2)
            expect(page.locator('#oh-status')).to_have_text(settled)
            expect(adopt).to_be_enabled()
            assert posts[1]['body'] == {'slug': 'lens_fixture', 'adopt': True,
                                        'expected_content_hash': shown_hash, 'auto_review': True}
            assert compute_content_hash(lens) != shown_hash, 'the backend CAS refuses these changed bytes'

            # Quiet history: keyboard-openable, safe link only, unsafe URL stays text.
            summary = card('lens_fixture').locator('details[data-oh-history] summary')
            summary.focus()
            page.keyboard.press('Enter')
            expect(card('lens_fixture').locator('details[data-oh-history]')).to_have_attribute('open', '')
            assert card('lens_fixture').locator('details[data-oh-history] a').get_attribute('href') == 'https://github.com/hub/project/pull/60'
            card('hub_fixture').locator('details[data-oh-history] summary').click()
            expect(card('hub_fixture')).to_contain_text('Submitted v0.2.11 · PR #62 · Local files differ from the submitted copy')
            expect(card('hub_fixture').locator('details[data-oh-history] a')).to_have_count(0)
            _screenshot(page, f'hub-history-{engine}-{width}.png')
            page.fill('#oh-query', 'fresh')
            expect(page.locator('#oh-status')).to_have_text('0 official skills · 1 local submission not in the catalog')
            expect(results.locator('[data-slug]')).to_have_count(1)
            card('fresh_fixture').locator('details[data-oh-history] summary').click()
            expect(card('fresh_fixture')).to_contain_text('Submitted v0.1.0 · PR #63')
            _screenshot(page, f'hub-local-submission-{engine}-{width}.png')
            page.fill('#oh-query', '')
            expect(page.locator('#oh-status')).to_have_text(settled)

            # My skills menu Update: same confirmation, same zero/one POST contract.
            page.click('.skills-tab[data-tab="installed"]')
            # Tab entry re-reads the list and replaces its cards: open details on the settled card.
            expect(page.locator('#skills-refresh')).to_be_enabled()
            own = page.locator('.skills-card[data-skill="hub_fixture"]')
            own.locator('.skills-details > summary').click()
            expect(own.locator('.skills-details')).to_contain_text('Submitted v0.2.11 · PR #62')
            # The opened details run past a phone-width fold: the evidence is the history row itself.
            history_row = own.locator('.skills-details .skills-detail-row', has_text='Submitted v0.2.11 · PR #62')
            expect(history_row).to_have_count(1)
            history_row.scroll_into_view_if_needed()
            expect(history_row).to_be_in_viewport()
            _screenshot(page, f'my-skills-history-{engine}-{width}.png')
            for name, seen in [('hub_fixture', 'v0.2.12'), ('same_fixture', 'v1.0.0')]:
                page.locator(f'.skills-card[data-skill="{name}"] [data-skill-menu-trigger]').click()
                page.locator(f'.skills-menu-item.skills-update[data-skill="{name}"]').click()
                expect(dialog).to_contain_text(f'Replace the local files of {name}, including any local edits')
                dialog.locator('summary').click()
                expect(dialog).to_contain_text(seen)
                _screenshot(page, f'my-skills-update-confirm-{name}-{engine}-{width}.png')
                dialog.locator('.marketplace-modal-actions [data-confirm-cancel]').click()
                expect(dialog).to_have_count(0)
            assert len(posts) == 2, 'Cancel from My skills posts nothing'
            page.locator('.skills-card[data-skill="same_fixture"] [data-skill-menu-trigger]').click()
            page.locator('.skills-menu-item.skills-update[data-skill="same_fixture"]').click()
            dialog.locator('[data-confirm-ok]').click()
            _wait_for_posts(page, posts, 3)
            expect(page.locator('.toast', has_text='same_fixture: updated')).to_have_count(1)
            assert [post['url'].rsplit('/api/', 1)[1] for post in posts[2:]] == ['marketplace/ouroboroshub/update/same_fixture']
            assert not external
        finally:
            browser.close()
