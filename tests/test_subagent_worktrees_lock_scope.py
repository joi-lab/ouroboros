"""The worktree ops lock guards shared metadata only (#1241).

One delegated snapshot of a project with 67,692 untracked files held the
machine-wide ``.worktree_ops.lock`` for 40 minutes, and every other mutating
``delegate_start`` on the machine waited 120 s and was refused. Listing,
classifying, hashing, populating, copying and deleting a snapshot's files now run
OUTSIDE the lock; the registry row, the baseline pin and the worktree admin dir
are the only things written under it — row first, so everything after it is
nameable by the startup GC. A held lock is a typed refusal that names its holder,
a dead holder is evicted at once, and a pre-POST refusal is definitely unrun.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from ouroboros import artifacts, subagent_worktrees as wt, workspace_patch_capture as capture
from ouroboros.platform_layer import _lock_identity, pid_is_alive
from tests._delegated_transport_shared import _owned_gateway_uses_each_test_transport  # noqa: F401
from tests.test_delegated_full_access import full_run  # noqa: F401
from tests.test_delegated_run_isolation import _git, _nanny_ctx, _seed_target

REPO = pathlib.Path(__file__).resolve().parents[1]


def _lock_held(snaps: pathlib.Path) -> bool:
    return bool(_lock_identity(pathlib.Path(snaps) / wt._LOCK_NAME))


def _provision(target, snaps, data, snapshot_id="snap1", task_id="t1"):
    return wt.provision_execution_snapshot(
        target_root=target, task_id=task_id, snapshot_id=snapshot_id, worktree_root=snaps, data_dir=data)


def _phase_spies(monkeypatch, snaps):
    """Record whether the lock was held when each phase ran."""
    seen: dict = {}
    real_verdicts, real_copy, real_git, real_git_env, real_save = (
        capture.untracked_binary_verdicts, artifacts.copy_artifact_file, wt._git, wt._git_env, wt._save_registry)

    def spy_git_env(repo_dir, *args, **kw):  # baseline staging goes through _git_env, not _git
        for marker in ("update-index", "write-tree", "commit-tree"):
            if marker in args:
                seen[marker] = _lock_held(snaps)
        return real_git_env(repo_dir, *args, **kw)

    def spy_verdicts(root, rels, **kw):
        seen["classify"] = _lock_held(snaps)
        return real_verdicts(root, rels, **kw)

    def spy_copy(source, destination, **kw):
        seen["copy"] = _lock_held(snaps)
        return real_copy(source, destination, **kw)

    def spy_git(repo_dir, *args, **kw):
        for marker in ("update-index", "commit-tree", "reset", "update-ref", "worktree"):
            if marker in args:
                seen[marker + ("_add" if marker == "worktree" and "add" in args else "")] = _lock_held(snaps)
        return real_git(repo_dir, *args, **kw)

    def spy_save(entries, data_dir=None):
        seen.setdefault("save_registry", []).append(_lock_held(snaps))
        return real_save(entries, data_dir)

    monkeypatch.setattr(capture, "untracked_binary_verdicts", spy_verdicts)
    monkeypatch.setattr(artifacts, "copy_artifact_file", spy_copy)
    monkeypatch.setattr(wt, "_git", spy_git)
    monkeypatch.setattr(wt, "_git_env", spy_git_env)
    monkeypatch.setattr(wt, "_save_registry", spy_save)
    return seen


def test_tree_walk_runs_outside_the_lock_and_shared_metadata_inside(tmp_path, monkeypatch):
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    seen = _phase_spies(monkeypatch, snaps)

    handle = _provision(target, snaps, data)

    assert seen["classify"] is False and seen["copy"] is False and seen["reset"] is False
    assert seen["update-index"] is False and seen["write-tree"] is False and seen["commit-tree"] is False
    assert seen["update-ref"] is True and seen["worktree_add"] is True
    assert seen["save_registry"] == [True, True]  # provisional row, then the final row
    assert not _lock_held(snaps)
    assert handle.provisioning_sec >= 0 and wt.find_execution_snapshot("snap1", data_dir=data)["file_baseline"] == {}


@pytest.mark.parametrize("same_target", [False, True])
def test_a_parked_provision_does_not_block_another(tmp_path, monkeypatch, same_target):
    target_a = _seed_target(tmp_path)
    (tmp_path / "other").mkdir()
    target_b = target_a if same_target else _seed_target(tmp_path / "other")
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    parked, release = threading.Event(), threading.Event()
    real_verdicts = capture.untracked_binary_verdicts
    observed: dict = {}

    def spy_verdicts(root, rels, **kw):
        if threading.current_thread().name == "provision-a":
            parked.set()
            assert release.wait(timeout=60), "the parked provision was never released"
        else:
            observed["b_saw_lock_held"] = _lock_held(snaps)
        return real_verdicts(root, rels, **kw)

    monkeypatch.setattr(capture, "untracked_binary_verdicts", spy_verdicts)
    errors: list = []

    def run_a():
        try:
            _provision(target_a, snaps, data, snapshot_id="snapA", task_id="ta")
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    thread_a = threading.Thread(target=run_a, name="provision-a")
    thread_a.start()
    assert parked.wait(timeout=60)
    index_before = (target_b / ".git" / "index").read_bytes()
    head_before = _git(target_b, "rev-parse", "HEAD").stdout

    b_done = threading.Event()

    def run_b():
        _provision(target_b, snaps, data, snapshot_id="snapB", task_id="tb")
        b_done.set()

    thread_b = threading.Thread(target=run_b, name="provision-b")
    thread_b.start()
    # With the old whole-function lock B would sit behind A forever; A is still parked.
    assert b_done.wait(timeout=120), "provision B did not finish while A was parked"
    assert observed["b_saw_lock_held"] is False
    release.set()
    thread_a.join(timeout=120)
    assert not errors and not thread_a.is_alive()
    assert (target_b / ".git" / "index").read_bytes() == index_before
    assert _git(target_b, "rev-parse", "HEAD").stdout == head_before
    rows = {row["snapshot_id"]: row for row in wt.list_worktrees(data_dir=data)}
    assert set(rows) == {"snapA", "snapB"}
    assert all(pathlib.Path(rows[s]["path"]).is_dir() for s in rows)


_HOLDER = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[2])
from ouroboros.platform_layer import acquire_exclusive_file_lock
fd = acquire_exclusive_file_lock(
    pathlib.Path(sys.argv[1]), timeout_sec=5, stale_sec=600,
    metadata=f"pid={os.getpid()} task=t-holder op=provision since=2026-09-24T00:00:00Z target=/tmp/with space")
assert fd is not None
print("HELD", os.getpid(), flush=True)
sys.stdin.readline()  # hold until the test closes our stdin (or kills us)
"""


def _hold_lock(snaps: pathlib.Path) -> "tuple[subprocess.Popen, int]":
    """A live lock holder in another process; returns it with the pid the holder
    itself wrote into the lock (on Windows a venv ``python.exe`` is a launcher whose
    CHILD is the interpreter, so ``proc.pid`` is not that pid)."""
    snaps.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(snaps / wt._LOCK_NAME), str(REPO)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    banner = proc.stdout.readline().split()
    assert banner[:1] == ["HELD"], banner
    return proc, int(banner[1])


def _release_holder(proc: subprocess.Popen, holder_pid: int) -> None:
    proc.kill()
    proc.stdin.close()  # the interpreter behind a launcher exits on EOF too
    proc.wait()
    # Wait for the HOLDER (not only the launcher) to be gone: it may still hold
    # the OS-level lock for a moment after EOF, and the dead-holder phase below
    # rewrites the lock file by hand.
    deadline = time.monotonic() + 10
    while pid_is_alive(holder_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pid_is_alive(holder_pid)


def test_a_live_holder_is_a_typed_refusal_and_a_dead_holder_is_evicted(tmp_path, monkeypatch):
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    monkeypatch.setattr(wt, "_LOCK_TIMEOUT_SEC", 0.5)
    holder, holder_pid = _hold_lock(snaps)
    try:
        started = time.monotonic()
        with pytest.raises(wt.WorktreeOpsLockBusy) as info:
            _provision(target, snaps, data)
        # A busy FIRST section registered nothing, so nothing is discarded: the typed
        # refusal arrives after the one wait, not after a second discard wait.
        assert time.monotonic() - started < 3
    finally:
        _release_holder(holder, holder_pid)
    busy = info.value
    assert busy.holder == {"pid": str(holder_pid), "task": "t-holder", "op": "provision",
                           "since": "2026-09-24T00:00:00Z", "target": "/tmp/with space"}
    assert busy.waited_sec > 0 and "t-holder" in str(busy)
    # Nothing was registered, pinned or checked out for the refused attempt.
    assert wt.find_execution_snapshot("snap1", data_dir=data) is None
    assert _git(target, "for-each-ref", "refs/ouroboros/").stdout == ""
    assert not list(snaps.glob("dlg_*"))

    # A holder that died (SIGKILL, panic) left its lock file behind: the owner-aware
    # stale check evicts it at once instead of refusing for _LOCK_STALE_SEC.
    lock_path = snaps / wt._LOCK_NAME
    lock_path.write_text(f"pid={holder.pid} task=t-dead op=provision since=x target=y", encoding="utf-8")
    started = time.monotonic()
    handle = _provision(target, snaps, data)
    assert time.monotonic() - started < 30 and pathlib.Path(handle.path).is_dir()  # far below _LOCK_STALE_SEC=600
    assert not _lock_held(snaps)


def test_a_provisioning_refusal_is_definitely_unrun_and_names_the_holder(tmp_path, monkeypatch):
    from ouroboros import delegate_custody as custody
    from ouroboros.delegate_shared import delegate_payload
    from ouroboros.subagent_bootstrap import _startup_refusal_definite
    from ouroboros.tools.delegate_integration import _provision_snapshot

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    drive = custody.custody_root(ctx)

    def busy(**kw):
        raise wt.WorktreeOpsLockBusy(tmp_path / "lock", 120.0, {"pid": "4242", "task": "t-x", "op": "provision"})

    monkeypatch.setattr(wt, "provision_execution_snapshot", busy)
    handle, refusal = _provision_snapshot(ctx, drive, str(target), "inv-busy")
    payload = delegate_payload(refusal)
    assert handle is None and payload["reason"] == "execution_snapshot_failed"
    assert payload["definitely_unrun"] is True and payload["cause"] == "lock_busy"
    assert payload["holder"]["pid"] == "4242" and payload["waited_sec"] == 120.0 and payload["retryable"] is True
    assert "t-x" in payload["detail"] and _startup_refusal_definite(payload)

    def broken(**kw):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(wt, "provision_execution_snapshot", broken)
    _handle, refusal = _provision_snapshot(ctx, drive, str(target), "inv-broken")
    payload = delegate_payload(refusal)
    assert payload["definitely_unrun"] is True and "cause" not in payload
    assert _startup_refusal_definite(payload)


def test_a_provisioning_refusal_leaves_a_durable_start_failed_row(full_run, monkeypatch):  # noqa: F811
    """The incident's two bootstrap refusals existed only inside the child's first
    prompt: no custody row, no event. A refused provision now settles its
    invocation durably, and never reaches the daemon."""
    from ouroboros import delegate_custody as custody
    from ouroboros.delegate_shared import delegate_payload
    from ouroboros.tools import delegate

    ctx, _target, facts = full_run

    def refused(**_kw):
        raise wt.WorktreeOpsLockBusy(pathlib.Path("/lock"), 120.0, {"pid": "7", "task": "t-other", "op": "provision"})

    monkeypatch.setattr(wt, "provision_execution_snapshot", refused)
    payload = delegate_payload(delegate._delegate_start(ctx, "Implement the fixture."))

    assert payload["reason"] == "execution_snapshot_failed" and payload["definitely_unrun"] is True
    assert payload["cause"] == "lock_busy" and payload["holder"]["task"] == "t-other"
    assert facts["requests"] == [], "a refused provision must never POST a run"
    rows = [json.loads(line) for line in custody.event_log_path(custody.custody_root(ctx))
            .read_text(encoding="utf-8").splitlines() if line.strip()]
    failed = [row for row in rows if row.get("type") == custody.START_FAILED]
    assert len(failed) == 1 and failed[0]["definite"] is True and failed[0]["invocation_id"]
    assert failed[0]["reason"] == "execution_snapshot_failed" and failed[0]["run_id"] == ""
    assert failed[0]["cause"] == "lock_busy" and failed[0]["holder"]["task"] == "t-other"  # the facts ride the row
    assert "t-other" in failed[0]["detail"]  # and so does the producer's own sentence


def test_configured_child_bootstrap_keeps_the_refusal_facts(tmp_path, monkeypatch):
    """The host pre-start of a configured leaf (the incident's first refusal) ends the
    child at $0 AND keeps the producer's facts: the $0 terminal names the lock holder,
    the availability row and the acceptance evidence carry the detail."""
    from ouroboros import subagent_bootstrap, subagent_runtime
    from ouroboros.agent_dispatch import executor_blocked_outcome
    from ouroboros.delegate_shared import _fail, lock_busy_facts
    from ouroboros.subagents import SubagentExecutorResolution

    busy = wt.WorktreeOpsLockBusy(tmp_path / "lock", 120.0, {"pid": "4242", "task": "t-other", "op": "provision"})
    refusal = _fail("delegate_start", "execution_snapshot_failed",
                    f"A private execution snapshot could not be provisioned ({busy}).",
                    definitely_unrun=True, **lock_busy_facts(busy))
    monkeypatch.setattr(subagent_runtime, "delegate_start_entry", lambda ctx, prompt, **kw: refusal)
    monkeypatch.setattr(subagent_runtime, "current_subagent_alternatives", lambda selected: [])

    class _Ctx:
        task_id = "t-child"

    ctx = _Ctx()
    task = {"configured_subagent": {"selected_subagent_id": "codex=gpt-6-astra/xhigh"}}
    wake = subagent_bootstrap._pre_start_leaf(ctx, task, {})

    assert wake == ""  # a definite refusal: the child ends unrun at $0, no model round
    stash = ctx._configured_startup_refusal
    assert stash["reason"] == "execution_snapshot_failed" and stash["cause"] == "lock_busy"
    assert stash["holder"]["task"] == "t-other" and "t-other" in stash["detail"]
    availability = task["subagent_availability"]
    assert availability["status"] == "unavailable" and availability["holder"]["pid"] == "4242"
    text, usage = executor_blocked_outcome(
        SubagentExecutorResolution(requested="harness", executor="blocked", reason=stash["reason"]),
        availability=availability)
    assert "t-other" in text and usage["reason_code"] == "subagent_executor_unavailable"


def test_acting_worktree_add_and_remove_keep_tree_work_outside_the_lock(tmp_path, monkeypatch):
    """The acting self_worktree lane follows the same split: admin dir + branch under
    the lock, the checkout populated and deleted outside it."""
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    seen: dict = {}
    real_git, real_rmtree = wt._git, wt._force_rmtree

    def spy_git(repo_dir, *args, **kw):
        if "worktree" in args and "add" in args:
            seen["worktree_add"] = (_lock_held(snaps), "--no-checkout" in args)
        if "reset" in args:
            seen["reset"] = _lock_held(snaps)
        return real_git(repo_dir, *args, **kw)

    def spy_rmtree(path):
        seen["rmtree"] = _lock_held(snaps)
        return real_rmtree(path)

    monkeypatch.setattr(wt, "_git", spy_git)
    monkeypatch.setattr(wt, "_force_rmtree", spy_rmtree)
    handle = wt.provision_worktree(repo_dir=target, task_id="acting1", worktree_root=snaps, data_dir=data)
    assert seen["worktree_add"] == (True, True) and seen["reset"] is False
    assert (pathlib.Path(handle.path) / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\n"  # Current source content, including the parent edit
    assert _git(target, "rev-parse", "--verify", handle.branch).returncode == 0
    assert any(row.get("task_id") == "acting1" for row in wt.list_worktrees(data_dir=data))

    assert wt.remove_worktree(task_id="acting1", worktree_root=snaps, data_dir=data)
    assert seen["rmtree"] is False
    assert not pathlib.Path(handle.path).exists()
    assert _git(target, "rev-parse", "--verify", handle.branch, check=False).returncode != 0
    assert not any(row.get("task_id") == "acting1" for row in wt.list_worktrees(data_dir=data))
    assert not _lock_held(snaps)


def test_a_row_naming_the_snapshot_root_itself_deletes_nothing(tmp_path):
    """The registry is durable state; a malformed row whose path IS the root must not
    wipe every live sibling snapshot (the root holds them all)."""
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    keep = _provision(target, snaps, data, snapshot_id="keep")
    rows = wt._load_registry(data)
    rows.append({**rows[0], "snapshot_id": "bad", "path": str(snaps.resolve())})
    wt._save_registry(rows, data)
    assert wt.remove_execution_snapshot("bad", worktree_root=snaps, data_dir=data)
    assert pathlib.Path(keep.path).is_dir() and wt.find_execution_snapshot("keep", data_dir=data) is not None
    assert wt.find_execution_snapshot("bad", data_dir=data) is None
    assert not wt._deletable(snaps, snaps) and not wt._deletable(pathlib.Path("/"), snaps)
    assert wt._deletable(pathlib.Path(keep.path), snaps)


def test_removal_deletes_files_outside_the_lock_and_forgets_metadata_inside(tmp_path, monkeypatch):
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    handle = _provision(target, snaps, data)
    seen: dict = {}
    real_rmtree, real_git = wt._force_rmtree, wt._git

    def spy_rmtree(path):
        seen["rmtree"] = _lock_held(snaps)
        return real_rmtree(path)

    def spy_git(repo_dir, *args, **kw):
        if "update-ref" in args and "-d" in args:
            seen["unpin"] = _lock_held(snaps)
        return real_git(repo_dir, *args, **kw)

    monkeypatch.setattr(wt, "_force_rmtree", spy_rmtree)
    monkeypatch.setattr(wt, "_git", spy_git)
    assert wt.remove_execution_snapshot("snap1", worktree_root=snaps, data_dir=data)
    assert seen == {"rmtree": False, "unpin": True}
    assert not pathlib.Path(handle.path).exists()
    assert wt.find_execution_snapshot("snap1", data_dir=data) is None
    assert _git(target, "rev-parse", handle.baseline_ref, check=False).returncode != 0
    assert not _lock_held(snaps)


@pytest.mark.parametrize("failing", ["update-ref", "worktree"])
def test_a_failure_inside_the_first_lock_section_after_the_row_leaves_nothing(tmp_path, monkeypatch, failing):
    """The provisional row is written first; a failed pin or admin-dir creation right
    after it (still inside the lock) must discard the row too, not only later phases."""
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    real_git = wt._git

    def failing_git(repo_dir, *args, **kw):
        if failing in args and (failing != "worktree" or "add" in args):
            raise subprocess.CalledProcessError(128, ["git", *args])
        return real_git(repo_dir, *args, **kw)

    monkeypatch.setattr(wt, "_git", failing_git)
    with pytest.raises(subprocess.CalledProcessError):
        _provision(target, snaps, data, snapshot_id="snapLockB")
    assert wt.find_execution_snapshot("snapLockB", data_dir=data) is None
    assert _git(target, "for-each-ref", "refs/ouroboros/").stdout == ""
    assert not list(snaps.glob("dlg_*")) and not list((target / ".git" / "worktrees").glob("dlg_*"))
    assert not _lock_held(snaps)


def test_a_failure_after_the_provisional_row_leaves_nothing_and_a_crash_is_gc_reclaimable(tmp_path, monkeypatch):
    target = _seed_target(tmp_path)
    snaps, data = tmp_path / "snaps", tmp_path / "data"

    def boom(*_a, **_k):
        raise OSError("copy failed")

    monkeypatch.setattr(artifacts, "copy_artifact_file", boom)
    with pytest.raises(OSError, match="copy failed"):
        _provision(target, snaps, data, snapshot_id="snapFail")
    assert wt.find_execution_snapshot("snapFail", data_dir=data) is None
    assert _git(target, "for-each-ref", "refs/ouroboros/").stdout == ""
    assert not list(snaps.glob("dlg_*"))

    # A worker killed between the provisional row and the final row: the row is
    # registered, custody never opened -> the startup GC removes checkout, ref, row.
    monkeypatch.setattr(wt, "_discard_snapshot_checkout", lambda *a, **k: None)
    with pytest.raises(OSError, match="copy failed"):
        _provision(target, snaps, data, snapshot_id="snapCrash")
    assert wt.find_execution_snapshot("snapCrash", data_dir=data) is not None
    assert "refs/ouroboros/delegated/snapCrash" in _git(target, "for-each-ref", "refs/ouroboros/").stdout
    report = wt.prune_execution_snapshots(set(), worktree_root=snaps, data_dir=data)
    assert report["removed"] == ["snapCrash"]
    assert wt.find_execution_snapshot("snapCrash", data_dir=data) is None
    assert _git(target, "for-each-ref", "refs/ouroboros/").stdout == ""
    assert not list(snaps.glob("dlg_*"))


@pytest.mark.parametrize("autocrlf", [False, True])
def test_populate_matches_worktree_add_and_runs_no_target_hook(tmp_path, monkeypatch, autocrlf):
    """``worktree add --no-checkout`` + ``reset --hard --no-recurse-submodules`` is what
    git's own ``worktree add`` runs — a target with ``submodule.recurse=true`` must
    still snapshot — minus the target's post-checkout hook, which no longer executes
    project-authored code at provision. Under ``core.autocrlf=true`` (Git for
    Windows' default) the checkout writes CRLF and the raw-bytes copy restores the
    source's LF, so the copied entries' stat must be re-recorded or every such file
    reads as modified in the child's ``git status`` (CI on windows-latest, #1247)."""
    # Both legs pin the setting explicitly: on windows-latest the system config
    # already says true, so an unset "False" leg would not be the working side.
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\tautocrlf = {'true' if autocrlf else 'false'}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q")
    (sub / "s.txt").write_text("s\n", encoding="utf-8")
    _git(sub, "add", "s.txt")
    _git(sub, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "sub")
    target = _seed_target(tmp_path)
    _git(target, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendored")
    _git(target, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "vendored")
    _git(target, "config", "submodule.recurse", "true")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "post-checkout").write_text("#!/bin/sh\ntouch .hook_ran\n", encoding="utf-8")
    (hooks / "post-checkout").chmod(0o755)
    _git(target, "config", "core.hooksPath", str(hooks))
    snaps, data = tmp_path / "snaps", tmp_path / "data"
    # Positive control: the hook DOES fire on a plain `worktree add`, so its absence
    # below is the populate path's doing, not a dead fixture.
    control = tmp_path / "control-wt"
    _git(target, "-c", "submodule.recurse=false", "worktree", "add", "--detach", str(control), "HEAD")
    assert (control / ".hook_ran").exists()
    _git(target, "worktree", "remove", "--force", str(control))

    handle = _provision(target, snaps, data)

    exec_root = pathlib.Path(handle.path)
    assert (exec_root / "vendored").is_dir() and (exec_root / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert not (exec_root / ".hook_ran").exists() and not (target / ".hook_ran").exists()
    assert _git(exec_root, "status", "--porcelain").stdout == ""
    assert (exec_root / ".gitmodules").read_bytes() == (target / ".gitmodules").read_bytes()
    assert "160000" in _git(exec_root, "ls-files", "-s", "vendored").stdout

    # The guard: without --no-recurse-submodules the populate fails on this target.
    real_git = wt._git

    def recursing_git(repo_dir, *args, **kw):
        return real_git(repo_dir, *tuple(a for a in args if a != "--no-recurse-submodules"), **kw)

    monkeypatch.setattr(wt, "_git", recursing_git)
    with pytest.raises(subprocess.CalledProcessError):
        _provision(target, snaps, data, snapshot_id="snapRecurse")


def test_snapshot_facts_ride_the_start_receipt_as_disclosure_only(tmp_path):
    from ouroboros.tools.delegate import _snapshot_facts

    target = _seed_target(tmp_path)
    (target / "blob.bin").write_bytes(b"\x7fELF\x00" * 10)
    handle = _provision(target, tmp_path / "snaps", tmp_path / "data")
    facts = _snapshot_facts(handle)
    assert set(facts) == {"entries", "untracked_files", "file_input_bytes", "provisioning_sec"}
    assert facts["entries"] == handle.entry_count and facts["untracked_files"] == 2  # untracked.txt + blob.bin
    assert facts["file_input_bytes"] == 50 and facts["provisioning_sec"] == handle.provisioning_sec
    assert _snapshot_facts(None) == {}
    # Facts, not a threshold: no runtime module consumes them to refuse or truncate.
    consumers = sorted(
        path.relative_to(REPO).as_posix() for path in (REPO / "ouroboros").rglob("*.py")
        if "provisioning_sec" in path.read_text(encoding="utf-8"))
    assert consumers == ["ouroboros/subagent_worktrees.py", "ouroboros/tools/delegate.py",
                         "ouroboros/tools/delegate_integration.py"]
