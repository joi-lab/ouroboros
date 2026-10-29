"""The Dashboard and state fallback share control truth and every clock status."""

import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ouroboros import consciousness_view as view
from ouroboros.consciousness import BackgroundConsciousness
from ouroboros.gateway.state import api_consciousness


NOW = dt.datetime(2027, 1, 15, 12, tzinfo=dt.timezone.utc).timestamp()


@pytest.mark.parametrize("facts,status", [
    ({}, "sleeping"),
    ({"live_wake_task_id": "own-1"}, "thinking"),
    ({"last_wake_outcome": "skipped:waiting_for_first_conversation"}, "waiting_for_first_conversation"),
    ({"last_wake_outcome": "skipped:allowance_exhausted"}, "allowance_exhausted"),
    ({"last_wake_outcome": "skipped:allowance_unknown"}, "allowance_unknown"),
    ({"last_wake_outcome": "rejected:budget_exhausted"}, "wake_rejected"),
    ({"last_wake_outcome": "paused"}, "wake_paused"),
    ({"last_wake_outcome": "pausing"}, "wake_paused"),
    ({"last_wake_outcome": "unknown"}, "wake_outcome_unknown"),
    ({"last_wake_outcome": "failed"}, "wake_failed"),
    ({"next_wake_at": "2027-01-15T11:59:59+00:00", "owner_turn_live": True}, "due"),
    ({"next_wake_at": "2027-01-15T11:59:59+00:00", "live_wake_task_id": "own-1"}, "thinking"),
])
def test_primary_endpoint_and_state_fallback_share_status(monkeypatch, tmp_path, facts, status):
    import server
    from supervisor import state

    monkeypatch.setattr(view, "time", SimpleNamespace(time=lambda: NOW))
    snapshot = {"enabled": True, "next_wake_at": "2027-01-15T12:30:00+00:00",
                "allowance_resets_at": "2027-01-15T18:00:00+00:00", "spent_24h_usd": 21.5,
                "daily_usd": 20, "unknown_unmetered": 2, "integrity_degraded": True,
                "places_brief": [{"place_id": "mail", "label": "Inbox"}], **facts}
    clock = object.__new__(BackgroundConsciousness)
    clock._drive_root = tmp_path
    monkeypatch.setattr(clock, "status_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(server, "_consciousness", clock)
    monkeypatch.setattr(state, "load_state", lambda: {"bg_consciousness_enabled": True})
    def wakes(root, places, *, live_wake_task_id, ids):
        assert live_wake_task_id == snapshot.get("live_wake_task_id", "")
        assert places == snapshot["places_brief"]
        assert ids == ["wake"]
        return [{"task_id": "wake"}]
    monkeypatch.setattr(view, "wake_ids", lambda *args: ["wake"])
    monkeypatch.setattr(view, "recent_wakes", wakes)
    response = asyncio.run(api_consciousness(Request({"type": "http", "app": server.app.app})))
    panel = json.loads(response.body)
    fallback = server.app.app.state.describe_bg_consciousness_state(True)
    assert panel["status"] == status and panel["detail"]
    assert {key: panel[key] for key in fallback} == fallback
    assert clock.details_snapshot(True) == panel
    assert "places" not in panel and panel["recent_wakes"] == [{"task_id": "wake"}]
    assert panel["recent_wakes_coverage"] == "wakes that reached a model call in the last 48 h"
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("wake,owner", [("", False), ("", True), ("wake", False), ("wake", True)])
@pytest.mark.parametrize("deadline", ["2027-01-15T11:59:59Z", "2027-01-15T12:00:00Z",
                                     "2027-01-15T12:00:01Z", "", "invalid"])
def test_due_headline_needs_a_due_clock_and_current_work(monkeypatch, wake, owner, deadline):
    monkeypatch.setattr(view, "time", SimpleNamespace(time=lambda: NOW))
    facts = {"next_wake_at": deadline, "live_wake_task_id": wake, "owner_turn_live": owner}
    result = view.describe_consciousness_state(facts, True)
    due = deadline in {"2027-01-15T11:59:59Z", "2027-01-15T12:00:00Z"}
    if due and owner and not wake:
        assert result["status"] == "due"
        assert result["detail"] == "Due; waiting for current work."
    else:
        assert result["status"] == ("thinking" if wake else "sleeping")
        assert "waiting for current work" not in result["detail"]
    assert result["next_wake_at"] == deadline  # projection never moves the alarm
    assert facts == {"next_wake_at": deadline, "live_wake_task_id": wake, "owner_turn_live": owner}


@pytest.mark.parametrize("enabled,status", [(None, "unknown"), (False, "disabled")])
def test_due_work_never_overrides_unknown_or_disabled_control(monkeypatch, enabled, status):
    monkeypatch.setattr(view, "time", SimpleNamespace(time=lambda: NOW))
    result = view.describe_consciousness_state({"next_wake_at": "2027-01-15T11:59:59Z",
                                              "live_wake_task_id": "wake", "owner_turn_live": True}, enabled)
    assert result["status"] == status and "waiting for current work" not in result["detail"]


@pytest.mark.parametrize("owner_live", [False, True])
def test_snapshot_keeps_the_owner_activity_fact(monkeypatch, tmp_path, owner_live):
    monkeypatch.setattr(BackgroundConsciousness, "_read_state", staticmethod(lambda: {"bg_consciousness_enabled": True}))
    clock = BackgroundConsciousness(tmp_path, tmp_path, lambda: 1, now=NOW)
    monkeypatch.setattr(clock, "live_turns", lambda: ("", owner_live))
    monkeypatch.setattr(clock, "_allowance_view", lambda now: {})
    monkeypatch.setattr(clock, "_context_facts", lambda window: {"places": [], "counted_roots": []})
    assert clock.status_snapshot()["owner_turn_live"] is owner_live


@pytest.mark.parametrize("constructed", [False, True])
@pytest.mark.parametrize("enabled,status", [(None, "unknown"), (False, "disabled"), (True, "stopped")])
def test_stored_control_and_unconstructed_clock_are_visible(monkeypatch, tmp_path, constructed, enabled, status):
    import server
    from supervisor import state

    clock = object.__new__(BackgroundConsciousness)
    clock._drive_root = tmp_path
    monkeypatch.setattr(clock, "status_snapshot", lambda: {"enabled": not enabled})
    monkeypatch.setattr(view, "place_views", lambda root: [])
    monkeypatch.setattr(view, "recent_wakes", lambda root, places, **kw: [])
    monkeypatch.setattr(server, "_consciousness", clock if constructed else None)
    monkeypatch.setattr(state, "load_state", lambda: {} if enabled is None else {"bg_consciousness_enabled": enabled})
    response = asyncio.run(api_consciousness(Request({"type": "http", "app": server.app.app})))
    panel = json.loads(response.body)
    fallback = server.app.app.state.describe_bg_consciousness_state(enabled)
    assert panel["status"] == ("sleeping" if constructed and enabled else status)
    assert panel["enabled"] is enabled
    assert {key: panel[key] for key in fallback} == fallback
    assert ("not constructed" in panel["detail"]) == (not constructed and enabled is True)


@pytest.mark.parametrize("selected_count", [0, 49, 50])
def test_recent_wake_coverage_names_the_selection_cap_even_with_pruned_documents(monkeypatch, tmp_path, selected_count):
    from tests._wake_summary_shared import seed_wake

    document = seed_wake(tmp_path)
    ids = [document["task_id"], *[f"pruned-{i}" for i in range(selected_count - 1)]] if selected_count else []
    reads = []
    monkeypatch.setattr(view, "wake_ids", lambda *args: reads.append(args) or ids)
    clock = object.__new__(BackgroundConsciousness)
    clock._drive_root = tmp_path
    monkeypatch.setattr(clock, "status_snapshot", lambda: {"places_brief": []})
    panel = clock.details_snapshot(True)
    assert len(reads) == 1 and len(panel["recent_wakes"]) == bool(selected_count)
    prefix = "the latest 50 " if selected_count == 50 else ""
    assert panel["recent_wakes_coverage"] == prefix + "wakes that reached a model call in the last 48 h"
