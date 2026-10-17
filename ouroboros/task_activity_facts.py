"""Incremental card display facts on the existing task document, never activity authority.

The delivery seam records visible model narration; the loop publishes its existing
tool-result counter. Census reads the document it already reads, without replaying
chat or tool history. Old tasks without these facts keep an empty narration line.
"""
from __future__ import annotations

import logging

from ouroboros.task_store import patch_document

log = logging.getLogger(__name__)


def _record(root, task_id, facts):
    if not root or not task_id or not facts:
        return

    def update(current):
        if not current:  # A display observation cannot create a lifecycle owner.
            return None
        changes = dict(facts)
        if "tool_calls" in changes:
            changes["tool_calls"] = max(changes["tool_calls"], current.get("tool_calls") or 0)
        if "last_line_at" in changes and changes["last_line_at"] < str(current.get("last_line_at") or ""):
            changes.pop("last_line")
            changes.pop("last_line_at")
        return {**current, **changes} if any(current.get(k) != v for k, v in changes.items()) else None

    try:
        patch_document(root, task_id, update)
    except Exception:
        log.debug("Task card display facts unavailable for %s", task_id, exc_info=True)


def record_narration(root, row):
    """Only an appended, explicitly model-authored progress row supplies this line."""
    if row.get("narration") is not True:
        return
    text = str(row.get("content", row.get("text", "")) or "")
    # The worker's presentation prefix is removed by the room card as well.
    if text.startswith("💬"):
        text = text[1:].lstrip()
    if text.strip():
        _record(root, row.get("task_id"), {"last_line": text, "last_line_at": str(row.get("ts") or "")})


def record_tool_count(ctx, activity):
    """Publish the counter already accumulated by task_pacing, including restored usage."""
    _record(getattr(ctx, "budget_drive_root", None) or getattr(ctx, "drive_root", None),
            getattr(ctx, "task_id", ""), {"tool_calls": sum(row["calls"] for row in activity.values())})


def card_facts(task):
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    calls = task.get("tool_calls")
    return {"last_line": str(task.get("last_line") or ""),
            "last_line_at": str(task.get("last_line_at") or ""),
            "tool_calls": calls if type(calls) is int and calls >= 0 else 0,
            "detached": bool(task.get("detached_at") or metadata.get("detached_at"))}


def enrich_activity(activity, facts, projects):
    """Apply cached task facts after exact-work room binding has been resolved."""
    card = facts.get("card") or card_facts({})
    detached = bool(activity.get("detached") or card["detached"])
    activity.update(card, detached=detached)
    project = projects.get(str(activity.get("project_id") or "")) or {}
    activity["project_name"] = str(project.get("name") or activity.get("project_name") or "")
