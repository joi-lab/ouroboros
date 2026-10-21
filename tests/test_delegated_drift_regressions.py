"""Focused authority-drift regressions kept outside the size-ratcheted isolation module."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ouroboros import delegate_custody as custody
from ouroboros.subagent_worktrees import provision_execution_snapshot, find_execution_snapshot
from test_delegated_run_isolation import _isolated_entry, _nanny_ctx, _seed_target


def test_drift_capture_freezes_evidence_and_safe_reject_releases_empty_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapDrift")
    entry = _isolated_entry(ctx, target, handle, run_id="run-drift")
    (target / "neighbor.txt").write_text("owner change\n", encoding="utf-8")

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_mutated_during_run"] == ["neighbor.txt"]
    manifest_path = custody.delegated_capture_dir(custody.custody_root(ctx), "t-nanny", "snapDrift") / "workspace_patch.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["authority_drift"]["paths"] == ["neighbor.txt"]
    (target / "neighbor.txt").unlink()

    replay = _capture_terminal_patch(ctx, entry)
    assert replay["status"] == "ready_no_changes"
    assert replay["target_mutated_during_run"] == ["neighbor.txt"]
    assert "Rejected delegated run" in _integrate_delegated_patch(
        ctx, "run-drift", "reject", "preserve for inspection")
    assert find_execution_snapshot("snapDrift") is None
    custody._CUSTODY.clear()


def test_excluded_untracked_baseline_drift_is_not_clean(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapSecretDrift")
    entry = _isolated_entry(ctx, target, handle, run_id="run-secret-drift")
    assert any(row.get("path") == ".env" and row.get("baseline") for row in handle.excluded_untracked)
    (target / ".env").write_text("SECRET=changed\n", encoding="utf-8")

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_mutated_during_run"] == [".env"]
    assert entry.patch_captured is True
    custody._CUSTODY.clear()


def test_nested_untracked_git_repository_does_not_break_snapshot_provision(tmp_path):
    target = _seed_target(tmp_path)
    nested = target / "vendor" / "inner"
    nested.mkdir(parents=True)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    (nested / "README").write_text("nested\n", encoding="utf-8")

    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nested", snapshot_id="snapNested")

    assert Path(handle.path).exists()
    assert any(row.get("reason") == "nested_repository" for row in handle.excluded_untracked)


def test_drift_uses_snapshot_registry_when_custody_root_differs(tmp_path, monkeypatch):
    from ouroboros.delegate_target_drift import _target_drift_evidence
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nanny", snapshot_id="snapSeparateRoots")
    entry = _isolated_entry(ctx, target, handle, run_id="run-separate-roots")
    assert find_execution_snapshot(handle.snapshot_id) is not None
    assert find_execution_snapshot(handle.snapshot_id, data_dir=entry.ledger_root) is None

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_drift_checked"] is True
    assert capture.get("target_mutated_during_run", []) == []
    (target / "untracked.txt").write_text("changed\n", encoding="utf-8")
    assert _target_drift_evidence(entry)["paths"] == ["untracked.txt"]
    custody._CUSTODY.clear()


_DEV_SERVER = ("import sqlite3, sys\n"
               "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
               "conn.execute('PRAGMA journal_mode=WAL')\n"
               "conn.execute('PRAGMA wal_autocheckpoint=0')\n"
               "conn.execute('CREATE TABLE IF NOT EXISTS t(v)')\n"
               "conn.execute(\"INSERT INTO t VALUES ('dev server row')\")\n"
               "print('ready', flush=True)\n"
               "for line in sys.stdin:\n"
               "    conn.execute(\"INSERT INTO t VALUES ('another row')\")\n"
               "    print('committed', flush=True)\n")


def _hold(database, *, killed):
    """Another process holding DATABASE open in WAL (a dev server), or killed after its first commit
    (its index and log left behind); ``commit()`` makes a live one commit again."""
    import subprocess

    holder = subprocess.Popen([sys.executable, "-I", "-c", _DEV_SERVER, str(database)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "ready"
    if killed:
        holder.kill()
        holder.wait()

    def commit():
        holder.stdin.write("commit\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "committed"

    def release():
        if holder.poll() is None:
            holder.stdin.close()
            try:
                holder.wait(timeout=60)
            except subprocess.TimeoutExpired:
                holder.kill()
                holder.wait()

    return commit, release


@pytest.mark.parametrize("ignored, killed", [(False, False), (True, False), (False, True)],
                         ids=["held", "held-db-and-log-ignored", "left-behind"])
def test_another_process_s_wal_index_is_excluded_without_a_baseline(tmp_path, monkeypatch, ignored, killed):
    """A project may hold another process's WAL database (a dev server, or one killed that left its
    index behind): its untracked ``app.db-shm`` is excluded from a mutating run's snapshot by its
    code, with no content baseline (its bytes are shared memory no raw reader opens), so the
    snapshot provisions, and the post-run capture and drift check pass it unread while it changes
    underneath (``.gitignore`` of ``*.db`` alone never covers it; with the log ignored too, nothing
    else moves)."""
    import hashlib

    from ouroboros.delegate_target_drift import _target_drift_evidence
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    if ignored:
        (target / ".gitignore").write_text("*.db\n*.db-wal\n", encoding="utf-8")
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    index = target / "app.db-shm"
    commit, release = _hold(target / "app.db", killed=killed)
    try:
        assert index.exists()
        handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapLiveWal")
        assert [row for row in handle.excluded_untracked if row.get("path") == "app.db-shm"] == [
            {"path": "app.db-shm", "reason": "sqlite_wal_index"}]
        assert not (Path(handle.path) / "app.db-shm").exists()
        entry = _isolated_entry(ctx, target, handle, run_id="run-live-wal")
        if not killed:
            before = hashlib.sha256(index.read_bytes()).hexdigest()  # this test process holds no connection to it
            commit()
            assert hashlib.sha256(index.read_bytes()).hexdigest() != before, "the index changed underneath"
        capture = _capture_terminal_patch(ctx, entry)
        evidence = _target_drift_evidence(entry)
    finally:
        release()
    assert evidence["checked"] is True and evidence["error"] == "", evidence
    assert capture["status"] == "ready_no_changes" and capture.get("target_drift_checked") is True, capture
    assert "app.db-shm" not in evidence["paths"] and "app.db-shm" not in capture.get("target_mutated_during_run", [])
    if ignored or killed:  # only the index moved, if anything, and it is no drift
        assert evidence["paths"] == [] and capture.get("target_mutated_during_run", []) == []
    custody._CUSTODY.clear()


@pytest.mark.parametrize("killed", [False, True], ids=["held", "left-behind"])
def test_a_task_copy_provisions_beside_another_process_s_wal_index(tmp_path, killed):
    """A ``self_worktree`` child's copy (``provision_worktree``) asks the same eligibility, so the
    index is left out unread there too and the child is admitted."""
    from ouroboros.subagent_worktrees import provision_worktree

    target = _seed_target(tmp_path)
    _commit, release = _hold(target / "app.db", killed=killed)
    try:
        handle = provision_worktree(repo_dir=target, task_id="t-self", source_is_system_repo=False,
                                    worktree_root=tmp_path / "snaps", data_dir=tmp_path / "data")
    finally:
        release()
    copied = {path.name for path in Path(handle.path).iterdir()}
    assert {"untracked.txt", "tracked.txt"} <= copied and "app.db-shm" not in copied


def _committed_index(tmp_path, mode):
    """A target whose history holds a dev server's WAL database WITH its index and log (``git add -A``
    while the server ran): still held, left behind by a killed server, or a fresh clone of that history
    where nothing holds the database. Returns the target and the server's release."""
    def git(cwd, *args):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "app.py").write_text("print(1)\n", encoding="utf-8")
    _commit, release = _hold(origin / "app.db", killed=mode == "left-behind")
    git(origin, "add", "-A")
    git(origin, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed with the dev database")
    assert (origin / "app.db-shm").exists()
    if mode != "fresh-clone":
        return origin, release
    release()  # a clean close removes the origin's index and log; the clone checks them out
    git(tmp_path, "clone", "-q", str(origin), "target")
    return tmp_path / "target", lambda: None


@pytest.mark.parametrize("kind", ["snapshot", "independent", "task-copy"])
@pytest.mark.parametrize("mode", ["held", "left-behind", "fresh-clone"])
def test_a_tracked_wal_index_is_dropped_from_every_copy(tmp_path, monkeypatch, mode, kind):
    """A committed index is refilled from the target with every tracked file, and its refusal failed
    every mutating run's snapshot, every evidence-only investigation's private copy and every
    ``self_worktree`` child's task copy, while held, left behind, and in a fresh clone where nothing
    holds it. It leaves the copy's baseline as a staged removal does, recorded by its code with no
    content baseline; the drift check passes it unread, and the copied database opens whole."""
    from ouroboros.delegate_target_drift import _target_drift_evidence
    from ouroboros.subagent_worktrees import provision_worktree

    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OUROBOROS_SUBAGENT_WORKTREE_ROOT", str(tmp_path / "snaps"))
    target, release = _committed_index(tmp_path, mode)
    try:
        if kind == "task-copy":
            handle = provision_worktree(repo_dir=target, task_id="t-self", source_is_system_repo=False,
                                        worktree_root=tmp_path / "snaps", data_dir=tmp_path / "data")
        else:
            handle = provision_execution_snapshot(target_root=target, task_id="t1", snapshot_id="snapTracked",
                                                  independent=kind == "independent")
            assert [row for row in handle.excluded_untracked if row.get("path") == "app.db-shm"] == [
                {"path": "app.db-shm", "reason": "sqlite_wal_index"}]
            evidence = _target_drift_evidence(types.SimpleNamespace(
                target_root=str(target), baseline_sha=handle.baseline_sha, snapshot_id=handle.snapshot_id,
                authority_source=""))
            assert (evidence["checked"], evidence["error"], evidence["paths"]) == (True, "", []), evidence
    finally:
        release()
    copy = Path(handle.path)
    assert sorted(path.name for path in copy.iterdir() if path.name != ".git") == ["app.db", "app.db-wal", "app.py"]
    with contextlib.closing(sqlite3.connect(copy / "app.db")) as conn:  # its index rebuilt from the copied log
        assert conn.execute("SELECT v FROM t").fetchall() == [("dev server row",)]


def test_an_index_a_child_stages_is_neither_captured_nor_applied_over_the_target_s(tmp_path, monkeypatch):
    """A child that runs the app in its copy and stages everything (``git add -A``) stages that copy's
    own index: the capture leaves it out of the patch and of the paths the integration stages, so the
    apply lands the child's change and never writes over the target's live index (a new-file patch for
    it was drift the apply refused, losing the child's change with it)."""
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target = _seed_target(tmp_path)
    (target / ".gitignore").write_text("*.db\n*.db-wal\n", encoding="utf-8")
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    index = target / "app.db-shm"
    _commit, release = _hold(target / "app.db", killed=False)
    try:
        handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapStaged")
        copy = Path(handle.path)
        _hold(copy / "app.db", killed=True)  # the app the child ran there, its index left behind
        (copy / "tracked.txt").write_text("one\ntwo\nchild\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=copy, check=True, capture_output=True)
        before = index.stat()
        entry = _isolated_entry(ctx, target, handle, run_id="run-staged-index")
        capture = _capture_terminal_patch(ctx, entry)
        manifest = json.loads((custody.delegated_capture_dir(custody.custody_root(ctx), "t-nanny", "snapStaged")
                               / "workspace_patch.json").read_text(encoding="utf-8"))
        out = _integrate_delegated_patch(ctx, "run-staged-index", "apply", "the child's change")
        after = index.stat()
    finally:
        release()
    assert capture["status"] == "ready_with_changes", capture
    assert "✅ Integrated" in out, out
    assert (target / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\nchild\n"
    assert manifest["tracked_changed"] == ["tracked.txt"]
    assert [row["path"] for row in manifest["tracked_excluded"]] == ["app.db-shm"]
    assert (after.st_ino, after.st_mtime_ns, after.st_size) == (before.st_ino, before.st_mtime_ns, before.st_size)
    assert subprocess.run(["git", "ls-files", "app.db-shm"], cwd=target, check=True,
                          capture_output=True, text=True).stdout == ""
    custody._CUSTODY.clear()
