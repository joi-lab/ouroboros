"""Unknown-outcome and retry facts docs/CLIENT_CONTRACT.md states, pinned on the real handlers.

A client decides whether to resend from these answers alone, so each published rule is checked
end to end: the refusal, what a read shows afterwards, and what the same request does again.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.contracts import ErrorResponse, TaskCreateResponse
from ouroboros.gateway.schema import validate_ingress
from ouroboros.gateway.tasks import api_task_get, api_tasks_create
from tests._headless_cli_shared import _managed_worker_pool_available  # noqa: F401  (autouse)
from tests.test_quiz_history_evidence import runtime as runtime  # fixture


def _task_client(tmp_path) -> TestClient:
    data, repo = tmp_path / "data", tmp_path / "repo"
    (data / "memory").mkdir(parents=True)
    repo.mkdir()
    app = Starlette(routes=[
        Route("/api/tasks", endpoint=api_tasks_create, methods=["POST"]),
        Route("/api/tasks/{task_id}", endpoint=api_task_get, methods=["GET"]),
    ])
    app.state.drive_root = data
    app.state.repo_dir = repo
    return TestClient(app)


def test_a_503_without_unconfirmed_scheduled_nothing_and_the_same_id_can_be_sent_again(tmp_path, monkeypatch):
    import supervisor.queue as queue
    from ouroboros.gateway import tasks

    monkeypatch.setattr(queue, "enqueue_task", lambda task: task)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda **_kwargs: True)
    real = tasks.stage_initial_task_attachments
    monkeypatch.setattr(tasks, "stage_initial_task_attachments",
                        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk unavailable")))
    client = _task_client(tmp_path)
    refused = client.post("/api/tasks", json={"task_id": "staging-503", "description": "plan it"})
    assert refused.status_code == 503 and refused.json().get("status") != "unconfirmed"
    assert not validate_ingress(refused.json(), ErrorResponse)
    assert client.get("/api/tasks/staging-503").status_code == 404
    monkeypatch.setattr(tasks, "stage_initial_task_attachments", real)
    accepted = client.post("/api/tasks", json={"task_id": "staging-503", "description": "plan it"})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["task_id"] == "staging-503" and accepted.json()["status"] == "scheduled"
    assert not validate_ingress(accepted.json(), TaskCreateResponse)


def test_an_unconfirmed_admission_reads_as_absent_but_is_never_started_twice(tmp_path, monkeypatch):
    import supervisor.queue as queue

    pending: list = []
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(queue, "PENDING", pending)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": reason == "api_task_create_rollback")
    client = _task_client(tmp_path)
    unknown = client.post("/api/tasks", json={"task_id": "maybe", "description": "plan it", "memory_mode": "empty"})
    assert unknown.status_code == 503
    assert unknown.json()["status"] == "unconfirmed" and unknown.json()["task_id"] == "maybe"
    # The task may still run: it sits in the live queue while its record reads as absent.
    assert [row["id"] for row in pending] == ["maybe"]
    assert client.get("/api/tasks/maybe").status_code == 404, "a 404 here is not proof it will not run"
    again = client.post("/api/tasks", json={"task_id": "maybe", "description": "plan it", "memory_mode": "empty"})
    assert again.status_code == 409, "the same task_id never starts a second copy"
    assert [row["id"] for row in pending] == ["maybe"]


def test_a_late_answer_follows_the_recorded_publication_and_an_unproven_one_is_never_guessed(runtime, monkeypatch):
    """§7 Late answers over HTTP: a hidden-partition ask shown in Main is answered in Main; an older
    card without a proven address stays recorded and owed, and repeating the request sends nothing."""
    import queue as queue_module

    from ouroboros import owner_quiz
    from ouroboros.gateway.task_decision import api_decision_answer
    from ouroboros.task_results import write_task_result
    from supervisor import queue
    from tests.test_quiz_history_evidence import _ask

    task_id = runtime.task["id"]
    write_task_result(runtime.root, task_id, "running", chat_id=0)
    _ask(runtime, "shown", chat_id=0)
    assert runtime.frames[-1]["chat_id"] == 1, "the hidden ask is published in Main"
    owner_quiz.record_asked(runtime.root, task_id, quiz_id="old", question="Keep?", options=["Yes", "No"],
                            assumption="Keep")
    monkeypatch.setattr(queue, "RUNNING", {})
    write_task_result(runtime.root, task_id, "completed", result="done")
    app = Starlette(routes=[Route("/api/decisions", endpoint=api_decision_answer, methods=["POST"])])
    app.state.drive_root = runtime.root
    client = TestClient(app)

    def answer(quiz_id):
        return client.post("/api/decisions", json={"request_id": f"late-{quiz_id}", "option_index": 0,
                                                   "decision_id": f"quiz:{task_id}:{quiz_id}"})

    shown = answer("shown")
    assert shown.status_code == 200 and shown.json()["answered_after_terminal"] is True
    assert shown.json()["forwarded"] is True
    assert runtime.bridge._inbox.get_nowait()["chat_id"] == 1, "the recorded publication, not the ask chat 0"
    for _attempt in range(2):
        old = answer("old")
        assert old.status_code == 200 and old.json()["forwarded"] is False
        assert old.json()["reason_code"] == "publication_address_unknown"
        assert old.json()["question_delivery"]["state"] == "owed"
        with pytest.raises(queue_module.Empty):
            runtime.bridge._inbox.get_nowait()
    assert owner_quiz.quiz_states(runtime.root, task_id)["old"]["state"] == "answered"
