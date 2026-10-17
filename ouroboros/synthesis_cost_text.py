"""Project the pre-synthesis usage snapshot's cost.

Extracted from ``ouroboros/agent_task_pipeline.py`` at its module-size ceiling:
one coherent concern — the flat cost fields of the task_summary row. The
pipeline re-exports the name (same object), so tests keep the historical surface.
"""

from __future__ import annotations

from typing import Any, Dict

from ouroboros.task_results import TASK_COST_META_FIELDS


def _summary_row_cost_fields(usage: Dict[str, Any]) -> Dict[str, Any]:
    """Flat task-scope cost fields for the task_summary chat row (v6.82 P1).

    Mapped explicitly from the pre-synthesis usage snapshot: only that snapshot's
    honest keys are copied. Its schema deliberately differs from the full browser
    set and a non-root snapshot without accounting keys yields nothing. Never
    fabricates values; the terminal task_results checkpoint stays authoritative.
    """
    return {key: usage[key] for key in TASK_COST_META_FIELDS if key in usage}


__all__ = [
    "_summary_row_cost_fields",
]
