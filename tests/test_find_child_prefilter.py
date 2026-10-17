"""find_child_tasks filters raw rows BEFORE paying for the projection.

The materializing effective projection is not a read: it copies files,
rewrites artifact manifests and re-hashes every artifact. Running it over
EVERY stored task result to then filter by lineage made a read-shaped API
mutate unrelated trees' filesystems (and one finalization ran it 4-5x).
The lineage fields the filter needs are already on the raw disk row, so
non-matching rows must never reach the projection; the two classes that
cannot be judged raw (retry pointers, queue-live lineage-less rows) keep
their paths.
"""

import json
import pathlib

import ouroboros.task_status as task_status
from ouroboros.task_status import find_child_tasks, wait_for_effective_tasks


def _write_result(drive_root: pathlib.Path, row: dict) -> None:
    """The row as stored (stamped by the seed): a read never imports, so the fixture seeds the store."""
    from tests._store_shared import seed_result

    seed_result(drive_root, row["task_id"], row)


def test_non_matching_rows_never_reach_the_projection(tmp_path, monkeypatch):
    _write_result(tmp_path, {
        "task_id": "t-child", "delegation_role": "subagent",
        "parent_task_id": "t-parent", "root_task_id": "t-parent",
        "status": "completed", "ts": "2026-08-30T00:00:01+00:00",
    })
    _write_result(tmp_path, {
        "task_id": "t-unrelated", "delegation_role": "",
        "parent_task_id": "", "root_task_id": "",
        "status": "completed", "ts": "2026-08-30T00:00:02+00:00",
    })
    _write_result(tmp_path, {
        "task_id": "t-other-tree", "delegation_role": "subagent",
        "parent_task_id": "t-elsewhere", "root_task_id": "t-elsewhere",
        "status": "completed", "ts": "2026-08-30T00:00:03+00:00",
    })
    projected = []
    real = task_status.effective_task_result

    def _spy(drive_root, item, **kwargs):
        projected.append(str(item.get("task_id") or ""))
        return real(drive_root, item, **kwargs)

    monkeypatch.setattr(task_status, "effective_task_result", _spy)
    rows = find_child_tasks(tmp_path, parent_task_id="t-parent", scope="direct")
    assert [r["task_id"] for r in rows] == ["t-child"]
    assert projected == ["t-child"], (
        "non-matching raw rows must be filtered before the materializing "
        f"projection, got projections for: {projected}"
    )


def test_retry_pointer_rows_are_admitted_past_the_prefilter(tmp_path):
    """A raw row whose lineage does not match may still project a RETRY row
    whose lineage does — the retry chain is followed inside the projection,
    so rows carrying a retry pointer must reach it."""
    _write_result(tmp_path, {
        "task_id": "t-old", "delegation_role": "subagent",
        "parent_task_id": "t-nobody", "root_task_id": "t-nobody",
        "superseded_by": "t-new",
        "status": "failed", "ts": "2026-08-30T00:00:01+00:00",
    })
    _write_result(tmp_path, {
        "task_id": "t-new", "delegation_role": "subagent",
        "parent_task_id": "t-parent", "root_task_id": "t-parent",
        "status": "completed", "ts": "2026-08-30T00:00:02+00:00",
    })
    rows = find_child_tasks(tmp_path, parent_task_id="t-parent", scope="direct")
    ids = {r["task_id"] for r in rows}
    assert "t-new" in ids


def test_queue_live_lineage_less_row_arrives_via_overlay(tmp_path):
    """A running child whose DISK row has no lineage yet is re-discovered by
    the queue-snapshot overlay (the raw prefilter may skip the disk row)."""
    _write_result(tmp_path, {
        "task_id": "t-live", "delegation_role": "",
        "parent_task_id": "", "root_task_id": "",
        "status": "running", "ts": "2026-08-30T00:00:01+00:00",
        "cost_usd": 1.75, "result": "partial work so far",
        "artifacts": [{"path": "a.txt"}],
    })
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "queue_snapshot.json").write_text(json.dumps({
        "pending": [],
        "running": [{
            "id": "t-live",
            "task": {
                "id": "t-live", "delegation_role": "subagent",
                "parent_task_id": "t-parent", "root_task_id": "t-parent",
            },
        }],
    }))
    rows = find_child_tasks(tmp_path, parent_task_id="t-parent", scope="direct")
    by_id = {r["task_id"]: r for r in rows}
    assert "t-live" in by_id
    # The overlay must carry the DISK row's content, not a thin queue stub —
    # a stub-only overlay zeroes the cost rollup and loses the partial result.
    live = by_id["t-live"]
    assert live["cost_usd"] == 1.75
    assert live["result"] == "partial work so far"
    assert live["artifacts"] == [{"path": "a.txt"}]
    assert live["status"] == "running"


def test_wait_polls_without_materializing_and_finishes_with_one_full_read(tmp_path, monkeypatch):
    _write_result(tmp_path, {
        "task_id": "t-done", "status": "completed",
        "ts": "2026-08-30T00:00:01+00:00",
    })
    calls = []
    real = task_status.load_effective_task_result

    def _spy(drive_root, tid, materialize_artifacts=True):
        calls.append(bool(materialize_artifacts))
        return real(drive_root, tid, materialize_artifacts=materialize_artifacts)

    monkeypatch.setattr(task_status, "load_effective_task_result", _spy)
    out = wait_for_effective_tasks(tmp_path, ["t-done"], timeout_sec=1)
    assert out["all_terminal"] is True
    # Every in-loop poll is a projection-only read; exactly the final read
    # (after the loop, on every exit path) materializes.
    assert calls, "no reads recorded"
    assert calls[-1] is True
    assert all(flag is False for flag in calls[:-1])


def test_child_lookup_reads_only_selected_bodies_and_sees_a_changed_lineage(tmp_path, monkeypatch):
    """The lineage WHERE selects by typed columns: 240 foreign bodies are never decoded, a row
    whose lineage changes joins the next lookup, and a deleted row leaves it."""
    from ouroboros import task_store
    from ouroboros.task_results import write_task_result
    from ouroboros.task_store import delete_document

    for number in range(240):
        _write_result(tmp_path, {"task_id": f"foreign-{number}", "status": "completed",
            "delegation_role": "subagent", "parent_task_id": "other", "root_task_id": "other",
            "result": "unrelated full answer " * 1000})
    _write_result(tmp_path, {"task_id": "child", "status": "completed", "delegation_role": "subagent",
                            "parent_task_id": "owner", "root_task_id": "owner", "result": "selected answer"})
    decoded = []
    real = task_store._decode

    def counted(*fetched):
        rows = real(*fetched)
        decoded.extend(str(row.get("task_id")) for row in rows)
        return rows

    monkeypatch.setattr(task_store, "_decode", counted)
    rows = find_child_tasks(tmp_path, parent_task_id="owner", scope="direct")
    assert [row["task_id"] for row in rows] == ["child"] and rows[0]["result"] == "selected answer"
    assert decoded == ["child"]
    decoded.clear()
    write_task_result(tmp_path, "foreign-0", "completed", delegation_role="subagent",
                      parent_task_id="owner", root_task_id="owner")
    assert {row["task_id"] for row in find_child_tasks(tmp_path, parent_task_id="owner", scope="direct")} == {"child", "foreign-0"}
    assert set(decoded) == {"child", "foreign-0"}
    delete_document(tmp_path, "foreign-0")
    assert [row["task_id"] for row in find_child_tasks(tmp_path, parent_task_id="owner", scope="direct")] == ["child"]


def test_child_selection_preserves_whole_scan_quarantine_for_unrelated_bad_rows(tmp_path, monkeypatch):
    """The boot import is the door: unrelated inadmissible files are quarantined with one
    batched event, exactly as the complete reader did, before the lineage query answers."""
    from ouroboros import store_import

    directory = tmp_path / "task_results"
    directory.mkdir(parents=True)
    (directory / "old.json").write_text(json.dumps({"task_id": "old", "status": "completed"}), encoding="utf-8")
    (directory / "torn.json").write_text("{broken", encoding="utf-8")
    events = []
    monkeypatch.setattr(store_import, "emit_quarantine_event", lambda _root, rows: events.append(rows) if rows else None)
    store_import.ensure_current_store(tmp_path)
    _write_result(tmp_path, {"task_id": "child", "status": "completed", "delegation_role": "subagent", "parent_task_id": "owner"})
    assert [row["task_id"] for row in find_child_tasks(tmp_path, parent_task_id="owner", scope="direct")] == ["child"]
    assert len(events) == 1 and {row["task_id"] for row in events[0]} == {"old", "torn"}
    assert not (directory / "old.json").exists() and not (directory / "torn.json").exists()


def test_compact_child_selection_matches_complete_reader_with_retry_and_subtree(tmp_path, monkeypatch):
    from ouroboros.task_results import list_task_results

    records = [
        {"task_id": "direct", "parent_task_id": "owner", "root_task_id": "owner"},
        {"task_id": "deep", "parent_task_id": "direct", "root_task_id": "owner"},
        {"task_id": "foreign", "parent_task_id": "elsewhere", "root_task_id": "elsewhere"},
        {"task_id": "old", "parent_task_id": "elsewhere", "root_task_id": "elsewhere", "retry_task_id": "direct"},
    ]
    for row in records:
        _write_result(tmp_path, {"status": "completed", "delegation_role": "subagent", "result": row["task_id"], **row})
    for scope in ("direct", "subtree"):
        expected = find_child_tasks(tmp_path, parent_task_id="owner", root_task_id="owner", scope=scope, materialize_artifacts=False)
        def complete_reader(root, parent, root_id, *, direct_only=False, queue_members=()):
            def may_match(item):
                if str(item.get("superseded_by") or item.get("retry_task_id") or "").strip():
                    return True
                if str(item.get("delegation_role") or "") != "subagent":
                    return False
                if parent and str(item.get("parent_task_id") or "") == parent:
                    return True
                return bool(not direct_only and root_id and str(item.get("root_task_id") or "") == root_id)
            return sorted((row for row in list_task_results(root) if may_match(row)), key=lambda row: row["task_id"])

        with monkeypatch.context() as complete:
            complete.setattr(task_status, "select_lineage", complete_reader)
            assert find_child_tasks(tmp_path, parent_task_id="owner", root_task_id="owner", scope=scope, materialize_artifacts=False) == expected
