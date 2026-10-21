"""The per-skill lifecycle facts of a task's related skills (visibility only).

A task that authored or repaired a skill used to leave no way to see whether
that skill actually reached a reviewed, ready, enabled state. These facts close
that gap; the task's completion observations carry them to post-task synthesis.
They are facts, never a gate on the execution route.
"""

from __future__ import annotations

import json
import pathlib

from ouroboros.skill_loader import find_skill
from ouroboros.skill_readiness import (
    _skill_names_from_review_history,
    acceptance_skill_lifecycle,
    skill_names_touched_by_trace,
    skill_readiness_for_execution,
)


def _write_skill(drive_root: pathlib.Path, name: str) -> pathlib.Path:
    skill_dir = drive_root / "skills" / "external" / name
    skill_dir.mkdir(parents=True)
    (drive_root / "state" / "skills" / name).mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ntype: instruction\nversion: 0.1.0\n---\nbody\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_history(drive_root: pathlib.Path, name: str, root_task_id: str) -> None:
    path = drive_root / "state" / "skills" / name / "review_history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": "2026-09-03T00:00:00+00:00", "status": "pass",
                    "root_task_id": root_task_id}) + "\n",
        encoding="utf-8",
    )
    projection = drive_root / "state" / "skill_review_root_tasks.jsonl"
    with projection.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"root_task_id": root_task_id, "skill": name}) + "\n")


def test_the_lifecycle_scan_joins_trace_edits_and_this_root_task_history(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_skill(drive_root, "edited")
    _write_skill(drive_root, "child_authored")
    _write_skill(drive_root, "someone_elses")
    _write_history(drive_root, "child_authored", "root-1")
    _write_history(drive_root, "someone_elses", "root-other")

    trace = {"tool_calls": [
        {"tool": "write_file", "args": {"path": "data/skills/external/edited/SKILL.md"}},
    ]}
    rows = acceptance_skill_lifecycle(drive_root, trace, "root-1")
    names = [row["name"] for row in rows]
    assert names == ["edited", "child_authored"]
    assert "someone_elses" not in names


def test_the_lifecycle_facts_match_the_readiness_predicate(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_skill(drive_root, "alpha")
    trace = {"tool_calls": [
        {"tool": "write_file", "args": {"bucket": "external", "skill_name": "alpha",
                                        "path": "SKILL.md"}},
    ]}
    row = acceptance_skill_lifecycle(drive_root, trace, "")[0]
    skill = find_skill(drive_root, "alpha")
    readiness = skill_readiness_for_execution(drive_root, skill)

    assert row["name"] == "alpha"
    assert row["source"] == skill.source
    assert row["review_status"] == skill.review.status
    assert row["ready"] is readiness.ready
    assert row["blockers"] == readiness.blockers
    assert row["enabled"] is bool(skill.enabled)
    # An unreviewed, disabled self-authored skill is exactly what must stay
    # visible, so the row must be honest rather than empty.
    assert row["ready"] is False
    assert row["blockers"]


def test_a_skill_lifecycle_tool_call_names_the_skill_without_a_payload_edit(tmp_path):
    """A free delegation lane integrates a patch and never calls write_file, so
    the lifecycle tools are the only carrier of the name in that shape."""
    trace = {"tool_calls": [
        {"tool": "skill_review", "args": {"skill": "delegated"}},
        {"tool": "skill_preflight", "args": {"skill": "probed"}},
        {"tool": "skill_exec", "args": {"skill": "executed", "script": "scripts/run.py"}},
        {"tool": "toggle_skill", "args": {"skill": "toggled", "enabled": True}},
        {"tool": "submit_skill_to_hub", "args": {
            "skill": "published", "confirm_public_submission": True,
        }},
        {"tool": "edit_text", "args": {
            "root": "skill_payload", "bucket": "user_repo",
            "skill_name": "user-repo-skill", "path": "SKILL.md",
            "old_text": "old", "new_text": "new",
        }},
        {"tool": "run_command", "args": {"cmd": "ls"}},
        {"tool": "run_command", "args": {
            "cmd": ["python3", "repair.py"], "cwd": "skill_payload/scripts",
            "bucket": "external", "skill_name": "command-edited",
        }},
        {"tool": "run_script", "args": {
            "script": "repair()", "cwd": "skill_payload",
            "bucket": "clawhub", "skill_name": "script-edited",
        }},
        {"tool": "delegate_start", "args": {
            "prompt": "repair", "root": "skill_payload",
            "bucket": "user_repo", "skill_name": "delegate-edited",
        }},
        {"tool": "delegate_start", "args": {
            "prompt": "ordinary workspace", "skill_name": "not-selected",
        }},
    ]}
    assert skill_names_touched_by_trace(trace) == [
        "delegated", "probed", "executed", "toggled", "published",
        "user-repo-skill", "command-edited", "script-edited", "delegate-edited",
    ]


def test_real_skill_payload_selectors_feed_lifecycle_rows(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    for name in ("command-edited", "script-edited", "delegate-edited"):
        _write_skill(drive_root, name)
    trace = {"tool_calls": [
        {"tool": "run_command", "args": {
            "cmd": ["python3", "repair.py"], "cwd": "skill_payload/scripts",
            "bucket": "external", "skill_name": "command-edited",
        }},
        {"tool": "run_script", "args": {
            "script": "repair()", "cwd": "skill_payload",
            "bucket": "external", "skill_name": "script-edited",
        }},
        {"tool": "delegate_start", "args": {
            "prompt": "repair", "root": "skill_payload",
            "bucket": "external", "skill_name": "delegate-edited",
        }},
    ]}

    assert [row["name"] for row in acceptance_skill_lifecycle(
        drive_root, trace, "root-1",
    )] == ["command-edited", "script-edited", "delegate-edited"]


def test_no_touched_skill_yields_no_lifecycle_rows(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    assert acceptance_skill_lifecycle(drive_root, {"tool_calls": []}, "") == []
    assert acceptance_skill_lifecycle(None, {"tool_calls": []}, "") == []


def test_bounded_history_projection_discloses_both_omission_limits(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    _write_skill(drive_root, "visible")
    projection = drive_root / "state" / "skill_review_root_tasks.jsonl"
    projection.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        json.dumps({
            "root_task_id": "other", "skill": f"skill-{index}",
            "padding": "x" * 1500,
        })
        for index in range(800)
    ]
    rows.append(json.dumps({"root_task_id": "root-1", "skill": "visible"}))
    projection.write_text("\n".join(rows) + "\n", encoding="utf-8")

    history = _skill_names_from_review_history(drive_root, "root-1")
    assert history["names"] == ["visible"]
    assert history["coverage"]["rows_scanned"] <= 512
    assert history["coverage"]["truncated"] is True
    assert set(history["coverage"]["gap_reasons"]) >= {
        "tail_bytes_truncated", "max_entries_truncated",
    }
    assert history["coverage"]["source_ref"] == {
        "kind": "canonical_jsonl",
        "path": "state/skill_review_root_tasks.jsonl",
        "reader": "read_file",
    }
