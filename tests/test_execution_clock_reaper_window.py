"""#939: can a grant consumption land after the supervisor's idle-timeout
decision captured ``job["meta"]`` and before the reaper's kill, so ``_enqueue_retry`` stamps a
stale clock? And (#1729) does the retry after a consumed Resume continue without that spent grant,
while an unconsumed grant returns the same id to its exact pause instead of a retry?

Supported producers: API ingress, saved work, stop/boot, owner Pause through the agent handler,
owner Resume grant, ``assign_tasks`` (with ``_tick_parked_work``), the real ``run_llm_loop`` and
``resume_paused_loop`` hold, the real ``enforce_task_timeouts`` idle rail (finalization grace, then
the reap decision), the real ``reap_timed_out_task`` (kill confirmation, ``_enqueue_retry``), and the
real assignment of its successor. Doubled: OS processes (a kill marks the double dead), model calls.

Concurrency is modelled single-threaded and deterministically: the worker's first handover write
fails (a real hold), and while it holds the supervisor's ticks run the idle rail to its reap
decision. ``consumed`` = the hold then clears before the reaper kills (consumption after capture);
``killed`` = the reaper kills first and the worker never reaches its next write.
Run with ``-p tests.conftest`` from the repository root (like the 6cde probe).
"""
from __future__ import annotations

import queue as stdqueue
import time

import pytest

from ouroboros import budget_pause, working_checkpoint as wc
from ouroboros.model_wait import execution_elapsed_seconds
from supervisor import queue, workers
from supervisor.events_budget import budget_hold_fact
from tests.test_execution_clock_resume_handover import (  # noqa: F401
    WORK_1, WORK_2, WORK_RESUMED, ProcessLost, _fresh_plan, _granted_after_capacity, _loop_once,
    _supervisor_events, _working_then_paused, clock,
)
from tests.test_project_hold_recovery import worker
from tests.test_resume_handover import _DeadProcess
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial
def _tick_until_reap_decision(clock, jobs):  # noqa: F811
    """The supervisor's own order (server.py): enforce_task_timeouts, then assign_tasks."""
    idle = max(float(queue.get_task_idle_timeout_sec()), float(queue.get_per_call_timeout_ceiling_sec()) + 120.0)
    clock[0] += idle + 1
    queue.enforce_task_timeouts()
    meta = workers.RUNNING["held"]
    assert meta.get("finalization_requested_at"), "the idle rail opens its finalization grace first"
    workers.assign_tasks()  # _tick_parked_work: nothing consumed yet, nothing to fold
    clock[0] += float(queue.FINALIZATION_GRACE_SEC) + 1
    queue.enforce_task_timeouts()
    assert "held" not in workers.RUNNING, "the reap decision withdrew RUNNING"
    job = jobs.get_nowait()
    return job, idle


@pytest.mark.parametrize("budget", [3600, None], ids=["execution-budget", "no-budget"])
@pytest.mark.parametrize("order", ["consumed_before_kill", "killed_before_consumption"])
def test_reaper_window_after_an_idle_decision_over_a_held_resume(host, monkeypatch, clock, budget, order):  # noqa: F811
    from supervisor.task_reaper import reap_timed_out_task

    fresh = _fresh_plan(monkeypatch)
    pause = _working_then_paused(host, monkeypatch, clock, budget, fresh)
    resumed = _granted_after_capacity(host, monkeypatch, clock, pause)
    process = _DeadProcess()
    # A new handle's birth is unknown, never the prior affirmed process's.
    workers.WORKERS[0].proc, workers.WORKERS[0].process_birth = process, ""
    jobs = stdqueue.Queue()
    monkeypatch.setattr(queue, "_reap_queue", jobs)
    monkeypatch.setattr(queue, "_ensure_reaper_started", lambda: None)
    monkeypatch.setattr("supervisor.worker_pool_lifecycle.kill_worker_tree", lambda pid, **_k: process.kill(pid))
    monkeypatch.setattr(workers, "respawn_worker", lambda *_a, **_k: None)
    monkeypatch.setattr(workers, "QUEUE_MAX_RETRIES", 3)
    monkeypatch.setattr(queue, "QUEUE_MAX_RETRIES", 3, raising=False)
    monkeypatch.setattr(queue, "reconstruct_task_cost", lambda *_a, **_k: {
        "cost_accounting_status": "available", "cost_final": True})
    facts = {"budget": budget, "order": order}
    events = _supervisor_events(host)
    real_save = wc.save_resumed
    calls = []

    def save_during_supervisor_ticks(*args, **kwargs):
        calls.append(clock[0])
        if len(calls) == 1:
            job, idle = _tick_until_reap_decision(clock, jobs)
            facts.update(idle_timeout=idle, grace=queue.FINALIZATION_GRACE_SEC, decision_at=clock[0],
                         will_retry=job["will_retry"], terminal_reason=job["terminal_reason"],
                         captured_sleep_parked_at=job["meta"].get("sleep_parked_at"),
                         captured_budget_paused_sec=job["meta"].get("budget_paused_sec"),
                         captured_runtime_sec=job["runtime_sec"])
            facts["job"] = job
            raise OSError("disk full: the handover write fails, the worker holds")
        if order == "killed_before_consumption":
            facts["reaped_at"] = clock[0]
            reap_timed_out_task(facts["job"])
            raise ProcessLost("killed by the reaper before its next handover write")
        return real_save(*args, **kwargs)

    def cut(fault, _ctx):
        fault.setattr(wc, "save_resumed", save_during_supervisor_ticks)
        fault.setattr(budget_pause, "_HOLD_POLL_SEC", 0.0)

    _loop_once(host, monkeypatch, resumed, plan=fresh, events=events, cut=cut)
    job = facts.pop("job")
    row = budget_pause.budget_pause_row(host.root, "held")
    facts.update(pause_state_after_worker=row.get("state"), consumed_at=(row.get("grant") or {}).get("consumed_at"),
                 consumed_paused_sec=row.get("paused_duration_sec"),
                 consumed_events=[event.get("phase") for event in events.delivered],
                 job_meta_sleep_parked_at_after_worker=job["meta"].get("sleep_parked_at"))
    if order == "consumed_before_kill":
        assert row["state"] == budget_pause.STATE_RESUMED, "consumption landed after the reap decision"
        assert "held" not in workers.RUNNING, "no RUNNING row left for the notification or the tick to fold"
        clock[0] += WORK_RESUMED  # the resumed worker works until the reaper's kill
        facts["reaped_at"] = clock[0]
        reap_timed_out_task(job)
    work_before_resume = WORK_1 + WORK_2
    truth = work_before_resume + (facts["reaped_at"] - facts["consumed_at"] if facts["consumed_at"] else 0.0)
    successors = [task for task in host.pending if task.get("timeout_retry_from") == "held"]
    facts.update(successor_count=len(successors), truth_bounded_sec=truth if budget else None)
    if successors:
        [successor] = successors
        facts.update(successor_id=successor["id"], successor_attempt=successor.get("_attempt"),
                     stamped_handoff_sec=successor.get("_execution_handoff_sec"),
                     successor_carriers={key: bool(successor.get(key)) for key in (
                         "_owner_wait_resume", "_budget_pause_resume", "_working_recovery")},
                     successor_budget_pause_resume=successor.get("_budget_pause_resume"),
                     successor_working_boundary=(successor.get("_working_recovery") or {}).get("boundary"),
                     successor_hold_before_assign=budget_hold_fact(successor))
        sent = worker(host, monkeypatch)
        workers.assign_tasks()
        workers.assign_tasks()
        facts.update(successor_dispatched=[task["id"] for task in sent],
                     successor_hold_after_assign=budget_hold_fact(successor),
                     successor_pending_after_assign=any(task is successor for task in host.pending),
                     successor_budget_pause_after_assign=successor.get("_budget_pause"),
                     successor_budget_pause_resume_after_assign=successor.get("_budget_pause_resume"))
        # The owner's explicit Resume, on the successor and on the original id: can either door run it?
        facts["owner_resume_successor"] = queue.resume_budget_paused_task(successor["id"])
        facts["owner_resume_original"] = queue.resume_budget_paused_task("held")
        workers.assign_tasks()
        workers.assign_tasks()
        facts.update(dispatched_after_owner_resume=[task["id"] for task in sent],
                     successor_hold_after_owner_resume=budget_hold_fact(successor),
                     successor_pending_after_owner_resume=any(task is successor for task in host.pending))
        for task_id in [task["id"] for task in sent]:
            if task_id in workers.RUNNING:
                facts.setdefault("spent_after_dispatch", {})[task_id] = execution_elapsed_seconds(
                    workers.RUNNING[task_id], time.time())
    print('REAPER_FACTS', facts)
    assert facts["will_retry"] is True and facts["terminal_reason"] == "idle_timeout"
    if order == "killed_before_consumption":
        # The grant was never consumed: the SAME id returns to its exact pause (revoked under its
        # own identity, as a worker death there does), nothing is retried, the owner may Resume again.
        assert facts['successor_count'] == 0 and facts['pause_state_after_worker'] == budget_pause.STATE_PAUSED
        [held] = [task for task in host.pending if task["id"] == "held"]
        assert held["_budget_pause"]["checkpoint"]["pause_id"] == pause["pause_id"]
        assert budget_pause.budget_pause_row(host.root, "held")["grant"]["revoked_at"]
        return
    # The consumed grant is spent authority: the successor continues the resumed work and dispatches.
    assert facts['successor_count'] == 1 and facts['successor_carriers']['_budget_pause_resume'] is False
    assert facts['successor_working_boundary'] == 'resumed'
    assert facts['successor_dispatched'] == [facts['successor_id']] and facts['successor_hold_after_assign'] is None
    assert facts['stamped_handoff_sec'] == (pytest.approx(truth) if budget else None)
