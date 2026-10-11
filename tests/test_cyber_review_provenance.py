"""Recovered first-review and exact-author evidence regressions from the preserved Ouroboros candidate."""
import copy
import json
import threading
from types import SimpleNamespace
import pytest
from tests.test_loop_acceptance_gate import _seed_acceptance_root
from tests.test_plan_review_engine import harness, _call, _control, _state  # noqa: F401

pytestmark = pytest.mark.serial

def test_plan_first_dispatch_then_author_repeat_keeps_exact_raw_wave(harness, monkeypatch):  # noqa: F811
    """The plan-first dispatch now returns at the P1-2 barrier (owner batch 2, Q2=A):
    the fresh envelope hands control back with the wave open and its rows
    ``pending_dispatch``, the settlement thread writes one mailbox frame, and the
    identical envelope collects the wave once. Only then does the recovered
    regression hold: the exact raw wave survives that collection and the author's
    repeated disposition neither re-sends nor rewrites it.
    """
    from ouroboros.tools import plan_review
    from ouroboros.tools.plan_review_artifacts import read_wave
    from tests.test_plan_review_event_route import _mailbox_entries, _wait_until

    h = harness
    h.state["enforcement"] = "advisory"
    raw = json.dumps([{"id": "f", "class": "blocking", "breaks": "claim_1",
                       "summary": "Unresolved criterion", "recommendation": "Prove it"}])
    from tests.test_review_agent_session_route import AccountedFakeLLM
    from ouroboros.tools import plan_review_runtime

    transport = AccountedFakeLLM(h.drive, reply=raw)
    release = threading.Event()
    chat = transport.chat

    def held_chat(**kwargs):
        assert release.wait(20), "test did not release the physical reviewer calls"
        return chat(**kwargs)

    monkeypatch.setattr(transport, "chat", held_chat)
    monkeypatch.setattr(plan_review_runtime, "LLMClient", lambda: transport)
    ctx = h.make_ctx()
    try:
        barrier = _control(_call(ctx))
        assert barrier == {"outcome": "DEGRADED", "closed": False}
        open_wave = _state(h)["waves"][-1]
        # Pin the pre-dispatch phase: a fast real send may otherwise settle a
        # slot before the barrier snapshot and correctly make the wave paid.
        assert open_wave["custody_pending"] is True and open_wave["paid"] is False
        assert {actor["operation_state"] for actor in open_wave["actors"]} == {"pending_dispatch"}
        assert not transport.calls
    finally:
        release.set()
    assert _wait_until(lambda: len(_mailbox_entries(h.drive, ctx.task_id)) == 1)
    dispatched = len(transport.calls)
    first = _control(_call(ctx))  # the identical envelope collects the settled wave
    assert first["outcome"] == "REVISE_PLAN"
    assert len(transport.calls) == dispatched  # collected once, no second send
    wave = _state(h)["waves"][-1]
    assert wave["request_fingerprint"] == open_wave["request_fingerprint"]
    assert wave["paid"] and transport.calls
    fingerprint = wave["request_fingerprint"]
    request = {"review_fingerprint": fingerprint, "items": [],
               "author_disposition": {"disposition": "deferred", "rationale": "Keep the verified scope."}}
    calls = len(transport.calls)
    for _ in range(2):
        out = plan_review._handle_plan_task(ctx, review_disposition=request)
        assert "Author finish" in out
        assert len(transport.calls) == calls
    current = _state(h)["waves"][-1]
    assert not current["closed"] and current["aggregate"] == "REVISE_PLAN"
    assert current["findings"] == wave["findings"]
    exact = read_wave(h.drive, ctx.task_id, current["wave_artifact"])
    assert raw in json.dumps(exact, ensure_ascii=False) or any(
        row.get("text") == raw or row.get("raw_text") == raw for row in exact.get("reviewer_outputs", [])
    )
    assert current["author_disposition"]["subject_hash"] == fingerprint


def test_plan_author_cannot_replace_first_dispatch_or_bind_stale_wave(harness):  # noqa: F811
    from ouroboros.tools import plan_review

    h = harness
    h.state["enforcement"] = "advisory"
    transport = h.install({"s1": "", "s2": "", "s3": ""})
    ctx = h.make_ctx()
    request = {"review_fingerprint": "a" * 64, "items": [],
               "author_disposition": {"disposition": "rejected", "rationale": "No repeat."}}
    assert "UNBINDABLE" in plan_review._handle_plan_task(ctx, review_disposition=request)
    assert not transport.calls
    _call(ctx)
    request["review_fingerprint"] = _state(h)["waves"][-1]["request_fingerprint"]
    _call(ctx, goal="A different subject")
    calls = len(transport.calls)
    assert "STALE" in plan_review._handle_plan_task(ctx, review_disposition=request)
    assert len(transport.calls) == calls


def test_plan_cancellation_preserves_review_without_author_finish(harness):  # noqa: F811
    from ouroboros.cancel_intents import request_cancel
    from ouroboros.tools import plan_review

    h = harness
    h.state["enforcement"] = "advisory"
    transport = h.install({"s1": "", "s2": "", "s3": ""})
    ctx = h.make_ctx()
    _call(ctx)
    prior = _state(h)["waves"][-1]
    request_cancel(h.drive, ctx.task_id, source="test", reason="Owner stopped")
    out = plan_review._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": prior["request_fingerprint"], "items": [],
        "author_disposition": {"disposition": "deferred", "rationale": "Stop reviewing"},
    })
    assert "cancellation prevents" in out
    assert _state(h)["waves"][-1] == prior
    assert len(transport.calls) == 1


@pytest.mark.parametrize("case", ["same", "no_first", "stale", "blocking", "post_changed"])
def test_commit_author_record_requires_prior_review_of_exact_attempt(tmp_path, monkeypatch, case):
    from types import SimpleNamespace
    from ouroboros.review_records import build_author_disposition
    from ouroboros.review_state import load_state
    from ouroboros.tools.commit_gate import _record_commit_attempt

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking" if case == "blocking" else "advisory")
    ctx = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path, task_id="",
                          _review_advisory=[], _current_review_attempt_number=0)
    raw = [{"slot_id": "critic", "text": "Raw unresolved finding"}]
    if case != "no_first":
        _record_commit_attempt(ctx, commit_message="fixture", status="reviewing",
                               paid=True, pre_review_fingerprint="a" * 64, triad_raw_results=raw)
    author = build_author_disposition(
        disposition="deferred", rationale="The remaining finding is deferred.",
        subject_hash=("b" if case == "stale" else "a") * 64, enforcement="advisory",
    )
    _record_commit_attempt(ctx, commit_message="fixture", status="blocked",
                           pre_review_fingerprint="a" * 64,
                           post_review_fingerprint=("b" if case == "post_changed" else "a") * 64,
                           author_disposition=author)
    row = load_state(tmp_path).attempts[-1]
    assert row.status == "blocked"
    assert bool(row.author_disposition) is (case == "same")
    if case != "no_first":
        assert row.triad_raw_results == raw


@pytest.fixture
def actual_acceptance(tmp_path, monkeypatch):
    from ouroboros import config, loop, review_substrate
    from ouroboros.loop_acceptance_review import _run_task_acceptance_review_once

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setattr(loop, "get_review_enforcement", lambda: config.get_review_enforcement())
    from tests.test_review_agent_session_route import AccountedFakeLLM

    slot = review_substrate.ReviewSlot(slot_id="s0", model="test-review", effort="high")
    monkeypatch.setattr(review_substrate, "triad_delivery_slots", lambda **k: [slot])
    raw = json.dumps({"verdict": "FAIL", "outcome_tier": "best_effort",
                      "completion_coach": "Check the criterion",
                      "findings": [{"item": "Unaccepted raw finding", "severity": "critical",
                                    "recommendation": "Check the criterion"}]})
    state = {"calls": 0, "unknown": False, "dispatch": True}

    class ControlledLLM(AccountedFakeLLM):
        def chat(self, **kwargs):
            if not state["dispatch"]:
                raise RuntimeError("Route refused before dispatch")
            if state["unknown"]:
                self.calls.append(kwargs)
                exc = TimeoutError("RPC outcome unknown")
                exc.physical_attempt_capture = SimpleNamespace(
                    state="unresolved", provider_status_code=None, provider_code="",
                    provider_error_type="TimeoutError",
                )
                raise exc
            return super().chat(**kwargs)

    physical = ControlledLLM(tmp_path, reply=raw)
    actual_run = review_substrate.run_review_request
    ctx = SimpleNamespace(_task_acceptance_reviewed=False, is_direct_chat=True, drive_root=tmp_path,
                          drive_logs=lambda: tmp_path / "logs")
    # Unknown paid operations also have process-local custody; each test owns
    # a different logical task, not merely a different directory for "task".
    task_id = "task-" + tmp_path.name
    _seed_acceptance_root(tmp_path, task_id, ctx)
    trace = {"tool_calls": [{"tool": "write_file", "args": {"path": "result.txt"}}]}
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "goal"}]
    tools = SimpleNamespace(_ctx=ctx)
    ctx._execution_trace = trace
    loop_ctx = SimpleNamespace(tools=tools, task_id=task_id, root_task_id=task_id,
                               status_drive_root=tmp_path, drive_root=tmp_path,
                               drive_logs=tmp_path / "logs", messages=messages)
    def transport(request, *, usage_ctx, **kwargs):
        state["calls"] += 1
        return actual_run(request, usage_ctx=usage_ctx, llm=physical, **kwargs)

    monkeypatch.setattr(review_substrate, "run_review_request", transport)

    def run(content="done"):
        return _run_task_acceptance_review_once(
            tools=tools, content=content, task_id=task_id, task_type="task",
            llm_trace=trace, drive_root=tmp_path, messages=messages, emit_progress=lambda *a, **k: None,
        )

    def annotate(content="Verified revised answer", rationale="The remaining criticism is outside this scope."):
        from jsonschema import validate
        from ouroboros.loop_delivery import completion_observation, consume_completion_request
        from ouroboros.loop_messages import capture_acceptance_observation
        from ouroboros.tools.review import _handle_task_acceptance_review, get_tools
        from ouroboros.loop_tool_execution import process_tool_results
        from tests.provider_contract_catalog import assert_portable_tool_schemas

        # Mirror the real response boundary: the tool stages the author's act,
        # and the loop consumes it only after all results in the batch exist.
        observed = capture_acceptance_observation(ctx, trace)
        ctx._completion_observation = completion_observation(ctx, trace)
        args = {"claim": content, "goal": "Complete the task",
                "agent_disposition": "partial", "rationale": rationale,
                "acceptance_subject": {"owner_source_sha256": observed["owner_source_sha256"]}}
        schema = get_tools()[0].schema
        assert_portable_tool_schemas([{"type": "function", "function": schema}])
        validate(args, schema["parameters"])
        result = _handle_task_acceptance_review(ctx, **args)
        process_tool_results([{"fn_name": "task_acceptance_review", "tool_call_id": "author",
                              "result": result, "is_error": False, "args_for_log": args,
                              "tool_args": args, "result_meta": {"status": "ok"}}],
                             messages, trace, emit_progress=lambda *a, **k: None,
                             tools=tools)
        assert consume_completion_request(tools, loop_ctx, trace), trace.get("completion_refusals")
        assert ctx._delivery_candidate.full_text == content
        return result

    def host_runs():
        # The legacy tool's completion receipt is not an independent critic.
        return [row for row in trace.get("review_runs", []) if row.get("authority") == "host_root"]

    return SimpleNamespace(ctx=ctx, trace=trace, state=state, run=run, annotate=annotate,
                           host_runs=host_runs, root=tmp_path, physical=physical)


@pytest.mark.parametrize("unknown", [False, True])
def test_real_task_panel_then_revised_author_finish_preserves_custody(actual_acceptance, unknown):
    h = actual_acceptance
    h.state["unknown"] = unknown
    # An explicit pre-panel stance still cannot replace the first physical call.
    h.annotate("Initial answer")
    assert not h.physical.calls
    assert not h.trace["acceptance_decision"].get("agent_finish_intent")
    h.run("Initial answer")
    assert h.state["calls"] == 1 and len(h.physical.calls) == 1
    first = copy.deepcopy(h.host_runs()[-1])
    if not unknown:
        import inspect
        from ouroboros.acceptance_settlement import expose_acceptance_feedback
        expose_acceptance_feedback(h.trace, inspect.getclosurevars(h.run).nonlocals["messages"], h.ctx.task_id)
    h.annotate("Initial answer" if unknown else "Verified revised answer")
    # A terminal unknown response receives no critic feedback to answer. An
    # unchanged subject must retain its original no-resend custody; explicitly
    # nominating different material is a new review, tested separately below.
    h.run("Initial answer" if unknown else "Verified revised answer")
    assert h.state["calls"] == 1 and len(h.physical.calls) == 1
    assert len(h.host_runs()) == 1
    assert h.host_runs()[-1]["actors"] == first["actors"]
    assert h.host_runs()[-1]["paid_identity"] == first["paid_identity"]
    decision = h.trace["acceptance_decision"]
    assert decision["status"] == "finalized_unaccepted"
    if unknown:
        assert not first.get("feedback_delivered")
        assert decision["reason"] == "identical_acceptance_refused"
        assert not decision.get("author_disposition")
        actor = first["actors"][0]
        assert actor["operation_state"] == "custody_lost" and actor["late_result_pending"]
        assert "RPC outcome unknown" in actor["error"]
    else:
        assert decision["reason"] == "author_finish"
        assert decision["reviewer_binding_hash"] == first["binding_hash"]
        assert decision["author_disposition"]["subject_hash"] != first["binding_hash"]


def test_new_subject_after_unknown_review_keeps_the_original_operation(actual_acceptance):
    h = actual_acceptance
    h.state["unknown"] = True
    h.annotate("Original result for explicit new-subject test")
    # A genuinely new selected subject may buy its own panel while the first
    # unknown operation remains separately recoverable.
    assert h.run("Original result for explicit new-subject test") is True  # one unavailable-outcome handback
    first = copy.deepcopy(h.host_runs()[-1])
    h.annotate("A materially revised complete result")
    h.run("A materially revised complete result")
    old, new = h.host_runs()
    assert len(h.physical.calls) == 2
    assert old["actors"] == first["actors"]
    assert old["actors"][0]["operation_state"] == "custody_lost"
    assert old["request"]["subject"] == "Original result for explicit new-subject test"
    assert new["request"]["subject"] == "A materially revised complete result"
    assert new["subject_hash"] != first["subject_hash"]
    assert new["paid_identity"] != first["paid_identity"]
    assert new["request"]["retry_key"] != first["request"]["retry_key"]
    assert new["actors"][0]["operation_id"] != first["actors"][0]["operation_id"]


@pytest.mark.parametrize("also_missing_roster", [False, True])
def test_legacy_settled_review_replays_its_proven_subject_without_key_error(actual_acceptance, also_missing_roster):
    h = actual_acceptance
    subject = f"Complete legacy result {also_missing_roster}"
    assert h.run(subject) is True  # The real critic supplied its improvement note.
    first = h.host_runs()[-1]
    old_actors = copy.deepcopy(first["actors"])
    first.pop("subject_hash")
    if also_missing_roster:
        first.pop("slot_roster", None)
    h.ctx._task_acceptance_reviewed = False
    h.run(subject)
    assert len(h.physical.calls) == 1
    assert h.host_runs()[-1]["actors"] == old_actors
    assert h.trace["acceptance_decision"]["reason"] != "infra_failure"
    assert "KeyError" not in json.dumps(h.trace.get("acceptance_decision") or {})


@pytest.mark.parametrize("runtime,enforcement", [("pro", "advisory"), ("cyber_pro", "advisory"), ("cyber_pro", "blocking")])
@pytest.mark.parametrize("explicit", [False, True])
def test_task_author_record_preserves_configured_and_effective_authority(actual_acceptance, monkeypatch, runtime, enforcement, explicit):
    import inspect
    from ouroboros import config
    from ouroboros.acceptance_settlement import expose_acceptance_feedback
    from ouroboros.outcomes import _objective_axis
    from ouroboros.project_dialogue import outcome_phase

    h = actual_acceptance
    # Obtain a real independent outcome in ordinary Advisory first.
    h.run("Initial answer")
    original = copy.deepcopy(h.host_runs()[-1])
    assert len(h.physical.calls) == 1
    expose_acceptance_feedback(h.trace, inspect.getclosurevars(h.run).nonlocals["messages"], h.ctx.task_id)
    if explicit or runtime == "pro":
        h.annotate()
    config.reset_runtime_mode_baseline_for_tests()
    config.initialize_runtime_mode_baseline(runtime)
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    try:
        h.run("Verified revised answer")
        decision = h.trace["acceptance_decision"]
        assert decision["enforcement"] == enforcement
        assert decision["author_disposition"]["enforcement"] == "advisory"
        assert decision["reason"] == "author_finish"
        if explicit or runtime == "pro":
            assert len(h.physical.calls) == len(h.host_runs()) == 1
        assert h.host_runs()[0]["actors"] == original["actors"]
        if not explicit and runtime == "cyber_pro":
            assert decision["author_disposition"]["source"] == "author_final_response"
        review = {"status": "fail", "acceptance_decision": decision}
        objective = _objective_axis(review)
        row = {"status": "completed", "outcome_axes": {
            "objective": objective, "review": review, "execution": {"status": "ok"}}}
        assert objective["status"] == "pass"
        assert outcome_phase(row, {}) == "done"
        row["outcome_axes"]["execution"]["status"] = "failed"
        assert outcome_phase(row, {}) == "error"
    finally:
        config.reset_runtime_mode_baseline_for_tests()
