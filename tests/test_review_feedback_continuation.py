"""Paid criticism reaches its author without buying an extra review cycle."""
import copy
import dataclasses
import json
from types import SimpleNamespace

import pytest

from ouroboros import loop, review_substrate, task_pacing
from ouroboros.acceptance_settlement import expose_acceptance_feedback
from ouroboros.loop_acceptance import merge_agent_acceptance_stance
from ouroboros.loop_acceptance_review import _apply_task_acceptance_result
from ouroboros.review_records import ReviewRunResult
from ouroboros.task_results import effective_task_acceptance_review_cycles
from tests.test_acceptance_async_loop import ANSWER, call, full_loop, keep  # noqa: F401


@pytest.mark.parametrize("cycles,passes", [("1", 0), ("2", 1), ("unlimited", 9)])
@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
def test_last_paid_feedback_gets_an_author_response(monkeypatch, cycles, passes, enforcement):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", cycles)
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setattr(loop, "_end_task_acceptance_fence", lambda *_a, **_kw: True)
    monkeypatch.setattr(loop, "_mark_root_acceptance_checkpoint", lambda *_a, **_kw: None)
    trace, messages = {"tool_calls": []}, []
    tools_ctx = SimpleNamespace(_task_acceptance_reviewed=False)
    ctx = loop._TaskAcceptanceContext(tools=SimpleNamespace(_ctx=tools_ctx),
        content="Candidate", task_id="task", task_type="task", llm_trace=trace,
        drive_root=None, messages=messages, emit_progress=lambda *_a: None,
        mode="required", subtree_statuses=[], budget_profile={}, passes_done=passes,
        review_binding={"binding_hash": "critic-subject"})
    result = ReviewRunResult(request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
        actors=[{"slot_id": "critic", "signal": "FAIL", "parse_status": "valid",
                 "parsed": {"verdict": "FAIL", "outcome_tier": "best_effort",
                            "dialogue_status": "stable_disagreement", "completion_coach": "Correct the budget."}}],
        parsed_findings=[], aggregate_signal="FAIL")
    assert _apply_task_acceptance_result(ctx, result)
    assert "Correct the budget" in messages[-1]["content"]
    assert not trace["review_runs"][-1].get("feedback_delivered")
    expose_acceptance_feedback(trace, messages, "task")
    assert trace["review_runs"][-1]["feedback_delivered"]
    assert effective_task_acceptance_review_cycles({}) == (None if cycles == "unlimited" else int(cycles))


def test_author_work_has_no_critic_sized_time_floor(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    snapshot = task_pacing.BudgetSnapshot(has_deadline=True, remaining_sec=150, reserve_sec=120)
    assert not task_pacing.review_launch_allowed(snapshot)[0]
    assert task_pacing.improvement_pass_allowed(snapshot, 0, {"improvement_policy": "adaptive"}) == (True, "")
    assert not task_pacing.improvement_pass_allowed(snapshot, 0, {"max_improvement_passes": 0})[0]
    assert effective_task_acceptance_review_cycles({"max_improvement_passes": 6}) == 7


@pytest.mark.parametrize("feedback_kind", ["critical", "infra", "transient"])
@pytest.mark.parametrize("cap", ["1", "2", "unlimited"])
@pytest.mark.parametrize("enforcement,action", [("advisory", "finish"), ("advisory", "stop"), ("blocking", "stop")])
def test_mailbox_feedback_allows_explicit_author_choice_without_second_panel(full_loop, monkeypatch, cap, enforcement, action, feedback_kind):  # noqa: F811
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", cap)
    f.reviewer_verdict = "FAIL"
    original = review_substrate._review_route_executor
    physical_calls = []
    def executor(assignment, **kwargs):
        value = original(assignment, **kwargs)
        run = value.execute
        def execute():
            physical_calls.append({"main_step": f.model_step, "settlements_before": f.settled_count, "reconcile_only": bool(assignment.request.reconcile_only)})
            result = run()
            if feedback_kind != "critical":
                error = RuntimeError("recorded reviewer transport unavailable")
                if feedback_kind == "infra":
                    from ouroboros.usage_accounting import PhysicalAttemptCapture
                    error.status_code = 400
                    error.physical_attempt_capture = PhysicalAttemptCapture(
                        attempt_id="terminal-review", model=assignment.slot.model, provider="fixture", state="settled", candidate_measurement_kind="opaque", provider_status_code=400)
                raise error
            body = json.loads(result.raw_text)
            body["summary"] = "Correct the budget."
            raw = json.dumps(body)
            return dataclasses.replace(result, raw_text=raw, message={"content": raw})
        value.execute = execute
        return value
    monkeypatch.setattr(review_substrate, "_review_route_executor", executor)
    corrected = ANSWER + " The corrected budget is $12."
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages)); f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER, "goal": "Prepare report"}, "first")]}, 0.0
        if f.model_step == 2:
            assert f.entered.wait(5)
            f.release.set()
            with f.condition:
                assert f.condition.wait_for(lambda: f.settled_count == 1, timeout=10)
            return {"content": "", "tool_calls": [call("send_user_message", {"text": "Checking the advice"}, "progress")]}, 0.0
        if f.model_step == 3:
            assert ("Correct the budget" if feedback_kind == "critical" else "unavailable") in str(messages)
            return {"content": "", "tool_calls": [call("task_acceptance_review", {
                "claim": corrected, "goal": "Prepare report", "agent_disposition": "partial",
                "rationale": "I corrected and checked the result.", "author_action": action,
            }, "finish")]}, 0.0
        assert f.model_step < 7
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == corrected
    # The established two-send transient retry occurs inside ONE critic wave,
    # before settlement. Collection and the author's final choice never resend.
    assert len(f.review_sends) == (2 if feedback_kind == "transient" else 1), physical_calls
    assert len(set(f.review_sends)) == 1
    assert all(row["settlements_before"] == 0 and not row["reconcile_only"] for row in physical_calls)
    from ouroboros.task_results import project_task_acceptance_review_capacity
    assert project_task_acceptance_review_capacity(f.ctx, task_id=f.ctx.task_id)["claimed_cycles"] == 1
    assert trace["acceptance_decision"]["reason"] == ("review_cycles_exhausted" if action == "stop" and cap == "1" else "author_" + action)
    assert trace["review_runs"][0]["aggregate_signal"] == ("FAIL" if feedback_kind == "critical" else "DEGRADED")


def test_queued_feedback_cannot_validate_a_predeclared_finish():
    trace = {"tool_calls": [], "review_runs": [{"authority": "host_root", "binding_hash": "b"}]}
    ctx = SimpleNamespace(_owner_directives=[])
    merge_agent_acceptance_stance(trace, {"disposition": "accepted", "explicit_finish": True,
                                        "rationale": "I have not actually seen a critic yet."}, ctx)
    assert not trace["acceptance_decision"].get("agent_finish_intent")
    expose_acceptance_feedback(trace, [], "task")
    assert not trace["review_runs"][0].get("feedback_delivered")


def test_current_author_completion_does_not_rewrite_critic_or_independent_failure(monkeypatch):
    from ouroboros.outcomes import _objective_axis
    from ouroboros.project_dialogue import outcome_phase
    from ouroboros.review_records import build_author_disposition
    author = build_author_disposition(disposition="partial", rationale="Corrected and verified.", subject_hash="current", reviewer_signal="FAIL", enforcement="advisory")
    review = {"status": "fail", "acceptance_decision": {"reason": "author_finish", "author_disposition": author}}
    objective = _objective_axis(review)
    assert objective["status"] == "pass" and review["status"] == "fail"
    row = {"status": "completed", "outcome_axes": {"objective": objective, "review": review, "execution": {"status": "ok"}}}
    assert outcome_phase(row, {}) == "done"
    row["outcome_axes"]["execution"]["status"] = "failed"
    assert outcome_phase(row, {}) == "error"
    author["enforcement"] = "blocking"
    assert _objective_axis(review)["status"] != "pass"
    review["acceptance_decision"].update(status="finalized_unaccepted", reason="author_stop")
    assert _objective_axis(review)["outcome_tier"] == "blocked_with_evidence"


def test_feedback_exposure_uses_the_returned_main_request(tmp_path):
    from ouroboros.loop_llm_call import call_llm_with_retry
    from tests.test_transport_death_retry import _ScriptedLLM, OK_RESPONSE
    trace = {"review_runs": [{"authority": "host_root", "binding_hash": "reviewed"}]}
    messages = [{"role": "user", "content": "Actual paid feedback.", "review_feedback": [
        {"task_id": "task", "run_index": 0, "binding_hash": "reviewed"}]}]
    observed = []
    def observer(sent):
        observed.extend(sent)
        expose_acceptance_feedback(trace, sent, "task")
    message, _ = call_llm_with_retry(_ScriptedLLM(OK_RESPONSE), messages, "test-model", None,
        "low", 1, tmp_path / "logs", "task", 1, None, {}, model_context_observer=observer)
    assert message["content"] == "done" and observed[0]["content"] == messages[0]["content"]
    assert trace["review_runs"][0]["feedback_delivered"]


@pytest.mark.parametrize("enforcement,action", [("advisory", "finish"), ("blocking", "stop")])
def test_review_launch_floor_returns_unavailability_without_vetoing_author_time(tmp_path, monkeypatch, enforcement, action):
    from ouroboros.loop_acceptance_review import _run_task_acceptance_review_once
    from tests.test_loop_acceptance_gate import _seed_acceptance_root

    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    ctx = SimpleNamespace(_task_acceptance_reviewed=False, is_direct_chat=False, drive_root=tmp_path, _owner_directives=[])
    _seed_acceptance_root(tmp_path, "author-time", ctx)
    monkeypatch.setattr(task_pacing, "build_budget_snapshot", lambda *_a, **_kw:
        task_pacing.BudgetSnapshot(has_deadline=True, remaining_sec=150, reserve_sec=120))
    monkeypatch.setattr(loop, "_execute_task_acceptance_panel", lambda *_a, **_kw: pytest.fail("review floor must deny paid dispatch"))
    trace, messages = {"tool_calls": [{"tool": "write_file", "args": {"path": "answer.txt"}}]}, []
    def run(text):
        return _run_task_acceptance_review_once(tools=SimpleNamespace(_ctx=ctx), content=text,
            task_id="author-time", task_type="task", llm_trace=trace, drive_root=tmp_path,
            messages=messages, emit_progress=lambda *_a, **_kw: None)
    assert run("Complete answer") is True
    assert trace["acceptance_review_outcome"]["reason"] == "review_skipped_deadline_reserve"
    assert not trace.get("review_runs")
    expose_acceptance_feedback(trace, messages, "author-time")
    trace["tool_calls"].append({"tool": "task_acceptance_review", "args": {}})
    merge_agent_acceptance_stance(trace, {"disposition": "partial", "rationale": "I inspected the unavailable review outcome.",
        "author_action": action, "explicit_finish": True}, ctx)
    assert run("Corrected current answer") is False
    assert trace["acceptance_decision"]["reason"] == "author_" + action
    assert not trace.get("review_runs")
