"""What a stop may retain: the ONE predicate every shutdown consumer shares.

Manual Restart, Panic, graceful shutdown and a crash-storm kill all end this
server generation's processes. None of them may turn a SAVED pause into a
terminal, and the owner's manual Restart additionally keeps the never-started
queue HELD instead of cancelling it (owner Batch4 7A: "tasks on Pause stay on
Pause across Restart"). The Restart's cancel census, ``kill_workers``'s running
and pending handling, the snapshot restore and the unused-grant revocation all
read the facts below, so no consumer can disagree about which task is a saved
pause — the census that skipped a paused id and the kill that then terminalized
it was exactly the failure this module exists to prevent.

The durable pause row on the task result stays the authority (``budget_pause``
row, whatever its ``reason``). A hash-validated warm ``owner_wait`` sleep can
transfer to that owner atomically, marking its old wait ``retained``. This
adds no store or scheduler; quiz/review waits never transfer by analogy.

- ``saved``: a stored continuation source for the live attempt (``pausing``
  after quiescence, or ``paused``) and no active cancel intent — Stop wins.
- ``unused_grant``: a Resume grant the loop never consumed over such a source;
  the stop revokes it back to the pause (the next Resume mints a fresh one).
- ``request_only``: ``pausing`` without a source — pause INTENT that never
  reached its checkpoint. The stop interrupts it; it is never an exact Resume.
- ``""``: anything else, including a consumed grant (running work) and an
  unreadable row (no pause is invented from an unknown).

A never-started queued row the owner's Restart catches is held, not cancelled,
under the typed ``owner_restart_hold`` on the queue's existing non-dispatch
hold carrier (``events_budget.BUDGET_HOLD_KEY``); only an explicit Resume
releases it. Other shutdown doors keep their own queue policy — this rule is
not extended to them by inference — but a row an earlier owner Restart already
held stays held through them, like a saved pause.
"""

from __future__ import annotations

import logging
import pathlib
import time
from typing import Any, Dict, Iterable, List, Optional

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

RETAIN_SAVED = "saved"
RETAIN_UNUSED_GRANT = "unused_grant"
PAUSE_REQUEST_ONLY = "request_only"
RETAIN_SLEEP = "saved_sleep"
HOLD_SAVED_SLEEP_RECOVERY = "saved_sleep_recovery"


def saved_sleep_hold_reason(result_root: Any, *, owner_restart: bool = False) -> str:
    """Read the existing stop carriers; an ordinary recovery asserts no owner action.

    Kill callers read this only after signalling workers. The explicit Restart
    door also supplies its known intent, independent of a flag read.
    """
    from supervisor.events_budget import HOLD_OWNER_RESTART, HOLD_PANIC

    state = pathlib.Path(result_root) / "state"
    if owner_restart or (state / "owner_restart_no_resume.flag").exists():
        return HOLD_OWNER_RESTART
    try:
        if (state / "panic_stop.flag").read_text(encoding="utf-8").strip() == "panic":
            return HOLD_PANIC
    except OSError:
        pass
    return HOLD_SAVED_SLEEP_RECOVERY


def pause_retention(result_root: Any, task_id: str, attempt: Optional[int] = None) -> str:
    """Classify ``task_id``'s durable pause row for a stop (see module docstring)."""
    from ouroboros.budget_pause import (
        STATE_PAUSED, STATE_PAUSING, STATE_RESUME_GRANTED, budget_pause_row,
    )

    try:
        from ouroboros.owner_wait import saved_sleep_checkpoint

        wait, _state = saved_sleep_checkpoint(pathlib.Path(result_root), str(task_id), attempt)
        if wait and not _cancel_intent_active(result_root, task_id):
            return RETAIN_SLEEP
    except Exception:
        log.debug("Warm sleep source unreadable for %s at a stop", task_id, exc_info=True)
    try:
        row = budget_pause_row(pathlib.Path(result_root), str(task_id))
    except Exception:
        log.debug("Pause row unreadable for %s at a stop", task_id, exc_info=True)
        return ""
    if not row:
        return ""
    state = str(row.get("state") or "")
    if attempt is not None and int(row.get("task_attempt") or 0) != int(attempt):
        return ""
    if not row.get("source_ref"):
        return PAUSE_REQUEST_ONLY if state == STATE_PAUSING else ""
    if _cancel_intent_active(result_root, task_id):
        return ""
    if state in {STATE_PAUSING, STATE_PAUSED}:
        return RETAIN_SAVED
    grant = row.get("grant") if isinstance(row.get("grant"), dict) else {}
    if state == STATE_RESUME_GRANTED and not grant.get("consumed_at"):
        return RETAIN_UNUSED_GRANT
    return ""


def _cancel_intent_active(result_root: Any, task_id: str) -> bool:
    """An explicit Stop outranks a saved pause; an unreadable authority is NOT a Stop."""
    try:
        from ouroboros.cancel_intents import has_active_intent

        return bool(has_active_intent(pathlib.Path(result_root), str(task_id), strict=True))
    except Exception:
        log.warning("Cancel authority unreadable for %s at a stop; its saved pause is retained",
                    task_id, exc_info=True)
        return False


def census_without_saved_pauses(task_ids: Iterable[str], result_root: Any) -> List[str]:
    """The Restart cancel census minus every id whose pause is already saved.

    A pooled task whose checkpoint is stored but whose park event has not been
    processed yet, and a direct actor that has not released its registry entry,
    are both still "live" to the census; a cancel intent minted for them would
    outrank their saved pause at restore and cancel it.
    """
    return [task_id for task_id in task_ids
            if pause_retention(result_root, task_id) not in {RETAIN_SAVED, RETAIN_UNUSED_GRANT, RETAIN_SLEEP}]


def park_saved_pause(task: Dict[str, Any], attempt: int, result_root: Any, *,
                     pause_source: str, sleep_hold_reason: str = HOLD_SAVED_SLEEP_RECOVERY) -> Optional[Dict[str, Any]]:
    """Return ``task`` as a PENDING row under its exact pause marker, or ``None``.

    Shared by ``kill_workers`` (a RUNNING row whose checkpoint landed before its
    park event) and the snapshot restore (such a row found in the snapshot's
    running list). A ``pausing`` row is confirmed ``paused`` (its source already
    exists; a failed confirmation leaves it ``pausing``, still retained); an
    unused grant is revoked first, and a revocation that cannot be written
    keeps the row parked under a typed hold. Nothing here is a terminal.
    """
    from ouroboros.budget_pause import (
        STATE_PAUSED, STATE_PAUSING, STATE_RESUME_GRANTED, budget_pause_row, exact_pause_marker,
        set_budget_pause,
    )
    from supervisor.events_budget import HOLD_RESTART_REVOCATION_UNWRITTEN, hold_budget_row

    task_id = str(task.get("id") or "")
    result_root = pathlib.Path(result_root)
    retention = pause_retention(result_root, task_id, attempt)
    if retention not in {RETAIN_SAVED, RETAIN_UNUSED_GRANT, RETAIN_SLEEP}:
        return None
    try:
        if retention == RETAIN_SLEEP:
            pause = retain_sleep_checkpoint(result_root, task_id, attempt,
                                            direct=bool(task.get("_is_direct_chat")))
        else:
            pause = budget_pause_row(result_root, task_id)
    except Exception:
        log.warning("Saved sleep retention could not be published for %s", task_id, exc_info=True)
        # Keep the validated locator for a later stop/restore retry. Never
        # terminalize a checkpoint merely because its conversion write failed.
        parked = dict(task)
        hold_after_stop(parked, result_root, sleep_hold_reason)
        return parked
    pause_id = str(pause.get("pause_id") or "")
    grant = pause.get("grant") if isinstance(pause.get("grant"), dict) else {}
    held_reason = ""
    if pause.get("state") == STATE_RESUME_GRANTED:
        if grant.get("revoked_at"):
            return None
        revoked = {**grant, "revoked_at": utc_now_iso(), "revoke_reason": "restart_before_consumption"}
        try:
            set_budget_pause(result_root, task_id, {**pause, "state": STATE_PAUSED, "grant": revoked},
                             expected_pause_id=pause_id, expected_state=STATE_RESUME_GRANTED,
                             expected_grant_id=str(grant.get("grant_id") or ""))
            pause = {**pause, "state": STATE_PAUSED, "grant": revoked}
        except Exception:
            log.warning("Stop could not revoke the unconsumed grant of %s; parked under a hold",
                        task_id, exc_info=True)
            held_reason = HOLD_RESTART_REVOCATION_UNWRITTEN
    elif pause.get("state") == STATE_PAUSING and pause.get("settlement") != "external_writers_running":
        # (An owner Pause over sent work still running stays ``pausing``: saved,
        # retained, but never confirmed as a clean Paused by a stop.)
        try:
            set_budget_pause(result_root, task_id, {**pause, "state": STATE_PAUSED,
                                                    "paused_confirmed_at": time.time(),
                                                    "pause_source": pause_source},
                             expected_pause_id=pause_id, expected_state=STATE_PAUSING)
        except Exception:
            log.warning("Parked pausing row %s stays 'pausing' (row unwritable at the stop)",
                        task_id, exc_info=True)
    parked = dict(task)
    parked["_attempt"] = int(attempt)
    parked.pop("_budget_pause_resume", None)
    parked.pop("_owner_wait_resume", None)
    parked["_budget_pause"] = exact_pause_marker(pause, default_root=str(task.get("root_task_id") or task_id))
    if retention == RETAIN_SLEEP:
        hold_after_stop(parked, result_root, sleep_hold_reason)
    if held_reason:
        hold_budget_row(
            parked, reason=held_reason,
            detail="a stop found an unconsumed Resume grant whose revocation could not be written",
            extra={"pause_id": pause_id, "grant_id": str(grant.get("grant_id") or ""),
                   "root_task_id": str(task.get("root_task_id") or task_id)},
            result_root=result_root)
    return parked


def retain_sleep_checkpoint(root: pathlib.Path, task_id: str, attempt: int, *, direct: bool = False) -> Dict[str, Any]:
    """Move a validated warm sleep to the existing single-use exact pause owner.

    Conversion is CAS-bound to the full owner-wait row. The original source is
    retained; its cognition is copied unchanged with the new pause identity.
    """
    import json
    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.owner_wait import saved_sleep_checkpoint
    from ouroboros.budget_pause import budget_pause_row, set_budget_pause, STATE_PAUSED, RAIL_MODEL_SLEEP

    wait, state = saved_sleep_checkpoint(root, task_id, attempt)
    if not wait:
        raise ValueError("saved_sleep_checkpoint_unavailable")
    old = budget_pause_row(root, task_id)
    pause_id = "sleep-" + wait["wait_id"]
    state = {**state, "pause_id": pause_id, "reason": "sleep", "rail": RAIL_MODEL_SLEEP, "scope": "task"}
    source = store_actor_source_bytes(root, task_id, category="context_checkpoints",
        source_id="budget-pause-" + pause_id, data=json.dumps(state, ensure_ascii=False).encode(), extension="json")
    pause = {"pause_id": pause_id, "pause_generation": int(old.get("pause_generation") or 0) + 1,
             "task_attempt": attempt, "state": STATE_PAUSED, "reason": "sleep", "rail": RAIL_MODEL_SLEEP,
             "scope": "task", "source_ref": source, "retained_owner_wait_id": wait["wait_id"],
             "sleep": wait["sleep"], "sleep_seen": state.get("seen") or [],
             "execution_drive_root": wait.get("execution_drive_root"), "started_at": wait.get("started_at"),
             "paused_at": float(wait.get("sleep_started_at") or time.time()),
             "paused_duration_sec": float(wait.get("budget_paused_sec") or 0),
             "model_wait_quota_clock": wait.get("model_wait_quota_clock") or {},
             "is_direct_chat": direct,
             "exact_continuation": True, "auto_resume": False, "replay_safe": False}
    return set_budget_pause(root, task_id, pause, expected_owner_wait=wait,
        expected_pause_id=str(old.get("pause_id") or ""), expected_state=str(old.get("state") or ""),
        expected_grant_id=str((old.get("grant") or {}).get("grant_id") or ""))


def restart_held(task: Any) -> bool:
    """Whether this queued row carries an UNRELEASED owner-Restart hold."""
    from supervisor.events_budget import BUDGET_HOLD_KEY, HOLD_OWNER_RESTART

    hold = task.get(BUDGET_HOLD_KEY) if isinstance(task, dict) else None
    return isinstance(hold, dict) and str(hold.get("reason") or "") == HOLD_OWNER_RESTART \
        and not hold.get("selected")


def never_started(task: Any) -> bool:
    """A queued row that never dispatched: first attempt, no continuation carrier.

    Retry rows (a later attempt), owner-wait handoffs, exact pauses and grant
    carriers all describe work that already ran; they keep their own rules.
    """
    if not isinstance(task, dict) or not str(task.get("id") or ""):
        return False
    return (task.get("admitted_dispatch") == "none"
            and not task.get("_owner_wait_resume")
            and not task.get("_budget_pause_resume")
            and not task.get("_terminalization_retry"))



def hold_for_owner_restart(task: Dict[str, Any], drive_root: Any) -> Dict[str, Any]:
    """Hold one never-started row for the owner's Restart (same id, no dispatch).

    A row already under another typed hold keeps that hold: it is already
    non-dispatchable and its own release rule stays the one that applies.
    """
    from supervisor.events_budget import HOLD_OWNER_RESTART

    return hold_after_stop(task, drive_root, HOLD_OWNER_RESTART)


def hold_after_stop(task: Dict[str, Any], drive_root: Any, reason: str) -> Dict[str, Any]:
    """Hold recovered sleep or an owner's stop, preserving stronger prior holds."""
    from supervisor.events_budget import HOLD_CONTINUATION_WRITER, budget_hold_fact, hold_budget_row

    prior = budget_hold_fact(task)
    if prior is not None:
        if reason == HOLD_SAVED_SLEEP_RECOVERY or prior.get("reason") not in {
                HOLD_CONTINUATION_WRITER, HOLD_SAVED_SLEEP_RECOVERY}:
            return task
    # A Continue waiting on its predecessor's writers is held by the newer
    # Restart instead: a later reconciliation must not release what the owner's
    # Restart now holds (its own release still re-checks those writers).
    hold_budget_row(
        task, reason=reason,
        detail=("saved sleep recovered; held until an explicit Resume" if reason == HOLD_SAVED_SLEEP_RECOVERY
                else "held by the owner's stop until an explicit Resume"),
        extra={"root_task_id": str(task.get("root_task_id") or task.get("id") or ""),
               **({"prior_hold": dict(prior)} if prior else {})},
        result_root=pathlib.Path(task.get("budget_drive_root") or drive_root))
    return task


# --- the kill_workers consumers ------------------------------------------------------------
#
# ``kill_workers`` calls these under the queue lock AFTER every worker was
# signalled, so retention adds no prerequisite before a Panic's kill.

def park_saved_running_rows(running: Dict[str, Any], pending: List[Dict[str, Any]],
                            preserve: Iterable[str], drive_root: Any, *,
                            sleep_hold_reason: str = HOLD_SAVED_SLEEP_RECOVERY) -> List[str]:
    """Move every RUNNING row whose pause is saved into PENDING under its marker.

    A checkpoint stored before its park event was handled leaves the row
    RUNNING; any door would otherwise terminalize a saved pause. Returns the
    parked ids; the rest of ``RUNNING`` is ordinary interrupted work.
    """
    preserved = set(preserve or ())
    parked_ids: List[str] = []
    for task_id, meta in list(running.items()):
        if task_id in preserved or not isinstance(meta, dict) or not isinstance(meta.get("task"), dict):
            continue
        task = meta["task"]
        parked = park_saved_pause(task, int(meta.get("attempt") or task.get("_attempt") or 1),
                                  pathlib.Path(task.get("budget_drive_root") or drive_root),
                                  pause_source="stopped_during_pausing", sleep_hold_reason=sleep_hold_reason)
        if parked is None:
            continue
        running.pop(task_id, None)
        if not any(isinstance(row, dict) and str(row.get("id") or "") == str(task_id) for row in pending):
            pending.append(parked)
        parked_ids.append(str(task_id))
    return parked_ids


def retained_pending(task: Dict[str, Any], *, sleep_hold_reason: str = "") -> bool:
    """A queued saved pause (an unused grant is first returned to it) or an
    earlier owner-Restart hold: kept as the same queued task on every door.
    A stop cause also holds cold sleeps and marker-less failed conversions;
    their own readiness must not wake what the owner just stopped."""
    if isinstance(task.get("_budget_pause_resume"), dict):
        from supervisor.budget_resume import revoke_exact_budget_resume

        revoke_exact_budget_resume(task, "restart_before_dispatch")
    pause = task.get("_budget_pause")
    saved = isinstance(pause, dict) and pause.get("exact_continuation") is True
    from supervisor.events_budget import budget_hold_fact

    hold = budget_hold_fact(task)
    if sleep_hold_reason and ((saved and pause.get("reason") == "sleep"
                               and sleep_hold_reason != HOLD_SAVED_SLEEP_RECOVERY)
                              or (hold or {}).get("reason") == HOLD_SAVED_SLEEP_RECOVERY):
        from supervisor import queue as q

        hold_after_stop(task, q.DRIVE_ROOT, sleep_hold_reason)
    return saved or isinstance(pause, dict) or hold is not None



def child_of_interrupted(task: Dict[str, Any], running_ids: Iterable[str], interrupted_roots: Iterable[str]) -> bool:
    """A never-started child whose parent (or root) this stop interrupts."""
    parent_id = str(task.get("parent_task_id") or "")
    return bool(parent_id and (parent_id in set(running_ids)
                               or str(task.get("root_task_id") or "") in set(interrupted_roots)))


def hold_never_started(task: Dict[str, Any], running_ids: Iterable[str], interrupted_roots: Iterable[str]) -> bool:
    """Retain queued work on owner Restart; unknown dispatch stays unresolved.

    The legacy entry-point name is shared with kill_workers. Missing evidence
    is held without cancellation or a fabricated ``admitted_dispatch=none``;
    only positively never-started rows get the selectable Restart hold.
    """
    from supervisor import queue as q
    from supervisor.events_budget import hold_budget_row

    if any(task.get(key) for key in ("_owner_wait_resume", "_budget_pause_resume", "_terminalization_retry")):
        return False
    if never_started(task):
        hold_for_owner_restart(task, q.DRIVE_ROOT)
    elif "admitted_dispatch" in task:
        return False  # recorded possible dispatch keeps the ordinary interruption path
    else:
        hold_budget_row(task, reason="dispatch_outcome_unknown",
                        detail="an assignment may have reached a worker; reconcile its receipt before resuming",
                        result_root=pathlib.Path(task.get("budget_drive_root") or q.DRIVE_ROOT))
    return True


def held_ids(pending: Iterable[Any]) -> List[str]:
    return [str(task.get("id") or "") for task in pending if restart_held(task)]
