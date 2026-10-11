"""History retains wait evidence even after a closed quiz leaves the hot projection."""
import json

import pytest

from ouroboros.gateway.history import _assemble_history_response
from ouroboros.owner_quiz import record_asked, record_answered, quiz_states
from ouroboros.owner_wait import set_owner_wait
from ouroboros.projects_registry import create_project
from ouroboros.task_results import write_task_result
from ouroboros.utils import append_jsonl


@pytest.mark.parametrize("required", [True, False])
def test_evicted_closed_question_uses_durable_ask_wait_evidence(tmp_path, required):
    project = create_project(tmp_path, "eviction", name="Eviction")
    write_task_result(tmp_path, "t", "running", project_id=project["id"], chat_id=project["chat_id"])
    for i in range(17):
        qid = f"q{i}"
        record_asked(tmp_path, "t", quiz_id=qid, question=qid, options=["A", "B"],
                     wait_for_answer=required if i == 0 else True)
        if i == 0:
            # A page can retain the ask without the later answer event. The hot
            # projection still knows the answer until the following asks evict it.
            append_jsonl(tmp_path / "logs/chat.jsonl", {
                "type": "quiz", "direction": "out", "chat_id": project["chat_id"],
                "task_id": "t", "ts": "2026-09-18T20:00:00Z", "text": qid,
                "quiz": {"quiz_id": qid, "question": qid, "options": ["A", "B"],
                         "state": "open", "wait_for_answer": required}})
            record_answered(tmp_path, "t", quiz_id=qid, option_index=0, request_id="answer")
    set_owner_wait(tmp_path, "t", {"quiz_id": "q16", "wait_id": "w", "state": "waiting"})
    assert "q0" not in quiz_states(tmp_path, "t")
    rows = json.loads(_assemble_history_response(tmp_path, project["chat_id"], 100, 0))["messages"]
    quiz = next(row["quiz"] for row in rows if row.get("msg_type") == "quiz")
    assert quiz.get("owner_wait_state") == ("resumed" if required else None)
    assert "answered_index" not in quiz, "Missing answer evidence must not be fabricated"
    main = json.loads(_assemble_history_response(tmp_path, 1, 100, 0))["messages"][0]
    assert main["quiz_state"] == "unknown", "Lost lifecycle remains unavailable, not guessed"
    assert main.get("owner_wait_state") == ("resumed" if required else None)
