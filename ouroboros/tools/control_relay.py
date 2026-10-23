"""Tell another work: model-authored note plus selected host-held owner quotes."""
from __future__ import annotations

import uuid


def _tell_task(ctx, task_id: str, note: str = "", quote: list | None = None, summary: str = "") -> str:
    from ouroboros.owner_relay import quote_rows
    from ouroboros.dialogue_provenance import presence_caller_binding, presence_sender_origin
    from ouroboros.project_dialogue import AGENT_RECEIPT_ID_PREFIX
    from ouroboros.tools.control_routing import _routing_issuer, _emit_and_wait_for_routing, _attach_client_surface
    from ouroboros.tools.tool_result import ToolResult, publish_no_effect
    from ouroboros.utils import utc_now_iso

    target = str(task_id or "").strip()
    quotes, error = quote_rows(ctx, [] if quote is None else quote)
    if error or not target or not isinstance(note, str) or (not note.strip() and not quotes):
        return publish_no_effect(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", meta={"reason_code": "QUOTE_NOT_HELD" if error else "argument_invalid"},
            text=error or "TOOL_ARG_ERROR: tell_task needs a task_id and a note or held owner quote."))
    relay_id = uuid.uuid4().hex
    message_id = next((q["source"]["client_message_id"] for q in quotes if q["source"]["client_message_id"]), "")
    own_chat = getattr(ctx, "current_chat_id", None)  # a teller may carry no chat: Main is the return room
    event = {"type": "steer_task", "routing_token": relay_id, "relay_id": relay_id,
             "target_task_id": target, "message": note, "quotes": quotes,
             "summary": summary.strip() if isinstance(summary, str) else "",
             "chat_id": int(quotes[0]["source"]["chat_id"] if quotes else 1 if own_chat is None else own_chat),
             "client_message_id": message_id or f"{AGENT_RECEIPT_ID_PREFIX}{relay_id}",
             "issuer": {**_routing_issuer(ctx), "task_id": str(getattr(ctx, "task_id", "") or "")},
             "ts": utc_now_iso()}
    _attach_client_surface(ctx, event)
    if (binding := presence_caller_binding(ctx)) is not None:
        event.update(presence_binding_id=binding, sender_origin=presence_sender_origin(ctx))
    mode, receipt = _emit_and_wait_for_routing(ctx, event)
    status = receipt.get("status")
    if status in {"delivered", "read"}:
        return f"Message written to {target}; relay_id={relay_id}. Read acknowledgement follows when it drains."
    reason = receipt.get("reason") or ("target_not_steerable" if status in {"rejected", "needs_manual_target"}
                                      else "confirmation_timeout")
    if status in {"rejected", "needs_manual_target"}:
        return publish_no_effect(ctx, ToolResult(status="error", code="TOOL_REPORTED_FAILURE",
            text=f"TELL_REJECTED: {target}: {reason}. Nothing was delivered.", meta={"reason_code": reason}))
    from ouroboros.tools.tool_result import _publish_tool_result
    return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_REPORTED_FAILURE",
        text=(f"TELL_UNCONFIRMED: {target}: {reason} ({mode}); relay_id={relay_id}. "
              "The request may still land. Check its receipt before sending the words again."),
        meta={"reason_code": reason, "relay_id": relay_id}))
