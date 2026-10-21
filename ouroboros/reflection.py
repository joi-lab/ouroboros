"""The task-reflection log: retained history and host rows, never an automatic lesson writer.

``logs/task_reflections.jsonl`` keeps the automatic post-task reflections earlier
versions wrote and the host rows they appended (one row per late task-acceptance
settlement, no model call; the result review that replaced that machine writes
none). Every row is dated evidence, not current understanding: the acting mind
alone authors lessons, the Pattern Register and the backlog (``knowledge_write``).
A Project task's row lives on its project store with a bounded canonical pointer,
so project facts never enter the global log. The legacy trace classifier remains
available independently of the retired automatic reflection writer.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict

from ouroboros._outcome_tool_errors import _OK_TOOL_STATUSES
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

REFLECTIONS_FILENAME = "task_reflections.jsonl"


def _trace_call_errored(tc: Dict[str, Any]) -> bool:
    """One reading of "this call went wrong".

    Reads the ok-status SSOT rather than a private spelling of it: ``untyped``
    (a successful dynamic provider body nothing typed) and ``ok_autocorrected``
    (a shell command whose regex the host repaired) are ok, not errors.
    """
    return bool(
        tc.get("is_error")
        or str(tc.get("status") or "").strip().lower() not in _OK_TOOL_STATUSES
    )


def append_reflection(drive_root: pathlib.Path, entry: Dict[str, Any]) -> None:
    """Append one row to the canonical reflection log; a failed append only warns."""
    try:
        append_jsonl(drive_root / "logs" / REFLECTIONS_FILENAME, entry)
    except Exception:
        log.warning("Failed to save reflection-log row", exc_info=True)


def append_reflection_routed(env: Any, task: Dict[str, Any], entry: Dict[str, Any]) -> None:
    """Store one row where its task's process memory lives (P1: it must outlive drive pruning).

    A project-scoped task writes on its PROJECT store and leaves a bounded pointer
    row in the canonical log, never the full text, which would carry project facts
    across projects. Any other task writes on the canonical budget drive, never on a
    prunable execution mirror.
    """
    canonical = pathlib.Path(str(task.get("budget_drive_root") or "").strip() or str(env.drive_root))
    try:
        from ouroboros.project_facts import resolve_project_id

        pid = resolve_project_id(task)
    except Exception:
        pid = ""
    if not pid:
        append_reflection(canonical, entry)
        return
    from ouroboros.project_facts import project_reflections_path

    path = project_reflections_path(pid)
    project_write_failed = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(path, entry)
    except Exception:
        project_write_failed = True
        log.warning("Failed to save a project reflection-log row", exc_info=True)
    try:
        append_jsonl(canonical / "logs" / REFLECTIONS_FILENAME, {
            "ts": str(entry.get("ts") or utc_now_iso()),
            "task_id": str(entry.get("task_id") or ""),
            "type": "project_reflection_pointer",
            "project_id": pid,
            "reflection_path": str(path),
            # The pointer never claims a row that was not written: a failed
            # project append is stamped, the gap represented as a gap (P1).
            **({"write_failed": True} if project_write_failed else {}),
        })
    except Exception:
        log.warning("Failed to write canonical reflection pointer", exc_info=True)
