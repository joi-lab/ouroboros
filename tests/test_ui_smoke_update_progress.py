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
            expect(page.locator("#updates-attempt")).to_be_hidden()
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
def test_successful_recheck_keeps_release_tags_and_the_last_attempt(direct_server):
    """A check refreshes availability: it neither clears the retained attempt nor loses its tags."""
    from playwright.sync_api import expect, sync_playwright

    status = {
        "managed": True, "check_ok": True, "available": False, "safe_to_apply": False,
        "current_version": "7.0.0", "current_short_sha": "aaaaaaaa",
        "update_tx": {"active": False}, "official_tags": [],
        "update_progress": {"operation_id": "failed-attempt", "current_generation": True, "active": False,
                            "result": "failed", "outcome": "not_applied", "reason": "release_moved",
                            "work_stop": "none", "error": "The target changed."},
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
            expect(page.locator("#updates-attempt-headline")).to_have_text("No update was applied.")
            expect(page.locator("#btn-update-primary")).to_have_text("Check again")
            page.click("#btn-update-primary")
            expect(page.locator("#updates-summary")).to_have_text("Checking the official channel…")
            expect(page.locator("#btn-update-primary")).to_be_disabled()
            # A concurrent invalidation during the check; its passive read carries no tags.
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            assert len(pending_check) == 1
            pending_check.pop().fulfill(json={**status, "official_tags": [{"tag": "v7.0.0-test", "sha": "a" * 40}]})
            expect(page.locator("#updates-official-tags")).to_contain_text("v7.0.0-test")
            expect(page.locator("#updates-attempt-headline")).to_have_text("No update was applied.")
            expect(page.locator("#updates-attempt-text")).to_contain_text("The update changed after it was prepared.")
            expect(page.locator("#updates-attempt-text")).not_to_contain_text("stopped")
        finally:
            for route in pending_check:
                route.abort()
            browser.close()


@pytest.mark.ui_browser
@pytest.mark.parametrize("width", [1100, 390])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_update_indicator_carries_the_outcome_until_dismissed_and_shows_the_next_attempt(direct_server, tmp_path, width, engine):
    """Owner 3A (#1818): the existing pill shows stage, then outcome; dismissing sends nothing and keeps the panel."""
    from playwright.sync_api import expect, sync_playwright

    refusal = {"operation_id": "attempt-a", "current_generation": True, "active": False, "result": "failed",
               "outcome": "not_applied", "outcome_at": datetime.now(timezone.utc).isoformat(),
               "stage": "draining_writers", "reason": "update_writer_fence_blocked",
               "blockers": ["active:Main conversation"], "work_stop": "none", "concluded_by": "executor", "restart_required": False,
               "error": "Could not prove every repository writer stopped.", "base_sha": "a" * 40, "target_sha": "b" * 40}
    status = {"managed": True, "check_ok": True, "available": True, "safe_to_apply": True,
              "current_version": "7.0.0", "latest_version": "7.1.0",
              "current_short_sha": "aaaaaaaa", "latest_short_sha": "bbbbbbbb",
              "update_tx": {"active": False}, "warnings": [],
              "update_progress": {**refusal, "active": True, "outcome": "", "result": "",
                                  "stage": "stopping_workers", "work_stop": "invoked"}}
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    actions = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 750}, color_scheme="dark" if width == 1100 else "light")
        try:
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})();")
            page.on("request", lambda request: actions.append(request.url) if request.method == "POST" and any(
                path in request.url for path in ("/api/update/", "/api/command", "/api/git/")) else None)
            page.route("**/api/update/status**", lambda route: route.fulfill(json=status))
            page.goto(direct_server, wait_until="domcontentloaded")
            page.wait_for_selector("#page-chat")
            page.wait_for_function("window.__testSockets?.some(s => s.readyState === 1)")
            if width < 980:
                page.locator("[data-mobile-nav-toggle]:visible").click()
            pill = page.locator("#update-pill")
            expect(pill).to_have_text("Stopping work…")
            status["update_progress"] = refusal
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            expect(pill).to_have_text("Update not applied")
            expect(pill).to_have_attribute("data-tone", "error")
            page.screenshot(path=str(evidence / f"update-indicator-{engine}-{width}-outcome.png"))
            if width < 980:
                # Exercise the settled modal drawer, not a click that races its inert boundary.
                page.wait_for_function("document.getElementById('content').inert === true")
            pill.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#dashboard-panel-updates")).to_be_visible()
            expect(page.locator("#updates-attempt-headline")).to_have_text("No update was applied.")
            expect(page.locator("#updates-attempt-text")).to_have_text(
                "Work was still finishing: Main conversation. This attempt stopped no running work. Try again when it finishes.")
            expect(page.locator("#btn-update-primary")).to_have_text("Update to 7.1.0")
            page.locator("#updates-attempt-details summary").click()
            expect(page.locator("#updates-attempt-facts")).to_contain_text("update_writer_fence_blocked")
            page.screenshot(path=str(evidence / f"update-panel-{engine}-{width}-attempt.png"), full_page=True)
            page.locator("#updates-attempt-dismiss").focus()
            page.keyboard.press("Enter")
            expect(page.locator("#updates-attempt-dismiss")).to_be_hidden()
            expect(page.locator("#updates-attempt-headline")).to_have_text("No update was applied.")
            if width < 980:
                page.locator("[data-mobile-nav-toggle]:visible").click()
            expect(pill).to_have_text("Update available")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("window.__testSockets?.some(s => s.readyState === 1)")
            if width < 980:
                page.locator("[data-mobile-nav-toggle]:visible").click()
            expect(pill).to_have_text("Update available")  # the dismissed result stays dismissed after reload
            status["update_progress"] = {**refusal, "operation_id": "attempt-b", "outcome": "unknown",
                                         "reason": "no_completion_record", "work_stop": "", "blockers": []}
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            expect(pill).to_have_text("Update outcome unconfirmed")
            expect(pill).to_have_attribute("data-tone", "neutral")
            if width < 980:
                page.wait_for_function("document.getElementById('content').inert === true")
            box = pill.bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, box
            pill.click()  # Pointer and keyboard enter the same existing panel.
            expect(page.locator("#dashboard-panel-updates")).to_be_visible()
            status["update_progress"]["retention_unconfirmed"] = True
            _emit_ws_frame(page, {"type": "update_progress_changed"})
            expect(page.locator("#updates-attempt-text")).to_contain_text("could not be saved")
            expect(page.locator("#updates-attempt-headline")).to_have_text("The last update outcome is unconfirmed.")
            status['update_progress'].update(outcome='', result='restart_required', restart_required=True)
            _emit_ws_frame(page, {'type': 'update_progress_changed'})
            expect(page.locator('#btn-update-primary')).to_have_text('Restart now')
            expect(page.locator('#updates-attempt-text')).to_be_visible()
            expect(page.locator('#updates-attempt-text')).to_contain_text('could not be saved')
            page.screenshot(path=str(evidence / f'update-panel-{engine}-{width}-retention-warning.png'), full_page=True)
            assert actions == [], actions
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            assert page.locator("#dashboard-panel-updates").evaluate("el => el.scrollWidth <= el.clientWidth")
        finally:
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
