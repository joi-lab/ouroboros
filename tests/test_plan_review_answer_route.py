"""The ROUTE of the addressed answer, end to end and in process: the real engine, the real
``task_results``/artifact store on ``tmp_path``, the fake review substrate of
``tests.test_plan_review_engine`` and the production finalization gate
(``owner_hurry.force_plan_decision``), all under BLOCKING enforcement at the shipped
cycle cap of 2 — the configuration every install starts from.

Five routes: the objector retires after the addressed re-ask (GREEN, closed, the gate
releases, the closed wave's claims bind acceptance); two objectors at quorum retire;
the objector never retires and the cap is spent (the typed cap state, the honest exits);
the objector fails to answer the re-ask (its finding is carried, never GREEN); and the
no-need path (a question and a note close at $0 with no second transport call).
"""
from __future__ import annotations

import json

from ouroboros.contracts.task_contract import effective_acceptance_claims
from ouroboros.outcomes import derive_loop_outcome
from ouroboros.owner_hurry import force_plan_decision
from ouroboros.review_cycles import review_max_cycles
from ouroboros.task_results import closed_plan_review_wave
from ouroboros.tools.plan_review_artifacts import read_wave
from tests.test_plan_review_answer_channel import _actors, _answer, _blocking, _ids, _item, _question, _reject
from tests.test_plan_review_engine import (  # noqa: F401
    CLEAN, DECK_SPEC, _call, _control, _finding, _state, harness,
)

REJECT_RATIONALE = "the budget line is already approved"


def _gate(ctx) -> dict:
    return force_plan_decision(ctx, {}, enforcement="blocking")


def _wave(h) -> dict:
    return _state(h)["waves"][-1]


def _events(h, event_type: str) -> list:
    return [e["data"] for e in list(h.events.queue)
            if e.get("type") == "log_event" and e.get("data", {}).get("type") == event_type]


def _addressed(ctx, fp: str, *fids: str) -> str:
    """The identical envelope carrying the reject of every named finding."""
    return _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item(fid, "reject", REJECT_RATIONALE) for fid in fids]})


def _bound_claims(state: dict) -> tuple[list, str]:
    return effective_acceptance_claims({}, closed_plan_review_wave(state))


def _objection(h, answers: dict) -> tuple:
    """Cycle 1 at the shipped cap: the configured seats answer ``answers``; the wave is
    open, one cycle is paid, and the blocking gate holds."""
    assert review_max_cycles() == 2, "the route runs at the shipped default cap"
    sub = h.install(answers)
    ctx = h.make_ctx()
    out = _call(ctx)
    assert not _control(out)["closed"], out
    assert len(sub.calls) == 1 and _state(h)["cycles_paid"] == 1
    gate = _gate(ctx)
    assert gate["allow"] is False and gate["status"] == "open", gate
    assert _bound_claims(_state(h)) == ([], "")  # no closed authority: nothing binds acceptance
    return ctx, _wave(h)["request_fingerprint"], sub


# ----------------------------------------------------------------- the happy route

def test_e2e_reject_addressed_retire_green_under_blocking(harness):  # noqa: F811
    """REVIEW_REQUIRED (s1 blocking below quorum) → the gate holds → a $0 reject → the
    gate still holds → the identical envelope with the reject asks s1 alone (s2, s3 kept
    at $0) → s1 retires → GREEN closed, two cycles paid, the gate releases and the closed
    wave's claims bind acceptance."""
    ctx, fp, sub1 = _objection(harness, {"s1": _blocking(), "s2": CLEAN, "s3": CLEAN})
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}  # replays free
    assert len(sub1.calls) == 1
    rejected = _reject(ctx, fp)
    assert _control(rejected) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert len(sub1.calls) == 1 and _state(harness)["cycles_paid"] == 1  # the answer costs nothing
    assert _ids(_wave(harness)) == [("s1:f1", "reject")]
    assert _gate(ctx)["allow"] is False, "a reject alone never closes a blocking finding under blocking"

    sub2 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _addressed(ctx, fp, "s1:f1")
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert len(sub2.calls) == 1 and [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1"]
    assert "**Addressed answer:** asked again s1 on s1:f1; kept at $0: s2, s3." in result

    state = _state(harness)
    wave = state["waves"][-1]
    assert state["cycles_paid"] == 2
    assert wave["request_fingerprint"] == fp and wave["cycle_index"] == 2 and wave["paid"]
    assert wave["aggregate"] == "GREEN" and wave["closed"] is True and wave["findings"] == []
    assert wave["addressed"] == {"slots": ["s1"], "finding_ids": ["s1:f1"], "kept": ["s2", "s3"],
                                 "wave_artifact": wave["previous_wave_artifact"]}
    actors = _actors(harness)
    for sid in ("s2", "s3"):
        assert actors[sid]["operation_state"] == "not_dispatched" and actors[sid]["cost"] == 0.0
        assert actors[sid]["replayed_from"] == {"request_fingerprint": fp, "cycle_index": 1,
                                                "operation_id": f"op-{sid}"}
    assert "replayed_from" not in actors["s1"] and actors["s1"]["operation_state"] == "settled"
    # The exact chain: the addressed wave names the cycle-1 wave it answered, byte-exact.
    prior = read_wave(harness.drive, ctx.task_id, wave["addressed"]["wave_artifact"])
    assert prior["cycle_index"] == 1 and prior["request_fingerprint"] == fp
    assert _ids(prior) == [("s1:f1", "reject")] and not prior["closed"]

    gate = _gate(ctx)
    assert gate["allow"] is True and gate["status"] == "closed" and gate["closed"] is True
    claims, source = _bound_claims(state)
    assert source == "plan_review"
    assert [c["claim"] for c in claims] == DECK_SPEC["acceptance_claims"] and claims[0]["id"] == "claim_1"


def test_e2e_revise_plan_two_objectors_retire(harness):  # noqa: F811
    """Two objectors at quorum → REVISE_PLAN, which no disposition closes; both rejects
    ride the identical envelope, both seats are re-asked (s3 kept), both retire → GREEN."""
    ctx, fp, sub1 = _objection(harness, {"s1": _blocking(), "s2": _blocking(), "s3": CLEAN})
    assert _wave(harness)["aggregate"] == "REVISE_PLAN"
    rejected = _answer(ctx, fp, _item("s1:f1", "reject", REJECT_RATIONALE), _item("s2:f1", "reject", REJECT_RATIONALE))
    assert _control(rejected) == {"outcome": "REVISE_PLAN", "closed": False}
    assert len(sub1.calls) == 1 and _gate(ctx)["allow"] is False

    sub2 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _addressed(ctx, fp, "s1:f1", "s2:f1")
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert len(sub2.calls) == 1 and [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1", "s2"]
    state = _state(harness)
    wave = state["waves"][-1]
    assert state["cycles_paid"] == 2 and wave["closed"] and wave["aggregate"] == "GREEN"
    assert wave["addressed"]["slots"] == ["s1", "s2"] and wave["addressed"]["kept"] == ["s3"]
    actors = _actors(harness)
    assert actors["s3"]["replayed_from"]["cycle_index"] == 1 and actors["s3"]["cost"] == 0.0
    assert all("replayed_from" not in actors[sid] for sid in ("s1", "s2"))
    assert _gate(ctx)["allow"] is True and _bound_claims(state)[1] == "plan_review"


# ---------------------------------------------------------- the honest failure routes

def test_e2e_unresolved_objector_spends_the_cap_and_exits_honestly(harness):  # noqa: F811
    """The re-ask at the final permitted cycle: s1 re-emits its finding → the wave stays
    open, both cycles are spent, the typed cap state lands at once (no further envelope
    needed), finalization is RELEASED for an honest blocked exit while no closed authority
    exists, and any further re-ask is refused typed at $0 with the answers kept."""
    ctx, fp, _sub1 = _objection(harness, {"s1": _blocking(), "s2": CLEAN, "s3": CLEAN})
    _reject(ctx, fp)
    sub2 = harness.install({"s1": _blocking(), "s2": CLEAN, "s3": CLEAN})
    result = _addressed(ctx, fp, "s1:f1")
    assert _control(result) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert len(sub2.calls) == 1 and [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1"]

    state = _state(harness)
    wave = state["waves"][-1]
    assert state["cycles_paid"] == 2 and wave["paid"] and not wave["closed"]
    assert [f["finding_id"] for f in wave["findings"] if f["class"] == "blocking"] == ["s1:f1"]
    assert wave["dispositions"] == [], "the re-asked seat's finding starts undispositioned"
    assert wave["cycles_exhausted"] is True
    assert state["current_attempt"] == {"fingerprint": fp, "status": "cycles_exhausted",
                                        "reason": "2/2 paid plan-review cycles spent"}
    exhausted = _events(harness, "review_cycles_exhausted")
    assert exhausted and exhausted[-1]["surface"] == "plan_review" and exhausted[-1]["cycles_paid"] == 2
    gate = _gate(ctx)
    assert gate["status"] == "cycles_exhausted" and gate["allow"] is True and gate["closed"] is False
    assert gate["review_capacity_reason"] == "review_cycles_exhausted" and gate["outcome"] == "REVIEW_REQUIRED"
    assert closed_plan_review_wave(state) is None and _bound_claims(state) == ([], "")
    objective = derive_loop_outcome("done", {}, {"force_plan_decision": gate, "tool_calls": []})["outcome_axes"]["objective"]
    assert (objective["status"], objective["outcome_tier"], objective["reason"]) == (
        "fail", "blocked_with_evidence", "review_cycles_exhausted")

    # A further re-ask at the spent cap: refused typed, nothing sent, the answer recorded.
    _reject(ctx, fp)
    sub3 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    refused = _addressed(ctx, fp, "s1:f1")
    assert refused.startswith("⚠️ PLAN_REVIEW_CYCLES_EXHAUSTED") and "blocked_with_evidence" in refused
    assert _control(refused) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert not sub3.calls and _state(harness)["cycles_paid"] == 2
    assert _ids(_wave(harness)) == [("s1:f1", "reject")]
    # The plain identical envelope replays the open wave free; the cap state stands.
    replay = _call(ctx)
    assert _control(replay) == {"outcome": "REVIEW_REQUIRED", "closed": False} and "cached exact review" in replay
    assert not sub3.calls and _state(harness)["cycles_paid"] == 2
    assert _gate(ctx)["status"] == "cycles_exhausted" and _wave(harness)["cycles_exhausted"] is True


def test_e2e_failed_objector_never_closes(harness):  # noqa: F811
    """s1 answers garbage to the re-ask: its recorded finding is CARRIED (disclosed), the
    wave stays REVIEW_REQUIRED and never turns GREEN; absence never retires a finding.
    The gate before the re-ask holds; the final permitted cycle ending open lands the cap
    state, so finalization is released for a blocked exit with no closed authority."""
    ctx, fp, _sub1 = _objection(harness, {"s1": _blocking(), "s2": CLEAN, "s3": CLEAN})
    _reject(ctx, fp)
    assert _gate(ctx)["allow"] is False
    sub2 = harness.install({"s1": "garbage, not an array", "s2": CLEAN, "s3": CLEAN})
    result = _addressed(ctx, fp, "s1:f1")
    assert _control(result) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert len(sub2.calls) == 1 and [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1"]
    assert "did not answer; its earlier finding is still listed" in result

    state = _state(harness)
    wave = state["waves"][-1]
    assert state["cycles_paid"] == 2 and wave["paid"] and not wave["closed"]
    assert wave["aggregate"] == "REVIEW_REQUIRED"
    assert [f["finding_id"] for f in wave["findings"]] == ["s1:f1"]
    actors = _actors(harness)
    assert not actors["s1"]["ok"] and "findings_carried_absent_answer:1" in actors["s1"]["disclosures"]
    assert actors["s2"]["replayed_from"]["cycle_index"] == 1 and actors["s3"]["cost"] == 0.0
    assert wave["addressed"]["slots"] == ["s1"] and wave["addressed"]["kept"] == ["s2", "s3"]
    assert closed_plan_review_wave(state) is None and _bound_claims(state) == ([], "")
    gate = _gate(ctx)
    assert gate["closed"] is False and gate["outcome"] == "REVIEW_REQUIRED"
    assert gate["status"] == "cycles_exhausted" and gate["allow"] is True  # the cap rail, not a verdict
    # Nothing turns it GREEN afterwards: the answers stay recorded, no seat is asked again.
    sub3 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    later = _addressed(ctx, fp, "s1:f1")
    assert "PLAN_REVIEW_CYCLES_EXHAUSTED" in later and not sub3.calls
    assert _wave(harness)["aggregate"] == "REVIEW_REQUIRED" and not _wave(harness)["closed"]


# ---------------------------------------------------------------- the no-need path

def test_e2e_no_need_path_closes_at_zero_cost(harness):  # noqa: F811
    """s1 asks the author (need_evidence on claim_1, no locator) and s2 leaves a note:
    REVIEW_REQUIRED holds the gate; answering the note alone changes nothing (notes never
    hold); the $0 accept of the question closes GREEN with no second transport call, one
    cycle paid, and the gate releases with the claims bound."""
    note = json.dumps([_finding("n1", "note", summary="a thought", rec="")])
    ctx, fp, sub = _objection(harness, {"s1": json.dumps([_question("q1")]), "s2": note, "s3": CLEAN})
    wave = _wave(harness)
    assert wave["aggregate"] == "REVIEW_REQUIRED"
    assert [(f["finding_id"], f["class"]) for f in wave["findings"]] == [("s1:q1", "need_evidence"), ("s2:n1", "note")]
    assert _state(harness)["need_evidence_seen"] == []  # no locator: the envelope stays the same

    noted = _answer(ctx, fp, _item("s2:n1", "accept", "noted"))
    assert _control(noted) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert _gate(ctx)["allow"] is False and len(sub.calls) == 1

    closed = _answer(ctx, fp, _item("s1:q1", "accept", "the author's word"))
    assert _control(closed) == {"outcome": "GREEN", "closed": True}
    assert len(sub.calls) == 1, "closing by answer sends nothing"
    state = _state(harness)
    wave = state["waves"][-1]
    assert state["cycles_paid"] == 1 and wave["closed"] and wave["aggregate"] == "GREEN"
    assert wave["cycle_index"] == 1 and "addressed" not in wave
    assert _ids(wave) == [("s2:n1", "accept"), ("s1:q1", "accept")]
    assert [f["finding_id"] for f in wave["findings"]] == ["s1:q1", "s2:n1"]  # the record keeps both
    gate = _gate(ctx)
    assert gate["allow"] is True and gate["status"] == "closed"
    claims, source = _bound_claims(state)
    assert source == "plan_review" and [c["claim"] for c in claims] == DECK_SPEC["acceptance_claims"]
    # Two-sided: an identical envelope after closure replays the verdict free, sends nothing.
    assert _control(_call(ctx)) == {"outcome": "GREEN", "closed": True} and len(sub.calls) == 1
