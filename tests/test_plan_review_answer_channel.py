"""The plan-review ANSWER CHANNEL: answers merge by ``finding_id`` across calls (a later
answer supersedes only its own id; a same-call duplicate stays contradictory), an author
finish carries its answers, and an envelope sent beside answers is validated BEFORE anything
is recorded — the answers land first, then the envelope is reviewed with them in view.

Driven through the real engine, the real ``task_results``/artifact store and the fake
review substrate of ``tests.test_plan_review_engine``.
"""
from __future__ import annotations

import json

from ouroboros.task_results import (STATUS_RUNNING, load_plan_review_state, plan_review_wave,
    record_plan_review_dispositions, record_plan_review_wave, write_task_result)
from ouroboros.tools import plan_review as pr
from ouroboros.tools.plan_review_artifacts import authority_wave, read_wave
from tests.test_plan_review_engine import (  # noqa: F401
    CLEAN, DECK_SPEC, _call, _control, _finding, _state, _user_text, harness,
)


def _question(fid: str, breaks: str = "claim_1") -> dict:
    return _finding(fid, "need_evidence", breaks=breaks, summary="which one?", rec="")


def _item(fid: str, decision: str = "accept", rationale: str = "the author's word") -> dict:
    return {"finding_id": fid, "decision": decision, "rationale": rationale}


def _answer(ctx, fp: str, *items: dict) -> str:
    return pr._handle_plan_task(ctx, review_disposition={"review_fingerprint": fp, "items": list(items)})


def _two_questions(h):
    """A REVIEW_REQUIRED wave with two questions to the author (s1:q1, s2:q2); s3 is clean."""
    sub = h.install({"s1": json.dumps([_question("q1")]), "s2": json.dumps([_question("q2", "goal")]), "s3": CLEAN})
    ctx = h.make_ctx()
    out = _call(ctx)
    assert _control(out) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    return ctx, _state(h)["waves"][-1]["request_fingerprint"], sub


def _ids(wave: dict) -> list:
    return [(d["finding_id"], d["decision"]) for d in wave.get("dispositions") or []]


# ------------------------------------------------------------------ item 1: merge by id

def test_answers_across_calls_merge_and_close_the_wave(harness):  # noqa: F811
    """Call A answers one question, call B the other: B's closure sees A's answer, and the
    hot record and the exact artifact both hold BOTH answers (before: B erased A)."""
    ctx, fp, _sub = _two_questions(harness)
    first = _answer(ctx, fp, _item("s1:q1"))
    assert _control(first) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    second = _answer(ctx, fp, _item("s2:q2"))
    assert _control(second) == {"outcome": "GREEN", "closed": True}
    hot = _state(harness)["waves"][-1]
    assert _ids(hot) == [("s1:q1", "accept"), ("s2:q2", "accept")] and hot["closed"]
    exact = read_wave(harness.drive, ctx.task_id, hot["wave_artifact"])  # the raw artifact, no hot overlay
    assert _ids(exact) == [("s1:q1", "accept"), ("s2:q2", "accept")] and exact["closed"]


def test_a_later_answer_supersedes_only_its_own_id(harness):  # noqa: F811
    ctx, fp, _sub = _two_questions(harness)
    _answer(ctx, fp, _item("s1:q1"))
    _answer(ctx, fp, _item("s1:q1", "defer", "deferred to the owner"))
    assert _ids(_state(harness)["waves"][-1]) == [("s1:q1", "defer")]
    closed = _answer(ctx, fp, _item("s2:q2"))
    assert _control(closed) == {"outcome": "GREEN", "closed": True}
    assert _ids(_state(harness)["waves"][-1]) == [("s1:q1", "defer"), ("s2:q2", "accept")]


def test_same_call_duplicate_stays_contradictory(harness):  # noqa: F811
    """The guard stays active: accept AND reject for one id in ONE call is refused as a
    contradiction and keeps the finding open; a later single answer then closes it."""
    ctx, fp, _sub = _two_questions(harness)
    twice = _answer(ctx, fp, _item("s1:q1", "accept"), _item("s1:q1", "reject", "no"), _item("s2:q2"))
    assert "duplicate_disposition:s1:q1" in twice
    assert _control(twice) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    later = _answer(ctx, fp, _item("s1:q1"))
    assert _control(later) == {"outcome": "GREEN", "closed": True}
    assert _ids(_state(harness)["waves"][-1]) == [("s2:q2", "accept"), ("s1:q1", "accept")]


def test_the_durable_writer_merges_by_finding_id(tmp_path):
    """``record_plan_review_dispositions`` applied to prior ``[a]`` and new ``[b]`` stores
    ``[a, b]``; with new ``[a']`` it stores ``[a']`` — never replace-on-write."""
    write_task_result(tmp_path, "t1", STATUS_RUNNING, result="running")
    fp = "c" * 64
    record_plan_review_wave(tmp_path, "t1", {
        "schema_version": 2, "cycle_index": 1, "request_fingerprint": fp,
        "spec": {"goal": "g", "acceptance_claims": []}, "spec_hash": "b" * 64,
        "findings": [{"finding_id": "s1:q1", "class": "need_evidence", "breaks": "goal"},
                     {"finding_id": "s2:q2", "class": "need_evidence", "breaks": "goal"}],
        "aggregate": "REVIEW_REQUIRED", "closed": False, "dispositions": [], "paid": True,
    })
    a = _item("s1:q1")
    b = _item("s2:q2", "defer", "later")
    record_plan_review_dispositions(tmp_path, "t1", fingerprint=fp, dispositions=[a], closed=False)
    record_plan_review_dispositions(tmp_path, "t1", fingerprint=fp, dispositions=[b], closed=False)
    assert plan_review_wave(load_plan_review_state(tmp_path, "t1"), fp)["dispositions"] == [a, b]
    a2 = _item("s1:q1", "reject", "no")
    record_plan_review_dispositions(tmp_path, "t1", fingerprint=fp, dispositions=[a2], closed=False)
    assert plan_review_wave(load_plan_review_state(tmp_path, "t1"), fp)["dispositions"] == [b, a2]


# ------------------------------------------------- item 2: answers travel with an envelope

def test_author_finish_with_items_records_answers_then_selects_the_plan(harness, monkeypatch):  # noqa: F811
    """Advisory: one call answers the critic's findings AND selects the corrected plan; the
    answers land on the critic wave first, merged by id, and no new panel runs."""
    harness.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    transport = harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]),
                                 "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    first = _call(ctx)
    assert _control(first) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    critic_fp = _state(harness)["waves"][-1]["request_fingerprint"]
    spec = {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]}
    result = _call(ctx, spec, plan="Corrected complete plan.", review_disposition={
        "review_fingerprint": critic_fp, "author_action": "finish",
        "items": [_item("s1:f1", "reject", "the budget line is already approved")],
        "author_disposition": {"disposition": "partial", "rationale": "Corrected the budget."}})
    assert "Current author plan saved" in result
    assert "1 answer(s) recorded on the critic wave, merged by finding_id." in result
    after = load_plan_review_state(harness.drive, ctx.task_id)
    assert len(transport.calls) == 1 and after["cycles_paid"] == 1
    critic = next(w for w in after["waves"] if w["request_fingerprint"] == critic_fp)
    assert _ids(critic) == [("s1:f1", "reject")]
    assert critic["closed"] and critic["aggregate"] == "GREEN"  # advisory: a reasoned reject closes it
    assert after["current_attempt"]["author_subject"]["review_fingerprint"] == critic_fp


def test_finish_while_reviewers_run_is_refused_and_records_no_answers(harness, monkeypatch):  # noqa: F811
    """The kept guard fires first: a finish while reviewers are still running is refused, and
    the items it carried are NOT written (nothing lands when the call refuses)."""
    from ouroboros import review_records

    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    monkeypatch.setattr(review_records, "review_outcome_received", lambda *_a, **_kw: False)
    refused = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": fp, "author_action": "finish",
        "items": [_item("s1:f1", "reject", "already approved")],
        "author_disposition": {"disposition": "accepted", "rationale": "Proceed."}})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in refused and "reviewers are still running" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_an_invalid_author_record_beside_items_writes_nothing(harness, monkeypatch):  # noqa: F811
    """The author record is validated BEFORE the answers land: a schema-filled empty rationale
    refuses the whole call and the critic wave stays byte-identical (the working case, a valid
    record, is pinned by test_author_finish_with_items_records_answers_then_selects_the_plan)."""
    harness.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    refused = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]}, plan="Corrected.", review_disposition={
        "review_fingerprint": fp, "author_action": "finish",
        "items": [_item("s1:f1", "reject", "the budget line is already approved")],
        "author_disposition": {"disposition": "partial", "rationale": ""}})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in refused and "rationale" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before
    omitted = pr._handle_plan_task(ctx, review_disposition={  # stop with items and no author record at all
        "review_fingerprint": fp, "author_action": "stop", "items": [_item("s1:f1", "reject", "already approved")]})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in omitted
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_author_finish_items_are_validated_against_the_critic_wave(harness):  # noqa: F811
    """An unknown finding id beside a finish is refused as a whole, before any write."""
    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    refused = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": fp, "author_action": "stop", "items": [_item("s9:zz", "reject", "phantom")],
        "author_disposition": {"disposition": "partial", "rationale": "Stopping."}})
    assert "PLAN_AUTHOR_SUBJECT_INVALID" in refused and "unknown finding ids s9:zz" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_invalid_envelope_beside_answers_records_nothing(harness):  # noqa: F811
    """Validate-first: a malformed envelope beside answers is refused by its form, and neither
    the answers nor a superseding raw attempt are written (the answered wave stays current)."""
    harness.install({"s1": json.dumps([_question("q1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]
    refused = pr._handle_plan_task(ctx, goal="Ship the deck", plan="Changed prose.",
        spec={"in_scope": ["one table"]},  # the legacy form: no affected_paths list
        review_disposition={"review_fingerprint": fp, "items": [_item("s1:q1")]})
    assert "PLAN_RESOURCE_FORM_REQUIRED" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_a_store_failure_in_the_validate_first_prepare_is_typed_and_records_nothing(harness, monkeypatch):  # noqa: F811
    harness.install({"s1": json.dumps([_question("q1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    before = load_plan_review_state(harness.drive, ctx.task_id)
    fp = before["waves"][-1]["request_fingerprint"]

    def broken(*_a, **_k):
        raise OSError("artifact store unreadable")

    monkeypatch.setattr(pr, "_prepare_plan_inputs", broken)
    refused = pr._handle_plan_task(ctx, goal="Ship the deck", plan="Changed prose.", spec=DECK_SPEC,
                                   review_disposition={"review_fingerprint": fp, "items": [_item("s1:q1")]})
    assert "PLAN_REVIEW_STATE_INVALID" in refused and "artifact store unreadable" in refused
    assert load_plan_review_state(harness.drive, ctx.task_id) == before


def test_changed_envelope_with_items_records_the_answers_then_reviews_every_slot(harness, monkeypatch):  # noqa: F811
    """A CHANGED envelope with items is an ordinary full wave: the answers are stored on the
    answered wave first, every slot reviews the new envelope, and the packet's PRIOR CYCLES
    section carries the rationale."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _call(ctx)
    old_fp = _state(harness)["waves"][-1]["request_fingerprint"]
    sub2 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, plan="A revised outline with the budget line fixed.", review_disposition={
        "review_fingerprint": old_fp, "items": [_item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1", "s2", "s3"]
    assert "no slot was asked again by the answers (envelope_changed)" in result, "the typed note rides the full dispatch too"
    packet = _user_text(sub2.calls[0]["request"].messages[1]["content"])
    assert "PRIOR CYCLES" in packet and "the budget line is already approved" in packet
    state = _state(harness)
    old = next(w for w in state["waves"] if w["request_fingerprint"] == old_fp)
    assert _ids(old) == [("s1:f1", "reject")]
    assert state["cycles_paid"] == 2 and state["waves"][-1]["request_fingerprint"] != old_fp


# ------------------------------------ item 3: the addressed answer re-asks only the named seats

def _blocking(fid: str = "f1") -> str:
    return json.dumps([_finding(fid, "blocking", breaks="claim_1")])


def _objection(h, monkeypatch, *, cap: str = "5"):
    """Under blocking: s1 objects (below quorum), s2 and s3 are clean → REVIEW_REQUIRED; the gate
    holds while a paid cycle remains (at a spent cap finalization is released by the cap rail)."""
    from ouroboros.owner_hurry import force_plan_decision

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", cap)
    h.install({"s1": _blocking(), "s2": CLEAN, "s3": CLEAN})
    ctx = h.make_ctx()
    out = _call(ctx)
    assert _control(out) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert force_plan_decision(ctx, {}, enforcement="blocking")["allow"] is (cap == "1")
    return ctx, _state(h)["waves"][-1]["request_fingerprint"]


def _reject(ctx, fp: str, fid: str = "s1:f1") -> str:
    return _answer(ctx, fp, _item(fid, "reject", "the budget line is already approved"))


def _actors(h) -> dict:
    return {a["slot_id"]: a for a in _state(h)["waves"][-1]["actors"]}


def test_addressed_answer_reasks_only_the_named_slot(harness, monkeypatch):  # noqa: F811
    """The identical envelope WITH the reject asks s1 alone (one paid cycle); s2 and s3 keep
    their recorded answers at $0 (not_dispatched, replayed_from cycle 1); s1 retires its
    finding → GREEN, closed, and the blocking gate releases."""
    from ouroboros.owner_hurry import force_plan_decision

    ctx, fp = _objection(harness, monkeypatch)
    _reject(ctx, fp)
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert [s.slot_id for s in sub.calls[0]["slots"]] == ["s1"] and len(sub.calls) == 1
    state = _state(harness)
    assert state["cycles_paid"] == 2 and state["waves"][-1]["request_fingerprint"] == fp
    actors = _actors(harness)
    for sid in ("s2", "s3"):
        assert actors[sid]["replayed_from"]["cycle_index"] == 1 and actors[sid]["cost"] == 0.0
        assert actors[sid]["operation_state"] == "not_dispatched" and actors[sid]["ok"]
    assert "replayed_from" not in actors["s1"] and actors["s1"]["ok"]
    assert state["waves"][-1]["addressed"] == {"slots": ["s1"], "finding_ids": ["s1:f1"], "kept": ["s2", "s3"],
                                               "wave_artifact": state["waves"][-1]["previous_wave_artifact"]}
    assert state["waves"][-1]["previous_wave_artifact"].get("path")
    assert "kept its cycle-1 answer at $0" in result and "**Addressed answer:** asked again s1 on s1:f1; kept at $0: s2, s3." in result
    assert force_plan_decision(ctx, {}, enforcement="blocking")["allow"] is True


def test_a_still_open_objector_holds_under_blocking(harness, monkeypatch):  # noqa: F811
    """The two-sided pair: s1 re-emits its finding after reading the answer → the wave stays
    open and the gate holds, although both kept answers are clean (a single objector is never
    outvoted under blocking)."""
    from ouroboros.owner_hurry import force_plan_decision

    ctx, fp = _objection(harness, monkeypatch)
    _reject(ctx, fp)
    sub = harness.install({"s1": _blocking(), "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(result) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert [s.slot_id for s in sub.calls[0]["slots"]] == ["s1"]
    wave = _state(harness)["waves"][-1]
    assert wave["paid"] and _state(harness)["cycles_paid"] == 2
    assert [f["finding_id"] for f in wave["findings"] if f["class"] == "blocking"] == ["s1:f1"]
    assert wave["dispositions"] == [], "a re-asked seat's finding starts undispositioned"
    assert force_plan_decision(ctx, {}, enforcement="blocking")["allow"] is False


def test_addressed_slot_without_an_answer_keeps_its_findings(harness, monkeypatch):  # noqa: F811
    """D4 / Q-v: s1 answers garbage to the re-ask → its recorded finding STANDS (carried,
    disclosed), the wave stays open, the cycle is paid; absence never retires a finding."""
    from ouroboros.owner_hurry import force_plan_decision

    ctx, fp = _objection(harness, monkeypatch)
    _reject(ctx, fp)
    harness.install({"s1": "garbage, not an array", "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(result) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    wave = _state(harness)["waves"][-1]
    assert wave["paid"] and _state(harness)["cycles_paid"] == 2
    assert [f["finding_id"] for f in wave["findings"]] == ["s1:f1"]
    assert wave["actors"][0]["slot_id"] == "s1" and not wave["actors"][0]["ok"]
    assert "findings_carried_absent_answer:1" in wave["actors"][0]["disclosures"]
    assert _ids(wave) == [("s1:f1", "reject")], "a finding carried for absence keeps the mind's recorded answer"
    assert "did not answer; its earlier finding is still listed" in result
    assert force_plan_decision(ctx, {}, enforcement="blocking")["allow"] is False


def test_the_last_answer_that_closes_the_wave_still_dispatches_the_re_ask(harness, monkeypatch):  # noqa: F811
    """Closure never cancels a requested exchange: the accept closes the question wave, yet the
    same call's envelope re-asks the seat that asked (was_open is judged BEFORE the answers)."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    harness.install({"s1": json.dumps([_question("q1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [_item("s1:q1")]})
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert [s.slot_id for s in sub.calls[0]["slots"]] == ["s1"] and _state(harness)["cycles_paid"] == 2
    assert _state(harness)["waves"][-1]["addressed"]["slots"] == ["s1"]


def test_kept_answers_carry_their_dispositions(harness, monkeypatch):  # noqa: F811
    """s2's question was answered at $0 earlier; when s1 is re-asked, s2's kept row keeps that
    answer, so the wave closes once s1 retires."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    harness.install({"s1": _blocking(), "s2": json.dumps([_question("q2", "goal")]), "s3": CLEAN})
    ctx = harness.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    _answer(ctx, fp, _item("s2:q2"))
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(result) == {"outcome": "GREEN", "closed": True}
    assert [s.slot_id for s in sub.calls[0]["slots"]] == ["s1"]
    wave = _state(harness)["waves"][-1]
    assert _ids(wave) == [("s2:q2", "accept")]
    assert [f["finding_id"] for f in wave["findings"]] == ["s2:q2"]  # s2's answer re-parsed from its record


def test_unaddressable_waves_follow_their_ordinary_rule(harness, monkeypatch):  # noqa: F811
    """No wave is asked again by structure alone: a closed note-only wave replays with the typed
    note; a DEGRADED wave (garbage answers, no structural epoch) re-dispatches every seat as the
    ordinary rule says; a spent cap refuses the send and keeps the answers recorded."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    note = json.dumps([_finding("n1", "note", summary="a thought", rec="")])
    harness.install({"s1": note, "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "GREEN", "closed": True}
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    closed = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [_item("s1:n1", "accept", "noted")]})
    assert not sub.calls and "answers_not_addressed: no slot was asked again by the answers (wave_closed)" in closed
    assert _ids(_state(harness)["waves"][-1]) == [("s1:n1", "accept")]
    # DEGRADED, no epoch: the identical envelope with items re-dispatches EVERY seat (no note, no kept row).
    degraded_ctx = harness.make_ctx(task_id="task-degraded")
    harness.install({"s1": _blocking(), "s2": "garbage", "s3": "garbage"})
    assert _control(_call(degraded_ctx)) == {"outcome": "DEGRADED", "closed": False}
    fp2 = _state(harness, "task-degraded")["waves"][-1]["request_fingerprint"]
    sub2 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    fresh = _call(degraded_ctx, review_disposition={"review_fingerprint": fp2, "items": [
        _item("s1:f1", "reject", "already approved")]})
    assert _control(fresh) == {"outcome": "GREEN", "closed": True}
    assert [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1", "s2", "s3"]
    assert "answers_not_addressed: no slot was asked again by the answers (no_quorum_to_keep)" in fresh
    assert "addressed" not in _state(harness, "task-degraded")["waves"][-1]


def test_an_envelope_beside_answers_to_a_closed_wave_is_still_reviewed(harness, monkeypatch):  # noqa: F811
    """A closed, immutable wave takes no answers, but the envelope beside them is reviewed: the
    IDENTICAL envelope replays the closed wave free with `wave_closed`; a CHANGED envelope goes to
    every seat as an ordinary wave (the old wave untouched, the typed note names why nothing was
    addressed) — never the old closure returned for new bytes."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    harness.install({"s1": json.dumps([_question("q1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(_answer(ctx, fp, _item("s1:q1"))) == {"outcome": "GREEN", "closed": True}
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    identical = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [_item("s1:q1", "defer", "later")]})
    assert _control(identical) == {"outcome": "GREEN", "closed": True} and not sub.calls
    assert "no slot was asked again by the answers (wave_closed)" in identical and "already_closed" not in identical
    assert _ids(_state(harness)["waves"][-1]) == [("s1:q1", "accept")], "the immutable wave took no answer"
    sub2 = harness.install({"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    changed = _call(ctx, plan="A revised outline with a new budget line.",
                    review_disposition={"review_fingerprint": fp, "items": [_item("s1:q1", "defer", "later")]})
    assert _control(changed) == {"outcome": "REVIEW_REQUIRED", "closed": False}, "the new bytes got their own review"
    assert [s.slot_id for s in sub2.calls[0]["slots"]] == ["s1", "s2", "s3"] and "already_closed" not in changed
    assert "no slot was asked again by the answers (envelope_changed)" in changed
    state = _state(harness)
    assert state["cycles_paid"] == 2 and state["waves"][-1]["request_fingerprint"] != fp
    old = next(w for w in state["waves"] if w["request_fingerprint"] == fp)
    assert old["closed"] and _ids(old) == [("s1:q1", "accept")]
    assert "the named wave is closed and takes no answers; reviewing the envelope." in "\n".join(harness.progress)


def test_a_spent_cap_refuses_the_re_ask_and_keeps_the_answers(harness, monkeypatch):  # noqa: F811
    ctx, fp = _objection(harness, monkeypatch, cap="1")
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    result = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert "PLAN_REVIEW_CYCLES_EXHAUSTED" in result and not sub.calls
    assert _ids(_state(harness)["waves"][-1]) == [("s1:f1", "reject")] and _state(harness)["cycles_paid"] == 1


def test_an_all_skipped_addressed_collection_keeps_the_paid_predecessor_reachable(harness, monkeypatch):  # noqa: F811
    """Async route: the re-asked seat settles as a $0 refusal (daemon unavailable) and the kept
    rows cost nothing, so the collected addressed wave is UNPAID and replaced the paid cycle-1
    wave in the hot index at the barrier. The next same-spec review must still start from that
    paid wave (through the recorded predecessor pointer): s1's objection stands, never a false
    GREEN. Two-sided: the sync all-skipped attempt (health skip) preserves the predecessor by the
    writer rule, pinned in test_plan_review_engine."""
    from tests.test_plan_review_reconciliation import _install_barrier_substrate

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    calls: list = []
    _install_barrier_substrate(monkeypatch, calls, texts={"s1": _blocking()})
    ctx = harness.make_ctx()
    _call(ctx)
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(_answer(ctx, fp)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    _reject(ctx, fp)
    calls2: list = []
    _install_barrier_substrate(monkeypatch, calls2, texts={"s1": CLEAN}, refused={"s1"})
    pending = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(pending) == {"outcome": "DEGRADED", "closed": False}
    settled = _call(ctx, review_disposition={"review_fingerprint": fp, "items": []})
    state = _state(harness)
    assert state["cycles_paid"] == 1 and state["waves"][-1]["paid"] is False, "nothing was sent: nothing is charged"
    assert _control(settled) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    assert not any(w.get("paid") for w in state["waves"]), "the paid cycle-1 wave left the hot index at the barrier"
    sub = harness.install({"s1": "garbage, not an array", "s2": CLEAN, "s3": CLEAN})
    later = _call(ctx, plan="A revised outline with the budget line fixed.")  # same spec, changed prose
    assert _control(later) == {"outcome": "REVIEW_REQUIRED", "closed": False}, "the objection stands through the pointer"
    assert len(sub.calls) == 1
    wave = _state(harness)["waves"][-1]
    assert wave["previous_fingerprint"] == fp and [f["finding_id"] for f in wave["findings"]] == ["s1:f1"]
    assert wave["actors"][0]["slot_id"] == "s1" and "findings_carried_absent_answer:1" in wave["actors"][0]["disclosures"]


def test_a_failed_re_ask_on_the_barrier_route_keeps_the_recorded_answer(harness, monkeypatch):  # noqa: F811
    """Production returns at the dispatch barrier: the re-asked seat is collected later and
    answers garbage. The carried finding must keep the mind's recorded answer (it lives on the
    exact predecessor while the wave is in flight) — the inline pin of the same rule is
    test_addressed_slot_without_an_answer_keeps_its_findings."""
    from tests.test_plan_review_reconciliation import _install_barrier_substrate

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    calls: list = []
    _install_barrier_substrate(monkeypatch, calls, texts={"s1": _blocking()})
    ctx = harness.make_ctx()
    _call(ctx)
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(_answer(ctx, fp)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    _reject(ctx, fp)
    calls2: list = []
    _install_barrier_substrate(monkeypatch, calls2, texts={"s1": "garbage, not an array"})
    pending = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(pending) == {"outcome": "DEGRADED", "closed": False}
    settled = _call(ctx, review_disposition={"review_fingerprint": fp, "items": []})
    assert _control(settled) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    wave = _state(harness)["waves"][-1]
    assert wave["paid"] and _state(harness)["cycles_paid"] == 2
    assert [(f["finding_id"], bool(f.get("carried_absent_answer"))) for f in wave["findings"]] == [("s1:f1", True)]
    assert _ids(wave) == [("s1:f1", "reject")], "the recorded answer rides the collected wave"
    assert _ids(authority_wave(harness.drive, ctx.task_id, wave)) == [("s1:f1", "reject")]


def test_a_retry_after_a_refused_addressed_attempt_is_collectable(harness, monkeypatch):  # noqa: F811
    """Barrier route: the first addressed re-ask is refused at $0 (daemon unreachable), so its
    collected wave is unpaid and the retry shares its cycle index and fingerprint. The retry is a
    DISTINCT wave (its own artifact): its collection settles it, GREEN closed, one cycle charged
    — never PLAN_REVIEW_CUSTODY_INVALID and never a wedged task. Two consecutive refusals then
    a changed-prose envelope still find the paid cycle-1 wave through the pointer chain."""
    from tests.test_plan_review_reconciliation import _install_barrier_substrate

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    calls: list = []
    _install_barrier_substrate(monkeypatch, calls, texts={"s1": _blocking()})
    ctx = harness.make_ctx()
    _call(ctx)
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(_answer(ctx, fp)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    _reject(ctx, fp)
    ask = {"review_fingerprint": fp, "items": [_item("s1:f1", "reject", "the budget line is already approved")]}
    for _attempt in range(2):  # two refused attempts in a row
        _install_barrier_substrate(monkeypatch, [], texts={"s1": CLEAN}, refused={"s1"})
        assert _control(_call(ctx, review_disposition=ask)) == {"outcome": "DEGRADED", "closed": False}
        settled = _call(ctx, review_disposition={"review_fingerprint": fp, "items": []})
        assert _control(settled) == {"outcome": "REVIEW_REQUIRED", "closed": False} and "CUSTODY_INVALID" not in settled
    assert _state(harness)["cycles_paid"] == 1
    calls3: list = []
    _install_barrier_substrate(monkeypatch, calls3, texts={"s1": CLEAN})
    assert _control(_call(ctx, review_disposition=ask)) == {"outcome": "DEGRADED", "closed": False}
    assert calls3[0]["slots"] == ["s1"]
    collected = _call(ctx, review_disposition={"review_fingerprint": fp, "items": []})
    assert _control(collected) == {"outcome": "GREEN", "closed": True}, collected[-600:]
    state = _state(harness)
    assert state["cycles_paid"] == 2 and state["waves"][-1]["paid"] and state["waves"][-1]["closed"]
    assert state["current_attempt"]["status"] == "open"


def test_predecessor_identity_is_the_artifact_when_both_references_are_known():
    """Two attempts of one cycle share (cycle_index, fingerprint); with both references known
    the artifact decides: a pointer naming the wave's OWN artifact is the wave itself (refused
    as self-naming), a pointer naming another artifact is a distinct predecessor; without both
    references the pair is the only identity there is."""
    from ouroboros.tools.plan_review_artifacts import _same_wave

    own = {"root": "artifact_store", "path": "waves/attempt-2.json"}
    other = {"root": "artifact_store", "path": "waves/attempt-1.json"}
    wave = {"cycle_index": 2, "request_fingerprint": "a" * 64, "wave_artifact": own}
    earlier = {"cycle_index": 2, "request_fingerprint": "a" * 64}
    assert _same_wave(earlier, wave, pointer=own) is True
    assert _same_wave(earlier, wave, pointer=other) is False
    assert _same_wave(earlier, {"cycle_index": 2, "request_fingerprint": "a" * 64}, pointer=other) is True  # pair fallback
    assert _same_wave({**earlier, "cycle_index": 1}, wave, pointer={}) is False


def test_addressed_helper_names_every_typed_reason():
    from ouroboros.tools.plan_review_artifacts import ADDRESSED_REASONS, addressed_slots

    fp = "a" * 64
    wave = {"request_fingerprint": fp, "aggregate": "REVIEW_REQUIRED", "closed": False,
            "findings": [{"finding_id": "s1:f1", "slot": "s1", "class": "blocking"},
                         {"finding_id": "s3:q1", "slot": "s3", "class": "need_evidence"}],
            "actors": [{"slot_id": "s1"}, {"slot_id": "s2"}, {"slot_id": "s3"}]}
    ask = {"review_fingerprint": fp, "finding_ids": ["s3:q1", "s1:f1"], "was_open": True}
    assert addressed_slots(wave, ask, fingerprint=fp) == (["s1", "s3"], "")
    assert addressed_slots(wave, None, fingerprint=fp) == ([], "")
    assert addressed_slots(None, ask, fingerprint=fp) == ([], "envelope_changed")
    assert addressed_slots(wave, ask, fingerprint="b" * 64) == ([], "envelope_changed")
    assert addressed_slots(wave, {**ask, "review_fingerprint": "b" * 64}, fingerprint=fp) == ([], "not_the_answered_wave")
    assert addressed_slots(wave, {**ask, "was_open": False}, fingerprint=fp) == ([], "wave_closed")
    assert addressed_slots({**wave, "aggregate": "DEGRADED"}, ask, fingerprint=fp) == ([], "no_quorum_to_keep")
    assert addressed_slots(wave, {**ask, "finding_ids": []}, fingerprint=fp) == ([], "no_finding_named")
    assert addressed_slots(wave, {**ask, "finding_ids": ["s9:zz"]}, fingerprint=fp) == ([], "no_finding_named")
    assert set(ADDRESSED_REASONS) == {"envelope_changed", "not_the_answered_wave", "wave_closed", "no_quorum_to_keep", "no_finding_named"}


def test_addressed_wave_collects_after_the_barrier(harness, monkeypatch):  # noqa: F811
    """The async route: the re-asked seat returns at the barrier (pending), the kept rows ride
    the custody-pending wave, and the $0 collection settles it — kept rows are recognized by
    ``replayed_from`` (an ok row without dispatch and without it is still refused)."""
    from ouroboros.owner_hurry import force_plan_decision
    from tests.test_plan_review_reconciliation import _install_barrier_substrate

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    calls: list = []
    _install_barrier_substrate(monkeypatch, calls, texts={"s1": _blocking()})
    ctx = harness.make_ctx()
    first = _call(ctx)
    fp = _state(harness)["waves"][-1]["request_fingerprint"]
    assert _control(first) == {"outcome": "DEGRADED", "closed": False}
    collected = _answer(ctx, fp)  # $0 collection of the settled first cycle
    assert _control(collected) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    _reject(ctx, fp)
    calls2: list = []
    _install_barrier_substrate(monkeypatch, calls2, texts={"s1": CLEAN})
    pending = _call(ctx, review_disposition={"review_fingerprint": fp, "items": [
        _item("s1:f1", "reject", "the budget line is already approved")]})
    assert _control(pending) == {"outcome": "DEGRADED", "closed": False}
    assert calls2[0]["slots"] == ["s1"] and calls2[0]["reconcile_only"] is False
    wave = _state(harness)["waves"][-1]
    assert wave["custody_pending"] and wave["addressed"]["kept"] == ["s2", "s3"]
    assert "2 recorded answers kept at $0" in "\n".join(harness.progress)
    # A5: custody pending + empty items + envelope collects, then follows the ordinary rule.
    settled = _call(ctx, review_disposition={"review_fingerprint": fp, "items": []})
    assert _control(settled) == {"outcome": "GREEN", "closed": True}
    assert "answers_not_addressed" not in settled, "the in-flight collection is decided before the addressed rule: no note"
    assert calls2[1]["slots"] == ["s1"] and calls2[1]["reconcile_only"] is True and len(calls2) == 2
    state = _state(harness)
    assert state["cycles_paid"] == 2 and state["waves"][-1]["closed"]
    settled = state["waves"][-1]  # the collection re-records the addressed cycle it settles: the lineage survives it
    assert settled["addressed"] == wave["addressed"] and settled["previous_wave_artifact"] == wave["addressed"]["wave_artifact"]
    assert authority_wave(harness.drive, ctx.task_id, settled)["addressed"] == wave["addressed"]
    actors = _actors(harness)
    assert actors["s2"]["replayed_from"]["cycle_index"] == 1 and actors["s2"]["cost"] == 0.0 and actors["s2"]["ok"]
    assert actors["s1"]["ok"] and "replayed_from" not in actors["s1"]
    assert force_plan_decision(ctx, {}, enforcement="blocking")["allow"] is True
