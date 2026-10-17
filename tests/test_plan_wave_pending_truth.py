"""An answer that has not arrived yet is a gap: not a failure and not a verdict.

A v2 plan-review wave released at the dispatch barrier stored a slot that had not
answered as an error actor (``ok=False``, ``operation_state="pending_dispatch"``).
The read-only views of such records (the plan-review reflection, the acceptance
exhibit) read the typed slot census (``plan_review_runtime.plan_wave_slot_census``);
these tests pin its classes and the custody predicates it rests on.
"""

from __future__ import annotations

import pytest

from ouroboros.review_records import review_slot_awaiting, review_slot_unresolved
from ouroboros.tools.plan_review_runtime import plan_wave_slot_census

PENDING_ERROR = "Pending dispatch; the physical review operation is in flight (window 21600s)"
FP = "f" * 64


def _pending(slot, state="pending_dispatch", **extra):
    """The error actor ``review_custody`` mints for a slot with no answer yet."""
    return {"slot_id": slot, "model": "m", "route": "agent_session", "ok": False, "failure_code": "",
            "error": PENDING_ERROR, "operation_state": state, "late_result_pending": True,
            "operation_id": f"op-{slot}", **extra}


def _ok(slot):
    return {"slot_id": slot, "model": "m", "route": "api_chat", "ok": True, "error": None,
            "operation_state": "settled", "operation_id": f"op-{slot}"}


def _failed(slot, code="run_failed", error="harness unavailable"):
    return {"slot_id": slot, "model": "m", "route": "agent_session", "ok": False, "failure_code": code,
            "error": error, "operation_state": "late_settled", "operation_id": f"op-{slot}"}


def _skipped(slot):
    return {"slot_id": slot, "model": "m", "route": "agent_session", "ok": False,
            "failure_code": "subscription_window_exhausted", "error": "health_skip[...]: skipped at $0",
            "operation_state": "not_dispatched"}


def _wave(actors, *, pending=True, **extra):
    """A recorded v2 wave shaped as the retired engine left it."""
    parseable = sum(1 for a in actors if isinstance(a, dict) and a.get("ok"))
    return {
        "cycle_index": 1, "request_fingerprint": FP, "aggregate": "DEGRADED", "closed": False,
        "custody_pending": pending, "actors": actors, "findings": [],
        "counts": {"configured": len(actors), "parseable": parseable, "quorum": 2,
                   "blocking": 0, "note": 0, "need_evidence": 0, "blocking_slots": 0},
        "reasons": [f"slot_unparseable:{a['slot_id']}:{a['error']}" for a in actors
                    if isinstance(a, dict) and not a.get("ok")]
                   + [f"parseable_slots_below_quorum:{parseable}/2"]
                   + (["review_late_result_pending"] if pending else []),
        **extra,
    }


def _supplement(slot, state="late_settled", cycle=1):
    return {"slot_id": slot, "operation_id": f"op-{slot}", "operation_state": state, "cycle_index": cycle}


# ------------------------------------------------------------------ typed vocabulary


def test_awaiting_is_only_pending_dispatch_and_late_result_pending_never_means_a_planned_wait():
    assert review_slot_awaiting({"operation_state": "pending_dispatch"})
    for state in ("in_flight", "custody_lost"):
        row = {"operation_state": state, "late_result_pending": True}
        assert review_slot_unresolved(row) and not review_slot_awaiting(row)
    for row in ({"operation_state": "settled", "late_result_pending": True}, {"late_result_pending": True},
                {"operation_state": "not_dispatched"}, {"operation_state": "late_settled"}, {}, None, "pending_dispatch"):
        assert not review_slot_awaiting(row) and not review_slot_unresolved(row)


def test_census_puts_every_roster_row_in_exactly_one_typed_class():
    rows = [_ok("a"), _pending("w"), _pending("f", "in_flight"), _pending("l", "custody_lost"),
            _pending("u"), _skipped("k"), _failed("x"),
            {"slot_id": "legacy", "ok": False, "error": "transport died"},  # a pre-typed row is a failure
            {**_ok("stale"), "late_result_pending": True}]  # ok beside pending custody stays fail-closed
    wave = _wave(rows, historical_supplements=[
        _supplement("u"), _supplement("w", cycle=2), _supplement("f", state="in_flight")])
    census = plan_wave_slot_census(wave)
    names = ("answered", "awaiting", "unresolved", "uncollected", "skipped", "failed")
    assert {name: [r["slot_id"] for r in census[name]] for name in names} == {
        "answered": ["a"], "awaiting": ["w"], "unresolved": ["f", "l", "stale"],
        "uncollected": ["u"], "skipped": ["k"], "failed": ["x", "legacy"]}
    assert census["configured"] == len(rows) == sum(len(census[name]) for name in names)
    by_id = {id(r) for r in rows if r["slot_id"] in {"w", "f", "l", "stale"}}
    assert {id(r) for r in census["awaiting"] + census["unresolved"]} == by_id  # the row objects themselves
    assert wave["actors"] == rows and all(r is w for r, w in zip(rows, wave["actors"]))  # pure


@pytest.mark.parametrize("roster", [None, {}, "rows", 7, [], ["row", 3, None]])
def test_census_of_a_malformed_roster_classifies_nothing_and_never_raises(roster):
    census = plan_wave_slot_census({"actors": roster, "custody_pending": True, "paid": True})
    assert census == {"answered": [], "awaiting": [], "unresolved": [], "uncollected": [],
                      "skipped": [], "failed": [], "configured": 0}
    assert plan_wave_slot_census(None)["configured"] == 0


# ------------------------------------------------------------------ owner progress line


# ------------------------------------------------------------------ finalization disclosure


# ------------------------------------------------------------------ agent tool result


# ------------------------------------------------------------------ durable truth


# ------------------------------------------------------------------ the floor, through the real substrate
