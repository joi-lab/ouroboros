"""Real Chat replay and controls over isolated durable logs, with a controlled census and its work projection."""
from __future__ import annotations

from tests import _dialogue as dialogue_test

import json
import time
import urllib.request

import pytest

from ouroboros import dialogue_store

from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data
from tests._store_shared import seed_result
from tests._work_projection_shared import work_fields
from ouroboros.test_evidence import diagnostic_directory

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_speechless_history_and_direct_resume(direct_server_with_data, engine):
    from playwright.sync_api import sync_playwright
    from ouroboros.task_results import load_task_result

    fixture = direct_server_with_data
    root, task_id, turn_id = fixture["data_dir"], "tool-only-pause", "direct-turn"
    fixture["stop_server"]()
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    dialogue_store.append(root, "chat", {"direction": "in", "chat_id": 1,
        "task_id": task_id, "text": "Inspect the saved sources", "ts": "2026-10-04T09:00:00Z"})
    calls = [{"type": kind, "task_id": task_id, "invocation_id": f"call-{index}",
              "tool": "read_file", "is_error": False, "ts": "2026-10-04T09:00:01Z"}
             for index in range(24) for kind in ("tool_call_started", "tool_call")]
    (logs / "tools.jsonl").write_text("".join(json.dumps(row) + "\n" for row in calls), encoding="utf-8")
    archive = root / "archive"
    archive.mkdir(exist_ok=True)
    for index in range(4):
        (archive / f"tools_2026100{index}.jsonl").write_text(json.dumps({
            **calls[1], "task_id": "unrelated", "invocation_id": f"unrelated-{index}"}) + "\n", encoding="utf-8")
    results = root / "task_results"
    results.mkdir(exist_ok=True)
    # An earlier version saved this answered root's upkeep as the owner's Pause, for a Resume.
    seed_result(root, task_id, {"_schema_version": 1, "task_id": task_id,
        "status": "completed", "suggested_name": "Read source records", "_is_direct_chat": True,
        "root_phase_checkpoint": {"post_task_synthesis": "paused"}})
    # A direct turn still running: its card comes from its own progress row.
    dialogue_store.append(root, "progress", {"task_id": turn_id, "chat_id": 1,
        "content": "Reading the saved sources", "ts": "2026-10-04T09:02:00Z"})
    fixture["start_server"]()
    # A Pause now ends the upkeep at once, so the maintenance pass ends that saved remainder once, disclosed.
    deadline, checkpoint = time.monotonic() + 60, {}
    while time.monotonic() < deadline:
        checkpoint = (load_task_result(root, task_id) or {}).get("root_phase_checkpoint") or {}
        if checkpoint.get("post_task_synthesis") == "degraded":
            break
        time.sleep(0.2)
    assert checkpoint.get("post_task_synthesis") == "degraded", checkpoint
    assert str(checkpoint.get("post_task_stop_reason") or "").startswith("owner_pause:skipped="), checkpoint
    census = {"phase": "budget_pausing", "pause_cause": "owner"}
    with urllib.request.urlopen(fixture["url"] + "/api/state", timeout=10) as response:
        state = json.load(response)

    def state_route(route):
        data = dict(state)
        rows = [{"activity_id": turn_id, "chat_id": 1, "kind": "direct_chat", **census}]
        # The server's own work projection of the controlled row: its word, reason and controls.
        data.update(active_chat_activities=rows, active_chat_activities_complete=True,
                    supervisor_ready=True, **work_fields(rows))
        route.fulfill(json=data)

    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        try:
            for width in (1440, 390):
                context = browser.new_context(viewport={"width": width, "height": 900},
                    is_mobile=width == 390, has_touch=width == 390)
                page = context.new_page()
                page.route("**/api/state", state_route)
                census["phase"] = "budget_pausing"
                page.goto(fixture["url"], wait_until="domcontentloaded")
                card = page.locator(f'.chat-live-card[data-task-id="{task_id}"]')
                card.wait_for(state="visible")
                # The ended upkeep leaves the answer Done, with nothing to resume or stop.
                page.wait_for_function("id => document.querySelector(`.chat-live-card[data-task-id='${id}']`)?.dataset.finished === '1'", arg=task_id)
                assert card.locator("[data-live-phase]").inner_text() == "Done"
                assert card.locator("[data-resume-run]").count() == 0
                assert card.locator("[data-cancel-run]").count() == 0
                assert card.locator("[data-live-title]").inner_text() == "Read source records"
                assert page.locator(".chat-bubble.assistant:not(.typing-bubble)").count() == 0
                assert "24 tool calls" in card.inner_text()
                assert "outcome unknown" not in card.inner_text(), "unrelated archives cannot unsettle known calls"
                # The running direct turn's Pause is still settling: the work's word alone (who paused is its line;
                # this controlled row's press declared nobody).
                turn = page.locator(f'.chat-live-card[data-task-id="{turn_id}"]')
                turn.wait_for(state="visible")
                page.wait_for_function("id => document.querySelector(`.chat-live-card[data-task-id='${id}'] [data-live-phase]`)?.textContent === 'Pausing…'", arg=turn_id)
                assert turn.locator("[data-resume-run]").count() == 0
                assert turn.locator("[data-cancel-run]").is_visible()
                page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / f"task-evidence-{engine}-{width}-pausing.png"), full_page=True)

                census["phase"] = "budget_paused"
                page.reload(wait_until="domcontentloaded")
                resume = turn.locator("[data-resume-run]")
                resume.wait_for(state="visible")
                assert resume.inner_text() == "Resume"
                assert turn.locator("[data-live-phase]").inner_text() == "Paused"
                assert page.locator("#chat-messages").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                assert resume.evaluate("el => { const r=el.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth; }")
                turn.locator("[data-cancel-run]").click()
                assert page.locator('[role="menuitem"]', has_text="Resume").count() == 0
                assert page.locator('[role="menuitem"]', has_text="Stop now").is_visible()
                page.keyboard.press("Escape")
                page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / f"task-evidence-{engine}-{width}-paused.png"), full_page=True)
                card.locator(":scope > [data-live-summary-button] [data-live-disclosure]").click()
                lines = card.locator(".chat-live-line")
                # The tool calls, then the answer's own Done line.
                assert lines.count() == 2, lines.all_inner_texts()
                assert "24 tool calls" in lines.nth(0).inner_text() and lines.nth(1).inner_text().startswith("Done")
                page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / f"task-evidence-{engine}-{width}-expanded.png"), full_page=True)
                tools = lines.nth(0)
                tools.locator("[data-live-line-toggle]").click()
                assert "Only recent tool history was read." in tools.locator(".chat-live-line-body").inner_text()
                page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / f"tool-history-bounded-{engine}-{width}.png"), full_page=True)

                # The controlled projection offers the door; the real server still
                # refuses a turn with no resumable queue/fence authority.
                resume.click()
                page.locator(".toast", has_text="Resume refused").wait_for(state="visible")
                assert turn.locator("[data-live-phase]").inner_text() == "Paused"
                assert card.locator("[data-live-phase]").inner_text() == "Done"
                context.close()
        finally:
            browser.close()


def test_pause_notice_translation_and_historical_wake_status(direct_server_with_data):
    from playwright.sync_api import expect, sync_playwright

    fixture = direct_server_with_data
    root = fixture["data_dir"]
    english = "The task paused because of the owner's Pause. Its work is retained. Use Resume on the task when available."
    translated = "Задача приостановлена владельцем. Работа сохранена. Продолжение доступно в карточке задачи."
    fixture["stop_server"]()
    notices = [{"direction": "system", "type": "task_pause_notice", "task_id": task,
                "card_row": "timeline", "card_row_id": f"pause:{task}:one", "narration": False,
                "text": english, "chat_id": 1, "ts": "2026-10-04T12:00:00Z"}
               for task in ("with-card", "without-card")]
    for row in notices:
        dialogue_store.append(root, "chat", row)
    dialogue_store.append(root, "progress", {"task_id": "with-card", "chat_id": 1,
        "content": "Model words stay unchanged", "ts": "2026-10-04T11:59:00Z"})
    fixture["start_server"]()
    with urllib.request.urlopen(fixture["url"] + "/api/state", timeout=10) as response:
        state = json.load(response)
    status = "wake_paused"
    owner_wait = {"owner_wait_state": "waiting", "quiz_state": "open"}
    details = {"wake_paused": "The last wake-up returned while paused; its task card shows the current state. Next wake check at 18:00.",
               "wake_outcome_unknown": "The last wake-up's outcome is unconfirmed; next check at 18:00."}
    payload = {"language": "ru", "english": False, "entries": {
        english: {"text": translated, "provenance": "imported"},
        "code:consciousness.wake_paused": {"text": "приостановлено", "provenance": "imported"},
        "code:consciousness.wake_outcome_unknown": {"text": "исход неизвестен", "provenance": "imported"}}}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            def rows():
                return [{"activity_id": "with-card", "chat_id": 1, "kind": "managed_task",
                         "phase": "working", "owner_wait": owner_wait, "required_question_unavailable": True}]
            page.route("**/api/state", lambda route: route.fulfill(json={**state, "bg_consciousness_enabled": True,
                "active_chat_activities": rows(), "active_chat_activities_complete": True, **work_fields(rows()),
                "bg_consciousness_state": {"status": status, "detail": details[status]}}))
            page.route("**/api/ui/i18n", lambda route: route.fulfill(json=payload))
            page.goto(fixture["url"], wait_until="domcontentloaded")
            card = page.locator('.chat-live-card[data-task-id="with-card"]')
            card.wait_for(state="visible")
            if card.get_attribute("data-expanded") != "1":
                card.locator(":scope > [data-live-summary-button] [data-live-disclosure]").click()
            expect(card).to_contain_text(translated)
            expect(card).to_contain_text("Model words stay unchanged")
            fallback = page.locator('.chat-bubble.system[data-task-id="without-card"]')
            expect(fallback).to_contain_text(translated)
            assert english in dialogue_test.log_text(root / "logs/chat.jsonl")
            page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / "pause-notice-translated-chat.png"), full_page=True)
            for wait_state, quiz_state, label in (("waiting", "open", "Waiting for your answer"),
                ("waiting", "answered", "Working"), ("resumed", "answered", "Working")):
                owner_wait = {"owner_wait_state": wait_state, "quiz_state": quiz_state}
                page.reload(wait_until="domcontentloaded")
                expect(card.locator("[data-live-phase]")).to_have_text(label)
                expect(page.locator("#chat-status")).not_to_have_text("Activity unconfirmed")
                page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / f"main-wait-{wait_state}-{quiz_state}.png"), full_page=True)
            page.locator('[data-nav-page="dashboard"]').click()
            page.locator('[data-dashboard-tab="evolution"]').click()
            pill = page.locator("#evo-bg-pill")
            for next_status, label in (("wake_paused", "приостановлено"), ("wake_outcome_unknown", "исход неизвестен")):
                status = next_status
                page.locator("#evo-refresh").click()
                expect(pill).to_have_text(f"Consciousness {label}")
                expect(pill).to_have_class("evo-runtime-pill offline")  # goes on by itself: the plain pill
                expect(page.locator("#evo-runtime-detail")).to_contain_text(details[status])
                page.screenshot(path=str(diagnostic_directory(root.parent, "task-evidence") / f"evolution-{status}.png"), full_page=True)
        finally:
            browser.close()
