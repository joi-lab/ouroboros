"""Retained Presence facts, projected from the existing task and conversation sources.

The runner owns eligibility and execution custody. This module neither retries nor
settles a turn: it records an observation in the existing room biography, including
for a lost RUNNING row. A task-local receipt suppresses ordinary repeated writes;
the stable summary id identifies an append/receipt crash gap without a history scan.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping


def retained_turn_facts(stored: Mapping[str, Any], task_id: str) -> dict[str, Any]:
    """Facts for an already-classified retained turn, never an authored answer.

    No absence of a tool/delivery record proves absence of an effect. A child
    reference is copied only from actual admitted work; the original is not its
    own child. Callers decide retained versus retry from their current authority.
    """
    metadata = stored.get("metadata") if isinstance(stored.get("metadata"), Mapping) else {}
    presence = metadata.get("presence") if isinstance(metadata.get("presence"), Mapping) else {}
    event = presence.get("event") if isinstance(presence.get("event"), Mapping) else {}
    reason, status = str(stored.get("reason_code") or ""), str(stored.get("status") or "")
    code = ("presence_resources_unavailable" if reason == "resource_refusal_no_resend" else
            "presence_turn_cancelled" if status == "cancelled" else "presence_attempt_outcome_unknown")
    facts: dict[str, Any] = {
        "code": code, "turn_ref": task_id, "recorded_status": status,
        "reason_code": reason, "source_event_id": str(event.get("source_event_id") or ""),
        "conversation_key": str(event.get("conversation_key") or ""),
        "delivery_reporting_version": int(presence.get("delivery_reporting_version") == 1),
    }
    continuation = stored.get("presence_continuation")
    continuation = continuation if isinstance(continuation, Mapping) else {}
    initial = continuation.get("initial") if isinstance(continuation.get("initial"), Mapping) else {}
    child = metadata.get("presence_work_ref") or continuation.get("child_work_ref") or initial.get("work_ref")
    if isinstance(child, str) and child.strip():
        facts["work_ref"] = child.strip()
    unknown = metadata.get("presence_unknown_outcome")
    if isinstance(unknown, Mapping):
        facts["uncertainty"] = {key: str(unknown[key]) for key in ("source", "error_kind", "operation_id")
                                if unknown.get(key) not in (None, "")}
    reconciled = stored.get("status_reconciled_from")
    if reconciled:
        facts["status_reconciled_from"] = str(reconciled)
    return facts


def record_unresolved_turn(drive_root: Path, task_id: str) -> bool:
    """Append one source-backed room observation under the runner's per-task lock.

    This is not a terminal transition or an owner question. Keep the CURRENT
    status when stamping the append receipt, even if a late result landed after
    the observation. A failed append gets no receipt and can be tried again.
    """
    from ouroboros.dialogue_provenance import presence_provenance_fields
    from ouroboros.project_dialogue import append_canonical_task_summary
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.utils import utc_now_iso

    stored = load_task_result(drive_root, task_id, strict=True) or {}
    metadata = stored.get("metadata") if isinstance(stored.get("metadata"), Mapping) else {}
    presence = metadata.get("presence")
    if not isinstance(presence, Mapping) or not stored.get("status"):
        return False
    summary_id = f"presence-retained:{task_id}"
    receipt = stored.get("presence_recovery_recorded")
    if isinstance(receipt, Mapping) and receipt.get("summary_id") == summary_id:
        return False
    facts = retained_turn_facts(stored, task_id)
    event = presence.get("event") if isinstance(presence.get("event"), Mapping) else {}
    observed_at = utc_now_iso()
    row = {
        "ts": observed_at, "direction": "system", "type": "task_summary", "text": "",
        "summary_kind": "host_task_facts", "summary_id": summary_id, "task_id": task_id,
        "chat_id": stored.get("chat_id"), "status": str(stored["status"]),
        "_is_direct_chat": bool(stored.get("_is_direct_chat")),
        "source": str(stored.get("source") or ""), "presence_recovery": facts,
        "source_event": copy.deepcopy(dict(event)), **presence_provenance_fields(stored),
        "outcome_final": False, "outcome_authority": "retained_presence_observation",
    }
    if isinstance(presence.get("observed_text"), str):
        row["source_text"] = presence["observed_text"]
    if not append_canonical_task_summary(drive_root, row):
        return False

    def stamp(current: dict, _patch: dict) -> dict | None:
        if not current or not current.get("status"):
            return None
        return {"status": current["status"], "presence_recovery_recorded": {
            "summary_id": summary_id, "written_at": observed_at,
        }}

    write_task_result(drive_root, task_id, str(stored["status"]), strict_existing_dict=True,
                      _field_projector=stamp)
    return True


def retained_work_view(stored: Mapping[str, Any], reference: str) -> tuple[int, dict[str, Any]]:
    """Project a bound work reference without confusing custody with authored speech."""
    metadata = stored.get("metadata") if isinstance(stored.get("metadata"), Mapping) else {}
    presence = metadata.get("presence") if isinstance(metadata.get("presence"), Mapping) else {}
    from ouroboros.presence_continuation import work_view

    status = str(stored.get("status") or "")
    from ouroboros.presence_runner import _terminal_refusal, presence_result_from_stored

    continued = work_view(stored, reference)  # a continuing Presence author's own poll (#1536)
    unresolved = (_terminal_refusal(stored) and
                  (status in {"completed", "failed", "cancelled"}
                   or stored.get("presence_recovery_recorded")
                   or (continued is not None and continued[1].get("status") == "interrupted")))
    if unresolved:
        # Already released continuation outputs keep their exact identities. A draft
        # salvaged from the unresolved final round is never newly selected speech.
        released = {key: continued[1][key] for key in (
            "outputs", "continuation_ref", "continuation_version", "child_work_ref") if key in continued[1]} if continued else {}
        return 200, {"ok": True, "status": "unresolved", "disposition": "retained",
                     **retained_turn_facts(stored, reference), **released}
    if continued is not None:
        return continued
    if status not in {"completed", "failed", "cancelled"}:
        return 202, {"ok": True, "status": "pending", "work_ref": reference,
                     "delivery_reporting_version": presence.get("delivery_reporting_version", 0)}
    result = presence_result_from_stored(stored, reference)
    return 200, {"ok": True, "status": status, "outcome": result.outcome, "text": result.text, "work_ref": reference,
                 "delivery_reporting_version": result.delivery_reporting_version}
