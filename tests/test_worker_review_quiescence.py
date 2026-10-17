"""A task's end cannot retire the process that still publishes its paid review."""

import threading

import pytest

from ouroboros import review_custody, review_operation
from ouroboros.review_substrate import run_review_request
from supervisor import queue, worker_owner_wait, worker_pool_lifecycle, workers
from tests.test_owner_wait_pool import maintain_capacity, park, pool as pool
from tests.test_review_operation_lifetime import TASK, env as env, _parent, _request, _slot, _rows, until
from tests.worker_pool_support import affirm_ready

pytestmark = pytest.mark.serial


@pytest.mark.parametrize("tail", ["wait_close", "final_pointer"])
def test_paid_review_quiescence_includes_empty_active_completion_tail(env, monkeypatch, tail):
    arrived, release = threading.Event(), threading.Event()
    with _parent(env):
        run_review_request(_request(), slots=[_slot()], drive_root=env.root,
                           usage_ctx=env.ctx, llm=env.model)
        row = until(lambda: _rows(env.root))[0]
    operation = review_operation._LIVE[row["review_operation"]["owner_id"]]
    if tail == "wait_close":
        original = operation.wait.close

        def held():
            arrived.set()
            assert release.wait(10)
            return original()
        monkeypatch.setattr(operation.wait, "close", held)
    else:
        original = review_operation._mark_operation

        def held(*args, **kwargs):
            if args[2] == operation.owner_id:
                arrived.set()
                assert release.wait(10)
            return original(*args, **kwargs)
        monkeypatch.setattr(review_operation, "_mark_operation", held)
    assert not review_operation.local_reviews_settled()
    try:
        env.model.ready.set()
        assert arrived.wait(10)
        assert not review_custody.operation_has_live_workers(operation)
        assert review_operation._LIVE[operation.owner_id] is operation
        # The probe's retry joins nothing while the one publisher still owns the tail.
        assert not review_operation.local_reviews_settled(), "empty _ACTIVE is not proof that publication ended"
    finally:
        release.set()
    until(review_operation.local_reviews_settled)


def test_historical_preparation_owns_quiescence_through_actual_drain_return(env):
    entered, release = threading.Event(), threading.Event()

    def work(operation):
        assert operation.preparing and review_operation._LIVE[operation.owner_id] is operation
        entered.set()
        assert release.wait(10)
        return {"status": "refused", "reason": "keyless_source", "dispatched": False}

    purpose = {"task_id": env.ctx.task_id, "task_attempt": 1, "debt_id": "preparation",
               "accounting_root_task_id": env.ctx.task_id, "automatic": False,
               "confirmed_delivery": {"chat_id": 7}}
    result = review_operation.prepare_historical_operation(root=env.root, purpose=purpose,
        event_queue=env.events, work=work, background=True)
    try:
        assert entered.wait(10) and result["status"] == "preparing"
        assert not review_operation.local_reviews_settled()
    finally:
        release.set()
    until(review_operation.local_reviews_settled)


def _checkpointed_operation(env, parent):
    operation = review_operation.ReviewOperation(parent=parent, request=_request(),
        task_id=parent.task_id, result_root=env.root)
    operation.checkpointed = True
    with review_operation._LOCK:
        review_operation._LIVE[operation.owner_id] = operation
    return operation


def test_unknown_final_pointer_keeps_local_retirement_custody(env, monkeypatch):
    attempts = []
    monkeypatch.setattr(review_operation, "_mark_operation", lambda *a, **kw: attempts.append(a[2]) or False)
    with _parent(env) as parent:
        operation = _checkpointed_operation(env, parent)
        try:
            operation.maybe_close()
            assert operation.closed and review_operation._LIVE[operation.owner_id] is operation
            # Each probe check retries the failed write once; failure stays unknown.
            assert not review_operation.local_reviews_settled()
            assert attempts == [operation.owner_id] * 2
            assert review_operation._LIVE[operation.owner_id] is operation
        finally:
            with review_operation._LOCK:
                review_operation._LIVE.pop(operation.owner_id)


def test_a_later_close_retries_the_failed_final_pointer(env, monkeypatch):
    healthy = [False]
    original = review_operation._mark_operation
    monkeypatch.setattr(review_operation, "_mark_operation",
                        lambda *a, **kw: original(*a, **kw) if healthy[0] else False)
    with _parent(env) as parent:
        operation = _checkpointed_operation(env, parent)
        try:
            operation.maybe_close()
            assert operation.closed and review_operation._LIVE.get(operation.owner_id) is operation
            healthy[0] = True
            operation.maybe_close()  # e.g. a late worker's release_review_operation
            assert operation.owner_id not in review_operation._LIVE, "a closed flag is not publication"
        finally:
            with review_operation._LOCK:
                review_operation._LIVE.pop(operation.owner_id, None)


def test_recovered_final_writer_lets_the_full_cap_resume_progress(env, pool, monkeypatch):
    """Reviewer counterexample: a transient pointer failure once pinned ``_LIVE`` forever."""
    park(pool)
    maintain_capacity()
    replacement = workers.WORKERS[1]
    replacement.reaping = False  # the readiness owner opened it
    affirm_ready(replacement)
    attempts, healthy = [], [False]
    original_mark = review_operation._mark_operation

    def transient(*args, **kwargs):
        attempts.append(healthy[0])
        return original_mark(*args, **kwargs) if healthy[0] else False

    monkeypatch.setattr(review_operation, "_mark_operation", transient)
    with _parent(env) as parent:
        operation = _checkpointed_operation(env, parent)
        try:
            operation.maybe_close()
            worker_owner_wait.handle_owner_wait({**pool.event, "phase": "resume"}, workers)
            worker_owner_wait.maintain_owner_wait_capacity()
            probe = replacement.in_q.get_nowait()
            assert probe["type"] == "worker_quiescence" and replacement.resume_hold == "owner"
            assert not review_operation.local_reviews_settled(), "unknown publication defers the answer"
            healthy[0] = True
            # The writer recovered: the probe's own bounded retry publishes and unregisters.
            assert review_operation.local_reviews_settled()
            assert attempts == [False, False, True] and operation.owner_id not in review_operation._LIVE
            worker_pool_lifecycle.handle_worker_quiescent({**probe, "worker_id": replacement.wid,
                "pid": replacement.proc.pid, "process_birth": replacement.process_birth}, workers)
            worker_owner_wait.maintain_owner_wait_capacity()
            assert replacement.retiring
            worker_pool_lifecycle.reap_idle_worker(queue._reap_queue.get_nowait())
            worker_owner_wait.maintain_owner_wait_capacity()
            assert pool.original.active_capacity and workers.WORKERS == {0: pool.original}
        finally:
            with review_operation._LOCK:
                review_operation._LIVE.pop(operation.owner_id, None)


def test_concurrent_close_publishes_the_final_pointer_once(env, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = review_operation._mark_operation

    def slow(*args, **kwargs):
        calls.append(args[2])
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(review_operation, "_mark_operation", slow)
    with _parent(env) as parent:
        operation = _checkpointed_operation(env, parent)
        closer = threading.Thread(target=operation.maybe_close)
        closer.start()
        try:
            assert entered.wait(10)
            operation.maybe_close()  # a concurrent settlement, and the probe's retry below
            assert not review_operation.local_reviews_settled()
        finally:
            release.set()
            closer.join(10)
    assert calls == [operation.owner_id] and operation.wait.closed
    assert operation.owner_id not in review_operation._LIVE and review_operation.local_reviews_settled()


@pytest.mark.parametrize("owed", [True, False])
def test_server_maintenance_retries_a_direct_turns_failed_final_pointer_without_a_worker_probe(
        env, monkeypatch, owed):
    """Reviewer counterexample (C1): the server never runs the worker probe, so maintenance skipped it."""
    from ouroboros import acceptance_settlement
    from ouroboros.task_results import load_task_result

    healthy, settled = [False], []
    original = review_operation._mark_operation
    monkeypatch.setattr(review_operation, "_mark_operation",
                        lambda *a, **kw: original(*a, **kw) if healthy[0] else False)
    monkeypatch.setattr(acceptance_settlement, "late_publication_owed", lambda *a: owed)
    monkeypatch.setattr(acceptance_settlement, "acceptance_actor_ended", lambda *a: True)
    monkeypatch.setattr(acceptance_settlement, "settle_acceptance_operation",
                        lambda ctx, **kw: settled.append(kw["checkpoint"]["owner_id"]) or "published")
    with _parent(env) as parent:
        operation = _checkpointed_operation(env, parent)
        pointer = {"state": review_operation.OPERATION_DISPATCHED, "surface": "task_acceptance",
                   "controller": operation.identity, "retry_key": operation.retry_key, "task_attempt": 1}
        review_operation._update_operations(env.root, TASK, lambda rows: {**rows, operation.owner_id: pointer},
                                            create=True)
        try:
            operation.maybe_close()  # a direct turn in the server process: the final write fails once
            review_operation.collect_orphaned_operations_softly(env.root)
            assert review_operation._LIVE.get(operation.owner_id) is operation and settled == []
            healthy[0] = True
            review_operation.collect_orphaned_operations_softly(env.root)  # the next ordinary pass
            assert operation.owner_id not in review_operation._LIVE
            rows = load_task_result(env.root, TASK)[review_operation.OPERATIONS_FIELD]
            expected = ([operation.owner_id], "collected") if owed else ([], "closed")
            assert (settled, rows[operation.owner_id]["state"]) == expected
        finally:
            with review_operation._LOCK:
                review_operation._LIVE.pop(operation.owner_id, None)
