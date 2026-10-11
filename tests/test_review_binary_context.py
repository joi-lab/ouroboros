"""Hardened staged-diff capture (``capture_staged_diff``) — the one shared
evidence source for the scope reviewer and the triad — and the fail-closed
exact staged-binary metadata renderer (``render_staged_binary_metadata``)."""

import subprocess

import pytest

from ouroboros.tools import review_binary_context
from ouroboros.tools.review_binary_context import (
    StagedDiffUnavailable,
    capture_staged_diff,
    render_staged_binary_metadata,
)


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "f.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
                   capture_output=True)
    return tmp_path


def _fail_tree_read(monkeypatch, *, ref):
    """Fail one exact tree read while real Git still proves the ref exists."""
    real_git_run = review_binary_context._git_run
    failed_calls = []

    def fail_selected_tree(repo_dir, args):
        if args == ["ls-tree", "-z", ref, "--", "bin.dat"]:
            failed_calls.append(args)
            return subprocess.CompletedProcess(args, 128, stdout=b"")
        return real_git_run(repo_dir, args)

    monkeypatch.setattr(review_binary_context, "_git_run", fail_selected_tree)
    return failed_calls


def test_capture_returns_the_staged_diff(tmp_path):
    repo = _repo(tmp_path)
    (repo / "f.py").write_text("a\nb\nCHANGED\nd\ne\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)

    diff = capture_staged_diff(repo)

    assert "+CHANGED" in diff
    assert "a/f.py" in diff and "b/f.py" in diff  # pinned prefixes


def test_capture_unified_zero_drops_unchanged_context(tmp_path):
    repo = _repo(tmp_path)
    (repo / "f.py").write_text("a\nb\nCHANGED\nd\ne\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)

    compact = capture_staged_diff(repo, unified=0)

    assert "+CHANGED" in compact
    assert " a\n" not in compact  # zero context: no unchanged surrounding lines


def test_non_utf8_staged_text_is_escaped_not_flattened(tmp_path):
    """Git treats NUL-free non-UTF-8 content as TEXT, so the bytes ride ordinary
    diff lines. They must reach the reviewer readably escaped — never U+FFFD,
    never an exception, never a placeholder."""
    repo = _repo(tmp_path)
    (repo / "f.py").write_bytes(b"a\nb\ncaf\xe9\nd\ne\n")  # latin-1 0xE9
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)

    diff = capture_staged_diff(repo)

    assert "\\xe9" in diff
    assert "�" not in diff
    assert "staged diff contained non-UTF-8 bytes" in diff


def test_capture_failure_raises_typed_runtime_error(tmp_path):
    (tmp_path / "not_a_repo").mkdir()

    with pytest.raises(StagedDiffUnavailable):
        capture_staged_diff(tmp_path / "not_a_repo")

    # The type is a RuntimeError so existing fail-closed paths catch it.
    assert issubclass(StagedDiffUnavailable, RuntimeError)


def test_git_diff_opts_env_cannot_reshape_the_capture(tmp_path, monkeypatch):
    """GIT_DIFF_OPTS overrides the context width from outside the argv; the
    capture drops it so the requested width survives."""
    repo = _repo(tmp_path)
    (repo / "f.py").write_text("a\nb\nCHANGED\nd\ne\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=0")

    diff = capture_staged_diff(repo)  # default width, env override dropped

    assert "+CHANGED" in diff
    assert " a\n" in diff  # context lines survived the hostile env override


def test_render_staged_binary_metadata_reports_absent_when_head_genuinely_lacks_the_path(
    tmp_path,
):
    """Control case: a newly added binary has no prior HEAD entry and there is
    no merge in progress. Both are a real absence, not a read error, so the
    function must still render normally rather than hard-block."""
    repo = _repo(tmp_path)
    (repo / "new.bin").write_bytes(b"\x00\x01\x02new")
    subprocess.run(["git", "add", "new.bin"], cwd=repo, check=True, capture_output=True)

    metadata = render_staged_binary_metadata(repo, "new.bin")

    assert metadata is not None
    assert "pre-merge HEAD blob: `absent`" in metadata
    assert "official MERGE_HEAD blob: `absent`" in metadata


def test_render_staged_binary_metadata_returns_none_when_git_cannot_even_run(tmp_path):
    """A ``cwd`` git cannot spawn into at all (subprocess raises OSError, not
    just a non-zero exit) must also fail closed, not be treated as absent."""
    missing_dir = tmp_path / "does-not-exist"

    metadata = render_staged_binary_metadata(missing_dir, "anything.bin")

    assert metadata is None


def test_render_staged_binary_metadata_hard_blocks_on_head_tree_read_error(
    tmp_path, monkeypatch,
):
    """A staged binary whose HEAD tree object is unreadable (corrupt object,
    IO error) must fail closed, not render the unread HEAD blob as `absent`:
    a git read failure is not proof the object was never there."""
    repo = _repo(tmp_path)
    (repo / "bin.dat").write_bytes(b"\x00\x01old")
    subprocess.run(["git", "add", "bin.dat"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "add binary"], cwd=repo, check=True,
                    capture_output=True)
    (repo / "bin.dat").write_bytes(b"\x00\x01new")
    subprocess.run(["git", "add", "bin.dat"], cwd=repo, check=True, capture_output=True)
    assert render_staged_binary_metadata(repo, "bin.dat") is not None
    failed_calls = _fail_tree_read(monkeypatch, ref="HEAD")

    metadata = render_staged_binary_metadata(repo, "bin.dat")

    assert metadata is None
    assert len(failed_calls) == 1


@pytest.mark.parametrize("probe_result", [None, 128])
def test_render_staged_binary_metadata_hard_blocks_when_the_ref_probe_itself_fails(
    tmp_path, monkeypatch, probe_result,
):
    """A failed ls-tree whose ref-absence probe ALSO fails (rev-parse times
    out, cannot spawn, or exits with anything but the clean rc 1) must fail
    closed: a broken probe is not proof the ref was absent. Only a proven
    missing ref may render as `absent`."""
    repo = _repo(tmp_path)
    (repo / "bin.dat").write_bytes(b"\x00\x01new")
    subprocess.run(["git", "add", "bin.dat"], cwd=repo, check=True, capture_output=True)

    real_git_run = review_binary_context._git_run

    def broken_ref_probes(repo_dir, args):
        if args[0] == "ls-tree":
            return subprocess.CompletedProcess(args, 128, stdout=b"")
        if args[0] == "rev-parse":
            if probe_result is None:
                return None
            return subprocess.CompletedProcess(args, probe_result, stdout=b"")
        return real_git_run(repo_dir, args)

    monkeypatch.setattr(review_binary_context, "_git_run", broken_ref_probes)

    metadata = render_staged_binary_metadata(repo, "bin.dat")

    assert metadata is None


def test_render_staged_binary_metadata_deletion_hard_blocks_on_merge_head_tree_read_error(
    tmp_path, monkeypatch,
):
    """Same fail-closed contract on the deleted-binary rendering path, and
    specifically for a MERGE_HEAD read failure during a real in-progress
    merge, where HEAD's own tree still reads fine. Silently masking that as
    `official MERGE_HEAD: absent` would hide the exact ambiguity this issue
    reports: a read failure is not proof the merge side never had the file."""
    repo = _repo(tmp_path)
    (repo / "bin.dat").write_bytes(b"\x00\x01old")
    (repo / "other.txt").write_text("a\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True,
                    capture_output=True)
    subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=repo, check=True)
    (repo / "other.txt").write_text("b\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "commit", "-aq", "-m", "side change"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-"], cwd=repo, check=True)
    (repo / "other.txt").write_text("c\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "commit", "-aq", "-m", "main change"], cwd=repo, check=True)
    subprocess.run(["git", "merge", "side"], cwd=repo, check=False, capture_output=True)
    assert (repo / ".git" / "MERGE_HEAD").exists(), (
        "fixture assumption: conflict left a merge in progress"
    )
    subprocess.run(["git", "rm", "-q", "bin.dat"], cwd=repo, check=True, capture_output=True)
    assert render_staged_binary_metadata(repo, "bin.dat") is not None
    failed_calls = _fail_tree_read(monkeypatch, ref="MERGE_HEAD")

    metadata = render_staged_binary_metadata(repo, "bin.dat")

    assert metadata is None
    assert len(failed_calls) == 1
