"""The handoff / model-wait projections live in ``task_handoffs`` rows (S3, R5: I4b).

A tool claim, a model wait and a consumer retirement each write ONE row of the
task's projection and never its body; ``load`` puts the rows back into the document
so every legacy reader and the exporter see the fields they always saw.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros import model_wait, store
from ouroboros.owner_pause import OwnerPauseRefused, tool_handoff
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.task_store import DELETE_ROW, patch_document, patch_handoff


def _body_and_seq(root, task_id):
    with store.open_store(root, write=False) as txn:
        return txn.body_text(task_id), txn.row(task_id, "write_seq")


def _source(root, task_id="root"):
    return SimpleNamespace(task_id=task_id, drive_root=str(root), task_metadata={"root_task_id": task_id})


def test_a_tool_claim_and_its_settlement_write_rows_not_the_body(tmp_path):
    write_task_result(tmp_path, "root", "running", root_task_id="root", payload="x" * 2000)
    body, seq = _body_and_seq(tmp_path, "root")
    with tool_handoff(_source(tmp_path), "write_file") as outcome:
        claims = load_task_result(tmp_path, "root")["launch_handoffs"]
        assert claims[outcome["operation_id"]]["state"] == "claimed" and claims[outcome["operation_id"]]["tool"] == "write_file"
        with store.open_store(tmp_path, write=False) as txn:
            row = txn.conn.execute("SELECT kind, state FROM task_handoffs WHERE task_id='root'").fetchone()
            assert tuple(row) == ("launch_handoffs", "claimed")
        assert _body_and_seq(tmp_path, "root")[0] == body  # the 2 KB body was never rewritten
        assert _body_and_seq(tmp_path, "root")[1] > seq  # but the signature moved: memos re-read
        outcome["settled"] = True
    assert load_task_result(tmp_path, "root").get("launch_handoffs", {}) == {}
    assert _body_and_seq(tmp_path, "root")[0] == body


def test_a_claim_needs_a_lifecycle_row(tmp_path):
    with pytest.raises(OwnerPauseRefused):
        with tool_handoff(_source(tmp_path, "ghost"), "write_file"):
            pass
    with pytest.raises(LookupError):
        patch_handoff(tmp_path, "ghost", "launch_handoffs", "op", lambda _row: {"state": "claimed"})


def test_model_waits_and_retired_consumers_are_rows_the_document_projects(tmp_path):
    write_task_result(tmp_path, "root", "running", root_task_id="root")
    body = _body_and_seq(tmp_path, "root")[0]
    assert model_wait.mutate_wait(tmp_path, "root", "w1", lambda _prev: {"state": "waiting", "revision": 1}) == {
        "state": "waiting", "revision": 1}
    assert model_wait.mutate_wait(tmp_path, "root", "w1", lambda prev: {**prev, "revision": 2})["revision"] == 2
    assert model_wait.mutate_wait(tmp_path, "root", "w1", lambda _prev: None)["revision"] == 2  # no write, the row read
    model_wait.retire_model_consumers(tmp_path, "root", {"c1": 1, "c2": 2})
    document = load_task_result(tmp_path, "root")
    assert document["model_waits"] == {"w1": {"state": "waiting", "revision": 2}}
    assert {cid: row["task_attempt"] for cid, row in document["retired_model_consumers"].items()} == {"c1": 1, "c2": 2}
    assert _body_and_seq(tmp_path, "root")[0] == body
    with pytest.raises(ValueError):
        model_wait.mutate_wait(tmp_path, "ghost", "w1", lambda _prev: {"state": "waiting"})
    with pytest.raises(ValueError):
        model_wait.retire_model_consumers(tmp_path, "ghost", {"c1": 1})


def test_a_document_mutator_over_a_projected_field_syncs_the_rows(tmp_path):
    write_task_result(tmp_path, "root", "running", root_task_id="root")
    patch_handoff(tmp_path, "root", "launch_handoffs", "op1", lambda _row: {"tool": "pr_merge", "state": "claimed"})
    patch_handoff(tmp_path, "root", "launch_handoffs", "op2", lambda _row: {"tool": "write_file", "state": "claimed"})

    def pop_merge(current):  # the legacy shape: the whole field rewritten through the document
        claims = dict(current["launch_handoffs"])
        claims.pop("op1")
        claims["op2"] = {**claims["op2"], "state": "returned"}
        return {**current, "launch_handoffs": claims}

    patch_document(tmp_path, "root", pop_merge)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.handoffs("root")["launch_handoffs"] == {"op2": {"tool": "write_file", "state": "returned"}}
        assert json.loads(txn.body_text("root"))["launch_handoffs"] == {}  # the body keeps the key, emptied
    assert patch_handoff(tmp_path, "root", "launch_handoffs", "op2", lambda _row: DELETE_ROW) is None
    assert load_task_result(tmp_path, "root")["launch_handoffs"] == {}  # the key the body carries stays


def test_the_rows_of_a_deleted_task_go_with_it_and_a_malformed_projection_imports_empty(tmp_path):
    write_task_result(tmp_path, "root", "running", root_task_id="root")
    patch_handoff(tmp_path, "root", "model_waits", "w1", lambda _row: {"state": "waiting"})
    with store.open_store(tmp_path, write=True) as txn:
        txn.delete_task("root")
        assert txn.handoffs("root") == {kind: {} for kind in store.ROW_BACKED_FIELDS}
        txn.store_task({"task_id": "odd", "status": "running", "model_waits": "garbage"})
        assert txn.load("odd")["model_waits"] == {}
