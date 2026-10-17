"""A store that cannot answer the lineage query is disclosed without inventing or losing lineage."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros.gateway.task_events import _TaskEventCursorFollower, _TaskEventFollower
from ouroboros.task_results import write_task_result
from ouroboros.task_store import patch_document
from tests._store_shared import delete_result
from tests.test_task_event_cursor import append, content, seed


def child_follower(tmp_path):
    root = seed(tmp_path / "root")
    child = seed(tmp_path / "child", "child")
    write_task_result(root, "child", "running", delegation_role="subagent",
                      parent_task_id="root", child_drive_root=str(child))
    append(child, "first", task_id="child")
    follower = _TaskEventCursorFollower(root, "root", {"v": 2, "seq": 0, "view": "", "positions": {}})
    follower.refresh_view()
    assert content(list(follower.read_events())) == ["first"]
    return root, child, follower


def store_outage(monkeypatch, *, on=True) -> dict:
    """Make the lineage query fail as the store does when it cannot answer; the returned
    switch toggles the outage without undoing the patch."""
    from ouroboros.gateway import task_events
    from ouroboros.store import StoreBusy

    real = task_events.task_facts
    switch = {"on": on}

    def facts(*args, **kwargs):
        if switch["on"]:
            raise StoreBusy("current store busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(task_events, "task_facts", facts)
    return switch


def test_living_follower_retains_its_prior_proof_through_an_outage_and_reports_it(tmp_path, monkeypatch):
    root, child, follower = child_follower(tmp_path)
    checkpoint = follower.checkpoint()
    outage = store_outage(monkeypatch)
    append(child, "pending", task_id="child")
    append(root, "root-continues")
    assert follower.refresh_view() is None
    assert child in follower.roots and follower.view == checkpoint["view"]
    rows = list(follower.read_events())
    gap = rows[0]
    assert gap["type"] == "history_gap" and gap["reason"] == "lineage_incomplete"
    assert gap["data"] == {"store_unavailable": True}
    assert gap["cursor"] == checkpoint  # diagnostic acknowledges no log row
    assert set(content(rows)) == {"pending", "root-continues"}
    follower.refresh_view()
    assert list(follower.read_events()) == []  # no warning storm per poll
    fresh = _TaskEventCursorFollower(root, "root", checkpoint)
    assert fresh.refresh_view()["type"] == "cursor_replay"
    assert child not in fresh.roots  # cursor paths cannot supply proof
    fresh_rows = list(fresh.read_events())
    assert fresh_rows[0]["data"] == {"store_unavailable": True}
    assert "pending" not in content(fresh_rows)
    outage["on"] = False
    follower.refresh_view()
    assert child in follower.roots and list(follower.read_events()) == []
    assert fresh.refresh_view()["type"] == "cursor_replay"
    assert child in fresh.roots


@pytest.mark.parametrize("change", ["parent", "role", "delete"])
def test_confirmed_membership_change_still_retires_the_old_proof(tmp_path, change):
    _root, child, follower = child_follower(tmp_path)
    if change == "delete":
        delete_result(_root, "child")
    elif change == "parent":
        patch_document(_root, "child", lambda row: {**row, "parent_task_id": "elsewhere", "root_task_id": "elsewhere"})
    else:
        patch_document(_root, "child", lambda row: {**row, "delegation_role": "root"})
    assert follower.refresh_view()["type"] == "cursor_replay"
    assert child not in follower.roots and "child" not in follower.task_filter_ids
    assert all(row["type"] != "history_gap" for row in follower.read_events())


def test_legacy_remerge_keeps_live_proof_and_does_not_insert_diagnostics_into_history_rank(tmp_path, monkeypatch):
    root, child, _cursor_follower = child_follower(tmp_path)
    follower = _TaskEventFollower(root, "root")
    before = follower.full_merge()
    store_outage(monkeypatch)
    follower.poll()
    assert child in follower.roots
    assert follower._lineage_notice["type"] == "history_gap"
    after = follower.full_merge()
    assert [(row["type"], row["seq"]) for row in before] == [(row["type"], row["seq"]) for row in after]
    assert child in follower.roots


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_live_http_stream_warns_without_cli_stopping_error_or_losing_child(tmp_path, method, monkeypatch):
    from starlette.requests import Request
    from ouroboros.gateway.task_events import api_task_events

    root, child, _follower = child_follower(tmp_path)
    outage = store_outage(monkeypatch, on=False)
    async def receive():
        return {"type": "http.request", "body": b'{"v":2,"wait":1}', "more_body": False}
    request = Request({"type": "http", "method": method, "path": "/api/tasks/root/events",
        "path_params": {"task_id": "root"}, "query_string": b"wait=1", "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(drive_root=root))}, receive)
    async def consume():
        response = await api_task_events(request)
        events, faulted = [], False
        async for frame in response.body_iterator:
            for line in frame.splitlines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if not faulted and event.get("data", {}).get("content") == "first":
                    faulted = True
                    outage["on"] = True
                    append(child, "pending", task_id="child", ts="2030-01-01T00:00:00Z")
                elif event.get("data", {}).get("content") == "pending":
                    outage["on"] = False
        return events
    events = asyncio.run(consume())
    assert "pending" in content(events)
    gaps = [event for event in events if event["type"] == "history_gap"]
    assert len(gaps) == 1 and gaps[0]["reason"] == "lineage_incomplete"
    assert gaps[0]["data"] == {"store_unavailable": True}
    assert not any(event["type"] == "error" for event in events)
