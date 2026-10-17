"""One read-only present for new heads, derived from the independent-root roster.

Focus is the model's authored account of its work. It is deliberately distinct
from the web card's latest visible narration and never supplies routing authority.
"""
from __future__ import annotations

import pathlib


def root_present_fields(root, task_id, task, stored, *, settled=False):
    """Enrich the roster using the task document it has already read."""
    from ouroboros.project_admission import project_hold_fact
    from ouroboros.projects_registry import get_project, project_binding_for_task

    fields = {}
    if root is not None:
        binding = project_binding_for_task(root, task_id) or {}
        if binding:
            fields.update(project_id=binding["project_id"], chat_id=binding.get("project_chat_id"))
        project = get_project(root, fields.get("project_id") or task.get("project_id") or "") or {}
        fields["project_name"] = str(project.get("name") or "")
    if settled:
        fields["recorded_status"] = stored["status"]
        return fields
    quizzes = stored.get("owner_quiz") or {}
    questions = [{"quiz_id": quiz_id, **{key: quiz[key] for key in (
        "question", "options", "asked_at", "wait_for_answer", "state") if key in quiz}}
        for quiz_id, quiz in quizzes.items() if isinstance(quiz, dict) and quiz.get("state", "open") == "open"]
    if questions:
        fields["open_questions"] = questions
    hold = project_hold_fact({**stored, **task})
    if hold:
        fields["project_admission_hold"] = hold
    return fields


def live_works_present(root, *, exclude=""):
    """Project every host-listed root; stale/missing source observations stay explicit."""
    from ouroboros.peer_roster import independent_roots

    roster = independent_roots(pathlib.Path(root))
    works = []
    for row in roster["roots"]:
        if row["task_id"] == exclude:
            continue
        focus = row.get("focus") or {}
        works.append({"task_id": row["task_id"], "title": row.get("title") or row.get("suggested_name") or "",
            "room": {key: row.get(key) for key in ("chat_id", "project_id", "project_name")},
            "state": row["status"], "started_at": row.get("started_at"),
            "open_questions": row.get("open_questions") or [], "waiting": row.get("waiting") or [],
            "last_line": focus.get("text", ""), "last_line_at": focus.get("authored_at"),
            **{key: row[key] for key in ("focus", "project_admission_hold", "recorded_status") if key in row}})
    return {"works": works, "last_line_source": "model_authored_focus", "incomplete": roster["incomplete"],
            **{key: roster[key] for key in ("queue_snapshot", "direct_roots")}}


def context_present(env, task):
    """Capture at head construction, including queued heads with older ingress metadata."""
    root = task.get("budget_drive_root") or getattr(env, "budget_drive_root", None) or env.drive_root
    return live_works_present(root, exclude=str(task.get("id") or ""))
