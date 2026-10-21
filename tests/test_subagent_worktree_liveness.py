"""C10: a plain working copy past retention goes only with proof its task is over (the supervisor's
liveness probe, a terminal row, no lineage process); a checkout that survives its removal keeps its
row and pin; the registry is written only when something changed; ``only`` leaves other rows alone."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest

from ouroboros import body_candidate
from ouroboros import subagent_worktrees as sw
from ouroboros.task_results import write_task_result


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)


def _init_repo(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo, data, root = tmp_path / "repo", tmp_path / "data", tmp_path / "wtroot"
    _init_repo(repo)
    data.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(data))
    return repo, data, root


def _copy(world, task_id):
    repo, data, root = world
    return pathlib.Path(sw.provision_worktree(repo_dir=repo, task_id=task_id, worktree_root=root, data_dir=data).path)


def _age(data, *, created_at=1.0):
    path = data / "state" / "subagent_worktrees.json"
    rows = json.loads(path.read_text())
    for row in rows["worktrees"]:
        row["created_at"] = created_at
    path.write_text(json.dumps(rows))


def _prune(world, **kwargs):
    repo, data, root = world
    return sw.prune_orphans(worktree_root=root, data_dir=data, **kwargs)


def _registry_mtime(data):
    return (data / "state" / "subagent_worktrees.json").stat().st_mtime_ns


@pytest.mark.parametrize("retention_days", [7, 0])
def test_an_old_copy_stays_while_its_task_runs_is_pending_unknown_or_unreadable(world, monkeypatch, retention_days):
    """L3, the keeping half: age is a courtesy, never a proof."""
    repo, data, root = world
    cases = {
        "running": ("running", True), "pending": ("", True), "unknown": ("completed", None),
        "unreadable": (None, False), "process": ("completed", False), "done": ("completed", False),
    }
    copies = {name: _copy(world, name) for name in cases}
    for name, (status, _probe) in cases.items():
        if status:
            write_task_result(data, name, status)
    _age(data)
    answers = {name: probe for name, (_status, probe) in cases.items()}
    monkeypatch.setattr(body_candidate, "live_lineage_processes",
                        lambda _root, task_id: [4242] if task_id == "process" else [])

    assert _prune(world, retention_days=retention_days) == {"removed": 0, "kept": 6}, "without a probe nothing goes"
    assert all(path.is_dir() for path in copies.values())
    report = _prune(world, retention_days=retention_days, live=lambda task_id: answers[task_id])
    assert report == {"removed": 1, "kept": 5}, "only the proven-over copy goes"
    assert not copies["done"].exists() and all(path.is_dir() for name, path in copies.items() if name != "done")


@pytest.mark.parametrize("retention_days", [7, 0])
def test_an_old_copy_goes_once_its_task_is_terminal_and_proven_not_live(world, retention_days):
    """L3, the removing half, and the registry written once for a changed row set."""
    repo, data, root = world
    done, fresh = _copy(world, "done"), _copy(world, "fresh")
    write_task_result(data, "done", "completed")
    write_task_result(data, "fresh", "completed")
    _age(data)
    if retention_days:
        rows = json.loads((data / "state" / "subagent_worktrees.json").read_text())
        for row in rows["worktrees"]:
            if row["task_id"] == "fresh":
                row["created_at"] = 9e12  # inside the wait
        (data / "state" / "subagent_worktrees.json").write_text(json.dumps(rows))

    report = _prune(world, retention_days=retention_days, live=lambda _task: False)
    expected = 1 if retention_days else 2
    assert report["removed"] == expected
    assert not done.exists() and fresh.exists() != bool(retention_days) or fresh.exists() == bool(retention_days)
    assert _git(repo, "branch", "--list", "subagent/done*").stdout.strip() == ""
    assert "done" not in {row["task_id"] for row in sw.list_worktrees(data_dir=data)}


def test_a_missing_checkout_keeps_its_row_and_branch_until_ownership_is_proven_over(world, monkeypatch):
    repo, data, root = world
    gone = _copy(world, "gone")
    shutil.rmtree(gone)
    before = _registry_mtime(data)
    for status, answer in (("running", False), ("completed", True), ("completed", None)):
        write_task_result(data, "gone", status)
        assert _prune(world, retention_days=9999, live=lambda _task: answer) == {"removed": 0, "kept": 1}
        assert _registry_mtime(data) == before
        assert _git(repo, "branch", "--list", "subagent/gone*").stdout.strip()
    assert _prune(world, retention_days=9999) == {"removed": 0, "kept": 1}
    with monkeypatch.context() as process:
        process.setattr(body_candidate, "live_lineage_processes", lambda *_args: [4242])
        assert _prune(world, retention_days=9999, live=lambda _task: False) == {"removed": 0, "kept": 1}
    assert _prune(world, retention_days=9999, live=lambda _task: False) == {"removed": 1, "kept": 0}
    assert _git(repo, "branch", "--list", "subagent/gone*").stdout == ""


def test_only_restricts_the_pass_and_an_unchanged_row_set_leaves_the_registry_untouched(world):
    repo, data, root = world
    first, second = _copy(world, "first"), _copy(world, "second")
    for name in ("first", "second"):
        write_task_result(data, name, "completed")
    _age(data)
    before = _registry_mtime(data)
    assert _prune(world, retention_days=0, live=lambda _task: False, only=["nobody"]) == {"removed": 0, "kept": 2}
    assert _registry_mtime(data) == before, "nothing changed: the registry is not rewritten"
    assert _prune(world, retention_days=0, live=lambda _task: False, only=["second"]) == {"removed": 1, "kept": 1}
    assert first.is_dir() and not second.exists()
    assert _registry_mtime(data) != before


def _registry_cost(monkeypatch):
    counts = {"locks": 0, "reads": 0}
    for name, counter in (("_ops_lock", "locks"), ("_load_registry", "reads")):
        real = getattr(sw, name)

        def counted(*args, _real=real, _counter=counter, **kwargs):
            counts[_counter] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(sw, name, counted)
    return counts


@pytest.mark.parametrize("count", [100, 800])
@pytest.mark.parametrize("only", [None, ["absent"], ["task-0"]])
def test_irrelevant_rows_do_not_add_registry_locks_or_reads(world, monkeypatch, count, only):
    repo, data, root = world
    for i in range(count):
        (root / str(i)).mkdir(parents=True)
    rows = [{"task_id": f"task-{i}", "kind": "delegated_exec" if i % 2 else "plain",
             "path": str(root / str(i)), "created_at": 9e12} for i in range(count)]
    sw._save_registry(rows, data)
    before = _registry_mtime(data)
    costs = _registry_cost(monkeypatch)
    assert _prune(world, retention_days=7, only=only, live=lambda _: False) == {"removed": 0, "kept": count}
    assert costs["locks"] <= 1 and costs["reads"] <= 2
    assert _registry_mtime(data) == before


def test_a_selected_old_copy_still_goes_among_many_unrelated_rows(world, monkeypatch):
    repo, data, root = world
    path = _copy(world, "selected")
    write_task_result(data, "selected", "completed")
    _age(data)
    unrelated = [{"task_id": f"task-{i}", "kind": "delegated_exec", "path": str(root / str(i))}
                 for i in range(400)]
    sw._save_registry(sw.list_worktrees(data) + unrelated, data)
    costs = _registry_cost(monkeypatch)
    assert _prune(world, retention_days=0, only=["selected"], live=lambda _: False) == {"removed": 1, "kept": 400}
    assert costs["locks"] <= 2 and costs["reads"] <= 4
    assert not path.exists() and sw.list_worktrees(data) == unrelated


def test_a_checkout_surviving_its_removal_keeps_its_row_and_is_retried(world, monkeypatch):
    """L5: a forced removal that leaves the directory marks the row instead of forgetting it."""
    repo, data, root = world
    stubborn = _copy(world, "stubborn")
    write_task_result(data, "stubborn", "completed")
    _age(data)
    with monkeypatch.context() as failed:
        failed.setattr(sw, "_force_rmtree", lambda _path: None)  # the removal fails to delete anything
        failed.setattr(sw, "_git_quiet", lambda *_args: None)
        report = _prune(world, retention_days=0, live=lambda _task: False)
    assert report == {"removed": 0, "kept": 1}
    [row] = sw.list_worktrees(data_dir=data)
    assert row["task_id"] == "stubborn" and row["removal_failed"]
    assert stubborn.is_dir()
    assert _git(repo, "branch", "--list", "subagent/stubborn*").stdout.strip(), "the branch is kept with the row"

    # Restore only deletion faults, keeping the fixture's canonical install selected.
    (stubborn / "released").touch()
    assert _prune(world, retention_days=0, live=lambda _task: False) == {"removed": 1, "kept": 0}
    assert not stubborn.exists() and sw.list_worktrees(data_dir=data) == []


def test_remove_worktree_keeps_the_row_when_the_directory_survives(world, monkeypatch):
    repo, data, root = world
    path = _copy(world, "held")
    monkeypatch.setattr(sw, "_force_rmtree", lambda _path: None)
    monkeypatch.setattr(sw, "_git_quiet", lambda *_args: None)
    assert sw.remove_worktree(task_id="held", worktree_root=root, data_dir=data) is False
    [row] = sw.list_worktrees(data_dir=data)
    assert row["removal_failed"] and path.is_dir()
    monkeypatch.undo()
    (path / "released").touch()
    assert sw.remove_worktree(task_id="held", worktree_root=root, data_dir=data) is True
    assert sw.list_worktrees(data_dir=data) == [] and not path.exists()


def test_a_snapshot_surviving_its_removal_keeps_its_row_and_pin_and_only_selects_snapshots(world, monkeypatch):
    repo, data, root = world
    write_task_result(data, "t1", "completed")
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: False)
    one = sw.provision_execution_snapshot(target_root=repo, task_id="t1", snapshot_id="snapOne", worktree_root=root, data_dir=data)
    two = sw.provision_execution_snapshot(target_root=repo, task_id="t2", snapshot_id="snapTwo", worktree_root=root, data_dir=data)
    with monkeypatch.context() as failed:
        failed.setattr(sw, "_force_rmtree", lambda _path: None)
        report = sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data, only={"snapOne"})
    assert report == {"removed": [], "kept": ["snapOne"]}
    assert sw.find_execution_snapshot("snapOne", data_dir=data)["removal_failed"]
    assert pathlib.Path(one.path).is_dir() and pathlib.Path(two.path).is_dir()
    assert _git(repo, "rev-parse", "--verify", one.baseline_ref).stdout.strip(), "the pin stays with the row"
    (pathlib.Path(one.path) / "released").touch()
    assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data, only={"snapOne"}) == {"removed": ["snapOne"], "kept": []}
    assert not pathlib.Path(one.path).exists() and pathlib.Path(two.path).is_dir()
    assert sw.find_execution_snapshot("snapTwo", data_dir=data) is not None


@pytest.mark.parametrize("kind", ["plain", "body", "snapshot"])
def test_pruning_deletes_outside_the_ops_lock_and_preserves_concurrent_registration(world, monkeypatch, kind):
    repo, data, root = world
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: False)
    if kind == "snapshot":
        handle = sw.provision_execution_snapshot(
            target_root=repo, task_id="done", snapshot_id="closed", worktree_root=root, data_dir=data)
        path = pathlib.Path(handle.path)
    else:
        path = _copy(world, "done")
        if kind == "body":
            rows = sw._load_registry(data)
            rows[0]["kind"] = "body_candidate"
            sw._save_registry(rows, data)
    write_task_result(data, "done", "completed")
    _age(data)
    real_remove, real_save, real_git = sw._force_rmtree, sw._save_registry, sw._git_quiet
    saves, deletions = [], []

    def remove(checkout):
        assert not sw._inproc_lock.locked(), "tree deletion must not hold the shared ops lock"
        deletions.append(checkout)
        _copy(world, "arrived")  # a real provision interleaves with this deletion
        real_remove(checkout)

    def save(rows, data_dir=None):
        saves.append(sw._inproc_lock.locked())
        real_save(rows, data_dir)

    def git(repo_dir, *args):
        if args[:2] == ("worktree", "remove") and path.exists():
            assert not sw._inproc_lock.locked(), "Git tree deletion must also be outside the shared lock"
        real_git(repo_dir, *args)

    monkeypatch.setattr(sw, "_force_rmtree", remove)
    monkeypatch.setattr(sw, "_save_registry", save)
    monkeypatch.setattr(sw, "_git_quiet", git)
    if kind == "snapshot":
        assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data)["removed"] == ["closed"]
    else:
        assert _prune(world, retention_days=0, live=lambda _: False)["removed"] == 1
    assert deletions == [path] and not path.exists()
    assert saves and all(saves), "registry mutations still require the shared lock"
    [row] = sw.list_worktrees(data_dir=data)
    assert row["task_id"] == "arrived" and pathlib.Path(row["path"]).is_dir()


def test_snapshot_gc_requires_terminal_task_negative_liveness_and_readable_custody(world, monkeypatch):
    from ouroboros import delegate_custody as custody

    repo, data, root = world
    handle = sw.provision_execution_snapshot(target_root=repo, task_id="owner", snapshot_id="proof",
                                             worktree_root=root, data_dir=data)
    probe, processes = [False], []
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: probe[0])
    monkeypatch.setattr(body_candidate, "live_lineage_processes", lambda *_args: processes)

    def kept(open_ids=()):
        before = _registry_mtime(data)
        assert sw.prune_execution_snapshots(set(open_ids), worktree_root=root, data_dir=data) == {
            "removed": [], "kept": ["proof"]}
        assert _registry_mtime(data) == before
        assert _git(repo, "rev-parse", handle.baseline_ref).stdout.strip() == handle.baseline_sha

    kept()  # no task record yet: the provisioning window
    write_task_result(data, "owner", "running")
    kept()
    write_task_result(data, "owner", "completed")
    for probe[0] in (True, None):
        kept()
    probe[0] = False
    processes.append(4242)
    kept()
    processes.clear()
    with monkeypatch.context() as unreadable:
        unreadable.setattr(custody, "custody_log_unreadable", lambda _root: True)
        kept()
    with monkeypatch.context() as broken:
        def fail(_task):
            raise OSError("unreadable liveness")
        broken.setattr("supervisor.queue.task_settlement_liveness", fail)
        kept()
    shutil.rmtree(handle.path)
    kept({"proof"})  # missing checkout does not release open custody or its pin
    assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data) == {
        "removed": ["proof"], "kept": []}
    assert _git(repo, "for-each-ref", handle.baseline_ref).stdout == ""


def test_snapshot_gc_rechecks_custody_between_removals(world, monkeypatch):
    from ouroboros import delegate_custody as custody

    repo, data, root = world
    handles = [sw.provision_execution_snapshot(target_root=repo, task_id="done", snapshot_id=name,
                                               worktree_root=root, data_dir=data) for name in ("first", "second")]
    write_task_result(data, "done", "completed")
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: False)
    real_remove = sw.remove_execution_snapshot

    def remove(snapshot_id, **kwargs):
        removed = real_remove(snapshot_id, **kwargs)
        custody.record_started(data, custody.RunCustody(
            run_id="late-start", task_id="done", snapshot_id="second", execution_root=handles[1].path))
        return removed

    monkeypatch.setattr(sw, "remove_execution_snapshot", remove)
    assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data) == {
        "removed": ["first"], "kept": ["second"]}
    assert not pathlib.Path(handles[0].path).exists() and pathlib.Path(handles[1].path).is_dir()
    assert _git(repo, "rev-parse", handles[1].baseline_ref).stdout.strip() == handles[1].baseline_sha


@pytest.mark.parametrize("kind", ["plain", "body", "snapshot"])
def test_failed_removal_waits_for_changed_directory_identity_without_rewriting_first_stamp(world, monkeypatch, kind):
    repo, data, root = world
    if kind == "snapshot":
        handle = sw.provision_execution_snapshot(target_root=repo, task_id="done", snapshot_id="stubborn",
                                                 worktree_root=root, data_dir=data)
        path = pathlib.Path(handle.path)
    else:
        path = _copy(world, "done")
        if kind == "body":
            rows = sw._load_registry(data)
            rows[0]["kind"] = "body_candidate"
            sw._save_registry(rows, data)
    write_task_result(data, "done", "completed")
    _age(data)
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: False)
    attempts, verdicts, commands = [], [], []
    real_remove, real_verdict, real_git = sw._force_rmtree, body_candidate.retention_verdict, sw._git_quiet
    monkeypatch.setattr(sw, "_force_rmtree", lambda checkout: attempts.append(checkout))

    def verdict(*args, **kwargs):
        verdicts.append(args[0]["path"])
        return real_verdict(*args, **kwargs)

    def git(*args):
        commands.append(args)
        return real_git(*args)

    monkeypatch.setattr(body_candidate, "retention_verdict", verdict)
    monkeypatch.setattr(sw, "_git_quiet", git)

    def prune():
        if kind == "snapshot":
            return sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data)["removed"]
        return _prune(world, retention_days=0, live=lambda _task: False)["removed"]

    assert not prune() and path.is_dir()
    [first] = sw.list_worktrees(data)
    stamp = first["removal_failed"]
    registry = data / "state" / "subagent_worktrees.json"
    before, mtime, judged = registry.read_bytes(), _registry_mtime(data), len(verdicts)
    for _ in range(2):
        assert not prune()
        assert registry.read_bytes() == before and _registry_mtime(data) == mtime
        assert attempts == [path] and len(verdicts) == judged and commands == []

    (path / "changed").touch()  # a changed root identity authorizes another attempt, not elapsed time
    assert not prune() and attempts == [path, path]
    assert sw.list_worktrees(data)[0]["removal_failed"] == stamp
    monkeypatch.setattr(sw, "_force_rmtree", real_remove)
    (path / "changed_again").touch()
    assert prune() and not path.exists() and sw.list_worktrees(data) == []


_RETRY_IN_NEW_PROCESS = """
import json, pathlib, sys
from ouroboros import subagent_worktrees as wt
from supervisor import queue
data, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
kind, failing = sys.argv[3], sys.argv[4] == "fail"
queue.task_settlement_liveness = lambda _: False
attempts = []
remove = wt._force_rmtree
def attempt(path):
    attempts.append(str(path))
    if not failing:
        remove(path)
wt._force_rmtree = attempt
removed = []
registry = data / "state" / "subagent_worktrees.json"
for _ in range(3):
    if kind == "snapshot":
        result = len(wt.prune_execution_snapshots(set(), worktree_root=root, data_dir=data)["removed"])
    else:
        result = wt.prune_orphans(worktree_root=root, data_dir=data, retention_days=0, live=lambda _: False)["removed"]
    removed.append(result)
    state = (registry.read_bytes(), registry.stat().st_mtime_ns)
    if len(removed) == 1:
        first = state
    else:
        assert state == first, "unchanged failure rewrote its registry on a later pass"
rows = wt.list_worktrees(data)
print(json.dumps({"attempts": len(attempts), "removed": removed,
                  "stamp": rows[0].get("removal_failed") if rows else None}))
"""


@pytest.mark.parametrize("kind", ["plain", "body", "snapshot"])
def test_a_deep_survivor_retries_once_in_each_new_process(world, monkeypatch, kind):
    repo, data, root = world
    if kind == "snapshot":
        path = pathlib.Path(sw.provision_execution_snapshot(
            target_root=repo, task_id="done", snapshot_id="deep", worktree_root=root, data_dir=data).path)
    else:
        path = _copy(world, "done")
        if kind == "body":
            rows = sw._load_registry(data)
            rows[0]["kind"] = "body_candidate"
            sw._save_registry(rows, data)
    write_task_result(data, "done", "completed")
    _age(data)
    held = path / "nested" / "deep" / "held.txt"
    held.parent.mkdir(parents=True)
    held.write_text("held by another process")
    monkeypatch.setattr(sw, "_force_rmtree", lambda _: None)
    monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _: False)
    if kind == "snapshot":
        assert sw.prune_execution_snapshots(set(), worktree_root=root, data_dir=data)["removed"] == []
    else:
        assert _prune(world, retention_days=0, live=lambda _: False)["removed"] == 0
    [failed] = sw.list_worktrees(data)
    held.unlink()  # release below the root does not change its saved stat identity
    seen = path.stat()
    assert failed["removal_failed_identity"] == [seen.st_dev, seen.st_ino, seen.st_mtime_ns, seen.st_ctime_ns]

    def restarted(action):
        child = subprocess.run([sys.executable, "-c", _RETRY_IN_NEW_PROCESS, str(data), str(root), kind, action],
                               capture_output=True, text=True, timeout=120)
        assert child.returncode == 0, child.stderr
        return json.loads(child.stdout)

    again = restarted("fail")
    assert again == {"attempts": 1, "removed": [0, 0, 0], "stamp": failed["removal_failed"]}
    assert restarted("remove") == {"attempts": 1, "removed": [1, 0, 0], "stamp": None}
    assert not path.exists() and sw.list_worktrees(data) == []
