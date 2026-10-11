"""Current author plans retain exact source and independent critic authority."""
import copy
import json

import pytest

from ouroboros.task_results import (closed_plan_review_wave, load_plan_review_state,
    plan_review_gate_projection, record_plan_review_wave)
from ouroboros.tools import plan_review as pr
from ouroboros.tools.plan_review_artifacts import current_author_plan, authority_wave
from tests.test_plan_review_engine import harness, _call, _finding, DECK_SPEC  # noqa: F401


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
@pytest.mark.parametrize("action", ["finish", "stop"])
def test_last_critic_correction_retains_current_source(harness, monkeypatch, enforcement, action):  # noqa: F811
    h = harness
    h.state["enforcement"] = enforcement
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    ctx = h.make_ctx()
    findings = json.dumps([_finding("bad-budget", "blocking", breaks="claim_1")])
    transport = h.install({slot: findings for slot in ("s1", "s2", "s3")})
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    critic = copy.deepcopy(before["waves"][0])
    spec = {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]}
    result = _call(ctx, spec, plan="Corrected complete plan.", reviewer_effort="default", review_disposition={
        "review_fingerprint": critic["request_fingerprint"], "items": [], "author_action": action,
        "author_disposition": {"disposition": "partial", "rationale": "Corrected the budget and saved the exact plan."}})
    assert "Current author plan saved" in result
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert len(transport.calls) == 1 and after["cycles_paid"] == before["cycles_paid"] == 1
    assert authority_wave(h.drive, ctx.task_id, after["waves"][0]) == critic
    assert closed_plan_review_wave(after) is None
    authored = current_author_plan(h.drive, ctx.task_id, after)
    assert authored["spec"]["acceptance_claims"][0]["claim"] == "the corrected claim"
    assert authored["plan_prose"] == "Corrected complete plan."
    gate = plan_review_gate_projection(after, enforcement)
    assert gate["closed"] is False
    assert gate["allow"] is True  # at cap: terminalization, never a clean verdict
    assert gate["status"] == ("author_stopped" if action == "stop" else "advisory_open" if enforcement == "advisory" else "cycles_exhausted")
    if action == "stop" or enforcement == "blocking":
        from ouroboros.outcomes import derive_loop_outcome
        from ouroboros.project_dialogue import outcome_phase
        outcome = derive_loop_outcome("Saved current plan; no implementation.", {}, {"force_plan_decision": gate})
        assert outcome_phase({"status": "completed", "outcome_axes": outcome["outcome_axes"]}, {}) == "error"
    from ouroboros.review_evidence_sections import _accept_effective_claims
    claims, source, _ = _accept_effective_claims(ctx, {}, h.drive, ctx.task_id)
    assert (source == "author_plan") is (enforcement == "advisory" and action == "finish")
    if source == "author_plan":
        assert claims[0]["claim"] == "the corrected claim"
        from ouroboros.review_evidence import build_task_acceptance_evidence
        packet = build_task_acceptance_evidence(ctx, llm_trace={}, drive_root=h.drive, task_id=ctx.task_id, budget_chars=1000000)
        assert packet["acceptance_claims_source"] == "author_plan"
        assert packet["task_contract"]["acceptance_claims"][0]["claim"] == "the corrected claim"
    # A late old-wave update preserves the independently selected current source.
    record_plan_review_wave(h.drive, ctx.task_id, critic)
    assert load_plan_review_state(h.drive, ctx.task_id)["current_attempt"] == after["current_attempt"]


def test_explicit_blocking_stop_before_cap_releases_only_finalization(harness):  # noqa: F811
    h = harness
    ctx = h.make_ctx()
    h.install({"s1": None, "s2": None, "s3": None})
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    assert not plan_review_gate_projection(before, "blocking")["allow"]
    text = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": before["current_attempt"]["fingerprint"], "items": [], "author_action": "stop",
        "author_disposition": {"disposition": "deferred", "rationale": "Review is unavailable; retain this plan for a later task."}})
    assert "No implementation approval" in text
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert plan_review_gate_projection(after, "blocking")["status"] == "author_stopped"
    assert closed_plan_review_wave(after) is None


def test_advisory_author_can_select_current_plan_after_no_dispatch_outcome(harness, monkeypatch):  # noqa: F811
    h = harness
    h.state.update(enforcement="advisory", slots=[])
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    ctx = h.make_ctx()
    unavailable = _call(ctx)
    assert "No review models configured" in unavailable
    before = load_plan_review_state(h.drive, ctx.task_id)
    assert before["cycles_paid"] == 0 and not before["waves"]
    result = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["current claim"]},
        review_disposition={"review_fingerprint": before["current_attempt"]["fingerprint"], "items": [],
            "author_action": "finish", "author_disposition": {"disposition": "accepted", "rationale": "The unavailable reviewer was disclosed; proceed with the current plan."}})
    assert "Advisory author finish permits proceeding" in result
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert after["cycles_paid"] == 0 and not after["waves"]
    assert current_author_plan(h.drive, ctx.task_id, after)["spec"]["acceptance_claims"][0]["claim"] == "current claim"


def test_full_plan_needs_explicit_action_even_with_a_prior_wave(harness, monkeypatch):  # noqa: F811
    """A changed plan beside a stance (no author_action) is REVIEWED — the stance lands on the
    critic wave, the envelope goes to the panel — and is never SELECTED as the author plan."""
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    ctx = h.make_ctx()
    transport = h.install({slot: json.dumps([_finding("budget", "blocking", breaks="claim_1")])
                           for slot in ("s1", "s2", "s3")})
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    critic_fp = before["waves"][-1]["request_fingerprint"]
    result = _call(ctx, plan="Changed plan without a finish action.", review_disposition={
        "review_fingerprint": critic_fp, "items": [],
        "author_disposition": {"disposition": "partial", "rationale": "This is a stance, not a finish choice."}})
    assert "Current author plan saved" not in result and "ERROR:" not in result
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert len(transport.calls) == 2 and after["cycles_paid"] == 2
    assert after["current_attempt"]["fingerprint"] != critic_fp
    assert not after["current_attempt"].get("author_subject"), "a full plan is never selected without author_action"
    critic = next(w for w in after["waves"] if w["request_fingerprint"] == critic_fp)
    assert critic["author_disposition"]["rationale"] == "This is a stance, not a finish choice."


@pytest.mark.parametrize("action", [None, "none"])
def test_first_plan_accepts_default_filled_neutral_fields(harness, action):  # noqa: F811
    ctx = harness.make_ctx()
    transport = harness.install({})
    disposition = {"review_fingerprint": "", "items": [],
                   "author_disposition": {"disposition": "deferred", "rationale": ""}}
    if action is not None:
        disposition["author_action"] = action
    before = copy.deepcopy(disposition)
    result = _call(ctx, reviewer_effort="default", review_disposition=disposition)
    assert "ERROR:" not in result
    state = load_plan_review_state(harness.drive, ctx.task_id)
    assert len(transport.calls) == state["cycles_paid"] == 1
    assert state["waves"][0]["reviewer_effort"] == ""
    assert not state["current_attempt"].get("author_subject")
    assert disposition == before
    # The named neutral and omission have exactly the same identity and replay.
    fingerprint = state["current_attempt"]["fingerprint"]
    assert "cached exact review" in _call(ctx)
    assert len(transport.calls) == 1
    assert load_plan_review_state(harness.drive, ctx.task_id)["current_attempt"]["fingerprint"] == fingerprint


@pytest.mark.parametrize("enforcement", ["advisory", "blocking"])
@pytest.mark.parametrize("author", [
    {"disposition": "partial", "rationale": "Collect"},
    {"disposition": "deferred", "rationale": ""},
])
def test_neutral_action_records_finding_answers_but_no_author_finish(harness, monkeypatch, enforcement, author):  # noqa: F811
    harness.state["enforcement"] = enforcement
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    ctx = harness.make_ctx()
    findings = json.dumps([_finding("question", "need_evidence", locator="notes.md")])
    transport = harness.install({s: findings for s in ("s1", "s2", "s3")})
    _call(ctx)
    state = load_plan_review_state(harness.drive, ctx.task_id)
    wave = state["waves"][-1]
    disposition = {"review_fingerprint": wave["request_fingerprint"], "author_action": "none",
        "author_disposition": author, "items": [
            {"finding_id": f["finding_id"], "decision": "accept", "rationale": "The requested notes are attached."}
            for f in wave["findings"]]}
    before = copy.deepcopy(disposition)
    result = pr._handle_plan_task(ctx, goal="", plan="", spec={k: [] for k in DECK_SPEC},
                                 reviewer_effort="low", review_disposition=disposition)
    assert "ERROR:" not in result
    after = load_plan_review_state(harness.drive, ctx.task_id)
    exact = authority_wave(harness.drive, ctx.task_id, after["waves"][-1])
    assert exact["closed"] and exact["dispositions"] == disposition["items"]
    assert not exact.get("author_disposition") and not after["waves"][-1].get("author_disposition")
    assert not after["current_attempt"].get("author_subject")
    assert len(transport.calls) == after["cycles_paid"] == 1
    assert disposition == before


@pytest.mark.parametrize("field,value", [("goal", "Changed goal"), ("plan", "Changed plan"),
    ("spec", {"in_scope": ["Changed scope"]})])
def test_neutral_action_beside_a_malformed_envelope_is_refused_by_its_form(harness, field, value):  # noqa: F811
    ctx = harness.make_ctx()
    transport = harness.install({})
    result = pr._handle_plan_task(ctx, **{field: value}, reviewer_effort="low", review_disposition={
        "review_fingerprint": "f" * 64, "items": [], "author_action": "none"})
    assert "PLAN_SPEC_INVALID" in result or "PLAN_RESOURCE_FORM_REQUIRED" in result, result
    assert not transport.calls and not (harness.drive / "task_results" / (ctx.task_id + ".json")).exists()


@pytest.mark.parametrize("action", ["finish", "stop", "bogus", ["finish"]])
def test_author_action_errors_name_values_without_erasing_explicit_intent(harness, action):  # noqa: F811
    ctx = harness.make_ctx()
    transport = harness.install({})
    result = _call(ctx, reviewer_effort="default", review_disposition={
        "review_fingerprint": "", "items": [], "author_action": action,
        "author_disposition": {"disposition": "deferred", "rationale": ""}})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in result
    assert "author_action=" in result and str(action if isinstance(action, str) else action[0]) in result
    assert "review_fingerprint" in result
    assert not transport.calls


@pytest.mark.parametrize("author", [[], {"disposition": "partial", "rationale": [],},
    {"disposition": "partial", "rationale": "Collect", "unknown": ""}])
def test_neutral_action_does_not_discard_malformed_author_fields(harness, author):  # noqa: F811
    result = _call(harness.make_ctx(), review_disposition={"review_fingerprint": "", "items": [],
        "author_action": "none", "author_disposition": author})
    assert "ERROR:" in result and "author_disposition" in result


def test_author_action_error_names_unknown_field_and_value(harness):  # noqa: F811
    result = _call(harness.make_ctx(), review_disposition={
        "review_fingerprint": "", "items": [], "author_action": "finish",
        "unexpected": "remove-this-field"})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in result
    assert 'unexpected="remove-this-field"' in result


def test_plan_neutral_enums_are_named_first_and_default():
    props = pr.get_tools()[0].schema["parameters"]["properties"]
    for schema, neutral in [(props["reviewer_effort"], "default"),
                            (props["review_disposition"]["properties"]["author_action"], "none")]:
        assert schema["enum"][0] == schema["default"] == neutral
        assert "" not in schema["enum"]
    assert "none" in props["reviewer_effort"]["enum"]  # real explicit effort remains


@pytest.mark.parametrize("effort", ["low", "high", "none"])
def test_collection_without_author_action_ignores_the_effort_override(harness, effort):  # noqa: F811
    ctx = harness.make_ctx()
    transport = harness.install({})
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    result = pr._handle_plan_task(ctx, goal="", plan="", spec={k: [] for k in DECK_SPEC},
        reviewer_effort=effort, review_disposition={
            "review_fingerprint": before["waves"][-1]["request_fingerprint"], "items": []})
    assert "ERROR:" not in result and len(transport.calls) == 1
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_omitted_action_keeps_intentional_legacy_advisory_finish(harness, monkeypatch):  # noqa: F811
    harness.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    ctx = harness.make_ctx()
    findings = json.dumps([_finding("budget", "blocking", breaks="claim_1")])
    transport = harness.install({s: findings for s in ("s1", "s2", "s3")})
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    disposition = {"review_fingerprint": before["waves"][-1]["request_fingerprint"], "items": [],
        "author_disposition": {"disposition": "partial", "rationale": "I considered the findings and will proceed."}}
    result = pr._handle_plan_task(ctx, review_disposition=disposition)
    assert "ERROR:" not in result
    after = load_plan_review_state(harness.drive, ctx.task_id)
    assert after["waves"][-1]["author_disposition"]["rationale"] == disposition["author_disposition"]["rationale"]
    assert not after["waves"][-1]["closed"] and len(transport.calls) == 1


def test_envelope_form_refusal_beside_answers_stays_bounded(harness):  # noqa: F811
    result = pr._handle_plan_task(harness.make_ctx(), plan="Long plan " * 10_000,
        review_disposition={"review_fingerprint": "f" * 64, "items": []})
    assert "PLAN_SPEC_INVALID" in result and len(result) < 1200
