"""The maintenance child's named jobs: the rest law, durable resume, and no history walk in the server.

Each job's second run over unchanged inputs opens no history file, decodes no task body and
commits nothing; a run killed after k items resumes at k from its durable row; a fresh server
process (a new dispatcher object, no module memory) finds every job where the rows left it.
"""
from __future__ import annotations

import builtins
import io
import json
import os
import pathlib

import pytest

from ouroboros import maintenance, maintenance_jobs, store
from ouroboros import projects_registry as pr


class _Killed(BaseException):
    """A hard stop between two items: no handler of the job runs (a SIGKILL's shape)."""


def _write_rows(path, chat_id, *, count=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(json.dumps({"chat_id": chat_id, "text": f"row {index}"}) + "\n")


def _archive(data, segments, chat_id=7):
    """SEGMENTS chat and progress segments of unrelated chats (none mentions a candidate)."""
    for index in range(segments):
        _write_rows(data / "archive" / f"chat_20260101T{index:06d}.jsonl", chat_id)
        _write_rows(data / "archive" / f"progress_20260101T{index:06d}.jsonl", chat_id)


@pytest.fixture
def opened(monkeypatch):
    """Every path opened through ``open``/``io.open``/``os.open`` while the fixture is active."""
    seen = []
    real_open, real_io, real_os = builtins.open, io.open, os.open

    def record(path):
        try:
            seen.append(pathlib.Path(os.fsdecode(path)).resolve(strict=False))
        except TypeError:
            pass  # a file descriptor

    def py_open(file, *args, **kwargs):
        record(file)
        return real_open(file, *args, **kwargs)

    def io_open(file, *args, **kwargs):
        record(file)
        return real_io(file, *args, **kwargs)

    def os_open(path, *args, **kwargs):
        record(path)
        return real_os(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", py_open)
    monkeypatch.setattr(io, "open", io_open)
    monkeypatch.setattr(os, "open", os_open)
    return seen


def _under(paths, directory):
    directory = pathlib.Path(directory).resolve()
    return [path for path in paths if path.is_relative_to(directory)]


def _decodes(monkeypatch):
    """Task-body reads (single documents and batched fetches)."""
    from ouroboros import task_store

    reads = []
    body_text, decode = store.Txn.body_text, task_store._decode

    def text(txn, task_id):
        reads.append(task_id)
        return body_text(txn, task_id)

    def batch(rows, handoffs):
        reads.extend(row["task_id"] for row in rows)
        return decode(rows, handoffs)

    monkeypatch.setattr(store.Txn, "body_text", text)
    monkeypatch.setattr(task_store, "_decode", batch)
    return reads


def _revision(data):
    with store.open_store(data, write=False) as txn:
        return txn.revision()


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    return root


def test_reconcile_and_room_reads_open_no_archive_after_dialogue_import(data, opened):
    from tests._dialogue import import_all

    pr.create_project(data, "never-active", origin="reconcile")
    _archive(data, 5)
    import_all(data)
    from ouroboros import dialogue_store
    assert dialogue_store.counts(data) == {"chat": 10, "progress": 10}
    opened.clear()
    pr.reconcile_projects(data)
    pr.reconcile_projects(data)
    [room] = pr.projects_summary(data)
    assert room["has_thread_activity"] is False
    assert _under(opened, data / "archive") == []
    assert "activity_backfill" not in maintenance.JOBS


def _promotion_member(parent, task):
    """A settled child whose call inventory misses one blob: a deterministic refusal to remember."""
    from ouroboros import headless, observability
    from ouroboros.task_results import write_task_result

    child = headless.prepare_task_drive(parent, task, "empty")
    call = observability.persist_call(child, task_id=task, call_id="broken", call_type="tool_call",
                                      payload={"tool": "read_file", "result": f"original of {task}"})
    manifest = json.loads(pathlib.Path(call["manifest_ref"]["path"]).read_text(encoding="utf-8"))
    pathlib.Path(manifest["full_payload_ref"]["path"]).unlink()
    write_task_result(child, task, "completed", result="answer", artifact_status="ready")
    headless.copy_child_task_result(parent, {"id": task, "drive_root": str(child)})
    return child


def test_child_ref_promotion_rests_on_its_second_run(tmp_path, opened, monkeypatch):
    """The remembered refusal is a durable row: the next run (any process; the memo is read
    from the store, never from module memory) reads no evidence and no body and commits nothing."""
    parent = tmp_path / "canonical"
    child = _promotion_member(parent, "member")
    assert "child_ref_promotion" in maintenance.due(parent)
    assert maintenance.run_job(parent, "child_ref_promotion")["state"] == "done"
    assert "child_ref_promotion" not in maintenance.due(parent)
    before, decodes = _revision(parent), _decodes(monkeypatch)
    opened.clear()
    assert maintenance.run_job(parent, "child_ref_promotion") is None  # at rest: only its facts were read
    assert decodes == [] and _revision(parent) == before
    assert _under(opened, parent / "observability") == [] and _under(opened, child / "observability") == []


def test_child_ref_promotion_killed_after_k_members_resumes_at_k(tmp_path, monkeypatch):
    from ouroboros import observability

    parent = tmp_path / "canonical"
    for task in ("t1", "t2", "t3"):
        _promotion_member(parent, task)
    walk, walked = observability._retry_pending_child_ref_promotion, []

    def killed_on_the_third(parent_root, child, task_id, result, **kwargs):
        if len(walked) == 2:
            raise _Killed()
        walked.append(task_id)
        return walk(parent_root, child, task_id, result, **kwargs)

    monkeypatch.setattr(observability, "_retry_pending_child_ref_promotion", killed_on_the_third)
    with pytest.raises(_Killed):
        maintenance.run_job(parent, "child_ref_promotion")
    assert sorted(maintenance_jobs.job(parent, "child_ref_promotion")["observed"]) == walked
    monkeypatch.setattr(observability, "_retry_pending_child_ref_promotion",
                        lambda parent_root, child, task_id, result, **kw: walked.append(task_id) or walk(
                            parent_root, child, task_id, result, **kw))
    assert maintenance.run_job(parent, "child_ref_promotion")["state"] == "done"
    assert sorted(walked) == ["t1", "t2", "t3"], "a member remembered before the kill was walked again"


def test_planner_statistics_once_per_store_and_again_after_tenfold_growth(data):
    from ouroboros.task_results import write_task_result

    for index in range(3):
        write_task_result(data, f"task-{index}", "completed", result="answer")
    assert "sqlite_analyze" not in maintenance.due(data), "never by itself a reason to spawn the child"
    assert maintenance.run_job(data, "sqlite_analyze") == {"state": "done", "items": 3}
    with store.open_store(data, write=False) as txn:
        assert txn.conn.execute("SELECT COUNT(*) FROM sqlite_stat1 WHERE tbl='tasks'").fetchone()[0] > 0
    before = _revision(data)
    assert maintenance.run_job(data, "sqlite_analyze") is None and _revision(data) == before
    for index in range(3, 30):
        write_task_result(data, f"task-{index}", "completed", result="answer")
    assert maintenance.run_job(data, "sqlite_analyze") == {"state": "done", "items": 30}


def test_legacy_reconcile_runs_only_when_scheduled_and_tolerates_a_body_without_it(data, monkeypatch):
    from ouroboros import store_import

    calls = []
    monkeypatch.setattr(store_import, "run_legacy_reconcile",
                        lambda root, **kw: calls.append(kw) or maintenance_jobs.record(root, "legacy_reconcile",
                                                                                       state="done", done=4),
                        raising=False)
    assert maintenance.run_job(data, "legacy_reconcile") is None and calls == [], "never scheduled: never run"
    maintenance_jobs.record(data, "legacy_reconcile", state="pending", reason="a store-unaware body ran")
    assert maintenance.due(data) == ["legacy_reconcile"], "a scheduled reconcile is a reason to spawn"
    assert maintenance.run_job(data, "legacy_reconcile") == {"state": "done", "items": 4}
    assert set(calls[0]) == {"should_stop"} and maintenance.due(data) == []
    monkeypatch.delattr(store_import, "run_legacy_reconcile")
    maintenance_jobs.record(data, "legacy_reconcile", state="pending")
    assert maintenance.run_job(data, "legacy_reconcile") == {"state": "pending", "items": 0}


def test_a_retained_drive_rests_until_its_inputs_change(data, monkeypatch):
    """Drive settlement stays in the server (the queue lock and the liveness probe are its), but
    a drive retained for a reason a later pass would meet again is not decoded, copied or hashed
    on every pass: its verdict and the cursor are the ``drive_settlement`` row. A row change
    re-attempts it, and the settlement then succeeds."""
    from ouroboros import headless, server_maintenance as sm, task_custody
    from ouroboros.startup_migrations import prepare_startup_state
    from ouroboros.task_results import load_task_result, write_task_result

    prepare_startup_state(data)
    monkeypatch.setattr(sm, "DATA_DIR", data)
    monkeypatch.setattr("ouroboros.retention.age_cutoff", lambda *a, **k: 4_000_000_000)
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: False)
    drive = headless.prepare_task_drive(data, "held", "empty")
    write_task_result(data, "held", "completed", result="answer", child_drive_root=str(drive),
                      root_phase_checkpoint={"post_task_synthesis": "running"})
    sm._run_drive_custody_pass()
    row = maintenance_jobs.job(data, "drive_settlement")
    assert row["retained"]["held"]["reason"] == "post_work_open" and drive.is_dir()

    settle, attempts = task_custody.settle_child_drive, []
    with monkeypatch.context() as patch:
        patch.setattr(task_custody, "settle_child_drive",
                      lambda *a, **k: attempts.append(a[1]) or settle(*a, **k))
        before, decodes = _revision(data), _decodes(patch)
        sm._run_drive_custody_pass()
        assert attempts == [] and decodes == [] and _revision(data) == before, "a resting verdict was re-attempted"
    # Only the probes go; this install stays bound while the post-work closes.
    write_task_result(data, "held", "completed", root_phase_checkpoint={"post_task_synthesis": "completed"})
    sm._run_drive_custody_pass()
    assert not drive.exists() and "held" not in maintenance_jobs.job(data, "drive_settlement")["retained"]
    assert load_task_result(data, "held")["status"] == "completed"


def _setup_job(name, data, monkeypatch):
    """Synthetic inputs that give job NAME real work on its first run."""
    import time

    from ouroboros import store_import
    from ouroboros.task_results import write_task_result

    if name == "legacy_reconcile":
        monkeypatch.setattr(store_import, "run_legacy_reconcile", lambda root, **kw: maintenance_jobs.record(
            root, "legacy_reconcile", state="done", done=1), raising=False)
        maintenance_jobs.record(data, "legacy_reconcile", state="pending")
    elif name == "child_ref_promotion":
        _promotion_member(data, "member")
    elif name == "custody_trash":
        moved = data / "state" / "custody_trash" / "task-0123456789ab" / "data"
        moved.mkdir(parents=True)
        (moved / "result.json").write_text("{}", encoding="utf-8")
    elif name == "temp_sweep":
        orphan = data / "state" / ".state.json.tmp.1.2.abc"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_text("x", encoding="utf-8")
        os.utime(orphan, (time.time() - 7200,) * 2)
    elif name == "sqlite_analyze":
        write_task_result(data, "task", "completed", result="answer")
    elif name == "evidence_packing":
        from tests.test_evidence_packs import _task

        _task(data, "settled", blobs=1)
    elif name == "dialogue_import":
        from ouroboros import dialogue_import
        from tests._dialogue import seed

        seed(data, archives={"chat_20261001T000000.jsonl": [{"ts": "2026-10-01T00:00:00Z", "direction": "in",
                                                             "chat_id": 1, "text": "archived"}]})
        dialogue_import.cutover(data)  # plans the archive segment; the job imports it
    elif name == "hot_facts":
        from ouroboros import store

        write_task_result(data, "older", "completed", result="answer")
        with store.open_store(data, write=True) as txn:  # an older writer left the hot columns unset
            txn.conn.execute("UPDATE tasks SET owner_pause_state=NULL WHERE task_id='older'")


@pytest.mark.parametrize("name", maintenance.JOB_NAMES)
def test_every_job_rests_on_unchanged_inputs(name, data, opened, monkeypatch):
    """The rest law, for every named job: once its work is done, another run over unchanged
    inputs reads only its facts (no history file opened, no task body decoded, nothing
    committed), and a fresh server process's dispatcher finds nothing due."""
    _setup_job(name, data, monkeypatch)
    outcomes = [maintenance.run_job(data, name) for _ in range(3)]  # a packing pass that unlinked owes one look
    assert outcomes[0] is not None and outcomes[-1] is None, outcomes
    before, decodes = _revision(data), _decodes(monkeypatch)
    opened.clear()
    assert maintenance.run_job(data, name) is None
    assert decodes == [] and _revision(data) == before
    for history in ("archive", "observability", "task_results", "state/headless_tasks"):
        assert _under(opened, data / history) == [], history
    assert name not in maintenance.due(data)


def test_no_history_sized_duty_runs_in_the_server_and_the_child_spawns_under_custody(data, monkeypatch):
    """Both cadences of the supervisor's maintenance tick run with every moved duty armed to fail
    in this process: none runs, no thread carries one's name, and the dispatcher spawns the one
    maintenance child through the custodied spawn (own module, session custody, own group)."""
    import threading

    from ouroboros import evidence_packer, observability, process_custody, server_maintenance as sm, task_custody, temp_sweep
    from tests.test_evidence_packs import REPO, _FakeChild

    ran = []

    def refuse(name):
        def run(*_args, **_kwargs):
            ran.append(name)
            raise AssertionError(f"{name} ran in the server process")
        return run

    monkeypatch.setattr(evidence_packer, "packing_pending", refuse("evidence discovery"))
    monkeypatch.setattr(evidence_packer, "run_pass", refuse("evidence packing"))
    monkeypatch.setattr(observability, "retry_pending_child_ref_promotions", refuse("child-ref promotion"))
    monkeypatch.setattr(task_custody, "remove_custody_trash", refuse("trash removal"))
    sweep = temp_sweep.sweep_stale_temp_files
    monkeypatch.setattr(temp_sweep, "sweep_stale_temp_files", lambda root, **kw: (
        refuse("atomic temp walk")() if kw.get("atomic_temps", True) else sweep(root, **kw)))
    started, start = [], threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda thread: started.append(thread) or start(thread))
    spawned = []
    report = json.dumps({"status": "completed", "jobs": {}}).encode()
    monkeypatch.setattr(process_custody, "spawn_supervised", lambda cmd, **kw: spawned.append((cmd, kw)) or _FakeChild(report))
    monkeypatch.setattr(process_custody, "current_custody_session_id", lambda: "session")
    monkeypatch.setattr("ouroboros.config.REPO_DIR", REPO)
    unit = maintenance.MaintenanceChild()
    monkeypatch.setattr(maintenance, "child", unit)
    monkeypatch.setattr(sm, "DATA_DIR", data)
    monkeypatch.setattr(sm, "_periodic_zombie_reconcile", lambda **_kw: None)
    maintenance_jobs.record(data, "legacy_reconcile", state="pending")  # a scheduled job: a reason to spawn
    pr.create_project(data, "never-active", origin="reconcile")
    _archive(data, 2)
    (data / "state" / "custody_trash" / "moved-0123456789ab").mkdir(parents=True)

    sm._periodic_supervisor_maintenance([0.0], [0.0])  # the 600 s and 300 s cadences are due too
    while any(thread.is_alive() for thread in started):  # every pass and the dispatch it ends with
        for thread in list(started):
            thread.join(20)

    assert ran == []
    names = {thread.name for thread in started}
    assert "maintenance-child" in names and "evidence-packer" not in names
    # The process's first look and the reconcile pass's look each spawn while the scheduled job
    # stays due (this stub child does no work); every spawn is the custodied one.
    assert 1 <= len(spawned) <= 2
    for cmd, kwargs in spawned:
        assert cmd[1:3] == ["-m", "ouroboros.maintenance"] and cmd[cmd.index("--data-root") + 1] == str(data)
        assert kwargs["purpose"] == "maintenance" and kwargs["scope"] == "session"
        assert kwargs.get("new_process_group", True) and kwargs["env"]["OUROBOROS_DATA_DIR"] == str(data)


def test_the_real_child_process_runs_the_due_jobs_and_reports(data):
    """End to end, one real ``python -m ouroboros.maintenance`` on a synthetic root: the custodied
    spawn, the child's own facts, its jobs and its bounded report in ``logs/supervisor.jsonl``."""
    from tests.test_evidence_packs import REPO

    pr.create_project(data, "never-active", origin="reconcile")
    _archive(data, 2)
    (data / "state" / "custody_trash" / "moved-0123456789ab").mkdir(parents=True)
    (data / "logs").mkdir(exist_ok=True)
    from ouroboros import dialogue_import

    dialogue_import.cutover(data)  # as the boot does before the dialogue opens: the archive is planned
    assert maintenance.due(data) == ["dialogue_import", "custody_trash"]
    unit = maintenance.MaintenanceChild()
    assert unit.tick(data, REPO)
    unit._thread.join(240)
    assert not unit._thread.is_alive() and unit.live_children() == []
    rows = [json.loads(line) for line in (data / "logs" / "supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
    finished = [row for row in rows if row.get("type") == "maintenance" and row["phase"] != "started"]
    assert [row["phase"] for row in finished] == ["completed"], finished
    assert finished[0]["exit_code"] == 0
    assert "activity_backfill" not in finished[0]["jobs"]
    assert finished[0]["jobs"]["custody_trash"] == {"state": "done", "items": 1}
    assert finished[0]["jobs"]["dialogue_import"]["state"] == "done"
    assert pr.projects_summary(data)[0]["has_thread_activity"] is False
    assert maintenance.due(data) == [], "the child left every job at rest"
