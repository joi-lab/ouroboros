"""Dashboard -> Storage -> Temporary space: the owner's free-space action (B).

A pure *preview* lists what the app's own retirers would remove now, in two groups: G1, the work
folders of finished tasks (direct and helper drives whose row is terminal and whose task the
supervisor's liveness probe proves not live), and G2, working copies of finished work (plain
worktrees with the same proof, delegated snapshots by the custody GC's own proof
(``subagent_worktrees.snapshot_released``: task proven over, custody closed and readable), own-body
candidates whose owner is terminal with no live lineage process). The preview changes nothing:
no file under the data root (the task rows are read through the read-only fact reader, never the
drive pass's projection fill), no event row, no settlement call; its sizes come from the storage
count child (``python -m ouroboros.storage --size-paths``, one JSON string per line each way)
with progress and Stop, and the plan is held in memory under an id. A folder the child could
not list keeps an unknown size (``None``, never zero) and the group says so. A *run* frees
exactly the previewed items of the chosen groups through the existing retirers with the wait
set to zero (owner decision Q1 = A; the background pass keeps its days): G1 through
``headless.prune_task_drives``/``prune_headless_task_drives`` (``settle_child_drive`` moves a
proven drive into ``state/custody_trash``; the maintenance child deletes it, asked at once
through ``maintenance.child.request``), G2 through ``subagent_worktrees.prune_orphans``/
``prune_execution_snapshots``, one copy per call with a stop check between calls (each call
decides under the ops lock, as the custody cadence does: the registry row, pin and own-body
verdict logic live there, and a move-aside would need a second row state). It runs on its own
thread and stops between items on the owner's Stop, the server's stop or a closed supervisor
generation (``_closed``), with no reconcile latch; concurrency is the retirers' own locks. A
moved folder counts as removed only once its trash entry is gone, "still being deleted"
meanwhile, and "stuck" once the maintenance child's ``custody_trash`` job recorded that the
entry survived its removal (the child retries it at its next run); free space is read
fresh on every projection. Words (labels, loss sentences) live in the client with the other
storage copy; this module reports ids, counts, sizes and typed reasons.
"""

from __future__ import annotations

import contextlib
import json
import logging
import pathlib
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Literal, Optional

from ouroboros.utils import utc_now_iso

try:  # Python 3.11+
    from typing import TypedDict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - CI supports Python 3.10.
    from typing_extensions import TypedDict  # type: ignore[assignment]

log = logging.getLogger(__name__)

GROUP_WORK_FOLDERS = "work_folders"
GROUP_WORKING_COPIES = "working_copies"
GROUPS = (GROUP_WORK_FOLDERS, GROUP_WORKING_COPIES)
LARGEST_ITEMS = 20
# A retirer's typed reason -> the owner's reason (the client's sentences): results or messages
# still being saved (late mail and call records that moved under the pass included: the next pass
# carries them), in use again, busy right now, the task record or its mailbox unreadable, a restart.
_KEPT_REASONS = {
    "results_pending": ("child_result_unadopted", "child_refs_pending", "call_inventory_pending", "call_inventory_changed",
                        "inputs_uncustodied", "unread_mailbox_uncustodied", "verification_receipts_uncustodied",
                        "canonical_changed", "drive_occupant_changed", "canonical_not_settled", "post_work_open",
                        "artifacts_not_terminal", "child_drive_mismatch"),
    "in_use": ("task_live", "liveness_unknown", "owner_processes_live", "task_not_terminal", "parent_not_terminal"),
    "busy": ("custody_busy", "mailbox_busy", "store_busy"),
    "unreadable_record": ("task_result_unreadable", "unread_mailbox_unreadable"),
    "restart": ("generation_closed",),
}


class StorageFreeItem(TypedDict):
    id: str
    name: str  # the task's title when it has one, else the folder name
    bytes: Optional[int]


class StorageFreeGroup(TypedDict):
    id: str  # work_folders | working_copies
    count: int
    inside_wait: int  # finished less than retention_days ago (the background would still wait)
    in_use: int  # live, pending, unknown or non-terminal: offered nowhere, named here
    unreadable: int  # no readable task record (or unreadable custody): shown, never freed
    bytes: Optional[int]
    bytes_complete: bool
    items: List[StorageFreeItem]  # the largest, at most LARGEST_ITEMS


class StorageFreeKept(TypedDict):
    reason: str  # results_pending | in_use | busy | unreadable_record | restart | other
    count: int


class StorageFreeRun(TypedDict):
    groups: List[str]
    selected: int
    done: int
    removed: int
    removed_bytes: int
    deleting: int  # moved aside, the maintenance child has not deleted them yet
    deleting_bytes: int
    stuck: int  # moved aside and survived the child's removal; retried at the child's next run
    kept: List[StorageFreeKept]
    free_before: Optional[int]
    free_after: Optional[int]
    stopped: bool
    error: Optional[str]


class StorageFree(TypedDict):
    state: Literal["idle", "listing", "ready", "running", "done", "failed", "stopped"]
    plan: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]
    done: int  # folders sized so far (listing) or attempted (running)
    total: int
    groups: List[StorageFreeGroup]
    run: Optional[StorageFreeRun]
    error: Optional[str]


class StorageFreeRequest(TypedDict):
    """``POST /api/storage/free``: the plan id the preview answered with and the groups to free of it."""

    plan: str
    groups: List[str]  # work_folders | working_copies


class _Stopped(Exception):
    """The owner or the generation stopped the preview or the run."""


class _Candidate:
    __slots__ = ("group", "key", "path", "layout", "inside_wait", "bytes", "unreadable", "name")

    def __init__(self, group: str, key: str, path: pathlib.Path, layout: str, *, inside_wait: bool = False) -> None:
        self.group, self.key, self.path, self.layout, self.inside_wait = group, key, path, layout, inside_wait
        self.bytes: Optional[int] = None  # None until sized, and when the folder could not be listed
        self.unreadable = 0  # entries inside it the sizing could not read
        self.name = key


def _free_bytes(root: pathlib.Path) -> Optional[int]:
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None


def _title(root: pathlib.Path, task_id: str) -> str:
    """The task's own title, best effort; the folder name when there is none."""
    try:
        from ouroboros.task_store import read_document

        document = read_document(root, task_id) or {}
        return str(document.get("title") or document.get("suggested_name") or task_id).strip() or task_id
    except Exception:
        return task_id


def _reason(raw: str) -> str:
    raw = str(raw or "")
    if raw.startswith("custody_error"):
        return "unreadable_record" if "ValueError" in raw else "other"
    for reason, codes in _KEPT_REASONS.items():
        if raw in codes:
            return reason
    return "other"


class FreeSpace:
    """The server's one free-space owner: a preview, then a run, one at a time (module docstring).
    ``live`` is the supervisor's liveness probe and ``guard`` its ownership interlock; both resolve
    to the supervisor's own at call time unless a test supplies them."""

    def __init__(self, *, live: Optional[Callable[[str], Optional[bool]]] = None, guard: Any = None,
                 offered: tuple = GROUPS) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._cancel = threading.Event()
        self._live, self._guard, self._offered = live, guard, tuple(offered)
        self._thread: Optional[threading.Thread] = None
        self._process = None
        self._state: str = "idle"
        self._plan: Optional[Dict[str, Any]] = None
        self._run: Optional[Dict[str, Any]] = None
        self._error: Optional[str] = None
        self._progress = {"done": 0, "total": 0}

    # ---- the probe and the interlock ------------------------------------------------------

    def _probe(self) -> Callable[[str], Optional[bool]]:
        if self._live is not None:
            return self._live
        from supervisor.queue import task_settlement_liveness

        return task_settlement_liveness

    def _interlock(self, stop: Callable[[], bool]) -> Any:
        if self._guard is not None:
            return lambda: self._guard(stop=stop)
        from supervisor.queue import task_settlement_interlock

        return lambda: task_settlement_interlock(stop=stop)

    def _closed(self) -> bool:
        """The owner's Stop, the server's stop, or a closed supervisor generation."""
        if self._cancel.is_set() or self._stopped.is_set():
            return True
        try:
            from ouroboros.server_maintenance import _stop_requested

            return bool(_stop_requested())
        except Exception:
            return False

    # ---- the projection ---------------------------------------------------------------------

    def projection(self, data_root: Any) -> StorageFree:
        root = pathlib.Path(data_root)
        with self._lock:
            state, plan, run, error = self._state, self._plan, self._run, self._error
            progress = dict(self._progress)
        groups = [self._group_view(group) for group in (plan or {}).get("groups", [])] if plan else []
        view: StorageFree = {
            "state": state,  # type: ignore[typeddict-item]
            "plan": plan["id"] if plan else None,
            "started_at": (plan or {}).get("started_at"),
            "finished_at": (plan or {}).get("finished_at"),
            "done": progress["done"], "total": progress["total"],
            "groups": groups,
            "run": self._run_view(root, run) if run else None,
            "error": error,
        }
        return view

    @staticmethod
    def _group_view(group: Dict[str, Any]) -> StorageFreeGroup:
        candidates: List[_Candidate] = group["candidates"]
        sized = [item for item in candidates if item.bytes is not None]
        ranked = sorted(sized, key=lambda item: -(item.bytes or 0))[:LARGEST_ITEMS]
        return {
            "id": group["id"], "count": len(candidates),
            "inside_wait": sum(1 for item in candidates if item.inside_wait),
            "in_use": group["in_use"], "unreadable": group["unreadable"],
            "bytes": sum(item.bytes or 0 for item in sized) if sized or not candidates else None,
            "bytes_complete": len(sized) == len(candidates) and not any(item.unreadable for item in sized),
            "items": [{"id": item.key, "name": item.name, "bytes": item.bytes} for item in ranked],
        }

    def _run_view(self, root: pathlib.Path, run: Dict[str, Any]) -> StorageFreeRun:
        """The result as it stands now: a moved folder is removed once its trash entry is gone,
        stuck once the ``custody_trash`` job recorded that this very entry survived its removal."""
        from ouroboros.task_custody import custody_trash_entries

        entries = custody_trash_entries(root) if run["moved"] else []
        survived = self._stuck_trash(root) if entries else {}
        waiting = {name.rsplit("-", 1)[0] for name, identity in entries if survived.get(name) != identity}
        blocked = {name.rsplit("-", 1)[0] for name, identity in entries if survived.get(name) == identity}
        deleting = [key for key in run["moved"] if key.split(":", 1)[-1] in waiting]
        stuck = [key for key in run["moved"] if key not in deleting and key.split(":", 1)[-1] in blocked]
        removed = [key for key in run["moved"] if key not in deleting and key not in stuck] + run["removed"]
        sizes = run["sizes"]
        return {
            "groups": run["groups"], "selected": run["selected"], "done": run["done"],
            "removed": len(removed), "removed_bytes": sum(sizes.get(key) or 0 for key in removed),
            "deleting": len(deleting), "deleting_bytes": sum(sizes.get(key) or 0 for key in deleting),
            "stuck": len(stuck),
            "kept": [{"reason": reason, "count": count} for reason, count in sorted(run["kept"].items()) if count],
            "free_before": run["free_before"],
            "free_after": _free_bytes(root),
            "stopped": run["stopped"], "error": run["error"],
        }

    @staticmethod
    def _stuck_trash(root: pathlib.Path) -> Dict[str, Any]:
        """The ``custody_trash`` job's record of entries that survived its removal (name -> the
        identity it saw); nothing when the row is absent or the store cannot be read now."""
        try:
            from ouroboros import maintenance_jobs

            row = maintenance_jobs.job(root, "custody_trash") or {}
            stuck = row.get("stuck")
            return stuck if isinstance(stuck, dict) else {}
        except Exception:
            return {}

    # ---- lifecycle --------------------------------------------------------------------------

    def _busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def preview(self, data_root: Any, repo_dir: Any) -> bool:
        """Start one preview unless one lists or a run runs; False means the caller joined it."""
        with self._lock:
            if self._stopped.is_set() or self._busy():
                return False
            self._cancel.clear()
            self._state, self._plan, self._run, self._error = "listing", None, None, None
            self._progress = {"done": 0, "total": 0}
            self._thread = threading.Thread(target=self._list, args=(pathlib.Path(data_root), pathlib.Path(repo_dir)),
                                            name="storage-free-preview", daemon=True)
            self._thread.start()
        return True

    def start(self, data_root: Any, repo_dir: Any, plan_id: Any, groups: Any) -> Dict[str, Any]:
        """Free the previewed items of GROUPS under PLAN_ID: ``{"started": bool, "reason": str}``."""
        chosen = [str(group) for group in groups] if isinstance(groups, list) else None
        with self._lock:
            if self._stopped.is_set() or self._busy():
                return {"started": False, "reason": "busy"}
            if self._plan is None or self._state != "ready" or str(plan_id or "") != self._plan["id"]:
                return {"started": False, "reason": "preview_outdated"}
            known = {group["id"] for group in self._plan["groups"]}
            if not chosen or any(group not in known for group in chosen):
                return {"started": False, "reason": "unknown_group" if chosen else "nothing_selected"}
            self._cancel.clear()
            self._state, self._error = "running", None
            self._progress = {"done": 0, "total": sum(len(group["candidates"]) for group in self._plan["groups"]
                                                     if group["id"] in chosen)}
            self._thread = threading.Thread(target=self._free, args=(pathlib.Path(data_root), self._plan, chosen),
                                            name="storage-free-run", daemon=True)
            self._thread.start()
        return {"started": True, "reason": ""}

    def cancel(self) -> bool:
        """Stop the listing (nothing is kept) or the run (what was freed stays freed)."""
        with self._lock:
            if not self._busy():
                return False
            self._cancel.set()
            process = self._process
        if process is not None:
            with contextlib.suppress(OSError):
                process.kill()
        return True

    def stop(self) -> None:
        self._stopped.set()
        self.cancel()

    def live_children(self) -> list:
        process, thread = self._process, self._thread
        if process is not None and process.poll() is None:
            return [process]
        return [thread] if thread is not None and thread.is_alive() else []

    # ---- the preview ------------------------------------------------------------------------

    def _candidates(self, root: pathlib.Path) -> List[Dict[str, Any]]:
        from ouroboros import body_candidate
        from ouroboros.headless import HEADLESS_TASKS_DIR, TASK_DRIVES_DIR, drive_candidates
        from ouroboros.retention import age_cutoff, get_gc_retention_days
        from ouroboros.task_status import FINAL_STATUSES

        live = self._probe()
        cutoff = age_cutoff(get_gc_retention_days())
        groups: List[Dict[str, Any]] = []
        if GROUP_WORK_FOLDERS in self._offered:
            group = {"id": GROUP_WORK_FOLDERS, "candidates": [], "in_use": 0, "unreadable": 0}
            for layout, base in (("headless", root / HEADLESS_TASKS_DIR), ("direct", root / TASK_DRIVES_DIR)):
                if not base.is_dir():
                    continue
                for task_id, status, _at, inside, fact in drive_candidates(base, root, cutoff, self._closed, record=False):
                    if self._closed():
                        raise _Stopped()
                    if not fact:
                        group["unreadable"] += 1  # no readable row: shown, never freed
                    elif status not in FINAL_STATUSES or live(task_id) is not False:
                        group["in_use"] += 1
                    else:
                        group["candidates"].append(_Candidate(GROUP_WORK_FOLDERS, f"{layout}:{task_id}", base / task_id,
                                                              layout, inside_wait=inside))
            groups.append(group)
        if GROUP_WORKING_COPIES in self._offered:
            from ouroboros.subagent_worktrees import list_worktrees, snapshot_released

            group = {"id": GROUP_WORKING_COPIES, "candidates": [], "in_use": 0, "unreadable": 0}
            rows = list_worktrees(data_dir=root)  # the soft reader: a malformed registry lists nothing
            delegated = [row for row in rows if row.get("kind") == "delegated_exec"]
            open_ids: Optional[set] = None
            if delegated:
                try:
                    from ouroboros.delegate_custody import open_snapshot_ids

                    open_ids = open_snapshot_ids(root)
                except Exception:
                    log.debug("Delegated custody unreadable: snapshots stay out of the preview", exc_info=True)
            for row in rows:
                if self._closed():
                    raise _Stopped()
                path = pathlib.Path(str(row.get("path") or ""))
                if not row.get("path") or not path.exists():
                    continue  # nothing on disk to free; the cadence forgets the row
                kind, task_id = str(row.get("kind") or ""), str(row.get("task_id") or "")
                if kind == "delegated_exec":
                    # Offered by the retirer's own proof (D6): a snapshot whose task is not proven
                    # over, or that custody holds, is in use; unreadable custody is shown, never freed.
                    snapshot = str(row.get("snapshot_id") or "")
                    if open_ids is None:
                        group["unreadable"] += 1
                    elif snapshot_released(row, open_ids, data_dir=root, live=live):
                        group["candidates"].append(_Candidate(GROUP_WORKING_COPIES, f"snapshot:{snapshot}", path, "delegated"))
                    else:
                        group["in_use"] += 1
                    continue
                over = body_candidate._recorded_terminal(task_id, root) and not body_candidate.live_lineage_processes(root, task_id)
                if kind != "body_candidate":
                    over = over and live(task_id) is False
                if over:
                    group["candidates"].append(_Candidate(GROUP_WORKING_COPIES, f"{kind or 'plain'}:{task_id}", path, kind or "plain"))
                else:
                    group["in_use"] += 1
            groups.append(group)
        return groups

    def _size(self, root: pathlib.Path, repo_dir: pathlib.Path, candidates: List[_Candidate]) -> None:
        """Size every candidate folder in the count child; each answer lands as it is known."""
        from ouroboros.startup_historical_audit import read_job_lines, spawn_maintenance_job
        from ouroboros.storage import PURPOSE

        by_path = {str(item.path): item for item in candidates}
        with self._lock:
            self._progress = {"done": 0, "total": len(candidates)}
        if not candidates:
            return
        process = spawn_maintenance_job(root, repo_dir, PURPOSE, args=("--size-paths",), stdin=subprocess.PIPE)
        with self._lock:
            self._process = process
        try:
            if self._closed():
                process.kill()
            try:  # one JSON string per line: ASCII on the pipe, the exact path back (storage.main)
                process.stdin.write("".join(f"{json.dumps(path)}\n" for path in by_path).encode("ascii"))
                process.stdin.close()
            except (OSError, ValueError):
                pass

            def on_line(raw: bytes) -> None:
                try:
                    line = json.loads(raw)
                except ValueError:
                    return
                if not isinstance(line, dict):
                    return
                if line.get("event") == "size" and str(line.get("path")) in by_path:
                    item, size = by_path[str(line["path"])], line.get("bytes")
                    item.bytes = size if isinstance(size, int) and not isinstance(size, bool) else None
                    item.unreadable = line.get("unreadable") if isinstance(line.get("unreadable"), int) else 0
                    with self._lock:
                        self._progress["done"] += 1
                elif "status" in line and line.get("status") != "finished":
                    raise RuntimeError(str(line.get("message") or "The sizes could not be read."))

            exit_code = read_job_lines(process, on_line, lambda proc: proc.kill())
        finally:
            with self._lock:
                self._process = None
        if self._closed():
            raise _Stopped()
        if exit_code != 0:
            raise RuntimeError("The sizes could not be read.")

    def _list(self, root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        started = utc_now_iso()
        try:
            groups = self._candidates(root)
            plan = {"id": uuid.uuid4().hex[:12], "started_at": started, "finished_at": None, "groups": groups}
            with self._lock:
                self._plan = plan  # the counts appear at once; the sizes fill in while the state stays listing
            candidates = [item for group in groups for item in group["candidates"]]
            self._size(root, repo_dir, candidates)
            for group in groups:
                for item in sorted(group["candidates"], key=lambda item: -(item.bytes or 0))[:LARGEST_ITEMS]:
                    item.name = _title(root, item.key.split(":", 1)[-1]) if item.layout != "delegated" else item.path.name
            with self._lock:
                self._plan, self._state = {**plan, "finished_at": utc_now_iso()}, "ready"
        except _Stopped:
            with self._lock:
                self._state, self._plan = "idle", None
        except Exception as exc:
            log.warning("Storage free preview failed", exc_info=True)
            with self._lock:
                self._state, self._plan = "failed", None
                self._error = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}"

    # ---- the run ----------------------------------------------------------------------------

    def _free(self, root: pathlib.Path, plan: Dict[str, Any], chosen: List[str]) -> None:
        from ouroboros.store import supervisor_row

        started = time.monotonic()
        run: Dict[str, Any] = {"groups": chosen, "selected": 0, "done": 0, "moved": [], "removed": [], "kept": {},
                               "sizes": {}, "free_before": _free_bytes(root),
                               "stopped": False, "error": None}
        with self._lock:
            self._run = run
        try:
            for group in plan["groups"]:
                if group["id"] not in chosen:
                    continue
                run["selected"] += len(group["candidates"])
                run["sizes"].update({item.key: item.bytes for item in group["candidates"]})
                if group["id"] == GROUP_WORK_FOLDERS:
                    self._free_drives(root, group["candidates"], run)
                else:
                    self._free_copies(root, group["candidates"], run)
        except _Stopped:
            run["stopped"] = True
        except Exception as exc:
            log.warning("Storage free run failed", exc_info=True)
            run["error"] = f"{type(exc).__name__}"
        with self._lock:
            self._state = "stopped" if run["stopped"] else "failed" if run["error"] else "done"
            self._plan = {**plan, "finished_at": utc_now_iso()}
        view = self._run_view(root, run)
        self._record(root, view, time.monotonic() - started)
        supervisor_row(root, "storage_free", {"phase": self._state, "duration_seconds": round(time.monotonic() - started, 2),
                                               **{key: view[key] for key in ("groups", "selected", "removed", "removed_bytes",
                                                                             "deleting", "stuck", "kept", "free_before", "free_after")}})

    @staticmethod
    def _record(root: pathlib.Path, view: StorageFreeRun, seconds: float) -> None:
        try:
            from ouroboros.server_maintenance import _prune_event

            _prune_event("storage_free", ("removed", "deleting", "kept"), report={**view, "duration_seconds": round(seconds, 2)})
        except Exception:
            log.debug("storage_free event not recorded", exc_info=True)

    def _tick(self, run: Dict[str, Any], count: int) -> None:
        with self._lock:
            run["done"] = count
            self._progress["done"] = count

    def _free_drives(self, root: pathlib.Path, candidates: List[_Candidate], run: Dict[str, Any]) -> None:
        from ouroboros import maintenance
        from ouroboros.headless import prune_headless_task_drives, prune_task_drives

        live, stop = self._probe(), self._closed
        guard = self._interlock(stop)
        by_layout: Dict[str, Dict[str, _Candidate]] = {"headless": {}, "direct": {}}
        for item in candidates:
            by_layout[item.layout][item.key.split(":", 1)[1]] = item
        attempted = run["done"]
        for layout, prune in (("headless", prune_headless_task_drives), ("direct", prune_task_drives)):
            selected = by_layout[layout]
            if not selected:
                continue
            if stop():
                raise _Stopped()
            counted = [0]

            def progress(report: Dict[str, Any]) -> None:
                counted[0] = len(report["pruned"]) + len(report["skipped"]) + len(report["errors"])
                self._tick(run, attempted + counted[0])

            report = prune(root, retention_days=0, only=list(selected), live=live, guard=guard, stop=stop, budget=None,
                           progress=progress)
            attempted += len(report["pruned"]) + len(report["skipped"]) + len(report["errors"])
            self._tick(run, attempted)
            reached = set()
            for row in report["pruned"]:
                reached.add(row["task_id"])
                run["moved"].append(selected[row["task_id"]].key)
            for row in report["skipped"]:
                item = selected.get(row["task_id"])
                if item is None:
                    continue
                reached.add(row["task_id"])
                if not item.path.exists():
                    run["moved"].append(item.key)  # the cadence settled it first; its trash may still exist
                else:
                    run["kept"][_reason(row.get("reason"))] = run["kept"].get(_reason(row.get("reason")), 0) + 1
            for row in report["errors"]:
                item = selected.get(row["task_id"])
                if item is None:
                    continue
                reached.add(row["task_id"])
                if not item.path.exists():
                    run["moved"].append(item.key)
                else:
                    reason = "unreadable_record" if "ValueError" in str(row.get("error")) else "other"
                    run["kept"][reason] = run["kept"].get(reason, 0) + 1
            if report["deferred"]:
                raise _Stopped()
            for task_id, item in selected.items():
                if task_id in reached:
                    continue  # the pruner's own verdict stands
                attempted += 1
                if not item.path.exists():
                    run["moved"].append(item.key)  # gone before listing; follow the cadence's trash too
                else:
                    run["kept"]["other"] = run["kept"].get("other", 0) + 1
            self._tick(run, attempted)
        if run["moved"]:
            maintenance.child.request()  # the moved drives' deletion is the maintenance child's job

    def _free_copies(self, root: pathlib.Path, candidates: List[_Candidate], run: Dict[str, Any]) -> None:
        """One retirer call per copy, so Stop lands between copies and each ops-lock hold covers
        one checkout; a stop leaves the remaining copies as they were."""
        from ouroboros.subagent_worktrees import prune_execution_snapshots, prune_orphans

        live, stop = self._probe(), self._closed
        open_ids: Optional[set] = None
        if any(item.layout == "delegated" for item in candidates):
            try:
                from ouroboros.delegate_custody import open_snapshot_ids

                open_ids = open_snapshot_ids(root)
            except Exception:
                open_ids = None  # unreadable custody: no snapshot is freed
        for item in candidates:
            if stop():
                raise _Stopped()
            task_id = item.key.split(":", 1)[1]
            if item.layout == "delegated":
                if open_ids is not None:
                    prune_execution_snapshots(open_ids, data_dir=root, only=[task_id], live=live)
            else:
                prune_orphans(data_dir=root, retention_days=0, live=live, only=[task_id])
            if not item.path.exists():
                run["removed"].append(item.key)
            else:
                reason = "in_use" if item.layout != "delegated" and live(task_id) is not False else "other"
                run["kept"][reason] = run["kept"].get(reason, 0) + 1
            self._tick(run, run["done"] + 1)


free = FreeSpace()


__all__ = [
    "GROUPS",
    "GROUP_WORKING_COPIES",
    "GROUP_WORK_FOLDERS",
    "FreeSpace",
    "StorageFree",
    "StorageFreeGroup",
    "StorageFreeItem",
    "StorageFreeKept",
    "StorageFreeRequest",
    "StorageFreeRun",
    "free",
]
