"""Acceptance preparation through the root pass, real tool results and full loop.

The state/identity and presentation contracts are in the incident unit suite;
these scenarios exercise the actual orchestration while substituting external
model/reviewer I/O. No assertion or contract comment is removed by the split.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace as NS

import pytest

from tests._acceptance_preparation_helpers import _ctx, _expose, _fail, _raise_fingerprint, _repo
from tests.test_acceptance_async_loop import full_loop as full_loop

pytestmark = pytest.mark.serial  # Root passes and source fixtures spawn real processes.


def test_first_success_on_repaired_material_clears_the_persisted_incident(tmp_path):
    from ouroboros.acceptance_preparation import begin_preparation, close_local_preparation
    from ouroboros.review_projection import publish_acceptance_checkpoint
    from ouroboros.task_results import load_task_result

    ctx = _ctx(tmp_path)
    failed = _fail(begin_preparation(ctx.llm_trace, ctx.tools._ctx))
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    before = load_task_result(tmp_path, ctx.task_id)["review_projection"]["acceptance_incident"]
    assert before["status"] == "failed" and before["attempts"] == 1
    ctx.tools._ctx._delivery_effective_criteria = "corrected material requirements"
    repaired = begin_preparation(ctx.llm_trace, ctx.tools._ctx)
    assert repaired["incident_id"] != failed["incident_id"] and repaired["attempts"] == 0
    close_local_preparation(ctx, repaired)
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    after = load_task_result(tmp_path, ctx.task_id)["review_projection"]["acceptance_incident"]
    assert after["incident_id"] == before["incident_id"]
    assert after["attempts"] == 1 and after["status"] == "resolved"
    assert repaired["history"][-1]["status"] == "failed"  # Original failure stays history.
    assert ctx.llm_trace.get("review_runs", []) == []  # Resolution needs no paid panel.


# ── the host pass itself: identity and the repeat guard precede every fallible step ──


def _root_pass(tmp_path, monkeypatch):
    """A real root acceptance pass over a fake tool ctx (no queue, no provider)."""
    import ouroboros.review_substrate as rs
    from ouroboros import loop as loop_mod
    from ouroboros.contracts.task_contract import build_task_contract
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    task_id = "root-prep"
    contract = build_task_contract({"id": task_id, "root_task_id": task_id, "delegation_role": "root",
                                    "budget_profile": {"max_improvement_passes": 1}})
    write_task_result(tmp_path, task_id, STATUS_RUNNING, root_task_id=task_id,
                      delegation_role="root", task_contract=contract, result="Task is running.")
    ctx = NS(_task_acceptance_reviewed=False, is_direct_chat=True, drive_root=str(tmp_path),
             task_id=task_id, root_task_id=task_id, delegation_role="root", task_contract=contract,
             task_metadata={"root_task_id": task_id, "budget_drive_root": str(tmp_path)},
             _owner_directives=[{"source": "initial_user", "content": "goal"}])
    monkeypatch.setattr(loop_mod, "get_task_review_mode", lambda: "auto")
    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "blocking")
    monkeypatch.setattr(rs, "triad_delivery_slots", lambda **_kwargs: [object(), object(), object()])
    trace = {"tool_calls": [{"tool": "write_file", "args": {"path": "x.py"}}]}
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "goal"}]
    progress: list = []

    def run(content="done"):
        from ouroboros.loop_acceptance_review import _run_task_acceptance_review_once

        return _run_task_acceptance_review_once(
            tools=NS(_ctx=ctx), content=content, task_id=task_id, task_type="task",
            llm_trace=trace, drive_root=tmp_path, messages=messages,
            emit_progress=lambda text, *, incident=None: progress.append(text))

    return NS(ctx=ctx, trace=trace, messages=messages, progress=progress, run=run, task_id=task_id)


def test_the_host_pass_counts_one_attempt_then_refuses_before_any_fallible_step(tmp_path, monkeypatch):
    import ouroboros.loop_acceptance_review as review_mod
    import ouroboros.loop_delivery as delivery_mod
    import ouroboros.review_evidence as evidence_mod
    from ouroboros.acceptance_settlement import expose_acceptance_feedback

    fx = _root_pass(tmp_path, monkeypatch)
    builder_calls = []

    def _broken_builder(_ctx):
        builder_calls.append(1)
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(review_mod, "_build_host_acceptance_evidence", _broken_builder)
    assert fx.run() is True                                      # one reaction offered
    record = fx.trace["acceptance_preparation"]
    assert record["attempts"] == 1 and builder_calls == [1]
    assert fx.trace.get("review_runs", []) == []                 # no synthetic reviewer record
    assert fx.messages[-1]["review_feedback"][0]["outcome_incident_attempt"] == 1

    # The author's request came back: the incident is exposed. From now on the
    # pass must refuse BEFORE the packet budget, the subject hash or the builder.
    expose_acceptance_feedback(fx.trace, fx.messages, fx.task_id)
    subject_calls = []

    def _fallible_subject(*_a, **_k):
        subject_calls.append(1)
        raise RuntimeError("subject hash is fallible too")

    monkeypatch.setattr(delivery_mod, "delivery_subject_hash", _fallible_subject)
    monkeypatch.setattr(evidence_mod, "acceptance_packet_budget_chars",
                        lambda _slots: (_ for _ in ()).throw(RuntimeError("budget is fallible too")))
    assert fx.run() is False
    assert record["attempts"] == 1 and builder_calls == [1] and subject_calls == []
    decision = fx.trace["acceptance_decision"]
    assert decision["status"] == "finalized_unaccepted"
    assert decision["reason"] == "acceptance_preparation_failed"
    assert decision["acceptance_incident"]["attempts"] == 1
    assert fx.ctx._task_acceptance_reviewed is True


def test_the_host_pass_honours_an_informed_stop_when_even_the_fingerprint_is_broken(tmp_path, monkeypatch):
    """The paid fingerprint fails, independently of preparation material.
    The failure is counted where it happened (before the builder),
    the stance still registers through the real tool-result path, and the next
    pass honours the informed stop without touching the builder or the fingerprint."""
    import ouroboros.loop_acceptance_review as review_mod
    import ouroboros.loop_delivery as delivery_mod
    from ouroboros.acceptance_settlement import expose_acceptance_feedback
    from ouroboros.loop_tool_execution import process_tool_results

    fx = _root_pass(tmp_path, monkeypatch)
    builder_calls = []
    monkeypatch.setattr(review_mod, "_build_host_acceptance_evidence",
                        lambda _ctx: builder_calls.append(1) or (_ for _ in ()).throw(RuntimeError("builder exploded")))
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("fingerprint broken")))
    assert fx.run() is True
    record = fx.trace["acceptance_preparation"]
    assert record["source_known"] is True and record["attempts"] == 1
    assert record["failure_kind"] == "RuntimeError" and "fingerprint broken" in record["failure_detail"]
    assert builder_calls == []                               # the failure came before the builder
    expose_acceptance_feedback(fx.trace, fx.messages, fx.task_id)
    # The stance arrives through the real tool-result path while the fingerprint is still broken.
    process_tool_results(
        [{"fn_name": "task_acceptance_review", "tool_call_id": "c2", "is_error": False,
          "result": json.dumps({
              "status": "deferred_to_host_acceptance", "authoritative": False,
              "request": {"surface": "task_acceptance", "task_id": fx.task_id},
              "agent_decision": {"disposition": "partial", "explicit_finish": True,
                                 "author_action": "stop", "rationale": "stopping honestly",
                                 "source": "agent_task_acceptance_review_tool"}}),
          "args_for_log": {}, "tool_args": {}, "result_meta": {"status": "ok"}}],
        [], fx.trace, emit_progress=lambda _m, *, incident=None: None, tools=NS(_ctx=fx.ctx),
    )
    intent = fx.trace["acceptance_decision"]["agent_finish_intent"]
    assert intent["preparation_identity"] == record["source_identity"] and intent["incident_attempt"] == 1
    assert fx.run() is False
    decision = fx.trace["acceptance_decision"]
    assert decision["reason"] == "author_stop" and decision["reviewer_signal"] == ""
    assert decision["acceptance_incident"]["incident_id"] == record["incident_id"]
    assert record["attempts"] == 1 and builder_calls == []


def test_identical_write_through_real_tool_results_does_not_reopen_preparation(tmp_path, monkeypatch):
    from ouroboros.acceptance_preparation import begin_preparation, preparation_blocked
    from ouroboros.loop_tool_execution import process_tool_results

    repo = _repo(tmp_path)
    ctx = _ctx(tmp_path, repo_dir=repo)
    (repo / "src.py").write_text("x = 2\n")

    def write(call_id):
        (repo / "src.py").write_text("x = 2\n")
        process_tool_results([{
            "fn_name": "write_file", "tool_call_id": call_id, "is_error": False,
            "result": "Written src.py", "args_for_log": {"path": "src.py", "content": "x = 2\n"},
            "tool_args": {"path": "src.py", "content": "x = 2\n"}, "result_meta": {"status": "ok"},
        }], [], ctx.llm_trace, emit_progress=lambda *_a, **_k: None, tools=ctx.tools)

    write("first-write")
    ctx.tools._ctx._delivery_material_tool_indices = (0,)
    record = _expose(_fail(begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    assert record["source_known"]
    write("identical-write")
    ctx.tools._ctx._delivery_material_tool_indices = (0, 1)
    assert begin_preparation(ctx.llm_trace, ctx.tools._ctx) is record
    assert preparation_blocked(record) and record["attempts"] == 1
    (repo / "src.py").write_text("x = 3\n")
    repaired = begin_preparation(ctx.llm_trace, ctx.tools._ctx)
    assert repaired["incident_id"] != record["incident_id"] and repaired["attempts"] == 0


@pytest.mark.parametrize("action,enforcement,phase", [("finish", "advisory", "warn"), ("stop", "blocking", "error")])
@pytest.mark.parametrize("prior_signal", ["", "PASS", "FAIL"])
def test_local_decision_through_real_outcome_and_checkpoint_keeps_prior_panel(tmp_path, monkeypatch, action, enforcement, phase, prior_signal):
    from ouroboros import loop
    from ouroboros.acceptance_preparation import begin_preparation, finish_exposed_preparation_author, record_local_preparation_failure
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.project_dialogue import outcome_phase, _completion_verdict
    from ouroboros.review_projection import publish_acceptance_checkpoint

    ctx = _ctx(tmp_path)
    if prior_signal:
        ctx.llm_trace["review_runs"] = [{
            "authority": "host_root", "aggregate_signal": prior_signal, "panel_id": "prior",
            "binding_hash": "prior-binding", "paid_identity": "paid", "cost_usd": 0.42,
            "actors": [{"operation_state": "unknown", "slot_id": "prior-slot", "custody": {"token": "old"}}],
            "request": {"surface": "task_acceptance"},
            "applied_decision": {"status": "finalized_unaccepted", "reason": "old-reason"},
        }]
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    before = copy.deepcopy(ctx.llm_trace.get("review_runs", []))
    monkeypatch.setattr(loop, "get_review_enforcement", lambda: enforcement)
    record = begin_preparation(ctx.llm_trace, ctx.tools._ctx)
    assert record_local_preparation_failure(ctx, RuntimeError("local source failed")) is True
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    assert ctx.llm_trace.get("review_runs", []) == before
    _expose(record)
    merge_agent_acceptance_stance(ctx.llm_trace, {"explicit_finish": True, "author_action": action,
        "disposition": "partial", "rationale": "The available result has an unresolved acceptance gap."}, ctx.tools._ctx)
    assert finish_exposed_preparation_author(ctx, record)
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    assert ctx.llm_trace.get("review_runs", []) == before
    result = {"status": "completed", **derive_loop_outcome("Available answer.", {}, ctx.llm_trace)}
    assert outcome_phase(result, {}) == ("error" if prior_signal == "FAIL" else phase)
    axes = result["outcome_axes"]
    assert axes["execution"]["status"] == "best_effort"
    assert axes["review"]["status"] == (prior_signal.lower() if prior_signal else "skipped")
    assert "could not assemble" in _completion_verdict(result, {})
    if action == "stop":
        assert axes["objective"]["status"] == "fail"


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
def test_preparation_failure_through_outcome_preserves_forced_rail(tmp_path, monkeypatch, enforcement):
    from ouroboros import loop
    from ouroboros.acceptance_preparation import begin_preparation, record_local_preparation_failure
    from ouroboros.loop_acceptance import terminalize_dangling_revision
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.project_dialogue import outcome_phase, _completion_verdict

    ctx = _ctx(tmp_path)
    monkeypatch.setattr(loop, "get_review_enforcement", lambda: enforcement)
    begin_preparation(ctx.llm_trace, ctx.tools._ctx)
    record_local_preparation_failure(ctx, RuntimeError("source unavailable"))
    terminalize_dangling_revision(ctx.llm_trace, rail="budget_exhausted")
    result = {"status": "completed", **derive_loop_outcome("Available answer.", {
        "execution_status": "failed", "reason_code": "budget_exhausted", "_best_effort_extracted": True,
    }, ctx.llm_trace)}
    assert result["reason_code"] == "budget_exhausted"
    assert outcome_phase(result, {}) == ("error" if enforcement == "blocking" else "warn")
    verdict = _completion_verdict(result, {})
    assert "could not assemble" in verdict and "budget" in verdict


@pytest.mark.parametrize("action,enforcement", [("finish", "advisory"), ("stop", "blocking")])
def test_full_loop_nomination_to_informed_terminal_with_broken_fingerprint(full_loop, monkeypatch, action, enforcement):
    """Real tool, result processor, nomination, no-tool delivery and reducers.

    Only the external model and broken evidence source are controlled. The
    fingerprint is made unavailable AFTER the original work was retained.
    """
    from ouroboros import loop, loop_acceptance_review as review, loop_delivery as delivery
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.project_dialogue import outcome_phase
    from tests.test_acceptance_async_loop import ANSWER, call

    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    f.ctx.task_contract["budget_profile"]["max_improvement_passes"] = 1
    builders = []
    fingerprints = []

    def broken_builder(*_a, **_k):
        builders.append(1)
        raise RuntimeError("local evidence assembly failed")

    def broken_fingerprint(*_a, **_k):
        fingerprints.append(1)
        raise RuntimeError("fingerprint unavailable after feedback")

    monkeypatch.setattr(review, "_build_host_acceptance_evidence", broken_builder)

    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "nominate")]}, 0.0
        if f.model_step == 2:
            assert "could not be assembled locally" in str(messages)
            assert f.ctx._delivery_candidate.full_text == ANSWER
            monkeypatch.setattr(delivery, "delivery_evidence_fingerprint", broken_fingerprint)
            return {"content": "", "tool_calls": [call("task_acceptance_review", {
                "claim": ANSWER,
                "agent_disposition": "partial", "author_action": action,
                "rationale": "The available result has an unresolved local acceptance gap.",
                "acceptance_subject": {"owner_source_sha256": f.ctx._acceptance_observation["owner_source_sha256"]},
            }, "informed-choice")]}, 0.0
        pytest.fail("the explicit author choice must not require another finish call")

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2
    assert builders == [1] and fingerprints == []
    assert f.review_sends == [] and f.waits == []
    assert trace["acceptance_decision"]["reason"] == "author_" + action
    assert trace["acceptance_preparation"]["attempts"] == 1
    assert trace["delivery_candidate"]["evidence_current"] is False
    assert trace["delivery_candidate"]["acceptance_binding"]["authoritative"] is False
    outcome = {"status": "completed", **derive_loop_outcome(result, usage, trace)}
    assert outcome_phase(outcome, {}) == ("warn" if action == "finish" else "error")


def _delivery_ctx(tmp_path, trace):
    """A real registry and round context for the delivery leaf (as test_delivery_candidate builds them)."""
    import ouroboros.loop as loop
    from ouroboros.tools.registry import ToolRegistry

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = "parent1"
    ctx = loop._RoundLimitContext(
        [{"role": "user", "content": "task"}], NS(), "test-model", "medium", 0, tmp_path / "logs",
        "parent1", 1, None, {}, "", False, 10, drive_root=tmp_path, status_drive_root=tmp_path, root_task_id="parent1",
    )
    loop._finalize_limit_ctx(ctx, registry, trace)
    return registry, ctx


def test_the_first_answer_is_retained_over_unknown_evidence_when_the_fingerprint_is_broken(tmp_path, monkeypatch):
    """Retention never waits on the fallible evidence read: the FIRST complete answer
    is kept and published as unavailable (never as approved); an unchanged repeat under
    the same broken read stays that one candidate; a repaired read moves the answer
    onto known evidence through the ordinary replacement."""
    import ouroboros.loop as loop
    import ouroboros.loop_delivery as delivery_mod

    _raise_fingerprint(monkeypatch)
    trace = {"tool_calls": [], "reasoning_notes": []}
    registry, ctx = _delivery_ctx(tmp_path, trace)
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "first complete answer", control="candidate")
    assert registry._ctx._delivery_candidate is candidate and candidate.revision == 1
    assert candidate.evidence_fingerprint == "" and candidate.evidence_revision == 0
    assert candidate.acceptance_binding["authoritative"] is False
    assert candidate.acceptance_binding["acceptance_status"] == "unaccepted"
    published = trace["delivery_candidate"]
    assert published["content_sha256"] == candidate.content_sha256 and published["revision"] == 1
    assert published["evidence_current"] is False and published["subject_sha256"] == ""
    assert published["evidence_status"] == "unavailable_local_preparation"
    assert "acceptance_preparation" not in trace  # the host pass, not retention, accounts the failure
    again = loop._replace_delivery_candidate(registry, ctx, trace, "first complete answer", control="candidate")
    assert again is candidate and candidate.revision == 1 and trace["delivery_candidate"]["revision"] == 1
    replaced = loop._replace_delivery_candidate(registry, ctx, trace, "a different complete answer", control="candidate")
    assert replaced is not candidate and replaced.revision == 2 and replaced.evidence_fingerprint == ""
    assert trace["delivery_candidate"]["evidence_status"] == "unavailable_local_preparation"
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", lambda *_a, **_k: "repaired-fingerprint")
    repaired = loop._replace_delivery_candidate(registry, ctx, trace, "a different complete answer", control="candidate")
    assert repaired is not replaced and repaired.revision == 3
    assert repaired.evidence_fingerprint == "repaired-fingerprint" and repaired.evidence_revision == 1
    assert trace["delivery_candidate"]["evidence_current"] is True and trace["delivery_candidate"]["subject_sha256"]
    assert "evidence_status" not in trace["delivery_candidate"]


def test_a_failed_subject_publication_keeps_the_retained_answer_unapproved(tmp_path, monkeypatch):
    """The subject hash is computed at publication, outside the informed-choice
    guard: its failure must publish the retained answer as unavailable, not crash
    after the answer was already retained and not claim currency."""
    import ouroboros.loop as loop
    import ouroboros.loop_delivery as delivery_mod

    monkeypatch.setattr(delivery_mod, "delivery_subject_hash",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("subject hash is fallible")))
    trace = {"tool_calls": [], "reasoning_notes": []}
    registry, ctx = _delivery_ctx(tmp_path, trace)
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "first complete answer", control="candidate")
    assert registry._ctx._delivery_candidate is candidate and candidate.evidence_fingerprint  # the fingerprint itself worked
    published = trace["delivery_candidate"]
    assert published["content_sha256"] == candidate.content_sha256
    assert published["subject_sha256"] == "" and published["evidence_status"] == "unavailable_local_preparation"
    assert published["evidence_current"] is False and published["acceptance_binding"]["authoritative"] is False
    monkeypatch.undo()
    loop._publish_delivery_candidate(registry, candidate, trace)
    assert trace["delivery_candidate"]["subject_sha256"] and trace["delivery_candidate"]["evidence_current"] is True
    assert "evidence_status" not in trace["delivery_candidate"]


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
def test_full_ordinary_loop_first_answer_is_retained_when_the_fingerprint_is_broken_from_the_start(full_loop, monkeypatch, enforcement):
    """The evidence fingerprint is broken BEFORE any answer exists (the existing
    regression breaks it only after the first answer was retained). The first plain
    answer is still retained over unknown evidence and published as such, the host
    pass accounts the failure as its local preparation incident without reaching the
    builder, and the loop ends honestly after Main selects the retained answer:
    Advisory keeps the caveat and Blocking stops unfinished — no
    reviewer, no second host attempt, no replay of the broken read."""
    from ouroboros import loop, loop_acceptance_review as review, loop_delivery as delivery
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.project_dialogue import outcome_phase
    from tests.test_acceptance_async_loop import ANSWER, keep

    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    f.ctx.task_contract["budget_profile"]["max_improvement_passes"] = 1
    builders, fingerprints = [], []

    def broken_builder(*_a, **_k):
        builders.append(1)
        raise RuntimeError("the builder must not be reached")

    def broken_fingerprint(*_a, **_k):
        fingerprints.append(1)
        raise RuntimeError("fingerprint unavailable before the first answer")

    monkeypatch.setattr(review, "_build_host_acceptance_evidence", broken_builder)
    monkeypatch.setattr(delivery, "delivery_evidence_fingerprint", broken_fingerprint)

    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 2:
            assert "could not be assembled locally" in str(messages)
            assert f.ctx._delivery_candidate.full_text == ANSWER and f.ctx._delivery_candidate.revision == 1
            return keep(f), 0.0
        else:
            assert f.model_step == 1, f.progress
        return {"content": ANSWER}, 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, usage, trace = f.run()
    assert result == ANSWER and builders == [] and fingerprints
    assert f.model_step == 2  # Both modes expose one meaningful author reaction.
    assert not f.review_sends and not f.waits and not trace.get("review_runs")
    record = trace["acceptance_preparation"]
    assert record["attempts"] == 1 and record["stage"] == "preparation"
    assert record["failure_kind"] == "RuntimeError" and "fingerprint unavailable" in record["failure_detail"]
    assert trace["acceptance_decision"]["reason"] == "acceptance_preparation_failed"
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
    published = trace["delivery_candidate"]
    assert published["revision"] == 1 and published["evidence_fingerprint"] == ""
    assert published["evidence_current"] is False and published["subject_sha256"] == ""
    assert published["evidence_status"] == "unavailable_local_preparation"
    assert published["acceptance_binding"]["authoritative"] is False
    outcome = {"status": "completed", **derive_loop_outcome(result, usage, trace)}
    assert outcome_phase(outcome, {}) == ("error" if enforcement == "blocking" else "warn")
    objective = outcome["outcome_axes"]["objective"]
    assert objective["status"] == ("fail" if enforcement == "blocking" else "best_effort")
    assert objective["outcome_tier"] == ("blocked_with_evidence" if enforcement == "blocking" else "best_effort")


@pytest.mark.parametrize("change", ["owner", "material", "ordinary", "blocking_finish", "old_attempt"])
def test_local_fingerprint_escape_remains_bound_to_informed_choice(tmp_path, monkeypatch, change):
    from ouroboros import loop
    from ouroboros.acceptance_preparation import begin_preparation, preparation_delivery_choice
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance
    from ouroboros.loop_messages import _record_owner_directive, owner_source_sha256
    from ouroboros.loop_delivery import DeliveryCandidate

    ctx = _ctx(tmp_path)
    tool_ctx = ctx.tools._ctx
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setattr(loop, "get_review_enforcement", lambda: "advisory")
    tool_ctx._delivery_candidate = DeliveryCandidate("saved answer", "saved-hash", 1, 1, "prior-fp", {})
    tool_ctx._delivery_candidate.owner_source_sha256 = owner_source_sha256(tool_ctx)
    record = _expose(_fail(begin_preparation(ctx.llm_trace, tool_ctx)))
    merge_agent_acceptance_stance(ctx.llm_trace, {"explicit_finish": True, "author_action": "finish",
        "disposition": "partial", "rationale": "caveat"}, tool_ctx)
    assert preparation_delivery_choice(tool_ctx, ctx.llm_trace)
    if change == "owner":
        _record_owner_directive(tool_ctx, source="owner_followup", content="add a table")
    elif change == "material":
        tool_ctx._delivery_effective_criteria = "changed requirements"
    elif change == "ordinary":
        merge_agent_acceptance_stance(ctx.llm_trace, {"explicit_finish": False}, tool_ctx)
    elif change == "blocking_finish":
        monkeypatch.setattr(loop, "get_review_enforcement", lambda: "blocking")
    else:
        _expose(_fail(record))
    assert not preparation_delivery_choice(tool_ctx, ctx.llm_trace)


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
@pytest.mark.parametrize("reaction_allowed", [False, True])
def test_full_loop_selected_finish_after_preparation_failure_is_unfinished_in_blocking(
    full_loop, monkeypatch, enforcement, reaction_allowed,
):
    from ouroboros import loop, loop_acceptance_review as review
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.project_dialogue import outcome_phase
    from tests.test_acceptance_async_loop import ANSWER, keep

    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    f.ctx.task_contract["budget_profile"]["max_improvement_passes"] = int(reaction_allowed)
    builders = []

    def broken(*_a, **_k):
        builders.append(1)
        raise RuntimeError("local preparation failed")

    monkeypatch.setattr(review, "_build_host_acceptance_evidence", broken)

    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": ANSWER}, 0.0
        assert reaction_allowed and f.model_step == 2
        assert "could not be assembled locally" in str(messages)
        return keep(f), 0.0  # Select the retained bytes without claiming review approval.

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, usage, trace = f.run()
    assert result == ANSWER and builders == [1]
    assert f.model_step == 1 + int(reaction_allowed)
    assert not f.review_sends and not f.waits and not trace.get("review_runs")
    assert trace["acceptance_decision"]["reason"] == "acceptance_preparation_failed"
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
    outcome = {"status": "completed", **derive_loop_outcome(result, usage, trace)}
    assert outcome_phase(outcome, {}) == ("error" if enforcement == "blocking" else "warn")
    objective = outcome["outcome_axes"]["objective"]
    assert objective["status"] == ("fail" if enforcement == "blocking" else "best_effort")
    assert objective["outcome_tier"] == ("blocked_with_evidence" if enforcement == "blocking" else "best_effort")


def test_full_loop_retry_rewording_and_basis_change_do_not_buy_another_attempt(full_loop, monkeypatch):
    from ouroboros import loop, loop_acceptance_review as review
    from tests.test_acceptance_async_loop import ANSWER, call, keep

    f = full_loop
    f.ctx.task_contract["budget_profile"]["max_improvement_passes"] = 3
    builders = []

    def broken(*_a, **_k):
        builders.append(1)
        raise RuntimeError("same source is still unavailable")

    monkeypatch.setattr(review, "_build_host_acceptance_evidence", broken)

    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "nominate")]}, 0.0
        if f.model_step in {2, 3}:
            record = f.ctx._execution_trace["acceptance_preparation"]
            assert record["attempts"] == f.model_step - 1
            return {"content": "", "tool_calls": [call("task_acceptance_review", {
                "claim": ANSWER,
                "acceptance_retry": {"incident_id": record["incident_id"],
                    "basis": "owner_retry" if f.model_step == 2 else "material_change",
                    "rationale": "the owner asked" if f.model_step == 2 else "a different explanation",
                    "owner_source_sha256": f.ctx._acceptance_observation["owner_source_sha256"]},
            }, f"retry-{f.model_step}")]}, 0.0
        assert f.model_step == 4
        return keep(f), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 4 and builders == [1, 1]
    assert trace["acceptance_preparation"]["attempts"] == 2
    assert len(trace["acceptance_preparation"]["retry_keys"]) == 1
    assert not trace.get("review_runs") and not f.review_sends


@pytest.mark.parametrize("seam", ["paid_identity", "prior_lookup", "capacity", "request"])
def test_binding_and_admission_failures_are_local_before_dispatch(tmp_path, monkeypatch, seam):
    from ouroboros import loop_acceptance_review as review, review_substrate, review_evidence, task_results
    from ouroboros.acceptance_settlement import expose_acceptance_feedback

    fx = _root_pass(tmp_path, monkeypatch)
    monkeypatch.setattr(review, "_build_host_acceptance_evidence", lambda _ctx: {"evidence": "ready"})
    monkeypatch.setattr(review_evidence, "acceptance_packet_budget_chars", lambda *_: 100_000)
    monkeypatch.setattr("ouroboros.review_dispatch.reconcile_pending_acceptance_runs", lambda *_a, **_k: None)
    fx.trace["review_runs"] = [{"authority": "host_root", "binding_hash": "old", "paid_identity": "old",
        "request": {"surface": "task_acceptance"}, "aggregate_signal": "PASS", "cost_usd": 0.25,
        "actors": [{"operation_state": "unknown", "custody": {"token": "old"}}]}]
    from ouroboros.review_projection import publish_acceptance_checkpoint
    publish_acceptance_checkpoint(fx.ctx, fx.trace)
    before = copy.deepcopy(fx.trace["review_runs"])

    def broken(*_a, **_k):
        raise RuntimeError("local " + seam + " failure")

    owner, name = {"paid_identity": (review, "bind_acceptance_paid_identity"),
                   "prior_lookup": (review, "_prior_acceptance_run"),
                   "capacity": (task_results, "project_task_acceptance_review_capacity"),
                   "request": (review_substrate, "ReviewRequest")}[seam]
    monkeypatch.setattr(owner, name, broken)
    assert fx.run() is True
    assert fx.trace["review_runs"] == before
    record = fx.trace["acceptance_preparation"]
    assert record["stage"] == "preparation" and record["attempts"] == 1
    assert not getattr(fx.ctx, "_task_acceptance_seen_bindings", {})
    expose_acceptance_feedback(fx.trace, fx.messages, fx.task_id)
    assert fx.run() is False
    assert record["attempts"] == 1 and fx.trace["review_runs"] == before


@pytest.mark.parametrize("seam", ["subject_hash", "delivery_choice"])
def test_already_reviewed_early_read_failure_enters_local_guard(tmp_path, monkeypatch, seam):
    """Both fallible early reads of an already-reviewed subject — the informed
    delivery choice and the subject hash — land under the local incident guard;
    neither may silently deliver under the old verdict or escape accounting."""
    from ouroboros import acceptance_preparation as prep, loop_delivery as delivery
    from ouroboros.acceptance_settlement import expose_acceptance_feedback

    fx = _root_pass(tmp_path, monkeypatch)
    fx.ctx._task_acceptance_reviewed = True
    fx.ctx._task_acceptance_reviewed_subject = "old-subject"
    calls = []

    def broken(*_a, **_k):
        calls.append(1)
        raise RuntimeError(seam + " unreadable")

    if seam == "subject_hash":
        monkeypatch.setattr(delivery, "delivery_subject_hash", broken)
    else:
        monkeypatch.setattr(prep, "preparation_delivery_choice", broken)
    assert fx.run() is True
    assert fx.trace["acceptance_preparation"]["attempts"] == 1 and calls == [1]
    assert not fx.trace.get("review_runs")
    expose_acceptance_feedback(fx.trace, fx.messages, fx.task_id)
    assert fx.run() is False
    assert fx.trace["acceptance_preparation"]["attempts"] == 1 and calls == [1]


def test_different_pending_panel_subject_failure_stays_with_its_custody(tmp_path, monkeypatch):
    from ouroboros import loop_acceptance_review as review, loop_delivery as delivery, review_evidence

    fx = _root_pass(tmp_path, monkeypatch)
    monkeypatch.setattr(review, "_build_host_acceptance_evidence", lambda _ctx: {"evidence": "ready"})
    monkeypatch.setattr(review_evidence, "acceptance_packet_budget_chars", lambda *_: 100_000)
    monkeypatch.setattr("ouroboros.review_dispatch.reconcile_pending_acceptance_runs", lambda *_a, **_k: None)
    fx.ctx._task_acceptance_pending = "old"
    fx.trace["review_runs"] = [{"authority": "host_root", "binding_hash": "old", "paid_identity": "old",
        "request": {"surface": "task_acceptance", "subject": "old answer"}, "aggregate_signal": "DEGRADED",
        "actors": [{"operation_state": "pending_dispatch", "custody": {"token": "original"}}]}]
    before = copy.deepcopy(fx.trace["review_runs"])
    original = delivery.delivery_subject_hash

    def subject(ctx, trace, content):
        if content == "old answer":
            raise RuntimeError("pending panel subject unavailable")
        return original(ctx, trace, content)

    monkeypatch.setattr(delivery, "delivery_subject_hash", subject)
    assert fx.run() is True
    assert fx.trace["review_runs"] == before and fx.ctx._task_acceptance_pending == "old"
    assert fx.trace["review_decision"]["host_failure"]["stage"] == "reconcile"
    assert fx.trace["acceptance_preparation"]["attempts"] == 0


@pytest.mark.parametrize("stage", ["reconcile", "dispatch", "application"])
@pytest.mark.parametrize("state", ["pending_dispatch", "unknown", "settled"])
def test_host_processing_failure_keeps_real_panel_custody_without_forged_findings(tmp_path, stage, state):
    from ouroboros.loop_acceptance_review import _record_acceptance_infra_failure
    from ouroboros.review_projection import publish_acceptance_checkpoint

    ctx = _ctx(tmp_path)
    ctx.stage = stage
    ctx.llm_trace["review_runs"] = [{"authority": "host_root", "binding_hash": "old", "panel_id": "paid",
        "request": {"surface": "task_acceptance"}, "aggregate_signal": "PASS", "cost_usd": 0.25,
        "parsed_findings": [{"item": "actual reviewer finding"}],
        "actors": [{"operation_state": state, "custody": {"token": "original"}}]}]
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    before = copy.deepcopy(ctx.llm_trace["review_runs"])
    _record_acceptance_infra_failure(ctx, RuntimeError("host operation failed"))
    publish_acceptance_checkpoint(ctx.tools._ctx, ctx.llm_trace)
    assert ctx.llm_trace["review_runs"] == before
    assert ctx.llm_trace["review_decision"]["host_failure"]["stage"] == stage
    assert "acceptance_preparation" not in ctx.llm_trace
    assert ctx.llm_trace["acceptance_decision"]["origin"] == "host_acceptance_processing"


# ── the ONE typed evidence read: no path fails after an answer was retained ──


def test_the_evidence_state_types_a_broken_read_as_unknown_without_moving_known_state(tmp_path, monkeypatch):
    """`_delivery_evidence_state` owns the fallible read: a broken fingerprint is
    UNKNOWN (revision unchanged, last KNOWN fingerprint kept, nothing superseded),
    never an exception — and a repaired read continues from the KNOWN state, so the
    outage itself is never counted as a change."""
    import ouroboros.loop as loop
    import ouroboros.loop_delivery as delivery_mod

    trace = {"tool_calls": [], "reasoning_notes": []}
    registry, ctx = _delivery_ctx(tmp_path, trace)
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", lambda *_a, **_k: "known-1")
    assert loop._delivery_evidence_state(registry, ctx, trace) == (1, "known-1")
    _raise_fingerprint(monkeypatch)
    assert loop._delivery_evidence_state(registry, ctx, trace) == (1, "")
    assert registry._ctx._delivery_evidence_fingerprint == "known-1"
    assert registry._ctx._delivery_evidence_revision == 1
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", lambda *_a, **_k: "known-1")
    assert loop._delivery_evidence_state(registry, ctx, trace) == (1, "known-1")   # the outage was not a change
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", lambda *_a, **_k: "known-2")
    assert loop._delivery_evidence_state(registry, ctx, trace) == (2, "known-2")


@pytest.mark.parametrize("path", ["arm", "post_tool", "selection", "stance"])
def test_every_retained_answer_path_survives_a_broken_read_and_claims_nothing(tmp_path, monkeypatch, path):
    """Retention was guarded, but arming a control round, the post-tool budget
    context, an answer selection and a stance merge still reached the broken read
    unguarded AFTER the answer was retained. Each now types it unknown: the answer
    stays, is published as unavailable (never current, never bound), and explicit
    selection preserves the same bytes over unknown evidence without borrowing
    the old candidate's verified subject."""
    import ouroboros.loop as loop
    import ouroboros.loop_delivery as delivery_mod
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance
    from ouroboros.tools.control_runtime import _finish_task

    trace = {"tool_calls": [], "reasoning_notes": []}
    registry, ctx = _delivery_ctx(tmp_path, trace)
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", lambda *_a, **_k: "known-1")
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "complete answer", control="candidate")
    assert candidate.evidence_fingerprint == "known-1" and trace["delivery_candidate"]["evidence_current"] is True
    _raise_fingerprint(monkeypatch)
    transcript = lambda: "\n".join(str(row.get("content") or "") for row in ctx.messages)  # noqa: E731
    if path == "arm":
        loop._arm_delivery_control(registry, ctx, trace)
        assert candidate.content_sha256 in transcript() and "select complete answer bytes or this answer_sha256" in transcript()
        assert candidate.finalization_control == "awaiting_control"
    elif path == "post_tool":
        loop._prepare_post_tool_budget_context(registry, ctx, trace, "test-model", False, "medium")
        assert candidate.finalization_control == "effect_revision_required"
        assert "select complete answer bytes or this answer_sha256" in transcript()
    elif path == "selection":
        registry._ctx._delivery_control_required = True
        candidate.finalization_control = "awaiting_control"
        registry._ctx._completion_observation = delivery_mod.completion_observation(registry._ctx, trace)
        payload = json.loads(_finish_task(registry._ctx, action="finish", answer_sha256=candidate.content_sha256))
        assert payload["status"] == "completion_requested"
        assert delivery_mod.consume_completion_request(registry, ctx, trace)
        assert registry._ctx._delivery_candidate.full_text == candidate.full_text
        assert registry._ctx._completion_selected["observation"]["evidence_fingerprint"] == ""
    else:
        merge_agent_acceptance_stance(trace, {"explicit_finish": True, "author_action": "stop",
                                              "disposition": "partial", "rationale": "stopping"}, registry._ctx)
        assert trace["acceptance_decision"]["agent_finish_intent"]["evidence_fingerprint"] == ""
        loop._publish_delivery_candidate(registry, candidate, trace)   # the publication path, same read
    published = trace["delivery_candidate"]
    assert published["evidence_current"] is False and published["subject_sha256"] == ""
    assert published["evidence_status"] == "unavailable_local_preparation"
    assert published["acceptance_binding"]["authoritative"] is False
    assert candidate.evidence_fingerprint == "known-1" and registry._ctx._delivery_evidence_revision == 1
    assert "acceptance_preparation" not in trace   # the host pass, not the read, accounts the incident
    restated = loop._replace_delivery_candidate(registry, ctx, trace, "complete answer", control="replace")
    assert restated is not candidate and restated.revision == 2 and restated.evidence_fingerprint == ""
    assert restated.acceptance_binding["authoritative"] is False
    assert loop._replace_delivery_candidate(registry, ctx, trace, "complete answer", control="candidate") is restated
    loop._arm_delivery_control(registry, ctx, trace)
    assert restated.content_sha256 in transcript() and "action=stop with a rationale" in transcript()
    loop._prepare_post_tool_budget_context(registry, ctx, trace, "test-model", False, "medium")
    assert restated.finalization_control == "awaiting_control"   # unchanged unknown evidence arms nothing new
    assert trace["delivery_candidate"]["evidence_status"] == "unavailable_local_preparation"


def test_a_forced_exit_over_an_unreadable_fingerprint_preserves_the_answer_as_unverified(tmp_path, monkeypatch):
    """A KNOWN candidate under a read the host cannot complete is never current
    (its approval cannot be verified), so the forced rail preserves the text
    unaccepted — and its notice says the evidence could not be read, not that
    newer evidence arrived."""
    import ouroboros.loop as loop
    import ouroboros.loop_delivery as delivery_mod

    trace = {"tool_calls": [], "reasoning_notes": []}
    registry, ctx = _delivery_ctx(tmp_path, trace)
    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", lambda *_a, **_k: "known-1")
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "complete answer", control="candidate")
    _raise_fingerprint(monkeypatch)
    assert loop._current_delivery_candidate(ctx, trace) is None
    preserved = loop._publish_stale_forced_candidate(ctx, trace, candidate, "budget_exhausted", "")
    notice = str(ctx.accumulated_usage["terminal_host_notice"])
    assert "STALE-EVIDENCE NOTICE" in notice and "could no longer read" in notice
    assert "newer task evidence" not in notice
    assert preserved.acceptance_binding["authoritative"] is False
    assert preserved.acceptance_binding["stale_evidence"] is True
    assert trace["delivery_candidate"]["evidence_current"] is False


@pytest.mark.parametrize("criteria", ["original requirements", "changed requirements"])
def test_forced_selection_cannot_rebind_authoritative_approval_to_unknown(tmp_path, monkeypatch, criteria):
    """An acknowledged subject on forced selection must not borrow the earlier PASS
    when the fingerprint is unreadable, even with unchanged answer bytes."""
    from ouroboros import loop, loop_delivery as delivery
    from ouroboros.loop_forced_finalization import _resolve_forced_delivery_control
    import ouroboros.loop_acceptance as acceptance

    trace = {"tool_calls": [], "reasoning_notes": []}
    registry, ctx = _delivery_ctx(tmp_path, trace)
    registry._ctx._delivery_effective_criteria = "original requirements"
    monkeypatch.setattr(delivery, "delivery_evidence_fingerprint", lambda *_a, **_k: "known")
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "complete answer", control="candidate")
    binding = {"authoritative": True, "acceptance_status": "accepted", "panel_id": "paid", "binding_hash": "paid-binding"}
    candidate.acceptance_binding = dict(binding)
    trace["review_runs"] = [{"authority": "host_root", "candidate_hash": candidate.content_sha256,
        "panel_id": "paid", "binding_hash": "paid-binding", "aggregate_signal": "PASS"}]
    trace["review_decision"] = dict(binding)
    registry._ctx._task_acceptance_reviewed = True
    registry._ctx._delivery_control_required = True
    monkeypatch.setattr(acceptance, "acknowledge_acceptance_observation", lambda *_a, **_k: True)
    _raise_fingerprint(monkeypatch)
    control = json.dumps({"action": "finish", "answer_sha256": candidate.content_sha256, "acceptance_subject": {
        "owner_source_sha256": "observed-owner", "effective_criteria": criteria}})
    text, reason, retained, replaced = _resolve_forced_delivery_control(registry._ctx, control, ctx=ctx, llm_trace=trace)
    assert text == "complete answer" and retained and not replaced and not reason
    assert candidate.evidence_fingerprint == ""
    assert candidate.acceptance_binding["authoritative"] is False
    assert trace["review_runs"][0]["aggregate_signal"] == "PASS"  # history not rewritten
    assert trace["review_runs"][0]["superseded_by_revision"] is True
    assert trace["delivery_candidate"]["evidence_current"] is False
    # Defence at the current-candidate reader, even against a malformed binding.
    candidate.acceptance_binding = dict(binding)
    assert loop._current_delivery_candidate(ctx, trace) is None
