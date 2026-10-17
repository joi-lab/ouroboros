"""Dashboard -> Storage: an explicit, bounded measurement of the data folder (issue #1418).

``StorageObserver`` runs at most one metadata walk per server process, on a daemon thread;
a second start while it runs joins it. Its projection is passive: it never walks and never
writes. Nothing is persisted, so a restarted server is "not measured yet" again, and a failed
refresh keeps the previous measurement beside its error. ``gateway.storage`` mounts the routes.

The walk reads directory entries and ``lstat`` sizes only, never file contents, inside the
data folder. It counts fixed, disjoint categories: a category root nested inside another
category's walk is pruned from it, so each byte lands in one row. It is depth-first and keeps
open only the folders on its current path; each category stops at a time, an entry and a
depth bound, checked before every listing as well as per entry. A multiply linked file counts
once by its (device, inode) identity; one whose identity cannot be read is left out. A
stopped, partly unreadable or partly skipped category is ``partial`` (its bytes a lower
bound); one whose folders could not be listed at all has ``bytes: None`` — unknown, never
zero. Sizes are logical file sizes, not reclaimable space.

A link is never counted, and its target is never walked where the platform can open a folder
relative to its parent without following links (``openat`` + ``O_NOFOLLOW``): a folder swapped
for a link mid-walk then fails to open and counts as an error. Elsewhere (Windows) each folder
is re-checked by path just before it is listed, so a swap inside that instant could be listed
through the link: there the walk is best effort, still metadata only and still bounded.

The engine's home inside the data folder and the configured worktree and project roots are
excluded by path and, where the platform reports it, by folder identity, wherever they sit;
they are reported as not measured. There is no whole-disk total, only the free space of the
data folder's volume.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import stat
import threading
import time
from typing import Any, Callable, Dict, FrozenSet, List, Literal, NamedTuple, Optional, Set, Tuple

from ouroboros.config import get_subagent_projects_root, get_subagent_worktree_root
from ouroboros.utils import utc_now_iso

try:  # Python 3.11+
    from typing import NotRequired, TypedDict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - CI supports Python 3.10.
    from typing_extensions import NotRequired, TypedDict  # type: ignore[assignment]

log = logging.getLogger(__name__)

# Per-category bounds of one owner-requested walk, so a huge, deep or slow folder (model-call
# records alone can hold ~200k files) cannot starve the other rows or run unbounded. A physical
# bound on one read, not a setting. The walk holds two descriptors per open level, so the depth
# bound also bounds its descriptors.
CATEGORY_SECONDS = 5.0
CATEGORY_ENTRIES = 250_000
CATEGORY_DEPTH = 32

# (id, roots relative to the data folder); "other" is the data folder itself minus every root
# above it and the engine's home.
CATEGORY_ROOTS: Tuple[Tuple[str, Tuple[Tuple[str, ...], ...]], ...] = (
    ("observability", (("observability",),)),
    ("task_drives", (("state", "headless_tasks"), ("task_drives",))),
    ("results", (("task_results",),)),
    ("archives", (("archive",),)),
    ("memory", (("memory",), ("projects",))),
    ("skills", (("skills",), ("state", "skills"))),
    ("other", ((),)),
)
ENGINE_DIR = "claudexor"  # the owned engine home's name (claudexor_daemon.owned_config_dir)
NOT_MEASURED = ("engine", "worktrees", "project_folders")

# Directory-relative opening that never follows a link; without it the walk falls back to a
# by-path check just before each listing.
_RELATIVE = bool(getattr(os, "O_NOFOLLOW", 0) and getattr(os, "O_DIRECTORY", 0)
                 and {os.open, os.stat} <= os.supports_dir_fd and os.scandir in os.supports_fd)


class StorageCategory(TypedDict):
    """One measured row; ``bytes`` is None when none of its folders could be listed."""

    id: str
    bytes: Optional[int]
    files: int
    coverage: Literal["complete", "partial", "unreadable"]
    stopped: Literal["", "time_limit", "entry_limit"]
    errors: int
    skipped: int


class StorageMeasurement(TypedDict):
    finished_at: str
    duration_seconds: float
    measured_bytes: int
    complete: bool
    free_bytes: Optional[int]
    categories: List[StorageCategory]
    not_measured: List[str]


class StorageError(TypedDict):
    at: str
    message: str


class StorageResponse(TypedDict):
    """``GET /api/storage``; ``POST /api/storage/measure`` adds ``started`` (202)."""

    measuring: bool
    measurement: Optional[StorageMeasurement]
    error: Optional[StorageError]
    started: NotRequired[bool]


class StorageUnavailable(Exception):
    """A measurement that could not start; its text is shown to the owner."""


class _Excluded(NamedTuple):
    """Folders one category does not walk, by path key and by (device, inode) where known.
    Every folder is asked on the way down, so the first excluded ancestor stops the walk."""

    keys: FrozenSet[str]
    identities: FrozenSet[Tuple[int, int]]

    def holds(self, path: str, info: Optional[os.stat_result] = None) -> bool:
        if _key(path) in self.keys:
            return True
        return bool(info is not None and info.st_ino and (info.st_dev, info.st_ino) in self.identities)


def _key(path: str) -> str:
    return os.path.normcase(path)


def _inside(path: str, root: str) -> bool:
    try:
        return path != root and os.path.commonpath([_key(path), _key(root)]) == _key(root)
    except ValueError:  # another drive on Windows
        return False


def _directory_flags() -> int:
    return (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


def _close(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def _is_reparse(info: os.stat_result) -> bool:
    """A symlink or Windows junction/reparse point is never traversed."""
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _listing_stat(entry: os.DirEntry, directory_fd: Optional[int]) -> os.stat_result:
    """An entry's own metadata, relative to its open parent where the platform allows."""
    if directory_fd is not None:
        return os.stat(entry.name, dir_fd=directory_fd, follow_symlinks=False)
    return entry.stat(follow_symlinks=False)


def _path_stat(path: str) -> os.stat_result:
    """A by-path ``lstat``: Windows listings carry no inode or link count, the file itself does."""
    return os.lstat(path)


def _open_root(base: str, parts: Tuple[str, ...], excluded: _Excluded) -> Tuple[str, Optional[int]]:
    """Open one category root one component at a time, never through a link and never into an
    excluded folder: (``"open"`` | ``"absent"`` | ``"excluded"`` | ``"error"``, descriptor)."""
    path, fd = base, None
    try:
        if _RELATIVE:
            fd = os.open(base, _directory_flags())
        for part in parts:
            path = os.path.join(path, part)
            if excluded.holds(path):
                _close(fd)
                return "excluded", None
            if fd is not None:
                child = os.open(part, _directory_flags(), dir_fd=fd)
                _close(fd)
                fd = child
            else:
                info = _path_stat(path)
                if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    return "error", None
            if excluded.holds(path, os.fstat(fd) if fd is not None else _path_stat(path)):
                _close(fd)
                return "excluded", None
    except FileNotFoundError:
        _close(fd)
        return "absent", None
    except OSError:  # a link, a file or a refusal where a folder should be
        _close(fd)
        return "error", None
    return "open", fd


def _measure_category(category_id: str, base: str, roots: Tuple[Tuple[str, ...], ...], excluded: _Excluded, *,
                      seconds: float, entries: int, depth: int, seen: Set[Tuple[int, int]],
                      clock: Callable[[], float]) -> StorageCategory:
    deadline = clock() + seconds
    counted = files = errors = skipped = visited = 0
    listed = False
    stopped: Literal["", "time_limit", "entry_limit"] = ""
    # Depth-first: only the folders on the current path are open, each as (path, own descriptor
    # or None, its listing, level), so a wide tree holds no more than a narrow one.
    frames: List[Tuple[str, Optional[int], Any, int]] = []

    def spent() -> bool:
        nonlocal stopped
        if visited >= entries:
            stopped = "entry_limit"
        elif clock() >= deadline:
            stopped = "time_limit"
        return bool(stopped)

    def enter(path: str, fd: Optional[int], level: int) -> None:
        """List one folder as the innermost frame; the frame owns FD from here on."""
        nonlocal errors, listed
        try:
            if fd is None:  # the by-path fallback re-checks the folder just before listing it
                info = _path_stat(path)
                if _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    errors += 1  # it changed into a link or a file while counting
                    return
            listing = os.scandir(fd if fd is not None else path)
        except FileNotFoundError:
            _close(fd)  # removed while counting: it holds nothing now
            return
        except OSError:
            _close(fd)
            errors += 1
            return
        listed = True
        frames.append((path, fd, listing, level))

    def leave() -> None:
        _path, fd, listing, _level = frames.pop()
        listing.close()
        _close(fd)

    try:
        for parts in roots:
            if spent():
                break
            status, fd = _open_root(base, parts, excluded)
            if status == "error":
                errors += 1
            if status != "open":
                continue  # an absent optional root is empty; an excluded one is reported elsewhere
            enter(os.path.join(base, *parts), fd, 0)
            while frames and not stopped:
                directory, directory_fd, listing, level = frames[-1]
                try:
                    entry = next(listing)
                except (StopIteration, FileNotFoundError):
                    leave()
                    continue
                except OSError:
                    errors += 1
                    leave()
                    continue
                if spent():
                    break
                visited += 1
                child = os.path.join(directory, entry.name)
                try:
                    info = _listing_stat(entry, directory_fd)
                    if _is_reparse(info):
                        continue  # a link is never counted and its target never walked
                    if stat.S_ISDIR(info.st_mode):
                        if excluded.holds(child, info):
                            continue
                        if level >= depth:
                            skipped += 1  # deeper than the bound: its contents stay unknown
                            continue
                        if spent():
                            break  # checked before every listing, not only per entry
                        child_fd = (os.open(entry.name, _directory_flags(), dir_fd=directory_fd)
                                    if directory_fd is not None else None)
                        enter(child, child_fd, level + 1)
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if info.st_nlink != 1 and not info.st_ino:
                        # Windows listings carry no link count or inode: ask the file itself.
                        info = _path_stat(child)
                        if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
                            continue
                    if info.st_nlink != 1:
                        if not info.st_ino:
                            skipped += 1  # possibly counted elsewhere: left out, never counted twice
                            continue
                        identity = (info.st_dev, info.st_ino)
                        if identity in seen:
                            continue  # a hard link already counted in this walk
                        seen.add(identity)
                    counted += info.st_size
                    files += 1
                except FileNotFoundError:
                    continue  # removed while counting: it holds nothing now
                except OSError:
                    errors += 1  # includes a folder swapped for a link before it opened
    finally:
        while frames:
            leave()

    # A root that existed but could not be listed is unknown, never zero; an absent optional
    # category root remains a truthful complete zero.
    unreadable = bool(errors) and not listed
    return {
        "id": category_id,
        "bytes": None if unreadable else counted,
        "files": files,
        "coverage": "unreadable" if unreadable else "partial" if stopped or errors or skipped else "complete",
        "stopped": stopped,
        "errors": errors,
        "skipped": skipped,
    }


def _excluded_identities(paths: Set[str]) -> FrozenSet[Tuple[int, int]]:
    """The folder identities of excluded paths that exist; a missing one has none."""
    identities = set()
    for path in paths:
        try:
            info = os.stat(path)
        except (OSError, ValueError):
            continue
        if info.st_ino and stat.S_ISDIR(info.st_mode):
            identities.add((info.st_dev, info.st_ino))
    return frozenset(identities)


def measure_data_folder(data_dir: pathlib.Path, *, external_roots: Dict[str, str],
                        seconds: float = CATEGORY_SECONDS, entries: int = CATEGORY_ENTRIES,
                        depth: int = CATEGORY_DEPTH, clock: Callable[[], float] = time.monotonic) -> StorageMeasurement:
    """One bounded metadata walk of ``data_dir`` by category (module docstring)."""
    root = os.path.realpath(data_dir)
    if not os.path.isdir(root):
        raise StorageUnavailable("The data folder could not be found.")
    began = clock()
    category_paths = {cid: {os.path.join(root, *parts) for parts in rels} for cid, rels in CATEGORY_ROOTS}
    # Other categories' roots and the engine's home are walked elsewhere or not at all.
    internal = {path for cid, paths in category_paths.items() if cid != "other" for path in paths}
    internal.add(os.path.join(root, ENGINE_DIR))
    # Configured roots are excluded from EVERY category, its own roots included: one that is (or
    # contains) a category root leaves that root unwalked rather than counted twice. Both spellings:
    # a configured path may itself be a link under the data folder, or an alias of one.
    external = set()
    for configured in external_roots.values():
        if configured:
            configured_path = os.path.abspath(os.fspath(configured))
            external.update((configured_path, os.path.realpath(configured_path)))
    # A path key only matches the walk's own spelling; a folder identity also matches an alias
    # spelled differently (letter case, another mount path), wherever the platform reports one.
    external_keys = {_key(path) for path in external if _inside(path, root)}
    external_identities = _excluded_identities(external)
    seen: Set[Tuple[int, int]] = set()
    categories = []
    for cid, rels in CATEGORY_ROOTS:
        paths = internal - category_paths[cid]
        excluded = _Excluded(frozenset({_key(path) for path in paths} | external_keys),
                             _excluded_identities(paths) | external_identities)
        categories.append(_measure_category(cid, root, rels, excluded, seconds=seconds, entries=entries,
                                            depth=depth, seen=seen, clock=clock))
    try:
        free_bytes: Optional[int] = shutil.disk_usage(root).free
    except OSError:
        free_bytes = None
    return {
        "finished_at": utc_now_iso(),
        "duration_seconds": round(max(0.0, clock() - began), 2),
        "measured_bytes": sum(row["bytes"] or 0 for row in categories),
        "complete": all(row["coverage"] == "complete" for row in categories),
        "free_bytes": free_bytes,
        "categories": categories,
        "not_measured": list(NOT_MEASURED),
    }


def configured_external_roots() -> Dict[str, str]:
    return {"worktrees": get_subagent_worktree_root(), "project_folders": get_subagent_projects_root()}


class StorageObserver:
    """The process-local measurement owner: one walk at a time, the last result in memory."""

    def __init__(self, data_dir: pathlib.Path, *,
                 external_roots: Callable[[], Dict[str, str]] = configured_external_roots,
                 measure: Callable[..., StorageMeasurement] = measure_data_folder) -> None:
        self._data_dir = pathlib.Path(data_dir)
        self._external_roots = external_roots
        self._measure = measure
        self._lock = threading.Lock()
        self._measuring = False
        self._measurement: Optional[StorageMeasurement] = None
        self._error: Optional[StorageError] = None

    def projection(self) -> StorageResponse:
        with self._lock:
            return {"measuring": self._measuring, "measurement": self._measurement, "error": self._error}

    def start(self) -> bool:
        """Start a walk unless one is running; False means the caller joined it."""
        with self._lock:
            if self._measuring:
                return False
            self._measuring = True
        try:
            threading.Thread(target=self._run, name="storage-measure", daemon=True).start()
        except RuntimeError:
            self._settle(error="The measurement could not be started.")
            return False
        return True

    def _run(self) -> None:
        try:
            result = self._measure(self._data_dir, external_roots=self._external_roots())
        except StorageUnavailable as exc:
            self._settle(error=str(exc))
        except Exception:
            log.warning("Storage measurement failed", exc_info=True)
            self._settle(error="The measurement failed unexpectedly.")
        else:
            self._settle(result=result)

    def _settle(self, *, result: Optional[StorageMeasurement] = None, error: str = "") -> None:
        with self._lock:
            if result is not None:
                self._measurement, self._error = result, None
            else:
                self._error = {"at": utc_now_iso(), "message": error}
            self._measuring = False


__all__ = [
    "StorageCategory",
    "StorageError",
    "StorageMeasurement",
    "StorageObserver",
    "StorageResponse",
    "StorageUnavailable",
    "measure_data_folder",
]
