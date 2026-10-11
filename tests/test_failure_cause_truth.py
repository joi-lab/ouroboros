"""What the engine REPORTED about a failed delegated run survives as ONE record field.

``reported_cause`` is the engine's ``failure.safeMessage``: whitespace-collapsed,
secret-redacted, strictly bounded. It is an opaque fact — these tests pin that it is
carried verbatim through the gateway translator, the actor record, the shared typed-fact
key list and the durable triad row, and that every path that never received words stays
exactly as it was. Nothing here (and nothing in the product) reads the words.
"""

from __future__ import annotations

import json

WORDS = "Selected model is at capacity. Please try a different model."
INCIDENT_FAILURE = {
    "phase": "harness", "category": "harness_error", "code": None,
    "safeMessage": WORDS, "nextActions": ["Inspect the run", "Retry the run"],
}


def test_the_reported_cause_is_verbatim_bounded_redacted_and_absent_when_nothing_was_reported():
    from ouroboros.gateways.claudexor import REPORTED_CAUSE_CHARS, run_failure_cause

    assert run_failure_cause(INCIDENT_FAILURE) == WORDS
    # Whitespace (newlines included) collapses; the words themselves are untouched.
    assert run_failure_cause({"safeMessage": "  at\n capacity\t now  "}) == "at capacity now"
    # A strict bound: the omission marker lives INSIDE the budget and states the cut.
    long = run_failure_cause({"safeMessage": "capacity " * 400})
    assert len(long) <= REPORTED_CAUSE_CHARS == 512
    assert "OMISSION NOTE" in long and "original length" in long
    # A token-shaped secret never reaches the record.
    secret = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8" * 4
    leaked = run_failure_cause({"safeMessage": f"auth failed for key {secret} on this route"})
    assert secret not in leaked and leaked.startswith("auth failed for key")
    # Other direction: nothing reported is honest absence, whatever the shape.
    for nothing in (None, [], {}, "prose", {"code": "x"}, {"safeMessage": ""}, {"safeMessage": None}):
        assert run_failure_cause(nothing) == ""


def test_a_null_engine_code_keeps_the_state_code_and_gains_the_cause():
    from ouroboros.gateways.claudexor import (
        ClaudexorSubscriptionWindowExhausted, ClaudexorUnavailable, run_failure_error,
    )

    exc = run_failure_error("run-1", "failed", INCIDENT_FAILURE)
    assert type(exc) is ClaudexorUnavailable
    assert exc.code == "run_failed" and exc.reported_cause == WORDS
    # The agent-facing message is byte-identical to the branch this translator replaced.
    assert str(exc) == ("delegated review session run-1 ended failed: "
                        + json.dumps(INCIDENT_FAILURE, ensure_ascii=False))
    # A typed window refusal keeps its class, code and reset instant — and the cause too.
    window = run_failure_error("run-2", "failed", {
        "code": "credential_pool_exhausted", "resetsAt": "2030-01-01T00:00:00Z",
        "safeMessage": "every credential profile for this route is spent"})
    assert isinstance(window, ClaudexorSubscriptionWindowExhausted)
    assert (window.code, window.reset_at) == ("credential_pool_exhausted", "2030-01-01T00:00:00Z")
    assert window.reported_cause == "every credential profile for this route is spent"
    # Other direction: an engine that reports nothing leaves the class-level default.
    for failure, state, code, message in (
        (None, "failed", "run_failed", "delegated review session run-3 ended failed"),
        ("prose", "", "run_unknown", "delegated review session run-3 ended unknown"),
        ({"code": "harness_unavailable"}, "interrupted", "harness_unavailable",
         'delegated review session run-3 ended interrupted: {"code": "harness_unavailable"}'),
    ):
        silent = run_failure_error("run-3", state, failure)
        assert (silent.code, str(silent), silent.reported_cause) == (code, message, "")
    # Every other refusal of the same class carries no words either.
    assert ClaudexorUnavailable("daemon_unreachable", "boom").reported_cause == ""


def test_the_shared_typed_fact_list_carries_the_cause_into_plan_rows_and_wave_records():
    from ouroboros.review_substrate import TYPED_FAILURE_FACT_KEYS
    from ouroboros.tools.plan_review_runtime import _plan_row_from_actor, plan_row_typed_facts

    assert "reported_cause" in TYPED_FAILURE_FACT_KEYS
    failed = _plan_row_from_actor({"slot_id": "s1", "model": "m", "status": "error", "error": "boom",
                                   "failure_code": "run_failed", "reported_cause": WORDS}, None)
    assert failed["reported_cause"] == WORDS and failed["failure_code"] == "run_failed"
    assert plan_row_typed_facts(failed)["reported_cause"] == WORDS
    # Other direction: rows that never received words keep honest absence (old rows included).
    healthy = _plan_row_from_actor({"slot_id": "s2", "model": "m", "status": "ok", "raw_text": "[]"}, None)
    assert healthy["reported_cause"] == ""
    assert plan_row_typed_facts({"slot_id": "legacy-row"})["reported_cause"] == ""


def test_durable_triad_rows_and_the_frozen_roster_keep_the_reported_cause():
    from types import SimpleNamespace

    from ouroboros.review_custody import _frozen_actor
    from ouroboros.tools.review import _parse_model_response
    from ouroboros.triad_review import parse_model_review_results

    def _durable_row(result):
        envelope = _parse_model_response("codex=gpt-6-astra", result, None)
        parsed = parse_model_review_results({"results": [envelope]}, required_items=("manifest_schema",))
        return parsed.actor_records[0].to_dict()

    failed = _durable_row({"error": "Error: delegated review session run-1 ended failed",
                           "slot_id": "triad_1", "failure_code": "run_failed", "reported_cause": WORDS})
    assert failed["reported_cause"] == WORDS and failed["failure_code"] == "run_failed"
    # The frozen-roster rebuild is an explicit field-by-field constructor: it must not drop it.
    slot = SimpleNamespace(slot_id="triad_1", model="codex=gpt-6-astra")
    assert _frozen_actor(failed, slot).reported_cause == WORDS
    # Other direction: a failure without words writes no key at all, and a row persisted
    # before the field existed rebuilds with the dataclass default.
    wordless = _durable_row({"error": "Error: route unavailable", "slot_id": "triad_2",
                             "failure_code": "route_unavailable"})
    assert "reported_cause" not in wordless and wordless["failure_code"] == "route_unavailable"
    assert _frozen_actor(wordless, slot).reported_cause == ""


def test_a_producer_outcome_persisted_before_the_field_existed_still_loads():
    from dataclasses import asdict

    from ouroboros.review_substrate import ReviewActorRecord

    old = asdict(ReviewActorRecord(slot_id="s1", model="m", status="error", error="boom",
                                   failure_code="run_failed"))
    old.pop("reported_cause")  # the shape an older process wrote
    assert ReviewActorRecord(**old).reported_cause == ""
    new = ReviewActorRecord(**{**old, "reported_cause": WORDS})
    assert asdict(new)["reported_cause"] == WORDS


def test_the_last_execution_projection_keeps_the_cause_only_when_one_was_reported():
    from types import SimpleNamespace

    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.reviewer_slot_config import (
        record_reviewer_slot_executions, reviewer_slot_last_executions,
    )

    slots = {name: ReviewSlot(slot_id=name, model="codex=gpt-6-astra", effort="high",
                              route=ReviewRouteKind.AGENT_SESSION, session_target="codex=gpt-6-astra")
             for name in ("c_dead", "c_wordless", "c_alive")}
    record_reviewer_slot_executions("multi_model_review", [
        SimpleNamespace(slot_id="c_dead", status="error", usage={},
                        failure_code="run_failed", reported_cause=WORDS),
        SimpleNamespace(slot_id="c_wordless", status="error", usage={}, failure_code="run_failed"),
        SimpleNamespace(slot_id="c_alive", status="ok", usage={}, reported_cause=""),
    ], slots)
    rows = reviewer_slot_last_executions()
    assert rows["c_dead"]["reported_cause"] == WORDS
    assert "reported_cause" not in rows["c_wordless"] and "reported_cause" not in rows["c_alive"]


def test_the_mind_reads_typed_facts_and_never_the_engines_retry_coach():
    """What the model reads for a wave with a dead reviewer: the typed facts (code, reset,
    model) and the engine's reported sentence — never its ``nextActions`` retry coach. The
    stored wave is untouched (its ``reasons`` keep the raw prose); a row that reported no
    words renders exactly today's bytes."""
    import copy

    from ouroboros.tools.plan_render import _render_wave

    prose = "delegated review session run-e23 ended failed: " + json.dumps(INCIDENT_FAILURE, ensure_ascii=False)

    def _wave(actor):
        return {"cycle_index": 1, "request_fingerprint": "f" * 64, "aggregate": "DEGRADED", "closed": False,
                "custody_pending": False, "findings": [], "actors": [actor, {"slot_id": "s2", "model": "m", "ok": True}],
                "counts": {"configured": 2, "parseable": 1, "quorum": 2, "blocking": 0, "note": 0, "need_evidence": 0},
                "reasons": [f"slot_unparseable:s1:{prose}", "parseable_slots_below_quorum:1/2"]}

    dead = {"slot_id": "s1", "model": "codex=gpt-6-astra", "route": "agent_session", "ok": False,
            "failure_code": "run_failed", "error": prose, "reported_cause": WORDS, "operation_state": "late_settled"}
    wave = _wave(dead)
    before = copy.deepcopy(wave)
    text = _render_wave(wave, cap=3, cycles_paid=1, enforcement="advisory")
    assert wave == before and "nextActions" in wave["reasons"][0], "a display substitution never rewrites the stored wave"
    assert f'· FAILED[run_failed] — model=codex=gpt-6-astra; reported cause: "{WORDS}"' in text
    assert f"Reasons: slot_unparseable:s1:{WORDS}, parseable_slots_below_quorum:1/2." in text
    for coach in ("nextActions", "Retry the run", "Inspect the run", "safeMessage", "harness_error"):
        assert coach not in text, coach
    # Other direction: a row that reported no words renders today's bytes, prose included.
    wordless = _render_wave(_wave({**dead, "reported_cause": ""}), cap=3, cycles_paid=1, enforcement="advisory")
    assert f"· FAILED[run_failed]: {prose}" in wordless and f"Reasons: slot_unparseable:s1:{prose}," in wordless
    assert "reported cause" not in wordless
