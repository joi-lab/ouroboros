"""Retire review checking copies whose review is over; the same pass migrates older copies.

A copy goes only on positive evidence that its run ended: a receipt of its operation, or
custody, read fresh for that seat, showing that a run started in this copy settled and none
still runs or is pending there. Each seat is decided under its cross-process lock, which
workers also take to prepare, refresh and capture it, so a review that starts or ends during
a pass is never deleted under itself. A thread's copy lives while its task is live. A failed
capture (counted from its receipt), a seat whose run never started and a seat without a
marker go once older than the GC retention period; a refresh's staging goes once its seat's
lock is free, and an older body's staging inside a seat at once (a pristine copy). A run
whose capture saved nothing keeps its copy. Evidence and markers stay.

The views pass runs once per process (``views_since``: when the process began), under the
lock (``.views.lock`` beside ``views/``) a freeze takes to reuse or publish a view. It removes
discarded views, and a view that no seat holding a copy needs and whose last use predates the
process, when a finished seat names it or it is older than the retention period. It does not
run after a pass that stopped early, found a seat busy or could not read a seat used within
the retention period.

The same pass retires an isolated checkout (``state/review_checkouts/<token>/``, made by
``review_subject.isolated_checkout``) that its wave kept for open custody or that a crash left,
once no review can still read it: its task is terminal and not live, no active review custody
(``state/advisory_review.json``) names its round, no live review operation runs for the task and
no seat that still holds a copy checks from it. Its owner is the task its ``checkout.json`` names;
a checkout made before the marker is owned only through a recorded round of that state, and goes
only once older than the retention period. Targeted ``git worktree remove`` retires only this
checkout's registration. A rejoin rebuilds a retired checkout at its frozen tree. An unreadable
review state or a busy or unreadable seat keeps every such checkout; an unreadable marker or task
row keeps its own. Isolated-checkout reuse and retirement share no per-token lock:
a rejoin starting after the custody judgment can reuse the tree and then lose it
during removal. The pass lock only serializes pruners; the seat/view locks above
protect other objects. Rebuilding after completed retirement does not close that race.
"""
from __future__ import annotations

import contextlib
import json
import pathlib
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from ouroboros.review_checking import (
    VIEWS_LOCK_NAME,
    _RECEIPTED,
    _capture_locked,
    _checking_area_present,
    _receipt,
    _receipt_path,
    _release_area,
    _seat_locked,
    seat_dir,
)

_PASS_LOCK = threading.Lock()


def _last_use(path: pathlib.Path) -> float:
    """ctime too: a copy's mtime is its source's (copystat); a reuse touches a view."""
    try:
        info = path.lstat()
    except OSError:
        return float("inf")  # unknown counts as recent
    return max(info.st_mtime, info.st_ctime)


def _view_containers(views: pathlib.Path, source: Mapping[str, Any]) -> set:
    """The ``views/`` entries a frozen source names (its folder view and evidence views)."""
    paths = [pathlib.Path(str(path)).resolve(strict=False)
             for path in [source.get("view"), *(source.get("evidence_views") or {}).values()] if path]
    return {views / path.relative_to(views).parts[0] for path in paths if path.is_relative_to(views) and path != views}


def _started_after(started: Any, since: Any) -> bool:
    try:
        return datetime.fromisoformat(str(started)) >= datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return False  # an unknown start proves nothing about this copy


def _copy_runs_over(data_root: Any, task_id: str, area: pathlib.Path, created_at: Any) -> Optional[bool]:
    """From one custody read: False while a run may still execute in ``area`` (unsettled or
    pending), True when a run started in this copy (after ``created_at``) settled, None when no
    run proves anything. A thread's earlier turns ran in the same folder and prove nothing."""
    from ouroboros import delegate_custody_current as current

    snapshot = current.task_snapshot(data_root, task_id)
    target = str(area.resolve(strict=False))

    def names(root: Any) -> bool:
        return bool(root) and str(pathlib.Path(str(root)).resolve(strict=False)) == target

    runs = [entry for entry in snapshot["runs"].values() if names(entry.execution_root)]
    if any(names(row.get("execution_root")) for row in snapshot["pending"]) or not all(
            entry.settled for entry in runs):
        return False
    return True if any(_started_after(entry.started_ts, created_at) for entry in runs) else None


def _checkout_entries(base: pathlib.Path) -> list:
    """The ``<token>/`` folders ``review_subject.isolated_checkout`` makes (not ``seats/``, ``views/`` or dot names)."""
    try:
        return [entry for entry in sorted(base.iterdir()) if entry.name not in {"seats", "views"}
                and not entry.name.startswith(".") and not entry.is_symlink() and entry.is_dir()]
    except OSError:
        return []


def _review_rounds(data_root: Any) -> tuple:
    """``(active, owners)`` from the one review state (``state/advisory_review.json``): the checkout
    names of rounds whose custody is still active, and every recorded round's task by its checkout
    name. A state that cannot be read raises: no checkout is judged without it."""
    from ouroboros.review_state import _load_state_unlocked
    from ouroboros.tools.review_subject import checkout_token

    state = _load_state_unlocked(pathlib.Path(data_root), strict_attempt_authority=True)

    def name(attempt: Any) -> str:
        return checkout_token(attempt.review_retry_key, task_id=attempt.task_id)

    return ({name(row) for row in state.get_active_attempts() if row.review_retry_key},
            {name(row): str(row.task_id) for row in state.attempts if row.review_retry_key and row.task_id})


def _checkout_hold(data_root: Any, entry: pathlib.Path, *, rounds: tuple, sources: set,
                   live: Callable[[], Any], cutoff: float) -> str:
    """Why the isolated checkout ENTRY stays, or ``""`` when no review can still read it."""
    from ouroboros.review_operation import task_has_live_review_operation
    from ouroboros.task_results import load_task_result
    from ouroboros.task_status import SETTLED_STATUSES
    from ouroboros.tools.review_subject import CHECKOUT_MARKER

    active, owners = rounds
    try:
        marker = json.loads((entry / CHECKOUT_MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        marker = None  # made before the marker: owned only through a recorded round, and only past the wait
    if marker is not None and (not isinstance(marker, dict) or marker.get("schema_version") != 1):
        return "marker_unreadable"
    task_id = str(marker.get("task_id") or "") if marker is not None else owners.get(entry.name, "")
    repo = (entry / "repo").resolve(strict=False)
    if not task_id:
        return "owner_unknown"
    if entry.name in active:
        return "review_custody_active"
    if any(root == repo or root.is_relative_to(repo) for root in sources):
        return "seat_checking"
    if marker is None and _last_use(entry) >= cutoff:
        return "younger_than_retention"
    row = load_task_result(data_root, task_id, strict=True) or {}
    if str(row.get("status") or "") not in SETTLED_STATUSES:
        return "task_not_terminal"
    if task_id in live():
        return "task_live"
    if task_has_live_review_operation(data_root, task_id, result_loader=lambda tid: row if tid == task_id else (
            load_task_result(data_root, tid, strict=True) or {})):
        return "operation_live"
    return ""


def _checkout_registration_gone(repo: pathlib.Path) -> bool:
    """A deleted subject leaves a Git pointer to an absent registration; unreadable is not absent."""
    pointer = (repo / ".git").read_text(encoding="utf-8").strip()
    if not pointer.startswith("gitdir: ") or not pointer[8:]:
        return False
    try:
        (repo / pointer[8:]).stat()
    except FileNotFoundError:
        return True
    return False


def _remove_checkout(entry: pathlib.Path) -> str:
    """Remove this checkout after Git unregisters it or its registration is already gone."""
    from ouroboros.subagent_worktrees import _force_rmtree
    from ouroboros.tools.review_subject import _git_bytes

    repo, common = entry / "repo", None
    if (repo / ".git").is_file():
        rc, out, err = _git_bytes(repo, ["rev-parse", "--git-common-dir"])
        text = out.decode("utf-8", "replace").strip()
        common = (repo / text).resolve(strict=False) if rc == 0 and text else None
        if common is None or not common.is_dir():
            if not _checkout_registration_gone(repo):
                return f"{entry.name}: Git common directory unavailable: {err.strip()}"
        else:
            rc, _out, err = _git_bytes(common, ["--git-dir", str(common), "worktree", "remove", "--force", str(repo)])
            if rc:
                return f"{entry.name}: Git worktree removal failed: {err.strip()}"
    _force_rmtree(entry)
    if entry.exists() or entry.is_symlink():
        return f"{entry.name}: the checkout survived its removal"
    return ""


def _retire_checkouts(data_root: Any, entries: list, live: Callable[[], Any], cutoff: float, sources: set,
                      stopped: Callable[[], bool], report: Dict[str, Any]) -> None:
    """Judge and remove the isolated checkouts ENTRIES; ``sources`` are the roots kept seats check from."""
    try:
        rounds = _review_rounds(data_root)
    except Exception as exc:  # unknown custody: every checkout stays
        report["errors"].append(f"review state unreadable: {type(exc).__name__}: {exc}")
        return
    for entry in entries:
        if stopped():
            return
        try:
            hold = _checkout_hold(data_root, entry, rounds=rounds, sources=sources, live=live, cutoff=cutoff)
            error = "" if hold else _remove_checkout(entry)
        except Exception as exc:  # an unreadable marker, row or tree keeps the checkout
            hold, error = "", f"{entry.name}: {type(exc).__name__}: {exc}"
        if error:
            report["errors"].append(error)
        elif hold:
            report["checkouts_kept"] += 1
        else:
            report["checkouts_removed"] += 1


def prune_review_checkouts(data_root: Any, live_task_ids: Any, *, views_since: Optional[float] = None,
                           retention_days: Optional[int] = None, now: Optional[float] = None,
                           stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """One pass over ``state/review_checkouts`` (see the module docstring).

    ``live_task_ids`` is a set or a callable asked again for each thread seat; ``stop`` is
    asked before each seat. ``views_done`` reports a completed views pass."""
    from ouroboros import delegate_custody_current as current
    from ouroboros.retention import age_cutoff, get_gc_retention_days

    base = (pathlib.Path(data_root) / "state" / "review_checkouts").resolve(strict=False)
    seats, view_root = base / "seats", base / "views"
    views = views_since is not None
    report: Dict[str, Any] = {"released": 0, "removed": 0, "kept": 0, "busy": 0, "capture_failed": 0,
                              "checkouts_removed": 0, "checkouts_kept": 0, "errors": []}
    checkouts = _checkout_entries(base)
    if not (seats.is_dir() or checkouts or views and view_root.is_dir()):
        return {**report, "views_done": views}
    if not _PASS_LOCK.acquire(blocking=False):
        return {**report, "skipped": "pass_in_progress"}
    try:
        if current.unreadable(data_root):
            return {**report, "skipped": "custody_unreadable"}
        live = live_task_ids if callable(live_task_ids) else (lambda: live_task_ids)
        stopped = stop or (lambda: False)
        cutoff = age_cutoff(get_gc_retention_days() if retention_days is None else retention_days, now)
        named, kept_views, unread, busy, sources = set(), set(), [], [], set()

        def remove(path: pathlib.Path) -> None:
            if not _checking_area_present(path):
                return
            error = _release_area(path, {}).get("cleanup_error")
            if error:
                report["errors"].append(error)
            else:
                report["removed"] += 1

        def retire(home: pathlib.Path, area: pathlib.Path) -> bool:
            """Under the seat lock: True keeps the copy (and the views it names)."""
            marker = json.loads((home / "checking.json").read_text(encoding="utf-8"))
            task_id, operation = str(marker.get("task_id") or ""), str(marker.get("operation_id") or "")
            thread = str(marker.get("thread_seat") or "")
            thread_home = bool(thread) and seat_dir(data_root, task_id, f"thread:{thread}").resolve(
                strict=False) == home
            over = _copy_runs_over(data_root, task_id, area, marker.get("created_at"))
            if over is False or thread_home and task_id in live():
                report["kept"] += 1
                return True
            receipt = _receipt(data_root, task_id, operation) if operation else {}
            if over and operation and receipt.get("status") not in (*_RECEIPTED, "capture_failed"):
                _capture_locked(data_root, task_id, operation)  # the run ended unobserved
                receipt = _receipt(data_root, task_id, operation)  # only a durable receipt counts
                if not receipt:  # nothing was saved (a full volume): the copy waits for a later pass
                    report["capture_failed"] += 1
                    return True
            if receipt.get("status") in _RECEIPTED:
                receipt = _release_area(area, receipt)
                if receipt.get("cleanup_error"):
                    report["errors"].append(receipt["cleanup_error"])
                    return True
                report["released"] += 1
                return False
            # A failed capture counts from its receipt; a run that never started from the marker.
            clock = _receipt_path(data_root, task_id, operation) if receipt else home / "checking.json"
            if _last_use(clock) < cutoff:
                remove(area)
                return False
            report["capture_failed" if receipt else "kept"] += 1
            return True

        def staging(entry: pathlib.Path) -> None:
            """``.next-<home>.<nonce>``: a refresh holds its home's lock while it builds one."""
            with _seat_locked(seats / entry.name[len(".next-"):].rsplit(".", 1)[0], wait=False) as held:
                if held:
                    remove(entry)

        def seat(home: pathlib.Path) -> set:
            """One seat home; returns the views it still needs."""
            area = home / "area"
            if home.is_symlink():  # never followed; it names no copy this pass could own
                if views:
                    report["errors"].append(f"{home.name}: the seat home is a link")
                return set()
            if not views and not _checking_area_present(area):
                return set()  # a periodic pass reads only seats that still hold a copy
            with _seat_locked(home, wait=False) as held:  # read under it: Windows refuses to replace an open marker
                if not held:  # a worker is changing it; the views it needs are read in a later pass
                    report["busy"] += 1
                    busy.append(home.name)
                    return set()
                try:
                    source = json.loads((home / "checking.json").read_text(encoding="utf-8")).get("source") or {}
                except FileNotFoundError:
                    if _last_use(area) < cutoff:  # a materialization that never wrote its marker
                        remove(area)
                    for leftover in home.glob(".next-*"):  # an older body's staging: never a reviewer's work
                        remove(leftover)
                    return set()
                except (OSError, ValueError, AttributeError):  # AttributeError: JSON that is not an object
                    if _last_use(home / "checking.json") < cutoff:  # torn longer than retention
                        if views:
                            report["errors"].append(f"{home.name}: checking marker unreadable")
                        return set()
                    raise
                views_of = _view_containers(view_root, source)
                named.update(views_of)
                try:
                    for leftover in home.glob(".next-*"):  # an older body's interrupted thread refresh
                        remove(leftover)
                    if not _checking_area_present(area):
                        return set()
                    kept = retire(home, area)
                except Exception as exc:  # its views are known and stay; the views pass need not wait for it
                    report["errors"].append(f"{home.name}: {type(exc).__name__}: {exc}")
                    kept = True
                if kept and source.get("root"):  # a copy that stays may still read the checkout it came from
                    sources.add(pathlib.Path(str(source["root"])).resolve(strict=False))
                return views_of if kept else set()

        for home in sorted(seats.iterdir()) if seats.is_dir() else ():
            if stopped():
                return {**report, "skipped": "stopped"}
            try:
                if home.name.startswith(".next-"):
                    staging(home)
                elif home.is_symlink() or home.is_dir():  # a stray file (.DS_Store) is not a seat
                    kept_views |= seat(home)
            except Exception as exc:  # a seat whose views are unknown never stops the pass; it keeps every view
                unread.append(home.name)
                report["errors"].append(f"{home.name}: {type(exc).__name__}: {exc}")
        if checkouts and not unread and not busy and not stopped():
            _retire_checkouts(data_root, checkouts, live, cutoff, sources, stopped, report)
        if views and not unread and not stopped():
            doomed = []
            with _seat_locked(base, wait=False, name=VIEWS_LOCK_NAME) if view_root.is_dir() else contextlib.nullcontext(
                    True) as held:
                if not held or busy:
                    return {**report, "skipped": "views_busy"}
                for view in sorted(view_root.iterdir()) if view_root.is_dir() else ():
                    if view in kept_views or not (view.is_symlink() or view.is_dir()):  # a stray file is no view
                        continue
                    if view.name.startswith(".discarded-"):
                        doomed.append(view)
                    elif _last_use(view) < views_since and (view in named or _last_use(view) < cutoff):
                        aside = view_root / f".discarded-{view.name.lstrip('.')}.{uuid.uuid4().hex[:8]}"
                        try:
                            view.rename(aside)  # gone at once for a freeze that takes the lock next
                        except OSError as exc:  # held open on Windows: the next process start retries it
                            report["errors"].append(f"{view.name}: {type(exc).__name__}: {exc}")
                            continue
                        doomed.append(aside)
            for view in doomed:
                remove(view)
            report["views_done"] = True
        return report
    except Exception as exc:  # housekeeping never breaks startup or maintenance
        return {**report, "errors": [*report["errors"], f"{type(exc).__name__}: {exc}"]}
    finally:
        _PASS_LOCK.release()
