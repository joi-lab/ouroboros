"""Rendered contracts of NESTED subagent cards and of card text selection.

A nested child is subordinate to its root: collapsed, it is an identity row
(status chip · role identity · notes and chevron, no `Show details` label) in
quieter ink over its metadata row (agent model, harness chip with the run count,
cost, last update, docked `Reviews N`). One useful narration line stays visible;
the complete narration waits for expansion. The card's
summary outranks its details: a body-size activity line over meta-size timeline
rows, an expanded row back at body size. Twins (same parent and displayed role)
keep the short task id, a lone child does not, and no headline carries the
status word the chip already shows. Card text is selectable, and a drag that
selects text never toggles the card while a plain click still does.

Kept apart from ``test_ui_smoke_playwright.py`` so that module stays under the
size-ratchet byte gate. Reuses its server fixture.
"""
from __future__ import annotations

import json

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET, _emit_ws_frame

pytestmark = pytest.mark.ui_browser

ROOT = '#page-chat .chat-live-card[data-task-id="nest-root"]'
CHILD = '#page-chat .chat-live-card.subagent[data-task-id="%s"]'
FACTS = """sel => {
    const card = document.querySelector(sel);
    const button = card.querySelector(':scope > .chat-live-summary-button');
    const q = (s) => button.querySelector(s);
    const style = (el) => getComputedStyle(el);
    const rect = (el) => el.getBoundingClientRect();
    const title = q('[data-live-title]');
    const lh = parseFloat(style(title).lineHeight);
    const activity = q('[data-live-activity]');
    const activityStyle = style(activity);
    const activityLh = parseFloat(activityStyle.lineHeight);
    return {
        text: title.textContent, expanded: card.dataset.expanded,
        titleLines: lh > 0 ? rect(title).height / lh : 99,
        titleWeight: style(title).fontWeight, titleColor: style(title).color,
        titleSelect: style(title).userSelect || style(title).webkitUserSelect,
        titleTop: rect(title).top, chipTop: rect(q('[data-live-phase]')).top,
        activityDisplay: activityStyle.display,
        activityLines: activityLh > 0 ? rect(activity).height / activityLh : 99,
        activityFont: activityStyle.fontSize,
        metaDisplay: style(q('[data-live-meta]')).display, metaText: q('[data-live-meta]').textContent,
        metaTop: rect(q('[data-live-meta]')).top,
        chipText: q('.chat-live-executor-chip')?.textContent || '', chipHeight: q('.chat-live-executor-chip') ? rect(q('.chat-live-executor-chip')).height : 0,
        reviewsText: q('[data-live-review-summary]')?.textContent || '', reviewsHidden: q('[data-live-review-summary]')?.hidden ?? true,
        reviewsDisplay: q('[data-live-review-summary]') ? style(q('[data-live-review-summary]')).display : 'none',
        reviewsTop: q('[data-live-review-summary]') ? rect(q('[data-live-review-summary]')).top : -1,
        toggleDisplay: style(q('[data-live-toggle]')).display,
        ariaExpanded: button.getAttribute('aria-expanded'),
        cardRight: rect(card).right, cardHScroll: card.scrollWidth > card.clientWidth + 1,
        sideRight: rect(q('.chat-live-summary-side')).right,
        titleClipped: title.scrollWidth > title.clientWidth + 1,
    };
}"""


def _seed(data_dir):
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    rows = [{
        "ts": "2026-09-02T10:00:00+00:00", "chat_id": 1, "task_id": "nest-root",
        "content": "Root task started", "suggested_name": "Nested cards keep their place",
        "is_progress": True,
    }]
    children = [("a1b2c3d4e5f6-twin", "scout"), ("f6e5d4c3b2a1-twin", "scout"), ("nest-review", "reviewer"),
                ("nest-long", "codex-local-forensics-reviewer-and-auditor-of-everything")]
    for index, (child_id, role) in enumerate(children, start=1):
        rows.append({
            "ts": f"2026-09-02T10:00:0{index}+00:00", "chat_id": 1, "task_id": child_id,
            "content": f"{role} finished a long narration that must stay out of the collapsed row "
                       + "word " * 40,
            "is_progress": True, "delegation_role": "subagent", "subagent_event": "completed",
            "subagent_task_id": child_id, "parent_task_id": "nest-root", "root_task_id": "nest-root",
            "subagent_role": role, "model": "google/gemini-3.6-flash", "status": "completed",
            "result": f"{role} result",
            **({"executor_route": "cursor=grok", "execution_evidence": {
                "delegated_runs_started": 1, "delegated_runs_settled": 1, "delegated_runs_failed": 0,
                "delegated_runs_succeeded": 1, "subscription_cost_usd": 0.4}} if role == "scout" else {}),
        })
    (logs_dir / "progress.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


@pytest.mark.parametrize("browser_engine", ["chromium", "webkit"])
def test_ui_smoke_nested_cards_are_one_identity_row_and_text_selects(direct_server_with_data, browser_engine):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    _seed(direct_server_with_data["data_dir"])
    url = direct_server_with_data["url"]
    try:
        with sync_playwright() as pw:
            try:
                browser = getattr(pw, browser_engine).launch(headless=True)
            except PlaywrightError as exc:
                pytest.skip(f"{browser_engine} is not installed: {exc}")
            try:
                page = browser.new_page(viewport={"width": 1100, "height": 800})
                page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function(
                    "() => document.querySelectorAll('#page-chat .chat-live-card.subagent').length === 4",
                    timeout=30_000,
                )
                page.wait_for_timeout(300)
                root = page.evaluate(FACTS, ROOT)
                twin_a = page.evaluate(FACTS, CHILD % "a1b2c3d4e5f6-twin")
                twin_b = page.evaluate(FACTS, CHILD % "f6e5d4c3b2a1-twin")
                lone = page.evaluate(FACTS, CHILD % "nest-review")
                # Identity, not status: twins keep the short id, a lone child does not.
                assert twin_a["text"] == "scout (a1b2c3d4)", twin_a
                assert twin_b["text"] == "scout (f6e5d4c3)", twin_b
                assert lone["text"] == "reviewer", lone
                for facts in (twin_a, twin_b):
                    assert "Coordinator: gemini-3.6-flash" in facts["metaText"], facts
                assert "Agent model: gemini-3.6-flash" in lone["metaText"], lone
                for facts in (twin_a, twin_b, lone):
                    assert "Done" not in facts["text"] and "—" not in facts["text"], facts
                    # Identity row: the title shares the chip row and takes one line; the
                    # metadata stays, with one useful narration line above it.
                    assert abs(facts["titleTop"] - facts["chipTop"]) <= 4, facts
                    assert facts["titleLines"] <= 1.2, facts
                    assert facts["activityDisplay"] != "none" and facts["activityLines"] <= 1.2, facts
                    assert facts["metaDisplay"] != "none", facts
                    assert "updated" in facts["metaText"], facts
                    # The `Show details` label is the root's; a child keeps notes + chevron.
                    assert facts["toggleDisplay"] == "none", facts
                    # Quieter than the root: weight 400 and the secondary ink.
                    assert facts["titleWeight"] in ("400", "normal"), facts
                    assert facts["titleColor"] != root["titleColor"], (facts, root)
                # The owner's first fact on a subagent — where it ran and how many runs settled —
                # is rendered in the collapsed row: the harness chip with the run count.
                for facts in (twin_a, twin_b):
                    assert facts["chipText"] == "Cursor · 1 ok" and facts["chipHeight"] > 0, facts
                assert lone["chipText"] == "", lone
                assert root["titleWeight"] == "500", root
                # Root history uses the same text for its title and latest event;
                # duplicate activity reserves no band, while metadata stays visible.
                assert root["activityDisplay"] == "none" and root["activityLines"] == 0, root
                assert root["metaDisplay"] != "none", root
                assert root["toggleDisplay"] != "none", root
                # The summary outranks the details: body-size activity line.
                assert root["activityFont"] == "14px", root
                # A long identity ellipsizes inside the row; the side controls stay inside the card.
                page.set_viewport_size({"width": 700, "height": 800})
                page.wait_for_timeout(300)
                long = page.evaluate(FACTS, CHILD % "nest-long")
                assert long["titleClipped"] and long["titleLines"] <= 1.2, long
                assert long["sideRight"] <= long["cardRight"] + 1 and not long["cardHScroll"], long
                page.set_viewport_size({"width": 1100, "height": 800})
                page.wait_for_timeout(300)
                # Live path: a twin's narration frame keeps its id, and a NEW sibling of the
                # lone child turns both of them into tagged twins — the already-rendered,
                # already-finished one included.
                page.wait_for_function(
                    "() => window.__testSockets?.some(socket => socket.readyState === WebSocket.OPEN)",
                    timeout=30_000,
                )
                child_frame = {
                    "type": "chat", "role": "assistant", "is_progress": True, "chat_id": 1,
                    "delegation_role": "subagent", "parent_task_id": "nest-root", "root_task_id": "nest-root",
                    "model": "google/gemini-3.6-flash", "ts": "2026-09-02T10:00:09+00:00",
                }
                _emit_ws_frame(page, {**child_frame, "task_id": "a1b2c3d4e5f6-twin", "subagent_task_id": "a1b2c3d4e5f6-twin",
                                      "subagent_role": "scout", "subagent_event": "running", "content": "still scouting"})
                _emit_ws_frame(page, {**child_frame, "task_id": "0badc0de9999-rev", "subagent_task_id": "0badc0de9999-rev",
                                      "subagent_role": "reviewer", "subagent_event": "running", "content": "second reviewer"})
                page.wait_for_selector(CHILD % "0badc0de9999-rev", state="attached", timeout=30_000)
                page.wait_for_timeout(300)
                assert page.evaluate(FACTS, CHILD % "a1b2c3d4e5f6-twin")["text"] == "scout (a1b2c3d4)"
                assert page.evaluate(FACTS, CHILD % "nest-review")["text"] == "reviewer (nest-rev)"
                assert page.evaluate(FACTS, CHILD % "0badc0de9999-rev")["text"] == "reviewer (0badc0de)"
                # A selection being copied survives a sibling's lineage frame (no-op title writes).
                twin_title = page.locator(f'{CHILD % "a1b2c3d4e5f6-twin"} > .chat-live-summary-button [data-live-title]')
                twin_title.evaluate("el => el.scrollIntoView({block: 'center'})")
                page.wait_for_timeout(150)
                tb = twin_title.bounding_box()
                # Start a few pixels into the first glyph (a drag started exactly on a glyph
                # boundary column can select nothing in Chromium).
                page.mouse.move(tb["x"] + 10, tb["y"] + tb["height"] / 2)
                page.mouse.down()
                page.mouse.move(tb["x"] + 90, tb["y"] + tb["height"] / 2, steps=8)
                page.mouse.up()
                page.wait_for_timeout(150)
                assert page.evaluate("() => window.getSelection().toString()").strip(), tb
                _emit_ws_frame(page, {**child_frame, "task_id": "5555eeee6666-new", "subagent_task_id": "5555eeee6666-new",
                                      "subagent_role": "archivist", "subagent_event": "scheduled", "content": "queued"})
                page.wait_for_selector(CHILD % "5555eeee6666-new", state="attached", timeout=30_000)
                assert page.evaluate("() => window.getSelection().toString()").strip(), "a sibling frame must not clear the selection"
                page.evaluate("() => window.getSelection().removeAllRanges()")
                # Twins are a role projection: model observations live in metadata and never
                # change whether same-role siblings need their short ids.
                for cid in ("11112222aaaa-pln", "33334444bbbb-pln"):
                    _emit_ws_frame(page, {**child_frame, "model": "", "task_id": cid, "subagent_task_id": cid,
                                          "subagent_role": "planner", "subagent_event": "scheduled", "content": "queued"})
                page.wait_for_selector(CHILD % "33334444bbbb-pln", state="attached", timeout=30_000)
                page.wait_for_timeout(300)
                assert page.evaluate(FACTS, CHILD % "11112222aaaa-pln")["text"] == "planner (11112222)"
                assert page.evaluate(FACTS, CHILD % "33334444bbbb-pln")["text"] == "planner (33334444)"
                # A system task_summary row (the summary-only replay shape) finishes the child
                # without renaming it to the status word.
                _emit_ws_frame(page, {"type": "chat", "role": "system", "system_type": "task_summary", "chat_id": 1,
                                      "task_id": "33334444bbbb-pln", "content": "Done", "ts": "2026-09-02T10:00:11+00:00",
                                      "delegation_role": "subagent", "parent_task_id": "nest-root", "subagent_task_id": "33334444bbbb-pln",
                                      "subagent_role": "planner", "task_terminal_status": "completed"})
                page.wait_for_timeout(300)
                summed = page.evaluate(FACTS, CHILD % "33334444bbbb-pln")
                assert summed["text"] == "planner (33334444)", summed
                # A generic log frame ("Working on it" / "Getting ready") lands in the timeline
                # only: the child's title stays its identity.
                _emit_ws_frame(page, {"type": "log", "chat_id": 1, "data": {"type": "task_started", "task_id": "11112222aaaa-pln",
                                                                            "ts": "2026-09-02T10:00:10+00:00"}})
                assert page.evaluate(FACTS, CHILD % "11112222aaaa-pln")["text"] == "planner (11112222)"
                for cid, model in (("11112222aaaa-pln", "openai/gpt-5.6-sol"), ("33334444bbbb-pln", "google/gemini-3.6-flash")):
                    _emit_ws_frame(page, {**child_frame, "model": model, "task_id": cid, "subagent_task_id": cid,
                                          "subagent_role": "planner", "subagent_event": "running", "content": "planning"})
                page.wait_for_timeout(300)
                first_planner = page.evaluate(FACTS, CHILD % "11112222aaaa-pln")
                second_planner = page.evaluate(FACTS, CHILD % "33334444bbbb-pln")
                assert first_planner["text"] == "planner (11112222)"
                assert second_planner["text"] == "planner (33334444)"
                assert "Agent model: gpt-5.6-sol" in first_planner["metaText"]
                assert "Agent model: gemini-3.6-flash" in second_planner["metaText"]
                # A child's review count docks on its metadata row while collapsed, like the root's.
                _emit_ws_frame(page, {**child_frame, "task_id": "nest-review", "subagent_task_id": "nest-review",
                                      "subagent_role": "reviewer", "subagent_event": "completed",
                                      "content": "reviewer re-read the candidate and filed a long note " + "word " * 40,
                                      "review_projection": {"panels": [{"panel_id": "nested-review", "surface": "task_acceptance",
                                                                        "aggregate_signal": "PASS", "reason": "smoke", "actors": []}]}})
                page.wait_for_function(
                    "() => document.querySelector('%s [data-live-review-summary]')?.textContent === 'Reviews 1'" % (CHILD % "nest-review"),
                    timeout=10_000,
                )
                reviewed = page.evaluate(FACTS, CHILD % "nest-review")
                assert reviewed["expanded"] == "0" and not reviewed["reviewsHidden"] and reviewed["reviewsDisplay"] != "none", reviewed
                assert abs(reviewed["reviewsTop"] - reviewed["metaTop"]) <= 2, reviewed
                # Expanding a child reveals its narration line (metadata was already there).
                page.locator(CHILD % "nest-review").locator(":scope > [data-live-summary-button]").click()
                page.wait_for_timeout(200)
                opened = page.evaluate(FACTS, CHILD % "nest-review")
                assert opened["expanded"] == "1" and opened["activityDisplay"] != "none", opened
                assert opened["metaDisplay"] != "none" and opened["activityFont"] == "14px", opened
                # Collapsed timeline rows are a meta-size log under the summary; an expanded
                # row returns to body size.
                rows = page.evaluate(
                    """sel => Array.from(document.querySelector(sel).querySelectorAll(':scope > [data-live-timeline] .chat-live-line'))
                        .map(line => ({size: getComputedStyle(line.querySelector('.chat-live-line-title')).fontSize,
                                       expandable: line.classList.contains('expandable')}))""",
                    CHILD % "nest-review",
                )
                assert rows and all(r["size"] == "12px" for r in rows), rows
                line_toggle = page.locator(f'{CHILD % "nest-review"} > [data-live-timeline] .chat-live-line-toggle').first
                assert line_toggle.count() >= 1, rows
                line_toggle.click()
                page.wait_for_timeout(150)
                assert line_toggle.locator(".chat-live-line-title").evaluate("el => getComputedStyle(el).fontSize") == "14px"
                # Text is selectable, and a drag that selects text does not toggle the card.
                assert root["titleSelect"] == "text", root
                title = page.locator(f"{ROOT} > .chat-live-summary-button [data-live-title]")
                # The transcript followed the live edge as children arrived; bring the root back
                # under the fixed page header (centre, not top) before dragging on it.
                title.evaluate("el => el.scrollIntoView({block: 'center'})")
                page.wait_for_timeout(150)
                box = title.bounding_box()
                # Start well inside the first glyph: a drag from a glyph boundary column can
                # select nothing (the disclosed Chromium quirk).
                page.mouse.move(box["x"] + 12, box["y"] + box["height"] / 2)
                page.mouse.down()
                page.mouse.move(box["x"] + min(box["width"] - 4, 120), box["y"] + box["height"] / 2, steps=8)
                page.mouse.up()
                page.wait_for_timeout(150)
                selected = page.evaluate("() => window.getSelection().toString()")
                assert selected.strip(), (selected, box)
                after_drag = page.evaluate(FACTS, ROOT)
                assert after_drag["ariaExpanded"] == root["ariaExpanded"], (root, after_drag)
                # A plain click still toggles.
                page.evaluate("() => window.getSelection().removeAllRanges()")
                page.locator(f"{ROOT} > .chat-live-summary-button [data-live-toggle]").click()
                page.wait_for_timeout(200)
                toggled = page.evaluate(FACTS, ROOT)["ariaExpanded"]
                assert toggled != root["ariaExpanded"]
                # Keyboard activation on the div[role=button] summary toggles it back.
                page.locator(f"{ROOT} > .chat-live-summary-button").focus()
                page.keyboard.press("Enter")
                page.wait_for_timeout(200)
                assert page.evaluate(FACTS, ROOT)["ariaExpanded"] == root["ariaExpanded"]
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
