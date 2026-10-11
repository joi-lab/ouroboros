"""Exact, bounded prompt metadata for staged binary files."""

from __future__ import annotations

import os
import pathlib
import subprocess


class StagedDiffUnavailable(RuntimeError):
    """The canonical staged diff could not be captured.

    A RuntimeError so it lands in the prompt-assembly fail-closed path: a review
    whose only evidence of a degraded file is the staged diff must not run
    authoritatively on a placeholder string that says the capture failed.
    """


def capture_staged_diff(repo_dir: pathlib.Path, *, unified: int = 3) -> str:
    """The staged diff exactly as the reviewer must see it.

    ONE capture for both ladder rungs (full context and ``-U0``). The flag tail
    pins away operator config that rewrites diff output into something that no
    longer describes the staged bytes: external diff drivers, textconv filters,
    colour escapes and prefix rewrites; GIT_DIFF_OPTS leaves the environment
    because it overrides the context width from outside the argv. Output is
    taken as BYTES and decoded strictly; non-UTF-8 staged text is rendered with
    ``backslashreplace`` plus an explicit note, so it reaches the reviewer in a
    readable form instead of raising or being flattened into U+FFFD.
    """
    env = {k: v for k, v in os.environ.items() if k != "GIT_DIFF_OPTS"}
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-color",
             "--src-prefix=a/", "--dst-prefix=b/", f"--unified={int(unified)}"],
            cwd=repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=300, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise StagedDiffUnavailable(f"staged diff capture failed: {exc!r}") from exc
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise StagedDiffUnavailable(
            f"staged diff capture failed (rc {result.returncode}): {detail or 'no detail'}"
        )
    raw = result.stdout or b""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        rendered = raw.decode("utf-8", "backslashreplace")
        return (
            f"{rendered}\n\n*(staged diff contained non-UTF-8 bytes; they are "
            "rendered above as backslash escapes)*\n"
        )


def _git_run(repo_dir: pathlib.Path, args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_bytes(repo_dir: pathlib.Path, args: list[str]) -> bytes:
    result = _git_run(repo_dir, args)
    if result is None or result.returncode != 0:
        return b""
    return result.stdout


def _ref_status(repo_dir: pathlib.Path, ref: str) -> bool | None:
    """Resolve ``ref``: True when it names a real object, False when git
    PROVES it absent (``--verify --quiet`` exits 1: no MERGE_HEAD outside a
    merge, an unborn HEAD), None when the probe itself failed (spawn error,
    timeout, any other exit) — a failed probe is not proof of absence.

    Disclosed residual: git reports an unreadable loose ref file with the
    same exit 1 as a missing ref, so broken ref storage still reads as
    absence; git itself cannot tell those apart at this seam."""
    result = _git_run(repo_dir, ["rev-parse", "--verify", "--quiet", ref])
    if result is None:
        return None
    if result.returncode == 0:
        return True
    return False if result.returncode == 1 else None


def _tree_entry(repo_dir: pathlib.Path, ref: str, rel: str) -> tuple[str, str, bool]:
    """Look up ``rel`` in ``ref``'s tree. Returns ``(mode, blob, ok)``.

    ``ok`` is False when the tree read failed and the ref is not PROVEN
    absent: the ref resolves but its tree is corrupt or unreadable, or the
    absence probe itself errored (spawn failure, timeout, unexpected exit).
    Only a proven missing ref (no MERGE_HEAD outside a merge, an unborn
    HEAD) or a path with no entry in a tree that read fine is a real
    ``ok=True`` absence.
    """
    result = _git_run(repo_dir, ["ls-tree", "-z", ref, "--", rel])
    if result is None or result.returncode != 0:
        return "", "", _ref_status(repo_dir, ref) is False
    record = (result.stdout or b"").split(b"\0", 1)[0]
    fields = record.partition(b"\t")[0].split()
    if len(fields) < 3:
        return "", "", True
    return fields[0].decode("ascii"), fields[2].decode("ascii"), True


def _object_size(repo_dir: pathlib.Path, blob_oid: str) -> str:
    value = _git_bytes(repo_dir, ["cat-file", "-s", blob_oid]).strip()
    return value.decode("ascii") if value.isdigit() else ""


def staged_path_is_binary(
    repo_dir: pathlib.Path, rel: str, *, m0_tree: str = "", staged_tree: str = ""
) -> bool:
    """Detect staged binary content from Git numstat, independent of filename.

    For a managed resolution (both baseline trees given) the detection ALSO
    consults the M0 -> S numstat: the reviewed delta is what the managed packet
    renders, and a path binary IN THAT DELTA must be represented as metadata
    even when the stage-vs-HEAD numstat alone would not call it binary."""
    expected_path = os.fsencode(rel)
    probes = [["diff", "--cached", "--numstat", "-z", "--", rel]]
    if m0_tree and staged_tree:
        probes.append(["diff", "--numstat", "-z", m0_tree, staged_tree, "--", rel])
    for probe in probes:
        for record in _git_bytes(repo_dir, probe).split(b"\0"):
            fields = record.split(b"\t", 2)
            if len(fields) == 3 and fields[2] == expected_path:
                if fields[0] == b"-" and fields[1] == b"-":
                    return True
    return False


def render_staged_binary_metadata(
    repo_dir: pathlib.Path, rel: str, *, m0_tree: str = ""
) -> str | None:
    """Render stage-0 identity, or fail closed when the index object is unbound.

    ``m0_tree`` (managed resolutions only) adds the mechanical-merge baseline
    identity: managed rows compare M0 vs the final candidate, and both real
    merge parents (pre-update HEAD, official MERGE_HEAD) are already rendered."""

    def _m0_row() -> str:
        if not m0_tree:
            return ""
        mode, blob, ok = _tree_entry(repo_dir, m0_tree, rel)
        identity = f"{mode} {blob}" if blob else ("unreadable" if not ok else "absent")
        return f"- mechanical merge M0 blob: `{identity}`\n"
    raw = _git_bytes(repo_dir, ["ls-files", "--stage", "-z", "--", rel])
    expected_path = os.fsencode(rel)
    for record in raw.split(b"\0"):
        prefix, separator, path = record.partition(b"\t")
        fields = prefix.split()
        if separator and path == expected_path and len(fields) == 3 and fields[2] == b"0":
            mode = fields[0].decode("ascii")
            blob_oid = fields[1].decode("ascii")
            object_size = _object_size(repo_dir, blob_oid)
            if not object_size:
                return None
            _head_mode, head_blob, head_ok = _tree_entry(repo_dir, "HEAD", rel)
            _merge_mode, merge_blob, merge_ok = _tree_entry(repo_dir, "MERGE_HEAD", rel)
            if not head_ok or not merge_ok:
                return None
            return (
                "*(binary bytes are represented by exact Git metadata for this review; "
                "they are not rendered as text)*\n\n"
                f"- staged blob: `{blob_oid}`\n"
                f"- staged mode: `{mode}`\n"
                f"- staged object size: `{object_size}` bytes\n"
                + _m0_row()
                + f"- pre-merge HEAD blob: `{head_blob or 'absent'}`\n"
                f"- official MERGE_HEAD blob: `{merge_blob or 'absent'}`\n"
            )
    deleted = _git_bytes(
        repo_dir, ["diff", "--cached", "--name-only", "--diff-filter=D", "-z", "--", rel]
    ).split(b"\0")
    m0_blob = ""
    if expected_path not in deleted:
        # Managed resolutions delete against M0, not only against HEAD: a path
        # the official target ADDED (absent from HEAD, so absent from the
        # HEAD→index deletion set above) that the resolver deletes exists in M0
        # but has no stage-0 entry — that IS the reviewed M0→S deletion, and it
        # must be represented, never silently dropped. Any remaining index
        # entry (an unmerged stage-1..3 path) is NOT a deletion and stays out.
        if not m0_tree or raw.strip():
            return None
        _m0_mode, m0_blob, m0_ok = _tree_entry(repo_dir, m0_tree, rel)
        if not m0_ok or not m0_blob:
            return None
    head_mode, head_blob, head_ok = _tree_entry(repo_dir, "HEAD", rel)
    merge_mode, merge_blob, merge_ok = _tree_entry(repo_dir, "MERGE_HEAD", rel)
    if not head_ok or not merge_ok:
        return None
    parent_blob = head_blob or merge_blob or m0_blob
    object_size = _object_size(repo_dir, parent_blob) if parent_blob else ""
    if not parent_blob or not object_size:
        return None
    return (
        "*(binary deletion is represented by exact Git metadata for this review; "
        "the deleted bytes are not rendered as text)*\n\n"
        "- staged blob: `absent (deletion)`\n"
        "- staged mode: `absent`\n"
        f"- deleted object size: `{object_size}` bytes\n"
        + _m0_row()
        + f"- pre-merge HEAD: `{head_mode or 'absent'} {head_blob or 'absent'}`\n"
        f"- official MERGE_HEAD: `{merge_mode or 'absent'} {merge_blob or 'absent'}`\n"
    )
