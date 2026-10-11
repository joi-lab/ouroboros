"""Boot-resume of the assisted managed-update resolver after a terminal shutdown.

The live-observed wedge (v6.105.x E2E): SIGTERM during ``assisted_resolution``
writes a durable terminal (cancelled) result for the recorded resolver task; on
boot the resume re-enqueued the SAME id, which ``_drop_cancelled_pending``
silently dropped pre-assignment — the tx sat in ``assisted_resolution`` with no
resolver task and no orphan watchdog (it only arms on task_done). These tests
pin the fix: a terminal recorded resolver is re-enqueued under a FRESH task id,
the tx's recorded id follows it, and the paid-review-cycle money ceiling stays
keyed to the ORIGINAL resolver id across the whole resume chain.
"""

import pathlib
from types import SimpleNamespace

import supervisor.update_merge as update_merge
from tests import test_update_merge_assisted as tua


def _stub_resolver_queue(monkeypatch):
    """Capture enqueue_task calls with an empty live queue (no pool spawn).

    Like the real queue, the stub returns the admitted row; a first admission's
    receipt lands under the test's own drive root."""
    import supervisor.git_ops as git_ops
    import supervisor.queue as queue
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "ensure_worker_pool_started", lambda **_kwargs: True)
    monkeypatch.setattr(workers, "PENDING", [])
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(queue, "DRIVE_ROOT", git_ops.DRIVE_ROOT)
    enqueued = []
    monkeypatch.setattr(queue, "enqueue_task",
                        lambda task, front=False, **_kwargs: enqueued.append(task) or task)
    return enqueued


def test_boot_resume_reenqueues_fresh_task_when_recorded_resolver_is_terminal(
    tmp_path, monkeypatch
):
    """Wedge reproduction through the REAL boot finalize: the resume must enqueue
    a FRESH task id, move the tx's recorded id to it, and disclose the recovery
    with a typed supervisor row (old behavior re-enqueued the terminally
    cancelled id, which the pre-assignment drop silently swallowed)."""
    from ouroboros.task_results import STATUS_CANCELLED, write_task_result

    repo, head, plan, tx = tua._materialized_conflict_tx(tmp_path, monkeypatch)
    tua._stub_worker_gates(monkeypatch)
    enqueued = _stub_resolver_queue(monkeypatch)
    write_task_result(tmp_path / "data", "resolver", STATUS_CANCELLED,
                      result="Cancelled at shutdown.")

    result = update_merge.finalize_managed_update_on_boot(supervisor_ready=True)

    assert result == {"finalized": False, "resumed": True, "resolution_attempts": 1}
    assert len(enqueued) == 1
    fresh = enqueued[0]
    fresh_id = str(fresh["id"])
    assert fresh_id != "resolver" and fresh_id.startswith("update_assisted_merge_")
    recovered = update_merge.read_update_tx()
    # The tx's recorded id follows the fresh task; the ORIGINAL id is pinned once.
    assert recovered["task_id"] == fresh_id
    assert recovered["original_root_task_id"] == "resolver"
    # The fresh task is the authorized resolver of the recovered tx.
    assert update_merge.authorized_assisted_task(fresh_id, fresh["metadata"]) == recovered
    # Paid-cycle ceiling continuity: the metadata pins the ORIGINAL root.
    assert fresh["metadata"]["root_task_id"] == "resolver"
    # Operator-visible typed recovery row.
    rows = tua._supervisor_events(tmp_path, "managed_update_assisted_reenqueued_fresh")
    assert rows and rows[-1]["task_old"] == "resolver"
    assert rows[-1]["task_new"] == fresh_id
    assert rows[-1]["prior_status"] == STATUS_CANCELLED


def test_fresh_reenqueue_keeps_the_original_root_across_repeated_resumes(
    tmp_path, monkeypatch
):
    """A second terminal boot-resume mints another fresh id but never re-points
    ``original_root_task_id``: the money ceiling stays keyed to the FIRST id."""
    from ouroboros.task_results import STATUS_CANCELLED, write_task_result

    repo, head = tua._init_repo(tmp_path)
    tua._point_at(monkeypatch, tmp_path, repo, head)
    enqueued = _stub_resolver_queue(monkeypatch)
    tx = {
        "phase": "assisted_resolution", "task_id": "second-resolver",
        "original_root_task_id": "resolver", "target_sha": "b" * 40,
        "owner_chat_id": 0,
    }
    update_merge.write_update_tx(tx)
    write_task_result(tmp_path / "data", "second-resolver", STATUS_CANCELLED,
                      result="Cancelled at shutdown.")

    task_id = update_merge.enqueue_assisted_resolution_task(tx)

    assert task_id not in {"resolver", "second-resolver"}
    assert tx["original_root_task_id"] == "resolver"
    assert enqueued[0]["metadata"]["root_task_id"] == "resolver"
    assert update_merge.read_update_tx()["task_id"] == task_id


def test_enqueue_keeps_task_id_when_prior_result_is_not_terminal(tmp_path, monkeypatch):
    """The normal resume path is unchanged: a non-terminal durable result (e.g.
    ``interrupted`` from a crash) re-enqueues under the SAME recorded id — the
    pre-assignment drop only strikes truly terminal results."""
    from ouroboros.task_results import STATUS_INTERRUPTED, write_task_result

    repo, head = tua._init_repo(tmp_path)
    tua._point_at(monkeypatch, tmp_path, repo, head)
    enqueued = _stub_resolver_queue(monkeypatch)
    tx = {
        "phase": "assisted_resolution", "task_id": "resolver-task",
        "target_sha": "b" * 40, "owner_chat_id": 0,
    }
    update_merge.write_update_tx(tx)
    write_task_result(tmp_path / "data", "resolver-task", STATUS_INTERRUPTED,
                      result="mid-flight restart")

    task_id = update_merge.enqueue_assisted_resolution_task(tx)

    assert task_id == "resolver-task"
    assert tx.get("original_root_task_id") is None
    assert enqueued and enqueued[0]["id"] == "resolver-task"
    # First-run root pinning is behavior-preserving: it equals the task's own id.
    assert enqueued[0]["metadata"]["root_task_id"] == "resolver-task"
    assert not tua._supervisor_events(tmp_path, "managed_update_assisted_reenqueued_fresh")


def test_resumed_resolver_root_continuity_for_paid_cycle_ceiling(tmp_path, monkeypatch):
    """Ceiling continuity (money, per ROOT task): paid review cycles recorded
    under the ORIGINAL resolver id still count for the commit gate of the
    fresh-id resumed resolver, because ``resolve_root_task_id`` honors the
    host-enqueued ``metadata.root_task_id`` pin before any live task id."""
    from ouroboros.tools.commit_gate import count_paid_review_cycles, resolve_root_task_id
    from ouroboros.review_state import CommitAttemptRecord, make_repo_key, update_state, _utc_now
    from ouroboros.task_results import STATUS_CANCELLED, write_task_result

    repo, head = tua._init_repo(tmp_path)
    tua._point_at(monkeypatch, tmp_path, repo, head)
    enqueued = _stub_resolver_queue(monkeypatch)
    tx = {
        "phase": "assisted_resolution", "task_id": "resolver",
        "target_sha": "b" * 40, "owner_chat_id": 0,
    }
    update_merge.write_update_tx(tx)
    write_task_result(tmp_path / "data", "resolver", STATUS_CANCELLED,
                      result="Cancelled at shutdown.")
    # One paid wave already dispatched by the ORIGINAL resolver before the restart.
    repo_key = make_repo_key(pathlib.Path(repo))

    def _mutate(state):
        state.attempts.append(CommitAttemptRecord(
            ts=_utc_now(), commit_message="msg", status="blocked",
            block_reason="triad_critical", block_class="verdict",
            repo_key=repo_key, tool_name="commit_reviewed", task_id="resolver",
            attempt=1, phase="blocking_review", paid=True,
            pre_review_fingerprint="fp-1", root_task_id="resolver",
        ))

    update_state(pathlib.Path(tmp_path / "data"), _mutate)

    fresh_id = update_merge.enqueue_assisted_resolution_task(tx)
    assert fresh_id != "resolver"

    ctx = SimpleNamespace(
        drive_root=tmp_path / "data", repo_dir=repo,
        task_id=fresh_id, task_metadata=enqueued[0]["metadata"],
    )
    root = resolve_root_task_id(ctx)
    assert root == "resolver"
    assert count_paid_review_cycles(ctx, root_task_id=root) == 1
