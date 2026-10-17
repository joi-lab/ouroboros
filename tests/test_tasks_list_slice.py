"""GET /api/tasks slice-before-projection tests (v6.9x P2).

The unfiltered list path sorts raw result filenames by the creation-stable raw
``ts`` (process-wide memo), slices to ``limit`` BEFORE the effective/public
projection, then re-sorts the slice by effective ts. Status-filtered requests
keep the full projection path (child-drive promotion pin, test_headless_cli).
LIST rows are a compact projection: the five bulk evidence fields are omitted;
DETAIL keeps them. ``limit`` defaults to 50 and caps at 500 (unchanged);
``limit=0`` returns all rows. ``queue_only=1`` skips the task-results scan.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ouroboros.gateway.tasks import api_task_get, api_tasks_list
from ouroboros.store_import import ensure_current_store
from ouroboros.task_results import write_task_result
from ouroboros.utils import utc_now_iso
from tests._shared import write_queue_document
from tests._store_shared import delete_result, seed_result


def _request(data, **params):
    return SimpleNamespace(
        query_params={key: str(value) for key, value in params.items()},
        path_params={},
        app=SimpleNamespace(state=SimpleNamespace(drive_root=data)),
    )


def _get_tasks(data, **params):
    response = asyncio.run(api_tasks_list(_request(data, **params)))
    return json.loads(response.body.decode("utf-8"))


def _write_raw(data, task_id, **fields):
    results = data / "task_results"
    results.mkdir(parents=True, exist_ok=True)
    seed_result(data, task_id, {"_schema_version": 1, "task_id": task_id, **fields})


def _seed_queue_snapshot(data):
    # A FRESH liveness stamp: an empty snapshot only proves a running row is orphaned
    # while it is still current — an undated or out-of-date snapshot fails open
    # toward liveness (GR7-1a), and the orphan projection would never run.
    (data / "state").mkdir(parents=True, exist_ok=True)
    write_queue_document(data / "state" / "queue_snapshot.json", {"ts": utc_now_iso(), "pending": [], "running": []})


def test_default_limit_50_cap_500_and_limit_0_returns_all(tmp_path):
    data = tmp_path / "data"
    for i in range(510):
        _write_raw(
            data, f"t{i:03d}", status="completed", result="done",
            ts=f"2026-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z",
        )

    default_payload = _get_tasks(data)
    assert len(default_payload["tasks"]) == 50  # default unchanged
    assert "queue" in default_payload
    ids = [row["task_id"] for row in default_payload["tasks"]]
    assert ids[0] == "t509" and ids[-1] == "t460"  # newest raw ts first

    assert len(_get_tasks(data, limit=9999)["tasks"]) == 500  # cap unchanged
    assert len(_get_tasks(data, limit=0)["tasks"]) == 510  # limit=0 = ALL


def test_slice_by_raw_ts_then_resort_by_effective_ts(tmp_path):
    data = tmp_path / "data"
    child = tmp_path / "child"
    _write_raw(data, "plain", status="completed", result="done", ts="2026-01-01T03:00:00Z")
    write_task_result(
        data, "promoted", "scheduled", child_drive_root=str(child),
        result="queued", ts="2026-01-01T02:00:00Z",
    )
    write_task_result(child, "promoted", "completed", result="child done", ts="2026-01-01T04:00:00Z")

    both = _get_tasks(data, limit=2)["tasks"]
    # Membership decided on RAW ts; ORDER decided on EFFECTIVE ts (the child
    # merge replaced promoted's ts with 04:00, so it displays first).
    assert [row["task_id"] for row in both] == ["promoted", "plain"]
    assert both[0]["status"] == "completed" and both[0]["result"] == "child done"

    # Disclosed residual: raw top-1 is "plain" (03:00 > 02:00) even though
    # promoted's EFFECTIVE ts (04:00) is newer — the slice is a raw-ts slice.
    top_one = _get_tasks(data, limit=1)["tasks"]
    assert [row["task_id"] for row in top_one] == ["plain"]


def test_rows_without_raw_ts_sort_last_with_filename_tiebreak(tmp_path):
    data = tmp_path / "data"
    _write_raw(data, "with-ts", status="completed", result="ok", ts="2026-01-01T01:00:00Z")
    _write_raw(data, "nots-a", status="completed", result="ok")
    _write_raw(data, "nots-b", status="completed", result="ok")

    assert [row["task_id"] for row in _get_tasks(data, limit=1)["tasks"]] == ["with-ts"]
    all_rows = [row["task_id"] for row in _get_tasks(data, limit=0)["tasks"]]
    # Missing ts = minus infinity (after every timestamped row); deterministic
    # filename tie-break among the ts-less rows.
    assert all_rows == ["with-ts", "nots-b", "nots-a"]


def test_inadmissible_files_are_quarantined_at_the_import_door_before_the_first_list(tmp_path):
    """ABI-2 in the store era: the door is the import, not the list scan. Files whose bytes
    fail to parse, unstamped pre-7.0 history and a future stamp move byte-unchanged under
    ``task_results/quarantine/`` on the root's first touch, counted in ONE batched durable
    event (6.3=B); the list then shows only admitted rows, and a fresh write reclaims a
    quarantined name with its real ts."""
    data = tmp_path / "data"
    results = data / "task_results"
    results.mkdir(parents=True)
    (results / "good.json").write_text(json.dumps({"_schema_version": 1, "task_id": "good", "status": "completed",
                                                   "result": "ok", "ts": "2026-01-01T01:00:00Z"}), encoding="utf-8")
    (results / "torn.json").write_text("{torn", encoding="utf-8")
    (results / "old.json").write_text(json.dumps({"task_id": "old", "status": "completed"}), encoding="utf-8")
    (results / "future.json").write_text(json.dumps({"_schema_version": 99, "task_id": "future",
                                                     "status": "completed"}), encoding="utf-8")

    ensure_current_store(data)  # the boot door's real import; a list never imports
    assert [row["task_id"] for row in _get_tasks(data)["tasks"]] == ["good"]
    assert sorted(p.name for p in (results / "quarantine").glob("*.json")) == ["future.json", "old.json", "torn.json"]
    events = [
        json.loads(line)
        for line in (data / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    quarantine_events = [e for e in events if e.get("type") == "task_results_quarantined"]
    assert len(quarantine_events) == 1, "one batched event for the whole import"
    assert quarantine_events[0]["count"] == 3
    assert quarantine_events[0]["reasons"] == {"malformed": 1, "unstamped_pre_7_0": 1, "future_schema": 1}

    _write_raw(data, "torn", status="completed", result="ok", ts="2026-01-01T02:00:00Z")
    assert [row["task_id"] for row in _get_tasks(data)["tasks"]] == ["torn", "good"]


def test_deleted_result_file_drops_out_of_list_and_memo(tmp_path):
    data = tmp_path / "data"
    _write_raw(data, "keep", status="completed", result="ok", ts="2026-01-01T01:00:00Z")
    _write_raw(data, "gone", status="completed", result="ok", ts="2026-01-01T02:00:00Z")

    assert [row["task_id"] for row in _get_tasks(data)["tasks"]] == ["gone", "keep"]

    delete_result(data, "gone")
    assert [row["task_id"] for row in _get_tasks(data)["tasks"]] == ["keep"]


def test_queue_only_returns_queue_without_scanning_task_results(tmp_path, monkeypatch):
    from ouroboros import queue_observation

    data = tmp_path / "data"
    _write_raw(data, "t1", status="completed", result="ok", ts="2026-01-01T01:00:00Z")
    (data / "state").mkdir(parents=True, exist_ok=True)
    (data / "state" / "queue_snapshot.json").write_text(
        json.dumps({"pending": [{"id": "queued-task"}], "running": []}), encoding="utf-8"
    )

    reads = []
    real = queue_observation.read_json_dict
    monkeypatch.setattr(
        queue_observation, "read_json_dict", lambda path: reads.append(str(path)) or real(path)
    )

    payload = _get_tasks(data, queue_only=1)

    assert payload["tasks"] == []
    assert payload["queue"]["pending"][0]["id"] == "queued-task"
    assert reads == [str(data / "state" / name) for name in ("queue_snapshot.json", "queue.alive")]  # no task-results scan


def test_list_rows_are_compact_while_detail_keeps_bulk_fields(tmp_path):
    data = tmp_path / "data"
    bulk = {
        "loop_outcome": {"execution_status": "ok"},
        "trace_refs": [{"kind": "events", "path": "x"}],
        "verification_ledger": {"claims": ["a"]},
        "review_evidence": {"raw": "y" * 64},
        "subagent_envelope": {"objective": "z"},
    }
    _write_raw(
        data, "fat", status="completed", result="done", ts="2026-01-01T01:00:00Z",
        cost_usd=0.5, **bulk,
    )

    row = _get_tasks(data)["tasks"][0]
    for field in bulk:
        assert field not in row  # compact LIST projection
    assert row["task_id"] == "fat"
    assert row["result"] == "done"  # pinned summary field
    assert row["status"] == "completed"
    assert row["ts"] == "2026-01-01T01:00:00Z"
    # ABI-3 (fix-round-2 conversion of this OLD-ABI clause): the stored
    # legacy spelling leaves the list row under the honest name only.
    assert row["accounted_upper_bound_usd"] == 0.5
    assert "cost_usd" not in row
    assert "outcome_axes" in row

    detail_request = SimpleNamespace(
        path_params={"task_id": "fat"},
        app=SimpleNamespace(state=SimpleNamespace(drive_root=data)),
    )
    detail = json.loads(asyncio.run(api_task_get(detail_request)).body.decode("utf-8"))
    for field in bulk:
        assert field in detail  # DETAIL is untouched


def test_status_filter_scans_full_store_not_the_raw_slice(tmp_path):
    data = tmp_path / "data"
    _seed_queue_snapshot(data)
    _write_raw(data, "old-done", status="completed", result="done", ts="2026-01-01T01:00:00Z")
    _write_raw(data, "new-a", status="scheduled", result="queued", ts="2026-01-01T02:00:00Z")
    _write_raw(data, "new-b", status="scheduled", result="queued", ts="2026-01-01T03:00:00Z")

    # A raw top-1 slice would only see new-b; the filtered path must still find
    # the oldest completed row (full-scan semantics preserved).
    rows = _get_tasks(data, status="completed", limit=1)["tasks"]
    assert [row["task_id"] for row in rows] == ["old-done"]


def test_events_tail_parsed_once_for_many_stale_running_rows(tmp_path, monkeypatch):
    import ouroboros.task_status as task_status_mod

    data = tmp_path / "data"
    _seed_queue_snapshot(data)
    (data / "logs").mkdir(parents=True, exist_ok=True)
    (data / "logs" / "events.jsonl").write_text(
        json.dumps({"ts": "2026-06-01T00:00:00Z", "type": "worker_boot"}) + "\n",
        encoding="utf-8",
    )
    for i in range(3):
        write_task_result(data, f"orphan{i}", "running", result="working", ts=f"2026-01-01T00:00:0{i}Z")

    calls = []
    real = task_status_mod.iter_jsonl_objects

    def counting(path, **kwargs):
        calls.append(str(path))
        return real(path, **kwargs)

    monkeypatch.setattr(task_status_mod, "iter_jsonl_objects", counting)

    rows = _get_tasks(data)["tasks"]

    assert len(rows) == 3
    assert all(row["status"] == "failed" for row in rows)
    assert all(row["reason_code"] == "orphaned_running_after_worker_restart" for row in rows)
    events_reads = [c for c in calls if c.endswith("events.jsonl")]
    assert len(events_reads) == 1  # ONE shared tail parse for the whole request
