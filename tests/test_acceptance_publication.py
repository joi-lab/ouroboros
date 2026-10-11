"""Full applied acceptance custody and monotonically published read models."""

import copy
import hashlib
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path

import pytest

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import artifacts, loop, review_projection
from ouroboros.gateway.tasks import api_task_artifact
from ouroboros.review_substrate import ReviewRunResult
from ouroboros.task_results import load_task_result, write_task_result


def _context(root):
    return SimpleNamespace(task_id="applied", task_attempt=1, drive_root=root,
                           task_metadata={}, event_queue=queue.Queue(), current_chat_id=1)


def _run(task_attempt=1):
    findings = [{"id": f"f{i}", "severity": "low", "item": f"full finding {i}",
                 "evidence": "complete context", "recommendation": "consider later"} for i in range(80)]
    return {"request": {"surface": "task_acceptance", "task_id": "applied"},
            "panel_id": "panel_exact", "authority": "host_root", "task_attempt": task_attempt, "aggregate_signal": "PASS",
            "actors": [{"slot_id": "s1", "signal": "PASS", "status": "ok",
                        "parsed": {"verdict": "PASS", "outcome_tier": "solved", "findings": findings},
                        "criteria_refs_unresolved": [{"criterion": "complete", "supported_evidence_resolves": True}]}],
            "parsed_findings": findings, "enforcement_impact": "allows_completion",
            "dialogue": {"status": "inconclusive"}}


def _source(root, panel):
    ref = panel["applied_source_ref"]
    path = artifacts.task_artifact_dir_path(root, "applied") / ref["path"]
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ref["sha256"]
    assert len(raw) == ref["size"]
    return json.loads(raw)


def test_full_applied_source_downloads_while_task_is_running(tmp_path):
    ctx = _context(tmp_path)
    write_task_result(tmp_path, "applied", "running", result="working", independent={"keep": True})
    trace = {"review_runs": [_run()]}
    loop._set_acceptance_decision(trace, {"status": "accepted", "reason": "clean_pass"})
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    stored = load_task_result(tmp_path, "applied")
    panel = stored["review_projection"]["panels"][0]
    assert stored["status"] == "running"
    assert stored["result"] == "working" and stored["independent"] == {"keep": True}
    assert "artifacts" not in stored, "the live publisher must not replace another writer's artifact list"
    full = _source(tmp_path, panel)
    assert full["applied_decision"]["status"] == "accepted"
    assert full["enforcement_impact"] == "allows_completion"
    assert full["actors"][0]["criteria_refs_unresolved"][0]["supported_evidence_resolves"] is True
    assert len(full["actors"][0]["parsed"]["findings"]) == 80
    assert len(panel["actors"][0]["findings"]) < 80
    # Real endpoint path and its normal effective-task materialization. Nothing
    # ran terminal collection, and no path-only bypass grants artifact access.
    app = Starlette(routes=[Route("/api/tasks/{task_id}/artifacts/{name}", api_task_artifact)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        ref = panel["applied_source_ref"]
        response = client.get(f"/api/tasks/applied/artifacts/{Path(ref['path']).name}",
                              params={"source": ref["path"]})
        assert response.status_code == 200
        assert response.json() == full
        assert client.get("/api/tasks/applied/artifacts/missing.json").status_code == 404
    envelope = ctx.event_queue.get_nowait()
    assert envelope["type"] == "log_event"
    event = envelope["data"]
    assert event["type"] == "review_reference" and event["surface"] == "task_acceptance"
    assert event["presentation_owner_task_id"] == "applied"
    assert len(event["state_revision"]) == 64


def test_delayed_publication_cannot_replace_supersession_or_terminal_fields(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    trace = {"review_runs": [_run()]}
    reached, release = threading.Event(), threading.Event()
    actual_store = artifacts.store_actor_source_bytes
    first_thread = []

    def delayed_store(*args, **kwargs):
        if not first_thread:
            first_thread.append(threading.get_ident())
            reached.set()
            assert release.wait(10)
        return actual_store(*args, **kwargs)

    monkeypatch.setattr(artifacts, "store_actor_source_bytes", delayed_store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        older = executor.submit(review_projection.publish_acceptance_checkpoint, ctx, trace)
        try:
            assert reached.wait(10)
            trace["review_runs"][0].update(superseded_by_revision=True, superseded_reason="owner_followup",
                                           enforcement_impact="requires_revision")
            loop._set_acceptance_decision(trace, {"status": "revision_requested", "reason": "owner_followup"})
            write_task_result(tmp_path, "applied", "completed", result="delivered", independent={"keep": True},
                              accounted_upper_bound_usd=7.25, cost_final=True)
            review_projection.publish_acceptance_checkpoint(ctx, trace)
        finally:
            release.set()
        older.result(timeout=10)
    stored = load_task_result(tmp_path, "applied")
    assert stored["status"] == "completed" and stored["result"] == "delivered"
    assert stored["independent"] == {"keep": True} and stored["accounted_upper_bound_usd"] == 7.25
    panels = stored["review_projection"]["panels"]
    assert len(panels) == 1 and panels[0]["publication_revision"] == 2
    assert panels[0]["superseded"] is True
    assert panels[0]["applied_source_ref"] == trace["review_runs"][0]["applied_source_ref"]
    assert _source(tmp_path, panels[0])["applied_decision"]["reason"] == "owner_followup"


def test_stale_child_read_and_copyback_keep_newest_canonical_panel(tmp_path):
    from ouroboros.headless import copy_child_task_result
    from ouroboros.task_status import load_effective_task_result

    canonical, child = tmp_path / "canonical", tmp_path / "child"
    old = {"surface": "task_acceptance", "panel_id": "p", "task_attempt": 1,
           "publication_revision": 1, "superseded": False}
    new = {**old, "publication_revision": 2, "superseded": True}
    write_task_result(canonical, "applied", "completed", child_drive_root=str(child),
                      root_phase_checkpoint={"post_task_synthesis": "completed"},
                      review_projection={"panels": [new]}, accounted_upper_bound_usd=7, cost_final=True)
    write_task_result(child, "applied", "completed", review_projection={"panels": [old]},
                      accounted_upper_bound_usd=2, cost_final=False, result="replica result")
    read = load_effective_task_result(canonical, "applied", materialize_artifacts=False)
    assert read["review_projection"]["panels"] == [new]
    copied = copy_child_task_result(canonical, {"id": "applied", "drive_root": str(child)})
    assert copied["review_projection"]["panels"] == [new]
    assert copied["accounted_upper_bound_usd"] == 7 and copied["cost_final"] is True
    assert copied["result"] == "replica result"


@pytest.mark.parametrize("superseded", [False, True])
def test_promoted_refs_do_not_shadow_next_publication_or_regress_on_child_replay(tmp_path, monkeypatch, superseded):
    from ouroboros import observability
    from ouroboros.headless import copy_child_task_result, prepare_task_drive, retry_child_task_refs

    parent = tmp_path / "canonical"
    child = prepare_task_drive(parent, "applied", "empty")
    ctx = _context(child)
    ctx.budget_drive_root = parent
    run = _run()
    call = observability.persist_call(child, task_id='applied', call_id='first', call_type='tool_call',
                                      payload={'tool': 'run_command', 'result': 'first'})
    run["request"]["evidence"] = {'tool_trajectory_source_ref': artifacts.persist_tool_trajectory_source(
        child, 'applied', [{'tool': 'run_command', 'trace_ref': call}])}
    trace = {"review_runs": [run]}
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    first = copy.deepcopy(load_task_result(parent, "applied")["review_projection"])
    first_ref = first["panels"][0]["applied_source_ref"]
    write_task_result(child, "applied", "completed", review_projection=first)
    task = {"id": "applied", "drive_root": str(child)}
    copied = copy_child_task_result(parent, task)
    copied = retry_child_task_refs(parent, child, "applied")
    assert copied["review_projection"]["panels"][0]["applied_source_ref"] == first_ref
    assert copied["review_projection"]["panels"][0]["publication_revision"] == 1
    assert trace["_acceptance_publication_revision"] == 1

    call = observability.persist_call(parent, task_id='applied', call_id='new', call_type='tool_call',
                                      payload={'tool': 'run_command', 'result': 'new publication'})
    run["request"]["evidence"] = {'tool_trajectory_source_ref': artifacts.persist_tool_trajectory_source(
        parent, 'applied', [{'tool': 'run_command', 'trace_ref': call}])}
    run["superseded_by_revision"] = superseded
    run["aggregate_signal"] = "FAIL"
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    current = load_task_result(parent, "applied")["review_projection"]
    assert current["panels"][0]["publication_revision"] == 2
    assert _source(parent, current["panels"][0])["aggregate_signal"] == "FAIL"
    missing = {**first_ref, "path": "source_handles/context_checkpoints/missing.json"}
    promote = observability._promote_task_source_ref

    def forbid_discarded_review(*args, **kwargs):
        ref = args[3]
        assert ref not in (first_ref, missing), "discarded child review was physically promoted"
        return promote(*args, **kwargs)

    monkeypatch.setattr(observability, "_promote_task_source_ref", forbid_discarded_review)
    for revision in (1, 2):
        stale = copy.deepcopy(first)
        stale["panels"][0].update(publication_revision=revision, applied_source_ref=missing)
        write_task_result(child, "applied", "completed", review_projection=stale)
        copied = copy_child_task_result(parent, task)
        copied = retry_child_task_refs(parent, child, "applied")
        assert copied["review_projection"] == current
        assert copied["child_ref_promotion"]["status"] == "complete"
        assert copied["child_ref_promotion"]["pending_refs"] == []
        assert copied["child_ref_promotion"]["unavailable_refs"] == []
        assert _source(parent, copied["review_projection"]["panels"][0])["request"]["evidence"] == run["request"]["evidence"]


def test_new_task_attempt_is_not_deduplicated_with_previous_attempt(tmp_path):
    ctx = _context(tmp_path)
    first = {"review_runs": [_run()]}
    review_projection.publish_acceptance_checkpoint(ctx, first)
    old = copy.deepcopy(load_task_result(tmp_path, "applied")["review_projection"])
    ctx.task_attempt = 2
    review_projection.publish_acceptance_checkpoint(ctx, {"review_runs": [_run(task_attempt=2)]})
    write_task_result(tmp_path, "applied", "running", review_projection=old)
    rows = load_task_result(tmp_path, "applied")["review_projection"]["panels"]
    assert [p["task_attempt"] for p in rows] == [1, 2]


def test_publishing_legacy_run_does_not_invent_its_task_attempt(tmp_path):
    ctx, run = _context(tmp_path), _run()
    run.pop("task_attempt")
    ctx.task_attempt = 2
    review_projection.publish_acceptance_checkpoint(ctx, {"review_runs": [run]})
    panel = load_task_result(tmp_path, "applied")["review_projection"]["panels"][0]
    assert "task_attempt" not in panel


def test_source_failure_discloses_unavailable_without_changing_verdict(tmp_path, monkeypatch):
    ctx, trace = _context(tmp_path), {"review_runs": [_run()]}
    monkeypatch.setattr(artifacts, "store_actor_source_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")))
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    panel = load_task_result(tmp_path, "applied")["review_projection"]["panels"][0]
    assert panel["aggregate_signal"] == "PASS"
    assert panel["applied_source_status"] == "unavailable" and "applied_source_ref" not in panel


@pytest.mark.parametrize("apply_failure", [False, True])
def test_host_application_publishes_full_decision_before_finalization(tmp_path, monkeypatch, apply_failure):
    from ouroboros.contracts.task_contract import build_task_contract
    import ouroboros.review_substrate as substrate

    ctx = _context(tmp_path)
    ctx.task_contract = build_task_contract({"id": "applied", "root_task_id": "applied"})
    ctx._task_acceptance_reviewed = False
    ctx.is_direct_chat = False
    write_task_result(tmp_path, "applied", "running", task_contract=ctx.task_contract)
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setattr(substrate, "triad_delivery_slots", lambda **kw: [])
    parsed = {"verdict": "PASS", "outcome_tier": "solved", "completion_coach": "ship",
              "criteria_used": [{"criterion": "requested file exists", "status": "supported",
                                 "evidence_refs": ["artifacts"]}]}
    result = ReviewRunResult(request={"surface": "task_acceptance"}, aggregate_signal="PASS",
                             actors=[{"signal": "PASS", "slot_id": "s1", "status": "ok", "parsed": parsed}], parsed_findings=[])
    monkeypatch.setattr(loop, "_execute_task_acceptance_panel", lambda context: result)
    if apply_failure:
        def fail_apply(*args, **kwargs):
            raise RuntimeError("host application failed after receiving the panel")
        monkeypatch.setattr("ouroboros.loop_acceptance_review._apply_task_acceptance_result", fail_apply)
    trace = {"tool_calls": []}
    again = loop._run_task_acceptance_review_once(
        tools=SimpleNamespace(_ctx=ctx), content="The requested file is ready.", task_id="applied", task_type="task",
        llm_trace=trace, drive_root=tmp_path, messages=[], emit_progress=lambda *_a, **_k: None,
    )
    assert again is apply_failure
    assert trace["acceptance_decision"]["status"] == ("revision_requested" if apply_failure else "accepted")
    # Host failure is a separate task fact, not a fabricated second critic.
    # The received reviewer record still gets its own exact source publication.
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    saved = load_task_result(tmp_path, "applied")
    assert saved["status"] == "running"
    panels = saved["review_projection"]["panels"]
    assert len(panels) == 1
    full = _source(tmp_path, panels[0])
    assert full["task_attempt"] == ctx.task_attempt
    assert full["actors"][0]["parsed"] == parsed
    if apply_failure:
        assert "applied_decision" not in full
        assert trace["review_decision"]["host_failure"]["stage"] == "application"
        assert trace["acceptance_decision"]["origin"] == "host_acceptance_processing"
    else:
        assert full["applied_decision"] == trace["acceptance_decision"]
        assert full["enforcement_impact"] == "allows_completion"


def test_a_task_only_decision_publishes_a_settled_panel_and_keeps_unchanged_ones_identity_bound(tmp_path):
    """The host's own local decision is no panel's applied verdict: it rewrites no
    custody and grants a never-published LIVE producer none. A pending producer that
    settles afterwards is a genuine producer update and must still reach the
    durable projection — and so must a settled record that was never published at
    all: a missing publication stamp alone never suppresses a real verdict. An
    unchanged historical record keeps its revision and source."""
    from ouroboros.acceptance_preparation import LOCAL_PREPARATION_ORIGIN

    ctx = _context(tmp_path)
    historical = {**_run(), "panel_id": "panel_history", "aggregate_signal": "FAIL"}
    pending = {**_run(), "panel_id": "panel_pending", "aggregate_signal": "", "parsed_findings": [],
               "actors": [{"slot_id": "s1", "operation_state": "in_flight", "operation_id": "op-1"}]}
    unpublished = {**_run(), "panel_id": "panel_unpublished", "aggregate_signal": "PASS"}
    live = {**_run(), "panel_id": "panel_live", "aggregate_signal": "", "parsed_findings": [],
            "actors": [{"slot_id": "s1", "operation_state": "in_flight", "operation_id": "op-2"}]}
    trace = {"review_runs": [historical, pending]}
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    first = copy.deepcopy(load_task_result(tmp_path, "applied")["review_projection"])
    assert [row["publication_revision"] for row in first["panels"]] == [1, 1]
    assert first["publication_revision"] == 1 and first["task_attempt"] == 1
    history_ref, pending_ref = historical["applied_source_ref"], pending["applied_source_ref"]

    trace["review_runs"].extend([unpublished, live])
    incident = {"incident_id": "acceptance-preparation:x", "status": "failed", "attempts": 1,
                "stage": "preparation", "source_identity": "x"}
    trace["acceptance_preparation"] = dict(incident)
    trace["acceptance_decision"] = {"origin": LOCAL_PREPARATION_ORIGIN, "status": "finalized_unaccepted",
                                    "reason": "acceptance_preparation_failed"}
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    stored = load_task_result(tmp_path, "applied")["review_projection"]
    assert stored["acceptance_incident"]["incident_id"] == "acceptance-preparation:x"
    assert stored["publication_revision"] == 2
    rows = {row["panel_id"]: row for row in stored["panels"]}
    assert rows["panel_history"] == first["panels"][0] and rows["panel_pending"] == first["panels"][1]
    # A genuine first-time SETTLED record is published under this revision…
    assert rows["panel_unpublished"]["publication_revision"] == 2
    assert _source(tmp_path, rows["panel_unpublished"])["aggregate_signal"] == "PASS"
    assert unpublished["publication_revision"] == 2 and unpublished["applied_source_ref"]
    # …while a never-published LIVE producer gains no custody from a task-only decision.
    assert "publication_revision" not in rows["panel_live"] and "applied_source_ref" not in rows["panel_live"]
    assert "publication_revision" not in live and "applied_source_ref" not in live
    assert historical["applied_source_ref"] == history_ref and historical["publication_revision"] == 1
    assert pending["applied_source_ref"] == pending_ref and pending["publication_revision"] == 1
    assert all("applied_decision" not in run for run in trace["review_runs"])

    # The pending producer settles ($0 reconcile, late settlement): its OWN record moved.
    pending["actors"] = [{"slot_id": "s1", "operation_state": "settled", "operation_id": "op-1",
                          "signal": "FAIL", "status": "ok", "parsed": {"verdict": "FAIL", "findings": []}}]
    pending["aggregate_signal"] = "FAIL"
    pending["late_settlement"] = {"note": "Reviewers later rejected this answer.",
                                  "reviewed_revision": "delivered", "settled_after_terminal": True}
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    settled = load_task_result(tmp_path, "applied")["review_projection"]
    rows = {row["panel_id"]: row for row in settled["panels"]}
    assert rows["panel_history"] == first["panels"][0]                 # unchanged: identity-bound
    assert rows["panel_pending"]["publication_revision"] == 3
    assert rows["panel_pending"]["aggregate_signal"] == "FAIL"
    assert rows["panel_pending"]["late_settlement"]["note"] == "Reviewers later rejected this answer."
    assert _source(tmp_path, rows["panel_pending"])["aggregate_signal"] == "FAIL"
    assert pending["applied_source_ref"] != pending_ref and pending["publication_revision"] == 3
    assert historical["applied_source_ref"] == history_ref and historical["publication_revision"] == 1
    assert unpublished["publication_revision"] == 2 and "publication_revision" not in live
    assert all("applied_decision" not in run for run in trace["review_runs"])
    assert settled["acceptance_incident"]["incident_id"] == "acceptance-preparation:x"
    assert settled["publication_revision"] == 3

    # A republication of the same settled bytes changes nothing again.
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    assert pending["publication_revision"] == 3
    assert load_task_result(tmp_path, "applied")["review_projection"]["panels"] == settled["panels"]


def test_a_task_only_decision_republishes_a_panel_whose_stored_source_is_unavailable(tmp_path, monkeypatch):
    """Without a stored digest a panel cannot prove itself unchanged: it is
    published again (one more attempt at the store), never given a new identity."""
    from ouroboros.acceptance_preparation import HOST_PROCESSING_ORIGIN

    ctx, run = _context(tmp_path), _run()
    trace = {"review_runs": [run]}
    monkeypatch.setattr(artifacts, "store_actor_source_bytes",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk unavailable")))
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    assert run["publication_revision"] == 1 and run["applied_source_status"] == "unavailable"
    monkeypatch.undo()
    trace["acceptance_decision"] = {"origin": HOST_PROCESSING_ORIGIN, "status": "finalized_unaccepted"}
    review_projection.publish_acceptance_checkpoint(ctx, trace)
    panel = load_task_result(tmp_path, "applied")["review_projection"]["panels"][0]
    assert panel["publication_revision"] == 2 and panel["applied_source_status"] == "available"
    assert panel["panel_id"] == "panel_exact" and _source(tmp_path, panel)["aggregate_signal"] == "PASS"
