"""Confirmed pause disclosures, retained on the task until local chat records them.

The pause writer registers an episode in the SAME result update as confirmation,
and that write publishes the task in the ``pause_notices`` obligation set
(``ouroboros/obligations.py``) before the result is replaced. The off-loop
maintenance owner reads that set, so a pass costs one result read per task that
owes a notice: it never enumerates completed results and never reads chat
history. The saved chat row is the receipt, recorded by notice id when the
append lands (``ouroboros/notice_receipts.py``) and dropped once the task
acknowledges it; it is never proof of external delivery. Resume and later
pauses retain the duty. No separate outbox or lifecycle authority exists.
"""
from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)
_LOCK = threading.Lock()


def notice_fields(current: dict, root: Any, task_id: str, episode_id: str, reason: str, *,
                  fence: dict | None = None) -> dict:
    """Called inside the result writer, only for a newly confirmed pause episode.

    ``fence`` is the owner Pause this episode confirms: the author it declared
    (``control_actor.pause_author``) is kept with the notice, so a later Pause
    of the same task never rewords it.
    """
    from ouroboros.control_actor import pause_author

    if not episode_id or reason not in {"budget", "owner", "authority"}:
        return {}
    identity = f"pause:{task_id}:{episode_id}"
    pending = dict(current.get("pause_notices") or {})
    by = {"by": pause_author(fence)} if fence is not None else {}
    pending.setdefault(identity, {"reason": reason, "confirmed_at": utc_now_iso(), **by})
    return {"pause_notices": pending}


_RETAINED = "Its work is retained; Resume continues it."


def notice_text(notice: dict) -> str:
    """The sentence one owed notice says; an owner Pause names the author its own fence declared."""
    if notice.get("reason") == "owner":
        by = notice.get("by") if isinstance(notice.get("by"), dict) else {}  # a notice saved before ``by``: nobody
        said = " ".join(str(by.get("text") or "").split())
        if by.get("actor") == "owner":
            return f"You paused this task. {_RETAINED}"
        if by.get("actor") == "ouroboros" and said:
            ended = said if said.endswith((".", "!", "?", "…")) else f"{said}."
            return f"Ouroboros paused this task: {ended} {_RETAINED}"
        if by.get("actor") == "ouroboros":
            return f"Ouroboros paused this task. {_RETAINED}"
        return f"This task was paused. {_RETAINED}"
    authority = notice.get("reason") == "authority"
    cause = ("a technical fault: its billing or control authority could not be read, which says nothing about "
             "money left") if authority else "its budget limit"
    resume = "Fix the cause, then use Resume on the task when available" if authority else (
        "Use Resume on the task when available")
    return f"The task paused because of {cause}. Its work is retained. {resume}."


def _acknowledge(root: pathlib.Path, task_id: str, identity: str, notice: dict) -> None:
    """Drop one recorded notice from the task; the last one retires its membership."""
    from ouroboros.task_results import require_writable_task_result_schema
    from ouroboros.task_store import patch_document

    def update(current: dict):
        require_writable_task_result_schema(current)
        pending = dict(current.get("pause_notices") or {})
        if pending.get(identity) != notice:
            return None
        pending.pop(identity)
        updated = {**current, "pause_notices": pending}
        if not pending:
            updated.pop("pause_notices")
        return updated

    patch_document(root, task_id, update)


def reconcile_pause_notices(drive_root: Any, *, stop_requested=None) -> None:
    """The existing off-loop maintenance retries local publication, never Resume."""
    from ouroboros import notice_receipts, obligations
    from ouroboros.task_results import load_task_result
    from supervisor.log_addressing import address_task_event
    from supervisor import message_bus

    root = pathlib.Path(drive_root).resolve()
    if message_bus.DATA_DIR is None or pathlib.Path(message_bus.DATA_DIR).resolve() != root:
        return  # The shared message writer has not bound this installation yet.
    if not _LOCK.acquire(blocking=False):
        return
    try:
        try:
            candidates = sorted(obligations.members(root, "pause_notices"))
        except obligations.ObligationsUnavailable:
            # The lifecycle import reported this already; the rebuild job repairs
            # it. Nothing here substitutes a scan of results or chat history.
            log.debug("Pause notices wait for the obligations rebuild", exc_info=True)
            return
        for task_id in candidates:
            if stop_requested is not None and stop_requested():
                return
            try:
                result = load_task_result(root, task_id, strict=True)
                if not result or not result.get("pause_notices"):
                    continue  # the membership is the document's own flag: nothing owed, nothing to retire
                event = address_task_event({task_id: {"task": result}}, root, {"task_id": task_id})
                chat_id = message_bus.notification_chat_route(event.get("chat_id"))
                if chat_id is None:
                    continue  # No destination is not permission to default to Main.
                for identity, notice in dict(result["pause_notices"]).items():
                    if stop_requested is not None and stop_requested():
                        return
                    if not isinstance(notice, dict):
                        continue
                    receipt = notice_receipts.pause_receipt_id(chat_id, identity)
                    # Read per notice: a failed send whose row DID land (a live
                    # transport error after the append) is seen on the next pass.
                    if receipt not in obligations.members(root, "pause_notice_receipts"):
                        message_bus.send_with_budget(
                            chat_id, notice_text(notice), task_id=task_id, role="system",
                            system_type="task_pause_notice", narration=False, require_write=True,
                            ensure_record_boundary=True,
                            progress_meta={**{k: event[k] for k in ("root_task_id", "parent_task_id") if k in event},
                                           "card_row": "timeline", "card_row_id": identity},
                        )
                    if stop_requested is not None and stop_requested():
                        return
                    _acknowledge(root, task_id, identity, notice)
                    obligations.remove(root, "pause_notice_receipts", receipt)
            except Exception:
                # append-before-ack is recovered from the receipt the append
                # wrote, not by trusting a failed send.
                log.warning("Pause notice for %s remains owed to local history", task_id, exc_info=True)
    finally:
        _LOCK.release()
