"""Regression tests for process/resource leak fixes (PR-C).

Covers:
  * #9  — reap orphaned worker process groups from a prior server instance.
  * #4/#6 — respawn closes the old worker queue under the queue lock.
  * #7  — emergency cleanup joins killed children.
  * #3  — a cancelled subagent's child drive is removed immediately.
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ───────────────────────── #3: subagent drive cleanup ───────────────────────

def test_remove_subagent_task_drive(tmp_path):
    from ouroboros.headless import (
        HEADLESS_TASKS_DIR,
        TASK_DRIVES_DIR,
        remove_subagent_task_drive,
    )

    from ouroboros.task_results import STATUS_CANCELLED, write_task_result

    tid = "abcd1234"
    headless_dir = tmp_path / HEADLESS_TASKS_DIR / tid / "data"
    drive_dir = tmp_path / TASK_DRIVES_DIR / tid
    headless_dir.mkdir(parents=True)
    drive_dir.mkdir(parents=True)
    # No settled row, no probe, or a live owner: custody is not proven, nothing goes.
    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: False) is False
    write_task_result(tmp_path, tid, STATUS_CANCELLED, delegation_role="subagent")
    assert remove_subagent_task_drive(tmp_path, tid) is False
    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: True) is False
    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: None) is False
    assert headless_dir.is_dir() and drive_dir.is_dir()

    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: False) is True
    assert not (tmp_path / HEADLESS_TASKS_DIR / tid).exists()
    assert not (tmp_path / TASK_DRIVES_DIR / tid).exists()

    # idempotent / no error when nothing to remove
    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: False) is False
    # invalid task id is rejected, not raised
    assert remove_subagent_task_drive(tmp_path, "../escape", live=lambda _task: False) is False


def test_remove_task_scratch_never_promotes_forged_terminal_result(tmp_path):
    from ouroboros.headless import TASK_DRIVES_DIR, remove_subagent_task_drive
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result

    tid = "directchild"
    write_task_result(
        tmp_path,
        tid,
        STATUS_CANCELLED,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
    )
    child_drive = tmp_path / TASK_DRIVES_DIR / tid
    write_task_result(
        child_drive,
        tid,
        "completed",
        result="late complete result",
        trace_summary="late trace",
        artifact_status="ready",
        artifacts=[],
    )

    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: False) is True
    stored = load_task_result(tmp_path, tid) or {}
    assert "terminal_child_result_snapshot" not in stored
    assert not child_drive.exists()


def test_remove_subagent_drive_does_not_promote_custom_late_result(tmp_path):
    from ouroboros.headless import TASK_DRIVES_DIR, remove_subagent_task_drive
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result

    tid = "customchild"
    custom_child = tmp_path / "isolated-child-data"
    write_task_result(
        custom_child,
        tid,
        "completed",
        result="authoritative late result",
        trace_summary="late trace",
        artifact_status="ready",
        artifacts=[],
    )
    write_task_result(
        tmp_path,
        tid,
        STATUS_CANCELLED,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        child_drive_root=str(custom_child),
    )
    scratch = tmp_path / TASK_DRIVES_DIR / tid
    scratch.mkdir(parents=True)

    assert remove_subagent_task_drive(tmp_path, tid, live=lambda _task: False) is True
    stored = load_task_result(tmp_path, tid) or {}
    assert stored["status"] == STATUS_CANCELLED
    assert "terminal_child_result_snapshot" not in stored
    assert "authoritative late result" not in str(stored)
    assert not scratch.exists()


def test_cancel_running_subagent_leaves_its_drive_to_the_off_loop_settlement(tmp_path):
    # The cancellation custody family lives in task_lifecycle; its settlement
    # PUBLICATION half was split into supervisor/cancel_publication.py at the
    # module-size boundary. Neither half deletes the subagent's drive any more:
    # settlement copies and hashes the child store, which the cancel path must not
    # carry, so the off-loop drive-custody pass settles a cancelled subagent's drive
    # WITHOUT waiting out retention (the promptness the cancel path used to give).
    from ouroboros import headless
    from ouroboros.task_results import write_task_result

    custody_src = _read("supervisor/task_lifecycle.py")
    publish_src = _read("supervisor/cancel_publication.py")
    assert "remove_subagent_task_drive" not in publish_src and "remove_subagent_task_drive" not in custody_src
    assert "settle_child_drive" not in publish_src and "shutil.rmtree" not in publish_src
    assert "survived kill escalation" in custody_src
    assert custody_src.index("survived kill escalation") < custody_src.index("_publish_cancelled_task(\n")

    data = tmp_path / "data"
    fresh = headless.prepare_task_drive(data, "young1", "empty")
    write_task_result(data, "young1", "cancelled", result="killed", delegation_role="subagent", child_drive_root=str(fresh))
    kept = headless.prepare_task_drive(data, "young2", "empty")
    write_task_result(data, "young2", "completed", result="done", delegation_role="subagent", child_drive_root=str(kept))
    report = headless.prune_headless_task_drives(data, retention_days=7, live=lambda _task: False)
    assert [row["task_id"] for row in report["pruned"]] == ["young1"] and not fresh.exists()
    assert kept.is_dir() and report["skipped"] == [{"task_id": "young2", "reason": "younger_than_retention"}]


# ───────────────────────── #9: orphan worker reaping ────────────────────────

def test_reap_orphaned_workers(tmp_path, monkeypatch):
    import ouroboros.platform_layer as pl
    from ouroboros.utils import atomic_write_json
    from supervisor import workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path, raising=False)
    workers.WORKERS.clear()
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        workers._worker_pids_path(),
        {"server_pid": 999999, "workers": [{"pid": 111}, {"pid": 222}, {"pid": 333}]},
    )

    # 111 = alive + ours (own session leader) → must be killed
    # 222 = dead (empty cmdline) → skipped
    # 333 = alive but unrelated process (pid reused) → skipped
    cmds = {111: "python -B -c multiprocessing.spawn", 222: "", 333: "/usr/bin/SomethingElse"}
    monkeypatch.setattr(pl, "process_command", lambda pid: cmds.get(int(pid), ""))
    monkeypatch.setattr(pl, "process_group_id", lambda pid: int(pid))  # session leader
    killed_groups, killed_trees = [], []
    monkeypatch.setattr(pl, "kill_process_group_id", lambda pgid: killed_groups.append(int(pgid)))
    # The shared tree owner is the platform boundary. Mock it on every OS so
    # this unit never executes taskkill against a synthetic PID on Windows;
    # native leaf mechanics have their own real-process tests.
    monkeypatch.setattr(
        pl,
        "kill_pid_tree",
        lambda pid, **kwargs: killed_trees.append((int(pid), kwargs)),
    )

    n = workers.reap_orphaned_workers()

    assert n == 1
    assert killed_trees == [(111, {"exclude_pids": set()})]
    assert killed_groups == [111]


def test_record_worker_pids_roundtrip(tmp_path, monkeypatch):
    from ouroboros.utils import read_json_dict
    from supervisor import workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path, raising=False)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

    monkeypatch.setattr(
        workers, "WORKERS",
        {0: workers.Worker(wid=0, proc=_FakeProc(4242), in_q=None)},
        raising=False,
    )
    workers._record_worker_pids()
    data = read_json_dict(workers._worker_pids_path()) or {}
    assert {"pid": 4242} in (data.get("workers") or [])


# ───────────────────── #4/#6 + #7: source contracts ─────────────────────────

def test_respawn_closes_old_queue_under_lock():
    # respawn moved to the pool-lifecycle owner; the pool still spawns.
    src = _read("supervisor/worker_pool_lifecycle.py")
    assert "with _queue_lock:" in src
    assert "old.in_q.close()" in src
    assert "old.in_q.cancel_join_thread()" in src


def test_spawn_reaps_orphans_and_records_pids():
    src = _read("supervisor/workers.py")
    assert "reap_orphaned_workers()" in src
    assert "_record_worker_pids()" in src
    # reap guards against PID reuse and only group-kills its own setsid session
    assert "if pgid and pgid == pid and not shares_retained_group:" in _read("supervisor/worker_pool_lifecycle.py")


def test_emergency_cleanup_joins_children():
    src = _read("server.py")
    # force_kill is followed by a join so the Process object is reaped
    assert "child.join(timeout=2)" in src
