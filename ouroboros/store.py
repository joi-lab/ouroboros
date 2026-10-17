"""Current facts in ``state/current.sqlite`` and the SQLite protocol shared with ``usage_store``.
Rollback journal, ``synchronous=FULL``, no WAL; one connection per transaction, ``BEGIN IMMEDIATE``
writes, every name-tier access under its name lock. SQLite 3.24 floor supports system SQLite 3.37.
``probe``/``open_store``: boot imports within the import window; first writes import absent stores;
reads never import (facts present: ``StoreMigrating`` until publication; no facts: empty).
The write gate requires a closed import window and PUBLISHED phase; ``service`` serves the
importer/exporter/seam."""

from __future__ import annotations

import contextlib
import contextvars
import errno
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ouroboros.utils import append_jsonl, machine_json, utc_now_iso  # one compact JSON text: key order kept, facts round-trip

STORE_REL = pathlib.Path("state/current.sqlite")
LOCK_NAME = "current.lock"  # the name-tier lock around every access
IMPORT_LOCK_NAME = "current_import.lock"
GENERATION = 8  # the lifecycle generation that owns the store
GENERATION_KEY = "current_store_generation"  # in state/migrations.json
SCHEMA_VERSION = 5  # 2: owner questions; 3: acceptance; 4: plans; 5: drive retention
STORE_WAIT_SEC = 10.0
TIER_ENFORCED, TIER_NAME = "enforced", "name"
PUBLISHED, EXPORTING, RETIRED = "published", "exporting", "retired"
# The header's application id names the lock protocol before the file is opened ('OUCE'/'OUCN').
_APPLICATION_ID = {TIER_ENFORCED: 0x4F554345, TIER_NAME: 0x4F55434E}
TYPED_COLUMNS = ("status", "parent_task_id", "root_task_id", "delegation_role", "superseded_by",
                 "retry_task_id", "project_id", "ts", "updated_at", "child_drive_root",
                 "headless_child_drive_root", "drive_root", "presence_binding_id")
# set name -> flag column; the writer decides ``pending_drive`` (nonterminal, or terminal files not ready).
# A column added after schema 1 is nullable: a whole-row write by an older body (its own column list)
# leaves NULL, "not derived", which the next boot derives (``store_import._derive_flag_columns``).
FLAG_COLUMNS = {"synthesis": "owes_synthesis", "terminal_projection": "owes_terminal_projection",
                "promotions": "has_pending_promotion", "pause_notices": "has_pause_notices",
                "delegated_runs": "delegated_runs_unreconciled", "pending_drives": "pending_drive",
                "owner_questions": "has_owner_questions"}
# The per-operation projections of a document that live in ``task_handoffs`` rows (``kind`` = the
# field): a claim, a wait or a retirement writes its one row, never the body. The body keeps a
# present key as ``{}`` (its place and presence); ``load`` puts the rows back there, so a document
# round-trips byte for byte. A key the body never had appears while it has rows.
ROW_BACKED_FIELDS: Tuple[str, ...] = ("launch_handoffs", "model_waits", "retired_model_consumers")
REVIEW_COLLECTION_INDEX = ("CREATE INDEX IF NOT EXISTS tasks_review_collection ON tasks(review_collection_pending, task_id) "
                           "WHERE review_collection_pending IS NULL OR review_collection_pending=1;")
PLAN_COLLECTION_INDEX = ("CREATE INDEX IF NOT EXISTS tasks_plan_collection ON tasks(plan_collection_pending, task_id) "
                         "WHERE plan_collection_pending IS NULL OR plan_collection_pending=1;")
_SCHEMA = """
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY, status TEXT, parent_task_id TEXT, root_task_id TEXT, delegation_role TEXT,
  superseded_by TEXT, retry_task_id TEXT, project_id TEXT, ts TEXT, updated_at TEXT,
  child_drive_root TEXT, headless_child_drive_root TEXT, drive_root TEXT, presence_binding_id TEXT,
  presence_authority_recorded INTEGER NOT NULL DEFAULT 0, write_seq INTEGER NOT NULL DEFAULT 0,
  owes_synthesis INTEGER NOT NULL DEFAULT 0, owes_terminal_projection INTEGER NOT NULL DEFAULT 0,
  has_pending_promotion INTEGER NOT NULL DEFAULT 0, has_pause_notices INTEGER NOT NULL DEFAULT 0,
  delegated_runs_unreconciled INTEGER NOT NULL DEFAULT 0, pending_drive INTEGER NOT NULL DEFAULT 0,
  has_owner_questions INTEGER,
  review_collection_pending INTEGER, plan_collection_pending INTEGER, drive_retention_at TEXT);
CREATE INDEX tasks_parent ON tasks(parent_task_id); CREATE INDEX tasks_root ON tasks(root_task_id);
CREATE INDEX tasks_status ON tasks(status); CREATE INDEX tasks_project ON tasks(project_id);
CREATE INDEX tasks_ts ON tasks(ts, task_id); CREATE INDEX tasks_updated ON tasks(updated_at, task_id);
CREATE INDEX tasks_superseded ON tasks(superseded_by) WHERE superseded_by IS NOT NULL;
CREATE INDEX tasks_retry ON tasks(retry_task_id) WHERE retry_task_id IS NOT NULL;
CREATE TABLE task_bodies (task_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE obligations (set_name TEXT NOT NULL, id TEXT NOT NULL, facts TEXT NOT NULL, PRIMARY KEY (set_name, id));
CREATE TABLE task_handoffs (task_id TEXT NOT NULL, op_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT,
  claimed_at TEXT, returned_at TEXT, owner TEXT, facts TEXT NOT NULL, PRIMARY KEY (task_id, op_id));
CREATE TABLE custody_runs (run_id TEXT PRIMARY KEY, task_id TEXT, project_id TEXT, invocation_id TEXT, state TEXT,
  facts TEXT NOT NULL, updated_at TEXT);
CREATE INDEX custody_task ON custody_runs(task_id); CREATE INDEX custody_state ON custody_runs(state);
CREATE INDEX custody_project ON custody_runs(project_id) WHERE project_id<>'';
CREATE INDEX custody_invocation ON custody_runs(invocation_id) WHERE invocation_id<>'';
CREATE TABLE custody_outbox (event_id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, event TEXT NOT NULL, created_at TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""" + REVIEW_COLLECTION_INDEX + PLAN_COLLECTION_INDEX + "".join(f"CREATE INDEX tasks_{column} ON tasks({column}) WHERE {column}=1;\n"
              for column in FLAG_COLUMNS.values())


class StoreError(RuntimeError):
    """The current-facts store cannot serve; the subclass says why."""


class StoreMigrating(StoreError):
    """Not published yet: the importer runs, or never ran on this root."""


class StoreRetired(StoreError):
    """The export began or completed: the files are the authority now."""


class StoreLost(StoreError):
    """The store is missing beside its completed stamp, or unreadable; never replaced silently."""


class StoreBusy(StoreError):
    """Contention or exhausted process resources: retry; never a new store."""

    def __init__(self, message: str, *, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


_WAIT_BUDGET = contextvars.ContextVar("current_store_wait_budget", default=None)
_ADMISSION_NO_WAIT = contextvars.ContextVar("current_store_admission_no_wait", default=False)


@contextlib.contextmanager
def acquisition_budget(seconds: float):
    """Narrow nested store acquisitions; zero makes an interlocked probe nonblocking."""
    prior = _WAIT_BUDGET.get()
    token = _WAIT_BUDGET.set(min(prior, seconds) if prior is not None else seconds)
    try:
        yield
    finally:
        _WAIT_BUDGET.reset(token)


@contextlib.contextmanager
def admission_acquisition():
    """Probe/claim without waiting; this temporary guard is not the body's IO policy."""
    token = _ADMISSION_NO_WAIT.set(True)
    try:
        yield
    finally:
        _ADMISSION_NO_WAIT.reset(token)


@contextlib.contextmanager
def physical_acquisition():
    """At accepted handoff, copy only the caller's physical budget into its body.

    Other ContextVars and explicit acquisition_budget scopes remain unchanged;
    a nested physical start must acquire its own fresh admission guard.
    """
    token = _ADMISSION_NO_WAIT.set(False)
    try:
        yield
    finally:
        _ADMISSION_NO_WAIT.reset(token)


_PROCESS_LOCKS: Dict[str, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def process_lock(path: pathlib.Path) -> threading.RLock:
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(str(path), threading.RLock())


def file_identity(path: pathlib.Path) -> Optional[tuple]:
    """``(inode, device)`` of a present file; ``None`` only for a missing one."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return None
    return info.st_ino, info.st_dev


class _Inode:
    """One file's open uses in this process: SQLite connections from ``connect`` and raw readers,
    with the unbuffered handles the readers opened (``files[0]`` serves every read)."""

    __slots__ = ("conns", "readers", "files")

    def __init__(self) -> None:
        self.conns, self.readers, self.files = 0, 0, []


_INODES: Dict[Tuple[int, int], _Inode] = {}
# Reentrant: a reader generator finalized by the cyclic GC returns its borrow on whatever thread
# triggered the collection, possibly inside this thread's own guarded section.
_INODE_GUARD = threading.RLock()


def _settle(key: Tuple[int, int], entry: _Inode) -> None:
    """Close the readers' descriptors once nothing in this process uses the inode (guard held):
    with no connection open, no lock of this process on the file can be released by a close."""
    if entry.conns or entry.readers:
        return
    for handle in entry.files:
        with contextlib.suppress(OSError):
            handle.close()
    _INODES.pop(key, None)


def _borrow(path: Any) -> Tuple[Tuple[int, int], int]:
    """A read descriptor of PATH's file (POSIX): on POSIX closing ANY descriptor of a file releases
    every advisory lock this process holds on it, an open transaction's on another thread included,
    and another process then writes the same pages (sqlite.org howtocorrupt §2.2). So a raw read
    shares one descriptor per inode, and ``_return`` closes it only when no store connection of this
    process has the file open (SQLite's unix VFS defers its own closes the same way)."""
    key = _identity_key(os.stat(path))
    with _INODE_GUARD:
        entry = _INODES.get(key)
        if entry is not None and entry.files:
            entry.readers += 1
            return key, entry.files[0].fileno()
    handle = pathlib.Path(path).open("rb", buffering=0)  # outside the guard: a slow open stalls only this caller
    key = _identity_key(os.fstat(handle.fileno()))  # the path may name a newer file than the stat did
    with _INODE_GUARD:
        entry = _INODES.setdefault(key, _Inode())
        entry.files.append(handle)  # never closed early: a second handle waits for the settle too
        entry.readers += 1
        return key, entry.files[0].fileno()


def _return(key: Tuple[int, int]) -> None:
    with _INODE_GUARD:
        entry = _INODES.get(key)
        if entry is not None:
            entry.readers -= 1
            _settle(key, entry)


def _identity_key(info: os.stat_result) -> Tuple[int, int]:
    return info.st_dev, info.st_ino


class _StoreConnection(sqlite3.Connection):
    """A ``connect`` connection, counted on its file's inode until it closes (see ``_borrow``)."""

    _store_key: Optional[Tuple[int, int]] = None

    def close(self) -> None:
        super().close()  # a close that raises leaves the connection counted: its lock may still be held
        key, self._store_key = self._store_key, None
        if key is not None:
            with _INODE_GUARD:
                entry = _INODES.get(key)
                if entry is not None:
                    entry.conns -= 1
                    _settle(key, entry)


def _read_header(path: pathlib.Path) -> bytes:
    """The first 100 bytes, read through the inode registry (``_borrow``); Windows locks belong
    to their handle, and a held handle there would block the export rename."""
    if os.name == "nt":
        with open(path, "rb") as handle:
            return handle.read(100)
    key, fd = _borrow(path)
    try:
        return os.pread(fd, 100, 0)
    finally:
        _return(key)


def held_descriptor(path: Any) -> Optional[int]:
    """The descriptor this process's open uses of PATH's file share right now, else ``None``."""
    if os.name == "nt":
        return None
    try:
        key = _identity_key(os.stat(path))
    except OSError:
        return None
    with _INODE_GUARD:
        entry = _INODES.get(key)
        return entry.files[0].fileno() if entry is not None and entry.files else None


def is_sqlite_file(path: Any) -> bool:
    """Whether PATH holds a SQLite database (its header magic), read without closing a descriptor
    a store connection may depend on."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False  # never open a FIFO or a device to look
        return _read_header(pathlib.Path(path)).startswith(b"SQLite format 3\x00")
    except OSError:
        return False


class _BorrowedReader:
    """A binary reader over a borrowed descriptor (``_borrow``); closing returns the borrow."""

    def __init__(self, path: Any) -> None:
        self._key, self._fd = _borrow(path)
        self._offset, self._open = 0, True

    def __enter__(self) -> "_BorrowedReader":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._open:
            self._open = False
            _return(self._key)

    def _check(self) -> None:
        if not self._open:  # the descriptor may be closed or reused by another file by now
            raise ValueError("I/O operation on closed file")

    def fileno(self) -> int:
        self._check()
        return self._fd

    def tell(self) -> int:
        self._check()
        return self._offset

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._check()
        if whence not in (os.SEEK_SET, os.SEEK_CUR, os.SEEK_END):
            raise ValueError(f"invalid whence ({whence})")
        base = os.fstat(self._fd).st_size if whence == os.SEEK_END else (0 if whence == os.SEEK_SET else self._offset)
        if base + offset < 0:
            raise ValueError("negative seek position")
        self._offset = base + offset
        return self._offset

    def read(self, size: Optional[int] = -1) -> bytes:
        """Up to ``size`` bytes (all that remain when negative); short ``pread``s are continued."""
        self._check()
        wanted, parts = (-1 if size is None else size), []
        while wanted:
            data = os.pread(self._fd, wanted if wanted > 0 else 1 << 20, self._offset)
            if not data:
                break
            parts.append(data)
            self._offset += len(data)
            wanted = wanted - len(data) if wanted > 0 else wanted
        return b"".join(parts)


def open_for_read(path: Any) -> Any:
    """``open(path, "rb")`` for any reader in a process that may hold a store: on POSIX the read
    borrows the inode's shared descriptor, whose close waits until no store connection of this
    process has the file open; Windows opens and closes as usual."""
    return pathlib.Path(path).open("rb") if os.name == "nt" else _BorrowedReader(path)


def iter_file_bytes(path: Any, *, limit: Optional[int] = None, chunk: int = 1 << 20) -> Iterator[bytes]:
    """A file's bytes (the first ``limit``, or all), read through ``open_for_read``."""
    with open_for_read(path) as handle:
        while limit is None or limit > 0:
            data = handle.read(chunk if limit is None else min(chunk, limit))
            if not data:
                return
            if limit is not None:
                limit -= len(data)
            yield data


def read_file_bytes(path: Any, *, limit: Optional[int] = None) -> bytes:
    return b"".join(iter_file_bytes(path, limit=limit))


def copy_file_bytes(source: Any, destination: Any) -> Any:
    """``shutil.copy2`` that reads a SQLite file through ``open_for_read`` (a ``copytree``
    ``copy_function``); the copy is a byte copy, not a consistent snapshot of a live store."""
    if os.name == "nt" or not is_sqlite_file(source):
        return shutil.copy2(source, destination)
    target = pathlib.Path(destination)
    if target.is_dir():
        target = target / pathlib.Path(source).name
    if target.exists() and os.path.samefile(source, target):
        raise shutil.SameFileError(f"{source!r} and {str(target)!r} are the same file")
    with open(target, "wb") as handle:
        for data in iter_file_bytes(source):
            handle.write(data)
    shutil.copystat(source, target)
    return str(target)


def header_tier(path: pathlib.Path, application_ids: Dict[str, int]) -> Optional[str]:
    """The lock tier the file's SQLite header names; ``None`` for a missing or foreign file."""
    try:
        header = _read_header(path)
    except FileNotFoundError:
        return None
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        return None
    application_id = int.from_bytes(header[68:72], "big")
    return next((tier for tier, value in application_ids.items() if value == application_id), None)


def connect(path: pathlib.Path, tier: str, *, create: bool = False, wait_sec: float = 0.0) -> sqlite3.Connection:
    """A connection with the tier's lock protocol (``sqlite3.Error`` is the caller's to type);
    ``wait_sec`` is SQLite's busy wait on the enforced tier (the name tier's one lock is the
    name protocol the caller holds); no statement runs here, the first may wait on a commit."""
    query = "mode=rwc" if create else "mode=rw"
    if tier == TIER_NAME:
        query += "&vfs=" + ("win32-none" if os.name == "nt" else "unix-none")
    conn = sqlite3.connect(f"{path.absolute().as_uri()}?{query}", uri=True, isolation_level=None,
                           timeout=max(0.0, wait_sec) if tier == TIER_ENFORCED else 0.0, factory=_StoreConnection)
    conn.row_factory = sqlite3.Row
    if os.name != "nt":  # counted before any statement can take a lock (see ``_borrow``)
        try:
            key = _identity_key(os.stat(path))
        except OSError:
            conn.close()
            raise
        with _INODE_GUARD:
            _INODES.setdefault(key, _Inode()).conns += 1
            conn._store_key = key
    return conn


def busy(exc: BaseException) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    return code in {5, 6} or "locked" in str(exc).lower()


def commit(conn: sqlite3.Connection) -> None:
    """COMMIT, rolling back on failure; the ``sqlite3.OperationalError`` is the caller's to type."""
    try:
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        exc.rollback_confirmed = False
        try:
            conn.execute("ROLLBACK")
            exc.rollback_confirmed = not conn.in_transaction
        except sqlite3.Error:
            pass
        raise


def fsync_directory(directory: pathlib.Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, *, write: bool, commit_wait_sec: Optional[float] = None,
                finish: Optional[Callable[[], None]] = None) -> Iterator[sqlite3.Connection]:
    """One transaction: a write takes the lock at ``BEGIN IMMEDIATE``, a read's only wait is
    here; any exception rolls back and propagates; ``finish`` (default ``commit``) runs at the
    end; ``commit_wait_sec`` is the busy wait a COMMIT spends on readers draining."""
    began = False
    try:
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        began = True
        if not write:  # take the read lock now
            conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
        if commit_wait_sec is not None:
            conn.execute(f"PRAGMA busy_timeout = {int(commit_wait_sec * 1000)}")
        yield conn
    except BaseException as exc:
        if not began:
            exc.rollback_confirmed = not conn.in_transaction
        elif not (getattr(exc, "rollback_confirmed", False) and not conn.in_transaction):
            try:
                conn.execute("ROLLBACK")
                exc.rollback_confirmed = not conn.in_transaction
            except sqlite3.Error:
                exc.rollback_confirmed = False
        raise
    (finish or (lambda: commit(conn)))()


def store_path(root: Any) -> pathlib.Path:
    return pathlib.Path(root) / STORE_REL


def generation_stamped(root: Any) -> bool:
    """Whether ``state/migrations.json`` records a completed store generation (a torn
    file proves nothing and reads as unstamped)."""
    from ouroboros.startup_migrations import watermarks

    try:
        return watermarks(root).get(GENERATION_KEY) == GENERATION
    except ValueError:
        return False


def probe(root: Any) -> Tuple[str, Optional[str]]:
    """``("absent", None)`` or ``("present", tier)``; the third answer is typed: ``StoreBusy`` when
    the file cannot be inspected (an exhausted descriptor table is not absence), ``StoreLost`` for a foreign file."""
    path = store_path(root)
    try:
        os.stat(path)
        tier = header_tier(path, _APPLICATION_ID)
    except FileNotFoundError:
        return "absent", None
    except OSError as exc:
        name = errno.errorcode.get(exc.errno, str(exc.errno))
        raise StoreBusy(f"current store not inspectable ({name}): {path}", reason="uninspectable") from exc
    if tier is None:
        raise StoreLost(f"current store unreadable or foreign, never replaced: {path}")
    return "present", tier


def retired_sibling(root: Any) -> Optional[pathlib.Path]:
    """The newest ``current.sqlite.exported-<UTC>`` beside the store path, or ``None``."""
    path = store_path(root)
    try:
        names = sorted(name for name in os.listdir(path.parent) if name.startswith(path.name + ".exported-"))
    except OSError:
        return None
    return path.with_name(names[-1]) if names else None


def _typed(exc: sqlite3.Error) -> StoreError:
    if isinstance(exc, sqlite3.OperationalError) and busy(exc):
        error = StoreBusy(f"current store busy: {exc}", reason="contention")
        if hasattr(exc, "rollback_confirmed"):
            error.rollback_confirmed = exc.rollback_confirmed
        return error
    if isinstance(exc, sqlite3.DatabaseError) and not isinstance(
            exc, (sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.ProgrammingError)):
        return StoreLost(f"current store unreadable: {exc}")
    return StoreError(f"current store unavailable: {exc}")


@contextlib.contextmanager
def name_lock(root: Any, filename: str, *, timeout_sec: float, stale_sec: float = 90.0) -> Iterator[None]:
    """The owner-aware name lock under ``state/`` (the name tier's one lock, the import lock)."""
    from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock

    path = pathlib.Path(root) / "state" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    outcome = {}
    fd = acquire_exclusive_file_lock(path, timeout_sec=timeout_sec, stale_sec=stale_sec,
                                     owner_aware_stale=True, outcome=outcome)
    if fd is None:
        error = StoreBusy(f"current store lock unavailable: {path}", reason=outcome.get("reason", "unknown"))
        error.rollback_confirmed = True  # No connection/transaction opened behind this name lock.
        raise error
    try:
        yield
    finally:
        release_exclusive_file_lock(path, fd)


_IMPORTING: set = set()  # roots this process's lifespan is importing: their readers see ``StoreMigrating``
_IMPORTING_GUARD = threading.Lock()
_FIRST_TOUCH = threading.local()  # roots THIS thread is importing on first touch: a nested open answers typed
_SERVICE = threading.local()  # the import owner's thread: its own post-publication writes pass the window


def begin_import(root: Any) -> None:
    """Open the lifespan's import window for ROOT: every public write of that root answers
    ``StoreMigrating`` and ``migrating`` is true until ``end_import`` (the boot door closes it
    only after a successful publication; a failed import leaves it open for the process's life)."""
    with _IMPORTING_GUARD:
        _IMPORTING.add(str(pathlib.Path(root)))


def end_import(root: Any) -> None:
    with _IMPORTING_GUARD:
        _IMPORTING.discard(str(pathlib.Path(root)))


@contextlib.contextmanager
def importing(root: Any) -> Iterator[None]:
    """The import window for the extent of a block (tests and the first-touch import)."""
    begin_import(root)
    try:
        yield
    finally:
        end_import(root)


@contextlib.contextmanager
def service_thread() -> Iterator[None]:
    """Service mode for every transaction THIS thread opens: the import owner's steps after
    publication write through the public APIs (latch migration, obligations) past its own window."""
    previous = getattr(_SERVICE, "active", False)
    _SERVICE.active = True
    try:
        yield
    finally:
        _SERVICE.active = previous


def _in_window(root: Any) -> bool:
    with _IMPORTING_GUARD:
        return str(pathlib.Path(root)) in _IMPORTING


def published(root: Any) -> bool:
    """Whether a published store answers for ROOT: present and in phase PUBLISHED (readiness is
    publication, never the file's existence)."""
    try:
        if probe(root)[0] != "present":
            return False
        with open_store(root, write=False) as txn:
            return txn.phase() == PUBLISHED
    except StoreError:
        return False


def store_ready(root: Any) -> bool:
    """Whether a write door may proceed: no import of ROOT runs here and its store is published."""
    return not _in_window(root) and published(root)


def migrating(root: Any) -> bool:
    """The doors' predicate (P1): True while the import window is open for ROOT, while its
    present store is not published (an export runs), or while ROOT holds facts no import has
    published yet (the boot door imports them); a root without facts is not migrating — its first
    write publishes an empty store. A pure read: nothing is opened for write, nothing imported."""
    if _in_window(root):
        return True
    root = pathlib.Path(root)
    try:
        state, _tier = probe(root)
    except StoreError:
        return False  # the door proceeds; its write gets the typed answer
    if state == "present":
        return not published(root)
    return holds_facts(root) and not (generation_stamped(root) or retired_sibling(root))


def holds_facts(root: pathlib.Path) -> bool:
    """Whether ROOT holds facts the importer would read: result files or obligation set files."""
    from ouroboros.store_import import holds_facts as _holds

    return _holds(root)


def _absent(root: pathlib.Path, *, write: bool, service: bool) -> bool:
    """The typed answer for an absent store; a first-touch import when a public WRITE asks for
    one and nothing forbids it (the window, a nested open); ``True`` when a READ may answer
    empty because ROOT holds no facts to import (a read never imports: a root with facts is
    the boot door's, ``StoreMigrating`` until it publishes)."""
    if generation_stamped(root):
        raise StoreLost(f"current store missing beside its completed stamp: {store_path(root)}")
    retired = retired_sibling(root)
    if retired is not None:
        raise StoreRetired(f"current store retired by an export as {retired.name}: the files are the authority")
    entered = getattr(_FIRST_TOUCH, "roots", None) or set()
    if _in_window(root) or str(root) in entered or (write and service):
        raise StoreMigrating(f"current store not published yet: {store_path(root)}")
    if not write:
        if holds_facts(root):
            raise StoreMigrating(f"current store not published yet: {store_path(root)} (a read never imports)")
        return True
    from ouroboros.store_import import ensure_current_store

    # Another thread importing the same root is waited for inside ``ensure_current_store`` (its
    # name lock); only THIS thread's nested open of the root answers typed instead of re-entering.
    _FIRST_TOUCH.roots = entered | {str(root)}
    try:
        ensure_current_store(root, with_children=False)
    except StoreError:
        raise
    except Exception as exc:  # the files the import reads are unavailable: the store cannot answer
        raise StoreMigrating(f"current store not published: the first-touch import failed: {exc}") from exc
    finally:
        _FIRST_TOUCH.roots = entered
    return False


@contextlib.contextmanager
def open_store(root: Any, *, write: bool, wait_sec: float = STORE_WAIT_SEC, service: bool = False,
               barrier: bool = False) -> Iterator["Txn"]:
    """One transaction on the published store: ``StoreMigrating`` while the lifespan imports or
    for a read of a never-imported root that holds facts (a read never imports; a root without
    facts, or one that does not exist, reads empty and creates nothing), a first-touch import
    when a public write reaches a never-imported root (a nested drive's first row), ``StoreLost``
    for a missing store beside its completed stamp, ``StoreRetired`` for one an export set aside
    (the files are the authority until the next import), ``StoreBusy`` on contention;
    ``mode=rw``, never creating here.

    The one write gate: a public write (``write`` without ``service``) refuses ``StoreMigrating``
    while ROOT is in the import window and ``StoreRetired`` inside the transaction when the phase
    is not PUBLISHED. ``service`` is the importer's, the exporter's and the seam's mode (also the
    import owner thread's, ``service_thread``): their transactions pass both checks. A ``barrier``
    holds the write lock and the gate without writing (the evidence packer's phase gate) and ends
    in ROLLBACK, so its release never waits for readers to drain as a COMMIT would."""
    if _ADMISSION_NO_WAIT.get():
        wait_sec = 0.0
    elif _WAIT_BUDGET.get() is not None:
        wait_sec = min(wait_sec, _WAIT_BUDGET.get())
    root = pathlib.Path(root)
    service = service or bool(getattr(_SERVICE, "active", False))
    if write and not service and _in_window(root):
        raise StoreMigrating(f"current store not published yet: the import of {root} runs")
    state, tier = probe(root)
    if state == "absent" and ((not write and not root.is_dir()) or _absent(root, write=write, service=service)):
        with contextlib.closing(sqlite3.connect(":memory:", isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            with transaction(conn, write=False):
                yield Txn(conn, root)
        return
    if state == "absent":
        state, tier = probe(root)
        if state == "absent":
            raise StoreMigrating(f"current store not published yet: {store_path(root)}")
    with contextlib.ExitStack() as stack:
        if tier == TIER_NAME:
            stack.enter_context(name_lock(root, LOCK_NAME, timeout_sec=wait_sec))
        try:
            conn = connect(store_path(root), tier, wait_sec=wait_sec)
        except sqlite3.Error as exc:
            raise _typed(exc) from exc
        stack.callback(conn.close)
        try:
            with transaction(conn, write=write, commit_wait_sec=wait_sec if tier == TIER_ENFORCED else None,
                             finish=(lambda: conn.execute("ROLLBACK")) if barrier else None):
                txn = Txn(conn, root)
                if write and not service and (phase := txn.phase()) != PUBLISHED:
                    raise StoreRetired(f"current store phase is {phase}: the files are the authority")
                yield txn
                if barrier and conn.total_changes:
                    raise StoreError("a barrier transaction wrote rows; its ROLLBACK would discard them")
                if write and conn.total_changes:
                    txn.set_meta("commit_seq", txn.revision() + 1)
        except sqlite3.Error as exc:
            raise _typed(exc) from exc


def utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def supervisor_row(root: pathlib.Path, kind: str, payload: Dict[str, Any], *, best_effort: bool = True) -> None:
    """One supervisor row; pack callers keep append failures, import/switch log them."""
    try:
        append_jsonl(root / "logs" / "supervisor.jsonl", {"ts": utc_now_iso(), "type": kind, **payload})
    except Exception:
        if not best_effort:
            raise
        import logging

        logging.getLogger(__name__).warning("%s row not recorded", kind, exc_info=True)


def document_text(document: Dict[str, Any]) -> str:
    """The whole document serialized exactly as the result writer serializes a file: what an
    export writes (the exact inverse of ``load``), what the content baseline hashes."""
    return machine_json(document)


def encode_document(document: Dict[str, Any]) -> str:
    """The body text stored in ``task_bodies``: the document with each present row-backed field
    emptied (the rows hold its content)."""
    return document_text({key: ({} if key in ROW_BACKED_FIELDS else value) for key, value in document.items()})


def typed_columns(document: Dict[str, Any], *, pending_drive: Optional[bool] = None) -> Dict[str, Any]:
    """Top-level facts and derived Presence, obligation, collection and retention projections."""
    from ouroboros.dialogue_provenance import presence_metadata_binding, presence_record_binding
    from ouroboros.obligations import result_memberships, review_collection_pending
    from ouroboros.plan_record import PLAN_RECORD_KEY, collection_pending
    from ouroboros.retention import task_result_retention_at

    names = result_memberships(document)
    columns: Dict[str, Any] = {}
    for column in TYPED_COLUMNS:
        value = document.get(column)
        text = "" if value is None else str(value).strip()
        columns[column] = text or None  # NULL is "no fact": the lineage prefilter and its index test IS NOT NULL
    # One derived scalar selects a Presence binding's own work without a body read; an empty
    # scalar is not proof of absent provenance (a malformed carrier, or a ceiling whose
    # carrier was lost, must never inherit a queue claim), hence the second fact.
    columns["presence_binding_id"] = presence_record_binding(document) or None
    contract = document.get("task_contract")
    columns["presence_authority_recorded"] = int(
        presence_metadata_binding(document.get("metadata")) is not None
        or (isinstance(contract, dict) and "capability_ceiling" in contract))
    for name, column in FLAG_COLUMNS.items():
        columns[column] = int(name in names)
    columns["pending_drive"] = int("nonterminal" in names if pending_drive is None else pending_drive)
    columns["review_collection_pending"] = int(review_collection_pending(document))
    columns["plan_collection_pending"] = int(collection_pending(document.get(PLAN_RECORD_KEY)))
    columns["drive_retention_at"] = task_result_retention_at(document)
    return columns


def decode_handoffs(task_ids: List[str], records: list) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``{task_id: {kind: {op_id: facts}}}`` from stored handoff rows, every kind present (no store held)."""
    rows: Dict[str, Dict[str, Dict[str, Any]]] = {task_id: {kind: {} for kind in ROW_BACKED_FIELDS} for task_id in task_ids}
    for record in records:
        rows[record["task_id"]].setdefault(record["kind"], {})[record["op_id"]] = json.loads(record["facts"])
    return rows


def project_handoffs(document: Dict[str, Any], rows: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """The body's emptied keys take their rows back; a key the body lacks appears only with rows."""
    for kind, held in rows.items():
        if held or kind in document:
            document[kind] = held
    return document


class Txn:
    """One store transaction: documents, obligation rows, meta; riders of the cutover
    (handoffs, custody, outbox) share the same connection."""

    def __init__(self, conn: sqlite3.Connection, root: Any = None) -> None:
        self.conn = conn
        self.root = pathlib.Path(root) if root is not None else None

    def meta(self, key: str) -> Any:
        record = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if record is None else json.loads(record["value"])

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, machine_json(value)))

    def phase(self) -> str:
        return str(self.meta("phase") or PUBLISHED)

    def revision(self) -> int:
        """Committed current-fact writes; rollback and empty write barriers do not advance it."""
        return int(self.meta("commit_seq") or 0)

    def task_ids(self) -> List[str]:
        return [record["task_id"] for record in self.conn.execute("SELECT task_id FROM tasks ORDER BY task_id")]

    def write_seqs(self) -> Dict[str, Tuple[int, Any]]:
        """Every task's ``(write_seq, updated_at)`` (``task_store.signature``): one narrow scan of which rows changed."""
        return {record["task_id"]: (record["write_seq"], record["updated_at"])
                for record in self.conn.execute("SELECT task_id, write_seq, updated_at FROM tasks")}

    def body_text(self, task_id: str) -> Optional[str]:
        record = self.conn.execute("SELECT body FROM task_bodies WHERE task_id=?", (task_id,)).fetchone()
        return None if record is None else record["body"]

    def load(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The document: its body with the row-backed fields projected from ``task_handoffs``."""
        body = self.body_text(task_id)
        return None if body is None else project_handoffs(json.loads(body), self.handoffs(task_id))

    def store_task(self, document: Dict[str, Any], *, pending_drive: Optional[bool] = None) -> None:
        """Upsert body, typed columns and handoffs; ``write_seq`` advances the memo signature."""
        task_id = str(document["task_id"])
        columns = typed_columns(document, pending_drive=pending_drive)
        columns["write_seq"] = (self.row(task_id, "write_seq") or 0) + 1
        self.conn.execute("INSERT OR REPLACE INTO task_bodies (task_id, body) VALUES (?, ?)",
                          (task_id, encode_document(document)))
        names = ("task_id", *columns)
        self.conn.execute(f"INSERT OR REPLACE INTO tasks ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                          (task_id, *columns.values()))
        self.sync_handoffs(task_id, document)

    def touch(self, task_id: str) -> None:
        """A row-only write still moves the task's signature (the memos re-read)."""
        self.conn.execute("UPDATE tasks SET write_seq = write_seq + 1 WHERE task_id=?", (task_id,))

    def row(self, task_id: str, *columns: str) -> Any:
        """One task's typed columns (one value for one column, a tuple for several; ``None`` when absent)."""
        record = self.conn.execute(f"SELECT {','.join(columns)} FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if record is None:
            return None
        return record[0] if len(columns) == 1 else tuple(record)

    def delete_task(self, task_id: str) -> None:
        for table in ("task_bodies", "tasks", "task_handoffs"):
            self.conn.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))

    def set_names(self) -> List[str]:
        return [record["set_name"] for record in
                self.conn.execute("SELECT DISTINCT set_name FROM obligations ORDER BY set_name")]

    def set_rows(self, set_name: str) -> Dict[str, Dict[str, Any]]:
        """One obligation set, in insertion order (the order its file had)."""
        return {record["id"]: json.loads(record["facts"]) for record in self.conn.execute(
            "SELECT id, facts FROM obligations WHERE set_name=? ORDER BY rowid", (set_name,))}

    def replace_set(self, set_name: str, rows: Dict[str, Dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM obligations WHERE set_name=?", (set_name,))
        self.conn.executemany("INSERT INTO obligations (set_name, id, facts) VALUES (?, ?, ?)",
                              [(set_name, str(identity), machine_json(facts)) for identity, facts in rows.items()])

    def put_row(self, set_name: str, identity: str, facts: Dict[str, Any]) -> None:
        """One owner-held obligation row (an existing row keeps its place; only changed facts write)."""
        self.conn.execute(
            "INSERT INTO obligations (set_name,id,facts) VALUES (?,?,?) ON CONFLICT(set_name,id) "
            "DO UPDATE SET facts=excluded.facts WHERE obligations.facts<>excluded.facts",
            (set_name, identity, machine_json(facts)))

    def delete_row(self, set_name: str, identity: str) -> None:
        self.conn.execute("DELETE FROM obligations WHERE set_name=? AND id=?", (set_name, identity))

    def get_row(self, set_name: str, identity: str) -> Optional[Dict[str, Any]]:
        record = self.conn.execute("SELECT facts FROM obligations WHERE set_name=? AND id=?",
                                   (set_name, identity)).fetchone()
        return None if record is None else json.loads(record["facts"])

    # -- handoff rows: one row per operation of a task's ``ROW_BACKED_FIELDS`` projection, keyed
    #    (task_id, op_id); ``kind`` names the field. Insertion order is the dict order the document had.
    def handoff_records(self, task_ids: List[str]) -> list:
        """The handoff rows of the given tasks as stored (``facts`` still text), in row order."""
        records: list = []
        for start in range(0, len(task_ids), 500):
            chunk = task_ids[start:start + 500]
            records.extend(self.conn.execute(
                f"SELECT task_id, kind, op_id, facts FROM task_handoffs WHERE task_id IN "
                f"({','.join('?' * len(chunk))}) ORDER BY rowid", chunk).fetchall())
        return records

    def handoffs_of(self, task_ids: List[str]) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """``{task_id: {kind: {op_id: facts}}}`` for the given tasks (one query), every kind present."""
        return decode_handoffs(task_ids, self.handoff_records(task_ids))

    def handoffs(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        return self.handoffs_of([task_id]).get(task_id, {})

    def handoff(self, task_id: str, op_id: str) -> Optional[Any]:
        record = self.conn.execute("SELECT facts FROM task_handoffs WHERE task_id=? AND op_id=?",
                                   (task_id, op_id)).fetchone()
        return None if record is None else json.loads(record["facts"])

    def put_handoff(self, task_id: str, kind: str, op_id: str, facts: Any) -> None:
        """Upsert one operation row (an existing row keeps its place); the typed columns are read
        from a dict's ``state`` / ``claimed_at`` / ``returned_at`` / ``local_owner`` when present."""
        typed = facts if isinstance(facts, dict) else {}
        owner = typed.get("local_owner")
        self.conn.execute(
            "INSERT INTO task_handoffs (task_id, op_id, kind, state, claimed_at, returned_at, owner, facts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id, op_id) DO UPDATE SET kind=excluded.kind, "
            "state=excluded.state, claimed_at=excluded.claimed_at, returned_at=excluded.returned_at, "
            "owner=excluded.owner, facts=excluded.facts",
            (task_id, op_id, kind, *(None if typed.get(key) is None else str(typed[key])
                                     for key in ("state", "claimed_at", "returned_at")),
             machine_json(owner) if isinstance(owner, dict) else None, machine_json(facts)))

    def delete_handoff(self, task_id: str, op_id: str) -> None:
        self.conn.execute("DELETE FROM task_handoffs WHERE task_id=? AND op_id=?", (task_id, op_id))

    def sync_handoffs(self, task_id: str, document: Dict[str, Any]) -> None:
        """The rows follow a whole-document write (the importer, a mutator over the projected
        fields): rows the document no longer names go, changed or new ones are written."""
        held = self.handoffs(task_id)
        for kind in ROW_BACKED_FIELDS:
            wanted = document.get(kind) if isinstance(document.get(kind), dict) else {}
            current = held.get(kind, {})
            for op_id in current.keys() - wanted.keys():
                self.delete_handoff(task_id, str(op_id))
            for op_id, facts in wanted.items():
                if str(op_id) not in current or current[str(op_id)] != facts:
                    self.put_handoff(task_id, kind, str(op_id), facts)

    # -- custody rows: one row per delegated run, the reducer's output, indexed by task / project /
    #    invocation; ``state`` is the custody hold (``held`` while the run owes work, ``closed`` after).
    def custody_rows(self, where: str = "", params: Tuple[Any, ...] = ()) -> Dict[str, Dict[str, Any]]:
        return {key: json.loads(raw) for key, raw in self.custody_raw_rows(where, params).items()}

    def custody_raw_rows(self, where: str = "", params: Tuple[Any, ...] = ()) -> Dict[str, str]:
        """Fetch one consistent selection; callers can decode after releasing the read transaction."""
        clause = f" WHERE {where}" if where else ""
        return {record["run_id"]: record["facts"] for record in
                self.conn.execute(f"SELECT run_id, facts FROM custody_runs{clause} ORDER BY run_id", params)}

    def custody_row(self, run_id: str) -> Optional[Dict[str, Any]]:
        record = self.conn.execute("SELECT facts FROM custody_runs WHERE run_id=?", (run_id,)).fetchone()
        return None if record is None else json.loads(record["facts"])

    def put_custody(self, run_id: str, facts: Dict[str, Any], *, state: str) -> None:
        self.conn.execute(
            "INSERT INTO custody_runs (run_id, task_id, project_id, invocation_id, state, facts, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET task_id=excluded.task_id, "
            "project_id=excluded.project_id, invocation_id=excluded.invocation_id, state=excluded.state, "
            "facts=excluded.facts, updated_at=excluded.updated_at",
            (run_id, str(facts.get("task_id") or ""), str(facts.get("project_id") or ""),
             str(facts.get("invocation_id") or ""), state, machine_json(facts), utc_now_iso()))

    # -- the custody outbox: an emitted event waits here between its COMMIT and its chain append.
    def outbox_put(self, event: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO custody_outbox (event_id, run_id, task_id, event, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(event.get("event_id") or ""), str(event.get("run_id") or ""), str(event.get("task_id") or ""),
             machine_json(event), utc_now_iso()))

    def outbox_rows(self) -> List[Dict[str, Any]]:
        return [json.loads(record["event"]) for record in
                self.conn.execute("SELECT event FROM custody_outbox ORDER BY created_at, event_id")]

    def outbox_delete(self, event_id: str) -> None:
        self.conn.execute("DELETE FROM custody_outbox WHERE event_id=?", (event_id,))


def update_task(root: Any, task_id: str, mutator: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]], *,
                pending_drive: Any = None) -> Optional[Dict[str, Any]]:
    """Merge CURRENT under the published write gate; ``None`` commits without a write.
    ``pending_drive`` is a value or ``(txn, current, updated) -> Optional[bool]``.
    Schema preparation precedes the callback, including on a first-touched old child store."""
    while True:
        with open_store(root, write=True) as txn:
            if int(txn.meta("schema_version") or 1) >= SCHEMA_VERSION:
                current = txn.load(task_id) or {}
                updated = mutator(json.loads(json.dumps(current)))
                if updated is None:
                    return None
                flag = pending_drive(txn, current, updated) if callable(pending_drive) else pending_drive
                txn.store_task({**updated, "task_id": task_id}, pending_drive=flag)
                return updated
        # No callback ran: the import owner upgrades with the same acquisition budget.
        from ouroboros.store_import import upgrade_current_schema
        upgrade_current_schema(root)
