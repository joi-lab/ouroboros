"""Pool shutdown reconciles exact dead owners without replaying history per PID."""

import json
import queue as stdqueue
from pathlib import Path

import pytest

from ouroboros import delegate_custody, review_owner_custody, review_state


def _attempt(pid, *, status="reviewing", operation=None):
    return review_state.CommitAttemptRecord(
        ts="2020-01-01T00:00:00+00:00", commit_message=f"owner {pid}",
        status=status, attempt=pid + 1, paid=True,
        review_owner_session_id="test-generation", review_owner_pid=pid,
        triad_raw_results=[_slot(operation or f"op-{pid}")] if status == "reviewing" else [],
    )


def _slot(operation):
    return {"slot_id": operation, "operation_id": operation,
            "operation_state": "in_flight", "late_result_pending": True}


def _write_state(root, attempts):
    review_state.save_state(root, review_state.AdvisoryReviewState(attempts=attempts))
    return root / "state/advisory_review.json"


@pytest.mark.parametrize("attempts", [[], [_attempt(0)], [_attempt(11, status="succeeded")]])
def test_no_matching_active_owner_skips_archive_and_save(tmp_path, monkeypatch, attempts):
    path = _write_state(tmp_path, attempts)
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(review_owner_custody, "_recoverable_review_invocations",
                        lambda *_: pytest.fail("idle cleanup read the event archive"))
    monkeypatch.setattr(review_state, "_save_state_unlocked",
                        lambda *_: pytest.fail("idle cleanup rewrote review state"))

    result = review_owner_custody.reconcile_review_custody_after_confirmed_process_deaths(
        tmp_path, set(range(1, 25)),
    )

    assert result == {"reconciled": [], "expired": []}
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime


def test_active_owner_batch_reuses_one_history_snapshot(tmp_path, monkeypatch):
    first, second, live, legacy = (_attempt(pid) for pid in (101, 202, 303, 0))
    first.triad_raw_results.append(_slot("op-pending"))
    first.scope_raw_result = {"raw_results": [_slot("op-started")]}
    # A terminal top-level projection must not hide an in-flight slot.
    second.status = "failed"
    _write_state(tmp_path, [first, second, live, legacy])
    for token, operation in (("inv-pending", "op-pending"), ("inv-started", "op-started")):
        assert delegate_custody.record_start_requested(
            tmp_path, invocation_id=token, operation_id=operation,
            surface="multi_model_review", request={"prompt": "review"},
        )
    assert delegate_custody.record_started(tmp_path, delegate_custody.RunCustody(
        run_id="run-started", invocation_id="inv-started", task_id="task-a",
    ))
    # The original request owns the invocation's operation, even after a duplicate.
    assert delegate_custody.record_start_requested(
        tmp_path, invocation_id="inv-started", operation_id="wrong-operation",
        surface="multi_model_review", request={"prompt": "duplicate"},
    )
    from ouroboros import delegate_custody_memo

    scans = []
    original = delegate_custody_memo._refresh

    def counted(drive_root, *args, **kwargs):
        # One custody refresh per reconcile: the batch shares that snapshot
        # across replay, pending and invocation projections (a `replay()` that
        # dropped `rows=` would refresh a second time and show here).
        scans.append(drive_root)
        return original(drive_root, *args, **kwargs)

    monkeypatch.setattr(delegate_custody_memo, "_refresh", counted)
    result = review_owner_custody.reconcile_review_custody_after_confirmed_process_deaths(
        tmp_path, {101, 202},
    )
    by_pid = {item.review_owner_pid: item for item in review_state.load_state(tmp_path).attempts}

    assert len(scans) == 1
    assert {item.review_owner_pid for item in result["reconciled"]} == {101, 202}
    assert by_pid[101].triad_raw_results[0]["failure_code"] == "process_local_review_worker_lost"
    assert by_pid[101].triad_raw_results[1]["pending_invocation_id"] == "inv-pending"
    assert by_pid[101].scope_raw_result["raw_results"][0]["pending_invocation_id"] == "inv-started"
    assert by_pid[101].status == "reviewing" and by_pid[101].late_result_pending
    assert by_pid[202].status == "failed" and not by_pid[202].late_result_pending
    assert by_pid[303].triad_raw_results == live.triad_raw_results
    assert by_pid[0].triad_raw_results == legacy.triad_raw_results


def test_archive_discovery_releases_lock_and_preserves_concurrent_checkpoint(tmp_path, monkeypatch):
    path = _write_state(tmp_path, [_attempt(101), _attempt(202)])
    checkpointed = []

    def discover(_root):
        def checkpoint(state):
            item = next(item for item in state.attempts if item.review_owner_pid == 101)
            item.triad_raw_results[0]["pending_invocation_id"] = "concurrent-invocation"
            item.late_result_pending = True
            checkpointed.append(True)
        # This real locked write fails if discovery retains the review-state lock.
        review_state.update_state(tmp_path, checkpoint)
        return {}

    monkeypatch.setattr(review_owner_custody, "_recoverable_review_invocations", discover)
    result = review_owner_custody.reconcile_review_custody_after_confirmed_process_deaths(
        tmp_path, {101, 202},
    )
    by_pid = {item.review_owner_pid: item for item in review_state.load_state(tmp_path).attempts}

    assert checkpointed == [True] and path.exists()
    assert by_pid[101].triad_raw_results[0]["pending_invocation_id"] == "concurrent-invocation"
    assert by_pid[101].status == "reviewing" and by_pid[101].late_result_pending
    assert by_pid[202].status == "failed"
    assert [item.review_owner_pid for item in result["reconciled"]] == [202]


def test_failed_discovery_retains_unknown_custody_without_save(tmp_path, monkeypatch):
    path = _write_state(tmp_path, [_attempt(101)])
    before = path.read_bytes()

    def unavailable(_root):
        raise OSError("archive unavailable")

    monkeypatch.setattr(review_owner_custody, "_recoverable_review_invocations", unavailable)
    review_owner_custody.reconcile_confirmed_dead_review_owners(tmp_path, {101})
    assert path.read_bytes() == before
    assert review_state.load_state(tmp_path).attempts[0].status == "reviewing"


def test_idle_check_strictly_refuses_malformed_authority(tmp_path, monkeypatch):
    path = tmp_path / "state/advisory_review.json"
    path.parent.mkdir()
    path.write_text('{"attempts": {"status": "reviewing"}}')
    before = path.read_bytes()
    monkeypatch.setattr(review_owner_custody, "_recoverable_review_invocations",
                        lambda *_: pytest.fail("malformed authority reached archive discovery"))
    with pytest.raises(ValueError, match="attempts must be a list"):
        review_owner_custody.reconcile_review_custody_after_confirmed_process_deaths(tmp_path, {101})
    assert path.read_bytes() == before


class _Process:
    def __init__(self, pid, *, dies_after=1):
        self.pid, self.dies_after = pid, dies_after
        self.alive, self.kills = True, 0

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass


@pytest.fixture
def pool(tmp_path, monkeypatch):
    from supervisor import queue, state, worker_pool_lifecycle, workers

    # conftest binds all process-global roots before tests import production helpers.
    assert state.DRIVE_ROOT == queue.DRIVE_ROOT == workers.DRIVE_ROOT
    assert state.DRIVE_ROOT != Path.home() / "Ouroboros/data"
    pending, running, slots = [], {}, {}
    for module in (queue, workers):
        monkeypatch.setattr(module, "DRIVE_ROOT", tmp_path)
        monkeypatch.setattr(module, "PENDING", pending)
        monkeypatch.setattr(module, "RUNNING", running)
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", tmp_path / "state/queue_snapshot.json")
    monkeypatch.setattr(workers, "WORKERS", slots)
    monkeypatch.setattr(workers, "_WORKER_POOL_DISABLED_REASON", "")
    for name in ("ADMISSION_RESERVATIONS", "ACCEPTANCE_FENCES", "BUDGET_ROOT_FENCES"):
        monkeypatch.setattr(queue, name, {})

    def kill(pid, **kwargs):
        for worker in all_workers:
            if worker.proc.pid == pid:
                worker.proc.kills += 1
                if worker.proc.dies_after and worker.proc.kills >= worker.proc.dies_after:
                    worker.proc.alive = False

    all_workers = []
    monkeypatch.setattr(workers, "kill_worker_tree", kill)
    monkeypatch.setattr(worker_pool_lifecycle, "kill_worker_tree", kill)

    def add(pid, **kwargs):
        worker = workers.Worker(len(slots), _Process(pid, **kwargs), stdqueue.Queue())
        slots[worker.wid] = worker
        all_workers.append(worker)
        return worker

    return workers, add


def test_idle_24_worker_update_reconciles_once_without_history_or_rewrite(tmp_path, monkeypatch, pool):
    workers, add = pool
    pids = set(range(50001, 50025))
    for pid in pids:
        add(pid)
    path = _write_state(tmp_path, [_attempt(0, status="succeeded")])
    before = path.read_bytes()
    monkeypatch.setattr(review_owner_custody, "_recoverable_review_invocations",
                        lambda *_: pytest.fail("idle update read the event archive"))
    calls = []
    original = review_owner_custody.reconcile_review_custody_after_confirmed_process_deaths

    def reconcile(root, owners):
        calls.append(set(owners))
        return original(root, owners)

    monkeypatch.setattr(review_owner_custody, "reconcile_review_custody_after_confirmed_process_deaths", reconcile)
    assert workers.kill_workers_for_update(result_reason="Managed update") == []
    assert calls == [pids]
    assert path.read_bytes() == before
    snapshot = json.loads((tmp_path / "state/queue_snapshot.json").read_text())
    assert snapshot["worker_total"] == 0 and snapshot["reason"] == "kill_workers"


def test_update_fallback_reconciles_newly_dead_owner_but_retains_survivor(tmp_path, pool):
    workers, add = pool
    dead = add(50001, dies_after=3)  # first kill + backstop + updater's final kill
    survivor = add(50002, dies_after=None)
    _write_state(tmp_path, [_attempt(dead.proc.pid), _attempt(survivor.proc.pid)])

    assert workers.kill_workers_for_update(result_reason="Managed update") == ["worker:50002"]
    by_pid = {item.review_owner_pid: item for item in review_state.load_state(tmp_path).attempts}
    assert dead.proc.kills == 3 and not dead.proc.alive
    assert by_pid[50001].status == "failed"
    assert by_pid[50002].status == "reviewing" and survivor.proc.alive
