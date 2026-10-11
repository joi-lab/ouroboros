"""The actual Updates consumer stays informative through a pending apply."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET, _emit_ws_frame

pytest_plugins = ("tests.test_ui_smoke_playwright",)


@pytest.mark.ui_browser
@pytest.mark.parametrize("width", [1280, 390])
def test_update_progress_pending_reopen_and_failure(direct_server, tmp_path, width):
    from playwright.sync_api import expect, sync_playwright

    plan = {
        "available": True, "kind": "clean", "local_dirty_count": 0,
        "base_sha": "a" * 40, "target_sha": "b" * 40,
        "code_conflict_paths": [], "doc_conflict_paths": [], "hot_code_paths": [],
    }
    status = {
        "managed": True, "check_ok": True, "available": True, "safe_to_apply": True,
        "current_version": "7.0.0", "latest_version": "7.0.0",
        "current_short_sha": "aaaaaaaa", "latest_short_sha": "bbbbbbbb",
        "update_tx": {"active": False}, "warnings": [],
    }
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900})
        pending_apply = []
        try:
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})();")
            def source_install_state(route):
                response = route.fetch()
                # This test owns pending-request recovery, not served-SHA
                # reload policy (covered by the WS reconnect unit tests). Use the
                # supported unversioned-source state during forced reconnect.
                route.fulfill(response=response, json={**response.json(), "sha": ""})

            page.route("**/api/state", source_install_state)
            page.route("**/api/update/status**", lambda route: route.fulfill(json=status))
            page.route("**/api/update/preflight", lambda route: route.fulfill(json={"merge_plan": plan}))
            page.route("**/api/update/apply", lambda route: pending_apply.append(route))
            page.goto(direct_server, wait_until="domcontentloaded")
            page.wait_for_selector("#page-chat")
            page.wait_for_function("window.__testSockets?.some(s => s.readyState === 1)")
            if width < 980:
                page.click("[data-mobile-nav-toggle]")
            page.click('[data-nav-page="dashboard"]')
            page.click('[data-dashboard-tab="updates"]')
            page.click("#btn-update-primary")
            page.click("[data-confirm-ok]")
            expect(page.locator("#updates-summary")).to_have_text("Applying the update…")
            page.screenshot(path=str(evidence / f"updates-{width}-before.png"), full_page=True)
            # Reconnect while the first apply is pending but the server has not
            # begun its observable executor yet. It must not offer another Apply.
            with page.expect_response("**/api/update/status"):
                page.evaluate("window.__testSockets[0].close()")
                page.wait_for_function("window.__testSockets.length > 1 && window.__testSockets.at(-1).readyState === 1")
            page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
            expect(page.locator("#updates-summary")).to_have_text("Applying the update…")
            expect(page.locator("#btn-update-primary")).to_be_disabled()
            page.evaluate("window.__testSockets = [window.__testSockets.at(-1)]")
            page.click('[data-dashboard-tab="logs"]')
            page.click('[data-dashboard-tab="updates"]')
            expect(page.locator("#updates-summary")).to_have_text("Applying the update…")
            expect(page.locator("#btn-update-primary")).to_be_disabled()
            status["update_progress"] = {
                "operation_id": "pending-apply", "generation": "server-a",
                "stage": "stopping_workers", "active": True,
                "stage_started_at": datetime.now(timezone.utc).isoformat(),
            }
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            expect(page.locator("#updates-summary")).to_have_text("Stopping worker processes…")
            expect(page.locator("#btn-update-primary")).to_be_disabled()
            page.screenshot(path=str(evidence / f"updates-{width}-after.png"), full_page=True)

            page.click('[data-dashboard-tab="logs"]')
            page.click('[data-dashboard-tab="updates"]')
            expect(page.locator("#updates-summary")).to_have_text("Stopping worker processes…")
            status["update_progress"].update(stage="checking")
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            expect(page.locator("#updates-summary")).to_have_text("Checking the update and dependencies…")

            # A disconnected apply client cannot erase server-owned progress.
            assert pending_apply, "the original apply stayed pending while progress changed"
            for route in pending_apply:
                route.abort()
            pending_apply.clear()
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("window.__testSockets?.some(s => s.readyState === 1)")
            if width < 980:
                page.click("[data-mobile-nav-toggle]")
            page.click('[data-nav-page="dashboard"]')
            page.click('[data-dashboard-tab="updates"]')
            expect(page.locator("#updates-summary")).to_have_text("Checking the update and dependencies…")
            status["update_progress"].update(active=False, result="failed", restart_required=True, error="A service could not be stopped.")
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            expect(page.locator("#updates-summary")).to_have_text("The update did not complete.")
            expect(page.locator("#btn-update-primary")).to_have_text("Restart now")
            page.evaluate("() => Promise.all(document.getAnimations().filter(a => a.effect?.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})))")
            page.screenshot(path=str(evidence / f"updates-{width}-failure.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        finally:
            for route in pending_apply:
                route.abort()
            browser.close()


@pytest.mark.ui_browser
def test_successful_recheck_keeps_release_tags_when_failure_ack_refreshes_status(direct_server):
    """The acknowledgement notice must not discard the explicit check's tags."""
    from playwright.sync_api import expect, sync_playwright

    status = {
        "managed": True, "check_ok": True, "available": True, "safe_to_apply": True,
        "update_tx": {"active": False}, "official_tags": [],
        "update_progress": {"operation_id": "failed-attempt", "active": False,
                            "result": "failed", "error": "The target changed."},
    }
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        pending_check = []
        try:
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})();")
            page.route("**/api/update/status**", lambda route: route.fulfill(json=status))

            page.route("**/api/update/check", lambda route: pending_check.append(route))
            page.goto(direct_server, wait_until="domcontentloaded")
            page.wait_for_selector("#page-chat")
            page.wait_for_function("window.__testSockets?.some(s => s.readyState === 1)")
            page.click('[data-nav-page="dashboard"]')
            page.click('[data-dashboard-tab="updates"]')
            expect(page.locator("#btn-update-primary")).to_have_text("Check for updates")
            page.click("#btn-update-primary")
            expect(page.locator("#updates-summary")).to_have_text("Checking the official channel…")
            expect(page.locator("#btn-update-primary")).to_be_disabled()
            status["update_progress"] = {}
            # Real api_update_check publishes this before returning its fresh
            # payload; the follow-up passive read carries no tags.
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            assert len(pending_check) == 1
            pending_check.pop().fulfill(json={**status, "official_tags": [{"tag": "v7.0.0-test", "sha": "a" * 40}]})
            expect(page.locator("#updates-official-tags")).to_contain_text("v7.0.0-test")
        finally:
            for route in pending_check:
                route.abort()
            browser.close()


@pytest.mark.serial
@pytest.mark.ui_browser
@pytest.mark.parametrize('failed_stage', ['preflight', 'apply'])
def test_definite_failed_request_releases_local_phase(direct_server, failed_stage):
    from playwright.sync_api import sync_playwright, expect
    status = {'managed': True, 'check_ok': True, 'available': True, 'safe_to_apply': True,
              'current_version': '7.0.0', 'latest_version': '7.0.0',
              'current_short_sha': 'aaaaaaaa', 'latest_short_sha': 'bbbbbbbb',
              'update_tx': {'active': False}, 'update_progress': {}, 'warnings': []}
    plan = {'available': True, 'kind': 'clean', 'local_dirty_count': 0,
            'base_sha': 'a' * 40, 'target_sha': 'b' * 40,
            'code_conflict_paths': [], 'doc_conflict_paths': [], 'hot_code_paths': []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        try:
            page.route('**/api/update/status**', lambda route: route.fulfill(json=status))
            page.route('**/api/update/preflight', lambda route: route.fulfill(
                status=409 if failed_stage == 'preflight' else 200,
                json={'error': 'network check failed'} if failed_stage == 'preflight' else {'merge_plan': plan}))
            page.route('**/api/update/apply', lambda route: route.fulfill(
                status=409, json={'error': 'the update changed after preflight; check again', 'reason': 'release_moved'}))
            page.goto(direct_server, wait_until='domcontentloaded')
            page.wait_for_selector('#page-chat')
            page.click('[data-nav-page="dashboard"]')
            page.click('[data-dashboard-tab="updates"]')
            expect(page.locator('#btn-update-primary')).to_be_enabled()
            if failed_stage == 'preflight':
                with page.expect_response('**/api/update/status'):
                    page.click('#btn-update-primary')
            else:
                page.click('#btn-update-primary')
                with page.expect_response('**/api/update/status'):
                    page.click('[data-confirm-ok]')
            page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
            expect(page.locator('#btn-update-primary')).to_be_enabled(timeout=2000)
        finally:
            browser.close()
