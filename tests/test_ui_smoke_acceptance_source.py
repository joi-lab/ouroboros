"""An old full applied review downloads through the live Chat Reviews consumer.

The retired task acceptance published an applied review record beside its panel; nothing
writes one now. A stored legacy panel without a record says so, and when the record and
its panel land (stored here as the publisher left them), a review reference refreshes the
open card to the download link, whose bytes are the record's exact bytes.
"""

import hashlib
import json
import os
from pathlib import Path

import pytest

pytest_plugins = ("tests.test_ui_smoke_playwright",)


def _run(task_id):
    findings = [{"id": f"f{i}", "severity": "low", "item": f"full finding {i}",
                 "evidence": "complete context", "recommendation": "consider later"} for i in range(80)]
    return {"request": {"surface": "task_acceptance", "task_id": task_id},
            "panel_id": "panel_exact", "authority": "host_root", "task_attempt": 1, "aggregate_signal": "PASS",
            "actors": [{"slot_id": "s1", "signal": "PASS", "status": "ok",
                        "parsed": {"verdict": "PASS", "outcome_tier": "solved", "findings": findings}}],
            "parsed_findings": findings, "enforcement_impact": "allows_completion"}


@pytest.mark.ui_browser
@pytest.mark.serial
def test_old_applied_review_download_appears_when_its_reference_refreshes_the_card(direct_server_with_data):
    from playwright.sync_api import sync_playwright

    from ouroboros import artifacts, review_projection
    from ouroboros.task_results import load_task_result, write_task_result
    from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET, _emit_ws_frame

    root, task_id = direct_server_with_data["data_dir"], "applied"
    run = _run(task_id)
    legacy_panel = review_projection.compact_review_projection([run])["panels"][0]
    legacy_panel["task_attempt"] = 1
    legacy_panel["panel_index"] = 0
    write_task_result(root, task_id, "running", result="Work is still in progress.",
                      review_projection={"panels": [legacy_panel]})
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(root.parent)))
    evidence.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            page.wait_for_function("() => window.__testSockets?.[0]?.readyState === 1")
            # Initial history must finish before adding this client-only reference.
            page.wait_for_selector('#chat-messages[data-history-hydrated="true"]')
            _emit_ws_frame(page, {"type": "chat", "role": "system", "system_type": "review_reference",
                                 "surface": "task_acceptance", "task_id": task_id,
                                 "chat_id": 1, "state_revision": "a" * 64})
            card = page.locator(f'.chat-live-card[data-task-id="{task_id}"]')
            card.wait_for(state="visible")
            card.locator("[data-live-disclosure]").click()
            card.locator("[data-review-section-toggle]").click()
            card.locator("[data-review-group-toggle]").click()
            card.locator("[data-review-attempt-toggle]").click()
            detail = card.locator("[data-review-attempt-detail]")
            assert "Full applied review unavailable." in detail.inner_text()

            # The record and its panel, as the retired publisher stored them.
            record = {**run, "applied_decision": {"status": "accepted", "reason": "clean_pass"}}
            ref = artifacts.store_actor_source_bytes(
                root, task_id, category="context_checkpoints", source_id="acceptance",
                data=json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"), extension="json")
            projection = review_projection.compact_review_projection([
                {**record, "panel_index": 0, "publication_revision": 1,
                 "applied_source_ref": ref, "applied_source_status": "available"}])
            projection.update(publication_revision=1, task_attempt=1)
            write_task_result(root, task_id, "running", review_projection=projection)
            stored = load_task_result(root, task_id)
            assert stored["status"] == "running"
            assert "artifacts" not in stored
            # A newer reference revision through Chat's existing socket decoder,
            # hydration and keyed DOM reconciliation.
            _emit_ws_frame(page, {"type": "chat", "role": "system", "system_type": "review_reference",
                                 "surface": "task_acceptance", "task_id": task_id,
                                 "chat_id": 1, "state_revision": "b" * 64})
            link = detail.get_by_role("link", name="Download full applied review")
            link.wait_for(state="visible")
            assert card.locator("[data-review-attempt-toggle]").get_attribute("aria-expanded") == "true"
            assert "Full applied review unavailable." not in detail.inner_text()
            href = link.get_attribute("href")
            response = page.request.get(direct_server_with_data["url"] + href)
            assert response.status == 200
            raw = response.body()
            assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
            full = json.loads(raw)
            assert len(full["actors"][0]["parsed"]["findings"]) == 80
            assert full["applied_decision"]["status"] == "accepted"
            assert load_task_result(root, task_id)["status"] == "running"
            link.scroll_into_view_if_needed()
            card.screenshot(path=str(evidence / "applied-review-link.png"))
            with page.expect_download() as downloaded:
                link.click()
            download = downloaded.value
            assert download.failure() is None
            download.save_as(str(evidence / "downloaded-applied-review.json"))
            assert hashlib.sha256((evidence / "downloaded-applied-review.json").read_bytes()).hexdigest() == ref["sha256"]
        finally:
            browser.close()
