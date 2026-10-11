"""The admitted Project address and nested waiting facts reach real gateways."""
import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ouroboros import projects_registry as registry
from ouroboros.gateway.tasks import _create_task_from_body, api_task_get, api_tasks_list
from ouroboros.gateway.state import _chat_activities_snapshot_safe
from ouroboros.task_results import load_task_result
from supervisor import queue, workers
from tests.test_swarm_host_admission import host as host_fixture
from tests.test_project_authority_outages import _uncertain_child
from tests.test_project_hold_recovery import accepted, worker

host = host_fixture
pytestmark = pytest.mark.serial


def request_for(host, task_id="", query=b""):
    return Request({"type": "http", "query_string": query, "path_params": {"task_id": task_id},
                    "app": SimpleNamespace(state=SimpleNamespace(drive_root=host.root, repo_dir=workers.REPO_DIR))})


@pytest.mark.parametrize("explicit", [False, True])
def test_project_api_uses_captured_address_without_a_second_authority_read(host, monkeypatch, explicit):
    project = registry.create_project(host.root, "target", name="Target")
    reads = []

    def unavailable(*args, **kwargs):
        reads.append(args)
        raise OSError("secondary lookup is unavailable")

    monkeypatch.setattr(registry, "get_reserved_project", unavailable)
    body = {"task_id": "addressed", "description": "Project work", "project_id": "target", "memory_mode": "empty"}
    if explicit:
        body["chat_id"] = project["chat_id"]
    response = _create_task_from_body(request_for(host), body)
    assert response.status_code == 200, response.body
    [queued] = host.pending
    stored = load_task_result(host.root, "addressed", strict=True)
    assert queued["chat_id"] == stored["chat_id"] == project["chat_id"]
    assert queued["_project_admission"]["project"]["chat_id"] == project["chat_id"]
    assert not reads and not host.attempts
    from supervisor.log_addressing import address_task_event
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert [task["id"] for task in sent] == ["addressed"]
    assert address_task_event(host.running, host.root, {"type": "llm_usage", "task_id": "addressed"})["chat_id"] == project["chat_id"]


@pytest.mark.parametrize("address", [0, 1, 999])
def test_project_api_retains_explicit_conflict_refusal(host, address):
    registry.create_project(host.root, "target")
    response = _create_task_from_body(request_for(host), {"task_id": "conflict", "description": "Project work",
        "project_id": "target", "chat_id": address, "memory_mode": "empty"})
    assert response.status_code == 400 and b"conflicts" in response.body
    assert not host.pending and load_task_result(host.root, "conflict") is None


@pytest.mark.parametrize("explicit", [False, True])
def test_project_api_inactive_room_keeps_the_admission_owners_typed_refusal(host, explicit):
    project = registry.create_project(host.root, "target")
    registry.begin_project_deletion(host.root, "target")
    body = {"task_id": "inactive", "description": "Project work", "project_id": "target", "memory_mode": "empty"}
    if explicit:
        body["chat_id"] = project["chat_id"]
    response = _create_task_from_body(request_for(host), body)
    assert response.status_code == 409, response.body
    assert json.loads(response.body)["admission"]["reason_code"] == "project_routing_fence"
    assert not host.pending


def test_queue_gateway_preserves_child_wait_and_recovery_without_root_census_inflation(host, tmp_path, monkeypatch):
    accepted(host, tmp_path, tid="sibling")
    child, original = _uncertain_child(host, monkeypatch)
    sent = worker(host, monkeypatch)
    workers.assign_tasks()
    assert [task["id"] for task in sent] == ["sibling"]
    assert [row["activity_id"] for row in _chat_activities_snapshot_safe(host.root)] == ["sibling"]
    # Its unreadable result cannot deliver detail, but accepted queue custody can.
    assert host.run_async(api_task_get(request_for(host, "held"))).status_code in (404, 503)
    before = queue.QUEUE_SNAPSHOT_PATH.read_bytes()
    response = host.run_async(api_tasks_list(request_for(host, query=b"queue_only=1")))
    assert response.status_code == 200
    payload = json.loads(response.body)
    held = next(row["task"] for row in payload["queue"]["pending"] if row["id"] == child["id"])
    assert held["project_admission_hold"]["label"] == "Waiting for Project verification"
    assert "unreadable" in held["project_admission_hold"]["detail"]
    assert queue.QUEUE_SNAPSHOT_PATH.read_bytes() == before  # passive projection
    host.running.clear()
    workers.WORKERS[0].busy_task_id = None
    (host.root / "task_results" / "held.json").write_bytes(original)
    workers.assign_tasks()
    workers.assign_tasks()
    assert [task["id"] for task in sent] == ["sibling", "held"]
    recovered = json.loads(host.run_async(api_tasks_list(request_for(host, query=b"queue_only=1"))).body)
    row = next(row["task"] for row in recovered["queue"]["running"] if row["id"] == child["id"])
    assert row["project_admission_hold"] == {}
    assert not host.attempts
