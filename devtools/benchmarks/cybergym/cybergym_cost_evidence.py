"""Read the terminal owner's coherent root cost evidence without reconstructing it.

Gateway envelopes contain overlapping own-task and root-tree projections. This
decoder keeps their scopes separate and is shared by delivery admission and the
campaign liability projection. It performs no ledger reads or persistence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def frame_accounting_sources(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Walk the existing result-envelope vocabulary, including display views."""
    sources: list[Mapping[str, Any]] = []
    queue = [payload]
    seen: set[int] = set()
    for source in queue:
        if id(source) in seen:
            continue
        seen.add(id(source))
        sources.append(source)
        for key in ("result", "task_result", "runtime_result", "cost_breakdown"):
            child = source.get(key)
            if isinstance(child, Mapping):
                queue.append(child)
    return sources


def root_cost_snapshot(payload: Mapping[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Return (present, validated snapshot); explicit invalidity never means absent.

    Phase terminality is the existing managed-root boundary, not a claim that
    unknown remote provider outcomes have become known. The decoder does not
    admit another task type or infer historical evidence from scalar totals.
    """
    sources = frame_accounting_sources(payload)
    checkpoints = [
        source["root_phase_checkpoint"] for source in sources
        if isinstance(source.get("root_phase_checkpoint"), Mapping)
        and "accounting" in source["root_phase_checkpoint"]
    ]
    if not checkpoints:
        return False, None
    root = payload.get("task_id")
    if not isinstance(root, str) or not root or payload.get("_is_direct_chat") is True:
        return True, None
    selected: dict[str, Any] | None = None
    for checkpoint in checkpoints:
        raw = checkpoint["accounting"]
        if (
            checkpoint.get("post_task_synthesis") not in ("completed", "degraded")
            or not isinstance(raw, Mapping)
            or raw.get("schema") != "ouroboros.root_cost_snapshot.v1"
            or raw.get("scope") != "root_tree"
            or raw.get("root_task_id") != root
            or raw.get("cost_accounting_status") != "available"
            or raw.get("ledger_integrity_degraded") is not False
        ):
            return True, None
        counts = raw.get("attempt_counts")
        unresolved = counts.get("unresolved") if isinstance(counts, Mapping) else None
        for value in (raw.get("non_final_rows"), raw.get("unknown_unmetered"), unresolved):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return True, None
        snapshot = dict(raw)
        snapshot["attempt_counts"] = {"unresolved": unresolved}
        for key in ("accounted_upper_bound_usd", "unresolved_upper_bound_usd", "reserved_usd"):
            value = _amount(raw.get(key))
            if value is None:
                return True, None
            snapshot[key] = round(value, 6)
        if (
            unresolved > snapshot["non_final_rows"]
            or snapshot["unresolved_upper_bound_usd"] > snapshot["accounted_upper_bound_usd"]
        ):
            return True, None
        if selected is not None and snapshot != selected:
            return True, None
        selected = snapshot
    for source in sources:
        # An own-task amount may differ from the tree. Only explicit whole-tree
        # replicas share this snapshot's scope and must agree with its amount.
        amount_keys = ("accounted_upper_bound_usd_with_children", "cost_usd_with_children")
        if source.get("authority") == "physical_attempt_ledger":
            amount_keys += ("accounted_upper_bound_usd",)
            count = source.get("non_final_rows")
            if type(count) is not int or count != selected["non_final_rows"]:
                return True, None
        for key in amount_keys:
            if key in source:
                value = _amount(source[key])
                if value is None or round(value, 6) != selected["accounted_upper_bound_usd"]:
                    return True, None
        if "cost_estimated" in source and source["cost_estimated"] is not False:
            return True, None
        if source.get("_is_direct_chat") is True:
            return True, None
        if source.get("artifact_status") in ("pending", "finalizing"):
            return True, None
        if "cost_accounting_status" in source and source["cost_accounting_status"] != "available":
            return True, None
        if "ledger_integrity_degraded" in source and source["ledger_integrity_degraded"] is not False:
            return True, None
        for key in ("reserved_usd", "unknown_unmetered"):
            if key in source:
                value = _amount(source[key])
                if value is None or value > selected[key]:
                    return True, None
        for key in ("task_id", "root_task_id"):
            if key in source and source[key] not in (None, "", root):
                return True, None
    return True, selected
