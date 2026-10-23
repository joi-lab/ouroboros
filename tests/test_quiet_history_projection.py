"""Quiet terminal facts survive the browser history boundary and result retention."""
import json

import pytest

from ouroboros import dialogue_store
from ouroboros.gateway.history import _assemble_history_response, _copy_task_summary_metadata
from ouroboros.projects_registry import bind_task_to_project, create_project
from ouroboros.task_results import write_task_result
from ouroboros.task_store import delete_document
from ouroboros.terminal_projection import _project_row


@pytest.mark.parametrize("silent,notice", [(True, ""), (True, "A tool failed."), (False, "")],
                         ids=["quiet", "quiet-with-notice", "ordinary"])
@pytest.mark.parametrize("retained", [True, False], ids=["result-present", "result-pruned"])
def test_terminal_flags_round_trip_through_room_history(tmp_path, silent, notice, retained):
    project = create_project(tmp_path, "checks", name="Scheduled checks")
    task_id = "scheduled-check"
    bind_task_to_project(tmp_path, task_id, project["id"], project["chat_id"], origin={"absent": "system"})
    stored = write_task_result(tmp_path, task_id, "completed", chat_id=project["chat_id"],
                               project_id=project["id"], root_task_id=task_id, delegation_role="root",
                               result="Private retained words", terminal_origin="model_final",
                               wake_silent=silent, terminal_host_notice=notice)
    terminal = _project_row(task_id, stored, {}, {"chat_id": project["chat_id"]})
    dialogue_store.append(tmp_path, "chat", terminal)
    if not retained:
        assert delete_document(tmp_path, task_id)
    response = json.loads(_assemble_history_response(tmp_path, project["chat_id"], 50, 200))
    rows = [row for row in response["messages"] if row.get("task_id") == task_id]
    summary = next(row for row in rows if row.get("system_type") == "task_summary"
                   and "wake_silent" in row)
    expected = {"wake_silent": silent, "quiet_effective": silent and not notice}
    assert {key: summary[key] for key in expected} == expected
    if expected["quiet_effective"]:
        assert summary["text"] == "Done · no message"
    else:
        assert summary["text"] != "Done · no message"
    if not retained:
        historical = next(row["historical_terminal"] for row in rows if "historical_terminal" in row)
        assert {key: historical[key] for key in expected} == expected
    assert "Private retained words" not in json.dumps(response)


@pytest.mark.parametrize("flags", [{}, {"wake_silent": True}, {"quiet_effective": False},
                                  {"wake_silent": False, "quiet_effective": False}])
def test_legacy_summary_flags_stay_present_or_absent_as_recorded(tmp_path, flags):
    entry = {"type": "task_summary", "direction": "system", "chat_id": 1,
             "task_id": "legacy", "text": "Recorded terminal", "ts": "2026-10-21T12:00:00Z", **flags}
    dialogue_store.append(tmp_path, "chat", entry)
    rows = json.loads(_assemble_history_response(tmp_path, 1, 50, 200))["messages"]
    summary = next(row for row in rows if row.get("system_type") == "task_summary")
    assert {key: summary[key] for key in ("wake_silent", "quiet_effective") if key in summary} == flags


def test_non_summary_rows_do_not_acquire_terminal_flags():
    projected = {}
    _copy_task_summary_metadata(projected, {"type": "message", "wake_silent": True, "quiet_effective": True})
    assert projected == {}
