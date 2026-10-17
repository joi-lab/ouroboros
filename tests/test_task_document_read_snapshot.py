"""Implicit task reads decode a coherent raw snapshot after releasing SQLite/name locks."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import threading

import pytest

from ouroboros import store, task_store
from ouroboros.task_results import load_task_result
from tests.test_store import document, publish
from tests._store_shared import unreadable_result


def version(number):
    return document("task", status="running", nested={"version": number, "items": [1, 2, 3]},
                    launch_handoffs={"op": {"version": number, "state": "claimed"}}, model_waits={})


@pytest.fixture(params=[store.TIER_ENFORCED, store.TIER_NAME])
def root(tmp_path, request):
    publish(tmp_path, version(1), tier=request.param)
    return tmp_path


def test_implicit_document_preserves_projection_shape_and_fresh_values(root):
    with store.open_store(root, write=False) as txn:
        expected = txn.load("task")
        assert task_store.read_document(root, "missing", txn=txn) is None
    signature = task_store.signature(root, "task")
    actual = task_store.read_document(root, "task")
    assert actual == expected and list(actual) == list(expected)
    assert actual["model_waits"] == {} and "retired_model_consumers" not in actual
    assert task_store.read_document(root, "missing") is None
    actual["nested"]["items"].append(99)
    actual["launch_handoffs"]["op"]["version"] = 99
    assert task_store.read_document(root, "task") == expected
    assert task_store.signature(root, "task") == signature


@pytest.mark.parametrize("decode", ["body", "handoff"])
def test_writer_commits_while_decode_waits_and_reader_keeps_one_snapshot(root, monkeypatch, decode):
    with store.open_store(root, write=False) as txn:
        expected = txn.load("task")
        target = txn.body_text("task") if decode == "body" else txn.handoff_records(["task"])[0]["facts"]
    entered, release = threading.Event(), threading.Event()
    reader_id = []
    original = json.loads

    def paused(value, *args, **kwargs):
        if reader_id and threading.get_ident() == reader_id[0] and value == target:
            entered.set()
            assert release.wait(15), "test must release its decoder"
        return original(value, *args, **kwargs)

    def read():
        reader_id.append(threading.get_ident())
        return task_store.read_document(root, "task")

    def write():
        with store.open_store(root, write=True) as txn:
            txn.store_task(version(2))
        return True

    monkeypatch.setattr(json, "loads", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read)
        try:
            assert entered.wait(5), "the targeted JSON decoder was not reached"
            writer = pool.submit(write)
            assert writer.result(timeout=5), "writer COMMIT waited for Python JSON decode"
            assert not release.is_set(), "the writer completed before decoding was released"
        finally:
            release.set()
        assert reader.result(timeout=5) == expected
    current = task_store.read_document(root, "task")
    assert current["nested"]["version"] == current["launch_handoffs"]["op"]["version"] == 2


def test_caller_owned_write_transaction_keeps_its_uncommitted_view_and_rollback(root, monkeypatch):
    class RollbackFixture(Exception):
        pass

    def unexpected_open(*args, **kwargs):
        pytest.fail("provided transaction was replaced by an independent read")

    with pytest.raises(RollbackFixture):
        with store.open_store(root, write=True) as txn:
            txn.store_task(version(2))
            with monkeypatch.context() as supplied:
                supplied.setattr(task_store, "open_store", unexpected_open)
                read = task_store.read_document(root, "task", txn=txn)
                assert read == txn.load("task")
                assert read["nested"]["version"] == read["launch_handoffs"]["op"]["version"] == 2
                assert txn.conn.in_transaction
            raise RollbackFixture()
    assert task_store.read_document(root, "task") == version(1)


def test_legacy_nonfinite_body_keeps_python_json_semantics(root):
    legacy = {**version(1), "legacy": {"nan": float("nan"), "positive": float("inf"), "negative": float("-inf")}}
    raw = json.dumps(legacy, ensure_ascii=False, allow_nan=True)
    with store.open_store(root, write=True) as txn:
        txn.conn.execute("UPDATE task_bodies SET body=? WHERE task_id=?", (raw, "task"))
    value = load_task_result(root, "task", strict=True)
    assert math.isnan(value["legacy"]["nan"])
    assert value["legacy"]["positive"] == math.inf and value["legacy"]["negative"] == -math.inf
    assert value["launch_handoffs"] == version(1)["launch_handoffs"]


@pytest.mark.parametrize("bad", ["body", "handoff", "both"])
def test_malformed_sources_stay_errors_body_first_and_release_the_store(root, bad):
    body, facts = "{bad-body", "[bad-handoff"
    with store.open_store(root, write=True) as txn:
        if bad in {"body", "both"}:
            txn.conn.execute("UPDATE task_bodies SET body=? WHERE task_id=?", (body, "task"))
        if bad in {"handoff", "both"}:
            txn.conn.execute("UPDATE task_handoffs SET facts=? WHERE task_id=? AND op_id=?", (facts, "task", "op"))
    with pytest.raises(json.JSONDecodeError) as caught:
        load_task_result(root, "task", strict=True)
    assert caught.value.doc == (facts if bad == "handoff" else body)
    # Failed decoding neither quarantines the raw evidence nor keeps a store hold.
    with store.open_store(root, write=True) as txn:
        txn.store_task(document("other"))
        if bad in {"body", "both"}:
            assert txn.body_text("task") == body
        if bad in {"handoff", "both"}:
            assert txn.handoff_records(["task"])[0]["facts"] == facts
    assert task_store.read_document(root, "other")["task_id"] == "other"


@pytest.mark.parametrize("field, value", [("task_id", "foreign"), ("status", "")])
def test_strict_result_admission_still_checks_decoded_identity(root, field, value):
    raw = json.dumps({**version(1), field: value})
    with store.open_store(root, write=True) as txn:
        txn.conn.execute("UPDATE task_bodies SET body=? WHERE task_id=?", (raw, "task"))
    with pytest.raises(ValueError, match="unreadable or invalid"):
        load_task_result(root, "task", strict=True)


def test_existing_unreadable_fixture_covers_implicit_and_owned_reads(root, monkeypatch):
    switch = unreadable_result(monkeypatch, "task", root=root)
    with pytest.raises(store.StoreError, match="simulated unreadable row"):
        task_store.read_document(root, "task")
    with pytest.raises(store.StoreError, match="simulated unreadable row"):
        with store.open_store(root, write=False) as txn:
            task_store.read_document(root, "task", txn=txn)
    switch["on"] = False
    assert task_store.read_document(root, "task") == version(1)
