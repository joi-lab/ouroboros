"""Real-reader counts for filtered storage reads; no wall-clock gates."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ouroboros import swarm_rollup, task_status, task_store
from ouroboros.task_finalization import build_swarm_efficiency
from ouroboros.task_results import list_task_results, write_task_result
from ouroboros.task_status import find_child_tasks
from ouroboros.utils import append_jsonl

pytestmark = pytest.mark.scale


@pytest.mark.parametrize("count", [1, 64, 256])
def test_listing_decodes_only_the_rows_the_status_prefilter_selects(tmp_path, monkeypatch, count):
    for index in range(count):
        write_task_result(tmp_path, f"task-{index}", "completed")
    decode = Mock(wraps=task_store._decode)
    monkeypatch.setattr(task_store, "_decode", decode)
    assert list_task_results(tmp_path, statuses=["running"]) == []
    assert [len(call.args[0]) for call in decode.call_args_list] == [0]
    assert len(list_task_results(tmp_path)) == count
    assert [len(call.args[0]) for call in decode.call_args_list] == [0, count]


def test_child_lookup_selects_only_the_lineage_rows_on_every_call(tmp_path, monkeypatch):
    for index in range(32):
        write_task_result(tmp_path, f"task-{index}", "completed", delegation_role="subagent",
                          parent_task_id="parent" if index == 0 else "other", root_task_id="parent" if index == 0 else "other")
    decode = Mock(wraps=task_store._decode)
    monkeypatch.setattr(task_store, "_decode", decode)
    for _ in range(2):
        assert [row["task_id"] for row in find_child_tasks(tmp_path, parent_task_id="parent", materialize_artifacts=False)] == ["task-0"]
    assert [len(call.args[0]) for call in decode.call_args_list] == [1, 1]


def test_swarm_rollup_reads_one_event_and_resolves_depth_once(tmp_path, monkeypatch):
    append_jsonl(tmp_path / "logs/events.jsonl", {"type": "swarm_fanout", "parent_task_id": "parent", "task_ids": ["child"]})
    decode = Mock(wraps=json.loads)
    children = Mock(wraps=task_status.find_child_tasks)
    monkeypatch.setattr(swarm_rollup, "json", SimpleNamespace(loads=decode))
    monkeypatch.setattr(task_status, "find_child_tasks", children)
    rollup = build_swarm_efficiency(SimpleNamespace(drive_root=tmp_path), {"id": "parent"})
    assert rollup["subagent_count"] == 1
    assert decode.call_count == 1
    children.assert_called_once_with(tmp_path, parent_task_id="parent", root_task_id="parent",
                                     scope="subtree", materialize_artifacts=False)


def test_reader_failure_propagates_and_next_call_succeeds(tmp_path, monkeypatch):
    from ouroboros import task_results

    write_task_result(tmp_path, "task", "completed")
    with monkeypatch.context() as broken:
        def refuse(*args, **kwargs):
            raise OSError("fixture unreadable")
        broken.setattr(task_results, "list_documents", refuse)
        with pytest.raises(OSError, match="fixture unreadable"):
            list_task_results(tmp_path)
    assert len(list_task_results(tmp_path)) == 1


@pytest.mark.parametrize("existing_journal", [False, True])
@pytest.mark.parametrize("another_root", [False, True])
def test_listing_does_not_write_to_runtime_or_inspected_root(tmp_path, monkeypatch, existing_journal, another_root):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    inspected = tmp_path / "inspected" if another_root else tmp_path
    write_task_result(inspected, "task", "completed")
    if existing_journal:
        append_jsonl(inspected / "logs/events.jsonl", {"type": "original_evidence"})
    before = {path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in tmp_path.rglob("*") if path.is_file()}
    assert len(list_task_results(inspected)) == 1
    after = {path.relative_to(tmp_path): (path.read_bytes(), path.stat().st_mtime_ns)
             for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before
