"""Roster rows are named by their handle in a REAL render: the Review-lanes picker
at 1 row and at 10 rows, desktop and phone width, and the Duplicate draft in the
Available-subagents editor. Controlled API responses; no runtime, no model calls."""
from __future__ import annotations

import json

import pytest
from tests import test_subscription_role_routes_browser as roles

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
subscription_ui = roles.subscription_ui
role_ui = roles.role_ui

EFFORTS = ("", "low", "medium", "high", "xhigh")


def _roster(count):
    """Stored ids deliberately carry the rotten role labels the owner must never see."""
    rows = []
    for index in range(count):
        effort = EFFORTS[index % len(EFFORTS)]
        row = {
            "subagent_id": "fast-scout" if not index else f"fast-scout_copy_{index}",
            "recommended_use": f"Notes {index}",
            "route": ({"kind": "agent_session", "target_id": f"codex=gpt-test-{index}"} if index % 2 == 0
                      else {"kind": "api_model", "target_id": f"openai::gpt-api-{index}"}),
        }
        if effort:
            row["effort"] = effort
        rows.append(row)
    return rows


def _handle(row):
    return row["route"]["target_id"] + (f"/{row['effort']}" if row.get("effort") else "")


def _configure(ui, count):
    roles.configure_mixed(ui)
    rows = _roster(count)
    slots = {
        "triad": [{"slot_id": "triad_1", "route": {"kind": "api_chat", "target_id": "openai::gpt-api"}}],
        "scope": [{"slot_id": "scope_1", "subagent_id": "fast-scout"}],
        "advisory": {"enabled": False},
    }
    ui["settings"].update(OUROBOROS_SUBAGENTS={"enabled": True, "items": rows},
                          OUROBOROS_REVIEWER_SLOTS=json.dumps(slots))
    ui["fixture"]["preview"]["reviewer_slots"] = slots
    return rows


@pytest.mark.parametrize("width", [1360, 390])
@pytest.mark.parametrize("count", [1, 10])
def test_the_review_lanes_picker_names_rows_by_handle_at_any_roster_size(role_ui, count, width):
    ui = role_ui
    rows = _configure(ui, count)
    ui["page"].set_viewport_size({"width": width, "height": 900})
    page = roles.open_agents(ui)
    picker = page.locator('[data-slot-id="scope_1"] [data-slot-route]')
    assert picker.input_value() == "subagent:fast-scout", "the stored id stays the option VALUE"
    labels = picker.locator('optgroup[label="Available subagents"] option').all_text_contents()

    assert labels == [f"{_handle(row)} — {row['recommended_use']}" for row in rows]
    # Scale invariant: the first row reads the same with one row and with ten.
    assert labels[0] == "codex=gpt-test-0 — Notes 0"
    for label in labels:
        assert "#" not in label and "fast-scout" not in label and "_copy_" not in label
    picker.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    roles.capture(page, f"roster-handles-picker-{count}-rows-{width}")


def test_duplicate_is_a_draft_whose_card_names_its_twin_until_the_engine_changes(role_ui):
    ui = role_ui
    _configure(ui, 1)
    page = roles.open_agents(ui)
    page.locator("[data-subagent-duplicate]").first.click()
    cards = page.locator("[data-subagent-row]")
    assert cards.count() == 2
    copy = cards.nth(1)
    # Named on the card BEFORE any Save click, tinted as an error; the source stays clean.
    meta = copy.locator("[data-subagent-meta]")
    assert "Subagent 2 runs the same engine as Subagent 1" in meta.text_content()
    assert meta.get_attribute("data-tone") == "error"
    assert copy.get_attribute("data-invalid") is not None
    assert cards.nth(0).get_attribute("data-invalid") is None
    roles.capture(page, "roster-handles-duplicate-draft")

    posts_before = len([1 for path, _ in ui["posts"] if path == "/api/settings"])
    page.locator("#btn-save-settings").click()
    assert "runs the same engine as Subagent 1" in page.locator("#settings-status").text_content()
    assert len([1 for path, _ in ui["posts"] if path == "/api/settings"]) == posts_before, "a twin is never sent"

    copy.locator('[data-subagent-field="effort"]').select_option("low")
    assert "same engine" not in (copy.locator("[data-subagent-meta]").text_content() or "")
    assert copy.get_attribute("data-invalid") is None
    with page.expect_response("**/api/settings"):
        page.locator("#btn-save-settings").click()
    saved = [body for path, body in ui["posts"] if path == "/api/settings"][-1]["OUROBOROS_SUBAGENTS"]["items"]
    assert [row["route"]["target_id"] for row in saved] == ["codex=gpt-test-0"] * 2
    assert saved[1]["effort"] == "low"
    assert saved[1]["subagent_id"].startswith("subagent_") and "fast-scout" not in saved[1]["subagent_id"]
    # The reviewer picker now offers both engines under their own names.
    labels = page.locator(
        '[data-slot-id="scope_1"] [data-slot-route] optgroup[label="Available subagents"] option'
    ).all_text_contents()
    assert labels == ["codex=gpt-test-0 — Notes 0", "codex=gpt-test-0/low — Notes 0"]


def test_twins_saved_earlier_are_hinted_and_never_block_an_unrelated_save(role_ui):
    ui = role_ui
    rows = _configure(ui, 1)
    rows.append({**rows[0], "subagent_id": "fast-scout_copy_legacy"})
    page = roles.open_agents(ui)
    twin = page.locator("[data-subagent-row]").nth(1)
    meta = twin.locator("[data-subagent-meta]")
    assert "Runs the same engine as Subagent 1" in meta.text_content()
    assert meta.get_attribute("data-tone") is None and twin.get_attribute("data-invalid") is None
    roles.capture(page, "roster-handles-legacy-twin-hint")
    with page.expect_response("**/api/settings"):
        page.locator("#btn-save-settings").click()
    saved = [body for path, body in ui["posts"] if path == "/api/settings"][-1]["OUROBOROS_SUBAGENTS"]["items"]
    assert [row["subagent_id"] for row in saved] == ["fast-scout", "fast-scout_copy_legacy"]
    # Editing the roster is what makes the twin a Save-blocking error.
    page.locator("[data-subagent-row]").first.locator('[data-subagent-field="recommended_use"]').fill("New words")
    posts = len(ui["posts"])
    page.locator("#btn-save-settings").click()
    assert "Subagent 2 runs the same engine as Subagent 1" in page.locator("#settings-status").text_content()
    assert twin.get_attribute("data-invalid") is not None and len(ui["posts"]) == posts


def test_a_switched_off_row_is_named_by_its_handle_with_the_switch_as_a_fact(role_ui):
    ui = role_ui
    rows = _configure(ui, 3)
    rows[0]["enabled"] = False  # the scope slot still references this row
    rows[2]["enabled"] = False  # nothing references this one
    page = roles.open_agents(ui)
    labels = page.locator(
        '[data-slot-id="scope_1"] [data-slot-route] optgroup[label="Available subagents"] option'
    ).all_text_contents()
    # The referenced off row keeps its option under its handle; the unreferenced one is no new choice.
    assert labels == ["codex=gpt-test-0 · switched off — Notes 0", "openai::gpt-api-1/low — Notes 1"]
    card = page.locator("[data-subagent-row]").first
    assert not card.locator('[data-subagent-field="enabled"]').is_checked()
    roles.capture(page, "roster-handles-switched-off-row")

