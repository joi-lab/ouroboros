import json
from types import SimpleNamespace

from ouroboros.tools.recent_tasks import _handle_recent_tasks
from tests._store_shared import seed_result, unreadable_result


def _ctx(tmp_path):
    return SimpleNamespace(drive_root=tmp_path)


def _write_task(root, name, *, result="done", ts="2026-01-01T00:00:00Z", **extra):
    data = {
        "task_id": name,
        "ts": ts,
        "updated_at": ts,
        "status": "completed",
        "description": f"task {name}",
        "cost_usd": 1.25,
        "total_rounds": 3,
        "result": result,
        **extra,
    }
    seed_result(root, name, data)


def test_recent_tasks_empty_drive(tmp_path):
    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))
    assert data["running"] == []
    assert data["tasks"] == []
    assert data["unreadable_tasks"] == []
    assert data["total"] == 0
    assert data["remaining"] == 0
    assert data["next"] is None


def test_recent_tasks_uses_canonical_budget_root_from_fork(tmp_path):
    canonical = tmp_path / "canonical"
    child = tmp_path / "child"
    child.mkdir()
    _write_task(canonical, "owner-task", result="canonical-result")
    ctx = SimpleNamespace(
        drive_root=child,
        budget_drive_root=str(canonical),
        task_metadata={},
    )

    data = json.loads(_handle_recent_tasks(ctx, include_results=True))

    assert data["tasks"][0]["task_id"] == "owner-task"
    assert data["tasks"][0]["result"] == "canonical-result"


def test_recent_tasks_returns_preview_by_default(tmp_path):
    _write_task(tmp_path, "abc123", result="hello world")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))

    assert data["tasks"][0]["task_id"] == "abc123"
    assert data["tasks"][0]["result_preview"] == "hello world"
    assert "result" not in data["tasks"][0]
    assert "trace_summary" not in data["tasks"][0]


def test_recent_tasks_include_results_and_traces(tmp_path):
    _write_task(tmp_path, "abc123", result="full result", trace_summary="trace")

    data = json.loads(_handle_recent_tasks(
        _ctx(tmp_path),
        include_results=True,
        include_traces=True,
    ))

    assert data["tasks"][0]["result"] == "full result"
    assert data["tasks"][0]["trace_summary"] == "trace"


def test_recent_tasks_sorted_by_latest_update(tmp_path):
    _write_task(tmp_path, "older", updated_at="2026-01-01T00:00:00Z")
    _write_task(tmp_path, "newer", updated_at="2026-01-02T00:00:00Z")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))

    assert [row["task_id"] for row in data["tasks"]] == ["newer", "older"]


def test_recent_tasks_limit_is_clamped(tmp_path):
    for idx in range(25):
        _write_task(tmp_path, f"task{idx}", updated_at=f"2026-01-01T00:00:{idx:02d}Z")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), limit=99))

    assert len(data["tasks"]) == 20


def test_recent_tasks_reports_unreadable_row(tmp_path, monkeypatch):
    _write_task(tmp_path, "good")
    _write_task(tmp_path, "bad")
    unreadable_result(monkeypatch, "bad", root=tmp_path)

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path), limit=10))

    assert [row["task_id"] for row in data["tasks"]] == ["good"]
    assert len(data["unreadable_tasks"]) == 1
    assert data["unreadable_tasks"][0]["task_id"] == "bad"
    assert "StoreError" in data["unreadable_tasks"][0]["error"]


def test_recent_tasks_reports_running_queue(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "queue_snapshot.json").write_text(json.dumps({
        "ts": "2026-01-01T00:00:00Z",
        "running": [{"id": "run1", "text": "keep going"}],
    }), encoding="utf-8")

    data = json.loads(_handle_recent_tasks(_ctx(tmp_path)))

    assert data["running"] == [{
        "task_id": "run1",
        "status": "running",
        "description": "keep going",
        "ts": "2026-01-01T00:00:00Z",
    }]
