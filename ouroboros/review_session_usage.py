"""UsageScope-to-custody attribution for delegated review sessions."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from ouroboros._usage_rows import REVIEW_ATTRIBUTION_KEYS


def session_invocation_fields(invocation: Any) -> Tuple[Any, ...]:
    """Unpack the session policy fields without reinterpreting them."""
    return (
        invocation.task_id,
        invocation.surface,
        invocation.slot_id,
        invocation.timeout_sec,
        invocation.logical_key_extra,
        invocation.output_schema,
        invocation.session_route,
        invocation.instructions,
        invocation.retry_state,
    )


def session_custody_attribution(scope: Any) -> Tuple[str, str, Dict[str, str]]:
    """Project task lineage and every landed review-attribution key verbatim."""
    root_task_id = str(getattr(scope, "root_task_id", "") or "")
    parent_task_id = str(getattr(scope, "parent_task_id", "") or "")
    usage_custody = {
        "category": str(getattr(scope, "category", "") or "subagent"),
        "source": str(getattr(scope, "source", "") or "delegated_subagent"),
        **{
            key: str(getattr(scope, key, "") or "")
            for key in REVIEW_ATTRIBUTION_KEYS
        },
    }
    return root_task_id, parent_task_id, usage_custody
