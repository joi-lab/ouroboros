"""Historical carriers, copyback and a fresh fork's real tool reader."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import artifacts, observability, review_operation
from ouroboros.acceptance_history import read_acceptance_history
from ouroboros.agent_task_pipeline import _store_task_result
from ouroboros.headless import copy_child_task_result, prepare_task_drive, remove_subagent_task_drive, retry_child_task_refs
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.tools.registry import ToolRegistry
from tests.test_acceptance_history import _finish, _fixture, review_policy  # noqa: F401
from tests.test_review_operation_collection import _checkpointed_operation, TASK, forbid_effects as forbid_effects


def _foreign_sources(f):
    source = artifacts.store_actor_source_bytes(f.worker, f.tid, category="tool_results",
        source_id="foreign", data=b"FOREIGN-BYTES-MUST-NOT-BE-READ", extension="txt")
    manifest = observability.persist_call(f.worker, task_id="foreign-owner", call_id="foreign",
        call_type="tool_call", payload={"result": "FOREIGN-MANIFEST"})["manifest_ref"]
    attachment = f.tmp_path / "foreign-attachment.txt"
    attachment.write_bytes(b"FOREIGN-ATTACHMENT")
    return {"task_source": source, "obsmanifest": manifest, "task_contract": {
        "attachment_manifest": [{"name": attachment.name, "abs_path": str(attachment),
            "status": "staged", "size": attachment.stat().st_size,
            "sha256": hashlib.sha256(attachment.read_bytes()).hexdigest()}]}}


@pytest.mark.parametrize("legacy", [False, True])
def test_historical_record_promotes_only_host_carriers_and_leaves_embedded_data_literal(tmp_path, monkeypatch, legacy):
    f = _fixture(tmp_path, split=True)
    foreign = _foreign_sources(f)
    owned = artifacts.store_actor_source_bytes(f.worker, f.tid, category="tool_results",
        source_id="host-result", data=b"ACTUAL-HOST-RESULT", extension="txt")
    f.trace["tool_calls"] = [{"tool": "read_file", "args": {"nested": foreign},
                              "result_source_ref": owned, "result": "bounded result"}]
    # These are deliberately embedded at every former broad-walk entry. The
    # source-bearing observation field itself is supplied by its host writer.
    evidence = {"task_inputs": {"literal": foreign}, "agent_supplied": {"nested": foreign}}
    observations = {"source_ref": owned, "delivery_results": [{"result": foreign}]}
    monkeypatch.setattr("ouroboros.task_finalization.build_completion_observations", lambda *_a: observations)
    _store_task_result(SimpleNamespace(drive_root=f.worker, repo_dir=tmp_path), f.task,
        "Final", {}, f.trace, review_evidence=evidence,
        final_delivery={"delivery_id": "exact", "chat_id": 0, "text": "Final"})
    debt = load_task_result(f.worker, f.tid)["acceptance_debt"]
    record = read_acceptance_history(f.worker, f.tid, debt)
    assert {item["location"] for item in record["sources"]} == {
        "trace.tool_calls[0].result_source_ref", "observations.source_ref"}
    assert record["owner_corpus"] == evidence["task_inputs"]
    if legacy:
        # A record emitted by the former recursive writer may already contain
        # unowned refs. Its valid outer digest does not authorize their promotion.
        record["sources"].extend({"location": "evidence.agent_supplied.nested." + key, "source_ref": ref}
                                 for key, ref in foreign.items())
        debt["source_ref"] = artifacts.store_actor_source_bytes(f.worker, f.tid,
            category="context_checkpoints", source_id="acceptance_historical",
            data=json.dumps(record).encode(), extension="json")
    forbidden = {artifacts.task_artifact_dir_path(f.worker, f.tid) / foreign["task_source"]["path"],
                 Path(foreign["obsmanifest"]["path"]),
                 Path(foreign["task_contract"]["attachment_manifest"][0]["abs_path"])}
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path not in forbidden, f"historical promotion opened unowned data: {path}"
        return real_open(path, *args, **kwargs)

    # Exercise precisely the new published historical closure. The older generic
    # review_evidence closure is not authority for this record's embedded data.
    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", guarded_open)
        promoted, state = observability.promote_child_task_refs(
            f.root, f.worker, f.tid, {"acceptance_debt": debt})
    assert not state["pending_refs"] and not state["unavailable_refs"]
    saved = read_acceptance_history(f.root, f.tid, promoted["acceptance_debt"])
    assert saved["owner_corpus"] == record["owner_corpus"]
    assert saved == record  # Even unowned legacy data keeps its captured bytes.
    assert artifacts.read_actor_source_bytes(f.root, f.tid, saved["sources"][0]["source_ref"]) == b"ACTUAL-HOST-RESULT"
    assert not (artifacts.task_artifact_dir_path(f.root, f.tid) / foreign["task_source"]["path"]).exists()


@pytest.mark.parametrize("trajectory", [False, True])
def test_tool_payload_arguments_are_not_historical_dependencies_after_copyback_gc(tmp_path, monkeypatch, trajectory):
    f = _fixture(tmp_path, split=True)
    foreign = _foreign_sources(f)
    call = {"tool": "read_file", "args": {"nested": foreign}, "result": "HOST-CALL-RESULT"}
    trace_ref = observability.persist_call(f.worker, task_id=f.tid, call_id="host-call",
        call_type="tool_call", payload=call)
    f.trace["tool_calls"] = [{**call, "trace_ref": trace_ref}]
    forbidden = artifacts.task_artifact_dir_path(f.worker, f.tid) / foreign["task_source"]["path"]
    real_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path != forbidden, "copyback treated agent arguments as source carriers"
        return real_open(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", guarded_open)
        evidence = {"tool_trajectory_source_ref": artifacts.persist_tool_trajectory_source(
            f.worker, f.tid, f.trace["tool_calls"])} if trajectory else None
        row = _finish(f, evidence=evidence)
    record = read_acceptance_history(f.root, f.tid, row["acceptance_debt"])
    ref = next(item["source_ref"] for item in record["sources"]
               if item["location"] == "trace.tool_calls[0].trace_ref.manifest_ref")
    retry_child_task_refs(f.root, f.worker, f.tid)
    assert remove_subagent_task_drive(f.root, f.tid, live=lambda _task: False)
    manifest = observability.read_call_manifest_ref(f.root, ref, task_id=f.tid)
    payload = observability.read_blob_ref(f.root, manifest["full_payload_ref"])
    assert payload == call
    if trajectory:
        trajectory_ref = next(item["source_ref"] for item in record["sources"]
                              if item["location"] == "evidence.tool_trajectory_source_ref")
        calls = json.loads(artifacts.read_actor_source_bytes(f.root, f.tid, trajectory_ref))
        assert calls[0]["args"] == call["args"]
        promoted = observability.read_call_manifest_ref(f.root, calls[0]["trace_ref"]["manifest_ref"], task_id=f.tid)
        assert observability.read_blob_ref(f.root, promoted["full_payload_ref"]) == call
    assert not (artifacts.task_artifact_dir_path(f.root, f.tid) / foreign["task_source"]["path"]).exists()


def test_partial_copyback_retry_retains_exact_history_without_replacing_subject(tmp_path, monkeypatch):
    f = _fixture(tmp_path, split=True)
    call = {"tool": "read_file", "args": {}, "result": "HOST-RETRY-RESULT"}
    trace_ref = observability.persist_call(f.worker, task_id=f.tid, call_id="retry-call",
        call_type="tool_call", payload=call)
    f.trace["tool_calls"] = [{**call, "trace_ref": trace_ref}]
    store = artifacts.store_actor_source_bytes

    def fail_history_destination(root, *args, **kwargs):
        if Path(root) == f.root and kwargs.get("source_id") == "acceptance_historical":
            raise OSError("interrupted historical publication")
        return store(root, *args, **kwargs)

    with monkeypatch.context() as fail:
        fail.setattr(artifacts, "store_actor_source_bytes", fail_history_destination)
        first = _finish(f)
        retry_child_task_refs(f.root, f.worker, f.tid)
        assert first["child_ref_promotion"]["pending_refs"]
        assert not remove_subagent_task_drive(f.root, f.tid, live=lambda _task: False)
    original = copy.deepcopy(first["acceptance_debt"])
    outcome = observability.retry_pending_child_ref_promotions(f.root)
    assert f.tid in outcome["completed"], outcome
    row = load_task_result(f.root, f.tid)
    assert row["acceptance_debt"]["source_ref"] == original["source_ref"]
    # A same-answer forged replica still cannot replace this published reference.
    write_task_result(f.root, f.tid, "completed", acceptance_debt={**original, "source_ref": {"path": "unowned"}})
    copy_child_task_result(f.root, {"id": f.tid, "drive_root": str(f.worker)})
    final = load_task_result(f.root, f.tid)
    assert final["acceptance_debt"] == row["acceptance_debt"]
    retry_child_task_refs(f.root, f.worker, f.tid)
    assert remove_subagent_task_drive(f.root, f.tid, live=lambda _task: False)
    frozen = read_acceptance_history(f.root, f.tid, final["acceptance_debt"])
    assert frozen["delivery"] == original["delivery"]
    ref = next(item["source_ref"] for item in frozen["sources"]
               if item["location"] == "trace.tool_calls[0].trace_ref.manifest_ref")
    manifest = observability.read_call_manifest_ref(f.root, ref, task_id=f.tid)
    assert observability.read_blob_ref(f.root, manifest["full_payload_ref"])["result"] == "HOST-RETRY-RESULT"


def test_fresh_fork_discovers_and_reads_historical_source_with_registered_tools(tmp_path):
    from ouroboros.tool_access import active_tool_profile

    f = _fixture(tmp_path, split=True)
    _finish(f)
    retry_child_task_refs(f.root, f.worker, f.tid)
    assert remove_subagent_task_drive(f.root, f.tid, live=lambda _task: False)
    caller_drive = prepare_task_drive(f.root, "fresh-caller", "empty")
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=caller_drive)
    ctx = registry._ctx
    ctx.task_id, ctx.current_chat_id = "fresh-caller", 0
    ctx.task_metadata = {"root_task_id": ctx.task_id, "delegation_role": "root", "budget_drive_root": str(f.root)}
    assert active_tool_profile(ctx) == "self_modification"
    assert ctx.drive_root not in (f.root, f.worker)
    ordinary = registry.execute_result("get_task_result", {"task_id": f.tid})
    assert ordinary.status == "ok", ordinary.text
    summary = json.loads(ordinary.text.split("[SUBTASK_OUTCOME]\n", 1)[1].split("\n[/SUBTASK_OUTCOME]", 1)[0])
    source = summary["acceptance_debt"]["source_ref"]
    view = registry.execute_result(source["reader"]["tool"], source["reader"]["arguments"])
    assert view.status == "ok", view.text
    projection = json.loads(view.text)["review_source"]
    assert projection["source_ref"]["sha256"] == source["sha256"]
    exact = registry.execute_result(source["reader"]["tool"], {
        **source["reader"]["arguments"], "source_start_char": 0,
        "source_end_char": projection["complete_chars"]})
    assert exact.status == "ok", exact.text
    body = json.loads(exact.text)["review_source"]["text"]
    assert hashlib.sha256(body.encode()).hexdigest() == source["sha256"]
    assert json.loads(body)["answer"] == "Original exact final answer"


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("kind", "other"), ("task_id", "foreign"),
    ("debt_id", "foreign"), ("task_attempt", 9), ("answer", "changed"),
    ("delivery", {"task_id": "foreign", "text_sha256": "a" * 64}),
])
def test_canonical_history_reader_rejects_bound_but_invalid_subject(tmp_path, field, value):
    from ouroboros.tools.control_task_results import _get_task_result
    from tests.test_acceptance_history import _caller

    f = _fixture(tmp_path)
    row = _finish(f)
    debt = row["acceptance_debt"]
    record = read_acceptance_history(f.root, f.tid, debt)
    record[field] = value
    ref = artifacts.store_actor_source_bytes(f.root, f.tid, category="context_checkpoints",
        source_id="acceptance_historical", data=json.dumps(record).encode(), extension="json")
    # Simulate corrupted canonical HOST metadata, bypassing replica protection
    # only in the fixture. Even a valid CAS hash is not historical identity.
    path = f.root / "task_results" / (f.tid + ".json")
    row["acceptance_debt"] = {**debt, "source_ref": ref}
    path.write_text(json.dumps(row))
    result = json.loads(_get_task_result(_caller(f), f.tid, review_source_sha256=ref["sha256"]))
    assert result["review_source"]["status"] == "unavailable"
    assert result["review_source"]["reason"] == "source_identity_mismatch"
    assert "text" not in result["review_source"]


def test_canonical_history_reader_requires_exact_debt_membership(tmp_path):
    from ouroboros.tools.control_task_results import _get_task_result
    from tests.test_acceptance_history import _caller

    f = _fixture(tmp_path)
    row = _finish(f)
    record = read_acceptance_history(f.root, f.tid, row["acceptance_debt"])
    record["gaps"].append({"section": "foreign", "reason": "not_pinned"})
    ref = artifacts.store_actor_source_bytes(f.root, f.tid, category="context_checkpoints",
        source_id="acceptance_historical", data=json.dumps(record).encode(), extension="json")
    result = json.loads(_get_task_result(_caller(f), f.tid, review_source_sha256=ref["sha256"]))
    assert result["review_source"]["status"] == "unavailable"
    assert "text" not in result["review_source"]


@pytest.mark.parametrize("tool", ["service_logs", "stop_service"])
def test_historical_service_log_carrier_does_not_promote_quoted_process_data(tmp_path, tool):
    f = _fixture(tmp_path, split=True)
    foreign = _foreign_sources(f)
    owned = observability.write_blob(f.worker, "HOST-RETAINED-LOG", kind="txt")
    quoted = "FULL_RESULT_SOURCE_JSON=" + json.dumps(foreign["task_source"])
    log = {"full_log_ref": owned, "tail": quoted, "errors": [foreign]}
    result = log if tool == "service_logs" else {"log_finalization": log}
    call = {"tool": tool, "args": {}, "result": json.dumps(result)}
    trace_ref = observability.persist_call(f.worker, task_id=f.tid, call_id="service-call",
                                          call_type="tool_call", payload=call)
    f.trace["tool_calls"] = [{**call, "trace_ref": trace_ref}]
    trajectory = artifacts.persist_tool_trajectory_source(f.worker, f.tid, f.trace["tool_calls"])
    raw = artifacts.read_actor_source_bytes(f.worker, f.tid, trajectory)
    row = _finish(f, evidence={"tool_trajectory_source_ref": trajectory})
    retry_child_task_refs(f.root, f.worker, f.tid)
    record = read_acceptance_history(f.root, f.tid, row["acceptance_debt"])
    ref = next(item["source_ref"] for item in record["sources"]
               if item["location"] == "trace.tool_calls[0].trace_ref.manifest_ref")
    assert remove_subagent_task_drive(f.root, f.tid, live=lambda _task: False)
    manifest = observability.read_call_manifest_ref(f.root, ref, task_id=f.tid)
    payload = observability.read_blob_ref(f.root, manifest["full_payload_ref"])
    assert payload == call
    assert artifacts.read_actor_source_bytes(f.root, f.tid, trajectory) == raw
    retained = json.loads(payload["result"])
    log = retained if tool == "service_logs" else retained["log_finalization"]
    assert observability.read_blob_ref(f.root, log["full_log_ref"], expected_kind="txt") == "HOST-RETAINED-LOG"
    assert log["tail"] == quoted and log["errors"] == [foreign]
    assert not (artifacts.task_artifact_dir_path(f.root, f.tid) / foreign["task_source"]["path"]).exists()


@pytest.mark.parametrize('field,value', [('schema_version', 2), ('kind', 'other'), ('task_id', 'foreign')])
def test_historical_promotion_requires_exact_host_shape_and_task_binding(tmp_path, monkeypatch, field, value):
    f = _fixture(tmp_path, split=True)
    owned = artifacts.store_actor_source_bytes(f.worker, f.tid, category='tool_results',
        source_id='must-not-follow', data=b'UNBOUND', extension='txt')
    record = {'schema_version': 1, 'kind': 'acceptance_historical_subject', 'task_id': f.tid,
              'sources': [{'location': 'observations.source_ref', 'source_ref': owned}], field: value}
    outer = artifacts.store_actor_source_bytes(f.worker, f.tid, category='context_checkpoints',
        source_id='acceptance_historical', data=json.dumps(record).encode(), extension='json')
    original = artifacts.read_actor_source_bytes
    def guarded(root, task_id, ref):
        assert ref.get('sha256') != owned['sha256'], 'unbound history acquired source authority'
        return original(root, task_id, ref)
    monkeypatch.setattr(artifacts, 'read_actor_source_bytes', guarded)
    promoted, state = observability.promote_child_task_refs(
        f.root, f.worker, f.tid, {'acceptance_debt': {'source_ref': outer}})
    assert promoted['acceptance_debt']['source_ref'] == outer
    assert state['status'] == 'incomplete' and state['pending_refs']
    assert 'historical acceptance source owner mismatch' in state['pending_refs'][0]['reason']
    assert not (artifacts.task_artifact_dir_path(f.root, f.tid) / owned['path']).exists()


@pytest.mark.parametrize('trajectory', [False, True])
def test_frozen_request_uses_existing_source_closure_after_current_state_and_author_gc(tmp_path, monkeypatch, trajectory):
    from ouroboros.review_source_closure import retain_review_request_sources
    from ouroboros.review_records import ReviewRequest
    from ouroboros.review_native_episode import inspection_registry

    f = _fixture(tmp_path, split=True)
    foreign = _foreign_sources(f)
    call = {'tool': 'read_file', 'args': {'agent_supplied': foreign}, 'result': 'HISTORICAL-OUTPUT'}
    trace_ref = observability.persist_call(f.worker, task_id=f.tid, call_id='frozen-call',
                                           call_type='tool_call', payload=call)
    f.trace['tool_calls'] = [{**call, 'trace_ref': trace_ref}]
    evidence = {'tool_trajectory_source_ref': artifacts.persist_tool_trajectory_source(
        f.worker, f.tid, f.trace['tool_calls'])} if trajectory else None
    row = _finish(f, evidence=evidence)
    debt = row['acceptance_debt']
    frozen = read_acceptance_history(f.root, f.tid, debt)
    write_task_result(f.root, f.tid, 'completed', result='CURRENT-MUST-NOT-BE-SNAPSHOT',
                      task_contract={'expected_output': 'CURRENT-CRITERIA'})
    retry_child_task_refs(f.root, f.worker, f.tid)
    assert remove_subagent_task_drive(f.root, f.tid, live=lambda _task: False)
    request = ReviewRequest(surface='task_acceptance', task_id=f.tid, task_attempt=debt['task_attempt'],
                            subject=frozen['answer'], goal='Review the frozen answer', evidence={'historical_subject': frozen})
    retain_review_request_sources(request, source_root=f.root, custody_root=f.root, historical_debt=debt)
    closure = request.policy['review_source_closure']
    assert {row['name'] for row in closure['sources']} == ({'historical-subject', 'evidence.tool_trajectory_source_ref'}
                                                         if trajectory else {'historical-subject'})
    named = closure['sources'][0]
    registry, _reader, _ = inspection_registry(str(f.tmp_path), Path(closure['read_root']), f.tid)
    read = registry.execute_result('read_file', named['source_ref']['read']['arguments'])
    assert read.status == 'ok' and frozen['answer'] in read.text
    assert 'CURRENT-MUST-NOT-BE-SNAPSHOT' not in read.text and 'CURRENT-CRITERIA' not in read.text
    retained = json.loads(artifacts.read_actor_source_bytes(closure['read_root'], f.tid, named['source_ref']))
    ref = next(item['source_ref'] for item in retained['sources']
               if item['location'] == 'trace.tool_calls[0].trace_ref.manifest_ref')
    manifest = observability.read_call_manifest_ref(Path(closure['read_root']), ref, task_id=f.tid)
    assert observability.read_blob_ref(Path(closure['read_root']), manifest['full_payload_ref']) == call
    assert not (artifacts.task_artifact_dir_path(closure['read_root'], f.tid) / foreign['task_source']['path']).exists()
    # Reuse verifies the original reader closure, even though mutable result changed.
    before = copy.deepcopy(request)
    retain_review_request_sources(request, source_root=closure['read_root'], custody_root=f.root)
    assert request == before and load_task_result(f.root, f.tid)['acceptance_debt'] == debt


@pytest.mark.serial
@pytest.mark.parametrize('mutation', [
    'none', 'lineage_missing', 'child', 'retry_alias', 'lineage_conflict', 'off_no_claim',
    'claimant', 'paid_identity', 'binding', 'subject', 'request_task', 'request_attempt',
    'pointer_attempt', 'controller', 'operations', 'roster', 'source_schema', 'source_task',
    'missing_source', 'corrupt_source', 'new_empty_authority',
])
def test_legacy_checkpoint_cold_maintenance_requires_exact_same_root_claim(tmp_path, monkeypatch, forbid_effects, mutation):
    from ouroboros.artifacts import read_actor_source_bytes, store_actor_source_bytes
    from ouroboros.task_results import task_result_path
    from supervisor.terminal_delivery import pending_deliveries

    checkpoint, binding = _checkpointed_operation(tmp_path, legacy=True)
    ref = checkpoint['source_ref']
    original_bytes = read_actor_source_bytes(tmp_path, TASK, ref)
    source = json.loads(original_bytes)
    assert 'paid_authority' not in source
    row = load_task_result(tmp_path, TASK)
    claim = row['task_acceptance_review_accounting']['claims_by_binding'][binding['binding_hash']]
    if mutation == 'lineage_missing':
        row.pop('root_task_id')
        row.pop('delegation_role')
    elif mutation == 'child':
        row.update(parent_task_id='parent', delegation_role='subagent')
    elif mutation == 'retry_alias':
        write_task_result(tmp_path, 'original-root', 'completed', root_task_id='original-root', delegation_role='root')
        row.update(root_task_id='original-root', original_task_id='original-root', timeout_retry_from='original-root')
    elif mutation == 'lineage_conflict':
        row['metadata'] = {'root_task_id': 'foreign', 'parent_task_id': 'parent', 'delegation_role': 'subagent'}
    elif mutation == 'off_no_claim':
        row.pop('task_acceptance_review_accounting')
        monkeypatch.setenv('OUROBOROS_TASK_REVIEW_MODE', 'off')
    elif mutation == 'claimant':
        claim['claimed_by_task_id'] = 'child'
    elif mutation == 'paid_identity':
        claim['paid_identity'] = 'f' * 64
    elif mutation == 'binding':
        claim['evidence_revision'] = 'f' * 64
    elif mutation in {'subject', 'request_task', 'request_attempt'}:
        key, value = {'subject': ('subject', 'different answer'), 'request_task': ('task_id', 'foreign'),
                      'request_attempt': ('task_attempt', 2)}[mutation]
        source['request'][key] = value
    elif mutation == 'pointer_attempt':
        checkpoint['task_attempt'] = 2
    elif mutation == 'controller':
        source['controller']['session'] = 'foreign-session'
    elif mutation == 'operations':
        source['operations'] = {'a': 'different-op'}
    elif mutation == 'roster':
        source['slot_roster'][0]['slot_id'] = 'foreign-slot'
    elif mutation == 'source_schema':
        source['schema_version'] = 2
    elif mutation == 'source_task':
        source['task_id'] = 'foreign'
    elif mutation == 'new_empty_authority':
        source['paid_authority'] = {}
    if source != json.loads(original_bytes):
        checkpoint['source_ref'] = store_actor_source_bytes(tmp_path, TASK, category='context_checkpoints',
            source_id='acceptance-operation', data=json.dumps(source).encode(), extension='json')
    if mutation == 'missing_source':
        checkpoint['source_ref'] = {**ref, 'path': ref['path'].replace('acceptance-operation', 'missing-operation')}
    elif mutation == 'corrupt_source':
        checkpoint['source_ref'] = {**ref, 'sha256': '0' * 64}
    owner = checkpoint.pop('owner_id')
    row['review_operations'] = {owner: {**checkpoint, 'surface': 'task_acceptance', 'state': 'dispatched'}}
    # Deliberately corrupt canonical facts, not a caller-supplied alias/result.
    task_result_path(tmp_path, TASK).write_text(json.dumps(row))
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    after = load_task_result(tmp_path, TASK)
    assert read_actor_source_bytes(tmp_path, TASK, ref) == original_bytes
    assert after.get('task_acceptance_review_accounting') == row.get('task_acceptance_review_accounting')
    if mutation == 'none':
        assert report['settled'] and not report['errors'], report
        assert after['review_operations'][owner]['state'] == 'collected'
        panels = after['review_projection']['panels']
        assert len(panels) == 1 and panels[0]['actors'][0]['operation_state'] == 'custody_lost'
        assert panels[0]['aggregate_signal'] == 'DEGRADED'  # missing API outcome, never resend
        assert len(pending_deliveries(tmp_path)) == 1
        assert not review_operation.recover_orphaned_acceptance_operations(tmp_path)['settled']
    else:
        assert not report['settled'] and (report['pending'] or report['errors']), report
        assert after['review_operations'][owner]['state'] == 'dispatched'
        assert not after.get('review_projection') and not pending_deliveries(tmp_path)
