"""A real worker's unreadable billing authority → technical pause → SPA Resume.

Only the fault is injected: a malformed cap-amendment field on this fixture's
isolated task. The worker, pause publication, restart retention, strict Resume,
model sends, history and browser rendering are production consumers. No provider
or installation state is touched. This is not an opaque vendor-session test.
"""
from __future__ import annotations

import json

import pytest

from tests import test_batch4_owner_controls_browser as flow
from tests import test_ui_smoke_playwright as smoke


direct_server_with_data = smoke.direct_server_with_data
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_authority_pause_rejects_unrepaired_resume_then_returns_same_work(
    direct_server_with_data, monkeypatch, engine,
):
    from playwright.sync_api import expect, sync_playwright
    from ouroboros.task_store import patch_document

    server = direct_server_with_data
    url, data = server["url"], server["data_dir"]
    evidence = flow._evidence_dir(data, f"authority-{engine}")
    model = flow._ScriptedModel(monkeypatch)
    server["stop_server"]()
    settings_path = data / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["OUROBOROS_TASK_REVIEW_MODE"] = "off"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    flow._seed_roots(data, (flow.ALPHA,))
    server["start_server"]()
    flow._resume_after_app_stop(url, flow.ALPHA)
    try:
        assert model.alpha_sent.wait(90), "the real worker never reached its first send"
        # The fault arrives AFTER a valid first admission, before the next one.
        patch_document(data, flow.ALPHA, lambda current: {
            **current, "acceptance_root_cap_amendments": [{"new_cap_usd": "broken"}],
        })
        model.alpha_release.set()
        paused = flow._wait(
            lambda: (lambda row: row if row.get("budget_pause", {}).get("state") == "paused" else None)(
                flow._http(url, "GET", f"/api/tasks/{flow.ALPHA}")),
            120, "the real technical pause",
        )
        assert paused["budget_pause"]["reason"] == "authority"
        assert paused["budget_pause"]["scope"] == "task"
        assert model.main_calls("ALPHA") == 1, "unreadable authority admitted another send"
        assert flow._fence(data, flow.ALPHA).get("state") not in {"requested", "paused"}
        before = paused["budget_pause"]
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 722})
            page.add_init_script(f"({flow._CAPTURE_TEST_SOCKET})()")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                flow._open_chat(page, url)
                expect(flow._chip(page, flow.ALPHA)).to_have_text("Paused · technical issue", timeout=30_000)
                resume = flow._card(page, flow.ALPHA).locator("[data-resume-run]")
                expect(resume).to_be_visible()
                page.screenshot(path=str(evidence / "01-technical-pause.png"))
                # The UI invokes the real Resume endpoint while authority is still bad.
                with page.expect_response(lambda response: response.url.endswith(f"/api/tasks/{flow.ALPHA}/resume")) as response:
                    resume.click()
                assert response.value.status == 409
                refused = response.value.json()
                assert refused["code"] == "billing_authority_unavailable"
                expect(page.locator(".toast").last).to_contain_text("could not be read")
                expect(page.locator(".toast").last).to_contain_text("Fix that cause")
                assert model.main_calls("ALPHA") == 1
                assert "Increase" not in page.locator("body").inner_text()
                page.screenshot(path=str(evidence / "02-resume-refused.png"))
                # A stopped fixture generation tests the durable carrier, not a DOM-only replay.
                server["restart_server"]()
                page.reload()
                flow._open_chat(page, url)
                expect(flow._chip(page, flow.ALPHA)).to_have_text("Paused · technical issue", timeout=60_000)
                retained = flow._http(url, "GET", f"/api/tasks/{flow.ALPHA}")["budget_pause"]
                assert retained["pause_id"] == before["pause_id"]
                assert retained["reason"] == "authority" and model.main_calls("ALPHA") == 1
                page.screenshot(path=str(evidence / "03-retained-after-restart.png"))
                # Repair just the injected fault; do not manufacture a grant or rewrite money.
                patch_document(data, flow.ALPHA, lambda current: {**current, "acceptance_root_cap_amendments": []})
                flow._card(page, flow.ALPHA).locator("[data-resume-run]").click()
                complete = flow._wait(
                    lambda: (lambda row: row if row.get("status") == "completed" else None)(
                        flow._http(url, "GET", f"/api/tasks/{flow.ALPHA}")),
                    120, "the same author completing after explicit Resume",
                )
                assert complete["root_task_id"] == flow.ALPHA and not complete.get("continued_by")
                assert complete["budget_pause"]["state"] == "resumed"
                assert model.main_calls("ALPHA") == 2 and model.main_calls("SUCCESSOR") == 0
                assert not flow._events(data, "owner_continue_admitted")
                expect(flow._chip(page, flow.ALPHA)).to_have_text("Done", timeout=60_000)
                page.screenshot(path=str(evidence / "04-same-work-done.png"))
                (evidence / "result.json").write_text(json.dumps({
                    "engine": engine, "pause_id": before["pause_id"], "refusal": refused,
                    "same_task_id": flow.ALPHA, "main_calls": model.main_calls("ALPHA"),
                    "successor_calls": model.main_calls("SUCCESSOR"), "page_errors": errors,
                }, indent=2), encoding="utf-8")
                assert errors == []  # preserve the full result even when a browser error remains
            finally:
                (evidence / "page-errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")
                browser.close()
    finally:
        model.alpha_release.set()
        model.teardown.set()
