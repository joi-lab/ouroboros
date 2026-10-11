"""Project delete orchestration: fence, cancel, quiesce, tombstone, resume."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest


def _request(drive_root, project_id: str = ""):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(drive_root=drive_root)),
        path_params={"project_id": project_id},
    )


def _wait_for_lifecycle(drive_root, project_id: str, expected: str) -> dict:
    from ouroboros.projects_registry import get_reserved_project

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = get_reserved_project(drive_root, project_id) or {}
        if row.get("lifecycle") == expected:
            return row
        time.sleep(0.01)
    pytest.fail(f"Project {project_id} did not reach {expected}")


@pytest.fixture
def isolated_project_queue(tmp_path, monkeypatch):
    import ouroboros.gateway.projects as gateway
    import supervisor.queue as queue
    # Project deletion lives in supervisor.queue_transitions since the
    # module-size split; task_lifecycle only re-exports its entry points.
    import supervisor.queue_transitions as lifecycle
    import supervisor.workers as workers

    pending: list[dict] = []
    running: dict[str, dict] = {}
    broadcasts: list[tuple[str, object]] = []
    cancelled: list[str] = []

    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", running)
    monkeypatch.setattr(queue, "PENDING", pending)
    monkeypatch.setattr(queue, "RUNNING", running)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(
        lifecycle,
        "_broadcast_projects_changed",
        lambda project_id, chat_id: broadcasts.append((project_id, chat_id)),
    )
    monkeypatch.setattr(
        gateway,
        "_broadcast_projects_changed",
        lambda project_id, chat_id: broadcasts.append((project_id, chat_id)),
    )

    def cancel_task(task_id: str, *, cascade: bool = False) -> bool:
        # GR5-5: the deletion cascades lineage ROOTS only, so the fake must
        # tear down the whole subtree the real cascade would (root + every
        # task whose recorded root is the cascaded id).
        assert cascade is True
        cancelled.append(task_id)
        removed = False
        for task in list(pending):
            tid = str(task.get("id") or "")
            if tid == task_id or str(task.get("root_task_id") or "") == task_id:
                pending.remove(task)
                removed = True
        for tid in list(running):
            task = running[tid].get("task") if isinstance(running[tid], dict) else {}
            if tid == task_id or str((task or {}).get("root_task_id") or "") == task_id:
                running.pop(tid)
                removed = True
        return removed

    monkeypatch.setattr(queue, "cancel_task_by_id", cancel_task)
    with lifecycle._PROJECT_DELETE_WORKERS_LOCK:
        lifecycle._PROJECT_DELETE_WORKERS.clear()
    yield SimpleNamespace(
        pending=pending,
        running=running,
        broadcasts=broadcasts,
        cancelled=cancelled,
        queue=queue,
        lifecycle=lifecycle,
    )
    deadline = time.monotonic() + 3
    while lifecycle._PROJECT_DELETE_WORKERS and time.monotonic() < deadline:
        time.sleep(0.01)
    with lifecycle._PROJECT_DELETE_WORKERS_LOCK:
        lifecycle._PROJECT_DELETE_WORKERS.clear()


def test_delete_cancels_bound_root_and_descendants_then_preserves_tombstone(
    tmp_path, isolated_project_queue
):
    from ouroboros.gateway.projects import api_project_delete
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        get_reserved_project,
        project_binding_for_task,
        reconcile_projects,
        update_project,
    )

    project = create_project(tmp_path, "alpha", name="Alpha")
    folder = tmp_path / "owner-folder"
    folder.mkdir()
    memory = tmp_path / "projects" / "alpha"
    memory.mkdir(parents=True)
    update_project(tmp_path, "alpha", working_dir=str(folder))
    bind_task_to_project(tmp_path, "root-bound", "alpha", origin={"absent": "system"})

    isolated_project_queue.pending.extend([
        {"id": "root-bound", "root_task_id": "root-bound"},
        {
            "id": "child-pending",
            "parent_task_id": "root-bound",
            "root_task_id": "root-bound",
        },
        {"id": "root-stored", "root_task_id": "root-stored", "project_id": "alpha"},
        {"id": "unrelated", "root_task_id": "unrelated", "project_id": "beta"},
    ])
    isolated_project_queue.running["grandchild-running"] = {
        "task": {
            "id": "grandchild-running",
            "parent_task_id": "child-pending",
            "root_task_id": "root-bound",
        }
    }

    # Route ids are compatibility-sanitized; cancellation must use the
    # canonical registry id rather than missing this task on a case variant.
    response = asyncio.run(api_project_delete(_request(tmp_path, "ALPHA")))
    assert response.status_code == 200
    response_body = json.loads(response.body)
    assert response_body["ok"] is True
    assert response_body["project_id"] == "alpha"
    tombstone = _wait_for_lifecycle(tmp_path, "alpha", "tombstoned")

    # GR5-5: only the lineage ROOTS get their own cascade — one cascade (and
    # therefore one summary) per tree; the descendants fall with their root.
    assert set(isolated_project_queue.cancelled) == {"root-bound", "root-stored"}
    assert len(isolated_project_queue.cancelled) == 2, (
        "GR5-5: no redundant per-descendant cascades"
    )
    assert [task["id"] for task in isolated_project_queue.pending] == ["unrelated"]
    assert isolated_project_queue.running == {}
    assert folder.is_dir() and memory.is_dir()
    assert project_binding_for_task(tmp_path, "root-bound")["project_id"] == "alpha"
    assert tombstone["chat_id"] == project["chat_id"]
    # The worker persists the tombstone before broadcasting that new state.
    # Observing the durable lifecycle can therefore win this intentional,
    # tiny scheduling window; wait for the asynchronous notification itself.
    broadcast_deadline = time.monotonic() + 3
    while len(isolated_project_queue.broadcasts) < 2 and time.monotonic() < broadcast_deadline:
        time.sleep(0.01)
    assert len(isolated_project_queue.broadcasts) >= 2  # deleting, then tombstoned

    # Boot reconcile sees the preserved memory store but the reserved id prevents
    # resurrection; the immutable binding remains available for history routing.
    assert reconcile_projects(tmp_path) == 0
    assert get_reserved_project(tmp_path, "alpha")["lifecycle"] == "tombstoned"


def test_delete_cancels_orphan_child_without_a_live_root(tmp_path, isolated_project_queue):
    """GR5-5 counterpart: a child whose recorded root/parent already settled
    has no live ancestor in the set, so the roots-only filter keeps it and it
    gets its own cascade instead of being skipped."""
    from ouroboros.projects_registry import begin_project_deletion, bind_task_to_project, create_project

    project = create_project(tmp_path, "orph", name="Orphan")
    bind_task_to_project(tmp_path, "gone-root", "orph", origin={"absent": "system"})
    isolated_project_queue.pending.append({
        "id": "orphan-child",
        "parent_task_id": "gone-root",
        "root_task_id": "gone-root",
    })

    begin_project_deletion(tmp_path, "orph")
    isolated_project_queue.lifecycle.run_project_deletion(
        tmp_path, "orph", project["chat_id"],
    )

    assert isolated_project_queue.cancelled == ["orphan-child"]
    assert isolated_project_queue.pending == []
    _wait_for_lifecycle(tmp_path, "orph", "tombstoned")


def test_delete_failure_stays_fenced_with_visible_error(tmp_path, isolated_project_queue, monkeypatch):
    from ouroboros.projects_registry import begin_project_deletion, create_project, get_reserved_project

    project = create_project(tmp_path, "stuck", name="Stuck")
    isolated_project_queue.pending.append({"id": "stuck-root", "project_id": "stuck"})
    monkeypatch.setattr(
        isolated_project_queue.queue,
        "cancel_task_by_id",
        lambda _task_id, **_kwargs: False,
    )

    begin_project_deletion(tmp_path, "stuck")
    isolated_project_queue.lifecycle.run_project_deletion(
        tmp_path, "stuck", project["chat_id"]
    )

    row = get_reserved_project(tmp_path, "stuck")
    assert row["lifecycle"] == "deleting"
    assert "did not quiesce" in row["delete_error"]
    assert isolated_project_queue.pending == [{"id": "stuck-root", "project_id": "stuck"}]
    assert isolated_project_queue.broadcasts[-1] == ("stuck", project["chat_id"])


def test_supervisor_retries_deleting_project_with_prior_error(
    tmp_path, isolated_project_queue, monkeypatch,
):
    from ouroboros.projects_registry import begin_project_deletion, create_project, get_reserved_project

    project = create_project(tmp_path, "retry", name="Retry")
    isolated_project_queue.pending.append({"id": "retry-root", "project_id": "retry"})
    working_cancel = isolated_project_queue.queue.cancel_task_by_id
    monkeypatch.setattr(
        isolated_project_queue.queue,
        "cancel_task_by_id",
        lambda _task_id, **_kwargs: False,
    )
    begin_project_deletion(tmp_path, "retry")
    isolated_project_queue.lifecycle.run_project_deletion(
        tmp_path, "retry", project["chat_id"],
    )
    assert get_reserved_project(tmp_path, "retry")["delete_error"]

    monkeypatch.setattr(isolated_project_queue.queue, "cancel_task_by_id", working_cancel)
    assert isolated_project_queue.lifecycle.resume_project_deletions(tmp_path) == 1
    _wait_for_lifecycle(tmp_path, "retry", "tombstoned")
    assert isolated_project_queue.cancelled == ["retry-root"]


def test_supervisor_resumes_deleting_project_after_restart(tmp_path, isolated_project_queue):
    from ouroboros.projects_registry import begin_project_deletion, create_project, reconcile_projects

    create_project(tmp_path, "resume", name="Resume")
    (tmp_path / "projects" / "resume").mkdir(parents=True)
    isolated_project_queue.pending.append({"id": "resume-root", "project_id": "resume"})
    begin_project_deletion(tmp_path, "resume")

    # A fresh process has no in-memory marker. Supervisor startup, not a UI GET,
    # resumes the durable deleting row for headless parity.
    with isolated_project_queue.lifecycle._PROJECT_DELETE_WORKERS_LOCK:
        isolated_project_queue.lifecycle._PROJECT_DELETE_WORKERS.clear()
    assert isolated_project_queue.lifecycle.resume_project_deletions(tmp_path) == 1
    _wait_for_lifecycle(tmp_path, "resume", "tombstoned")
    assert isolated_project_queue.cancelled == ["resume-root"]
    assert reconcile_projects(tmp_path) == 0
