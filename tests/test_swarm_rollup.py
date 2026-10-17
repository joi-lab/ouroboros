"""Persisted fan-out summaries retain legacy, rotated and interrupted writes."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ouroboros import swarm_rollup, task_results, task_status
from ouroboros.finalization_timing import finalization_child_scope
from ouroboros.task_finalization import build_swarm_efficiency
from ouroboros.task_status import find_child_tasks
from ouroboros.tools.control_scheduling import _emit_swarm_fanout
from ouroboros.utils import append_jsonl
from tests._store_shared import seed_result


@pytest.fixture
def decode_events(monkeypatch):
    decode = Mock(wraps=json.loads)
    monkeypatch.setattr(swarm_rollup, "json", SimpleNamespace(loads=decode))
    return decode


def emit(root, child="child"):
    _emit_swarm_fanout(SimpleNamespace(drive_logs=lambda: root / "logs"), parent_task_id="parent",
                      root_task_id="parent", depth=1, task_group_id="group", task_ids=[child],
                      role="researcher", requested_model_lane="auto", objective="fixture", emitted_live=True)


def build(root):
    return build_swarm_efficiency(SimpleNamespace(drive_root=root), {"id": "parent"})


@pytest.mark.scale
def test_new_parent_skips_history_and_recovers_failed_producer_update(tmp_path, monkeypatch, decode_events):
    for index in range(256):
        append_jsonl(tmp_path / "logs/events.jsonl", {"type": "unrelated", "index": index})
    task_results.write_task_result(tmp_path, "parent", "running", future_field={"kept": True})
    emit(tmp_path)
    assert task_results.load_task_result(tmp_path, "parent")["swarm_fanout_rollup"]["fanout_count"] == 1
    with monkeypatch.context() as broken:
        broken.setattr(task_results, "write_task_result", lambda *a, **kw: (_ for _ in ()).throw(OSError("fixture full")))
        emit(tmp_path, "second")
    decode_events.reset_mock()
    assert build(tmp_path)["subagent_count"] == 2
    assert decode_events.call_count == 1
    assert task_results.load_task_result(tmp_path, "parent")["future_field"] == {"kept": True}
    decode_events.reset_mock()
    assert build(tmp_path)["fanout_count"] == 2
    assert decode_events.call_count == 0


@pytest.mark.scale
def test_legacy_replays_once_and_rotation_keeps_cursor(tmp_path, decode_events):
    seed_result(tmp_path, "parent", {"_schema_version": 1, "task_id": "parent", "status": "running"})
    for index in range(64):
        append_jsonl(tmp_path / "logs/events.jsonl", {"type": "unrelated", "index": index})
    append_jsonl(tmp_path / "logs/events.jsonl", {"type": "swarm_fanout", "parent_task_id": "parent",
                                                "task_ids": ["first"], "inter_wave_latency_sec": 2.5})
    assert build(tmp_path)["fanout_interval_sec_total"] == 2.5
    assert decode_events.call_count == 65
    archive = tmp_path / "archive/events_20261010T000000.jsonl"
    archive.parent.mkdir()
    (tmp_path / "logs/events.jsonl").rename(archive)
    emit(tmp_path, "second")
    decode_events.reset_mock()
    assert build(tmp_path)["subagent_count"] == 2
    assert decode_events.call_count == 0


def test_incomplete_or_shortened_history_never_publishes_partial_counts(tmp_path):
    task_results.write_task_result(tmp_path, "parent", "running")
    emit(tmp_path)
    path = tmp_path / "logs/events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"type":"swarm_fanout","parent_task_id":"parent","task_ids":["next"]}')
    before = task_results.load_task_result(tmp_path, "parent")["swarm_fanout_rollup"]
    assert swarm_rollup.load_swarm_rollup(tmp_path, "parent")["fanout_count"] == 1
    assert task_results.load_task_result(tmp_path, "parent")["swarm_fanout_rollup"] == before
    with path.open("ab") as stream:
        stream.write(b"\n")
    assert swarm_rollup.load_swarm_rollup(tmp_path, "parent")["fanout_count"] == 2
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="shortened"):
        swarm_rollup.load_swarm_rollup(tmp_path, "parent")
    assert task_results.load_task_result(tmp_path, "parent")["swarm_fanout_rollup"]["fanout_count"] == 2


def test_malformed_history_does_not_become_zero_swarm(tmp_path):
    task_results.write_task_result(tmp_path, "parent", "running")
    path = tmp_path / "logs/events.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text("broken\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        swarm_rollup.load_swarm_rollup(tmp_path, "parent")
    path.write_text("", encoding="utf-8")
    assert swarm_rollup.load_swarm_rollup(tmp_path, "parent")["child_ids"] == []


def test_child_set_is_shared_within_pass_and_refreshed_afterwards(tmp_path, monkeypatch):
    for task_id, parent in (("child", "parent"), ("grandchild", "child")):
        task_results.write_task_result(tmp_path, task_id, "completed", delegation_role="subagent",
                                       parent_task_id=parent, root_task_id="parent")
    select = Mock(wraps=task_status.select_lineage)
    monkeypatch.setattr(task_status, "select_lineage", select)
    query = {"parent_task_id": "parent", "root_task_id": "parent", "materialize_artifacts": False}
    with finalization_child_scope():
        direct = find_child_tasks(tmp_path, **query, scope="direct")
        assert [row["task_id"] for row in direct] == ["child"]
        direct[0]["status"] = "mutated"
        with finalization_child_scope():
            subtree = find_child_tasks(tmp_path, **query, exclude_task_id="grandchild")
            assert len(subtree) == 1 and subtree[0]["status"] == "completed"
        assert select.call_count == 1
        task_results.write_task_result(tmp_path, "later", "completed", delegation_role="subagent",
                                       parent_task_id="parent", root_task_id="parent")
        assert len(find_child_tasks(tmp_path, **query)) == 2
    assert len(find_child_tasks(tmp_path, **query)) == 3
    assert select.call_count == 2


def test_child_cache_isolates_query_roots_modes_and_failed_passes(tmp_path, monkeypatch):
    from ouroboros import task_status

    calls = []
    def select(root, parent, root_id, *, direct_only, queue_members=()):
        calls.append(root)
        return []
    monkeypatch.setattr(task_status, "select_lineage", select)
    with pytest.raises(RuntimeError), finalization_child_scope():
        find_child_tasks(tmp_path, parent_task_id="parent")
        raise RuntimeError("fixture ended")
    with finalization_child_scope():
        for kwargs in ({}, {"parent_task_id": "other"}, {"materialize_artifacts": False}):
            find_child_tasks(tmp_path, **kwargs)
    assert len(calls) == 4


def test_concurrent_projection_cannot_overwrite_a_newer_cursor(tmp_path, monkeypatch):
    task_results.write_task_result(tmp_path, "parent", "running")
    emit(tmp_path)
    append_jsonl(tmp_path / "logs/events.jsonl", {"type": "unrelated"})
    write = task_results.write_task_result
    newer = {**task_results.load_task_result(tmp_path, "parent")["swarm_fanout_rollup"],
             "through": (tmp_path / "logs/events.jsonl").stat().st_size, "concurrent_fact": "kept"}
    def race(*args, **kwargs):
        write(tmp_path, "parent", "completed", swarm_fanout_rollup=newer)
        return write(*args, **kwargs)
    monkeypatch.setattr(task_results, "write_task_result", race)
    swarm_rollup.load_swarm_rollup(tmp_path, "parent")
    row = task_results.load_task_result(tmp_path, "parent")
    assert row["status"] == "completed" and row["swarm_fanout_rollup"] == newer


def test_unreadable_journal_does_not_block_admission_and_foreign_cursor_replays(tmp_path, monkeypatch):
    with monkeypatch.context() as broken:
        broken.setattr(swarm_rollup, "JsonlChainSnapshot", lambda *a, **kw: (_ for _ in ()).throw(OSError("fixture denied")))
        row = task_results.write_task_result(tmp_path, "parent", "running")
    assert row["swarm_fanout_rollup"] == {}
    emit(tmp_path)
    row = task_results.load_task_result(tmp_path, "parent")
    row["swarm_fanout_rollup"].update(source_root="different-fixture-root", through=10_000_000)
    seed_result(tmp_path, "parent", row)
    assert swarm_rollup.load_swarm_rollup(tmp_path, "parent")["fanout_count"] == 1
