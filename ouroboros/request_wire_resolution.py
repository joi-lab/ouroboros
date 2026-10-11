"""Deterministic composition of route-scoped request-wire evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Tuple

from ouroboros.deadline_utils import parse_deadline_ts
from ouroboros.request_wire_contract import (
    RequestWireProfile,
    StoredWireAction,
    apply_effort_action,
    canonical_json,
    validate_wire_action,
    validate_wire_action_for_profile,
    wire_action_identity,
)


@dataclass(frozen=True)
class ResolvedWireActions:
    requested_effort: str
    applied_effort: str
    source_tool_dialect: str
    applied_tool_dialect: str
    drop_fields: Tuple[str, ...]
    actions: Tuple[Mapping[str, Any], ...]
    effort_conflict: bool = False


@dataclass(frozen=True)
class EffortActionSetResolution:
    requested_effort: str
    applied_effort: str
    actions: Tuple[Mapping[str, Any], ...]
    conflict: bool = False


def resolve_effort_action_set(
    requested_effort: str,
    actions: Iterable[Mapping[str, Any]],
) -> EffortActionSetResolution:
    """Resolve bounds and exact transitions as one ordered physical chain."""
    from ouroboros.config import EFFORT_SCALE, effort_rank

    requested = str(requested_effort or "").strip().lower()
    if requested not in EFFORT_SCALE:
        raise ValueError("unsupported requested effort")
    normalized = [
        action
        for raw in actions
        for action in [validate_wire_action(raw)]
        if action.get("kind") == "set_value"
    ]
    floors = [action for action in normalized if action.get("mode") == "floor"]
    ceilings = [action for action in normalized if action.get("mode") == "ceiling"]
    def _select_bound(candidates, *, strongest):
        if not candidates:
            return None
        rank = strongest(effort_rank(str(action.get("to") or "")) for action in candidates)
        tied = [
            action for action in candidates
            if effort_rank(str(action.get("to") or "")) == rank
        ]
        return min(tied, key=canonical_json)

    lower_action = _select_bound(floors, strongest=max)
    upper_action = _select_bound(ceilings, strongest=min)
    lower_rank = effort_rank(str(lower_action.get("to"))) if lower_action else 0
    upper_rank = (
        effort_rank(str(upper_action.get("to")))
        if upper_action else len(EFFORT_SCALE) - 1
    )
    if lower_rank > upper_rank:
        return EffortActionSetResolution(requested, requested, (), conflict=True)

    exact_by_source: dict[str, Mapping[str, Any]] = {}
    for action in normalized:
        if action.get("mode") != "exact":
            continue
        source = str(action.get("from") or "")
        previous = exact_by_source.get(source)
        if previous is not None and previous != action:
            return EffortActionSetResolution(requested, requested, (), conflict=True)
        exact_by_source[source] = action

    current = requested
    selected = []
    visited = set()
    while True:
        if current in visited:
            return EffortActionSetResolution(requested, requested, (), conflict=True)
        visited.add(current)
        current_rank = effort_rank(current)
        if current_rank < lower_rank:
            action = lower_action
        elif current_rank > upper_rank:
            action = upper_action
        else:
            action = exact_by_source.get(current)
        if action is None:
            break
        next_effort = apply_effort_action(current, action)
        if next_effort == current:
            return EffortActionSetResolution(requested, requested, (), conflict=True)
        selected.append(action)
        current = next_effort
    return EffortActionSetResolution(
        requested_effort=requested,
        applied_effort=current,
        actions=tuple(dict(action) for action in selected),
    )


def _record_sort_key(record: StoredWireAction) -> float:
    parsed = parse_deadline_ts(record.observed_at)
    return parsed.timestamp() if parsed is not None else float("-inf")


def resolve_wire_actions(
    profile: RequestWireProfile,
    *,
    requested_effort: str,
    records: Iterable[StoredWireAction],
) -> ResolvedWireActions:
    """Resolve independent actions from the original request, never in arrival order."""
    from ouroboros.config import EFFORT_SCALE

    requested = str(requested_effort or "").strip().lower()
    if requested not in EFFORT_SCALE:
        raise ValueError("unsupported requested effort")
    valid_records = [
        StoredWireAction(action=action, observed_at=record.observed_at)
        for record in records
        if isinstance(record, StoredWireAction)
        for action in [validate_wire_action_for_profile(profile, record.action)]
        if action
    ]
    drop_fields = sorted({
        field_name
        for record in valid_records
        if record.action.get("kind") == "drop_field"
        for field_name in record.action.get("fields") or []
    })
    dialect_records = [
        record for record in valid_records
        if record.action.get("kind") == "replace_dialect"
        and record.action.get("from") == profile.tool_dialect
    ]
    dialect_record = max(dialect_records, key=_record_sort_key) if dialect_records else None
    applied_dialect = (
        str(dialect_record.action.get("to")) if dialect_record is not None
        else profile.tool_dialect
    )

    effort_records = [
        record for record in valid_records
        if record.action.get("kind") == "set_value"
    ]
    effort_resolution = resolve_effort_action_set(
        requested, (record.action for record in effort_records)
    )
    selected_effort_records: list[StoredWireAction] = []
    remaining = list(effort_records)
    for selected_action in effort_resolution.actions:
        candidates = [record for record in remaining if record.action == selected_action]
        if candidates:
            chosen = max(candidates, key=_record_sort_key)
            selected_effort_records.append(chosen)
            remaining.remove(chosen)
    non_effort = [
        record for record in valid_records
        if record.action.get("kind") == "drop_field"
    ]
    non_effort.sort(key=lambda item: (
        wire_action_identity(item.action), _record_sort_key(item)
    ))
    selected = [
        *non_effort,
        *([dialect_record] if dialect_record is not None else []),
        *selected_effort_records,
    ]
    return ResolvedWireActions(
        requested_effort=requested,
        applied_effort=effort_resolution.applied_effort,
        source_tool_dialect=profile.tool_dialect,
        applied_tool_dialect=applied_dialect,
        drop_fields=tuple(drop_fields),
        actions=tuple(dict(record.action) for record in selected),
        effort_conflict=effort_resolution.conflict,
    )
