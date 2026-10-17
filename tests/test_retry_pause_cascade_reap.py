"""A cascade Stop must never re-hold a child captured by the idle reaper.

Derived from the recovered Opus negative witness. That original asserted the
observed bug; this consumer test asserts the opposite and preserves the new
intent-first HTTP contract. Exact saved pauses and unsaved seeds share the rule.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros import budget_pause
from ouroboros.task_results import load_task_result
from supervisor import queue, workers
from tests import test_execution_clock_resume_handover as handover
from tests._budget_pause_exact_helpers import _loop_ctx
from tests.test_execution_clock_resume_handover import clock  # noqa: F401
from tests.test_retry_pause_identity import (  # noqa: F401
    _DeadProcess, _WorkerDied, _attempt_ctx, _running_child, _tick_until_reap_decision, reaper,
)
from tests.test_swarm_host_admission import host  # noqa: F401

pytestmark = pytest.mark.serial


def _cascade_stop(host, task_id):  # noqa: F811
    from starlette.requests import Request

    from ouroboros.gateway.tasks import api_task_cancel

    async def receive():
        return {"type": "http.request", "body": json.dumps({"cascade": True}).encode(), "more_body": False}

    request = Request({"type": "http", "method": "POST", "path": f"/api/tasks/{task_id}/cancel",
                       "path_params": {"task_id": task_id}, "headers": [(b"content-type", b"application/json")],
                       "app": SimpleNamespace(state=SimpleNamespace(drive_root=host.root, repo_dir=workers.REPO_DIR))},
                      receive)
    response = asyncio.run(api_task_cancel(request))
    return response.status_code, json.loads(response.body)


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("phase", ["inflight", "retained"])
def test_cascade_stop_cannot_rehold_a_reaped_child(host, monkeypatch, clock, reaper, saved, phase):  # noqa: F811
    from supervisor.task_lifecycle import root_cancellation_fenced
    from supervisor.task_reaper import reap_timed_out_task

    fresh, root = _running_child(host, monkeypatch, saved=False)
    ctx, limit, _waiter = _attempt_ctx(host, root, monkeypatch, attempt=1)
    handover._pause_through_agent(host, root, ctx, limit, plan=fresh)  # the owner's Pause parks the root
    cctx, climit = _loop_ctx(host.root, "held-child", attempt=1)
    cctx.root_task_id, cctx.active_model, cctx.context_fit_plan = "held", fresh.model, fresh
    cctx.task_metadata = {"root_task_id": "held", "parent_task_id": "held", "delegation_role": "subagent"}
    climit.round_idx, climit.budget_tail = 0, "no_tool"
    climit.messages = [{"role": "user", "content": "child work"}]

    def die(*_a, **_k):
        raise _WorkerDied

    with monkeypatch.context() as fault:  # the child meets the Pause, then its worker is lost
        fault.setattr(budget_pause, "_HOLD_POLL_SEC", 0.0)
        if not saved:
            fault.setattr(budget_pause, "_exact_continuation_row", die)
        with pytest.raises(budget_pause.BudgetPauseRequested if saved else _WorkerDied):
            budget_pause.enter_owner_pause(climit)
    budget_pause.end_dispatch_fence("held-child")
    jobs = reaper.arm()
    process = workers.WORKERS[1].proc = _DeadProcess()
    monkeypatch.setattr("supervisor.worker_pool_lifecycle.kill_worker_tree", lambda pid, **_k: process.kill(pid))
    job, _idle = _tick_until_reap_decision(clock, jobs, "held-child")  # RUNNING withdrawn into the job
    from supervisor import task_reaper
    from tests.test_retry_pause_stop_transfer import _Drained, _ReapPump
    if phase == "retained":
        from ouroboros import cancel_intents
        import sys
        pump = _ReapPump()
        monkeypatch.setattr(queue, "_reap_queue", pump)
        monkeypatch.setattr(task_reaper, "reap_queue", pump)
        monkeypatch.setattr(task_reaper, "_deferred_reap_jobs", [])
        pump.put(job)
        real = cancel_intents.has_active_intent

        def unavailable(*args, **kwargs):
            if sys._getframe(1).f_code.co_name == "<genexpr>" and sys._getframe(2).f_code.co_name == "_pause_rehold_allowed":
                raise OSError("cancel authority is temporarily unreadable")
            return real(*args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(cancel_intents, "has_active_intent", unavailable)
            with pytest.raises(_Drained):
                task_reaper.reaper_loop()
        assert task_reaper._deferred_reap_jobs == [job]
        assert not any(t["id"] == "held-child" for t in workers.PENDING)
        assert load_task_result(host.root, "held-child")["status"] not in {"failed", "cancelled"}
    status, body = _cascade_stop(host, "held")  # the owner's Stop lands before the reaper's pass
    root_after_stop = load_task_result(host.root, "held").get("status")
    if phase == "retained":
        task_reaper._retry_deferred_reap_jobs()
        with pytest.raises(_Drained):
            task_reaper.reaper_loop()
        assert not task_reaper._deferred_reap_jobs
    else:
        reap_timed_out_task(job)
    rows = {t["id"]: t for t in workers.PENDING}
    child = rows.get("held-child") or {}
    facts = {"stop": (status, body), "root_status": root_after_stop,
             "child_status": (load_task_result(host.root, "held-child") or {}).get("status"),
             "child_pending": "held-child" in rows,
             "child_hold": (child.get("_budget_pause_hold") or {}).get("reason"),
             "child_pause_marker": bool(child.get("_budget_pause")),
             "root_cancellation_fenced": root_cancellation_fenced(child, "held") if child else None,
             "subtree_live_after": queue.task_subtree_is_live("held", ignore_intents=True)}
    print("CASCADE REAP REPRO", facts)
    assert status in {200, 202}, facts
    if status == 202:
        assert body["cancel_state"] == "pending" and body["reason_code"] == "teardown_unconfirmed"
        from supervisor.task_lifecycle import sweep_cancel_intents
        sweep_cancel_intents(now=clock[0] + 60)
    assert load_task_result(host.root, "held")["status"] == "cancelled", facts
    assert not facts["child_pending"], facts
    assert facts["child_status"] in {"cancelled", "failed", "interrupted"}, facts
    assert not queue.task_subtree_is_live("held", ignore_intents=True), facts
