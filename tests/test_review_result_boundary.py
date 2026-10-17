"""#1602: a review result is a producer-owned review structure, never a tool name.

The real completion producer (``_handle_task_acceptance_review``), the real
capture (``process_tool_results``) and the real downstream consumers (review
axis, verification ledger, panel projection, host eligibility) are exercised
with no reviewer, model, runtime or paid panel. The control replies that are
not reviews are the exact producer shapes; the historical cap amendment and
panel summary are synthetic copies of the producer's key set (no live cap or
historical panel is touched here).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros.loop_acceptance import _task_acceptance_eligible
from ouroboros.loop_tool_execution import process_tool_results
from ouroboros.outcomes import _review_axis, build_verification_ledger
from ouroboros.review_projection import compact_review_projection

STOP_CLAIM = "Incomplete result retained."
STOP_RATIONALE = "The requested work is unfinished."


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")


def _ctx():
    return SimpleNamespace(task_id="boundary-root", task_metadata={}, task_contract={})


def _capture(result, *, is_error=False, args=None, trace=None):
    trace = trace if trace is not None else {"tool_calls": [], "reasoning_notes": []}
    args = args or {}
    process_tool_results([{
        "fn_name": "task_acceptance_review", "tool_call_id": f"call-{len(trace['tool_calls']) + 1}",
        "result": result, "is_error": is_error, "args_for_log": args, "tool_args": args,
        "result_meta": {"status": "error" if is_error else "ok"},
    }], [], trace, emit_progress=lambda *_a, **_k: None)
    return trace


def _consumers(trace):
    """Every consumer that turned the acknowledgement into a false review."""
    axis = _review_axis(trace)
    ledger = build_verification_ledger(task={"id": "boundary-root"}, loop_outcome={"outcome_axes": {}},
                                       llm_trace=trace, artifact_bundle={})
    review_rows = [row for row in ledger["entries"] if row.get("kind") == "task_acceptance_review"]
    panels = compact_review_projection(trace.get("review_runs"))["panels"]
    return axis, review_rows, panels


def _real_result(*, actors, aggregate_signal="PASS", degraded=False, degraded_reasons=()):
    from ouroboros.review_substrate import ReviewRunResult

    result = ReviewRunResult(
        request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
        actors=list(actors), parsed_findings=[], aggregate_signal=aggregate_signal,
        degraded=degraded, degraded_reasons=list(degraded_reasons),
    )
    payload = {**dict(result.__dict__), "dissent_noted": False}
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _historical_amendment():
    """``owner_historical_acceptance`` on ``amend_cap``: its exact key set, synthetic values."""
    return json.dumps({
        "status": "amended", "reason": "original_root_cap_amended", "action": "amend_cap",
        "dispatched": False, "task_id": "older-root", "debt_id": "acceptance-late:0" * 8,
        "source_ref": {"path": "frozen.json"}, "owner_source_ref": {"path": "owner.json"},
        "cap_amendment": {"new_cap_usd": 9.0}, "execution_blocked_by": [],
        "effective_original_root_cap": {"state": "finite", "usd": 9.0, "source": "owner_amendment"},
        "delivery_status": "unconfirmed", "review_requested": False,
    })


def _historical_summary():
    """``_run_historical_acceptance`` after a historical panel: a summary, not that panel."""
    return json.dumps({
        "status": "published", "task_id": "older-root", "debt_id": "acceptance-late:1" * 8,
        "paid_identity": "f" * 64, "panel_id": "panel_0123456789abcdef", "dispatched": True,
        "delivery_status": "delivered", "source_ref": {"path": "applied.json"},
    })


def test_a_stop_acknowledgement_is_recorded_as_control_not_as_a_review():
    """The incident: ``completion_requested`` became one degraded run, an ``ok``
    verification row and a panel with no reviewer."""
    from ouroboros.tools.review import _handle_task_acceptance_review

    args = {"claim": STOP_CLAIM, "author_action": "stop", "rationale": STOP_RATIONALE}
    ack = _handle_task_acceptance_review(_ctx(), **args)
    assert json.loads(ack) == {"status": "completion_requested", "completion_control": True, "action": "stop"}
    trace = _capture(ack, args=args)
    assert trace["tool_calls"][0]["completion_control"] is True  # the control fact stays on the call
    assert "review_runs" not in trace and "acceptance_evidence_calls" not in trace
    axis, review_rows, panels = _consumers(trace)
    assert axis["status"] == "skipped" and axis["run_count"] == 0
    assert review_rows == [] and panels == []
    from ouroboros.loop_acceptance import recorded_acceptance_nomination

    assert not recorded_acceptance_nomination(trace)
    assert _task_acceptance_eligible("auto", trace, False) == (False, "skipped_conversation")


def test_a_finish_acknowledgement_is_not_a_review_either():
    from ouroboros.tools.review import _handle_task_acceptance_review

    args = {"claim": "The export endpoint ships.", "agent_disposition": "accepted",
            "author_action": "finish", "rationale": "Everything verified."}
    ack = _handle_task_acceptance_review(_ctx(), **args)
    assert json.loads(ack)["status"] == "completion_requested"
    trace = _capture(ack, args=args)
    assert "review_runs" not in trace
    assert _task_acceptance_eligible("auto", trace, False) == (False, "skipped_conversation")
    # The configured policy still reviews its own triggers beside the acknowledgement:
    # Required reviews observed effects and produced material. Queue membership and
    # declared outputs prove no production (#1731), so they stay quiet.
    assert _task_acceptance_eligible("required", trace, False) == (False, "skipped_conversation")
    assert _task_acceptance_eligible("auto", trace, False, task_contract={"expected_output": "x"}) == (False, "skipped_conversation")
    effect = {"tool": "write_file", "args": {"path": "export.py"}, "is_error": False, "status": "ok"}
    assert _task_acceptance_eligible("required", {**trace, "tool_calls": [*trace["tool_calls"], effect]}, False) == (True, "required_effect")
    assert _task_acceptance_eligible("required", {**trace, "acceptance_material": {"produced": True}}, False) == (True, "required_material")


@pytest.mark.parametrize("control", ["amend_cap", "historical_summary", "no_review_slots",
                                     "invalid_slot_config", "child_selection_required"])
def test_other_control_replies_of_the_same_tool_never_become_reviews(control):
    if control == "amend_cap":
        reply = _historical_amendment()
    elif control == "historical_summary":
        reply = _historical_summary()
    elif control == "no_review_slots":
        reply = json.dumps({"status": "not_dispatched", "reason": "no_review_slots",
                            "detail": "no review pool row is configured; no reviewer was called"})
    elif control == "invalid_slot_config":
        reply = json.dumps({"status": "not_dispatched",
                            "error": "invalid reviewer-slot configuration blocks task acceptance: bad row"})
    else:
        from ouroboros.reviewer_slot_config import child_acceptance_slots

        rows = [SimpleNamespace(slot_id="a", model="m-a", route="api_chat"),
                SimpleNamespace(slot_id="b", model="m-b", route="api_chat")]
        slots, refusal = child_acceptance_slots(rows, "")
        assert slots == [] and refusal["reason"] == "reviewer_selection_required"
        reply = json.dumps(refusal)
    trace = _capture(reply)
    assert "review_runs" not in trace, control
    axis, review_rows, panels = _consumers(trace)
    assert axis["run_count"] == 0 and review_rows == [] and panels == []


def test_a_failed_tool_call_is_not_a_review():
    trace = _capture("ERROR: TOOL_ARG_ERROR: author_action requires finish|stop and a rationale.", is_error=True)
    assert "review_runs" not in trace
    assert trace["tool_calls"][0]["is_error"] is True


def test_a_root_nomination_is_evidence_and_the_only_agent_trigger():
    """The deferral record, not the tool name, nominates the result."""
    from ouroboros.loop_acceptance import recorded_acceptance_nomination
    from ouroboros.tools.review import _handle_task_acceptance_review

    args = {"claim": "Sources disagree about the date.", "goal": "Compare the sources."}
    deferred = _handle_task_acceptance_review(_ctx(), **args)
    assert json.loads(deferred)["status"] == "deferred_to_host_acceptance"
    trace = _capture(deferred, args=args)
    assert "review_runs" not in trace
    assert trace["acceptance_evidence_calls"][0]["authoritative"] is False
    assert recorded_acceptance_nomination(trace)
    assert _task_acceptance_eligible("auto", trace, False) == (True, "auto_agent_request")
    assert _task_acceptance_eligible("auto", trace, True) == (True, "auto_agent_request")
    axis, review_rows, panels = _consumers(trace)
    assert axis["run_count"] == 0 and review_rows == [] and panels == []
    # A bare tool-call row (no recorded deferral) is not a nomination.
    bare = {"tool_calls": [{"tool": "task_acceptance_review", "is_error": False, "status": "ok", "args": {}}]}
    assert not recorded_acceptance_nomination(bare)
    assert _task_acceptance_eligible("auto", bare, False) == (False, "skipped_conversation")


@pytest.mark.parametrize("shape", ["pass", "fail", "zero_actor_refusal", "pending", "malformed", "transport_error", "capsule"])
def test_real_review_structures_keep_their_verdict_semantics(shape):
    if shape == "pass":
        reply = _real_result(actors=[{"slot_id": "a", "status": "ok", "signal": "PASS", "parsed": {"verdict": "PASS"}}])
    elif shape == "fail":
        reply = _real_result(actors=[{"slot_id": "a", "status": "ok", "signal": "FAIL", "parsed": {"verdict": "FAIL"}}],
                             aggregate_signal="FAIL")
    elif shape == "zero_actor_refusal":
        # A real run object whose every row was refused before dispatch: still the producer's structure.
        reply = _real_result(actors=[], aggregate_signal="DEGRADED", degraded=True,
                             degraded_reasons=["no valid reviewer quorum"])
    elif shape in {"malformed", "transport_error"}:
        # Dispatched reviews remain real runs even when no valid verdict arrives.
        actor = {"slot_id": "a", "status": "error", "operation_state": "completed",
                 "transport_status": "ok" if shape == "malformed" else "error",
                 "parse_status": "malformed" if shape == "malformed" else "not_parsed",
                 "error": "Invalid review JSON" if shape == "malformed" else "Provider unavailable"}
        reply = _real_result(actors=[actor], aggregate_signal="DEGRADED", degraded=True,
                             degraded_reasons=[actor["error"]])
    elif shape == "pending":
        reply = _real_result(actors=[{"slot_id": "a", "status": "pending", "operation_state": "pending_dispatch",
                                      "transport_status": "awaiting"}], aggregate_signal="", degraded=True)
    else:
        payload = _real_result(actors=[{"slot_id": "a", "status": "ok", "signal": "PASS", "parsed": {"verdict": "PASS"}}])
        reply = "Improvement capsule: nothing to fix.\n\n<full_review>\n" + payload + "\n</full_review>"
    trace = _capture(reply)
    assert len(trace["review_runs"]) == 1
    assert isinstance(trace["review_runs"][0]["actors"], list)
    axis, review_rows, panels = _consumers(trace)
    assert axis["run_count"] == 1 and len(review_rows) == 1 and len(panels) == 1
    expected = {"pass": "pass", "fail": "fail", "zero_actor_refusal": "degraded", "malformed": "degraded", "transport_error": "degraded", "capsule": "pass"}
    if shape in expected:
        assert axis["status"] == expected[shape]
        assert review_rows[0]["status"] == ("failed" if shape in {"fail", "zero_actor_refusal", "malformed", "transport_error"} else "ok")


def test_an_earlier_genuine_panel_survives_a_later_stop_acknowledgement():
    from ouroboros.tools.review import _handle_task_acceptance_review

    trace = _capture(_real_result(actors=[{"slot_id": "a", "status": "ok", "signal": "FAIL",
                                           "parsed": {"verdict": "FAIL"}}], aggregate_signal="FAIL"))
    args = {"claim": STOP_CLAIM, "author_action": "stop", "rationale": STOP_RATIONALE}
    _capture(_handle_task_acceptance_review(_ctx(), **args), args=args, trace=trace)
    assert len(trace["review_runs"]) == 1 and trace["review_runs"][0]["aggregate_signal"] == "FAIL"
    axis, review_rows, panels = _consumers(trace)
    assert axis == {**axis, "status": "fail", "run_count": 1, "aggregate_signals": ["FAIL"]}
    assert len(review_rows) == 1 and len(panels) == 1
