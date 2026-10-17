"""The real review collector selects current debt, including after an old writer returns."""
from __future__ import annotations

import copy
import json
import queue
import re

import pytest

from ouroboros import obligations, review_operation, store, store_import, task_store
from ouroboros.task_results import load_task_result, write_task_result
from tests.test_store import document, publish
from tests.test_store_cutover import _plan, captured_statements

_SCHEMA_SHAPES = {"v1": (), "owner-v2": ("has_owner_questions",),
                  "review-v2": ("review_collection_pending",)}


def _operation(state="dispatched", **fields):
    return {"surface": "task_acceptance", "state": state, "retry_key": "wave", **fields}


def _panel(**fields):
    return {"surface": "task_acceptance", "authority": "host_root", "panel_id": "panel",
            "actors": [], **fields}


def _late_document(*, receipt=None, **fields):
    panel = _panel(late_settlement={"reviewed_subject": {"retry_key": "wave"}, "settled_at": "now"})
    projection = {"panels": [panel]}
    if receipt is not None:
        projection["late_notice_receipts"] = {"acceptance-late:wave": receipt}
    return document("legacy", review_projection=projection, **fields)


@pytest.mark.parametrize("state,pending", [
    ("preparing", True), ("retained", True), ("dispatched", True), ("unpublished", True),
    ("closed", False), ("collected", False), ("preparation_unknown", False),
    ("preparation_refused", False), ("unrecognized", True),
])
def test_operation_selection_is_current_and_conservative(state, pending):
    row = document("task", review_operations={"owner": _operation(state)})
    assert obligations.review_collection_pending(row) is pending
    row["review_operations"]["owner"]["control_only"] = True
    assert obligations.review_collection_pending(row) is False
    row["review_operations"]["owner"] = "unreadable"
    assert obligations.review_collection_pending(row) is True


@pytest.mark.parametrize("actor", [
    {"transport_status": "awaiting"}, {"operation_state": "pending_dispatch"},
    {"operation_state": "in_flight"},
])
def test_pending_actor_selection_needs_no_controller_death_or_source_read(actor):
    row = document("task", status="running", review_projection={"panels": [_panel(actors=[actor])]})
    assert obligations.review_collection_pending(row)
    row["review_projection"]["panels"][0]["authority"] = "advisory"
    assert not obligations.review_collection_pending(row)


@pytest.mark.parametrize("receipt,pending", [
    (None, False), ({"panel_id": "other", "custody": "publication"}, False),
    ({"panel_id": "panel", "custody": "publication"}, True),
    ({"panel_id": "panel", "custody": "terminal_outbox", "settled_at": "old"}, True),
    ({"panel_id": "panel", "custody": "terminal_outbox", "settled_at": "now"}, False),
    ("malformed", True),
])
def test_legacy_notice_selection_does_not_invent_or_reclaim_outbox_duty(receipt, pending):
    row = _late_document(receipt=receipt)
    assert obligations.review_collection_pending(row) is pending
    row["review_operations"] = {"owner": _operation("collected")}
    assert not obligations.review_collection_pending(row)


@pytest.mark.parametrize("fields", [
    {"review_operations": []}, {"review_projection": "bad"},
    {"review_operations": {"op": _operation(state=[])}},
    {"review_projection": {"panels": [None]}},
    {"review_projection": {"panels": [_panel(actors=[None])]}},
    {"review_projection": {"panels": [_panel(actors=[{"operation_state": []}])]}},
    {"review_projection": {"panels": [_panel(late_settlement="bad")]}},
])
def test_unknown_review_shapes_never_certify_no_work(tmp_path, fields):
    row = document("unknown", **fields)
    assert obligations.review_collection_pending(row)
    publish(tmp_path, row)
    assert _flag(tmp_path, "unknown")[0] == 1


def _legacy_store(root, *rows, owned_root, tier=store.TIER_ENFORCED, shape="v1"):
    """Actual v1, either independent v2, or v3, with that writer's column list."""
    assert root.resolve().is_relative_to(owned_root.resolve()), "fixture store must stay inside its tmp_path"
    path = store.store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    known = ("has_owner_questions", "review_collection_pending") if shape == "v3" else _SCHEMA_SHAPES[shape]
    schema = store._SCHEMA
    for column, index in (
        ("has_owner_questions", "CREATE INDEX tasks_has_owner_questions ON tasks(has_owner_questions) WHERE has_owner_questions=1;\n"),
        ("review_collection_pending", store.REVIEW_COLLECTION_INDEX),
        ("plan_collection_pending", store.PLAN_COLLECTION_INDEX),
    ):
        if column not in known:
            schema = re.sub(rf",\s*{column} INTEGER", "", schema).replace(index, "")
    schema = re.sub(r",\s*drive_retention_at TEXT", "", schema)
    conn = store.connect(path, tier, create=True)
    try:
        conn.executescript(schema)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert columns & {"has_owner_questions", "review_collection_pending"} == set(known)
        assert "plan_collection_pending" not in columns
        conn.execute(f"PRAGMA application_id={store._APPLICATION_ID[tier]}")
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            _old_replace(conn, row, known=known)
        txn = store.Txn(conn)
        txn.set_meta("schema_version", 3 if shape == "v3" else (1 if shape == "v1" else 2))
        txn.set_meta("lock_tier", tier)
        txn.set_meta("phase", store.PUBLISHED)
        store.commit(conn)
    finally:
        conn.close()


def _old_replace(conn, row, *, known=()):
    columns = store.typed_columns(row)
    for column in ("has_owner_questions", "review_collection_pending", "plan_collection_pending", "drive_retention_at"):
        if column not in known:
            columns.pop(column)
    previous = conn.execute("SELECT write_seq FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()
    columns["write_seq"] = (previous[0] if previous else 0) + 1
    conn.execute("INSERT OR REPLACE INTO task_bodies(task_id,body) VALUES (?,?)",
                 (row["task_id"], store.encode_document(row)))
    names = ("task_id", *columns)
    conn.execute(f"INSERT OR REPLACE INTO tasks ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                 (row["task_id"], *columns.values()))


def _flag(root, task_id):
    with store.open_store(root, write=False) as txn:
        return txn.row(task_id, "review_collection_pending", "write_seq")


def _combined_schema(txn):
    assert txn.meta("schema_version") == store.SCHEMA_VERSION
    columns = {row["name"]: row for row in txn.conn.execute("PRAGMA table_info(tasks)")}
    assert all(columns[name]["notnull"] == 0 for name in (
        "has_owner_questions", "review_collection_pending", "plan_collection_pending"))
    indexes = {row["name"] for row in txn.conn.execute("PRAGMA index_list(tasks)")}
    assert {"tasks_has_owner_questions", "tasks_review_collection", "tasks_plan_collection"} <= indexes


def _question_document(task_id):
    return document(task_id, result="The complete answer stays intact", owner_quiz={"q": {
        "quiz_id": "q", "state": "open", "question": "Keep this format?", "options": ["Yes", "No"]}})


@pytest.mark.parametrize("tier", [store.TIER_ENFORCED, store.TIER_NAME])
@pytest.mark.parametrize("shape", ["v1", "owner-v2"])
def test_upgrade_then_old_writer_resets_unknown_without_history_loss(tmp_path, tier, shape):
    closed = document("closed", review_operations={"op": _operation("collected")}, result="full old answer")
    _legacy_store(tmp_path, closed, owned_root=tmp_path, tier=tier, shape=shape)
    store_import.upgrade_current_schema(tmp_path)
    assert _flag(tmp_path, "closed") == (None, 1)
    assert list(task_store.review_collection_candidates(tmp_path)) == []
    assert _flag(tmp_path, "closed") == (0, 1)
    newer = {**closed, "review_operations": {"op": _operation("unpublished")}}
    with store.open_store(tmp_path, write=True) as txn:
        _old_replace(txn.conn, newer, known=_SCHEMA_SHAPES[shape])
    assert _flag(tmp_path, "closed") == (None, 2)
    store_import.upgrade_current_schema(tmp_path)
    assert list(task_store.review_collection_candidates(tmp_path)) == ["closed"]
    assert _flag(tmp_path, "closed") == (1, 2)
    assert task_store.read_document(tmp_path, "closed") == newer


@pytest.mark.parametrize("shape", _SCHEMA_SHAPES)
def test_passive_read_leaves_old_schema_and_first_child_write_upgrades_before_mutator(tmp_path, monkeypatch, shape):
    child = tmp_path / "child"
    original = _question_document("child")
    _legacy_store(child, original, owned_root=tmp_path, shape=shape)
    before = store.store_path(child).read_bytes()
    assert task_store.read_document(child, "child") == original
    assert store.store_path(child).read_bytes() == before
    monkeypatch.setattr(store_import, "_reconcile", lambda *_: pytest.fail("first write must not replay/import history"))
    calls = []
    def mutate(row):
        calls.append(row["result"])
        with store.open_store(child, write=False) as txn:
            _combined_schema(txn)
            assert txn.row("child", "has_owner_questions", "write_seq") == (1, 1)
        return {**row, "review_operations": {"op": _operation()}}
    task_store.patch_document(child, "child", mutate)
    assert calls == [original["result"]] and _flag(child, "child") == (1, 2)
    assert task_store.read_document(child, "child")["owner_quiz"] == original["owner_quiz"]
    assert set(obligations.members(child, "owner_questions")) == {"child"}


@pytest.mark.parametrize("shape", _SCHEMA_SHAPES)
def test_interrupted_schema_upgrade_rolls_back_and_retries(tmp_path, monkeypatch, shape):
    _legacy_store(tmp_path, _question_document("task"), owned_root=tmp_path, shape=shape)
    with store.open_store(tmp_path, write=False) as txn:
        columns = {r["name"] for r in txn.conn.execute("PRAGMA table_info(tasks)")}
        indexes = {r["name"] for r in txn.conn.execute("PRAGMA index_list(tasks)")}
        raw = txn.body_text("task")
    real = store_import._upgrade_review_collection
    def fail(txn):
        real(txn)  # the preceding owner-flag derivation and both DDLs must roll back together
        _combined_schema(txn)
        assert txn.row("task", "has_owner_questions") == 1
        raise OSError("interrupted before the combined transaction commits")
    with monkeypatch.context() as patch:
        patch.setattr(store_import, "_upgrade_review_collection", fail)
        with pytest.raises(OSError):
            store_import.upgrade_current_schema(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        assert txn.meta("schema_version") == (1 if shape == "v1" else 2)
        assert {r["name"] for r in txn.conn.execute("PRAGMA table_info(tasks)")} == columns
        assert {r["name"] for r in txn.conn.execute("PRAGMA index_list(tasks)")} == indexes
        assert txn.body_text("task") == raw and txn.row("task", "write_seq") == 1
    store_import.upgrade_current_schema(tmp_path)
    store_import.upgrade_current_schema(tmp_path)
    assert _flag(tmp_path, "task") == (0 if shape == "review-v2" else None, 1)
    with store.open_store(tmp_path, write=False) as txn:
        _combined_schema(txn)
        assert txn.row("task", "has_owner_questions") == 1 and txn.body_text("task") == raw


@pytest.mark.parametrize("resume", ["writer", "collector", "boot"])
def test_partial_export_upgrade_does_not_publish_a_complete_schema_version(tmp_path, resume):
    original = {**_question_document("task"), "review_operations": {"op": _operation()}}
    _legacy_store(tmp_path, original, owned_root=tmp_path)
    with store.open_store(tmp_path, write=True) as txn:
        txn.set_meta("phase", store.EXPORTING)
    assert store_import.ensure_current_store(tmp_path, with_children=False)["status"] == "switch_pending"
    with store.open_store(tmp_path, write=False, service=True) as txn:
        columns = {row["name"] for row in txn.conn.execute("PRAGMA table_info(tasks)")}
        assert "has_owner_questions" in columns and "review_collection_pending" not in columns
        assert txn.meta("schema_version") == 1
        assert txn.load("task") == original and txn.row("task", "write_seq") == 1
    with pytest.raises(store.StoreRetired):
        task_store.patch_document(tmp_path, "task", lambda _: pytest.fail("export is not writable"))
    assert list(task_store.review_collection_candidates(tmp_path)) == []
    with store.open_store(tmp_path, write=True, service=True) as txn:
        txn.set_meta("phase", store.PUBLISHED)
    calls = []
    if resume == "writer":
        def mutate(row):
            with store.open_store(tmp_path, write=False) as txn:
                _combined_schema(txn)
            calls.append(row)
            return row
        assert task_store.patch_document(tmp_path, "task", mutate) == original
        assert calls == [original]
    elif resume == "boot":
        store_import.ensure_current_store(tmp_path, with_children=False)
    assert list(task_store.review_collection_candidates(tmp_path)) == ["task"]
    with store.open_store(tmp_path, write=False) as txn:
        _combined_schema(txn)
        assert txn.load("task") == original
        assert txn.row("task", "has_owner_questions", "write_seq") == (1, 2 if resume == "writer" else 1)


@pytest.mark.parametrize("shape", _SCHEMA_SHAPES)
def test_first_touch_schema_owner_honors_zero_wait_and_never_calls_mutator(tmp_path, monkeypatch, shape):
    _legacy_store(tmp_path, document("task"), owned_root=tmp_path, shape=shape)
    with store.open_store(tmp_path, write=True):
        with store.acquisition_budget(0), pytest.raises(store.StoreBusy):
            store_import.upgrade_current_schema(tmp_path)
        with store.acquisition_budget(0), pytest.raises(store.StoreBusy):
            task_store.patch_document(tmp_path, "task", lambda _: pytest.fail("not acquired"))
    assert task_store.patch_document(tmp_path, "task", lambda row: row)["task_id"] == "task"


@pytest.mark.parametrize("shape", _SCHEMA_SHAPES)
@pytest.mark.parametrize("boot", [False, True], ids=["first-touch-upgrade", "boot"])
def test_combined_upgrade_keeps_question_duty_and_lazy_review_selection(tmp_path, shape, boot):
    from ouroboros import evidence_packs

    asked, quiet = _question_document("asked"), document("quiet", result="closed history")
    _legacy_store(tmp_path, asked, quiet, owned_root=tmp_path, shape=shape)
    with store.open_store(tmp_path, write=False) as txn:
        bodies = {tid: txn.body_text(tid) for tid in ("asked", "quiet")}
    if boot:
        store_import.ensure_current_store(tmp_path, with_children=False)
    else:
        store_import.upgrade_current_schema(tmp_path)
    with store.open_store(tmp_path, write=False) as txn:
        _combined_schema(txn)
        assert txn.row("asked", "has_owner_questions") == 1
        assert txn.row("quiet", "has_owner_questions") == 0
        for tid, raw in bodies.items():
            assert txn.body_text(tid) == raw and txn.row(tid, "write_seq") == 1
            assert txn.row(tid, "review_collection_pending") == (0 if shape == "review-v2" else None)
    assert set(obligations.members(tmp_path, "owner_questions")) == {"asked"}
    assert "owner_questions" in obligations.DUTY_SETS
    assert "review_collection_pending" not in {*store.FLAG_COLUMNS.values(), *obligations.DUTY_SETS}
    # A conservative selector alone, even 1, grants no new authority to block packing.
    with store.open_store(tmp_path, write=True) as txn:
        txn.conn.execute("UPDATE tasks SET review_collection_pending=1 WHERE task_id='quiet'")
    assert evidence_packs.task_settled(tmp_path, "quiet")
    assert not evidence_packs.task_settled(tmp_path, "asked")
    task_store.patch_document(tmp_path, "asked", lambda row: {**row, "owner_quiz": {"q": {"state": "closed"}}})
    assert obligations.members(tmp_path, "owner_questions") == {}
    assert evidence_packs.task_settled(tmp_path, "asked")


@pytest.mark.parametrize("shape", _SCHEMA_SHAPES)
def test_first_collector_upgrades_either_v2_shape_and_preserves_question_obligations(tmp_path, shape):
    asked = _question_document("asked")
    pending = document("review", review_operations={"op": _operation("dispatched")})
    _legacy_store(tmp_path, asked, pending, owned_root=tmp_path, shape=shape)
    assert list(task_store.review_collection_candidates(tmp_path)) == ["review"]
    assert _flag(tmp_path, "asked") == (0, 1) and _flag(tmp_path, "review") == (1, 1)
    assert set(obligations.members(tmp_path, "owner_questions")) == {"asked"}
    assert task_store.read_document(tmp_path, "asked") == asked
    assert task_store.read_document(tmp_path, "review") == pending
    with store.open_store(tmp_path, write=False) as txn:
        _combined_schema(txn)


def test_classification_is_off_read_lock_batched_and_cas_preserves_current_writer(tmp_path, monkeypatch):
    _legacy_store(tmp_path, *(document(f"old-{i:03}", result="body" * 1000) for i in range(130)), owned_root=tmp_path)
    store_import.upgrade_current_schema(tmp_path)
    original = obligations.review_collection_pending
    classified, commits = [], []
    def classify(row):
        # Real other-connection COMMIT during classification proves no SHARED lock remains.
        if not classified:
            with store.open_store(tmp_path, write=True, wait_sec=0) as txn:
                changed = {**row, "review_operations": {"op": _operation("unpublished")}}
                txn.store_task(changed)
        classified.append(row["task_id"])
        return original(row)
    # Avoid recursive predicate instrumentation on the probe's body replacement.
    def classify_once(row):
        with monkeypatch.context() as patch:
            patch.setattr(obligations, "review_collection_pending", original)
            return classify(row)
    real_commit = store.commit
    def commit(conn):
        commits.append(conn.total_changes)
        return real_commit(conn)
    monkeypatch.setattr(obligations, "review_collection_pending", classify_once)
    monkeypatch.setattr(store, "commit", commit)
    assert list(task_store.review_collection_candidates(tmp_path)) == []
    assert _flag(tmp_path, "old-000") == (1, 2)
    assert len(classified) == 130
    assert sum(changes > 0 for changes in commits) == 4  # one real body write + three classification commits
    classified.clear()
    assert list(task_store.review_collection_candidates(tmp_path)) == ["old-000"]
    assert not classified


def test_handoff_touch_leaves_unknown_for_retry_without_creating_review_debt(tmp_path, monkeypatch):
    _legacy_store(tmp_path, document("task"), owned_root=tmp_path)
    store_import.upgrade_current_schema(tmp_path)
    original = obligations.review_collection_pending
    touched = []
    def classify(row):
        if not touched:
            task_store.patch_handoff(tmp_path, "task", "model_waits", "wait", lambda _: {"state": "closed"})
            touched.append(True)
        return original(row)
    monkeypatch.setattr(obligations, "review_collection_pending", classify)
    assert list(task_store.review_collection_candidates(tmp_path)) == []
    assert _flag(tmp_path, "task") == (None, 2)
    assert list(task_store.review_collection_candidates(tmp_path)) == []
    assert _flag(tmp_path, "task") == (0, 2)
    assert "review_collection_pending" not in store.FLAG_COLUMNS.values()
    assert not obligations.review_collection_pending(task_store.read_document(tmp_path, "task"))


def test_keyset_uses_captured_index_and_known_debt_precedes_legacy_history(tmp_path, monkeypatch):
    _legacy_store(tmp_path, *(document(f"a-{i:03}") for i in range(130)), owned_root=tmp_path)
    write_task_result(tmp_path, "z-pending", "running", review_operations={"op": _operation()})
    visited = []
    def read_one():
        candidates = iter(task_store.review_collection_candidates(tmp_path))
        visited.append(next(candidates))
        candidates.close()
    statements = captured_statements(monkeypatch, read_one)
    assert visited == ["z-pending"]
    assert not any("task_bodies" in sql for sql, _ in statements)
    selections = [(sql, params) for sql, params in statements if "review_collection_pending" in sql]
    assert selections
    for sql, params in selections:
        assert any("tasks_review_collection" in step for step in _plan(tmp_path, sql, params))
    assert _flag(tmp_path, "a-000") == (None, 1)


def test_real_collector_does_not_read_closed_history_again_and_keeps_live_debt(tmp_path, monkeypatch):
    rows = [document(f"old-{i:03}", review_projection={"panels": [_panel()]}, result="history" * 1000)
            for i in range(130)]
    _legacy_store(tmp_path, *rows, owned_root=tmp_path)
    write_task_result(tmp_path, "live", "running", review_operations={"op": _operation()})
    monkeypatch.setattr(review_operation, "local_reviews_settled", lambda: None)
    monkeypatch.setattr(review_operation, "controller_state", lambda _: "dead")
    ended = []
    monkeypatch.setattr("ouroboros.acceptance_settlement.acceptance_actor_ended", lambda *_: bool(ended))
    collected = []
    def settle(*_, **fields):
        collected.append(fields["task_id"])
        return "settled"
    monkeypatch.setattr("ouroboros.acceptance_settlement.settle_acceptance_operation", settle)
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", lambda *_a, **_k: pytest.fail("paid replay"))
    first = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert not first["settled"] and not collected
    assert _flag(tmp_path, "old-000") == (0, 1) and _flag(tmp_path, "live")[0] == 1
    real_body = store.Txn.body_text
    def body(txn, task_id):
        assert not task_id.startswith("old-"), "closed history revisited by periodic collector"
        return real_body(txn, task_id)
    monkeypatch.setattr(store.Txn, "body_text", body)
    ended.append(True)  # no body change: the previously live actor ended
    second = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert second["settled"] == [{"task_id": "live", "owner_id": "op", "status": "settled"}]
    assert collected == ["live"] and _flag(tmp_path, "live")[0] == 0
    assert not review_operation.recover_orphaned_acceptance_operations(tmp_path)["settled"]


def test_real_legacy_notice_transfers_once_to_the_existing_outbox(tmp_path, monkeypatch):
    from supervisor.terminal_delivery import pending_deliveries

    row = _late_document(receipt={"panel_id": "panel", "custody": "publication"})
    row.update(chat_id=17, result="original answer remains exact")
    row["review_projection"]["panels"][0]["late_settlement"]["note"] = "A retained critic answered."
    old = _late_document()
    old["task_id"] = "settled-without-notice"
    publish(tmp_path, row, old)
    bus = queue.Queue()
    monkeypatch.setattr("supervisor.workers.get_event_q", lambda **_kwargs: bus)
    monkeypatch.setattr(review_operation, "local_reviews_settled", lambda: None)
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", lambda *_a, **_k: pytest.fail("paid replay"))
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["settled"] == [{"task_id": "legacy", "panel_id": "panel", "status": "announced"}]
    stored = load_task_result(tmp_path, "legacy")
    assert stored["result"] == row["result"]
    assert stored["review_projection"]["late_notice_receipts"]["acceptance-late:wave"]["custody"] == "terminal_outbox"
    assert _flag(tmp_path, "legacy")[0] == 0
    assert [(event["delivery_id"], event["chat_id"]) for event in pending_deliveries(tmp_path)] == [("acceptance-late:wave", 17)]
    assert bus.qsize() == 1
    assert not review_operation.recover_orphaned_acceptance_operations(tmp_path)["settled"]
    assert bus.qsize() == 1
    assert not load_task_result(tmp_path, "settled-without-notice")["review_projection"].get("late_notice_receipts")


def test_storeless_export_import_and_changed_legacy_files_derive_membership(tmp_path):
    root = tmp_path / "original"
    row = document("task", review_operations={"op": _operation("unpublished")}, result="exact evidence")
    publish(root, row)
    store_import.export_facts(root)
    exported = root / "task_results" / "task.json"
    restored = tmp_path / "restored"
    (restored / "task_results").mkdir(parents=True)
    (restored / "task_results" / "task.json").write_bytes(exported.read_bytes())
    store_import.ensure_current_store(restored, with_children=False)
    assert task_store.read_document(restored, "task") == row and _flag(restored, "task")[0] == 1
    changed = copy.deepcopy(row)
    changed["review_operations"]["op"]["state"] = "collected"
    (restored / "task_results" / "task.json").write_text(json.dumps(changed), encoding="utf-8")
    store_import.ensure_current_store(restored, with_children=False)
    assert task_store.read_document(restored, "task") == changed and _flag(restored, "task")[0] == 0


def test_stopped_classification_leaves_unknown_and_no_per_row_commit(tmp_path, monkeypatch):
    _legacy_store(tmp_path, document("one"), document("two"), owned_root=tmp_path)
    store_import.upgrade_current_schema(tmp_path)
    stopped = []
    original = obligations.review_collection_pending
    def classify(row):
        stopped.append(True)
        return original(row)
    monkeypatch.setattr(obligations, "review_collection_pending", classify)
    assert list(task_store.review_collection_candidates(tmp_path, stop=lambda: bool(stopped))) == []
    assert _flag(tmp_path, "one") == (None, 1) and _flag(tmp_path, "two") == (None, 1)
