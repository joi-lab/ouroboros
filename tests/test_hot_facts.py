"""Current controls are atomic, addressed and independent of settled history."""
from __future__ import annotations

import json

import pytest

from ouroboros import budget_pause, maintenance_jobs, owner_pause, store, store_hot_facts as hot
from ouroboros.task_results import write_task_result
from ouroboros.task_store import patch_document


def _task(txn, tid, **fields):
    txn.store_task({"task_id": tid, "status": "running", "_schema_version": 1, **fields})


def _old_task(txn, tid, **fields):
    document = {"task_id": tid, "status": "running", "_schema_version": 1, **fields}
    columns = {key: value for key, value in store.typed_columns(document).items() if key not in hot.TASK_COLUMNS}
    columns["root_task_id"] = document.get("root_task_id")
    columns["write_seq"] = (txn.row(tid, "write_seq") or 0) + 1
    txn.conn.execute("INSERT OR REPLACE INTO task_bodies VALUES (?,?)", (tid, store.encode_document(document)))
    names = ("task_id", *columns)
    txn.conn.execute(f"INSERT OR REPLACE INTO tasks ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                     (tid, *columns.values()))


def test_control_writers_publish_both_body_and_columns_or_neither(tmp_path, monkeypatch):
    write_task_result(tmp_path, "root", "running")
    fence, _ = owner_pause.install_fence(tmp_path, "root", request_id="press")
    pause = {"state": "pausing", "pause_id": "pause", "task_attempt": 3, "reason": "owner"}
    budget_pause.set_budget_pause(tmp_path, "root", pause)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.row("root", "owner_pause_state", "owner_pause_fence_id", "budget_pause_state", "budget_pause_attempt") == (
            "requested", fence["fence_id"], "pausing", 3)
        before = txn.load("root")
    with store.open_store(tmp_path, write=True) as txn:
        txn.conn.execute("CREATE TEMP TRIGGER reject_hot BEFORE INSERT ON tasks "
                         "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END")
    with pytest.raises(store.StoreError, match="injected write failure"):
        budget_pause.set_budget_pause(tmp_path, "root", {**pause, "state": "paused"})
    with store.open_store(tmp_path, write=True) as txn:
        assert txn.load("root") == before
        assert txn.row("root", "budget_pause_state") == "pausing"
        txn.conn.execute("DROP TRIGGER reject_hot")
    budget_pause.set_budget_pause(tmp_path, "root", {**pause, "state": "paused"})
    owner_pause.set_fence_state(tmp_path, "root", fence_id=fence["fence_id"], state="paused", finishing_reviews=["review"])
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.load("root")["budget_pause"]["state"] == txn.row("root", "budget_pause_state") == "paused"
    monkeypatch.setattr(store.Txn, "body_text", lambda *_: pytest.fail("derived controls read a task body"))
    assert budget_pause.budget_pause_row(tmp_path, "root") == {**pause, "state": "paused"}
    assert owner_pause.read_fence(tmp_path, "root")["finishing_reviews"] == ["review"]
    owner_pause._CACHE.clear()  # a fresh process has the same body-free answer
    assert owner_pause.read_fence(tmp_path, "root")["state"] == "paused"


def test_null_controls_fall_back_only_for_the_addressed_row(tmp_path, monkeypatch):
    with store.open_store(tmp_path, write=True) as txn:
        _old_task(txn, "legacy", budget_pause={"state": "pausing", "task_attempt": 2},
                  owner_pause={"state": "requested", "fence_id": "f"})
        _task(txn, "modern", result="unrelated body" * 1000)
    seen, real = [], store.Txn.body_text
    monkeypatch.setattr(store.Txn, "body_text", lambda self, tid: seen.append(tid) or real(self, tid))
    assert budget_pause.budget_pause_row(tmp_path, "legacy")["task_attempt"] == 2
    assert owner_pause.read_fence(tmp_path, "modern") == {}
    assert seen == ["legacy"]


def test_control_unknown_and_missing_authority_remain_distinct(tmp_path):
    assert owner_pause.read_fence(tmp_path, "absent") == {}
    with store.open_store(tmp_path, write=True) as txn:
        _task(txn, "bad", status=7, owner_pause={"state": "released"})
        _task(txn, "malformed", owner_pause=["closed"])
        _task(txn, "known")
    for tid in ("bad", "malformed"):
        with pytest.raises(ValueError):
            owner_pause.read_fence(tmp_path, tid)
    assert owner_pause.read_fence(tmp_path, "known") == {}
    with store.open_store(tmp_path, write=True) as txn:
        txn.delete_task("known")
    with pytest.raises(ValueError, match="authority_missing"):
        owner_pause.read_fence(tmp_path, "known")


def test_derivation_job_resumes_and_leaves_body_and_write_sequence_unchanged(tmp_path, monkeypatch):
    with store.open_store(tmp_path, write=True) as txn:
        for tid in ("a", "b", "c"):
            _old_task(txn, tid, metadata={"root_task_id": "tree"},
                      root_phase_checkpoint={"post_task_synthesis": "paused"})
        before = {tid: (txn.body_text(tid), txn.row(tid, "write_seq")) for tid in ("a", "b", "c")}
    progress = []
    monkeypatch.setattr(hot, "_DERIVE_BATCH", 1)  # one row per committed batch: a stop lands between rows
    first = hot.run_hot_facts(tmp_path, progress=progress.append,
                              should_stop=lambda: any(row.get("done") == 1 for row in progress))
    assert first["state"] == "pending" and first["cursor"] == {"table": "tasks", "after": "a"}
    store.close_connections(store.store_path(tmp_path))
    finished = hot.run_hot_facts(tmp_path)
    assert finished["state"] == "done" and finished["done"] == 3
    assert maintenance_jobs.job(tmp_path, hot.JOB) == finished
    with store.open_store(tmp_path, write=False) as txn:
        for tid in before:
            assert (txn.body_text(tid), txn.row(tid, "write_seq")) == before[tid]
            assert txn.row(tid, "root_task_id", "post_task_synthesis") == ("tree", "paused")
    monkeypatch.setattr(store.Txn, "body_text", lambda *_: pytest.fail("finished derivation read a body"))
    assert hot.run_hot_facts(tmp_path) == finished
    assert hot.read(tmp_path, "b")["post_task_synthesis"] == "paused"


def test_derivation_cas_cannot_overwrite_a_racing_writer(tmp_path, monkeypatch):
    with store.open_store(tmp_path, write=True) as txn:
        _old_task(txn, "task", budget_pause={"state": "paused", "task_attempt": 1})
    real, raced = hot.task_columns, []

    def derive(document):
        result = real(document)
        if not raced:
            raced.append(True)
            patch_document(tmp_path, "task", lambda row: {**row, "budget_pause": {"state": "resumed", "task_attempt": 2}})
        return result

    monkeypatch.setattr(hot, "task_columns", derive)
    assert hot.run_hot_facts(tmp_path)["state"] == "done"
    assert budget_pause.budget_pause_row(tmp_path, "task") == {"state": "resumed", "task_attempt": 2}
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.row("task", "budget_pause_state", "budget_pause_attempt") == ("resumed", 2)


def test_derivation_keeps_mismatched_task_identity_unreadable(tmp_path):
    with store.open_store(tmp_path, write=True) as txn:
        _old_task(txn, "bad")
        _old_task(txn, "good")
        document = txn.load("bad")
        document["task_id"] = "another"
        txn.conn.execute("UPDATE task_bodies SET body=? WHERE task_id='bad'", (store.encode_document(document),))
    for derive in (False, True):
        if derive:
            assert hot.run_hot_facts(tmp_path)["state"] == "done"
        with pytest.raises(ValueError, match="authority is unreadable"):
            hot.read(tmp_path, "bad")
        assert hot.read(tmp_path, "good")["control_valid"] == 1


def test_old_custody_upsert_invalidates_columns_in_its_own_transaction(tmp_path):
    with store.open_store(tmp_path, write=True) as txn:
        txn.put_custody("run", {"run_id": "run", "task_id": "task", "root_task_id": "old"}, state="held")
        seq = txn.conn.execute("SELECT write_seq FROM custody_runs").fetchone()[0]
    updated = {"run_id": "run", "task_id": "task", "root_task_id": "new"}
    with store.open_store(tmp_path, write=True) as txn:
        txn.conn.execute("INSERT INTO custody_runs(run_id,facts) VALUES (?,?) "
                         "ON CONFLICT(run_id) DO UPDATE SET facts=excluded.facts", ("run", json.dumps(updated)))
        assert tuple(txn.conn.execute("SELECT root_task_id,facts_derived,write_seq FROM custody_runs").fetchone()) == (
            None, None, seq + 1)
    assert hot.run_hot_facts(tmp_path)["state"] == "done"
    with store.open_store(tmp_path, write=False) as txn:
        assert tuple(txn.conn.execute("SELECT root_task_id,facts_derived,write_seq FROM custody_runs").fetchone()) == (
            "new", 1, seq + 1)


def _schema_five(root, monkeypatch):
    """A real pre-upgrade store, not a new schema with a falsified version stamp."""
    with monkeypatch.context() as patch:
        patch.setattr(store, "_SCHEMA", store._SCHEMA.removesuffix(hot.SCHEMA))
        patch.setattr(store, "SCHEMA_VERSION", 5)
        with store.open_store(root, write=True) as txn:
            _old_task(txn, "legacy", metadata={"root_task_id": "tree"},
                      owner_pause={"state": "paused", "fence_id": "f"})
            txn.conn.execute("INSERT INTO custody_runs(run_id,task_id,facts,state) VALUES (?,?,?,?)",
                             ("old-run", "legacy", json.dumps({"run_id": "old-run", "task_id": "legacy",
                                 "root_task_id": "tree", "output_complete": True}), "closed"))
    store.close_connections(store.store_path(root))


def test_schema_gate_is_atomic_and_never_decodes_old_bodies(tmp_path, monkeypatch):
    from ouroboros import delegate_custody_current as custody
    from ouroboros.store_import import upgrade_current_schema

    _schema_five(tmp_path, monkeypatch)
    assert owner_pause.read_fence(tmp_path, "legacy")["state"] == "paused"
    assert custody.tree_task_ids(tmp_path, "tree") == ["legacy"]
    assert list(custody.health_runs(tmp_path)) == ["old-run"]
    upgrade = hot.upgrade

    def fail_after_ddl(txn):
        upgrade(txn)
        raise RuntimeError("injected schema publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(hot, "upgrade", fail_after_ddl)
        with pytest.raises(RuntimeError, match="schema publication"):
            upgrade_current_schema(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("schema_version") == 5
        assert "owner_pause_state" not in {row["name"] for row in txn.conn.execute("PRAGMA table_info(tasks)")}
        before = txn.body_text("legacy")
    with monkeypatch.context() as patch:
        patch.setattr(store.Txn, "body_text", lambda *_: pytest.fail("schema upgrade decoded a task body"))
        upgrade_current_schema(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("schema_version") == store.SCHEMA_VERSION >= hot.VERSION
        assert txn.row("legacy", "owner_pause_state", "root_task_id") == (None, None)
        assert txn.body_text("legacy") == before
        assert txn.meta("job:" + hot.JOB)["state"] == "pending"
    assert hot.run_hot_facts(tmp_path)["state"] == "done"
    assert owner_pause.read_fence(tmp_path, "legacy")["state"] == "paused"
    assert custody.tree_task_ids(tmp_path, "tree") == ["legacy"]


def test_first_custody_publication_upgrades_an_old_child_store(tmp_path, monkeypatch):
    from ouroboros import delegate_custody as custody

    _schema_five(tmp_path, monkeypatch)
    assert custody.emit(tmp_path, custody.STARTED, {"run_id": "new-run", "task_id": "child", "root_task_id": "tree"})
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("schema_version") == store.SCHEMA_VERSION >= hot.VERSION
        assert tuple(txn.conn.execute("SELECT root_task_id,facts_derived FROM custody_runs WHERE run_id='new-run'").fetchone()) == ("tree", 1)
        assert txn.row("legacy", "owner_pause_state") is None


def test_request_backfill_and_old_writer_invalidation_are_source_cas(tmp_path, monkeypatch):
    for name in hot.REQUEST_SETS:
        with store.open_store(tmp_path, write=True) as txn:
            txn.conn.execute("INSERT INTO obligations(set_name,id,facts) VALUES (?,?,?)",
                             (name, "invocation:old", json.dumps({"request": {"task_id": "child", "root_task_id": "old"}})))
    real, raced = hot.request_columns, []

    def derive(facts):
        values = real(facts)
        if not raced:
            raced.append(True)
            with store.open_store(tmp_path, write=True) as txn:
                txn.conn.execute("UPDATE obligations SET facts=? WHERE set_name='custody_open'",
                                 (json.dumps({"request": {"task_id": "child", "root_task_id": "new"}}),))
        return values

    monkeypatch.setattr(hot, "request_columns", derive)
    assert hot.run_hot_facts(tmp_path)["state"] == "done"
    with store.open_store(tmp_path, write=False) as txn:
        assert [(name, key) for name, key, _facts in hot.tree_requests(txn, "new", refused=True)] == [
            ("custody_open", "invocation:old")]
        assert [(name, key) for name, key, _facts in hot.tree_requests(txn, "old", refused=True)] == [
            ("custody_refused", "invocation:old")]
    with store.open_store(tmp_path, write=True) as txn:
        txn.conn.execute("UPDATE obligations SET facts=? WHERE set_name='custody_open'",
                         (json.dumps({"request": {"task_id": "child", "root_task_id": "third"}}),))
        assert txn.conn.execute("SELECT request_root_task_id FROM obligations WHERE set_name='custody_open'").fetchone()[0] is None
    assert hot.run_hot_facts(tmp_path)["state"] == "done"
    with store.open_store(tmp_path, write=False) as txn:
        assert len(hot.tree_requests(txn, "third")) == 1


def test_damaged_job_input_waits_for_its_own_change(tmp_path, monkeypatch):
    with store.open_store(tmp_path, write=True) as txn:
        _old_task(txn, "bad")
        txn.conn.execute("UPDATE task_bodies SET body=? WHERE task_id='bad'", ("{",))
    with pytest.raises(ValueError):
        hot.run_hot_facts(tmp_path)
    failed = maintenance_jobs.job(tmp_path, hot.JOB)
    assert failed["state"] == "failed" and failed["waiting_on"]["id"] == "bad"
    with monkeypatch.context() as patch:
        patch.setattr(store.Txn, "body_text", lambda *_: pytest.fail("unchanged damaged input was retried"))
        assert hot.run_hot_facts(tmp_path) == failed
    with store.open_store(tmp_path, write=True) as txn:
        _task(txn, "bad")
    assert hot.run_hot_facts(tmp_path)["state"] == "done"


def test_derived_rows_read_no_body_and_underived_rows_still_do(tmp_path):
    """Proved by SQLite's authorizer, not by patching ``Txn.body_text``: the SQL itself joins no body."""
    import sqlite3

    with store.open_store(tmp_path, write=True) as txn:
        _task(txn, "modern", result="unrelated body" * 1000)
        _old_task(txn, "legacy", budget_pause={"state": "pausing", "task_attempt": 2})
        _old_task(txn, "torn")
        txn.conn.execute("UPDATE task_bodies SET body='{' WHERE task_id='torn'")
        txn.conn.execute("UPDATE task_bodies SET body='{' WHERE task_id='modern'")  # a corrupt body behind derived columns
    reads = []

    def authorizer(action, table, column, _db, _source):
        if action == sqlite3.SQLITE_READ and table == "task_bodies":
            reads.append(column)
        return sqlite3.SQLITE_OK

    with store.open_store(tmp_path, write=False) as txn:
        txn.conn.set_authorizer(authorizer)
        try:
            rows = hot._read_rows(txn, ["modern"])
            assert rows["modern"]["control_valid"] == 1 and rows["modern"]["budget_pause_state"] == ""
            assert reads == [], "a derived row's read touched task_bodies"
            rows = hot._read_rows(txn, ["legacy", "torn"])
        finally:  # ``set_authorizer(None)`` denies everything on Python 3.10: allow all instead (the fixture closes the handle)
            txn.conn.set_authorizer(lambda *_args: sqlite3.SQLITE_OK)
    assert "body" in reads, "an underived row must still pay its strict body read"
    assert (rows["legacy"]["control_valid"], rows["legacy"]["budget_pause_attempt"]) == (1, 2)
    assert rows["torn"]["control_valid"] == 0  # an unparsable legacy body stays unknown, for that row only
    assert hot.read(tmp_path, "modern")["control_valid"] == 1
    with pytest.raises(ValueError, match="unreadable"):
        hot.read(tmp_path, "torn")
