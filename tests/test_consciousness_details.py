"""The Dashboard and state fallback share control truth and every clock status."""

import asyncio
import json

import pytest
from starlette.requests import Request

from ouroboros import consciousness_view as view
from ouroboros.consciousness import BackgroundConsciousness
from ouroboros.gateway.state import api_consciousness


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
])
def test_primary_endpoint_and_state_fallback_share_status(monkeypatch, tmp_path, facts, status):
    import server
    from supervisor import state

    snapshot = {"enabled": True, "next_wake_at": "2027-01-15T12:30:00+00:00",
                "allowance_resets_at": "2027-01-15T18:00:00+00:00", "spent_24h_usd": 21.5,
                "daily_usd": 20, "unknown_unmetered": 2, "integrity_degraded": True, **facts}
    clock = object.__new__(BackgroundConsciousness)
    clock._drive_root = tmp_path
    monkeypatch.setattr(clock, "status_snapshot", lambda: dict(snapshot))
    monkeypatch.setattr(server, "_consciousness", clock)
    monkeypatch.setattr(state, "load_state", lambda: {"bg_consciousness_enabled": True})
    monkeypatch.setattr(view, "place_views", lambda root: [{"place_id": "mail"}])
    monkeypatch.setattr(view, "recent_wakes", lambda root, places: [{"task_id": "wake"}])
    response = asyncio.run(api_consciousness(Request({"type": "http", "app": server.app.app})))
    panel = json.loads(response.body)
    fallback = server.app.app.state.describe_bg_consciousness_state(True)
    assert panel["status"] == status and panel["detail"]
    assert {key: panel[key] for key in fallback} == fallback
    assert clock.details_snapshot(True) == panel
    assert panel["places"] == [{"place_id": "mail"}] and panel["recent_wakes"] == [{"task_id": "wake"}]
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("constructed", [False, True])
@pytest.mark.parametrize("enabled,status", [(None, "unknown"), (False, "disabled"), (True, "stopped")])
def test_stored_control_and_unconstructed_clock_are_visible(monkeypatch, tmp_path, constructed, enabled, status):
    import server
    from supervisor import state

    clock = object.__new__(BackgroundConsciousness)
    clock._drive_root = tmp_path
    monkeypatch.setattr(clock, "status_snapshot", lambda: {"enabled": not enabled})
    monkeypatch.setattr(view, "place_views", lambda root: [])
    monkeypatch.setattr(view, "recent_wakes", lambda root, places: [])
    monkeypatch.setattr(server, "_consciousness", clock if constructed else None)
    monkeypatch.setattr(state, "load_state", lambda: {} if enabled is None else {"bg_consciousness_enabled": enabled})
    response = asyncio.run(api_consciousness(Request({"type": "http", "app": server.app.app})))
    panel = json.loads(response.body)
    fallback = server.app.app.state.describe_bg_consciousness_state(enabled)
    assert panel["status"] == ("sleeping" if constructed and enabled else status)
    assert panel["enabled"] is enabled
    assert {key: panel[key] for key in fallback} == fallback
    assert ("not constructed" in panel["detail"]) == (not constructed and enabled is True)
