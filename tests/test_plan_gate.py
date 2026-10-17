"""The planning gate at the one finalization seam: ``owner_hurry.force_plan_decision``
over the plan record (and the read-only import of a legacy v2 state), the reminder and
disclosure texts, the Swarm obligation, the forced-finalization identity/facts and the
outcome axis. Both directions on every rule."""
from __future__ import annotations

import json
import queue
from types import SimpleNamespace


from ouroboros import owner_hurry as oh
from ouroboros import plan_record
from tests._store_shared import seed_result

SEAT, CRITIC = "triad_a", "critic_x"


def _ctx(tmp_path, *, task_id="t-plan", force_plan=False, latched=False):
    ctx = SimpleNamespace(drive_root=str(tmp_path), budget_drive_root=str(tmp_path), task_id=task_id,
                          task_metadata={"force_plan": True} if force_plan else {}, task_contract={}, task_attempt=1,
                          event_queue=queue.Queue(), emit_progress_fn=lambda *_a, **_k: None)
    if latched:
        ctx._owner_hurry_latch = {"msg_id": "hurry:x", "requested_at": "", "reason": oh.REASON_OWNER_HURRY}
    return ctx


def _seed(tmp_path, task_id="t-plan"):
    seed_result(tmp_path, task_id, {"task_id": task_id, "status": "running"})


def _plan(tmp_path, task_id="t-plan", *, participants=(), with_revision=True):
    """A durable record: one revision and, when participants are given, one ask over it."""
    _seed(tmp_path, task_id)
    if with_revision:
        fields = plan_record.compose_revision(None, goal="ship", plan="do it", done_when=["five slides"], affected_paths=[])
        plan_record.append_revision(tmp_path, task_id, fields, body=False)
    if participants:
        plan_record.append_ask(tmp_path, task_id, {"ask_id": "a1", "ts": "t", "revision": 1, "message": "look",
                                                   "participants": list(participants)})
    return plan_record.load_plan_record(tmp_path, task_id)


def _participant(seat, *, owner_marked=True, reply=None, status=None, dispatched=True):
    row = {"seat_id": seat, "model": "m/x", "route": "api_chat", "owner_marked": owner_marked, "dispatched": dispatched,
           "status": status or ("answered" if reply is not None else "pending")}
    if reply is not None:
        row["reply"] = {"prose": "", "objections": [], "withdrawn": [], **reply}
    return row


OBJECTION = {"objections": [{"id": "o1", "text": "the bar is too low"}]}


# ---------------------------------------------------------------- force_plan_decision

def test_a_task_without_a_plan_is_not_required_and_a_held_one_is_held(tmp_path):
    _seed(tmp_path)
    assert oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")["status"] == "not_required"
    _plan(tmp_path, participants=[_participant(SEAT, reply=OBJECTION), _participant("triad_b", reply={"prose": "fine"})])
    held = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    assert held["required"] and held["self_opened"] and held["allow"] is False and held["status"] == "held"
    assert held["holding"] == [f"{SEAT}:o1"] and held["plan_review_class"] == "objections_open"
    advisory = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="advisory")
    assert advisory["allow"] is True and advisory["status"] == "objections_open"
    railed = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking", hard_rail="round_limit")
    assert railed["allow"] is True and railed["status"] == "rail_degraded" and railed["reason"] == "round_limit"
    hurried = oh.force_plan_decision(_ctx(tmp_path, latched=True), {}, enforcement="blocking")
    assert hurried["allow"] is True and hurried["owner_hurry_local_advisory"] is True and hurried["configured_enforcement"] == "blocking"


def test_silence_critics_and_withdrawals_release_the_gate(tmp_path):
    _plan(tmp_path, participants=[_participant(SEAT), _participant(CRITIC, owner_marked=False, reply=OBJECTION)])
    decision = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    assert decision["allow"] is True and decision["status"] == "objections_open" and decision["holding"] == []
    assert decision["pending_asks"] == ["a1"] and "review_only_awaited" in decision and decision["review_only_awaited"] is False
    _plan(tmp_path, "t-await", participants=[_participant(SEAT)])
    awaiting = oh.force_plan_decision(_ctx(tmp_path, task_id="t-await"), {}, enforcement="blocking")
    assert awaiting["status"] == "awaiting" and awaiting["review_only_awaited"] is True and awaiting["allow"] is True
    record = _plan(tmp_path, "t-rel", participants=[_participant(SEAT, reply=OBJECTION)])
    plan_record.append_ask(tmp_path, "t-rel", {"ask_id": "a2", "ts": "t", "revision": 1, "message": "",
                                               "participants": [_participant(SEAT, reply={"withdrawn": [{"id": "o1", "why": "fixed"}]})]})
    assert oh.force_plan_decision(_ctx(tmp_path, task_id="t-rel"), {}, enforcement="blocking")["status"] == "clear"
    assert record["asks"][0]["participants"][0]["reply"]["objections"]  # the earlier objection is history, not erased


def test_cyber_pro_never_holds(tmp_path, monkeypatch):
    _plan(tmp_path, participants=[_participant(SEAT, reply=OBJECTION)])
    monkeypatch.setattr("ouroboros.tools.review_helpers.review_enforcement_blocks", lambda *_a, **_k: False)
    decision = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    assert decision["allow"] is True and decision["decision_authority"] == "cyber_pro" and decision["status"] == "objections_open"


def test_force_plan_is_met_only_by_an_ask_that_reached_a_reviewer(tmp_path):
    _seed(tmp_path)
    unmet = oh.force_plan_decision(_ctx(tmp_path, force_plan=True), {}, enforcement="blocking")
    assert unmet["required"] and not unmet["self_opened"] and unmet["allow"] is False and unmet["status"] == "force_plan_unmet"
    assert oh.force_plan_decision(_ctx(tmp_path, force_plan=True), {}, enforcement="advisory")["allow"] is False
    assert oh.force_plan_decision(_ctx(tmp_path, force_plan=True, latched=True), {}, enforcement="blocking")["allow"] is False
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, force_plan=True)) == {"unmet": True, "source": "operator"}
    _plan(tmp_path, participants=[_participant(SEAT, dispatched=False, status="not_dispatched")])
    assert oh.force_plan_decision(_ctx(tmp_path, force_plan=True), {}, enforcement="blocking")["status"] == "force_plan_unmet"
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, force_plan=True))["unmet"] is True
    _plan(tmp_path, "t-met", participants=[_participant(SEAT)])
    met = oh.force_plan_decision(_ctx(tmp_path, task_id="t-met", force_plan=True), {}, enforcement="blocking")
    assert met["force_plan_met"] is True and met["allow"] is True and met["status"] == "awaiting"
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, task_id="t-met", force_plan=True)) == {"unmet": False, "reason": "plan_asked"}
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, task_id="t-met")) == {"unmet": False, "reason": "not_required"}


def test_an_unreadable_record_is_engaged_not_absent(tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setattr("ouroboros.plan_record.load_plan_record",
                        lambda *_a, **_k: (_ for _ in ()).throw(plan_record.PlanRecordInvalid("PLAN_RECORD_INVALID: torn")))
    decision = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    assert decision["required"] and decision["allow"] is False and decision["status"] == "unreadable"
    assert oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="advisory")["allow"] is False
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, force_plan=True)) == {"unmet": False, "reason": "plan_record_unreadable"}
    assert "cannot be read" in oh.plan_review_reminder(decision)


def test_an_unreadable_legacy_state_is_engaged_not_absent(tmp_path, monkeypatch):
    """I-17 fail-closed symmetry for the earlier-schema authority: a gate that cannot read a task's
    v1/v2 ``plan_review_state`` is engaged, never absent; the same task read cleanly needs nothing."""
    _seed(tmp_path)
    assert oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")["status"] == "not_required"
    monkeypatch.setattr("ouroboros.task_results.load_plan_review_state",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("PLAN_REVIEW_STATE_INVALID: corrupt")))
    decision = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    assert decision["required"] is True and decision["allow"] is False and decision["status"] == "unreadable"


# ---------------------------------------------------------------- legacy v2 records

def _v2(fingerprint="f" * 64, *, closed, blocking=("triad_old",)):
    return {"schema_version": 2, "cycles_paid": 1, "need_evidence_seen": [], "waves_omitted": 0,
            "current_attempt": {"fingerprint": fingerprint, "status": "open", "reason": ""},
            "waves": [{"request_fingerprint": fingerprint, "closed": closed, "aggregate": "GREEN" if closed else "REVISE_PLAN",
                       "paid": True, "cycle_index": 1, "spec": {"goal": "g", "acceptance_claims": ["old claim"]}, "dispositions": [],
                       "findings": [{"finding_id": f"{seat}:f1", "class": "blocking", "slot": seat, "summary": "wrong shape",
                                     "breaks": "goal"} for seat in blocking],
                       "actors": [{"slot_id": seat, "model": "m/old", "ok": True} for seat in blocking]}]}


def test_an_in_flight_legacy_blocking_finding_holds_until_its_seat_withdraws_through_the_new_organ(tmp_path):
    seed_result(tmp_path, "t-legacy", {"task_id": "t-legacy", "status": "running", "plan_review_state": _v2(closed=False)})
    held = oh.force_plan_decision(_ctx(tmp_path, task_id="t-legacy"), {}, enforcement="blocking")
    assert held["status"] == "held" and held["source"] == "legacy_v2" and held["holding"] == ["triad_old:triad_old:f1"]
    assert held["open_objections"][0]["legacy"] is True
    assert oh.force_plan_decision(_ctx(tmp_path, task_id="t-legacy"), {}, enforcement="advisory")["allow"] is True
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, task_id="t-legacy", force_plan=True))["unmet"] is False
    fields = plan_record.compose_revision(None, goal="g", plan="new", done_when=[], affected_paths=[])
    plan_record.append_revision(tmp_path, "t-legacy", fields, body=False)
    plan_record.append_ask(tmp_path, "t-legacy", {"ask_id": "a1", "ts": "t", "revision": 1, "message": "",
                                                  "participants": [_participant("triad_old", reply={"withdrawn": [{"id": "triad_old:f1", "why": "fixed"}]})]})
    released = oh.force_plan_decision(_ctx(tmp_path, task_id="t-legacy"), {}, enforcement="blocking")
    assert released["status"] == "clear" and released["source"] == "plan_record"
    seed_result(tmp_path, "t-closed", {"task_id": "t-closed", "status": "running", "plan_review_state": _v2(closed=True)})
    assert oh.force_plan_decision(_ctx(tmp_path, task_id="t-closed"), {}, enforcement="blocking")["status"] == "clear"


# ---------------------------------------------------------------- texts

def test_reminder_names_the_holding_objections_and_the_exits(tmp_path):
    _plan(tmp_path, participants=[_participant(SEAT, reply=OBJECTION), _participant(CRITIC, owner_marked=False, reply={"objections": [{"id": "k1", "text": "critic"}]})])
    decision = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    reminder = oh.plan_review_reminder(decision)
    assert reminder.startswith("[PLAN_REVIEW_HOLD]") and f"{SEAT}/o1" in reminder and "the bar is too low" in reminder
    assert "critic_x/k1" not in reminder  # a critic's objection never holds, so it is not what the hold names
    assert "ask=[<its seat id>]" in reminder and "finish_task(action='stop')" in reminder
    _seed(tmp_path, "t-force")
    unmet = oh.plan_review_reminder(oh.force_plan_decision(_ctx(tmp_path, task_id="t-force", force_plan=True), {}, enforcement="blocking"))
    assert "planning obligation" in unmet and "ask='council'" in unmet


def test_disclosure_speaks_only_when_the_council_was_not_done(tmp_path):
    _seed(tmp_path)
    assert oh.plan_review_disclosure(oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")) == ""
    _plan(tmp_path, participants=[_participant(SEAT, reply={"prose": "fine"})])
    clear = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    assert clear["status"] == "clear" and oh.plan_review_disclosure(clear) == "" and oh.plan_review_disclosure(clear, "round_limit") == ""
    _plan(tmp_path, "t-open", participants=[_participant(SEAT, reply=OBJECTION)])
    advisory = oh.plan_review_disclosure(oh.force_plan_decision(_ctx(tmp_path, task_id="t-open"), {}, enforcement="advisory"))
    assert advisory.startswith("\n\n⚠️ Plan review: 1 open objection remains open") and "advisory enforcement" in advisory and "the bar is too low" in advisory
    hurried = oh.plan_review_disclosure(oh.force_plan_decision(_ctx(tmp_path, task_id="t-open", latched=True), {}, enforcement="blocking"))
    assert "owner's hurry" in hurried
    railed = oh.plan_review_disclosure(oh.force_plan_decision(_ctx(tmp_path, task_id="t-open"), {}, enforcement="blocking", hard_rail="round_limit"), "round_limit")
    assert railed.startswith("\n\n⚠️ Blocking plan review is open (1 open objection") and "`round_limit`" in railed
    _plan(tmp_path, "t-wait", participants=[_participant(SEAT)])
    waiting = oh.plan_review_disclosure(oh.force_plan_decision(_ctx(tmp_path, task_id="t-wait"), {}, enforcement="advisory"))
    assert "had not arrived" in waiting and "a1" in waiting


# ---------------------------------------------------------------- forced finalization identity and facts

def test_gate_identity_and_facts_follow_the_typed_fields(tmp_path):
    from ouroboros.loop_forced_finalization import _plan_gate_facts, _plan_gate_identity

    _plan(tmp_path, participants=[_participant(SEAT, reply=OBJECTION)])
    held = oh.force_plan_decision(_ctx(tmp_path), {}, enforcement="blocking")
    hurried = oh.force_plan_decision(_ctx(tmp_path, latched=True), {}, enforcement="blocking")
    assert _plan_gate_identity(held) != _plan_gate_identity(hurried) and f"{SEAT}:o1" in _plan_gate_identity(held)
    facts = _plan_gate_facts(held)
    assert "plan_review_open=true" in facts and "plan_gate_status=held" in facts and "plan_holding=1" in facts
    assert "owner_hurry_local_advisory=true" in _plan_gate_facts(hurried) and "configured_enforcement=blocking" in _plan_gate_facts(hurried)
    _plan(tmp_path, "t-clean", participants=[_participant(SEAT, reply={"prose": "ok"})])
    assert _plan_gate_facts(oh.force_plan_decision(_ctx(tmp_path, task_id="t-clean"), {}, enforcement="blocking")) == ""
    assert _plan_gate_facts({"required": False}) == ""


def test_enforce_swarm_actions_holds_and_releases_on_the_decision(tmp_path, monkeypatch):
    import ouroboros.loop as loop_mod

    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "blocking")
    _plan(tmp_path, participants=[_participant(SEAT, reply=OBJECTION)])

    def run(ctx):
        trace = {"reasoning_notes": []}
        held = loop_mod._enforce_swarm_actions("answer", [], SimpleNamespace(_ctx=ctx), trace, lambda _m: None)
        return held, trace

    held, trace = run(_ctx(tmp_path))
    assert held is True and trace["force_plan_decision"]["status"] == "held" and f"{SEAT}/o1" in trace["reasoning_notes"][0]
    released, trace = run(_ctx(tmp_path, latched=True))
    assert released is False and trace["force_plan_decision"]["owner_hurry_local_advisory"] is True
    _plan(tmp_path, "t-clean", participants=[_participant(SEAT, reply={"prose": "ok"})])
    assert run(_ctx(tmp_path, task_id="t-clean"))[0] is False


# ---------------------------------------------------------------- outcome axis

def test_open_objections_and_pending_asks_ride_the_execution_axis_and_never_fail_the_objective():
    from ouroboros.outcomes import derive_loop_outcome

    objections = {"required": True, "allow": True, "status": "objections_open", "enforcement": "advisory",
                  "open_objections": [{"seat_id": SEAT, "id": "o1", "holds": True}], "holding": [f"{SEAT}:o1"], "pending_asks": [],
                  "review_only_awaited": False, "plan_review_class": "objections_open"}
    outcome = derive_loop_outcome("done", {}, {"force_plan_decision": objections, "tool_calls": []})
    assert outcome["outcome_axes"]["execution"]["plan_review"] == "objections_open"
    assert outcome["outcome_axes"]["objective"]["status"] != "fail"
    awaiting = {**objections, "status": "awaiting", "open_objections": [], "holding": [], "pending_asks": ["a1"], "review_only_awaited": True}
    awaiting.pop("plan_review_class")
    outcome = derive_loop_outcome("done", {}, {"force_plan_decision": awaiting, "tool_calls": []})
    assert outcome["outcome_axes"]["execution"]["plan_review"] == "awaiting"
    clean = derive_loop_outcome("done", {}, {"force_plan_decision": {"required": False, "allow": True, "status": "not_required"}, "tool_calls": []})
    assert "plan_review" not in clean["outcome_axes"]["execution"]
    from ouroboros.project_dialogue import TASK_CAUSE_PHRASES, _terminal_limitations

    record = {"outcome_axes": {"execution": {"plan_review": "objections_open"}, "objective": {}}}
    assert _terminal_limitations(record, {}, "final_message") == ["", TASK_CAUSE_PHRASES["plan_review_objections_open"]]
    assert _terminal_limitations({"outcome_axes": {"execution": {}, "objective": {}}}, {}, "final_message") == ["", ""]


# ---------------------------------------------------------------- tool meta carried into the trace

def test_plan_task_meta_is_carried_only_when_typed(tmp_path):
    from ouroboros.loop_tool_execution import _extract_result_metadata
    from ouroboros.tools.tool_result import ToolResult

    good = ToolResult(status="ok", code="OK", text="Revision 1 recorded", meta={
        "plan_effect": "write", "plan_revision": 1, "plan_open_objections": 0, "plan_holding": 0, "plan_pending_asks": 0})
    meta = _extract_result_metadata("plan_task", good.text, False, good)
    assert meta["plan_effect"] == "write" and meta["plan_revision"] == 1 and meta["plan_holding"] == 0
    for bad in ({"plan_effect": "dance", "plan_revision": 1, "plan_open_objections": 0, "plan_holding": 0, "plan_pending_asks": 0},
                {"plan_effect": "write", "plan_revision": "1", "plan_open_objections": 0, "plan_holding": 0, "plan_pending_asks": 0},
                {"plan_effect": "write"}, {}):
        result = ToolResult(status="ok", code="OK", text="x", meta=bad)
        assert "plan_effect" not in _extract_result_metadata("plan_task", result.text, False, result)
    assert "plan_effect" not in _extract_result_metadata("plan_task", good.text, True, good)
    assert "plan_review_outcome" not in json.dumps(meta)


# ---------------------------------------------------------------- Codex audit (2026-10-15), point 2 at the finalization seam

def test_an_attempt_no_route_could_take_releases_the_blocking_obligation_as_disclosed(tmp_path):
    _plan(tmp_path)  # a plan the mind never asked about
    held = oh.force_plan_decision(_ctx(tmp_path, force_plan=True), {}, enforcement="blocking")
    assert held["allow"] is False and held["asks_unroutable"] == 0 and oh.plan_review_reminder(held).startswith("[PLAN_REVIEW_HOLD]")
    plan_record.append_attempt(tmp_path, "t-plan", {"revision": 1, "kind": "no_route", "ask": "council",
                                                    "not_sendable": {SEAT: "health_skip[credential_pool_exhausted]"}})
    attempted = oh.force_plan_decision(_ctx(tmp_path, force_plan=True), {}, enforcement="blocking")
    assert attempted["status"] == "force_plan_unmet" and attempted["allow"] is True and attempted["asks_unroutable"] == 1
    assert attempted["holding"] == [] and attempted["force_plan_met"] is False
    text = oh.plan_review_disclosure(attempted)
    assert "1 ask could reach no reviewer (every named route was unavailable)" in text and "disclosed, not held" in text
    assert "advisory enforcement" not in text
    assert oh.unmet_force_plan_obligation(_ctx(tmp_path, force_plan=True))["unmet"] is True, "unmet (nobody reached), yet not held"
    assert "advisory enforcement" in oh.plan_review_disclosure(
        oh.force_plan_decision(_ctx(tmp_path, force_plan=True), {}, enforcement="advisory"))
    _seed(tmp_path, "t-none")  # an attempt without a plan is not a plan: held to write one
    plan_record.append_attempt(tmp_path, "t-none", {"revision": None, "kind": "no_route", "ask": "council", "not_sendable": {}})
    assert oh.force_plan_decision(_ctx(tmp_path, task_id="t-none", force_plan=True), {}, enforcement="blocking")["allow"] is False
