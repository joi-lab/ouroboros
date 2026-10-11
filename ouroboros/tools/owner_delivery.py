"""Live-first delivery of owner-directed chat events from a running task.

One seam for the send family (``send_user_message`` / ``send_photo`` /
``send_video`` / ``send_file`` / ``send_links``): try the live worker event
queue first so the owner sees the frame while the task is still RUNNING, and
fall back to ``ctx.pending_events`` (the end-of-task drain) when live
transport is unavailable. The transport contract mirrors
``_emit_control_event`` (tools/control.py) — live XOR deferred, never both —
with two structural gates (a consciousness wake-up is an ordinary Main turn
and delivers like one):

- **A2A chats stay deferred.** A peer's ``wait_for_response`` subscription
  resolves on the FIRST non-progress chat frame, so a live mid-task frame
  would hijack the answer the peer is waiting for.
- **Sticky-deferred after the first live failure.** Once one frame falls back
  to the buffer, later frames must not overtake it: narrative order beats
  freshness, so the task finishes the attempt on the deferred path.

Frames must stay multiprocessing-safe by construction (JSON primitives and
base64 strings only): the production worker queue is manager-backed, so a
poison value raises at the caller and falls back cleanly — but a plain
``multiprocessing.Queue`` transport would serialize in a feeder thread and
lose it AFTER ``put_nowait`` returned, so the by-construction rule is the
contract, not the transport's forgiveness.

Retry semantics are the progress channel's: a live-delivered frame from a
failed attempt is not recalled, and a retried task may narrate again. That is
an accepted property of live delivery, not a defect to dedupe away.

Known, pre-existing exception to the ordering rule: the blocking-post-task
final-answer shortcut (``deliver_final_message_live``) may ship the FINAL
ahead of frames still buffered here — the answer is deliberately never held
hostage to trailing narration. The sticky rule orders the send family among
itself, not against the terminal shortcut.
"""

from __future__ import annotations

from typing import Any, Dict

_STICKY_ATTR = "_owner_delivery_sticky_deferred"


def deliver_owner_event(ctx: Any, evt: Dict[str, Any]) -> str:
    """Deliver an owner-directed chat event live, or buffer it. Returns
    ``"live"`` or ``"deferred"`` — the caller words its receipt honestly.

    Lineage (``task_id`` / ``parent_task_id`` / ``root_task_id``) is stamped
    for every task frame so the supervisor can resolve project binding for
    live deliveries exactly as it does for the end-of-task drain.
    """
    meta = getattr(ctx, "task_metadata", {})
    meta = meta if isinstance(meta, dict) else {}

    def _deferred() -> str:
        ctx.pending_events.append(evt)
        return "deferred"

    evt.setdefault("task_id", str(getattr(ctx, "task_id", "") or ""))
    evt.setdefault("parent_task_id", str(meta.get("parent_task_id") or ""))
    evt.setdefault("root_task_id", str(meta.get("root_task_id") or ""))
    try:
        from ouroboros.contracts.chat_id_policy import is_a2a_chat_id

        if is_a2a_chat_id(int(evt.get("chat_id"))):
            return _deferred()
    except (TypeError, ValueError):
        pass
    if getattr(ctx, _STICKY_ATTR, False):
        return _deferred()
    event_queue = getattr(ctx, "event_queue", None)
    if event_queue is None:
        return _deferred()
    try:
        event_queue.put_nowait(dict(evt))
        return "live"
    except Exception:
        try:
            setattr(ctx, _STICKY_ATTR, True)
        except Exception:
            pass
        return _deferred()
