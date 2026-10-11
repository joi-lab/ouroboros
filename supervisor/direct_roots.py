"""The supervisor's off-lock fragment of live direct-chat roots.

Pooled roots reach a worker through ``state/queue_snapshot.json``; direct-chat
turns live only in the server process's actor registry, so the main loop
writes this small projection beside the snapshot (owner decision 6C).  Actor
locks are only ever tried (``acquire(blocking=False)``): a turn mid-admission
is skipped and the fragment says so through ONE aggregate ``incomplete`` fact
rather than blocking the loop or fabricating a row.  Queue init takes the
roster over and clears it in the same step, so a stale process's turns never
outlive it and snapshot restore still learns which direct roots the stop caught.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict

from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso
from ouroboros.focus import compact_focus as _compact_focus

log = logging.getLogger(__name__)

FRAGMENT_NAME = pathlib.Path("state") / "direct_roots.json"


def _fragment_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / FRAGMENT_NAME


def direct_turn_facts(turn: Dict[str, Any]) -> Dict[str, Any]:
    """The live turn's suggested name, start and typed origin; absent stays absent."""
    from ouroboros.peer_roster import iso_from_epoch, typed_origin

    facts: Dict[str, Any] = {}
    suggested = str(turn.get("suggested_name") or "").strip()
    if suggested:
        facts["suggested_name"] = suggested
    started = iso_from_epoch(turn.get("_started_at"))
    if started:
        facts["started_at"] = started
    origin = typed_origin(turn)
    if origin:
        facts["origin"] = origin
    return facts


def publish_direct_roots(drive_root: Any) -> Dict[str, Any]:
    """Write the current direct-root rows; never raises, never blocks on an actor."""
    rows = []
    incomplete = False
    try:
        from supervisor.active_activity import get_direct_activity_registry
        from supervisor.workers import direct_chat_turn

        for entry in get_direct_activity_registry().actors():
            lock = getattr(entry.actor, "_owner_message_admission_lock", None)
            if lock is None:
                continue
            if not lock.acquire(blocking=False):
                incomplete = True
                continue
            try:
                turn = direct_chat_turn(entry.activity_id)
            finally:
                lock.release()
            if turn is not None:
                row = {
                    "task_id": str(turn.get("id") or ""),
                    "title": str(turn.get("title") or "").strip(),
                    "chat_id": turn.get("chat_id"),
                    "project_id": str(turn.get("project_id") or ""),
                }
                # Host facts the live actor already holds, carried until the
                # turn's durable result exists (the roster prefers that result).
                row.update(direct_turn_facts(turn))
                focus = _compact_focus(turn.get("focus"))
                if focus is not None:
                    row["focus"] = focus
                rows.append(row)
    except Exception:
        log.debug("direct roots projection failed", exc_info=True)
        incomplete = True
    payload = {"ts": utc_now_iso(), "roots": rows, "incomplete": incomplete}
    try:
        atomic_write_json(_fragment_path(drive_root), payload)
    except Exception:
        log.debug("direct roots fragment write failed", exc_info=True)
    return payload


def clear_direct_roots(drive_root: Any) -> None:
    """An empty fragment: no live direct roots are known for this process yet."""
    try:
        atomic_write_json(_fragment_path(drive_root), {"ts": utc_now_iso(), "roots": [], "incomplete": False})
    except Exception:
        log.debug("direct roots fragment clear failed", exc_info=True)


def take_direct_roots(drive_root: Any) -> Dict[str, Any]:
    """Hand the PREVIOUS process's rows over and clear the fragment in one step.

    Queue init is the moment the roster stops describing anything live, so it is
    also the last moment those ids exist: reading here keeps the clear exactly
    where it was — a stale process's turns never outlive it however the boot
    continues — while snapshot restore still learns which direct roots the stop
    caught.  An ``incomplete`` roster skipped a turn that was mid-admission: the
    rows it DOES list are real and are handed over, the skipped turn keeps the
    projection it has today, and the flag rides along so the restore record
    discloses that gap.  Never raises.
    """
    payload = read_json_dict(_fragment_path(drive_root))
    clear_direct_roots(drive_root)
    if not isinstance(payload, dict):
        return {"task_ids": [], "incomplete": False}
    incomplete = bool(payload.get("incomplete"))
    rows = payload.get("roots")
    task_ids = [] if not isinstance(rows, list) else [
        str(row.get("task_id") or "")
        for row in rows
        if isinstance(row, dict) and str(row.get("task_id") or "")
    ]
    return {"task_ids": task_ids, "incomplete": incomplete}
