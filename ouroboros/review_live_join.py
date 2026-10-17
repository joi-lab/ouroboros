"""Recorded collection's live join (#1547): a positive hint, never dispatch authority.

A recorded collection (a commit, preflight or ``review_change`` retry of the same
candidate and intent) collects an original seat from its recorded assignment. When
that seat's ORIGINAL physical operation still runs in this process's custody
(``review_custody._ACTIVE``), the collection joins its drain and remaining window
(``review_operation.review_operation_scope``) instead of a cold read. Absence here is
no custody: the caller keeps its nonblocking collection. Nothing here mints, re-sends
or replaces a seat; the custody registry is read under its own lock, as
``review_pause`` reads it.
"""

from __future__ import annotations

import copy
from typing import Any, List


def recorded_live_review_attempt(request: Any, slot: Any, row: dict) -> Any:
    """A positive live-join hint, never authority for a replacement dispatch."""
    from ouroboros.review_custody import _ACTIVE, _ACTIVE_LOCK, _attempt_key
    from ouroboros.review_dispatch import review_operation_binding

    with _ACTIVE_LOCK:
        entry = _ACTIVE.get(_attempt_key(request, slot))
        if entry is not None and entry.operation_id == str(row.get("operation_id") or ""):
            binding = {k: v for k, v in entry.recovery_binding.items() if k != "pending_invocation_id"}
            if binding == review_operation_binding(request, slot, entry.operation_id):
                return entry
    return None


def live_join_operation(request: Any, slots: List[Any], usage_ctx: Any) -> Any:
    """The one open operation every seat of a reconcile-only wave still runs in, else ``None``
    (a seat without a live original operation, or seats split across owners)."""
    rows = (getattr(usage_ctx, "_review_frozen_rows", {}) or {}).get(request.surface, {})
    entries = [recorded_live_review_attempt(request, slot, rows.get(str(slot.slot_id), {})) for slot in slots]
    if not entries or not all(entry is not None and entry.operation is not None and not entry.operation.closed
                              for entry in entries):
        return None
    owners = {id(entry.operation): entry.operation for entry in entries}
    return next(iter(owners.values())) if len(owners) == 1 else None


def preparing_live_review_assignment(*, task_id: str, surface: str, row: dict, retry_key: str,
                                     root_task_id: str = "", review_contract: str = "") -> Any:
    """The exact unsent preparation may precede its prompt CAS; absence alone is no custody."""
    from ouroboros.review_custody import _ACTIVE, _ACTIVE_LOCK
    from ouroboros.review_dispatch import review_operation_binding

    binding = row.get("recovery_binding") or {}
    if row.get("operation_state") != "pending_dispatch" or not isinstance(binding, dict):
        return None
    with _ACTIVE_LOCK:
        entry = _ACTIVE.get(str(binding.get("request_key") or ""))
        if entry is None or not entry.assignment or entry.operation_id != row.get("operation_id"):
            return None
        request, slot = entry.assignment
        expected = review_operation_binding(request, slot, entry.operation_id)
        if ({k: v for k, v in binding.items() if k != "pending_invocation_id"} != expected
                or entry.recovery_binding != expected or request.task_id != task_id or request.surface != surface
                or request.retry_key != retry_key or slot.slot_id != row.get("slot_id")
                or (root_task_id and expected.get("root_task_id") != root_task_id)
                or (review_contract and expected.get("review_contract") != review_contract)):
            return None
        return copy.deepcopy((request, slot))
