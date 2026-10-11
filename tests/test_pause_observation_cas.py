"""Off-lock pause observers never write over a newer Resume, revocation or pause.

The owner-Pause settlement re-check (``owner_pause_control``) and the cold-sleep
readiness pass (``sleep_wake``) read the durable pause row, observe custody or
readiness OUTSIDE the queue lock, then record their observation. A real owner
Resume (a grant), a grant then its revocation (back to a paused state), or a
newer pause may land during that observation; the write compares the pause id,
state AND grant it read, and a lost comparison is a benign stale observation
that the next pass redoes. Driven through the real Resume/revoke/assignment
seams, never by rewriting the row by hand, except where a test names a newer
writer explicitly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._budget_pause_exact_helpers import _fast_hold, _install_queue, _loop_ctx, _mock_pause_observation

RUNNING_RUN = [{"run_id": "run-1", "state": "running", "stop_outcome": ""}]


def _quiet_custody(root, task_id, **kw):
    return {"custody_read": "ok", "runs": [], "observed_at": 0.0, "coverage_basis": "test"}


def _owner_park(tmp_path, monkeypatch, task_id, root):
    from ouroboros import budget_pause

    _ctx, limit_ctx = _loop_ctx(tmp_path, task_id)
    _ctx.root_task_id = root
    _fast_hold(monkeypatch, budget_pause)
    _mock_pause_observation(monkeypatch, budget_pause, RUNNING_RUN)
    with pytest.raises(budget_pause.BudgetPauseRequested) as raised:
        budget_pause.enter_owner_pause(limit_ctx)
    budget_pause.end_dispatch_fence(task_id)
    return raised.value.pause


def _sup(tmp_path, queue, workers):
    return SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING=workers.RUNNING, PENDING=workers.PENDING,
                           WORKERS=workers.WORKERS, sort_pending=lambda: None,
                           persist_queue_snapshot=queue.persist_queue_snapshot, bridge=None)


def _owner_paused_solo(tmp_path, monkeypatch):
    """A root parked by the owner's Pause while the run it sent still ran."""
    from ouroboros import budget_pause, owner_pause
    from ouroboros.task_results import STATUS_RUNNING, write_task_result
    from supervisor.events_budget import install_exact_budget_pause
    from supervisor.owner_pause_control import request_owner_pause

    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    write_task_result(tmp_path, "solo", STATUS_RUNNING, chat_id=0)
    workers.RUNNING["solo"] = {"task": {"id": "solo", "type": "task", "chat_id": 0, "root_task_id": "solo"},
                               "worker_id": 0, "attempt": 1}
    assert request_owner_pause("solo", request_id="p")["ok"]
    row = _owner_park(tmp_path, monkeypatch, "solo", "solo")
    install_exact_budget_pause(_sup(tmp_path, queue, workers), "solo",
                               budget_pause.exact_pause_marker(row)["checkpoint"])
    parked = budget_pause.budget_pause_row(tmp_path, "solo")
    assert parked["state"] == budget_pause.STATE_PAUSING
    assert parked["settlement"] == owner_pause.SETTLEMENT_EXTERNAL_RUNNING
    return queue, workers


def _competing(tmp_path, queue, kind, task_id):
    """The newer authority that lands while an observer is off the lock."""
    from ouroboros import budget_pause
    from supervisor.budget_resume import revoke_exact_budget_resume

    if kind in {"resume", "grant_then_revoke"}:
        granted = queue.resume_budget_paused_task(task_id)
        assert granted["ok"] is True, granted
        if kind == "grant_then_revoke":
            with queue._queue_lock:
                task = next(item for item in queue.PENDING if item.get("id") == task_id)
                assert revoke_exact_budget_resume(task, "competing_test_revoke") is True
        return granted
    if kind == "newer_pause":
        # A newer pause of the same task, as its own park writer would record it.
        row = budget_pause.budget_pause_row(tmp_path, task_id)
        budget_pause.set_budget_pause(tmp_path, task_id, {**row, "pause_id": "pause-newer"},
                                      expected_pause_id=str(row["pause_id"]))
        return {}
    assert kind == "none"
    return {}


@pytest.mark.parametrize("kind", ["none", "resume", "grant_then_revoke", "newer_pause"])
def test_owner_pause_settlement_never_overwrites_a_newer_resume_or_pause(tmp_path, monkeypatch, kind):
    from ouroboros import budget_pause, owner_pause
    from supervisor import owner_pause_control

    queue, workers = _owner_paused_solo(tmp_path, monkeypatch)
    before = budget_pause.budget_pause_row(tmp_path, "solo")
    landed: dict = {}

    def observe(root, task_id, **kw):
        if kw.get("reason") == "owner_pause_settlement_check" and "done" not in landed:
            landed["done"] = True
            landed["granted"] = _competing(tmp_path, queue, kind, task_id)
        return _quiet_custody(root, task_id, **kw)

    monkeypatch.setattr(budget_pause, "observe_task_runs", observe)
    monkeypatch.setattr(owner_pause_control, "_LAST_SETTLE_CHECK", {})
    settled = owner_pause_control.settle_requested_owner_pauses(queue, now=100.0)
    row = budget_pause.budget_pause_row(tmp_path, "solo")
    if kind == "none":
        # The fix is not vacuous: an uncontested observation still settles the tree.
        assert settled == ["solo"]
        assert row["settlement"] == owner_pause.SETTLEMENT_SETTLED and row["state"] == before["state"]
        assert owner_pause.read_fence(tmp_path, "solo")["state"] == owner_pause.FENCE_PAUSED
        return
    assert "settlement_observed_at" not in row, "the stale observation wrote nothing"
    if kind == "newer_pause":
        assert row["pause_id"] == "pause-newer"
        assert row["settlement"] == owner_pause.SETTLEMENT_EXTERNAL_RUNNING
        return
    grant_id = landed["granted"]["grant_id"]
    assert row["grant"]["grant_id"] == grant_id and row["resume_generation"] == 1
    if kind == "grant_then_revoke":
        assert row["state"] == budget_pause.STATE_PAUSED and row["grant"]["revoked_at"]
        assert owner_pause.read_fence(tmp_path, "solo")["state"] != owner_pause.FENCE_RELEASED
        return
    # The owner's Resume survives: the queue carrier still matches the durable
    # grant, and real assignment hands exactly that grant to a worker once.
    assert row["state"] == budget_pause.STATE_RESUME_GRANTED and not row["grant"].get("revoked_at")
    assert workers.PENDING[0]["_budget_pause_resume"]["grant_id"] == grant_id
    sent = []
    workers.WORKERS[0] = SimpleNamespace(wid=0, busy_task_id=None, reaping=False,
                                         in_q=SimpleNamespace(put=lambda task: sent.append(dict(task))))
    workers.assign_tasks()
    assert [(task["id"], task["_budget_pause_resume"]["grant_id"]) for task in sent] == [("solo", grant_id)]


def test_one_superseded_member_neither_blocks_its_sibling_nor_the_tree_census(tmp_path, monkeypatch):
    from ouroboros import budget_pause, owner_pause
    from ouroboros.task_results import STATUS_RUNNING, write_task_result
    from supervisor import owner_pause_control
    from supervisor.events_budget import install_exact_budget_pause
    from supervisor.owner_pause_control import request_owner_pause

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    write_task_result(tmp_path, "root-1", STATUS_RUNNING, chat_id=0)
    workers.RUNNING["root-1"] = {"task": {"id": "root-1", "type": "task", "chat_id": 0, "root_task_id": "root-1"},
                                 "worker_id": 0, "attempt": 1}
    for index, child in enumerate(("child-a", "child-b"), start=1):
        write_task_result(tmp_path, child, STATUS_RUNNING, chat_id=0)
        workers.RUNNING[child] = {"task": {"id": child, "type": "task", "chat_id": 0, "root_task_id": "root-1",
                                           "parent_task_id": "root-1", "delegation_role": "subagent"},
                                  "worker_id": index, "attempt": 1}
    assert request_owner_pause("root-1", request_id="p")["ok"]
    sup = _sup(tmp_path, queue, workers)
    for member in ("child-a", "child-b", "root-1"):
        row = _owner_park(tmp_path, monkeypatch, member, "root-1")
        install_exact_budget_pause(sup, member, budget_pause.exact_pause_marker(row)["checkpoint"])
    assert owner_pause.read_fence(tmp_path, "root-1")["state"] == owner_pause.FENCE_REQUESTED

    competing = {"grant_id": "g-newer", "granted_at": "later"}

    def observe(root, task_id, **kw):
        if kw.get("reason") == "owner_pause_settlement_check" and task_id == "child-a" and "seen" not in competing:
            competing["seen"] = True
            # A newer grant written and revoked meanwhile, back to the same state:
            # only the grant identity tells this observation is stale.
            current = budget_pause.budget_pause_row(tmp_path, "child-a")
            budget_pause.set_budget_pause(
                tmp_path, "child-a", {**current, "grant": {**competing, "revoked_at": "later"},
                                      "resume_generation": 1},
                expected_pause_id=current["pause_id"], expected_state=current["state"], expected_grant_id="")
        return _quiet_custody(root, task_id, **kw)

    census: list = []
    real_refresh = owner_pause_control.refresh_owner_pause_tree
    monkeypatch.setattr(owner_pause_control, "refresh_owner_pause_tree",
                        lambda root_id: census.append(root_id) or real_refresh(root_id))
    monkeypatch.setattr(budget_pause, "observe_task_runs", observe)
    monkeypatch.setattr(owner_pause_control, "_LAST_SETTLE_CHECK", {})
    assert owner_pause_control.settle_requested_owner_pauses(queue, now=100.0) == []
    a, b = (budget_pause.budget_pause_row(tmp_path, member) for member in ("child-a", "child-b"))
    assert a["grant"]["grant_id"] == "g-newer" and a["resume_generation"] == 1
    assert a["settlement"] == owner_pause.SETTLEMENT_EXTERNAL_RUNNING, "the lost write was dropped, not retried blind"
    assert b["settlement"] == owner_pause.SETTLEMENT_SETTLED, "the sibling still settled in the same pass"
    assert census == ["root-1"], "the tree's current-authority census still ran"
    assert owner_pause.read_fence(tmp_path, "root-1")["state"] == owner_pause.FENCE_REQUESTED

    # The next pass re-observes the member under its current identity and settles the tree.
    assert owner_pause_control.settle_requested_owner_pauses(queue, now=110.0) == ["root-1"]
    a = budget_pause.budget_pause_row(tmp_path, "child-a")
    assert a["settlement"] == owner_pause.SETTLEMENT_SETTLED and a["grant"]["grant_id"] == "g-newer"
    assert owner_pause.read_fence(tmp_path, "root-1")["state"] == owner_pause.FENCE_PAUSED


def _cold_sleeper(tmp_path, monkeypatch):
    from tests.test_model_sleep import _cold_park, _result

    queue, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    _result(tmp_path, "peer-a")
    _cold_park(tmp_path, monkeypatch, workers, senders=["peer-a"])
    return queue, workers


@pytest.mark.parametrize("kind", ["none", "resume", "grant_then_revoke", "newer_pause"])
def test_sleep_readiness_never_overwrites_a_newer_resume_or_pause(tmp_path, monkeypatch, kind):
    from ouroboros import budget_pause, model_sleep
    from supervisor.sleep_wake import wake_ready_sleepers

    queue, workers = _cold_sleeper(tmp_path, monkeypatch)
    landed: dict = {}

    def ready(ctx, sleep):
        if "done" not in landed:
            landed["done"] = True
            landed["granted"] = _competing(tmp_path, queue, kind, "sleeper")
        return "mail:peer-a"

    monkeypatch.setattr(model_sleep, "wake_reason", ready)
    outcomes = wake_ready_sleepers(queue)
    row = budget_pause.budget_pause_row(tmp_path, "sleeper")
    if kind == "none":
        assert outcomes[0]["ok"] is True and row["sleep_ready"]["reason"] == "mail:peer-a"
        assert row["grant"]["selected_by"] == "sleep_wake" and row["resume_generation"] == 1
        return
    assert outcomes == [], "a superseded observation grants nothing in this pass"
    assert "sleep_ready" not in row
    if kind == "newer_pause":
        assert row["pause_id"] == "pause-newer" and row["state"] == budget_pause.STATE_PAUSED
        return
    grant_id = landed["granted"]["grant_id"]
    assert row["grant"]["grant_id"] == grant_id and row["resume_generation"] == 1
    if kind == "resume":
        assert row["state"] == budget_pause.STATE_RESUME_GRANTED and row["grant"]["selected_by"] == "owner"
        assert workers.PENDING[0]["_budget_pause_resume"]["grant_id"] == grant_id
        return
    # Grant then revoke returned the SAME paused state; the revocation and its
    # generation survive, and the next pass wakes it under a HIGHER generation.
    assert row["state"] == budget_pause.STATE_PAUSED and row["grant"]["revoked_at"]
    again = wake_ready_sleepers(queue)
    assert again[0]["ok"] is True and again[0]["grant_generation"] == 2
    row = budget_pause.budget_pause_row(tmp_path, "sleeper")
    assert row["grant"]["grant_id"] != grant_id and row["resume_generation"] == 2
