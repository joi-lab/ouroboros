"""A private execution copy is filled in place, before anything can run in it.

Git checks the baseline out; each regular file then receives the source's exact
working bytes and ``copystat`` metadata in the file checkout created, without
the per-file temporary file and rename of ``artifacts.copy_artifact_file``; only
an entry checkout left absent (an inherited sparse cone) goes through that
helper, and only while the copy still has its ``.git``. The whole-tree diff
against the baseline still refuses a source that moved, and a copy whose
provisioning has not returned is no consumer's ready copy.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import threading
import time

import pytest

from ouroboros import artifacts
from ouroboros import subagent_worktrees as trees

_WAIT_SEC = 30.0
# Checkout rewrites each of these differently from its source bytes: longer (CRLF),
# longer only after the first 1 MiB read, same size, shorter; the rest are written as-is.
_CONVERTED = {"lines.txt": b"alpha\nbeta\n", "late.txt": b"a" * (1024 * 1024 + 7) + b"\nend\n",
              "word.shout": b"quiet words\n", "pizza.squeeze": b"pizza fizz\n"}
_PLAIN = {"tool.sh": b"#!/bin/sh\necho hi\n", "locked.cfg": b"keep\n", "empty.cfg": b"",
          "dirty.cfg": b"v1\n", "blob.bin": bytes(range(256)) * 3}


def git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True)


def source_tree(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    for args in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "f@example.invalid"),
                 ("config", "core.autocrlf", "false"), ("config", "filter.shout.clean", "cat"),
                 ("config", "filter.shout.smudge", "tr a-z A-Z"), ("config", "filter.squeeze.clean", "cat"),
                 ("config", "filter.squeeze.smudge", "tr -d z")):
        git(root, *args)
    (root / ".gitattributes").write_bytes(
        b"*.txt text eol=crlf\n*.shout filter=shout\n*.squeeze filter=squeeze\n*.bin -text\n")
    for name, content in {**_CONVERTED, **_PLAIN, "gone.cfg": b"deleted before the snapshot\n"}.items():
        (root / name).write_bytes(content)
    if os.name != "nt":
        os.symlink("tool.sh", root / "link")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")
    (root / "dirty.cfg").write_bytes(b"v2 dirty\n")  # an unstaged edit is the copy's input
    (root / "gone.cfg").unlink()
    os.chmod(root / "tool.sh", 0o755)
    os.chmod(root / "locked.cfg", 0o444)
    return root


def provision(tmp_path, target, *, snapshot_id="fill", independent=False):
    return trees.provision_execution_snapshot(
        target_root=target, task_id="fill-task", snapshot_id=snapshot_id,
        worktree_root=tmp_path / "snapshots", data_dir=tmp_path / "data", independent=independent)


def open_files_deletable(tmp_path):
    """POSIX unlinks a file another handle holds open; Windows refuses (a sharing violation)."""
    probe = tmp_path / "open-probe"
    with open(probe, "wb"):
        try:
            os.unlink(probe)
        except PermissionError:
            return False
    return True


def nothing_left(tmp_path, target, snapshot_id):
    assert trees.find_execution_snapshot(snapshot_id, tmp_path / "data") is None
    assert not git(target, "for-each-ref", "refs/ouroboros/delegated/").stdout
    assert not list((tmp_path / "snapshots").glob("dlg_*"))


@pytest.mark.parametrize("independent", [False, True], ids=["worktree", "clone"])
def test_checkout_is_filled_in_place_with_exact_bytes_and_metadata(tmp_path, monkeypatch, independent):
    target = source_tree(tmp_path)
    copies = (tmp_path / "snapshots").resolve()
    published, checked_out = [], {}

    def counted_copy(source, destination, **kwargs):
        published.append(pathlib.Path(destination).name)
        return real_copy(source, destination, **kwargs)

    def counted_replace(real):
        def replace(source, destination, *args, **kwargs):
            if str(pathlib.Path(destination).resolve()).startswith(str(copies)):
                published.append(f"replace:{pathlib.Path(destination).name}")
            return real(source, destination, *args, **kwargs)
        return replace

    def checkout_spy(repo_dir, *args, **kwargs):
        result = real_git(repo_dir, *args, **kwargs)
        if args[:1] == ("reset",):
            checked_out.update({name: os.lstat(pathlib.Path(repo_dir) / name).st_ino for name in names})
        return result

    names = [*_CONVERTED, *_PLAIN]
    real_copy, real_git = artifacts.copy_artifact_file, trees._git
    monkeypatch.setattr(artifacts, "copy_artifact_file", counted_copy)
    monkeypatch.setattr(pathlib.Path, "replace", counted_replace(pathlib.Path.replace))
    monkeypatch.setattr(os, "replace", counted_replace(os.replace))
    monkeypatch.setattr(trees, "_git", checkout_spy)
    handle = provision(tmp_path, target, independent=independent)
    copy = pathlib.Path(handle.path)
    assert published == []  # no per-file atomic publication into the copy
    assert sorted(checked_out) == sorted(names)
    for name in names:
        source, copied = os.stat(target / name), os.stat(copy / name)
        assert (copy / name).read_bytes() == (target / name).read_bytes(), name
        assert copied.st_ino == checked_out[name], f"{name} was replaced, not filled"
        assert stat.S_IMODE(copied.st_mode) == stat.S_IMODE(source.st_mode), name
        assert copied.st_mtime_ns == source.st_mtime_ns, name
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(copy / "tool.sh").st_mode) & 0o111
        assert os.path.islink(copy / "link") and os.readlink(copy / "link") == "tool.sh"
    assert not (copy / "gone.cfg").exists()
    assert git(copy, "status", "--porcelain=v1", "-z").stdout == b""
    git(copy, "diff", "--quiet", "--no-ext-diff", handle.baseline_sha, "--")


@pytest.mark.parametrize("edit", [b"v3 dirty\n", b"v2 dirty and longer\n", b"v2\n"], ids=["same_size", "longer", "shorter"])
def test_a_source_edit_after_the_baseline_is_refused_by_the_whole_tree_diff(tmp_path, monkeypatch, edit):
    target = source_tree(tmp_path)
    real_git = trees._git

    def edit_at_checkout(repo_dir, *args, **kwargs):
        if args[:1] == ("reset",):  # the baseline is pinned and registered; population has not begun
            (target / "dirty.cfg").write_bytes(edit)
        return real_git(repo_dir, *args, **kwargs)

    monkeypatch.setattr(trees, "_git", edit_at_checkout)
    with pytest.raises(subprocess.CalledProcessError) as refused:
        provision(tmp_path, target)
    assert refused.value.returncode == 1 and refused.value.cmd[:3] == ["git", "diff", "--quiet"]
    assert (target / "dirty.cfg").read_bytes() == edit
    nothing_left(tmp_path, target, "fill")


def test_a_same_size_edit_git_would_trust_by_its_stat_is_still_refused(tmp_path, monkeypatch):
    """Git compares times to the second, and Git for Windows reports a file's creation time as its
    ctime: a same-size source edit whose time population puts back onto a row checkout wrote in that
    same second looks unchanged to Git once the index is written later (proving run 39251921588). The
    rows checkout wrote are hashed whatever their stat says, so the whole-tree diff still refuses the
    edit. Emulated on every platform: ctime is not trusted, and the edit and the checkout share a time
    older than every later index write."""
    for key, value in (("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_KEY_0", "core.trustctime"),
                       ("GIT_CONFIG_VALUE_0", "false")):
        monkeypatch.setenv(key, value)
    target = source_tree(tmp_path)
    real_git = trees._git

    def edit_at_checkout(repo_dir, *args, **kwargs):
        if args[:1] != ("reset",):
            return real_git(repo_dir, *args, **kwargs)
        (target / "dirty.cfg").write_bytes(b"v3 dirty\n")  # the baseline's b"v2 dirty\n", same size
        result = real_git(repo_dir, *args, **kwargs)
        earlier = time.time_ns() - 2_000_000_000
        for path in (target / "dirty.cfg", pathlib.Path(repo_dir) / "dirty.cfg"):
            os.utime(path, ns=(earlier, earlier))
        real_git(repo_dir, "update-index", "-q", "--refresh", check=False)  # checkout's row takes that time
        return result

    monkeypatch.setattr(trees, "_git", edit_at_checkout)
    with pytest.raises(subprocess.CalledProcessError) as refused:
        provision(tmp_path, target)
    assert refused.value.returncode == 1 and refused.value.cmd[:3] == ["git", "diff", "--quiet"]
    assert (target / "dirty.cfg").read_bytes() == b"v3 dirty\n"
    nothing_left(tmp_path, target, "fill")


@pytest.mark.parametrize("consumer", ["startup_gc", "disposition"])
def test_a_copy_whose_provisioning_has_not_returned_is_never_a_ready_copy(tmp_path, monkeypatch, consumer):
    target = source_tree(tmp_path)
    reset_done, parked, release = threading.Event(), threading.Event(), threading.Event()
    real_git, real_stream = trees._git, artifacts.stream_artifact_file

    def reset_spy(repo_dir, *args, **kwargs):
        result = real_git(repo_dir, *args, **kwargs)
        if args[:1] == ("reset",):
            reset_done.set()
        return result

    def stream(path, sink=None, **kwargs):
        if reset_done.is_set() and sink is not None and not parked.is_set():
            parked.set()  # population stops before its first file
            assert release.wait(_WAIT_SEC)
        return real_stream(path, sink, **kwargs)

    monkeypatch.setattr(trees, "_git", reset_spy)
    monkeypatch.setattr(artifacts, "stream_artifact_file", stream)
    outcome = {}

    def run():
        try:
            outcome["handle"] = provision(tmp_path, target)
        except Exception as exc:  # surfaced by the assertions below
            outcome["error"] = exc

    worker = threading.Thread(target=run, name="test-provision", daemon=True)
    worker.start()
    try:
        assert parked.wait(_WAIT_SEC)
        row = trees.find_execution_snapshot("fill", tmp_path / "data")
        copy = pathlib.Path(row["path"])
        # The provisional row is light: no completion facts, and its bytes are checkout's, not the source's.
        assert row["provisioning_sec"] == 0 and row["file_baseline"] == {} and row["untracked_baseline"] == {}
        assert (copy / "lines.txt").read_bytes() == b"alpha\r\nbeta\r\n" != (target / "lines.txt").read_bytes()
        if consumer == "startup_gc":  # custody is not open yet, but the task has not been proved over
            assert trees.prune_execution_snapshots(set(), worktree_root=tmp_path / "snapshots",
                                                   data_dir=tmp_path / "data") == {"removed": [], "kept": ["fill"]}
        else:
            removed = trees.remove_execution_snapshot("fill", worktree_root=tmp_path / "snapshots",
                                                      data_dir=tmp_path / "data")
            # Where a file population holds open cannot be deleted (Windows), the removal defers: its
            # row stays marked for custody GC, and the failing provisioning discards the copy below.
            assert removed or (not open_files_deletable(tmp_path)
                               and (trees.find_execution_snapshot("fill", tmp_path / "data") or {}).get("removal_failed"))
    finally:
        release.set()
        worker.join(_WAIT_SEC)
    if consumer == "startup_gc":
        from ouroboros.task_results import write_task_result
        assert "error" not in outcome and outcome["handle"].path == str(copy)
        assert (copy / "lines.txt").read_bytes() == (target / "lines.txt").read_bytes()
        write_task_result(tmp_path / "data", "fill-task", "completed")
        monkeypatch.setattr("supervisor.queue.task_settlement_liveness", lambda _task: False)
        assert trees.prune_execution_snapshots(set(), worktree_root=tmp_path / "snapshots",
                                               data_dir=tmp_path / "data")["removed"] == ["fill"]
    else:
        assert "handle" not in outcome  # explicit disposition cannot return a removed copy as ready
        assert isinstance(outcome["error"], (OSError, subprocess.CalledProcessError)), outcome
    nothing_left(tmp_path, target, "fill")  # nothing resurrected after the removal


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs a privilege on Windows")
def test_population_refuses_a_path_checkout_left_as_a_link(tmp_path, monkeypatch):
    target = source_tree(tmp_path)
    real_git = trees._git

    def replace_with_link(repo_dir, *args, **kwargs):
        result = real_git(repo_dir, *args, **kwargs)
        if args[:1] == ("reset",):
            outside = tmp_path / "outside.cfg"
            outside.write_bytes(b"never written\n")
            (pathlib.Path(repo_dir) / "dirty.cfg").unlink()
            os.symlink(outside, pathlib.Path(repo_dir) / "dirty.cfg")
        return result

    monkeypatch.setattr(trees, "_git", replace_with_link)
    with pytest.raises(OSError, match="not a regular file"):
        provision(tmp_path, target)
    assert (tmp_path / "outside.cfg").read_bytes() == b"never written\n"
    nothing_left(tmp_path, target, "fill")


@pytest.mark.parametrize("flow", ["snapshot", "task_copy"])
def test_an_entry_outside_an_inherited_sparse_cone_is_still_copied(tmp_path, monkeypatch, flow):
    root = tmp_path / "sparse"
    (root / "docs").mkdir(parents=True)
    for args in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "f@example.invalid")):
        git(root, *args)
    (root / "a.txt").write_bytes(b"in cone\n")
    (root / "docs" / "b.txt").write_bytes(b"committed\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "fixture")
    (root / "docs" / "b.txt").write_bytes(b"dirty outside the cone\n")
    if git(root, "sparse-checkout", "set", "--no-cone", "/a.txt", check=False).returncode != 0:
        pytest.skip("this git has no sparse-checkout")
    if not (root / "docs" / "b.txt").exists():
        pytest.skip("this git removed the modified entry outside the cone")
    fallback, real_copy = [], artifacts.copy_artifact_file

    def counted_copy(source, destination, **kwargs):
        fallback.append(pathlib.Path(destination).relative_to(copies).as_posix())
        return real_copy(source, destination, **kwargs)

    copies = (tmp_path / "snapshots").resolve()
    monkeypatch.setattr(artifacts, "copy_artifact_file", counted_copy)
    if flow == "snapshot":
        handle = provision(tmp_path, root, snapshot_id="sparse")
    else:
        handle = trees.provision_worktree(repo_dir=root, task_id="sparse-task", worktree_root=copies,
                                          data_dir=tmp_path / "data", source_is_system_repo=False)
    copy = pathlib.Path(handle.path).resolve()
    assert (copy / "docs" / "b.txt").read_bytes() == b"dirty outside the cone\n"
    assert (copy / "a.txt").read_bytes() == b"in cone\n"
    if not fallback:
        pytest.skip("this git did not carry the sparse patterns into the new worktree")
    assert fallback == [(copy / "docs" / "b.txt").relative_to(copies).as_posix()]


def test_a_copy_removed_mid_population_is_not_re_created(tmp_path, monkeypatch):
    target = source_tree(tmp_path)
    fallback, real_fill = [], trees._fill_private_copy
    monkeypatch.setattr(artifacts, "copy_artifact_file", lambda *a, **k: fallback.append(a[1]))

    def remove_after_first(source, checkout, copy_root):
        real_fill(source, checkout, copy_root)
        if copy_root.exists():
            shutil.rmtree(copy_root)  # a removal that has already reached .git

    monkeypatch.setattr(trees, "_fill_private_copy", remove_after_first)
    with pytest.raises(OSError):
        provision(tmp_path, target)
    assert fallback == []
    nothing_left(tmp_path, target, "fill")
