"""An ordinary project folder reaches managed admission without Git side effects."""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._headless_cli_shared import _managed_worker_pool_available  # noqa: F401


def test_attach_plain_folder_then_enqueue_managed_task(tmp_path, monkeypatch):
    from ouroboros.gateway.projects import api_projects_create
    from ouroboros.gateway.tasks import api_tasks_create
    from ouroboros.task_results import load_task_result
    from ouroboros.workspace_admission import resolve_room_workspace

    data = tmp_path / "data"
    data.mkdir()
    system_repo = tmp_path / "system-repo"
    system_repo.mkdir()
    folder = tmp_path / "documents"
    folder.mkdir()
    (folder / "draft.txt").write_text("Owner's draft\n", encoding="utf-8")
    captured = []

    def enqueue(task):
        captured.append(task)
        return task

    monkeypatch.setattr("supervisor.queue.enqueue_task", enqueue)
    monkeypatch.setattr("supervisor.queue.persist_queue_snapshot", lambda reason="": True)
    app = Starlette(routes=[
        Route("/api/projects", api_projects_create, methods=["POST"]),
        Route("/api/tasks", api_tasks_create, methods=["POST"]),
    ])
    app.state.drive_root = data
    app.state.repo_dir = system_repo
    client = TestClient(app)

    attached = client.post("/api/projects", json={"name": "Documents", "path": str(folder)})
    assert attached.status_code == 200, attached.text
    project = attached.json()["project"]
    workspace, error = resolve_room_workspace(
        drive_root=data, system_repo_dir=system_repo, project_id=project["id"],
    )
    assert error == ""
    admitted = client.post("/api/tasks", json={
        "description": "Revise draft.txt in the selected folder.",
        "project_id": project["id"], "workspace_root": workspace,
    })
    assert admitted.status_code == 200, admitted.text
    assert len(captured) == 1
    task = captured[0]
    assert task["workspace_root"] == str(folder.resolve())
    assert task["workspace_mode"] == "external"
    result = load_task_result(data, admitted.json()["task_id"])
    assert result["workspace_root"] == str(folder.resolve())
    assert result["status"] == "scheduled"
    assert not (folder / ".git").exists()
    assert (folder / "draft.txt").read_text(encoding="utf-8") == "Owner's draft\n"
