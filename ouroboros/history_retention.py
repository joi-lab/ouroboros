"""Separate result adoption from bulk retention using the existing custody ledger.

The answer, output files and verification receipts are completion work. Historical
call/source closure belongs to the off-loop child-ref retry owner. Its pending
row holds the execution drive across restart; readers can still resolve exact
sources there until canonical copies have been verified.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from ouroboros.utils import append_jsonl, utc_now_iso


DEFERRED_KIND = "history_retention_deferred"


def prepare_result_retention(parent: Path, child: Path, task_id: str,
                             result: dict[str, Any]) -> tuple[dict, dict]:
    """Copy deliverables, retain source custody, and leave history to maintenance."""
    from ouroboros.artifacts import promote_task_attachment_refs
    from ouroboros.headless import _copy_child_artifacts_to_parent

    copied = copy.deepcopy(result)
    separate = parent.resolve(strict=False) != child.resolve(strict=False)
    state = {"schema_version": 1, "status": "incomplete" if separate else "complete",
             "promoted_ref_count": 0, "promoted_source_handle_count": 0,
             "pending_refs": [], "unavailable_refs": []}
    if not separate:
        return copied, state
    state["pending_refs"].append({"kind": DEFERRED_KIND, "path": str(child.resolve(strict=False)),
                                  "reason": "background_history_retention"})
    promote_task_attachment_refs(parent, child, task_id, copied, state)
    if isinstance(copied.get("artifacts"), list):
        copied["artifacts"] = _copy_child_artifacts_to_parent(
            parent, task_id, child, copied["artifacts"], promotion=state)
    # Logs is diagnostic only: the task-result publication owns the durable duty.
    event = {
        "ts": utc_now_iso(), "type": "history_retention", "task_id": task_id,
        **retention_summary({"child_ref_promotion": state}),
        "diagnostics": retention_diagnostics({"child_ref_promotion": state}),
        **{key: result[key] for key in ("chat_id", "project_id", "parent_task_id", "root_task_id") if key in result},
    }
    try:
        append_jsonl(parent / "logs" / "events.jsonl", event)
    except OSError:
        pass  # The task-result duty still owns custody if the diagnostic log is unavailable.
    return copied, state


def retention_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    """Full existing failure facts for Logs/raw detail, excluding routine deferral."""
    state = result.get("child_ref_promotion")
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        return {}
    pending = state.get("pending_refs") or []
    problems = [row for row in pending if isinstance(row, dict) and row.get("kind") != DEFERRED_KIND]
    unavailable = state.get("unavailable_refs") or []
    return {"pending_refs": copy.deepcopy(problems), "unavailable_refs": copy.deepcopy(unavailable)}


def retention_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Group failure reasons for display; preserve every full fact in diagnostics."""
    from collections import Counter

    diagnostics = retention_diagnostics(result)
    if not diagnostics:
        return {}
    state = result["child_ref_promotion"]
    pending = state.get("pending_refs") or []
    problems, unavailable = diagnostics["pending_refs"], diagnostics["unavailable_refs"]
    reasons = Counter(str(row.get("reason") or "No failure reason was recorded.")
                      for row in (*problems, *unavailable) if isinstance(row, dict))
    return {"status": "problem" if problems or unavailable else
            "complete" if state.get("status") == "complete" else "pending",
            "pending_count": len(pending), "problem_count": len(problems) + len(unavailable),
            "problem_reasons": [{"reason": reason, "count": count} for reason, count in reasons.items()],
            "promoted_ref_count": state.get("promoted_ref_count", 0),
            "promoted_source_handle_count": state.get("promoted_source_handle_count", 0)}


def call_inventory_custodied(parent: Path, child: Path, task_id: str,
                             current: dict[str, Any], *, stop=None) -> bool:
    """Arm the same background duty before GC of old/cancelled or newly grown history.

    Manifest writers atomically replace files, changing their directory revision.
    Compare that cheap revision here; scanning and copying calls remains off the
    completion path. The no-live custody check owns whether GC may proceed.
    """
    from ouroboros.task_custody import attempt_basis, fence_publication, publication_fence, task_custody_lock
    from ouroboros.task_results import write_task_result

    directory = child / "observability" / "calls" / task_id
    try:
        revision = directory.stat().st_mtime_ns
    except FileNotFoundError:
        return True
    promotion = current.get("child_ref_promotion") or {}
    if promotion.get("call_inventory_preserved") is True and promotion.get("call_inventory_mtime_ns") == revision:
        return True
    with publication_fence(stop), task_custody_lock(parent, task_id, timeout_sec=0.5) as locked:
        if not locked:
            return False
        def owe(row, _incoming):
            fence_publication()
            if attempt_basis(row) != attempt_basis(current):
                return None
            state = copy.deepcopy(row.get("child_ref_promotion") or {})
            pending = list(state.get("pending_refs") or [])
            marker = {"kind": DEFERRED_KIND, "path": str(child), "reason": "background_history_retention"}
            if marker not in pending:
                pending.append(marker)
            state.update(schema_version=1, status="incomplete", pending_refs=pending)
            return {"status": row["status"], "child_ref_promotion": state}
        write_task_result(parent, task_id, current["status"], _field_projector=owe, strict_existing_dict=True)
    return False
