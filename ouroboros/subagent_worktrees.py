"""Lifecycle for acting-subagent ``self_worktree`` checkouts.

Acting subagents use an isolated ``git worktree`` of the selected Git source's
current eligible files, under a root OUTSIDE ``repo/`` and ``data/``. Source
identity distinguishes Ouroboros-body copies from external project copies.
The child returns its delta; the parent integrates and owns final delivery.

git has no automatic worktree garbage collection, so we keep a durable JSON
registry (``data/state/subagent_worktrees.json``) and prune on the custody cadence.
Mutations of SHARED metadata — a target's ``.git/worktrees`` (add/prune), its
``refs/ouroboros/delegated/*`` pins and the registry file — are serialized by a
portable cross-process lock (the existing repo git lock is drive-root scoped,
not ``.git`` scoped). Tree-proportional work never runs under it (#1241):
listing, classifying, hashing, populating, copying (and re-recording the copied
bytes' stat) and deleting a snapshot's files happen outside the lock, so one
huge inventory delays only its own task.

Registry updates are whole-file read-modify-write, O(rows). Eligible untracked
text is hashed into the target's object database; once the pin or branch is
deleted those blobs are unreachable objects until ``git gc``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.force_remove import force_rmtree as _force_rmtree  # the shared permission-granting removal
from ouroboros.utils import atomic_write_json, utc_now_iso
from ouroboros.config import DATA_DIR, get_subagent_projects_root, get_subagent_worktree_root
from ouroboros.retention import age_cutoff, get_gc_retention_days
from ouroboros import worktree_removal

log = logging.getLogger(__name__)

_REGISTRY_NAME = "subagent_worktrees.json"
_LOCK_NAME = ".worktree_ops.lock"
_LOCK_TIMEOUT_SEC = 120.0
_LOCK_STALE_SEC = 600.0
_BRANCH_PREFIX = "subagent/"
# Delegated-run execution snapshots (C1): registry `kind` and the protected
# baseline ref namespace. The ref pins the baseline commit against GC for as
# long as the snapshot lives; it is deleted with the snapshot.
_KIND_DELEGATED_EXEC = "delegated_exec"
# Own-body authoring candidates (``body_candidate.py``): retention is decided by
# unique work and last use, never by age alone, and never by a child task's id.
_KIND_BODY_CANDIDATE = "body_candidate"
_BASELINE_REF_PREFIX = "refs/ouroboros/delegated/"

# Serializes worktree mutations within this process; the on-disk lock serializes
# across processes (parent worker, supervisor startup prune, etc.).
_inproc_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Paths and registry
# --------------------------------------------------------------------------- #
def _data_dir(data_dir: Optional[Any] = None) -> Path:
    if data_dir:
        return Path(data_dir)
    env = os.environ.get("OUROBOROS_DATA_DIR")
    if env:
        return Path(env)
    return Path(DATA_DIR)


def _registry_path(data_dir: Optional[Any] = None) -> Path:
    return _data_dir(data_dir) / "state" / _REGISTRY_NAME


def _resolve_root(worktree_root: Optional[Any] = None) -> Path:
    root = Path(worktree_root) if worktree_root else Path(get_subagent_worktree_root())
    return root.expanduser().resolve()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _assert_root_isolated(root: Path, repo_dir: Path, data_dir: Path) -> None:
    """Refuse a worktree root that overlaps the live repo or runtime data."""
    if _is_within(root, repo_dir) or _is_within(repo_dir, root):
        raise ValueError(f"subagent worktree root {root} overlaps the Ouroboros repo {repo_dir}")
    if _is_within(root, data_dir) or _is_within(data_dir, root):
        raise ValueError(f"subagent worktree root {root} overlaps runtime data {data_dir}")


def _safe_name(task_id: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id or "").strip())
    safe = safe or f"wt_{int(time.time())}"
    # Bound the path component so an arbitrary-length input (e.g. a project display name,
    # which is not length-validated upstream) never hits ENAMETOOLONG on mkdir. On
    # truncation keep a short hash of the full slug so two long names with the same prefix
    # do not silently collide.
    if len(safe) > 64:
        import hashlib
        digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[:55]}_{digest}"
    return safe


class SubagentWorktreeRegistryCorrupt(RuntimeError):
    """``state/subagent_worktrees.json`` exists but cannot be read as a registry.

    Raised by every caller that would go on to REWRITE the file. Collapsing a
    malformed registry to an empty one hands the next write a clean slate: the
    rows are gone, and with them the only record naming the checkouts and the
    ``refs/ouroboros/delegated/*`` refs those rows pin — a leak nothing can
    reconcile afterwards. The malformed bytes are kept instead, which is what
    the sibling registries (cancel intents, terminal deliveries) do and what
    the custody GC does when the custody log is unreadable.
    """


def _load_registry(
    data_dir: Optional[Any] = None, *, strict: bool = False, op: str = "",
) -> List[Dict[str, Any]]:
    """The registered rows; ``strict`` refuses a malformed registry.

    ABSENT is an ordinary empty registry in both modes (the first-write case).
    MALFORMED is a different fact, and ``strict=True`` — passed by everything
    that authors a record or acts destructively on one — reports it as such
    instead of as "nothing is registered". Inspection reads (the UI listing)
    stay soft: they display what they can and destroy nothing.
    """
    path = _registry_path(data_dir)
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("worktrees") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            raise ValueError("subagent worktree registry 'worktrees' is not a list")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        if strict:
            raise _refuse_corrupt_registry(data_dir, op, exc) from exc
        return []
    return [e for e in entries if isinstance(e, dict)]


def _refuse_corrupt_registry(
    data_dir: Optional[Any], op: str, exc: Exception,
) -> SubagentWorktreeRegistryCorrupt:
    """Disclose an unreadable registry durably, then refuse the mutation."""
    path = _registry_path(data_dir)
    log.error(
        "subagent worktree registry is corrupt; %s refused, bytes kept (%s)",
        op or "mutation", exc,
    )
    try:
        from ouroboros.utils import append_jsonl, utc_now_iso

        append_jsonl(
            _data_dir(data_dir) / "logs" / "events.jsonl",
            {"ts": utc_now_iso(), "type": "subagent_worktree_registry_corrupt",
             "op": str(op or ""), "registry": str(path), "error": str(exc)[:200]},
        )
    except Exception:
        log.debug("registry-corrupt event append failed", exc_info=True)
    return SubagentWorktreeRegistryCorrupt(str(exc))


def _save_registry(entries: List[Dict[str, Any]], data_dir: Optional[Any] = None) -> None:
    path = _registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"worktrees": entries}, trailing_newline=True)


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #
class WorktreeOpsLockBusy(TimeoutError):
    """The worktree ops lock stayed held for the whole wait.

    ``holder`` is what the holder wrote into the lock file (``pid``, ``task``,
    ``op``, ``since``, ``target``; ``{}`` when unreadable), so a refusal names who
    is doing what instead of a bare timeout."""

    def __init__(self, lock_path: Path, waited_sec: float, holder: Dict[str, str]):
        self.lock_path, self.waited_sec, self.holder = str(lock_path), waited_sec, holder
        who = (f" (held by pid {holder.get('pid')} task {holder.get('task') or '?'} op "
               f"{holder.get('op') or '?'} since {holder.get('since') or '?'})") if holder else ""
        super().__init__(f"subagent worktree ops lock timeout after {waited_sec:.0f}s: {lock_path}{who}")


def _lock_holder(lock_path: Path) -> Dict[str, str]:
    """Parse ``pid=… task=… op=… since=… target=…``; ``target`` is the tail of the
    line so a path with spaces survives. Readable while held: flock never blocks reads."""
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return {}
    head, sep, target = text.partition(" target=")
    holder = {key: value for key, eq, value in (field.partition("=") for field in head.split())
              if eq and key in ("pid", "task", "op", "since")}
    if sep:
        holder["target"] = target
    return holder


@contextlib.contextmanager
def _ops_lock(root: Path, *, op: str, task_id: str = "", target: str = "",
              timeout_sec: Optional[float] = None):
    """Serialize SHARED-METADATA mutations in-process (threading.Lock) and across
    processes via the portable file-lock SSOT (platform_layer).

    Held for milliseconds plus a registry read-modify-write — never for a tree
    walk (#1241). The holder names itself in the lock file, ``pid=`` first: the
    owner-aware stale check reads it, so a SIGKILLed holder is evicted at once
    instead of blacking out every waiter for ``_LOCK_STALE_SEC``; a live holder
    is never evicted (its kernel flock is probed). A timeout is typed with the
    holder's facts."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_NAME
    metadata = f"pid={os.getpid()} task={task_id or '-'} op={op} since={utc_now_iso()} target={target}"
    with _inproc_lock:
        started = time.monotonic()
        fd = acquire_exclusive_file_lock(
            lock_path, timeout_sec=_LOCK_TIMEOUT_SEC if timeout_sec is None else timeout_sec,
            stale_sec=_LOCK_STALE_SEC, metadata=metadata, owner_aware_stale=True)
        if fd is None:
            raise WorktreeOpsLockBusy(lock_path, time.monotonic() - started, _lock_holder(lock_path))
        try:
            yield
        finally:
            release_exclusive_file_lock(lock_path, fd)


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def _git(repo_dir: Path, *args: str, check: bool = True,
         env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        env=env,
    )


def _deletable(path: Path, root: Path) -> bool:
    """A checkout path this module may delete: non-empty, not a root spelling, and
    STRICTLY inside the worktree root (the root itself holds every live snapshot) —
    the registry is durable state and a malformed row must never name an arbitrary
    path. One guard for every delete this module performs."""
    text = str(path).strip()
    if not text or text in (".", "/", "//"):
        return False
    try:
        return path.resolve() != Path(root).resolve() and _is_within(path, root)
    except OSError:
        return False


def _git_quiet(repo_dir: Path, *args: str) -> None:
    """Best-effort git: a failing command or a vanished repo is not an error here."""
    try:
        _git(repo_dir, *args, check=False)
    except Exception:
        pass


def _remove_paths(repo_dir: Path, wt_path: Path, branch: str, *, allowed_root: Optional[Any] = None) -> bool:
    """Forget a deleted checkout's Git registration and branch under the ops lock.

    The caller deletes files OUTSIDE the lock first. A surviving directory returns
    False without retrying deletion here, so its row and pin stay (``removal_failed``).

    When ``allowed_root`` is given, refuse to touch any path that is empty or not
    strictly inside it. The registry is durable runtime state; a corrupt/malformed
    entry must never cause deletion of an arbitrary filesystem path.
    """
    wt_path = Path(wt_path)
    if allowed_root is not None and not _deletable(wt_path, Path(allowed_root)):
        return True  # not ours to delete: the malformed row is forgotten, the path untouched
    if wt_path.exists():
        return False
    _git_quiet(repo_dir, "worktree", "prune")
    if branch:
        _git_quiet(repo_dir, "branch", "-D", branch)
    return True


def _mark_removal_failed(match: Dict[str, Any], data_dir: Optional[Any], op: str) -> None:
    """Keep the first failure time and the surviving directory identity, under the lock."""
    rows = _load_registry(data_dir, strict=True, op=op)
    if match in rows:
        failed = worktree_removal.record_failure(match)
        if failed != match:
            rows[rows.index(match)] = failed
            _save_registry(rows, data_dir)


def _register_snapshot(handle: "ExecutionSnapshotHandle", excluded: List[Dict[str, Any]],
                       data_dir: Optional[Any]) -> None:
    """Registry read-modify-write (under the ops lock): upsert the snapshot's row by path."""
    record = asdict(handle)
    record["kind"] = _KIND_DELEGATED_EXEC
    record["excluded_untracked"] = list(excluded)
    entries = [e for e in _load_registry(data_dir, strict=True, op="register_execution_snapshot")
               if e.get("path") != handle.path]
    entries.append(record)
    _save_registry(entries, data_dir)


def _unregister_snapshot(snapshot_id: str, data_dir: Optional[Any], op: str) -> None:
    """Registry read-modify-write (under the ops lock): drop the snapshot's row."""
    survivors = [e for e in _load_registry(data_dir, strict=True, op=op) if not (
        e.get("kind") == _KIND_DELEGATED_EXEC and e.get("snapshot_id") == snapshot_id)]
    _save_registry(survivors, data_dir)


def _discard_snapshot_checkout(target: Path, wt_path: Path, ref: str, snapshot_id: str, *,
                               root: Path, data_dir: Optional[Any], task_id: str,
                               lock_wait_sec: Optional[float] = None) -> None:
    """Undo a Git execution snapshot once its row and pin may exist: the checkout's
    files go first, OUTSIDE the lock (a 1.8 GB tree takes minutes), then one short
    section forgets the admin dir, unpins the baseline and drops the row — the
    postcondition ``tests/test_snapshot_file_inputs.py`` asserts (no row, no ref,
    no ``dlg_*`` directory). Best-effort: custody GC reconciles crash leftovers
    at boot and each custody pass once the owning task is proven over.
    ``lock_wait_sec`` shortens the wait when the failure WAS a busy lock:
    the same holder is still there, and the typed refusal must not wait twice."""
    if _deletable(wt_path, root) and wt_path.exists():
        _force_rmtree(wt_path)
    try:
        with _ops_lock(root, op="discard", task_id=task_id, target=str(target), timeout_sec=lock_wait_sec):
            _git_quiet(target, "worktree", "prune")
            _git_quiet(target, "update-ref", "-d", ref)
            _unregister_snapshot(snapshot_id, data_dir, "discard_execution_snapshot")
    except Exception:
        log.warning("Failed to discard execution snapshot %s after a provisioning failure",
                    snapshot_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    path: str
    branch: str
    base_sha: str
    repo_dir: str
    created_at: float
    parent_task_id: str = ""
    file_baseline: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    untracked_baseline: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    source_is_system_repo: bool = True
    target_head: str = ""
    source_index_clean: bool = False
    git_dir: str = ""


def provision_worktree(
    *,
    repo_dir: Any,
    task_id: Any,
    base_sha: str = "",
    parent_task_id: str = "",
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    source_is_system_repo: bool = True,
) -> WorktreeHandle:
    """Give a child the source's current eligible tree, under task-owned GC.

    The branch pins the synthetic current-tree baseline, not just source HEAD.
    Unlike delegated execution snapshots, this checkout belongs to the child
    task and is removed by worktree retention, never by delegated-run custody.
    """
    target = Path(repo_dir).resolve()
    if base_sha:
        expected = _git(target, "rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
        if _git(target, "rev-parse", "HEAD").stdout.strip() != expected:
            raise ValueError("isolated child source HEAD differs from the requested base_sha")
    return _provision_git_copy(
        target_root=target, task_id=task_id, snapshot_id=f"task_{_safe_name(task_id)}",
        worktree_root=worktree_root, data_dir=data_dir,
        task_copy=True, parent_task_id=parent_task_id,
        source_is_system_repo=source_is_system_repo,
    )


def provision_genesis_project(
    *,
    repo_dir: Any,
    task_id: Any,
    parent_task_id: str = "",
    projects_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    dir_name: str = "",
) -> WorktreeHandle:
    """Provision a durable, isolated, EMPTY git project for a genesis acting child.

    Unlike a worktree this is a standalone repo (not a checkout of the live body)
    under the durable projects root. It is the deliverable itself and is NEVER
    GC-pruned, so it is intentionally not added to the worktree registry. The
    child builds the whole project here and returns a ``workspace.patch`` that is
    a diff from the empty initial commit (``base_sha``).

    ``dir_name`` names the genesis directory meaningfully (e.g. the project name,
    readable Unicode via ``project_folder_basename``) instead of the raw task id, so
    sibling builders share a recognizable project root; the handle's binding
    identity stays ``task_id`` (I, v6.39). Existing folders are never renamed.
    """
    from ouroboros.project_facts import project_folder_basename

    repo_dir = Path(repo_dir).resolve()
    root = Path(projects_root) if projects_root else Path(get_subagent_projects_root())
    root = root.expanduser().resolve()
    _assert_root_isolated(root, repo_dir, _data_dir(data_dir))
    safe_task = project_folder_basename(dir_name) or _safe_name(task_id)
    with _ops_lock(root, op="genesis", task_id=str(task_id or "")):
        # Genesis projects are durable: never clobber an existing entry. The candidate is
        # created EXCLUSIVELY before anything resolves it, so an existing directory, a
        # case/normalization alias, or a (dangling) symlink all count as taken -> "_N".
        root.mkdir(parents=True, exist_ok=True)
        _suffix = 0
        while True:
            proj = root / (f"{safe_task}_{_suffix}" if _suffix else safe_task)
            try:
                os.mkdir(proj)
                break
            except FileExistsError:
                _suffix += 1
        if proj.resolve().parent != root:
            raise RuntimeError(f"genesis project escaped its root: {proj}")
        proj = proj.resolve()
        try:
            _git(proj, "init")
            # A fresh repo may have no commit identity; set a local one for the seed
            # commit only (does not touch the user's global git config).
            _git(
                proj,
                "-c", "user.email=ouroboros@localhost",
                "-c", "user.name=Ouroboros",
                "commit", "--allow-empty", "-m", "genesis: empty project",
            )
            base_sha = _git(proj, "rev-parse", "HEAD").stdout.strip()
        except Exception:
            # Do not leak a partial/uninitialized project dir on git failure.
            _force_rmtree(proj)
            raise
        return WorktreeHandle(
            task_id=str(task_id),
            path=str(proj),
            branch="",
            base_sha=base_sha,
            repo_dir=str(proj),
            created_at=time.time(),
            parent_task_id=str(parent_task_id or ""),
        )


def remove_genesis_project(path: str, *, projects_root: Optional[Any] = None) -> bool:
    """Best-effort removal of a provisioned-but-unused genesis project.

    Only removes a path strictly INSIDE the configured projects root (never an
    arbitrary caller path). Used to clean up a genesis project whose schedule was
    rejected before the child ran; genesis projects are otherwise durable.
    """
    if not str(path or "").strip():
        return False
    root = Path(projects_root) if projects_root else Path(get_subagent_projects_root())
    root = root.expanduser().resolve()
    target = Path(path).resolve()
    if target == root or not _is_within(target, root):
        return False
    if target.exists():
        _force_rmtree(target)
    return True


@dataclass(frozen=True)
class ExecutionSnapshotHandle:
    """A private, disposable execution root for ONE mutating delegated run.

    The snapshot is a detached ``git worktree`` of the AUTHORITY TARGET tree,
    checked out at a synthetic BASELINE commit that captures the target's real
    current state: tracked + staged changes plus eligible untracked files.
    Large and binary inputs are copied beside the Git baseline with exact
    identities in ``file_baseline``; credential/junk exclusions stay shared
    with workspace-patch capture. The run writes ONLY here; its Git diff and
    file changes against these baselines form its contribution, and the nanny
    applies or rejects that result into the target explicitly — never automatically.
    """

    snapshot_id: str
    task_id: str
    path: str
    target_root: str
    baseline_ref: str
    baseline_sha: str
    baseline_tree: str
    manifest_digest: str
    target_head: str
    created_at: float
    entry_count: int = 0
    excluded_untracked: tuple = ()
    # Standalone payload snapshots (R1): the snapshot owns its OWN .git (the
    # target is a non-Git skill payload), so cleanup touches no target-repo
    # worktree/ref command. ``payload_hash`` is the pre-copy skill-loader
    # content hash — the whole-payload CAS baseline for the explicit apply.
    standalone: bool = False
    payload_hash: str = ""
    file_baseline: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    untracked_baseline: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    capture_warnings: tuple = ()
    # Wall-clock seconds the provision took; a disclosure on the start receipt and
    # the baseline manifest (a heavy tree is visible the moment it is snapshotted).
    provisioning_sec: float = 0.0
    git_dir: str = ""  # Stable shared Git metadata survives deletion of a source worktree.


def _git_env_index(index_path: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _git_env(repo_dir: Path, *args: str, env: Dict[str, str],
             check: bool = True, input_bytes: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        check=check,
        env=env,
        input=input_bytes if input_bytes else None,
    )


class _InPlaceFill:
    """A ``stream_artifact_file`` sink that rewrites an open file from its first differing byte."""

    def __init__(self, handle: Any):
        self.handle, self.size, self.differs = handle, 0, False

    def write(self, chunk: bytes) -> None:
        if not self.differs:
            if self.handle.read(len(chunk)) == chunk:
                self.size += len(chunk)
                return
            self.differs = True
            self.handle.seek(self.size)
        self.handle.write(chunk)
        self.size += len(chunk)


def _fill_private_copy(source: Path, checkout: Path, copy_root: Path) -> bool:
    """Give one file Git checked out into a not-yet-returned copy the source's exact bytes; True when
    checkout wrote it (its index row then carries checkout's stat).

    Nothing reads or runs in the copy before provisioning returns, and the
    whole-tree diff after population binds every byte to the baseline, so the
    per-file temporary file and rename of ``copy_artifact_file`` (the measured
    cost) buys nothing here. The same stable source read rewrites the file in
    place from its first differing byte, an equal file is not written, and
    ``copystat`` applies the source's mode, times and flags as before. An entry
    checkout did not write (outside an inherited sparse-checkout cone) is created
    by ``copy_artifact_file`` as before while the copy still has its ``.git``, so
    a removal that has reached ``.git`` re-creates nothing; entries a removal
    still walking the tree already deleted can be re-created until it gets
    there, and the failed start's discard removes them. A path checkout left as
    something other than a regular file is refused, never written through. No retention flow
    provisions copies, so skipping that helper's publication fence loses nothing.
    """
    from ouroboros.artifacts import copy_artifact_file, stream_artifact_file

    try:
        info = os.lstat(checkout)
    except FileNotFoundError:
        if not os.path.lexists(copy_root / ".git"):
            raise
        copy_artifact_file(source, checkout)
        return False
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"snapshot checkout is not a regular file: {checkout}")
    if info.st_mode & 0o600 != 0o600:  # an owner-unwritable umask; copystat restores the mode
        os.chmod(checkout, stat.S_IMODE(info.st_mode) | 0o600)
    with open(checkout, "r+b") as handle:
        sink = _InPlaceFill(handle)
        stream_artifact_file(source, sink)
        handle.truncate(sink.size)
    shutil.copystat(source, checkout)
    return True


def provision_execution_snapshot(
    *, target_root: Any, task_id: Any, snapshot_id: str,
    worktree_root: Optional[Any] = None, data_dir: Optional[Any] = None, independent: bool = False,
) -> ExecutionSnapshotHandle:
    """Provision a run-owned current-tree snapshot; custody owns its lifetime.

    ``independent`` (an evidence-only investigation, #1609) makes the execution
    root a private clone instead of a linked worktree, so the run's own Git
    operations never reach the target's refs, stash or config; the clone still
    compares working files as the target does (``_adopt_source_comparison``)."""
    return _provision_git_copy(
        target_root=target_root, task_id=task_id, snapshot_id=snapshot_id,
        worktree_root=worktree_root, data_dir=data_dir, independent=independent,
    )


def _provision_git_copy(
    *,
    target_root: Any,
    task_id: Any,
    snapshot_id: str,
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    task_copy: bool = False,
    parent_task_id: str = "",
    source_is_system_repo: bool = True,
    independent: bool = False,
) -> ExecutionSnapshotHandle | WorktreeHandle:
    """Snapshot ``target_root``'s REAL current tree into a private execution root.

    Baseline construction never touches the target's own index, HEAD or working
    files: a TEMPORARY index is seeded from HEAD and stages the eligible
    tracked/staged/text inputs (``.gitignore`` respected). The execution copy
    retains original regular-file bytes despite Git checkout filters; a Git
    comparison binds the copied content to that same baseline. Binary and large
    untracked inputs are streamed into the execution root outside Git's ODB,
    with exact preimages retained in the existing snapshot record. The Git tree
    is committed as a synthetic baseline pinned by a ref under
    ``refs/ouroboros/delegated/``. The execution root is a detached worktree at
    that commit, registered durably BEFORE the caller records any start intent,
    and removed only by an explicit disposition (``remove_execution_snapshot``)
    or by custody GC (boot and each custody pass), after the owning task is proven
    over and custody says the run is closed.

    The ops lock is held twice, briefly (#1241): once to register the row, pin
    the baseline and create the worktree's admin dir — row FIRST, so everything
    after it is nameable by custody GC — and once to finalize the row.
    Listing, classifying (one git process for every binary verdict), hashing,
    populating, copying and the one ``update-index`` that re-records the copied
    bytes' stat run OUTSIDE it, so a huge untracked inventory delays only its
    own task instead of refusing every other mutating start.
    """
    from ouroboros.workspace_patch_capture import (
        binary_verdict_candidates, untracked_binary_verdicts, untracked_capture_veto_reason)

    target = Path(target_root).resolve()
    if not (target / ".git").exists():
        raise ValueError(f"delegated execution snapshot target {target} is not a git working tree")
    root = _resolve_root(worktree_root)
    data_root = _data_dir(data_dir)
    if _is_within(root, data_root) or _is_within(data_root, root):
        raise ValueError(f"subagent worktree root {root} overlaps runtime data {data_root}")
    snap = str(snapshot_id or "").strip()
    if not snap:
        raise ValueError("snapshot_id is required for a delegated execution snapshot")
    safe_snap = _safe_name(snap)
    task = str(task_id or "")
    started = time.monotonic()
    name = _safe_name(task_id) if task_copy else f"dlg_{_safe_name(task_id)}_{safe_snap[:16]}"
    wt_path = (root / name).resolve()
    _assert_root_isolated(wt_path, target, data_root)
    branch = f"{_BRANCH_PREFIX}{_safe_name(task_id)}" if task_copy else ""
    baseline_ref = f"refs/heads/{branch}" if task_copy else f"{_BASELINE_REF_PREFIX}{safe_snap}"
    common = Path(_git(target, "rev-parse", "--git-common-dir").stdout.strip())
    git_dir = str((common if common.is_absolute() else target / common).resolve())
    # A malformed registry refuses HERE, before the tree is hashed (strict read).
    _load_registry(data_dir, strict=True, op="provision_execution_snapshot")
    root.mkdir(parents=True, exist_ok=True)
    # A stale checkout of the SAME snapshot id (a crashed earlier attempt) is plain
    # files; its admin dir and its pin are replaced under the lock below.
    if _deletable(wt_path, root) and wt_path.exists():
        _force_rmtree(wt_path)
    head_proc = _git(target, "rev-parse", "--verify", "HEAD", check=False)
    target_head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
    index_path = root / f".baseline_index_{safe_snap[:16]}_{os.getpid()}"
    env = _git_env_index(index_path)
    try:
        if target_head:
            _git_env(target, "read-tree", target_head, env=env)
        else:
            _git_env(target, "read-tree", "--empty", env=env)
        # ELIGIBILITY IS DECIDED BEFORE ANYTHING IS HASHED. `git add -A` writes a
        # blob for EVERY untracked file into the target's object database —
        # including `.env` / `credentials.json` — and removing the index entry
        # afterwards does not unwrite the object: the execution worktree shares
        # that ODB, so a vetoed secret stayed readable there by hash. The staged
        # set is therefore computed first (tracked/staged paths from the REAL
        # index, plus only the ELIGIBLE untracked ones) and fed to plumbing that
        # touches nothing else. NUL-delimited stdin: byte-safe and immune to argv
        # limits.
        real_env = dict(os.environ)
        indexed = [
            p for p in _git_env(target, "ls-files", "-z", env=real_env)
            .stdout.decode("utf-8", errors="surrogateescape").split("\0") if p
        ]
        if target_head:
            # A staged removal is absent from the real index but still in the
            # HEAD-seeded scratch index. Remove it there before eligible inputs
            # are staged; a recreated excluded file must not be hashed either.
            removed = _git_env(target, "diff", "--cached", "--no-renames", "--diff-filter=D",
                               "--name-only", "-z", target_head, "--", env=real_env).stdout
            if removed:
                _git_env(target, "update-index", "--force-remove", "-z", "--stdin", env=env, input_bytes=removed)
        from ouroboros.store import is_wal_index  # a WAL index is shared memory: never staged, read or copied
        tracked_indexes = [p for p in indexed if is_wal_index(target / p)]  # dropped as a staged removal is
        if tracked_indexes:
            _git_env(target, "update-index", "--force-remove", "-z", "--stdin", env=env,
                     input_bytes="\0".join(tracked_indexes).encode("utf-8", errors="surrogateescape") + b"\0")
        indexed = [p for p in indexed if p not in tracked_indexes]
        source_index_clean = bool(target_head and _git_env(
            target, "diff-index", "--cached", "--quiet", target_head, "--", env=real_env, check=False).returncode == 0)
        untracked_raw = _git_env(
            target, "ls-files", "-z", "--others", "--exclude-standard",
            env=real_env,
        ).stdout.decode("utf-8", errors="surrogateescape")
        untracked = [p for p in untracked_raw.split("\0") if p]
        excluded: List[Dict[str, Any]] = [{"path": p, "reason": "sqlite_wal_index"} for p in tracked_indexes]
        eligible: List[str] = []
        untracked_baseline: Dict[str, Dict[str, Any]] = {}
        file_inputs: List[str] = []
        capture_warnings: List[Dict[str, Any]] = []
        # git's binary verdict for the whole inventory in one process — two when
        # empty files need their attribute verdict — never one per file (#1241).
        binary_verdicts = untracked_binary_verdicts(
            target, binary_verdict_candidates(target, untracked), warnings=capture_warnings)
        from ouroboros.workspace_file_outputs import _side
        for rel in untracked:
            candidate = target / rel
            if candidate.is_dir() and not candidate.is_symlink():
                excluded.append({"path": rel, "reason": "nested_repository"})
                continue
            reference: List[str] = []
            reason = untracked_capture_veto_reason(
                target, rel, file_outputs=reference, warnings=capture_warnings, binary_verdicts=binary_verdicts)
            if reference:
                file_inputs.append(rel)
            elif reason:  # a WAL index is recorded by its code with no content baseline: shared memory, never read
                excluded.append({"path": rel, "reason": "sqlite_wal_index"} if is_wal_index(target / rel)
                                else {"path": rel, "reason": reason, "baseline": _side(target / rel)})
            else:
                eligible.append(rel)
                baseline = _side(target / rel)
                if baseline is not None:
                    untracked_baseline[rel] = baseline
        staged_paths = indexed + eligible
        if staged_paths:
            # `--add --remove` stages each named path's CURRENT worktree content
            # (and drops the entry when the file is gone), which is exactly what
            # `git add -A` did for these paths — without visiting any other file.
            _git_env(
                target, "update-index", "-z", "--add", "--remove", "--stdin", env=env,
                input_bytes=b"\0".join(
                    p.encode("utf-8", errors="surrogateescape") for p in staged_paths) + b"\0")
        tree_sha = _git_env(target, "write-tree", env=env).stdout.decode("utf-8").strip()
        manifest_raw = _git_env(target, "ls-tree", "-r", "-z", tree_sha,
                                env=dict(os.environ)).stdout
        import hashlib
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        entry_count = sum(1 for chunk in manifest_raw.split(b"\0") if chunk)
        commit_args = ["commit-tree", tree_sha, "-m",
                       f"ouroboros: delegated-run baseline {snap}"]
        if target_head:
            commit_args[2:2] = ["-p", target_head]
        baseline_sha = _git_env(
            target, *commit_args,
            env={**env,
                 "GIT_AUTHOR_NAME": "Ouroboros", "GIT_AUTHOR_EMAIL": "ouroboros@localhost",
                 "GIT_COMMITTER_NAME": "Ouroboros", "GIT_COMMITTER_EMAIL": "ouroboros@localhost"},
        ).stdout.decode("utf-8").strip()
        if task_copy and target_head and tree_sha == _git(target, "rev-parse", f"{target_head}^{{tree}}").stdout.strip():
            baseline_sha = target_head
    finally:
        try:
            index_path.unlink()
        except OSError:
            pass
    fields: Dict[str, Any] = dict(
        snapshot_id=snap, task_id=task, path=str(wt_path), target_root=str(target),
        baseline_ref=baseline_ref, baseline_sha=baseline_sha, baseline_tree=tree_sha,
        manifest_digest=manifest_digest, target_head=target_head, created_at=time.time(),
        entry_count=entry_count, excluded_untracked=tuple(excluded),
        untracked_baseline=untracked_baseline, capture_warnings=tuple(capture_warnings), git_dir=git_dir)
    registered = False  # nothing to discard until the row exists (a busy first section is a plain refusal)
    def record_copy(handle, exclusions):
        if not task_copy:
            _register_snapshot(handle, exclusions, data_dir)
            return handle
        owned = WorktreeHandle(
            task_id=task, path=str(wt_path), branch=branch, base_sha=baseline_sha,
            repo_dir=str(target), created_at=handle.created_at, parent_task_id=parent_task_id,
            file_baseline=handle.file_baseline, untracked_baseline=handle.untracked_baseline,
            source_is_system_repo=source_is_system_repo, target_head=target_head,
            source_index_clean=source_index_clean, git_dir=git_dir,
        )
        entries = [e for e in _load_registry(data_dir, strict=True, op="provision_worktree")
                   if e.get("path") != str(wt_path)]
        entries.append(asdict(owned))
        _save_registry(entries, data_dir)
        return owned

    try:
        with _ops_lock(root, op="provision", task_id=task, target=str(target)):
            # Row FIRST, then the pin, then the admin dir: a crash after any of these
            # leaves a REGISTERED snapshot custody never opened. Custody GC removes
            # it (checkout, ref, row) once its owning task is proven over.
            # A pin without a row would be invisible.
            # The provisional row is LIGHT (no per-file maps): the GC needs only
            # path, ref and snapshot id, and the maps are O(files) to serialize.
            _git_quiet(target, "worktree", "prune")
            record_copy(ExecutionSnapshotHandle(**{**fields, "untracked_baseline": {},
                                                    "excluded_untracked": ()}), [])
            registered = True
            _git(target, "update-ref", baseline_ref, baseline_sha)
            wt_path.parent.mkdir(parents=True, exist_ok=True)
            if task_copy:
                _git(target, "worktree", "add", "--no-checkout", "--force", str(wt_path), branch)
            elif independent:
                clone_private_checkout(Path(git_dir), wt_path, head=baseline_sha)
            else:
                _git(target, "worktree", "add", "--detach", "--no-checkout", str(wt_path), baseline_sha)
        if independent:
            _adopt_source_comparison(target, Path(git_dir), wt_path)
        # Populate outside the lock: the same reset git's own `worktree add` runs
        # (no submodule recursion) — minus the target's post-checkout hook.
        _git(wt_path, "reset", "--hard", "--quiet", "--no-recurse-submodules")
        # Git owns the baseline representation, but the child must see the
        # source's actual working bytes, not checkout's CRLF/smudge rewrite.
        # Read the existing tree inventory so deletions, links and gitlinks
        # keep Git's semantics and excluded paths can never enter the copy.
        copied: List[bytes] = []
        checked_out: List[bytes] = []
        for item in manifest_raw.split(b"\0"):
            metadata, separator, raw_path = item.partition(b"\t")
            if separator and metadata.split()[0] in (b"100644", b"100755"):
                relative = raw_path.decode("utf-8", errors="surrogateescape")
                original = target / relative
                if original.is_symlink():
                    raise OSError(f"snapshot input changed from a regular file: {relative}")
                if _fill_private_copy(original, wt_path / relative, wt_path):
                    checked_out.append(item)
                copied.append(raw_path)
        # The checkout recorded each entry's stat for the bytes IT wrote, and git trusts that stat: a
        # CRLF/smudge rewrite (core.autocrlf=true is Git for Windows' default) differs in size and would
        # read as modified for the run's whole life, and a same-size source edit since the baseline keeps
        # checkout's blob when its time matches to the second (copystat puts the source's mtime back; Git
        # for Windows' ctime is the creation time), so the diff below would miss it. Those rows go back to
        # the baseline without stat, and one update-index hashes every copied byte through the same clean
        # filters the baseline used (identical blob) and records its stat.
        if checked_out:
            _git_env(wt_path, "update-index", "-z", "--index-info", env=dict(os.environ),
                     input_bytes=b"\0".join(checked_out) + b"\0")
        if copied:
            _git_env(wt_path, "update-index", "-z", "--stdin", env=dict(os.environ),
                     input_bytes=b"\0".join(copied) + b"\0")
        # A concurrent source edit must not appear as the child's work.
        # Use the same Git representation as ordinary patch capture, once
        # for the whole tree, before the separately tracked file inputs.
        _git(wt_path, "diff", "--quiet", "--no-ext-diff", baseline_sha, "--")
        from ouroboros.workspace_file_outputs import copy_snapshot_file_inputs
        file_baseline = copy_snapshot_file_inputs(target, wt_path, file_inputs)
        if file_baseline:
            manifest_digest = hashlib.sha256(
                manifest_raw + json.dumps(file_baseline, sort_keys=True).encode("utf-8")
            ).hexdigest()
            entry_count += len(file_baseline)
        handle = ExecutionSnapshotHandle(**{
            **fields, "manifest_digest": manifest_digest, "entry_count": entry_count,
            "file_baseline": file_baseline, "provisioning_sec": round(time.monotonic() - started, 3)})
        with _ops_lock(root, op="provision", task_id=task, target=str(target)):
            handle = record_copy(handle, excluded)
    except Exception as exc:
        if registered:
            if task_copy:
                try:
                    remove_worktree(path=str(wt_path), worktree_root=root, data_dir=data_dir,
                                    lock_wait_sec=5.0 if isinstance(exc, WorktreeOpsLockBusy) else None)
                except Exception:
                    log.warning("Failed to discard task copy %s after provisioning failure", task, exc_info=True)
            else:
                _discard_snapshot_checkout(Path(git_dir), wt_path, baseline_ref, snap, root=root, data_dir=data_dir, task_id=task,
                                           lock_wait_sec=5.0 if isinstance(exc, WorktreeOpsLockBusy) else None)
        raise
    return handle


def isolated_git_env() -> Dict[str, str]:
    """A git environment no host or child configuration can shape.

    Parent-side git over a payload snapshot must never consult the system or
    the user's global config (external diff drivers, textconv, excludes,
    hooks templates): `GIT_CONFIG_NOSYSTEM` plus a `/dev/null` global leave
    only command-line `-c` overrides and the repo-local config — which the
    payload paths reset to a known-good baseline before trusting.
    """
    env = dict(os.environ)
    # Repository-location vars inherited from the HOST process would silently
    # redirect every command here at another repo/index (observed with a leaked
    # GIT_DIR: `git init` "reinitialized" a foreign directory).
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR"):
        env.pop(var, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def clone_private_checkout(source: Path, dest: Path, *, head: str = "") -> None:
    """An INDEPENDENT private clone of ``source`` at ``dest``, with nothing checked out.

    A linked worktree shares its repository's refs, stash, config and hooks, so an
    ordinary ``git stash``/``branch``/``tag``/``config`` run inside it lands in the
    author's repository. This clone borrows the source's object store read-only
    (``--shared`` alternates; objects it writes stay its own) and owns everything
    else. Its ``origin`` remote is removed, so no push has a default target.
    ``head`` becomes its detached HEAD; the caller populates index and worktree.
    """
    env = isolated_git_env()
    dest.parent.mkdir(parents=True, exist_ok=True)
    for args in (["clone", "--shared", "--no-checkout", "--quiet", str(source), str(dest)],
                 ["-C", str(dest), "remote", "remove", "origin"],
                 *([["-C", str(dest), "update-ref", "--no-deref", "HEAD", head]] if head else [])):
        subprocess.run(["git", *args], capture_output=True, check=True, env=env, timeout=600)


# What git consults, besides the tree's own .gitattributes, to convert and
# compare working files: line endings and executable-bit trust.
_COMPARISON_CONFIG = r"^core\.(autocrlf|eol|filemode|attributesfile)$"


def _adopt_source_comparison(target: Path, git_dir: Path, clone: Path) -> None:
    """Make an independent clone convert and compare working files as its source does.

    The baseline was hashed through the source's EFFECTIVE conversion; the
    copied bytes are re-hashed in the clone (the provisioning check, the child's
    own Git, the capture), which sees only the system/global scope of the config
    a linked worktree would share. So the source's effective values of these
    keys that the clone does not already see, and its ``info/attributes``, are
    written into the clone. Data only: no filter, driver, hook or other
    executable setting is transferred, so a baseline that depends on one still
    refuses at the provisioning check.
    """
    def effective(repo: Path) -> Dict[str, str]:
        listed = _git(repo, "config", "-z", "--get-regexp", _COMPARISON_CONFIG, check=False).stdout
        # Later scopes win; a valueless entry is a boolean true.
        return {key: value if newline else "true"
                for key, newline, value in (item.partition("\n") for item in listed.split("\0") if item)}

    inherited = effective(clone)
    for key, value in effective(target).items():
        if key == "core.attributesfile":
            # Keep this lower-precedence input separate from info/attributes.
            # A relative path may name ignored data absent from the clone.
            source_path = _git(target, "config", "-z", "--path", "--get", key).stdout.rstrip("\0")
            source_file = target / source_path
            if source_file.is_file():
                private_file = clone / ".git" / "info" / "source-attributes"
                private_file.parent.mkdir(exist_ok=True)
                private_file.write_bytes(source_file.read_bytes())
                value = str(private_file)
        if inherited.get(key) != value:
            _git(clone, "config", "--local", key, value)
    attributes = git_dir / "info" / "attributes"
    if attributes.is_file():
        (clone / ".git" / "info").mkdir(exist_ok=True)
        (clone / ".git" / "info" / "attributes").write_bytes(attributes.read_bytes())


def payload_git_metadata_refusal(exec_root: Path) -> str:
    """lstat-trust check on a child-writable snapshot's Git metadata, or "".

    The child held a shell inside the snapshot: a ``.git`` (or ``.git/config``,
    ``.git/objects``) replaced by a symlink would make the parent's capture
    read — or a naive fix WRITE — through a child-chosen path outside the
    snapshot. Checked with lstat semantics BEFORE any parent git operation;
    a refusal is a typed capture failure and touches nothing.
    """
    git_dir = Path(exec_root) / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return ".git is not a real directory (symlinked or replaced by the run)"
    config = git_dir / "config"
    if os.path.lexists(config) and (config.is_symlink() or not config.is_file()):
        return ".git/config is not a regular file (symlinked or replaced by the run)"
    objects = git_dir / "objects"
    if objects.is_symlink() or not objects.is_dir():
        return ".git/objects is not a real directory (symlinked or replaced by the run)"
    return ""


class _RawBlobWriter:
    """ONE ``git fast-import`` storing the raw blobs of one staging call.

    A Git process per blob would cost a process start per file. Each blob is
    framed by its exact length (``data <n>``), so payload bytes are never parsed
    as commands, and fast-import opens no path and applies no attribute, filter
    or eol conversion. Each id is read back (``get-mark``) before the next blob
    is sent and stderr goes to a file, so no unread pipe can fill and stall the
    writer. The ids name stored objects only after the explicit ``done``
    (``--done``) and exit 0 (``finish``); any other end kills the process.
    By default fast-import holds a blob, its predecessor and a delta of them in
    memory; ``core.bigFileThreshold`` streams every blob over 1 MiB (the
    ``--big-file-threshold`` option reaches only the first blob in Git 2.53),
    and the written pack is kept rather than exploded into loose objects by a
    second process; large payloads need not stay in Git's delta search.
    """

    def __init__(self, root: Path, env: Dict[str, str]):
        self._root, self._env, self._errors = root, env, None
        self._proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "_RawBlobWriter":
        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.kill()  # it never acknowledged ``done``: nothing it wrote is staged
            self._proc.wait()
            for pipe in (self._proc.stdin, self._proc.stdout):
                with contextlib.suppress(OSError):
                    pipe.close()
        if self._errors is not None:
            self._errors.close()

    def _failure(self, detail: bytes) -> subprocess.CalledProcessError:
        if self._proc.poll() is None:
            self._proc.kill()
        self._errors.seek(0)
        return subprocess.CalledProcessError(
            self._proc.wait(), self._proc.args, detail, self._errors.read() or detail)

    def write(self, blob: bytes) -> str:
        """Store one blob; its object id."""
        if self._proc is None:
            import tempfile

            self._errors = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                ["git", "-c", "core.bigFileThreshold=1m", "-c", "fastimport.unpackLimit=0",
                 "fast-import", "--quiet", "--done"], cwd=str(self._root), env=self._env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._errors)
        try:
            self._proc.stdin.write(b"blob\nmark :1\ndata %d\n" % len(blob))
            self._proc.stdin.write(blob)
            self._proc.stdin.write(b"\nget-mark :1\n")
            self._proc.stdin.flush()
        except OSError as exc:  # the process is gone: its status and stderr say why
            raise self._failure(str(exc).encode()) from exc
        answer = self._proc.stdout.readline().strip()
        if not re.fullmatch(rb"[0-9a-f]{40}|[0-9a-f]{64}", answer):
            raise self._failure(b"no object id from fast-import: " + answer[:80])
        return answer.decode("ascii")

    def finish(self) -> None:
        """End the stream; raises unless every written blob is now a stored object."""
        if self._proc is None:
            return
        try:
            self._proc.stdin.write(b"done\n")
            self._proc.stdin.close()
        except OSError as exc:
            raise self._failure(str(exc).encode()) from exc
        trailing = self._proc.stdout.read()
        if self._proc.wait() != 0 or trailing:
            raise self._failure(trailing[:200])


class RawStagingIncomplete(subprocess.CalledProcessError):
    """``update-index --index-info`` exited 0, but the index lacks records as sent."""

    def __str__(self) -> str:
        return self.stderr.decode("utf-8", "replace")


def stage_raw_payload_inventory(
    worktree: Path, rel_paths: Any, env: Dict[str, str],
    baseline_modes: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Stage exact RAW bytes into the CURRENT index — no .gitattributes, no filters.

    ``git add``/worktree ``update-index`` run clean/eol filters from a child- or
    payload-authored ``.gitattributes`` (CRLF staged as LF — reproduced by
    review), so each file's raw bytes are read here, one file at a time, stored
    by ONE ``git fast-import`` for the whole inventory (``_RawBlobWriter``) and
    staged via ``--index-info``, both in the caller's ``env`` (its index, object
    database and config); ``.gitattributes`` is just content, a symlink stages
    as 120000 over its raw link-target bytes. Modes:
    with ``baseline_modes`` (capture) regular files pin to the baseline mode for
    existing paths / 100644 for new ones so an executable-bit flip can never
    ride a patch; without it (provisioning/reviewer capture) on-disk modes ride.
    ``--index-info`` exits 0 even when it skips a path Git refuses (``..``, a
    ``.git`` component, a Windows device name), so each record is sent in its
    ``ls-files --stage`` shape and ONE such read must list it back verbatim.
    Returns the paths whose on-disk executable bit diverges from the staged mode.
    """
    root = Path(worktree)
    lines: List[bytes] = []
    divergent: List[str] = []
    with _RawBlobWriter(root, env) as writer:
        for rel in sorted(str(p) for p in rel_paths):
            path = root / rel
            if path.is_symlink():
                blob, mode = os.readlink(os.fsencode(str(path))), "120000"
            else:
                blob = path.read_bytes()
                executable = bool(path.stat().st_mode & 0o111)
                if baseline_modes is None:
                    mode = "100755" if executable else "100644"
                else:
                    base = baseline_modes.get(rel, "")
                    mode = base if base in ("100644", "100755") else "100644"
                    if executable != (mode == "100755"):
                        divergent.append(rel)
            sha = writer.write(blob)
            lines.append(f"{mode} {sha} 0\t{rel}".encode("utf-8", errors="surrogateescape"))
        writer.finish()
    update = subprocess.run(["git", "update-index", "-z", "--index-info"], cwd=str(root), capture_output=True,
                            env=env, input=b"\0".join(lines) + b"\0" if lines else b"", check=True)
    listed = set(_git_env(root, "ls-files", "--stage", "-z", env=env).stdout.split(b"\0"))
    unstaged = [line.partition(b"\t")[2] for line in lines if line not in listed]
    if unstaged:  # names at most five; Git's own stderr is carried as evidence, never parsed
        detail = b"update-index exited 0, but the index lacks %d of %d records as sent (%s); git: %s" % (
            len(unstaged), len(lines), b", ".join(unstaged[:5]), update.stderr.strip()[:300])
        raise RawStagingIncomplete(update.returncode, update.args, update.stdout, detail)
    return divergent


@contextlib.contextmanager
def payload_capture_git_env(exec_root: Path):
    """A PARENT-OWNED throwaway Git control dir over the child-writable snapshot.

    Yields a git env whose GIT_DIR, config, hooks template and GIT_INDEX_FILE
    all live in a host-managed temp directory (mkdtemp, 0700; the index file is
    pre-created 0600) — never inside the snapshot the child could write. The
    child's object database is attached READ-ONLY via the alternates mechanism
    (objects are content-addressed, so the recorded baseline commit/tree can be
    read but not silently substituted), while new blobs staged by the capture
    land in the parent-owned control ODB. Child ``.git/index`` and
    ``.git/config`` are never read and never written: an index-only blob forged
    by the child simply does not exist for this environment, and no
    child-controlled diff driver / filter / hook can execute in the parent.
    """
    import tempfile

    resolved = Path(exec_root).resolve()
    control = Path(tempfile.mkdtemp(prefix="obo-payload-capture-"))
    try:
        env = isolated_git_env()
        subprocess.run(["git", "init", "--template="], cwd=str(control),
                       capture_output=True, check=True, env=env)
        alternates = control / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        # Byte-exact: text mode would emit CRLF on Windows -> git seeks "objects\r".
        alternates.write_bytes((str(resolved / ".git" / "objects") + "\n").encode("utf-8"))
        index = control / "index"
        os.close(os.open(index, os.O_CREAT | os.O_WRONLY, 0o600))
        env["GIT_DIR"] = str(control / ".git")
        env["GIT_WORK_TREE"] = str(resolved)
        env["GIT_INDEX_FILE"] = str(index)
        yield env
    finally:
        _force_rmtree(control)  # the capture's git objects are read-only; a plain rmtree leaves them on Windows


def _reproduce_confined_link(target_root: Path, src_link: Path, dest_link: Path) -> bool:
    """Reproduce one payload symlink in the snapshot, or refuse an escape (R1 item 4).

    A confined RELATIVE link is copied verbatim (``copytree(symlinks=True)``
    semantics); a confined ABSOLUTE link is rewritten to the equivalent relative
    link so the snapshot stays self-contained; a link resolving outside the
    payload is NOT copied (it is already outside the loader inventory).
    """
    try:
        raw = os.readlink(src_link)
        resolved = src_link.resolve(strict=False)
        rel_target = resolved.relative_to(target_root)
    except (OSError, ValueError):
        return False
    if os.path.isabs(raw):
        raw = os.path.relpath(target_root / rel_target, src_link.parent)
    dest_link.parent.mkdir(parents=True, exist_ok=True)
    if not os.path.lexists(dest_link):
        os.symlink(raw, dest_link)
    return True


def _copy_payload_inventory(target: Path, dest: Path, *, manifest_entry: str = "",
                            manifest_scripts: Optional[List[Dict[str, Any]]] = None,
                            preserve_links: bool = True) -> int:
    """Copy the exact skill-loader-visible inventory of ``target`` into ``dest``.

    The loader inventory is the SSOT walk (cache/control-dir exclusions, symlink
    escape exclusion, credential-shape refusal) — no second filesystem walk is
    invented; a declared manifest entry or script joins it even under an excluded
    folder, exactly as it joins the content hash. A file reached through a
    symlinked ancestor directory reproduces the ancestor LINK once instead of
    materializing a second copy under it. Read-only review views set
    ``preserve_links=False`` to retain each inventory path's bytes even when a
    link points into an excluded dependency folder; they copy no extra files.
    """
    from ouroboros.skill_loader import _iter_payload_files

    resolved_target = target.resolve()
    dest.mkdir(parents=True, exist_ok=False)
    copied = 0
    for path in _iter_payload_files(resolved_target, manifest_entry=manifest_entry,
                                    manifest_scripts=manifest_scripts):
        rel = path.relative_to(resolved_target)
        # A symlinked ANCESTOR directory is reproduced as the link itself; the
        # linked content is copied at its real (confined) location by its own
        # inventory entry, exactly like copytree(symlinks=True).
        ancestor = resolved_target
        via_link = False
        for part in rel.parts[:-1]:
            ancestor = ancestor / part
            if preserve_links and ancestor.is_symlink():
                link_rel = ancestor.relative_to(resolved_target)
                if _reproduce_confined_link(resolved_target, ancestor, dest / link_rel):
                    copied += 1
                via_link = True
                break
        if via_link:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if preserve_links and path.is_symlink():
            if _reproduce_confined_link(resolved_target, path, out):
                copied += 1
            continue
        shutil.copy2(path, out)
        copied += 1
    return copied


def provision_payload_snapshot(
    *,
    target_root: Any,
    task_id: Any,
    snapshot_id: str,
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
) -> ExecutionSnapshotHandle:
    """Snapshot ONE non-Git skill payload into a private STANDALONE Git repo (R1 §9.2).

    The live payload is NEVER initialized as Git and never touched: the
    loader-visible inventory is copied into a private directory under the
    delegated snapshot root (outside repo/data), Git is initialized only THERE,
    and the copied payload becomes the synthetic baseline commit. The pre-copy
    skill-loader content hash is recomputed after the copy — a changed hash
    means the snapshot raced another writer, so it is removed and the run must
    not start. Registered durably (``standalone=true``) BEFORE any start intent.
    """
    from ouroboros.tools.delegate_integration import payload_content_hash

    target = Path(target_root).resolve()
    if not target.is_dir():
        raise ValueError(f"skill payload {target} does not exist")
    if (target / ".git").exists():
        raise ValueError(
            f"skill payload {target} unexpectedly contains .git; refusing to snapshot")
    root = _resolve_root(worktree_root)
    if _is_within(root, target) or _is_within(target, root):
        raise ValueError(f"subagent worktree root {root} overlaps the snapshot target {target}")
    # The TARGET legitimately lives inside runtime data (data/skills/...), but
    # the snapshot ROOT itself must stay outside BOTH the system repo and the
    # runtime data root — a snapshot under data/ would put a child-writable
    # Git repo inside live state (reproduced by review).
    _assert_root_isolated(root, Path(__file__).resolve().parents[1], _data_dir(data_dir))
    snap = str(snapshot_id or "").strip()
    if not snap:
        raise ValueError("snapshot_id is required for a delegated payload snapshot")
    safe_snap = _safe_name(snap)
    task = str(task_id or "")
    started = time.monotonic()
    wt_path = (root / f"dlgp_{_safe_name(task_id)}_{safe_snap[:16]}").resolve()
    _load_registry(data_dir, strict=True, op="provision_payload_snapshot")  # refuse before the copy
    root.mkdir(parents=True, exist_ok=True)
    if _deletable(wt_path, root) and wt_path.exists():
        _force_rmtree(wt_path)  # idempotent re-provision of the SAME snapshot id
    source_hash = payload_content_hash(target)
    env = isolated_git_env()
    try:
        # Copy, init, stage and commit run OUTSIDE the ops lock (#1241): the
        # standalone snapshot's .git is private, so its only shared metadata is
        # the registry row written under the lock at the end.
        entry_count = _copy_payload_inventory(target, wt_path)
        # Fully config-isolated baseline: empty template dir (no user hooks),
        # no global/system config (Fable F1). RAW staging instead of `git
        # add` (Sol P1 modes/filters): a payload .gitattributes (eol/clean)
        # must not normalize the recorded baseline away from the live raw
        # bytes, and the baseline records the REAL file modes.
        _git(wt_path, "init", "--template=", env=env)
        from ouroboros.skill_loader import _iter_payload_files

        stage_raw_payload_inventory(
            wt_path,
            (p.relative_to(wt_path).as_posix() for p in _iter_payload_files(wt_path)),
            env)
        _git(
            wt_path,
            "-c", "user.email=ouroboros@localhost", "-c", "user.name=Ouroboros",
            "commit", "--allow-empty", "-m",
            f"ouroboros: delegated payload baseline {snap}", env=env,
        )
        baseline_sha = _git(wt_path, "rev-parse", "HEAD", env=env).stdout.strip()
        baseline_tree = _git(wt_path, "rev-parse", "HEAD^{tree}", env=env).stdout.strip()
        manifest_raw = _git(wt_path, "ls-tree", "-r", "-z", baseline_tree, env=env).stdout
        import hashlib

        manifest_digest = hashlib.sha256(
            manifest_raw.encode("utf-8", errors="surrogateescape")).hexdigest()
        if payload_content_hash(target) != source_hash:
            raise RuntimeError(
                "the live payload changed while it was being snapshotted "
                "(another writer raced the copy); retry the delegation")
        handle = ExecutionSnapshotHandle(
            snapshot_id=snap,
            task_id=task,
            path=str(wt_path),
            target_root=str(target),
            baseline_ref="",
            baseline_sha=baseline_sha,
            baseline_tree=baseline_tree,
            manifest_digest=manifest_digest,
            target_head="",
            created_at=time.time(),
            entry_count=entry_count,
            standalone=True,
            payload_hash=source_hash,
            provisioning_sec=round(time.monotonic() - started, 3),
        )
        # Registry write INSIDE the cleanup scope: an unregistered snapshot
        # directory would be invisible to disposal/retention (orphan leak).
        with _ops_lock(root, op="provision_payload", task_id=task, target=str(target)):
            _register_snapshot(handle, [], data_dir)
    except Exception:
        _force_rmtree(wt_path)
        raise
    return handle


def find_execution_snapshot(snapshot_id: str, data_dir: Optional[Any] = None) -> Optional[Dict[str, Any]]:
    """The registry record for a delegated execution snapshot, or None."""
    snap = str(snapshot_id or "").strip()
    if not snap:
        return None
    return next((entry for entry in _load_registry(data_dir, strict=True, op="find_execution_snapshot")
                 if entry.get("kind") == _KIND_DELEGATED_EXEC and entry.get("snapshot_id") == snap), None)


def remove_execution_snapshot(
    snapshot_id: str,
    *,
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
) -> bool:
    """Remove a released snapshot, its baseline pin and registry row.
    Call only after explicit disposition or GC's ownership/custody proof.
    A surviving tree keeps its row/pin; unchanged failures wait until a new process.
    """
    entry = find_execution_snapshot(snapshot_id, data_dir)
    if entry is None or worktree_removal.unchanged(entry):
        return False
    root = _resolve_root(worktree_root)
    wt_path = Path(str(entry.get("path") or ""))
    # The checkout's files go first, OUTSIDE the lock (#1241: a 1.8 GB snapshot
    # took minutes to delete and timed every other mutating start out).
    if _deletable(wt_path, root) and wt_path.exists():
        _force_rmtree(wt_path)
    with _ops_lock(root, op="remove", task_id=str(entry.get("task_id") or ""),
                   target=str(entry.get("target_root") or "")):
        if _deletable(wt_path, root) and wt_path.exists():
            # The directory survived its removal: the row and the baseline pin stay for the next pass.
            _mark_removal_failed(entry, data_dir, "remove_execution_snapshot")
            return False
        if not entry.get("standalone"):
            # A standalone payload snapshot (R1 §10.4) keeps its .git INSIDE the
            # directory just deleted; a Git snapshot also owns an admin dir and a
            # baseline pin in the TARGET repository.
            target = Path(str(entry.get("git_dir") or entry.get("target_root") or "."))
            _git_quiet(target, "worktree", "prune")
            ref = str(entry.get("baseline_ref") or "")
            if ref.startswith(_BASELINE_REF_PREFIX):
                _git_quiet(target, "update-ref", "-d", ref)
        _unregister_snapshot(str(entry.get("snapshot_id") or ""), data_dir, "remove_execution_snapshot")
    worktree_removal.forget(entry)
    return True


def _task_proven_over(task_id: str, data_dir: Any, live: Any) -> bool:
    """C10: terminal record, a negative supervisor probe, and no lineage process."""
    from ouroboros import body_candidate
    try:
        return bool(task_id) and live is not None and body_candidate._recorded_terminal(task_id, data_dir) \
            and live(task_id) is False and not body_candidate.live_lineage_processes(data_dir, task_id)
    except Exception:
        return False  # unreadable or unknown ownership never authorizes GC


def snapshot_released(entry: Dict[str, Any], open_snapshot_ids: set, *, data_dir: Any = None, live: Any = None) -> bool:
    """The one proof a delegated snapshot goes by, for the custody GC and the owner's free action
    alike (its preview offers exactly what this answers): the keep-set does not name it, its task is
    proven over (``live``: the supervisor's probe unless given), fresh readable custody does not hold it."""
    from ouroboros import delegate_custody as custody

    if live is None:
        from supervisor.queue import task_settlement_liveness as live
    snap = str(entry.get("snapshot_id") or "")
    try:
        return snap not in open_snapshot_ids and _task_proven_over(str(entry.get("task_id") or ""), data_dir, live) \
            and not custody.custody_log_unreadable(_data_dir(data_dir)) \
            and snap not in custody.open_snapshot_ids(_data_dir(data_dir))
    except Exception:
        return False


def prune_execution_snapshots(
    open_snapshot_ids: set,
    *,
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    only: Optional[Any] = None,
    live: Optional[Any] = None,
) -> Dict[str, Any]:
    """Retire snapshots only after their task is proven over and custody releases them
    (``snapshot_released``, ``live`` as there; the keep-set is conservative, fresh custody is checked
    before each removal). A provisional snapshot can precede START_REQUESTED, so absent custody is not
    proof its owner is over; unknown task/process/custody state keeps even a missing checkout's row
    and pin. ``only`` restricts the pass; surviving trees keep ``removal_failed``."""
    open_ids = {str(s) for s in (open_snapshot_ids or set())}
    chosen = None if only is None else {str(s) for s in only}
    removed: List[str] = []
    kept: List[str] = []
    for entry in list(_load_registry(data_dir, strict=True, op="prune_execution_snapshots")):
        if entry.get("kind") != _KIND_DELEGATED_EXEC:
            continue
        snap = str(entry.get("snapshot_id") or "")
        if chosen is not None and snap not in chosen:
            continue
        if not snapshot_released(entry, open_ids, data_dir=data_dir, live=live):
            kept.append(snap)
            continue
        (removed if remove_execution_snapshot(snap, worktree_root=worktree_root, data_dir=data_dir) else kept).append(snap)
    return {"removed": removed, "kept": kept}


def remove_worktree(
    *,
    task_id: str = "",
    path: str = "",
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    lock_wait_sec: Optional[float] = None,
) -> bool:
    """Tear down a worktree by task_id or path; unregister it. Returns success."""
    want_path = str(Path(path).resolve()) if path else ""
    entries = _load_registry(data_dir, strict=True, op="remove_worktree")
    match: Optional[Dict[str, Any]] = None
    for entry in entries:
        if entry.get("kind") in (_KIND_DELEGATED_EXEC, _KIND_BODY_CANDIDATE):
            continue
        if (task_id and entry.get("task_id") == str(task_id)) or (want_path and entry.get("path") == want_path):
            match = entry
            break
    root = _resolve_root(worktree_root)
    if match is None:
        # Unregistered path: best-effort directory removal, but ONLY inside the
        # configured worktree root (never an arbitrary path supplied by a caller);
        # no shared metadata is involved, so no lock.
        if want_path and Path(want_path).exists() and _is_within(Path(want_path), root):
            _force_rmtree(Path(want_path))
            return True
        return False
    return _retire_worktree(match, root, data_dir, match.get("branch") or "", lock_wait_sec)


def _retire_worktree(match: Dict[str, Any], root: Path, data_dir: Any, branch: str,
                     lock_wait_sec: Optional[float] = None) -> bool:
    """Delete files without the shared lock, then reconcile only the unchanged row."""
    if worktree_removal.unchanged(match):
        return False
    wt_path = Path(match.get("path") or "")
    if _deletable(wt_path, root) and wt_path.exists():
        _force_rmtree(wt_path)  # the checkout's files go first, OUTSIDE the lock (#1241)
    from ouroboros import body_candidate
    body_candidate.discard_scratch(match)  # an own-body copy's sibling process environment, unless retained
    with _ops_lock(root, op="remove_worktree", task_id=str(match.get("task_id") or ""), timeout_sec=lock_wait_sec):
        rows = _load_registry(data_dir, strict=True, op="remove_worktree")
        if match not in rows:  # another writer changed ownership while files were being removed
            return False
        if not _remove_paths(Path(match.get("git_dir") or match.get("repo_dir") or "."), wt_path,
                             branch, allowed_root=root):
            _mark_removal_failed(match, data_dir, "remove_worktree")  # the directory survived: row and pin stay
            return False
        rows.remove(match)
        _save_registry(rows, data_dir)
    worktree_removal.forget(match)
    return True


def prune_orphans(
    *,
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    retention_days: Optional[int] = None,
    live: Optional[Any] = None,
    only: Optional[Any] = None,
) -> Dict[str, Any]:
    """Reconcile worktrees on the custody cadence and the owner's free action.

    A plain copy missing or past retention goes only with proof its task is over: its row is
    terminal, ``live(task_id)`` (the supervisor's liveness probe: True live, False proven
    not live, None unknown) answers False, and no lineage process lives. Without a probe,
    or with an unknown answer, age alone deletes nothing. ``only`` restricts the pass to
    those task ids (the free action's previewed rows), leaving every other row untouched.
    The registry is written only when a row set or a row changed; a checkout that
    survives removal keeps its first failure stamp and pin; a new process or directory identity retries it.
    """
    retention = retention_days if retention_days is not None else get_gc_retention_days()
    cutoff = age_cutoff(retention)
    root = _resolve_root(worktree_root)
    chosen = None if only is None else {str(task_id) for task_id in only}
    entries = _load_registry(data_dir, strict=True, op="prune_orphans")
    selected = [entry for entry in entries if entry.get("kind") != _KIND_DELEGATED_EXEC
                and (chosen is None or str(entry.get("task_id") or "") in chosen)
                and not worktree_removal.unchanged(entry)]
    if not selected:
        return {"removed": 0, "kept": len(entries)}
    # Capture candidate evidence outside the lock; one decision batch then rechecks
    # the selected rows and marks retiring candidates before any files are deleted.
    from ouroboros import body_candidate
    verdicts = {
        str(entry.get("path") or ""): body_candidate.retention_verdict(
            entry, data_dir=data_dir,
            expired=max(float(entry.get("created_at") or 0), float(entry.get("used_at") or 0)) < cutoff)
        for entry in selected if entry.get("kind") == _KIND_BODY_CANDIDATE}
    retiring = []
    with _ops_lock(root, op="prune"):
        rows = _load_registry(data_dir, strict=True, op="prune_orphans")
        by_path = {str(row.get("path") or ""): row for row in rows}
        changed = False
        for preview in selected:
            entry = by_path.get(str(preview.get("path") or ""))
            if entry != preview:
                continue
            branch = str(entry.get("branch") or "")
            if entry.get("kind") == _KIND_BODY_CANDIDATE:
                verdict = verdicts[str(entry.get("path") or "")]
                if not verdict["remove"] or not body_candidate.verdict_holds(entry, verdict, data_dir=data_dir):
                    continue
                if not entry.get("retiring"):
                    entry["retiring"], changed = True, True
                if verdict.get("keep_branch"):
                    branch = ""
            else:
                path_exists = bool(entry.get("path")) and Path(entry["path"]).exists()
                if (path_exists and float(entry.get("created_at") or 0) >= cutoff) \
                        or not _task_proven_over(str(entry.get("task_id") or ""), data_dir, live):
                    continue
            retiring.append((entry, branch))
        if changed:
            _save_registry(rows, data_dir)
    removed = sum(_retire_worktree(entry, root, data_dir, branch) for entry, branch in retiring)
    return {"removed": removed, "kept": len(_load_registry(data_dir, strict=True, op="prune_orphans"))
            if retiring else len(rows)}


def list_worktrees(data_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return registered worktree records (for UI / inspection)."""
    return _load_registry(data_dir)


def registered_checkout(path: Any, data_dir: Optional[Any] = None) -> bool:
    """Is ``path`` one of this registry's own checkouts (a child's copy, a delegated
    execution snapshot, a body candidate)? A soft read: an unreadable registry answers no."""
    target = Path(str(path or "")).resolve()
    return bool(str(path or "").strip()) and any(
        Path(str(entry.get("path"))).resolve() == target
        for entry in _load_registry(data_dir) if entry.get("path"))
