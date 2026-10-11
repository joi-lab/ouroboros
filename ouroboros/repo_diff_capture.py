"""One bytes capture of the acceptance repository diff, and its text projection.

The acceptance packet used to read the repository TWICE — once for the bounded
preview and once for the exact source — so the two views could describe two
different working trees, and both read git through ``text=True``, which raises
``UnicodeDecodeError`` on a text-classified file that is not UTF-8 (outside the
``SubprocessError``/``OSError`` guard, so the whole panel died). This module owns
the ONE capture both views project from:

* bytes, never a decoded stream, so a non-UTF-8 hunk cannot crash the capture;
* an explicit gap for every nonzero exit, timeout, unreadable repo and bounded
  cut — an unavailable diff is never projected as an empty (clean) tree;
* bounded memory and a bounded deadline: git writes to a private spool file
  (no pipe to deadlock on, a real subprocess timeout), only a bounded prefix of
  each section is held in memory, and a section past that ceiling keeps its
  FULL bytes on disk for the private retention instead of being thrown away.

The EXACT bytes stay private (``ouroboros.observability`` blobs: 0700/0600,
content-addressed, streamed, outside the actor-readable artifact store). What
reaches a reviewer, a log, an export or a download is the REDACTED text
projection: decoded and redacted in full BEFORE any presentation cut, so a
secret straddling a cut boundary can never leak half of itself.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import pathlib
import stat as stat_mod
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

# One capture's wall-clock and memory bounds. The ceiling bounds the HOST's
# memory, not the truth: the bytes past it stay on the private spool, the cut is
# disclosed as a gap and the projection says so.
CAPTURE_TIMEOUT_SEC = 20.0
CAPTURE_MAX_BYTES = 4_000_000
_READ_CHUNK = 65536
# Past this many individually located undecodable runs the decoder stops
# locating and finishes the remainder in one bounded pass.
_MAX_DECODE_GAPS = 32
_DECODE_MARK = "�"

# Source identity bounds: how much of the changed working tree is hashed by
# content before the identity becomes unknown (never an mtime proxy).
SOURCE_IDENTITY_TIMEOUT_SEC = 10.0
SOURCE_IDENTITY_MAX_FILES = 400
SOURCE_IDENTITY_MAX_BYTES = 8_000_000
_SOURCE_LISTING_MAX_BYTES = 1_000_000

# The section headers are part of the historical evidence text: existing
# readers (and their tests) find untracked files and the turn's commit by these
# exact lines, so the capture carries them and both projections reuse them.
UNTRACKED_HEADER = (
    "# Untracked working-tree files (new, not yet committed; "
    "may include pre-existing untracked files):"
)
COMMIT_HEADER = "# Most recent commit (committed this turn):"

_SECTION_HEADERS = {"tracked": "", "untracked": UNTRACKED_HEADER, "commit": COMMIT_HEADER}
_STRIPPED_SECTIONS = frozenset({"untracked", "commit"})
# Sections that are unified diffs: their changed lines start with a marker.
_DIFF_SECTIONS = frozenset({"tracked", "commit"})
_DIFF_LINE_MARKERS = ("+", "-")
_DIFF_FILE_HEADERS = ("+++ ", "--- ")
_REDACTED = "***REDACTED***"

# A capture that hit one of these never observed the tree; anything else
# (a bounded cut, an undecodable run) observed it partially and says where.
_UNAVAILABLE_STATUSES = frozenset({"git_unavailable", "git_timeout", "git_exit_nonzero"})
# A selected root PROVEN to be a plain folder (`.git` and `HEAD` both absent by
# lstat) has no Git baseline: its capture is `applicable=False` — complete, no
# Git call, never a clean-tree claim — and its source identity is a bounded
# content hash of the folder itself. Any entry (a broken gitfile included), an
# unreadable or a missing root stays Git-required and keeps its typed gaps.
PLAIN_FOLDER_NOTE = (
    "# REPOSITORY DIFF NOT APPLICABLE: the selected root is a plain folder without a Git "
    "repository, so no baseline exists to diff against. This is NOT a clean-tree claim."
)
# Git's own retargeting variables would make a capture read a FOREIGN repository
# (or turn a plain folder into one); the selected root alone decides.
_GIT_RETARGET_ENV = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
})


@dataclass(frozen=True)
class RepoDiffCapture:
    """One read of the repository, as bytes, with every gap it could not close.

    ``sections`` hold a BOUNDED prefix of each section; ``spool`` names the
    private files that hold the FULL bytes of any section that exceeded the
    memory ceiling. ``raw_sha256``/``raw_size`` describe the exact raw stream
    (headers included) whether or not it fit in memory. ``applicable`` is False
    for a proven plain folder: nothing to diff, which is not a clean tree.
    """

    sections: Tuple[Tuple[str, bytes], ...] = ()
    gaps: Tuple[Dict[str, Any], ...] = ()
    repo: str = ""
    raw_sha256: str = ""
    raw_size: int = 0
    spool: Tuple[Tuple[str, str], ...] = ()
    applicable: bool = True

    @property
    def available(self) -> bool:
        """Did the capture observe the tree at all? An unavailable capture is
        NOT an empty diff — the caller must project the gap, never a clean tree."""
        return not any(str(gap.get("status") or "") in _UNAVAILABLE_STATUSES for gap in self.gaps)

    @property
    def complete(self) -> bool:
        return self.available and not self.gaps

    def section(self, name: str) -> bytes:
        for key, data in self.sections:
            if key == name:
                return data
        return b""

    def iter_raw(self) -> Iterator[bytes]:
        """The exact raw stream: headers plus the FULL bytes of every section,
        read from the private spool where a section exceeded memory."""
        spooled = dict(self.spool)
        for name, data in self.sections:
            header = _SECTION_HEADERS.get(name, "")
            if header:
                yield f"\n{header}\n".encode("utf-8")
            path = spooled.get(name)
            if not path:
                yield data
                continue
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_READ_CHUNK)
                    if not chunk:
                        break
                    yield chunk

    def release(self) -> None:
        """Drop the private spool files; idempotent, never raises."""
        for _name, path in self.spool:
            _unlink_quietly(path)


def _unlink_quietly(path: Any) -> None:
    try:
        os.unlink(str(path))
    except OSError:
        pass


def _read_prefix(path: Any, limit: int) -> bytes:
    try:
        with open(str(path), "rb") as fh:
            return fh.read(max(0, int(limit)))
    except OSError:
        return b""


def _run_git_to_file(repo: Any, args: List[str], *, deadline: float) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Run one git command with stdout spooled to a private file.

    No pipe is read by the host, so a child that floods stderr cannot deadlock
    the capture, and ``subprocess.run``'s own timeout kills a child that does
    not finish by the shared deadline. The returned path ALWAYS exists (possibly
    empty); the caller owns its removal. A gap names why the output is not the
    whole truth: ``git_unavailable``, ``git_timeout`` or ``git_exit_nonzero``.
    """
    command = " ".join(["git", *args])
    out_fd, out_path = tempfile.mkstemp(prefix="ouroboros-repo-diff-", suffix=".bin")
    try:
        err_fd, err_path = tempfile.mkstemp(prefix="ouroboros-repo-diff-", suffix=".err")
    except BaseException:
        os.close(out_fd)  # a half-created spool pair closes and removes what it opened
        _unlink_quietly(out_path)
        raise
    gap: Optional[Dict[str, Any]] = None
    returncode: Optional[int] = None
    try:
        with os.fdopen(out_fd, "wb") as out, os.fdopen(err_fd, "wb") as err:
            try:
                proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    ["git", "-c", "core.fsmonitor=false", *args], cwd=str(repo),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err, check=False,
                    env={**{k: v for k, v in os.environ.items() if k not in _GIT_RETARGET_ENV},
                         "LC_ALL": "C",
                         "GIT_CEILING_DIRECTORIES": str(pathlib.Path(repo).resolve().parent)},
                    timeout=max(0.1, deadline - time.monotonic()),
                )
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                gap = {"command": command, "status": "git_timeout",
                       "detail": f"no result within {CAPTURE_TIMEOUT_SEC:.0f}s"}
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                gap = {"command": command, "status": "git_unavailable",
                       "detail": f"{type(exc).__name__}: Git diagnostics withheld"}
        if gap is None and returncode != 0:
            # stderr may end inside a credential, even when the process exited.
            # No bounded prefix can be safely redacted. Publish only typed facts.
            gap = {"command": command, "status": "git_exit_nonzero", "returncode": returncode,
                   "detail": "Git diagnostics withheld"}
    finally:
        _unlink_quietly(err_path)
    return out_path, gap


def _repo_base(repo: Any, *, deadline: float) -> Tuple[str, bool, Optional[Dict[str, Any]]]:
    """HEAD, or an empty-tree base only for a proven missing symbolic branch.

    A failed HEAD read alone proves nothing: corrupt refs/objects, timeouts and
    unavailable Git must stay unavailable. Hashing the empty tree is read-only
    and follows the repository's object format (SHA-1 or SHA-256).
    """
    def read(args):
        path, gap = _run_git_to_file(repo, args, deadline=deadline)
        try:
            return _read_prefix(path, 1024).decode("ascii", errors="replace").strip(), gap
        finally:
            _unlink_quietly(path)

    head, failure = read(["rev-parse", "--verify", "HEAD^{commit}"])
    if failure is None and head:
        return head, False, None
    if failure and failure.get("status") == "git_exit_nonzero":
        branch, gap = read(["symbolic-ref", "--quiet", "HEAD"])
        if gap is None and branch.startswith("refs/heads/"):
            _, gap = read(["show-ref", "--verify", "--quiet", branch])
            if gap and gap.get("status") == "git_exit_nonzero" and gap.get("returncode") == 1:
                empty, gap = read(["hash-object", "-t", "tree", "--stdin"])
                if gap is None and empty:
                    return empty, True, None
    return "HEAD", False, failure or {"status": "git_unavailable", "detail": "HEAD identity unavailable"}


def proven_plain_folder(root: Any) -> bool:
    """Is ``root`` an existing directory with NEITHER a ``.git`` entry NOR a
    ``HEAD`` entry (lstat, no traversal)? Only that proof makes it a plain
    folder. Any entry — a directory, a valid or broken gitfile, a bare/git-dir
    ``HEAD`` — an unreadable or a missing root stays Git-required, so its
    capture and identity keep Git's typed unavailability. The selected root
    alone is inspected: a parent repository is never discovered."""
    try:
        if not stat_mod.S_ISDIR(os.stat(str(root)).st_mode):
            return False
        for name in (".git", "HEAD"):
            try:
                os.lstat(os.path.join(str(root), name))
                return False
            except FileNotFoundError:
                continue
    except OSError:
        return False
    return True


def capture_repo_diff(
    repo: Any, *, include_recent_commit: bool = False, limit: int = CAPTURE_MAX_BYTES,
) -> RepoDiffCapture:
    """Capture the working-tree diff (and optionally this turn's commit) ONCE.

    Both acceptance views — the bounded packet preview and the exact source —
    project this single capture, so they can never describe two different
    trees. ``limit`` bounds the bytes held in MEMORY per section; a section past
    it keeps its full bytes on a private spool for the retention step, and the
    holder must ``release()`` the capture once it is retained or projected.
    """
    if not repo:
        return RepoDiffCapture()
    if proven_plain_folder(repo):
        return RepoDiffCapture(repo=str(repo), applicable=False)  # no baseline, no Git call, no gap
    deadline = time.monotonic() + CAPTURE_TIMEOUT_SEC
    base, unborn, base_gap = _repo_base(repo, deadline=deadline)
    commands = [
        ("tracked", ["diff", "--no-ext-diff", "--no-textconv", "--no-color", base, "--"]),
        ("untracked", ["ls-files", "--others", "--exclude-standard"]),
    ]
    if include_recent_commit and not unborn:
        commands.append(
            ("commit", ["show", "--no-ext-diff", "--no-textconv", "--no-color", "--stat", "-p", "HEAD"]),
        )
    sections: List[Tuple[str, bytes]] = []
    gaps: List[Dict[str, Any]] = [{"section": "tracked", **base_gap}] if base_gap else []
    spool: List[Tuple[str, str]] = []
    hasher, raw_size = hashlib.sha256(), 0
    path = ""  # the section file being read; a removed or spooled one is harmless to remove again
    try:
        for name, args in commands:
            path, gap = _run_git_to_file(repo, args, deadline=deadline)
            if gap is not None:
                gaps.append({"section": name, **gap})
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size <= 0:
                _unlink_quietly(path)
                continue
            header = _SECTION_HEADERS.get(name, "")
            if header:
                header_bytes = f"\n{header}\n".encode("utf-8")
                hasher.update(header_bytes)
                raw_size += len(header_bytes)
            parts: List[bytes] = []
            taken = 0
            try:
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(_READ_CHUNK)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        raw_size += len(chunk)
                        if taken < limit:
                            take = min(len(chunk), limit - taken)
                            parts.append(chunk[:take])
                            taken += take
            except OSError as exc:
                gaps.append({"section": name, "command": " ".join(["git", *args]), "status": "git_unavailable",
                             "detail": f"{type(exc).__name__}: captured output unreadable"})
                _unlink_quietly(path)
                continue
            sections.append((name, b"".join(parts)))
            if size > taken:
                gaps.append({
                    "section": name, "command": " ".join(["git", *args]),
                    "status": "capture_bytes_truncated", "captured_bytes": taken, "total_bytes": size,
                    "detail": (f"the in-memory ceiling ({limit} bytes) was reached; {size - taken} further "
                               "bytes of this section are held only in the private source"),
                })
                spool.append((name, path))
            else:
                _unlink_quietly(path)
    except BaseException:
        # The capture that would own these files is never constructed: release
        # the section being read and every earlier spool before propagating.
        _unlink_quietly(path)
        for _name, held in spool:
            _unlink_quietly(held)
        raise
    return RepoDiffCapture(
        sections=tuple(sections), gaps=tuple(gaps), repo=str(repo),
        raw_sha256=hasher.hexdigest() if sections else "", raw_size=raw_size, spool=tuple(spool),
    )


def decode_capture_text(data: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    """Decode captured bytes, keeping every readable run and LOCATING the rest.

    A binary hunk, a latin-1 source file or a mixed patch keeps its readable
    text; each undecodable run becomes one explicit gap (byte offset + length)
    and one replacement mark in the text. Nothing is dropped silently, and the
    work stays bounded: past ``_MAX_DECODE_GAPS`` located runs the remainder is
    decoded in one replacing pass and summarized as a single gap.
    """
    if not data:
        return "", []
    parts: List[str] = []
    gaps: List[Dict[str, Any]] = []
    index = 0
    total = len(data)
    while index < total:
        if len(gaps) >= _MAX_DECODE_GAPS:
            tail = data[index:]
            parts.append(tail.decode("utf-8", errors="replace"))
            gaps.append({"byte_offset": index, "bytes": len(tail), "reason": "further_undecodable_runs",
                         "located": False})
            break
        try:
            parts.append(data[index:].decode("utf-8"))
            index = total
        except UnicodeDecodeError as exc:
            parts.append(data[index:index + exc.start].decode("utf-8"))
            gaps.append({"byte_offset": index + exc.start, "bytes": max(1, exc.end - exc.start),
                         "reason": str(exc.reason), "located": True})
            parts.append(_DECODE_MARK)
            index += max(exc.end, exc.start + 1)
    return "".join(parts), gaps


def _redact_diff_text(text: str) -> str:
    """Redact a unified diff with each changed line's marker set aside.

    The assignment rules anchor a key at a line start or after whitespace; in a
    diff the changed line starts with ``+``/``-`` instead, so ``+API_KEY = <opaque>``
    and ``-API_KEY = <opaque>`` (a value without a provider prefix, which only
    the assignment rule can catch) reached the reviewer intact. Each marked
    line's BODY is redacted as its own line and the marker is restored, so the
    projection's bytes change only where a secret was masked; file headers
    (``--- a/``, ``+++ b/``) and unmarked lines are redacted exactly as before.
    """
    from ouroboros.observability import redact_projection

    lines = text.split("\n")
    markers = [line[:1] if line[:1] in _DIFF_LINE_MARKERS and not line.startswith(_DIFF_FILE_HEADERS) else ""
               for line in lines]
    bodies = [line[len(marker):] for line, marker in zip(lines, markers)]
    redacted = str(redact_projection("\n".join(bodies)).value).split("\n")
    if len(redacted) != len(bodies):  # a rule consumed a line break: redact line by line instead
        redacted = [str(redact_projection(body).value) for body in bodies]
    return "\n".join(marker + body for marker, body in zip(markers, redacted))


def repo_diff_projection(capture: Any, *, section_limits: Dict[str, int] | None = None):
    """Decode one capture into the historical, REDACTED evidence text, plus its gaps.

    Each section is decoded and redacted WHOLE before any presentation bound is
    applied, so a credential straddling a cut can never leak a fragment past
    the redactor. ``section_limits`` bounds an individual section (the packet
    preview bounds the tracked patch and the untracked list separately, so a
    huge patch can never push the untracked names out). A bound is a PROJECTION
    bound: it never shortens the captured source, and it discloses itself.
    """
    from ouroboros.observability import redact_projection
    from ouroboros.secret_masking import _PEM_PRIVATE_KEY_RE
    from ouroboros.utils import truncate_review_artifact

    limits = dict(section_limits or {})
    if not getattr(capture, "applicable", True):
        return f"{PLAIN_FOLDER_NOTE}\n", []  # stated as not applicable, never as an empty diff
    decode_gaps: List[Dict[str, Any]] = []
    rendered: Dict[str, str] = {}
    for name, data in getattr(capture, "sections", ()) or ():
        # A memory-cut prefix can end inside a credential or private-key block.
        # Redacting that prefix cannot prove safety. Retain the complete private
        # source but withhold this section rather than publish an unsafe fragment.
        if (name in dict(getattr(capture, "spool", ()) or ()) or any(
            gap.get("section") == name and gap.get("status") in _UNAVAILABLE_STATUSES
            for gap in getattr(capture, "gaps", ()) or ()
        )):
            rendered[name] = "[Section withheld: complete output is unavailable within the safe text projection bound.]"
            continue
        text, gaps = decode_capture_text(data)
        decode_gaps.extend({"section": name, "status": "undecodable_bytes", **gap} for gap in gaps)
        # Git's binary marker is valid UTF-8 but carries no changed contents.
        # Keep neighbouring text hunks while disclosing that coverage gap.
        offset = 0
        for line in data.splitlines(keepends=True):
            if name in {"tracked", "commit"} and line.startswith(b"Binary files ") and line.rstrip().endswith(b" differ"):
                decode_gaps.append({"section": name, "status": "binary_content_omitted",
                                    "byte_offset": offset, "bytes": len(line), "located": True,
                                    "detail": "Git reported a binary change without its contents"})
            offset += len(line)
        if name in _STRIPPED_SECTIONS:
            text = text.strip()
        text = _redact_diff_text(text) if name in _DIFF_SECTIONS else str(redact_projection(text).value)
        # Mask a whole private-key block in the diagnostic projection before
        # any presentation bound can cut it; source file reads stay unchanged.
        text = _PEM_PRIVATE_KEY_RE.sub(_REDACTED, text)
        if name in limits:
            text = truncate_review_artifact(text, limit=limits[name])
        rendered[name] = text
    out = rendered.get("tracked", "")
    if rendered.get("untracked"):
        out = f"{out}\n{UNTRACKED_HEADER}\n{rendered['untracked']}\n"
    if rendered.get("commit"):
        out = f"{out}\n{COMMIT_HEADER}\n{rendered['commit']}\n"
    note = capture_gap_note([*(getattr(capture, "gaps", ()) or ()), *decode_gaps])
    return (f"{out}{note}" if note else out), decode_gaps


def repo_diff_projection_text(capture: Any, *, section_limits: Dict[str, int] | None = None) -> str:
    """The text half of ``repo_diff_projection``."""
    return repo_diff_projection(capture, section_limits=section_limits)[0]


def capture_gap_note(gaps: Any) -> str:
    """The reviewer-facing disclosure of everything this capture could not show.

    An empty note means the capture closed every gap — it never means "no diff".
    """
    from ouroboros.observability import redact_projection

    rows = redact_projection([gap for gap in (gaps or []) if isinstance(gap, dict)]).value
    if not rows:
        return ""
    lines = ["", "# REPOSITORY DIFF CAPTURE GAPS (this source is incomplete; it is NOT a clean tree):"]
    for gap in rows[:20]:
        status = str(gap.get("status") or gap.get("reason") or "unknown")
        where = str(gap.get("command") or gap.get("section") or "")
        detail = str(gap.get("detail") or "")
        offset = gap.get("byte_offset")
        located = f" at byte {offset} (+{gap.get('bytes')})" if offset is not None else ""
        lines.append(f"#   {status}{f' [{where}]' if where else ''}{located}"
                     f"{f': {detail}' if detail else ''}")
    if len(rows) > 20:
        lines.append(f"#   (+{len(rows) - 20} further gaps recorded on the capture)")
    return "\n".join(lines) + "\n"


def capture_disclosure(capture: RepoDiffCapture, decode_gaps: Any = ()) -> Dict[str, Any]:
    """The packet-visible FACTS about the private source: identity, size, gaps.

    A digest and a byte count are not the content. The raw bytes themselves are
    never published here — see ``retain_private_capture``; the caller states
    ``raw_retained`` from that step's actual outcome.
    """
    from ouroboros.observability import redact_projection

    gaps = redact_projection([*(capture.gaps or ()), *[dict(gap) for gap in (decode_gaps or [])]]).value
    return {
        "schema": 1,
        "applicable": bool(getattr(capture, "applicable", True)),
        "available": bool(capture.available),
        "complete": bool(capture.available and not gaps),
        "raw_sha256": capture.raw_sha256 if capture.sections else "",
        "raw_bytes": int(capture.raw_size),
        "projection": "redacted_text",
        "gaps": gaps[:40],
        "gaps_omitted": max(0, len(gaps) - 40),
    }


# The private BYTES side of the existing observability CAS, here because this
# capture is its only producer and consumer: a diff larger than the memory
# ceiling must be hashed and compressed as it streams. The store LAYOUT — root,
# private modes, ref resolution — stays with its observability owner and is read
# through that owner at call time.
def write_blob_stream(drive_root: pathlib.Path, chunks: Any, *, kind: str = "bin") -> Dict[str, Any]:
    """Persist a private BYTES stream as a content-addressed gzip blob.

    The chunks are hashed and compressed as they arrive, so a source larger
    than memory (the acceptance repository diff spooled to disk) is retained
    without ever being buffered whole; the digest is known only at the end, so
    the blob is written under a temporary name and renamed onto its address.
    """
    from ouroboros.observability import _chmod_private, _chmod_private_dir, _observability_root
    from ouroboros.utils import replace_atomic

    root = _observability_root(pathlib.Path(drive_root)) / "blobs"
    root.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(root)
    tmp = root / f".stream.{kind}.gz.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    hasher, size = hashlib.sha256(), 0
    try:
        with gzip.open(tmp, "wb") as fh:
            for chunk in chunks:
                if not chunk:
                    continue
                hasher.update(chunk)
                size += len(chunk)
                fh.write(chunk)
        _chmod_private(tmp)
        digest = hasher.hexdigest()
        path = root / f"{digest}.{kind}.gz"
        if path.exists():
            tmp.unlink()  # the same content-addressed blob is already durable
        else:
            replace_atomic(tmp, path)
        _chmod_private(path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return {
        "sha256": digest,
        "path": str(path),
        "kind": kind,
        "encoding": "gzip",
        "size": size,
        "compressed_size": path.stat().st_size if path.exists() else 0,
    }


def read_blob_bytes(drive_root: pathlib.Path, ref: Dict[str, Any], *, expected_kind: str = "bin") -> bytes:
    """Read and verify one content-addressed BYTES blob below this drive root."""
    from ouroboros.observability import _blob_ref_path

    if not isinstance(ref, dict):
        raise ValueError("observability blob ref must be an object")
    if str(ref.get("kind") or "") != expected_kind or ref.get("encoding") != "gzip":
        raise ValueError("observability blob ref has an unexpected kind or encoding")
    expected_sha = str(ref.get("sha256") or "")
    try:
        expected_size = int(ref["size"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("observability blob ref has no valid size") from exc
    path = _blob_ref_path(drive_root, ref)
    with gzip.open(path, "rb") as handle:
        raw = handle.read()
    if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("observability blob ref failed size or sha256 verification")
    return raw


def retain_private_capture(drive_root: Any, capture: RepoDiffCapture) -> Dict[str, Any]:
    """Retain the EXACT captured bytes in the existing private observability CAS.

    The stream is written chunk by chunk (spooled sections included), so a
    source larger than the memory ceiling is retained whole rather than thrown
    away. The returned ref is a HOST-side forensic handle: it is deliberately
    not part of the evidence packet, carries no actor-readable root, and is
    never exported or downloaded. A retention that could not happen says so —
    the packet never implies a retention that never took place. The capture's
    spool is released either way.
    """
    try:
        if drive_root is None or not capture.sections:
            return {}
        try:
            ref = write_blob_stream(pathlib.Path(drive_root), capture.iter_raw(), kind="bin")
            if str(ref.get("sha256")) != capture.raw_sha256 or int(ref.get("size") or -1) != capture.raw_size:
                raise ValueError("the retained stream does not match the capture digest")
            return {"access": "host_private", "store": "observability_blob",
                    "blob_ref": dict(ref), "raw_sha256": capture.raw_sha256, "raw_bytes": capture.raw_size}
        except Exception as exc:  # never fail acceptance because forensics could not be kept
            log.debug("private repo-diff capture retention unavailable", exc_info=True)
            return {"access": "host_private", "status": "unavailable",
                    "reason": f"{type(exc).__name__}: {exc}"}
    finally:
        capture.release()


def read_private_capture(drive_root: Any, private_ref: Dict[str, Any]) -> bytes:
    """Read back the exact private capture bytes (HOST forensics only).

    Never a reviewer, export or download path: the caller must already hold the
    host-private ref, which no published projection carries.
    """
    ref = (private_ref or {}).get("blob_ref")
    if not isinstance(ref, dict):
        raise ValueError("private repo-diff capture ref is unavailable")
    raw = read_blob_bytes(pathlib.Path(drive_root), ref)
    if hashlib.sha256(raw).hexdigest() != str((private_ref or {}).get("raw_sha256") or ""):
        raise ValueError("private repo-diff capture failed sha256 verification")
    return raw


# ── the SOURCE the capture would read, as a bounded identity ────────────────


def _path_identity(root: pathlib.Path, rel: str, budget: int, *, hash_content: bool) -> Tuple[Dict[str, Any], int]:
    """One path's content identity; a non-content row cannot certify equality."""
    path = root / rel
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"path": rel, "state": "absent"}, 0
    except OSError:
        return {"path": rel, "state": "unreadable"}, 0
    if stat_mod.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(path)
        except OSError:
            return {"path": rel, "state": "unreadable"}, 0
        return {"path": rel, "state": "symlink", "target": target}, 0
    if not stat_mod.S_ISREG(st.st_mode):
        return {"path": rel, "state": "other", "mode": int(st.st_mode)}, 0
    if hash_content and st.st_size <= budget:
        hasher = hashlib.sha256()
        total = 0
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > budget:
                        return {"path": rel, "state": "unreadable"}, 0
                    hasher.update(chunk)
                after = os.fstat(fh.fileno())
                if (total != st.st_size or (after.st_size, after.st_mtime_ns, after.st_ino)
                        != (st.st_size, st.st_mtime_ns, st.st_ino)):
                    return {"path": rel, "state": "unreadable"}, 0
        except OSError:
            return {"path": rel, "state": "unreadable", "size": int(st.st_size),
                    "mtime_ns": int(st.st_mtime_ns)}, 0
        return {"path": rel, "state": "hashed", "sha256": hasher.hexdigest(),
                "size": int(st.st_size), "executable": bool(st.st_mode & stat_mod.S_IXUSR)}, int(st.st_size)
    return {"path": rel, "state": "stat", "size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}, 0


def repo_source_identity(
    repo: Any, *, max_files: int = SOURCE_IDENTITY_MAX_FILES, max_bytes: int = SOURCE_IDENTITY_MAX_BYTES,
    timeout: float = SOURCE_IDENTITY_TIMEOUT_SEC,
) -> str:
    """A bounded, streaming identity of the working-tree SOURCE a capture reads.

    HEAD plus every changed or untracked path, hashed by content within the
    bounds; an incomplete inventory raises instead of using size/mtime.
    Unchanged material yields
    the same identity every round; a repaired or edited source changes it. An
    unreadable repository RAISES — the caller records the explicit unknown.
    """
    root = pathlib.Path(str(repo))
    deadline = time.monotonic() + timeout
    if proven_plain_folder(root):
        return _plain_folder_identity(root, max_files=max_files, max_bytes=max_bytes, deadline=deadline)
    head, _unborn, gap = _repo_base(root, deadline=deadline)
    if gap is not None:
        raise RuntimeError("repository HEAD identity unavailable")
    # Match the working-tree-vs-HEAD evidence, not porcelain's index status.
    # Staging the same bytes (or changing only the index) is not new material.
    paths: set[str] = set()
    for args in (["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", head, "--"],
                 ["ls-files", "--others", "--exclude-standard", "-z"]):
        listing_path, gap = _run_git_to_file(root, args, deadline=deadline)
        try:
            if gap is not None:
                raise RuntimeError(f"{gap['status']}: {gap.get('detail') or ''}".strip())
            with open(listing_path, "rb") as fh:
                listing = fh.read(_SOURCE_LISTING_MAX_BYTES + 1)
            if len(listing) > _SOURCE_LISTING_MAX_BYTES:
                raise RuntimeError("repository material identity incomplete")
            paths.update(os.fsdecode(path) for path in listing.split(b"\0") if path)
        finally:
            _unlink_quietly(listing_path)
    files: List[Dict[str, Any]] = []
    budget = int(max_bytes)
    for rel in sorted(paths)[:max_files]:
        row, used = _path_identity(root, rel, budget, hash_content=time.monotonic() <= deadline)
        budget -= used
        files.append(row)
    if len(paths) > max_files or any(
        row.get("state") not in {"hashed", "absent", "symlink"} for row in files
    ):
        # A partial inventory or mtime/size proxy cannot certify unchanged bytes.
        # The incident's stable unknown identity keeps explicit repair/retry open.
        raise RuntimeError("repository material identity incomplete")
    payload = {
        "head": head,
        "files": files,
        "files_omitted": max(0, len(paths) - max_files),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _plain_folder_identity(root: pathlib.Path, *, max_files: int, max_bytes: int, deadline: float) -> str:
    """Bounded content identity of a proven plain folder (no Git baseline).

    Every regular file is hashed by content and every symlink — a directory
    symlink included — is its link target, under the same file/byte/time bounds
    as the Git identity; nothing is traversed through a symlink and a nested
    repository's ``.git`` internals are skipped. A special, unreadable or
    racing entry, or an inventory past its bounds, RAISES: unknown, never a
    size/mtime proxy. The same bytes yield the same identity; an edit (a
    same-size one included), an added or a removed file changes it.
    """
    budget = int(max_bytes)
    rows: List[Dict[str, Any]] = []

    def unreadable(exc):
        raise exc

    for parent, dirs, files in os.walk(root, onerror=unreadable, followlinks=False):
        if time.monotonic() > deadline:
            raise RuntimeError("folder material identity incomplete")
        base = pathlib.Path(parent)
        entries = [name for name in files if name != ".git"]
        kept: List[str] = []
        for name in dirs:
            if name == ".git":
                continue  # a nested repository's internals are not this folder's content
            (entries if (base / name).is_symlink() else kept).append(name)
        dirs[:] = sorted(kept)
        for name in sorted(entries):
            if len(rows) >= max_files:
                raise RuntimeError("folder material identity incomplete")
            row, used = _path_identity(root, str((base / name).relative_to(root)), budget,
                                       hash_content=time.monotonic() <= deadline)
            if row.get("state") not in {"hashed", "symlink"}:
                raise RuntimeError("folder material identity incomplete")
            rows.append(row)
            budget -= used
    payload = {"root": "plain_folder", "files": sorted(rows, key=lambda row: row["path"])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
