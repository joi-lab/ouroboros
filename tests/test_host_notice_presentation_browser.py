"""Owner-facing host notices through the real SPA and the real server (#1369).

The served page is the real ``web/`` tree behind the real ``server.py`` with the
isolated stub model; only the socket frames are authored by the test, through
the same decoder every live row uses. What a person can see is asserted: the
late-review row reads in words with no slot id, verdict token or omission
marker, its link downloads the exact applied review record the host published
(bytes that hash to the recorded digest), a forged pointer buys no bytes, a
running card's status chip says Working while its title carries no copy of the
word, a reload replays the durable row with the same link, and a settled
acceptance panel without a verdict heads its Reviews group in words while the
attempt detail keeps the stored DEGRADED and the raw provider error.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

pytest_plugins = ("tests.test_ui_smoke_playwright",)

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

LATE_TS = "2026-09-28T00:06:00+00:00"

# The record link's painted colour beside the chat link ink it should wear: a
# bare anchor paints the browser's default link blue instead of `--accent-light`.
_LINK_INK = """(link) => { const ink = document.createElement('span'); ink.style.color = 'var(--accent-light)';
    link.parentElement.appendChild(ink); const want = getComputedStyle(ink).color; ink.remove();
    return [getComputedStyle(link).color, want]; }"""


def _late_row(task_id, note, evidence, *, row_id="acceptance-late:rk"):
    return {"type": "chat", "role": "system", "system_type": "acceptance_late_settlement", "task_id": task_id,
            "chat_id": 1, "card_row": "reviews", "card_row_id": row_id, "content": note, "ts": LATE_TS,
            **({"late_evidence": evidence} if evidence is not None else {})}


def _progress(task_id, text, *, narration=None):
    frame = {"type": "chat", "role": "assistant" if narration else "system", "is_progress": True,
             "task_id": task_id, "chat_id": 1, "content": text, "ts": "2026-09-28T00:00:00+00:00"}
    if narration is not None:
        frame["narration"] = narration
    return frame


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_late_review_row_reads_in_words_and_downloads_its_record(direct_server_with_data, engine):
    playwright_api = pytest.importorskip("playwright.sync_api")

    from ouroboros import loop, review_projection
    from ouroboros.acceptance_settlement import _late_settlement_text, late_evidence_fact
    from ouroboros.task_results import load_task_result, write_task_result
    from supervisor import message_bus
    from supervisor.message_bus import append_jsonl
    from tests.test_acceptance_publication import _context, _run
    from tests.ui_chat_viewport_smoke import (
        _CAPTURE_TEST_SOCKET, _OBSERVE_STATE_READS, _emit_ws_frame, _wait_socket_open_quiescent,
    )

    root = direct_server_with_data["data_dir"]
    url = direct_server_with_data["url"]
    evidence_dir = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(root.parent)))
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # --- the real producers: a published acceptance panel and the owner row -----------
    ctx = _context(root)
    run = {**_run(), "task_attempt": ctx.task_attempt,
           "slot_roster": [{"slot_id": "s1", "model": "fixture/reviewer", "route": "agent_session"}]}
    # The engine's final-attempt report is what names the model that answered.
    run["actors"][0].update(model="fixture/reviewer", operation_state="settled",
                            usage={"resolved_model": "fixture/reviewer-2026-09",
                                   "observed_attempt": {"harness_id": "codex", "model": "fixture/reviewer-2026-09"}})
    run["actors"][0]["parsed"]["summary"] = "Every claim is backed by the ledger. " * 16
    trace = {"review_runs": [run]}
    write_task_result(root, ctx.task_id, "completed", chat_id=1, result="The delivered answer.")
    loop._set_acceptance_decision(trace, {"status": "accepted", "reason": "clean_pass"})
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    panel = load_task_result(root, ctx.task_id)["review_projection"]["panels"][0]
    ref = panel["applied_source_ref"]
    wave = {"slots": {"s1": "ok"}, "verdicts": {"s1": {"verdict": "PASS", "note": "x"}}, "total": 1}
    fact = late_evidence_fact(run, {"source": "terminal_delivery_registry", "state": "unknown", "delivered": []},
                              settled_at=LATE_TS)
    note = _late_settlement_text(run, wave, fact)
    assert note.startswith("On the reviewed version of this answer")
    assert "fixture/reviewer-2026-09: passed it" in note and note.endswith("… (shortened)")
    assert "review record" not in note, "a reviewer's cut promises no record; the row's link is the pointer"
    evidence = {"task_id": ctx.task_id, "panel_id": panel["panel_id"], "settled_at": LATE_TS,
                "reviewed_revision": fact["reviewed_revision"], "reviewed_is_emitted": fact["reviewed_is_emitted"],
                "source_ref": ref}
    # A well-formed pointer at a source this task never published: the client can
    # only judge the shape, the server judges the identity and serves nothing.
    forged = {**evidence, "source_ref": {**ref, "sha256": "f" * 64,
                                         "path": f"source_handles/context_checkpoints/acceptance-{'f' * 64}.json"}}
    malformed = {**evidence, "source_ref": {**ref, "root": "runtime_data"}}

    # A late-review row can outlive its task card. Keep the source task outside
    # Main's result-card lens while placing its test-authored notice in Main,
    # so this exercises the card-less fallback rather than the card-row path.
    cardless_ctx = _context(root)
    cardless_ctx.task_id = "cardless-review"
    cardless_ctx.current_chat_id = 0
    cardless_run = _run()
    cardless_run["request"]["task_id"] = cardless_ctx.task_id
    cardless_run["panel_id"] = "panel_cardless"
    cardless_trace = {"review_runs": [cardless_run]}
    write_task_result(root, cardless_ctx.task_id, "completed", chat_id=0, result="The delivered card-less answer.")
    loop._set_acceptance_decision(cardless_trace, {"status": "accepted", "reason": "clean_pass"})
    review_projection.publish_acceptance_checkpoint(cardless_ctx, cardless_trace)
    cardless_panel = load_task_result(root, cardless_ctx.task_id)["review_projection"]["panels"][0]
    cardless_ref = cardless_panel["applied_source_ref"]
    cardless_fact = late_evidence_fact(
        cardless_run, {"source": "terminal_delivery_registry", "state": "unknown", "delivered": []},
        settled_at=LATE_TS,
    )
    cardless_note = _late_settlement_text(cardless_run, wave, cardless_fact)
    cardless_evidence = {
        "task_id": cardless_ctx.task_id, "panel_id": cardless_panel["panel_id"], "settled_at": LATE_TS,
        "reviewed_revision": cardless_fact["reviewed_revision"],
        "reviewed_is_emitted": cardless_fact["reviewed_is_emitted"], "source_ref": cardless_ref,
    }

    def write_durable_rows():
        """The rows a reload replays: the worker's progress row and the host's late row."""
        append_jsonl(root / "logs" / "progress.jsonl", {
            "ts": "2026-09-28T00:00:00+00:00", "type": "send_message", "task_id": ctx.task_id, "is_progress": True,
            "direction": "out", "chat_id": 1, "user_id": 0, "text": "💬 Checking the ledger",
            "content": "Checking the ledger", "format": "", "role": "assistant", "system_type": "", "narration": True})
        message_bus.log_chat("system", 1, 0, note, ts=LATE_TS, record_type="acceptance_late_settlement",
                             task_id=ctx.task_id, drive_root=root,
                             message_meta={"card_row": "reviews", "card_row_id": "acceptance-late:rk",
                                           "late_evidence": evidence})
        message_bus.log_chat(
            "system", 1, 0, cardless_note, ts=LATE_TS, record_type="acceptance_late_settlement",
            task_id=cardless_ctx.task_id, drive_root=root,
            message_meta={"card_row": "reviews", "card_row_id": "acceptance-late:cardless",
                          "late_evidence": cardless_evidence},
        )

    def open_page(target):
        # The initial history read must land before any test-authored frame, and
        # the socket-open census must settle so a live card is not concluded by absence.
        with target.expect_response(lambda response: "/api/chat/history" in response.url):
            target.goto(url, wait_until="domcontentloaded")
        target.wait_for_selector("#chat-input", state="attached")
        _wait_socket_open_quiescent(target)

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = getattr(playwright, engine).launch()
        except playwright_api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip(f"{engine} is not installed: {exc}")
            raise
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
            page.add_init_script(f"({_OBSERVE_STATE_READS})()")
            open_page(page)

            # --- the same late-review row without a card, live -----------------------------
            _emit_ws_frame(page, _late_row(cardless_ctx.task_id, cardless_note, cardless_evidence,
                                           row_id="acceptance-late:cardless"))
            cardless_bubble = page.locator(
                f'.chat-bubble.system[data-task-id="{cardless_ctx.task_id}"]',
                has_text="reviewers later passed it.",
            )
            try:
                cardless_bubble.wait_for(state="visible", timeout=5000)
            except playwright_api.TimeoutError:
                raise AssertionError({
                    "bubbles": page.locator(".chat-bubble.system").all_inner_texts()[-8:],
                    "cards": page.locator(".chat-live-card").all_inner_texts()[-8:],
                    "pageerrors": errors,
                }) from None
            cardless_link = cardless_bubble.get_by_role("link", name="Download the review record")
            cardless_link.wait_for(state="visible")
            cardless_href = cardless_link.get_attribute("href")
            assert cardless_href and cardless_href.startswith(
                f"/api/tasks/{cardless_ctx.task_id}/artifacts/"
            ) and "?source=" in cardless_href
            assert page.request.get(url + cardless_href).status == 200
            painted, ink = cardless_link.evaluate(_LINK_INK)
            assert painted == ink, (painted, ink)
            cardless_bubble.screenshot(path=str(evidence_dir / f"host-notice-cardless-live-{engine}.png"))

            # --- a running card: the chip says Working, the title repeats nothing -----------
            _emit_ws_frame(page, _progress("turn-live", "Checkpoint saved.", narration=False))
            live = page.locator('.chat-live-card[data-task-id="turn-live"]')
            live.wait_for(state="visible")
            assert live.locator("[data-live-phase]").inner_text().strip() == "Working"
            assert live.locator("[data-live-title]").inner_text().strip() == ""
            _emit_ws_frame(page, _progress("turn-live", "Working through the ledger", narration=True))
            assert live.locator("[data-live-title]").inner_text().strip() == "Working through the ledger"
            assert live.locator("[data-live-phase]").inner_text().strip() == "Working"
            live.screenshot(path=str(evidence_dir / f"host-notice-working-card-{engine}.png"))

            # --- the late-review row, live ----------------------------------------------------
            _emit_ws_frame(page, _progress(ctx.task_id, "Checking the ledger", narration=True))
            card = page.locator(f'.chat-live-card[data-task-id="{ctx.task_id}"]')
            card.wait_for(state="visible")
            _emit_ws_frame(page, _late_row(ctx.task_id, note, evidence))
            card.locator("[data-live-summary-button]").click()
            row = card.locator(".chat-live-line", has_text="reviewers later passed it.")
            row.wait_for(state="visible")
            text = row.inner_text()
            assert "fixture/reviewer-2026-09: passed it" in text
            for internal in ("s1:", "PASS", "DEGRADED", "OMISSION NOTE", "triad_"):
                assert internal not in text, (internal, text)
            assert "… (shortened)" in text
            assert page.locator(f'.chat-bubble.system[data-task-id="{ctx.task_id}"]',
                                has_text="reviewers later passed it.").count() == 0, \
                "the placed row is a card row, not a standalone System bubble"
            link = row.get_by_role("link", name="Download the review record")
            link.wait_for(state="visible")
            painted, ink = link.evaluate(_LINK_INK)
            assert painted == ink, (painted, ink)
            href = link.get_attribute("href")
            assert href and href.startswith(f"/api/tasks/{ctx.task_id}/artifacts/") and "?source=" in href
            response = page.request.get(url + href)
            assert response.status == 200
            raw = response.body()
            assert hashlib.sha256(raw).hexdigest() == ref["sha256"] and len(raw) == ref["size"]
            record = json.loads(raw)
            assert record["request"]["surface"] == "task_acceptance" and record["applied_decision"]["status"] == "accepted"
            card.screenshot(path=str(evidence_dir / f"host-notice-late-review-row-{engine}.png"))
            if engine == "chromium":
                with page.expect_download() as downloaded:
                    link.click()
                download = downloaded.value
                assert download.failure() is None
                saved = evidence_dir / f"downloaded-review-record-{engine}.json"
                download.save_as(str(saved))
                assert hashlib.sha256(saved.read_bytes()).hexdigest() == ref["sha256"]

            # --- forged and malformed pointers buy nothing -----------------------------------
            _emit_ws_frame(page, _late_row(ctx.task_id, note.replace("passed it.", "passed it. (forged)"),
                                           forged, row_id="acceptance-late:forged"))
            forged_row = card.locator(".chat-live-line", has_text="(forged)")
            forged_row.wait_for(state="visible")
            forged_link = forged_row.get_by_role("link", name="Download the review record")
            assert forged_link.count() == 1, "a well-formed pointer renders; only the server can judge its digest"
            assert page.request.get(url + forged_link.get_attribute("href")).status == 404
            _emit_ws_frame(page, _late_row(ctx.task_id, note.replace("passed it.", "passed it. (malformed)"),
                                           malformed, row_id="acceptance-late:malformed"))
            malformed_row = card.locator(".chat-live-line", has_text="(malformed)")
            malformed_row.wait_for(state="visible")
            assert malformed_row.get_by_role("link").count() == 0, "a pointer of another shape offers no link"

            # --- reload: the durable row replays on its card with the same link ---------------
            write_durable_rows()
            open_page(page)
            replayed_card = page.locator(f'.chat-live-card[data-task-id="{ctx.task_id}"]')
            replayed_card.wait_for(state="visible")
            if replayed_card.get_attribute("data-expanded") != "1":
                replayed_card.locator("[data-live-summary-button]").click()
            replayed = replayed_card.locator(".chat-live-line", has_text="reviewers later passed it.")
            replayed.wait_for(state="visible")
            replayed_link = replayed.get_by_role("link", name="Download the review record")
            replayed_link.wait_for(state="visible")
            assert replayed_link.get_attribute("href") == href
            assert hashlib.sha256(page.request.get(url + href).body()).hexdigest() == ref["sha256"]
            replayed_card.screenshot(path=str(evidence_dir / f"host-notice-late-review-replayed-{engine}.png"))
            cardless_replayed = page.locator(
                f'.chat-bubble.system[data-task-id="{cardless_ctx.task_id}"]',
                has_text="reviewers later passed it.",
            )
            cardless_replayed.wait_for(state="visible")
            cardless_replayed_link = cardless_replayed.get_by_role(
                "link", name="Download the review record",
            )
            cardless_replayed_link.wait_for(state="visible")
            assert cardless_replayed_link.get_attribute("href") == cardless_href
            assert page.request.get(url + cardless_href).status == 200
            cardless_replayed.screenshot(
                path=str(evidence_dir / f"host-notice-cardless-replayed-{engine}.png"),
            )
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_settled_no_verdict_panel_heads_in_words_and_keeps_degraded_in_detail(direct_server_with_data, engine):
    playwright_api = pytest.importorskip("playwright.sync_api")

    from ouroboros import review_projection
    from ouroboros.task_results import load_task_result, write_task_result
    from tests.test_acceptance_publication import _context, _run
    from tests.ui_chat_viewport_smoke import (
        _CAPTURE_TEST_SOCKET, _OBSERVE_STATE_READS, _emit_ws_frame, _wait_socket_open_quiescent,
    )

    root = direct_server_with_data["data_dir"]
    url = direct_server_with_data["url"]
    evidence_dir = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(root.parent)))
    evidence_dir.mkdir(parents=True, exist_ok=True)

    # The real producer settles a panel with no quorum: one reviewer's own DEGRADED
    # answer, one PASS and one provider failure, on a task that has already ended.
    ctx = _context(root)
    run = {**_run(), "task_attempt": ctx.task_attempt, "aggregate_signal": "DEGRADED", "parsed_findings": [],
           "enforcement_impact": "degrades_completion",
           "slot_roster": [{"slot_id": slot, "model": f"fixture/reviewer-{slot}", "route": "api_chat"}
                           for slot in ("s1", "s2", "s3")],
           "actors": [
               {"slot_id": "s1", "model": "fixture/reviewer-s1", "status": "ok", "operation_state": "settled",
                "signal": "DEGRADED", "semantic_verdict": "DEGRADED",
                "parsed": {"verdict": "DEGRADED", "summary": "The ledger could not be opened.", "findings": []}},
               {"slot_id": "s2", "model": "fixture/reviewer-s2", "status": "ok", "operation_state": "settled",
                "signal": "PASS", "semantic_verdict": "PASS",
                "parsed": {"verdict": "PASS", "summary": "Every claim is backed.", "findings": []}},
               {"slot_id": "s3", "model": "fixture/reviewer-s3", "status": "error", "operation_state": "settled",
                "error": "ClaudexorModelError: engine_busy (HTTP 429)"}]}
    write_task_result(root, ctx.task_id, "completed", chat_id=1, result="The delivered answer.")
    assert review_projection.publish_acceptance_checkpoint(ctx, {"review_runs": [run]})["status"] == "published"
    stored = load_task_result(root, ctx.task_id)
    panel = stored["review_projection"]["panels"][0]
    assert stored["status"] == "completed" and panel["aggregate_signal"] == "DEGRADED"

    with playwright_api.sync_playwright() as playwright:
        try:
            browser = getattr(playwright, engine).launch()
        except playwright_api.Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip(f"{engine} is not installed: {exc}")
            raise
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
            page.add_init_script(f"({_OBSERVE_STATE_READS})()")
            with page.expect_response(lambda response: "/api/chat/history" in response.url):
                page.goto(url, wait_until="domcontentloaded")
            page.wait_for_selector("#chat-input", state="attached")
            _wait_socket_open_quiescent(page)
            # The host's own reference frame puts the Reviews section on the task card.
            _emit_ws_frame(page, {"type": "chat", "role": "system", "system_type": "review_reference",
                                  "surface": "task_acceptance", "task_id": ctx.task_id,
                                  "chat_id": 1, "state_revision": "b" * 64})
            card = page.locator(f'.chat-live-card[data-task-id="{ctx.task_id}"]')
            card.wait_for(state="visible")
            if card.get_attribute("data-expanded") != "1":
                card.locator("[data-live-summary-button]").click()
            card.locator("[data-review-section-toggle]").click()
            meta = card.locator(".chat-review-group-meta")
            meta.wait_for(state="visible")
            # The group head: the outcome in words, then its attempt count.
            assert meta.inner_text().strip() == "no verdict · 2 of 3 answered · 1 unavailable · 1"
            assert "warn" in (card.locator("[data-review-group]").get_attribute("class") or "")
            card.locator("[data-review-group-toggle]").click()
            attempt_meta = card.locator(".chat-review-attempt-meta")
            attempt_meta.wait_for(state="visible")
            assert "no verdict · 2 of 3 answered · 1 unavailable" in attempt_meta.inner_text()
            for header in (meta, attempt_meta):
                assert "DEGRADED" not in header.inner_text()
            card.locator("[data-review-attempt-toggle]").click()
            detail = card.locator("[data-review-attempt-detail]")
            detail.wait_for(state="visible")
            detail_text = detail.inner_text()
            # The stored verdict and the provider's raw error stay in the detail.
            assert "verdict=DEGRADED" in detail_text
            assert "ClaudexorModelError: engine_busy (HTTP 429)" in detail_text
            card.screenshot(path=str(evidence_dir / f"host-notice-no-verdict-reviews-{engine}.png"))
            assert not errors, errors
        finally:
            browser.close()
