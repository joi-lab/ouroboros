"""Same-device Docker mounts must be refused before the legacy archive can move files.

The two mount rows reproduce the real S39 named-volume failure: identical 253:17
st_dev, different mount IDs/roots, and a nested mount at body/repo. Only their
paths are relocated into the test's disposable tree. The kernel format is defined
at https://docs.kernel.org/filesystems/proc.html#proc-pid-mountinfo-information-about-mounts.
No Docker, mount privileges or synthetic ismount answer is used here.
"""
from __future__ import annotations

import errno
import logging
import os
import pathlib
import subprocess
import sys
from dataclasses import replace

import pytest

from ouroboros import launcher_bootstrap, platform_layer


@pytest.fixture
def mounted_tree(tmp_path, monkeypatch):
    parent = tmp_path / "volume with space's and\\slash"  # no trailing-space component on Windows
    repo = parent / "body" / "repo"
    repo.mkdir(parents=True)
    (repo / "keep.txt").write_text("keep\n")
    assert repo.stat().st_dev == repo.parent.stat().st_dev
    assert not os.path.ismount(repo), "the real stat-based probe does not see the fixture mount"
    context = launcher_bootstrap.BootstrapContext(
        bundle_dir=tmp_path / "seed", repo_dir=repo, data_dir=tmp_path / "data", settings_path=tmp_path / "s.json",
        embedded_python=sys.executable, app_version="7.6.0", hidden_run=subprocess.run,
        save_settings=lambda _: None, log=logging.getLogger("mount-test"))
    table = tmp_path / "mountinfo"
    def escape(path):
        return str(path.resolve()).replace("\\", r"\134").replace(" ", r"\040")
    table.write_text(
        f"324 315 253:17 /docker/volumes/parent/_data {escape(parent)} rw,relatime master:138 - ext4 /dev/vdb1 rw\n"
        f"328 324 253:17 /docker/volumes/repo/_data {escape(repo)} rw,relatime master:138 - ext4 /dev/vdb1 rw\n")
    def mounts(path, **kwargs):
        assert path == "/proc/self/mountinfo"
        return table.open(**kwargs)
    monkeypatch.setattr(platform_layer, "IS_LINUX", True)
    monkeypatch.setattr(platform_layer, "open", mounts, raising=False)
    monkeypatch.setattr(launcher_bootstrap, "load_bundle_manifest", lambda _: {})
    return context, table


def test_same_device_mount_is_refused_without_archiving(mounted_tree):
    context, _ = mounted_tree
    before = (context.repo_dir / "keep.txt").stat()
    with pytest.raises(RuntimeError, match="mount point without a checkout; nothing was moved"):
        launcher_bootstrap.ensure_managed_repo(context)
    assert (context.repo_dir / "keep.txt").read_text() == "keep\n"
    assert (context.repo_dir / "keep.txt").stat().st_ino == before.st_ino
    assert not (context.data_dir / "archive").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs Windows privileges")
def test_resolved_symlink_to_mount_is_also_refused(mounted_tree, tmp_path):
    context, _ = mounted_tree
    link = tmp_path / "alias"
    link.symlink_to(context.repo_dir, target_is_directory=True)
    context = replace(context, repo_dir=link)
    with pytest.raises(RuntimeError, match="mount point without a checkout"):
        launcher_bootstrap.ensure_managed_repo(context)
    assert (link / "keep.txt").read_text() == "keep\n"


def test_nonmount_legacy_archive_keeps_cross_filesystem_fallback(mounted_tree, monkeypatch):
    context, table = mounted_tree
    # Keep the parent mount and an unrelated prefix-neighbor; the repo is NOT a mount.
    table.write_text(table.read_text().replace("repo rw,", "repo-other rw,"))
    assert platform_layer.is_mount_point(context.repo_dir) is False
    rename = os.rename
    def cross_device(source, target, *args, **kwargs):
        if pathlib.Path(source) == context.repo_dir:
            raise OSError(errno.EXDEV, "cross-device fixture")
        return rename(source, target, *args, **kwargs)
    monkeypatch.setattr(os, "rename", cross_device)
    def clone(_context, _manifest):
        staged = context.repo_dir.parent / "new-checkout"
        (staged / ".git").mkdir(parents=True)
        return staged
    monkeypatch.setattr(launcher_bootstrap, "_clone_repo_from_bundle", clone)
    assert launcher_bootstrap.ensure_managed_repo(context) == "replaced"
    assert (context.repo_dir / ".git").is_dir()
    archived = list((context.data_dir / "archive/managed_repo").glob("*/keep.txt"))
    assert len(archived) == 1 and archived[0].read_text() == "keep\n"


def test_existing_checkout_on_mount_is_kept(mounted_tree, monkeypatch):
    context, _ = mounted_tree
    (context.repo_dir / ".git").mkdir()
    monkeypatch.setattr(launcher_bootstrap, "_repo_manifest_matches", lambda *args: True)
    monkeypatch.setattr(launcher_bootstrap, "_ensure_managed_remote", lambda *args: None)
    assert launcher_bootstrap.ensure_managed_repo(context) == "unchanged"
    assert (context.repo_dir / "keep.txt").read_text() == "keep\n"


def test_unreadable_mount_table_refuses_without_guessing(mounted_tree):
    context, table = mounted_tree
    table.unlink()
    with pytest.raises(RuntimeError, match="Cannot determine.*nothing was moved"):
        launcher_bootstrap.ensure_managed_repo(context)
    assert (context.repo_dir / "keep.txt").read_text() == "keep\n"
    assert not (context.data_dir / "archive").exists()
