"""The wrapper replays exact Git patch bytes across newline conversion policies."""

import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from scripts import run_external_review as runner
from ouroboros.tools import git as review_git


pytestmark = pytest.mark.serial


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf-blob", "crlf-blob"])
@pytest.mark.parametrize("autocrlf", ["false", "true"])
@pytest.mark.parametrize("windows_text", [False, True], ids=["native-stdio", "windows-text-stdio"])
@pytest.mark.parametrize("drift", [False, True], ids=["same-tree", "drift-bytes"])
def test_patch_roundtrip(tmp_path, monkeypatch, eol, autocrlf, windows_text, drift):
    repo, output = tmp_path / "repo", tmp_path / "output"
    repo.mkdir()
    original_run = subprocess.run

    def git_bytes(*args, cwd=repo):
        return original_run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout

    for args in (("init",), ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.invalid"),
                 ("config", "core.autocrlf", autocrlf)):
        git_bytes(*args)
    # Explicit raw text permits either Git blob spelling under either user
    # conversion preference. The ordinary no-attributes fixture is covered by
    # test_external_review_pending_checkout, including on native Windows CI.
    (repo / ".gitattributes").write_bytes(b"change.py -text\n")
    (repo / "VERSION").write_bytes(b"1.0.0\n")
    (repo / "change.py").write_bytes(b"value = 1" + eol)
    git_bytes("add", ".")
    git_bytes("commit", "-m", "base")
    proposed = b"value = 2" + eol
    (repo / "change.py").write_bytes(proposed)
    git_bytes("add", "change.py")
    expected_tree = git_bytes("write-tree")
    observed = {}
    checkouts = []

    def run(args, *positional, **kwargs):
        # POSIX normally masks Windows' text-pipe translation. Exercise that
        # exact transformation only if a regression reintroduces text stdin;
        # native Windows already performs it and must not translate twice.
        if (windows_text and os.name != "nt" and list(args)[:2] == ["git", "apply"]
                and kwargs.get("text") and isinstance(kwargs.get("input"), str)):
            kwargs["input"] = kwargs["input"].replace("\n", "\r\n")
        return original_run(args, *positional, **kwargs)

    def owned_temp(*, prefix):
        root = Path(tempfile.mkdtemp(prefix=prefix, dir=tmp_path))
        checkouts.append(root)
        return str(root)

    def cycle(ctx, _message, **kwargs):
        assert kwargs["skip_advisory_review"] is False
        assert git_bytes("write-tree", cwd=ctx.repo_dir) == expected_tree
        assert (ctx.repo_dir / "change.py").read_bytes() == proposed
        observed["cycle"] = True
        if drift:
            (ctx.repo_dir / "change.py").write_bytes(b"value = 3" + eol)
            observed["drift"] = git_bytes("diff", "HEAD", "--binary", cwd=ctx.repo_dir)
        return {"status": "blocked", "block_reason": "preflight", "message": "fixture"}

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(runner, "tempfile", SimpleNamespace(mkdtemp=owned_temp))
    monkeypatch.setattr(runner, "REPO", repo)
    monkeypatch.setattr(runner, "_parse_args", lambda: SimpleNamespace(
        contributor=False, commit_message="candidate", goal="", scope="",
        output=str(output), drive_root=str(tmp_path / "data"), no_isolated_checkout=False))
    monkeypatch.setattr(runner, "_prepare_review_configuration", lambda _args: (None, "HEAD", {}))
    monkeypatch.setattr(runner, "_advisory_unavailability_warning", lambda: "")
    monkeypatch.setattr(review_git, "_run_non_committing_review_cycle", cycle)
    try:
        assert runner.main() == 3
        assert observed.get("cycle"), "Patch replay must reach the existing review cycle"
        artifact = output / "reviewed-tree-drift.diff"
        assert artifact.exists() is drift
        if drift:
            assert artifact.read_bytes() == observed["drift"]
        assert all(not root.exists() for root in checkouts)
    finally:
        # Also clean a checkout when a regression fails before returning its
        # handle to main; never leave fixture-created Git registrations behind.
        for root in checkouts:
            if root.exists():
                runner._remove_isolated_checkout(root, root / "repo")
