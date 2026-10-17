"""#1729: an idle-reaped MONEY seed that saved nothing keeps the existing ordinary idle retry.

Restored baseline policy (owner: eligibility, count and cadence stay): only an owner Pause seed
with nothing saved is held as pause intent (``worker_health._hold_unsaved_pause``). Real producers:
admission, assignment, the handler's money boundary, the idle rail and ``reap_timed_out_task``,
the owner's Pause and HTTP Stop, the boot hold and the real loop's continuation; the shared
fixtures double only OS processes and model calls.
"""
from __future__ import annotations

import pytest

from ouroboros import budget_pause
from ouroboros import working_checkpoint as wc
from ouroboros.task_results import load_task_result
from supervisor import queue, workers
from supervisor.events_budget import budget_hold_fact
from supervisor.owner_pause_control import request_owner_pause
from tests.test_execution_clock_resume_handover import MARK, _admit, _attempt_ctx, _dispatch_one, clock  # noqa: F401
from tests.test_project_hold_recovery import worker
from tests.test_retry_pause_identity import (  # noqa: F401
    _assign,
    _first_attempt_seed,
    _fresh_plan,
    _loop_once,
    _owner_stop,
    _pause_at_a_boundary,
    _reap,
    _successors,
    _tick_until_reap_decision,
    reaper,
)
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


def _money_unavailable(monkeypatch):  # noqa: F811
    """The assignment tick's own global money read answers zero (``supervisor.state.budget_remaining``)."""
    from supervisor import state

    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 0.0)


@pytest.mark.parametrize("case", ["retries_left", "retries_exhausted", "money_unavailable"])
@pytest.mark.parametrize("rail", ["global", "root"])
def test_a_money_seed_with_nothing_saved_keeps_the_ordinary_idle_retry(host, monkeypatch, clock, reaper,  # noqa: F811
                                                                       rail, case):
    """#1729 restores the existing idle policy for a money seed of the dead attempt with nothing saved:
    eligibility, count and cadence, then the existing assignment money gate. Never a pause intent."""
    if case == "retries_exhausted":
        monkeypatch.setattr(workers, "QUEUE_MAX_RETRIES", 0)
        monkeypatch.setattr(queue, "QUEUE_MAX_RETRIES", 0, raising=False)
    fresh, seed = _first_attempt_seed(host, monkeypatch, rail, first_boundary=True)
    assert seed["reason"] == "budget" and seed["state"] == budget_pause.STATE_PAUSING and not seed["source_ref"]
    assert not wc.checkpoint_path(host.root, "held", 1).exists(), "its first boundary came before any save"
    job = _reap(clock, reaper)
    stored = load_task_result(host.root, "held")
    facts = {"rail": rail, "case": case, "will_retry": job.get("will_retry"), "stored": stored.get("status"),
             "reason": stored.get("reason_code"), "pending": [t["id"] for t in host.pending]}
    print("money seed idle policy:", facts)
    assert not [t for t in host.pending if budget_hold_fact(t)], ("never held as pause intent", facts)
    assert "held" not in workers.RUNNING and not [t for t in host.pending if t["id"] == "held"], facts
    if case == "retries_exhausted":
        assert not _successors(host) and (stored["status"], stored["reason_code"]) == ("failed", "idle_timeout")
        return
    [retry] = _successors(host)
    assert "_budget_pause_resume" not in retry and "_budget_pause" not in retry and "_working_recovery" not in retry
    if case == "money_unavailable":
        sent = worker(host, monkeypatch)
        _money_unavailable(monkeypatch)
        workers.assign_tasks()
        assert not sent, "money still unavailable: nothing dispatches"
        retried = load_task_result(host.root, retry["id"])
        assert (retried["status"], retried["reason_code"]) == ("failed", "budget_exhausted"), retried
        return
    [sent] = _assign(host, monkeypatch)
    assert sent["id"] == retry["id"]
    started = _loop_once(host, monkeypatch, sent, plan=fresh)
    assert (started["kind"], started["round_idx"]) == ("none", None), "nothing saved, nothing fabricated"
    assert started["messages"][0]["content"] == "go", "the original prompt starts it again"


@pytest.mark.parametrize("control", ["owner_pause", "stop"])
def test_an_owner_pause_or_stop_beside_an_unsaved_money_seed_still_wins(host, monkeypatch, clock, reaper,  # noqa: F811
                                                                        control):
    """The owner's Pause or Stop accepted while the money seed's attempt hangs: the existing controls
    decide. A Pause holds the ordinary path's one retry until its Resume; Stop's custody ends the
    attempt itself, so nothing is reaped, retried or held."""
    _first_attempt_seed(host, monkeypatch, "global", first_boundary=True)
    if control == "stop":
        reaper.arm()  # the worker's process double, which Stop's custody kills
        status, body = _owner_stop(host, "held")
        stored = load_task_result(host.root, "held")
        print("money seed beside a control:", control, status, body, stored.get("status"))
        assert (status, body) == (200, {"ok": True, "task_id": "held"}) and stored["status"] == "cancelled"
        assert "held" not in workers.RUNNING and not host.pending and not _successors(host)
        assert not _assign(host, monkeypatch), "a Stop admits no retry"
        return
    assert request_owner_pause("held", request_id="beside-money")["ok"]
    _reap(clock, reaper)
    stored = load_task_result(host.root, "held")
    print("money seed beside a control:", control, stored.get("status"), stored.get("reason_code"),
          [t["id"] for t in host.pending])
    assert not [t for t in host.pending if budget_hold_fact(t) and t["id"] == "held"]
    [retry] = _successors(host)
    assert not _assign(host, monkeypatch), "the accepted Pause holds the retry"
    assert queue.resume_budget_paused_task(retry["id"])["ok"]
    assert [t["id"] for t in _assign(host, monkeypatch)] == [retry["id"]]


def test_an_unsaved_money_seed_retry_continues_earlier_saved_work_without_replaying_unknown(
        host, monkeypatch, clock, reaper):  # noqa: F811
    """Attempt 1 saved work with a call that has no recorded result; a stop held it; the Resumed
    attempt 2 meets a money seed before saving its own state. The ordinary retry carries attempt
    1's saved work: the call stays UNKNOWN, closed as never re-executed."""
    from tests.test_execution_clock_resume_handover import _stop_boot

    fresh = _fresh_plan(monkeypatch)
    _admit(host, None)
    first = _dispatch_one(host, monkeypatch)
    ctx, limit, _waiter = _attempt_ctx(host, first, monkeypatch, attempt=1)
    ctx.active_model, ctx.context_fit_plan = fresh.model, fresh  # the route the saved state records
    limit.messages = [{"role": "user", "content": "go"}, {"role": "assistant", "content": MARK, "tool_calls": [
        {"id": "call_a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
        {"id": "call_b", "type": "function", "function": {"name": "y", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_a", "content": "done a"}]
    wc.save_round(limit, "post_batch")
    monkeypatch.setattr(workers, "WORKERS", {})
    assert _stop_boot(monkeypatch) == 1
    assert queue.resume_budget_paused_task("held")["ok"]
    second = _dispatch_one(host, monkeypatch)
    assert second["_attempt"] == 2 and second["_working_recovery"]["from_attempt"] == 1
    ctx, limit, _waiter = _attempt_ctx(host, second, monkeypatch, attempt=2)
    seed = _pause_at_a_boundary(host, monkeypatch, fresh, ctx, limit, second, "global", seedless=True,
                                first_boundary=True)
    assert seed["task_attempt"] == 2 and seed["reason"] == "budget" and not seed["source_ref"]
    assert not wc.checkpoint_path(host.root, "held", 2).exists(), "attempt 2 saved nothing of its own"
    _reap(clock, reaper)
    [retry] = _successors(host)
    assert retry["_working_recovery"] and not [t for t in host.pending if budget_hold_fact(t)]
    [sent] = _assign(host, monkeypatch)
    seen = _loop_once(host, monkeypatch, sent, plan=fresh)
    notices = [m for m in seen["messages"] if m.get("role") == "tool" and m.get("tool_call_id") == "call_b"]
    print("earlier saved work:", seen["kind"], [m.get("content", "")[:60] for m in seen["messages"]])
    assert seen["kind"] == "working" and any(MARK in str(m.get("content")) for m in seen["messages"])
    assert len(notices) == 1 and "UNKNOWN" in notices[0]["content"] and "NOT re-executed" in notices[0]["content"]


def test_a_carried_resume_beside_an_unsaved_money_seed_defers_never_retired(host, monkeypatch, clock,  # noqa: F811
                                                                            reaper):  # noqa: F811
    """Not a produced shape (a resumed attempt saves before consuming, so its seed completes and re-parks:
    ``test_a_pause_alive_at_the_reap_parks_the_same_id[after_consumed_resume-*-B4-sourceless-seed]``).
    Built here only to prove such authority is neither retired, copied nor held: the reap defers."""
    from supervisor import task_reaper

    _first_attempt_seed(host, monkeypatch, "global", first_boundary=True)
    workers.RUNNING["held"]["task"]["_budget_pause_resume"] = {"pause_id": "older", "grant_id": "g-older",
                                                                "pause_generation": 0}
    job, _idle = _tick_until_reap_decision(clock, reaper.arm())
    with pytest.raises(task_reaper.TerminalFileRecoveryPending):
        task_reaper.reap_timed_out_task(job)
    assert not _successors(host) and not [t for t in host.pending if t["id"] == "held"]
    assert load_task_result(host.root, "held")["status"] not in {"failed", "cancelled"}
