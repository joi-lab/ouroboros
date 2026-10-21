"""Typed sources, bounded reads, exact artifacts and real registry delivery."""
from __future__ import annotations

import errno
import gzip
import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from ouroboros import artifacts, observability, store, store_import, tool_capabilities as caps
from ouroboros.tools.query_data import _query_data, get_tools
from ouroboros.tools.registry import ToolContext, ToolRegistry


@pytest.fixture
def ctx(tmp_path):
    store_import.ensure_current_store(tmp_path, with_children=False)
    store.update_task(tmp_path, "caller", lambda _: {"status": "completed", "result": "first"})
    store.update_task(tmp_path, "second", lambda _: {"status": "completed", "result": "second"})
    repo = tmp_path / "repo"
    repo.mkdir()
    return ToolContext(repo_dir=repo, drive_root=tmp_path, task_id="caller")


def write(ctx, relative, raw):
    path = ctx.drive_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def call(ctx, **kwargs):
    text = _query_data(ctx, **kwargs)
    assert isinstance(text, str)
    return json.loads(text)


def saved(ctx, result):
    receipt = result["saved"]
    path = ctx.drive_root / "task_results" / "artifacts" / ctx.task_id / receipt["path"]
    raw = path.read_bytes()
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["size"] == len(raw)
    assert receipt["source"] == result["source"]
    return raw


def test_task_batches_use_store_and_canonical_projection(ctx):
    # A stale file must never override the public store document.
    write(ctx, "task_results/caller.json", b'{"result":"stale"}')
    result = call(ctx, op="json", kind="task_result", ids=["caller", "second"], select="result", save_as="batch.json")
    assert result["status"] == "ok" and result["data"] == {"caller": "first", "second": "second"}
    assert result["reconstructed"] is True
    assert saved(ctx, result) == b'{"caller":"first","second":"second"}'
    single = call(ctx, op="json", kind="task_result", ids=["caller"], save_as="whole.json")
    assert json.loads(saved(ctx, single))["result"] == "first"
    assert single["saved"]["reconstructed"] is True


def test_exact_whole_document_and_string_projection(ctx):
    raw = b'{ "z": 2, "primary_output": {"text":"line\\r\\n\\u03bb\\n"}, "a": 1 }\n'
    write(ctx, "state/review_ledger/review-one.json", raw)
    whole = call(ctx, op="json", kind="review_ledger", ids=["review-one"], save_as="record.json")
    assert saved(ctx, whole) == raw and not whole["reconstructed"]
    text = call(ctx, op="json", kind="review_ledger", ids=["review-one"], select="primary_output.text",
                format="text", save_as="output.txt")
    assert saved(ctx, text) == "line\r\nλ\n".encode("utf-8")
    projection = call(ctx, op="json", kind="review_ledger", ids=["review-one"], select="primary_output", save_as="projection.json")
    assert saved(ctx, projection) == '{"text":"line\\r\\nλ\\n"}'.encode("utf-8")
    assert call(ctx, op="json", kind="review_ledger", ids=["review-one"], format="text")["status"] == "error"
    collision = call(ctx, op="json", kind="review_ledger", ids=["review-one"], select="a", save_as="record.json")
    assert collision["status"] == "error" and "collision" in collision["message"]
    assert saved(ctx, whole) == raw


@pytest.mark.parametrize("same_bytes", [False, True])
@pytest.mark.parametrize("link_error", [None, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EPERM,
                                       errno.EXDEV, errno.ENOSYS, "win32"])
def test_concurrent_exports_publish_exclusively(ctx, monkeypatch, same_bytes, link_error):
    if link_error is not None:
        def unsupported(*_args, **_kwargs):
            exc = OSError(errno.EINVAL if link_error == "win32" else link_error, "fixture link unsupported")
            if link_error == "win32":
                exc.winerror = 50  # ERROR_NOT_SUPPORTED
            raise exc
        monkeypatch.setattr(artifacts.os, "link", unsupported)
    payloads = [b'{"value":"first"}', b'{"value":"first"}' if same_bytes else b'{"value":"second"}']
    for index, raw in enumerate(payloads):
        write(ctx, f"state/review_ledger/source-{index}.json", raw)
    ready, first_done, worker = threading.Barrier(2), threading.Event(), threading.local()
    original_write = artifacts.write_bytes_atomic

    def publish(path, data):
        ready.wait(timeout=10)  # Both writers reach publication before either publishes.
        if worker.index == 1:
            assert first_done.wait(timeout=10)
        return original_write(path, data)

    def export(index):
        worker.index = index
        try:
            result = call(ctx, op="json", kind="review_ledger", ids=[f"source-{index}"], save_as="shared.json")
            if index == 0:
                assert result["status"] == "ok", result
                assert saved(ctx, result) == payloads[0]
            return result
        finally:
            if index == 0:
                first_done.set()

    monkeypatch.setattr(artifacts, "write_bytes_atomic", publish)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(export, range(2)))
    if same_bytes:
        assert second["status"] == "ok", second
        assert saved(ctx, second) == payloads[0]
    else:
        assert second["status"] == "error" and "collision" in second["message"], second
    assert saved(ctx, first) == payloads[0]
    artifact_dir = artifacts.task_artifact_dir_path(ctx.drive_root, ctx.task_id)
    assert not list(artifact_dir.glob(".*.tmp"))


def test_export_link_io_failure_preserves_absence(ctx, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError(errno.EIO, "fixture link IO failure")
    monkeypatch.setattr(artifacts.os, "link", broken)
    with pytest.raises(OSError, match="fixture link IO failure"):
        artifacts.store_task_artifact_bytes(ctx.drive_root, ctx.task_id, "io.json", b"{}")
    artifact_dir = artifacts.task_artifact_dir_path(ctx.drive_root, ctx.task_id)
    assert not (artifact_dir / "io.json").exists()
    assert not list(artifact_dir.glob(".*.tmp"))


def test_artifact_publication_failure_removes_temporary(ctx, monkeypatch):
    def fail_link(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(artifacts.os, "link", fail_link)
    with pytest.raises(OSError, match="publication failed"):
        artifacts.store_task_artifact_bytes(ctx.drive_root, ctx.task_id, "failed.json", b"{}")
    artifact_dir = artifacts.task_artifact_dir_path(ctx.drive_root, ctx.task_id)
    assert not (artifact_dir / "failed.json").exists()
    assert not list(artifact_dir.glob(".*.tmp"))


@pytest.mark.parametrize("kind,path", [("queue_snapshot", "state/queue_snapshot.json"),
                                      ("queue_dispatch", "state/queue_dispatch.json"),
                                      ("advisory_review", "state/advisory_review.json")])
def test_fixed_names_and_selectors(ctx, kind, path):
    write(ctx, path, b'{"rows":[{"items":[1,2]},{"items":[3,4]}]}')
    value = call(ctx, op="json", kind=kind, select="rows[*].items[1]")
    assert value["data"] == [2, 4]
    for selector in ("rows..items", "rows[-1]", "rows[0]items", "rows[8]", "rows[*].missing"):
        assert call(ctx, op="json", kind=kind, select=selector)["status"] == "error"


@pytest.mark.parametrize("name", ["supervisor", "events", "tools", "chat"])
def test_jsonl_filter_tail_and_exact_rows(ctx, name):
    lines = [b'{ "type": "read", "n": 1 }\r\n', b'{"type":"write","n":2}\n', b'{"type":"read","n":3}\n']
    write(ctx, f"logs/{name}.jsonl", b"".join(lines))
    result = call(ctx, op="jsonl", kind=name + "_log", filter={"field": "type", "op": "eq", "value": "read"}, tail=1, save_as="row.jsonl")
    assert result["data"] == [{"type": "read", "n": 3}]
    assert saved(ctx, result) == lines[-1]
    contains = call(ctx, op="jsonl", kind=name + "_log", filter={"field": "type", "op": "contains", "value": "rea"}, select="[*].n")
    assert contains["data"] == [1, 3]
    assert call(ctx, op="jsonl", kind=name + "_log", filter={"field": "missing", "op": "eq", "value": 1})["data"] == []
    assert call(ctx, op="jsonl", kind=name + "_log", filter={"bad": 1})["status"] == "error"


def test_whole_jsonl_blank_lines_and_receipts(ctx):
    raw = b'\n {"pass":true}\n\n'
    write(ctx, "task_results/artifacts/caller/verification_receipts.jsonl", raw)
    result = call(ctx, op="jsonl", kind="verification_receipts", tail=0, save_as="receipt.jsonl")
    assert saved(ctx, result) == raw
    assert result["data"] == [{"pass": True}]


def test_stream_and_document_byte_limits(ctx, monkeypatch):
    from ouroboros import query_data_sql
    monkeypatch.setattr(query_data_sql, "MAX_BYTES", 512)
    write(ctx, "logs/tools.jsonl", b'{"n":1}\n' * 200)
    tail = call(ctx, op="jsonl", kind="tools_log", tail=2, max_bytes=512)
    assert tail["data"] == [{"n": 1}, {"n": 1}] and tail["source"]["scan_truncated"]
    assert call(ctx, op="jsonl", kind="tools_log", tail=0, max_bytes=512)["status"] == "error"
    write(ctx, "state/queue_snapshot.json", json.dumps({"large": "x" * 600}).encode("utf-8"))
    assert call(ctx, op="json", kind="queue_snapshot", max_bytes=512)["status"] == "error"
    write(ctx, "logs/tools.jsonl", b'{"value":"' + b"x" * 600 + b'"}\n')
    assert call(ctx, op="jsonl", kind="tools_log", tail=0, max_bytes=512)["status"] == "error"
    write(ctx, "state/queue_snapshot.json", json.dumps({"large": "x" * 300}).encode("utf-8"))
    assert call(ctx, op="json", kind="queue_snapshot", max_bytes=256)["status"] == "error"


def child(ctx, identity, parent="caller"):
    from ouroboros.task_custody import HEADLESS_TASKS_DIR
    store.update_task(ctx.drive_root, identity, lambda _: {"parent_task_id": parent, "root_task_id": "caller", "status": "completed"})
    root = ctx.drive_root / HEADLESS_TASKS_DIR / identity / "data"
    root.mkdir(parents=True)
    path = root / "task_drives" / identity / "delegated_runs" / "run-one.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"primary_output":{"text":"exact child output"}}', encoding="utf-8")
    return root


def test_owned_child_grandchild_and_nonowned_drive(ctx):
    owned = child(ctx, "child")
    child(ctx, "grandchild", parent="child")
    child(ctx, "stranger", parent="unrelated")
    for identity in ("child", "grandchild"):
        result = call(ctx, op="json", kind="run", child_task_id=identity, ids=["run-one"],
                      select="primary_output.text", format="text", save_as=identity + ".txt")
        assert result["status"] == "ok", result
        assert saved(ctx, result) == b"exact child output"
    assert not (owned / "task_results/artifacts/caller/child.txt").exists()
    blocked = call(ctx, op="json", kind="run", child_task_id="stranger", ids=["run-one"])
    assert blocked["status"] == "blocked" and blocked["code"] == "access_denied"
    # A child's shared root_task_id never grants a sibling's drive.
    child_ctx = ToolContext(repo_dir=ctx.repo_dir, drive_root=owned, task_id="child", budget_drive_root=str(ctx.drive_root))
    assert call(child_ctx, op="json", kind="run", child_task_id="stranger", ids=["run-one"])["status"] == "blocked"
    assert call(child_ctx, op="json", kind="task_result", ids=["caller"], select="result")["data"] == "first"


def test_shared_drive_run_uses_the_delegate_writer_location(ctx):
    """#1573: the writer retains a run's terminal JSON once in the staging task's canonical
    store and the run's custody row names it; a pre-retention run keeps its task-drive file."""
    from ouroboros import delegate_custody as custody
    from ouroboros.delegate_output import _stage_full_output
    from ouroboros.task_custody import HEADLESS_TASKS_DIR

    store.update_task(ctx.drive_root, "child", lambda _: {"parent_task_id": "caller", "status": "completed"})
    child_ctx = ToolContext(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root, task_id="child")
    raw = '{ "primary_output": {"text": "shared child output"} }\n'
    staged = _stage_full_output(child_ctx, "run-one", raw)
    retained = ctx.drive_root / "task_results" / "artifacts" / "child" / staged["source"]["path"]
    assert staged["abs_path"] == str(retained) and retained.read_bytes() == raw.encode("utf-8")
    for run_id, owner in (("run-one", "child"), ("run-legacy", "child"), ("run-other", "second")):
        assert custody.record_started(ctx.drive_root, custody.RunCustody(
            run_id=run_id, task_id=owner, route_id="r", ledger_root=str(ctx.drive_root)))
    assert custody.emit(ctx.drive_root, custody.OUTPUT_SPILLED, {
        "run_id": "run-one", "task_id": "child", "artifact": staged["source"]["path"], "source": staged["source"],
        "staged": True, "full_content": True, "sha256": staged["sha256"]})
    assert not (ctx.drive_root / HEADLESS_TASKS_DIR / "child").exists()
    result = call(ctx, op="json", kind="run", child_task_id="child", ids=["run-one"], save_as="shared-run.json")
    assert result["status"] == "ok", result
    assert saved(ctx, result) == raw.encode("utf-8")
    legacy = write(ctx, "task_drives/child/delegated_runs/run-legacy.json", b'{"legacy": true}\n')
    assert call(ctx, op="json", kind="run", child_task_id="child", ids=["run-legacy"])["data"] == {"legacy": True}
    # Another task's retained run is not the selected child's: no path reaches it here.
    other = _stage_full_output(ToolContext(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root, task_id="second"),
                               "run-other", raw)
    assert custody.emit(ctx.drive_root, custody.OUTPUT_SPILLED, {
        "run_id": "run-other", "task_id": "second", "artifact": other["source"]["path"], "source": other["source"],
        "staged": True, "full_content": True, "sha256": other["sha256"]})
    assert call(ctx, op="json", kind="run", child_task_id="child", ids=["run-other"])["status"] != "ok"
    # Retained bytes that no longer match the custody ref are refused, never served.
    retained.write_bytes(raw.replace("shared", "SHARED").encode("utf-8"))
    assert call(ctx, op="json", kind="run", child_task_id="child", ids=["run-one"])["status"] != "ok"
    assert legacy.exists()


@pytest.mark.parametrize("identity", ["../caller", "/tmp/caller", "a/b", "a\\b", "..", "C:caller"])
def test_names_refuse_paths(ctx, identity):
    assert call(ctx, op="json", kind="task_result", ids=[identity])["status"] == "error"
    assert call(ctx, op="json", kind="task_result", ids=["caller"], save_as=identity)["status"] == "error"
    assert call(ctx, op="sql", db=identity, query="SELECT 1")["status"] == "error"


def test_symlink_escapes_and_secrets_but_skill_reads_work(ctx, tmp_path):
    from ouroboros.tools.core_secret_paths import is_runtime_secret_target
    state = write(ctx, "state/skills/example/review_job.json", b'{"status":"done"}')
    assert is_runtime_secret_target(state, ctx.repo_dir, data_root=ctx.drive_root, ctx=ctx)
    assert call(ctx, op="json", kind="skill_state", ids=["example"])["data"] == {"status": "done"}
    secret = write(ctx, "settings.json", b'{"value":"fake-key-for-tests"}')
    state.unlink()
    state.symlink_to(secret)
    assert call(ctx, op="json", kind="skill_state", ids=["example"])["status"] == "blocked"
    outside = tmp_path.parent / (tmp_path.name + "-outside.json")
    outside.write_text('{"ordinary":true}', encoding="utf-8")
    target = ctx.drive_root / "state" / "queue_snapshot.json"
    target.symlink_to(outside)
    assert call(ctx, op="json", kind="queue_snapshot")["status"] == "blocked"
    target.unlink()
    target.hardlink_to(secret)
    assert call(ctx, op="json", kind="queue_snapshot")["status"] == "blocked"


def test_blob_exact_bytes_projection_limits_and_packed_resolver(ctx, monkeypatch):
    raw = '{ "text": "λ\\r\\n", "z":2 }\n'.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    path = write(ctx, f"observability/blobs/{digest}.json.gz", gzip.compress(raw))
    result = call(ctx, op="blob", sha256=digest, save_as="blob.json")
    assert saved(ctx, result) == raw
    projected = call(ctx, op="blob", sha256=digest, select="text", format="text", save_as="blob.txt")
    assert saved(ctx, projected) == "λ\r\n".encode("utf-8")
    ref = observability.write_blob(ctx.drive_root, {"big": "x" * 10000})
    assert observability.read_blob_ref(ctx.drive_root, ref) == {"big": "x" * 10000}
    with pytest.raises(ValueError, match="byte limit"):
        observability.read_blob_ref(ctx.drive_root, ref, max_bytes=100)
    with pytest.raises(ValueError, match="size"):
        observability.read_blob_ref(ctx.drive_root, {key: value for key, value in ref.items() if key != "size"})
    path.write_bytes(gzip.compress(b'{"changed":true}'))
    assert call(ctx, op="blob", sha256=digest)["status"] == "error"
    path.unlink()
    seen = []

    def packed(root, ref, **kwargs):
        seen.append((root, ref, kwargs))
        return raw

    monkeypatch.setattr(observability, "read_blob_ref", packed)
    assert call(ctx, op="blob", sha256=digest)["data"]["z"] == 2
    assert seen[0][2]["raw_bytes"] and seen[0][2]["max_bytes"] > 0


def test_money_defaults_snapshot_and_missing_packs_are_typed(ctx, monkeypatch):
    from ouroboros import query_data_sql, usage_store
    usage_store.migrate_from_journal(ctx.drive_root)
    requests = []
    monkeypatch.setattr(query_data_sql, "run_sql", lambda *a, **kw: requests.append(kw) or {"status": "ok", "data": 1})
    for snapshot in (None, False):
        assert call(ctx, op="sql", db="money", query="SELECT 1", snapshot=snapshot)["status"] == "ok"
    assert [request["snapshot"] for request in requests] == [True, False]
    no_packs = call(ctx, op="sql", db="packs", query="SELECT 1")
    assert no_packs["status"] == "unavailable" and no_packs["code"] == "no_packs"
    assert not (ctx.drive_root / "observability/packs").exists()


@pytest.mark.parametrize("cache", ["valid", "missing", "old_version", "quick_check"])
def test_pack_index_sql_recovers_from_sidecars(ctx, cache):
    from ouroboros import evidence_packs as packs
    from tests.test_evidence_packs import _task

    raw = b'{"same": true}'
    _task(ctx.drive_root, "packed", blobs=1, payloads=[raw], handle=raw)
    pack_path, sidecar = packs.write_pack(ctx.drive_root, "packed", 1, packs.loose_members(ctx.drive_root, "packed"))
    packs.index_upsert(ctx.drive_root, pack_path, sidecar)
    path = packs.packs_dir(ctx.drive_root) / packs.INDEX_NAME
    authority = {p: p.read_bytes() for p in (pack_path, path.parent / "packed.members.json")}
    if cache == "missing":
        path.unlink()
    elif cache == "old_version":
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript("""
                ALTER TABLE members RENAME TO previous_members;
                CREATE TABLE members (sha256 TEXT, task_id TEXT, generation INTEGER, offset INTEGER,
                    size INTEGER, kind TEXT, PRIMARY KEY (sha256, task_id, generation));
                INSERT OR REPLACE INTO members SELECT * FROM previous_members;
                DROP TABLE previous_members;
                UPDATE meta SET value='1' WHERE key='schema_version';
            """)
            assert conn.execute("SELECT count(*) FROM members").fetchone() == (1,)
    elif cache == "quick_check":
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE damaged(value INTEGER CHECK(value=0));
                PRAGMA ignore_check_constraints=ON;
                INSERT INTO damaged VALUES (1);
                DELETE FROM members WHERE kind='checkpoint';
                PRAGMA ignore_check_constraints=OFF;
            """)
            assert conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]
    result = call(ctx, op="sql", db="packs", query="SELECT kind, size FROM members ORDER BY kind")
    assert result["status"] == "ok", result
    assert result["data"]["rows"] == [["checkpoint", len(raw)], ["json", len(raw)]], result
    assert authority == {p: p.read_bytes() for p in authority}
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (str(packs.INDEX_SCHEMA_VERSION),)


def test_no_packs_depends_on_sidecars_not_an_empty_cache(ctx):
    from ouroboros import evidence_packs as packs
    with packs.open_index(ctx.drive_root):
        pass
    result = call(ctx, op="sql", db="packs", query="SELECT count(*) FROM members")
    assert result["status"] == "unavailable" and result["code"] == "no_packs", result


@pytest.mark.parametrize("kwargs", [
    {"select": "rows..value"}, {"filter": {"field": "a..b", "op": "eq", "value": 1}},
    {"max_bytes": 0}, {"tail": -1}, {"max_rows": 2}, {"snapshot": False},
])
def test_invalid_requests_are_rejected_before_reading(ctx, kwargs):
    assert call(ctx, op="jsonl", kind="events_log")["status"] == "unavailable"
    assert call(ctx, op="jsonl", kind="events_log", **kwargs)["status"] == "error"


def test_real_registry_light_children_and_typed_errors(ctx, monkeypatch):
    from ouroboros import config, safety
    monkeypatch.setattr(config, "get_runtime_mode", lambda: "light")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    assert registry._module_load_omissions == []
    assert "query_data" in registry._FROZEN_TOOL_MODULES
    assert safety.TOOL_POLICY["query_data"] == safety.POLICY_SKIP
    for names in (caps.CORE_TOOL_NAMES, caps.LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
                  caps.ACTING_SUBAGENT_TOOL_NAMES, caps.READ_ONLY_PARALLEL_TOOLS, caps.PAGEABLE_TOOL_RESULTS):
        assert "query_data" in names
    for constraint in ({}, {"mode": "local_readonly_subagent"},
                       {"mode": "acting_subagent", "surface": "self_worktree", "root_task_id": "caller"}):
        ctx.task_constraint = constraint
        registry.set_context(ctx)
        assert registry.get_schema_by_name("query_data") is not None
        result = registry.execute_result("query_data", {"op": "json", "kind": "task_result", "ids": ["caller"]})
        assert result.status == "ok", result.text
        denied = registry.execute_result("query_data", {"op": "sql", "query": "DELETE FROM tasks"})
        assert denied.status == "error", denied.text
    schema = get_tools()[0].schema["parameters"]
    assert "path" not in schema["properties"] and "root" not in schema["properties"]


@pytest.mark.parametrize("retained_store", [True, False])
def test_native_inspection_queries_stay_on_selected_data_plane(ctx, retained_store):
    from ouroboros.review_native_episode import inspection_registry
    from ouroboros.task_results import write_task_result
    from ouroboros.tool_access import canonical_data_root
    from ouroboros.usage_accounting import UsageScope, usage_scope

    write_task_result(ctx.drive_root, "caller", "running", root_task_id="caller")
    retained = ctx.drive_root / "retained-review"
    retained.mkdir()
    if retained_store:
        store_import.ensure_current_store(retained, with_children=False)
        store.update_task(retained, "caller", lambda _: {"status": "failed", "result": "retained"})
    retained_ctx = ToolContext(repo_dir=ctx.repo_dir, drive_root=retained, task_id="caller")
    for relative in ("state/review_ledger/record.json", "state/skills/example/review_job.json"):
        write(ctx, relative, b'{"plane":"live"}')
        write(retained_ctx, relative, b'{"plane":"retained"}')
    with usage_scope(UsageScope(drive_root=ctx.drive_root, task_id="caller", root_task_id="caller")):
        registry, inspection, _ = inspection_registry(str(ctx.repo_dir), retained, "reviewer")
        assert inspection.drive_root == retained
        assert inspection.task_id == "caller" and inspection.task_lifecycle_bound
        assert canonical_data_root(inspection) == ctx.drive_root  # Budget/Pause keep their owner.
        task = registry.execute_result("query_data", {"op": "json", "kind": "task_result", "ids": ["caller"], "select": "result"})
        facts = registry.execute_result("query_data", {"op": "sql", "query": "SELECT status FROM tasks"})
        if retained_store:
            assert task.status == "ok" and json.loads(task.text)["data"] == "retained", task.text
            assert facts.status == "ok" and json.loads(facts.text)["data"]["rows"] == [["failed"]], facts.text
        else:
            for result in (task, facts):
                assert result.status == "unavailable", result.text
                assert json.loads(result.text)["code"] == "not_found"
            assert not store.store_path(retained).exists()
        file_read = registry.execute_result("read_file", {"root": "runtime_data", "path": "state/review_ledger/record.json"})
        assert file_read.status == "ok" and '"plane":"retained"' in file_read.text, file_read.text
        for kind, identity in (("review_ledger", "record"), ("skill_state", "example")):
            result = registry.execute_result("query_data", {"op": "json", "kind": kind, "ids": [identity]})
            assert result.status == "ok" and json.loads(result.text)["data"] == {"plane": "retained"}, result.text


def test_delivery_retains_the_complete_query_before_cut(ctx):
    from tests.test_tool_result_delivery import _deliver, _row, _source_ref, _view_json
    from tests._tool_result_delivery_shared import measured_fit
    write(ctx, "state/queue_snapshot.json", json.dumps({"text": "large data " * 12000}).encode("utf-8"))
    full = _query_data(ctx, op="json", kind="queue_snapshot", max_bytes=200000)
    assert json.loads(full)["status"] == "ok"
    messages, trace = _deliver(ctx, [_row("query", "query_data", full)], measured_fit(window=12000, reserve=2000))
    shown = messages[0]["content"]
    assert _view_json(shown)["complete_chars"] == len(full)
    ref = _source_ref(shown)
    assert artifacts.read_actor_source_bytes(ctx.drive_root, ctx.task_id, ref) == full.encode("utf-8")
    assert trace["tool_calls"][0]["result_partial"]


def test_direct_current_reads_stop_within_half_the_writers_wait(ctx):
    # A direct current-store read holds the lock task writes wait on; a longer query goes to a snapshot.
    from ouroboros import query_data_sql as sql

    refused = call(ctx, op="sql", query="SELECT 1", timeout_sec=sql.CURRENT_HOLD_SEC + 1)
    assert refused["status"] == "error" and "snapshot=true" in refused["message"]
    assert call(ctx, op="sql", query="SELECT 1", timeout_sec=sql.CURRENT_HOLD_SEC)["status"] == "ok"
    assert call(ctx, op="sql", query="SELECT 1", timeout_sec=sql.CURRENT_HOLD_SEC + 1, snapshot=True)["status"] == "ok"


def test_storage_kind_reads_the_saved_count_from_the_installation_root_and_refuses_a_child(ctx, monkeypatch):
    """``kind="storage"`` is installation-wide: the saved count comes from the canonical root
    through the existing admission; a child selector is refused before any read."""
    from ouroboros import storage

    (ctx.drive_root / "observability").mkdir()
    (ctx.drive_root / "observability" / "a.bin").write_bytes(b"x" * 4096)
    table = [{"id": "model_calls", "group": "records", "roots": [(str(ctx.drive_root), ("observability",))]}]
    record = storage.measure(ctx.drive_root, places=table, sleep=lambda _s: None)
    storage.save_count(ctx.drive_root, record)
    result = call(ctx, op="json", kind="storage")
    assert result["status"] == "ok" and result["source"]["kind"] == "storage"
    assert result["data"]["latest"]["finished_at"] == record["finished_at"] and result["data"]["previous"] is None
    assert call(ctx, op="json", kind="storage", select="latest.places[*].id")["data"] == ["model_calls"]
    monkeypatch.setattr("ouroboros.tools.query_data._child_root", lambda *_a: pytest.fail("storage must not resolve a child"))
    refused = call(ctx, op="json", kind="storage", child_task_id="second")
    assert refused["status"] == "error" and "installation-wide" in refused["message"]
    assert call(ctx, op="json", kind="storage", ids=["x"])["status"] == "error"
