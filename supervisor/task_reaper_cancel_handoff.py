"""The reaper's cancel-suppressed retry handoff: custody for the cancel that won retry admission,
then the physical terminal event once that custody is durable.

Moved byte-preserving from ``supervisor/task_reaper.py`` at its module ceiling; the reaper
imports both helpers and stays the only caller.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ouroboros.outcomes import EXECUTION_INFRA_FAILED, terminal_outcome_axes

log = logging.getLogger(__name__)


def _settle_retry_cancel_handoff(
    q: Any, task_id: str, retry_task_id: str, cancel_target: str,
) -> Dict[str, str]:
    """Give a cancel that won retry admission custody of every physical id.

    Retry admission was suppressed before successor publication.  Route the
    winning intent through its existing policy owner: cascade keeps the subtree
    postcondition, graceful stop gets its policy-aware hold, and an immediate
    single request uses ordinary custody.  No second intent is minted.
    """
    outcomes: Dict[str, str] = {}
    if not cancel_target:
        return outcomes
    try:
        from ouroboros.cancel_intents import (
            SCOPE_CASCADE,
            active_intent,
        )
        from supervisor.owner_stop import (
            OWNER_STOP_HOLDING,
            sweep_owner_stop_hold,
        )
        import time as _time

        intent = active_intent(q.DRIVE_ROOT, cancel_target) or {}
        if str(intent.get("scope") or "") == SCOPE_CASCADE:
            outcomes[cancel_target] = (
                q.CANCEL_CANCELLED
                if q.cancel_task_by_id(cancel_target)
                else q.CANCEL_FAILED
            )
        elif sweep_owner_stop_hold(
            q, cancel_target, intent, now=_time.time(),
        ):
            outcomes[cancel_target] = OWNER_STOP_HOLDING
        else:
            outcomes[cancel_target] = str(q.cancel_task_custody(cancel_target))
    except Exception:
        outcomes[cancel_target] = "failed"
        log.error(
            "Reaper: cancellation custody failed for suppressed retry id %s",
            cancel_target,
            exc_info=True,
        )
    return outcomes


def _emit_cancel_suppressed_retry_task_done(
    q: Any,
    workers_mod: Any,
    task: Dict[str, Any],
    task_id: str,
    task_type: str,
    cancel_target: str,
    handoff_outcomes: Dict[str, str],
    terminal_metadata: Any,
) -> bool:
    """Publish only the physical terminal event after cancel handoff is durable.

    The cascade owns the one owner-facing summary, so this deliberately does
    not call the self-finalized delivery helper.  The fast-path card event is
    safe only after the winning intent has left the active projection (which
    proves its answer/summary was durably owed) and the physical retry row is
    settled.
    """
    outcome = str(handoff_outcomes.get(cancel_target) or "")
    if outcome not in {q.CANCEL_CANCELLED, q.CANCEL_ALREADY_SETTLED}:
        return False
    try:
        from ouroboros.cancel_intents import active_intent
        from ouroboros.cost_projection import carry_cost_meta
        from ouroboros.task_results import load_task_result
        from ouroboros.task_status import SETTLED_STATUSES

        if active_intent(q.DRIVE_ROOT, cancel_target, strict=True) is not None:
            return False
        stored = load_task_result(q.DRIVE_ROOT, task_id, strict=True) or {}
        status = str(stored.get("status") or "")
        if status not in SETTLED_STATUSES:
            return False
        cost_fields = carry_cost_meta(stored)
        if not cost_fields:
            cost_fields = q.reconstruct_task_cost(task_id, fields=True)
        workers_mod.get_event_q().put({
            "type": "task_done",
            "task_id": task_id,
            "root_task_id": str(task.get("root_task_id") or "") if isinstance(task, dict) else "",
            "task_type": task_type,
            "chat_id": (
                int(task.get("chat_id") or 0)
                if isinstance(task, dict) else 0
            ),
            "status": status,
            "reason_code": str(stored.get("reason_code") or ""),
            "outcome_axes": (
                dict(stored["outcome_axes"])
                if isinstance(stored.get("outcome_axes"), dict)
                else terminal_outcome_axes(
                    lifecycle=status,
                    execution=EXECUTION_INFRA_FAILED,
                    reason_code=str(stored.get("reason_code") or status),
                    review_trigger="supervisor_terminal",
                )
            ),
            **cost_fields,
            "metadata": terminal_metadata,
        })
        return True
    except Exception:
        log.warning(
            "Reaper: failed to publish cancel-suppressed task_done for %s",
            task_id,
            exc_info=True,
        )
        return False
