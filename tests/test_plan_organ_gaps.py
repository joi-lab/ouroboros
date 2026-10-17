"""Gaps of the planning organ found while the retired engine was deleted (W2, 2026-10-15),
each fixed at the seam where it lived and pinned in both directions through the real
ToolContext, the real plan record, the real ledger and the organ's own delivery builders
(the review substrate is the fake of ``tests/test_plan_task_tool.py``)."""
from __future__ import annotations

import json

from ouroboros.review_records import ReviewActorRecord
from ouroboros.tools import plan_ask, plan_packet
from ouroboros.utils import append_jsonl
from tests.test_plan_task_tool import CLEAN, _call, _record, _reply, _slots, _user_text, _write, harness as _harness

harness = _harness  # noqa: F811 - pytest fixture re-export

GOAL_TEXT = "Weigh the plan — an intention, not a verdict — before the work starts."
COACH = ('delegated review session run-e23 ended failed: {"nextActions": ["Retry the run", "Inspect the run"], '
         '"safeMessage": "harness_error"}')
CAUSE = "the harness could not start a session for this profile"


def _room(harness, ctx):
    ctx.current_chat_id = 1
    chat = harness.drive / "logs" / "chat.jsonl"
    append_jsonl(chat, {"direction": "in", "chat_id": 1, "ts": "2026-10-15T10:00:00Z", "text": "Use the agreed outline"})
    append_jsonl(chat, {"direction": "out", "chat_id": 1, "ts": "2026-10-15T10:01:00Z", "text": "Option A is faster"})


def _frames(harness, ask_id):
    from ouroboros.owner_mailbox import _mailbox_path, mailbox_lines

    path = _mailbox_path(harness.drive, "task-1")
    rows = [json.loads(line) for line in mailbox_lines(path.read_text(encoding="utf-8"))] if path.exists() else []
    return [str(m.get("text") or "") for m in rows if f"Plan ask {ask_id}" in str(m.get("text") or "")]


# ---------------------------------------------------------------- (b) the body pack reaches a continued seat

def test_the_body_pack_reaches_a_continued_packet_participant_when_the_plan_becomes_a_body_plan(harness):
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _write(ctx, ask="council")  # revision 1: no files, not a body plan
    first = sub.calls[0]["request"]
    assert first.goal == GOAL_TEXT and "plan spec" not in first.goal  # (i)
    assert "## BIBLE.md (constitution" not in _user_text(first.slot_messages["s1"][0]["content"])

    _write(ctx, affected_paths=[str(harness.system / "ouroboros" / "loop.py")], ask="council")  # revision 2: a body plan
    record = _record(harness)
    assert record["revisions"][1]["body"] is True and record["asks"][1]["participants"][0]["continued"] is True
    messages = sub.calls[1]["request"].slot_messages["s1"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"], "the seat is continued, not restarted"
    system = _user_text(messages[0]["content"])
    assert "## BIBLE.md (constitution" in system and "## Principle 0: Agency" in system and "the loop." in system
    assert "Outline first, then draft each slide." in _user_text(messages[1]["content"]) and messages[2]["content"] == CLEAN
    assert "Changes since revision 1" in _user_text(messages[3]["content"])

    _write(ctx, affected_paths=[], ask="council")  # revision 3: no files again — the pack leaves with the body
    third = sub.calls[2]["request"].slot_messages["s1"]
    assert [m["role"] for m in third] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert "## BIBLE.md (constitution" not in _user_text(third[0]["content"]) and "Plan Review Checklist" in _user_text(third[0]["content"])


# ---------------------------------------------------------------- (c) + (C) the room snapshot

def test_the_room_snapshot_offers_prose_not_a_retired_channel_and_its_locator_resolves(harness):
    sub = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    _room(harness, ctx)
    _write(ctx, ask="council")
    content = _user_text(sub.calls[0]["request"].slot_messages["s1"][1]["content"])
    assert "need_evidence" not in content
    assert "asks for lines in prose" in content and "the mind reads every reply" in content
    assert "## RELATED ROOMS (pointers only; ask in prose for a room you need" in content
    carrier = _record(harness)["asks"][0]["dialogue_ref"]
    assert carrier["chat_id"] == 1 and len(carrier["sha256"]) == 64 and carrier["ref"]["path"]
    locator = f"chat:1@{carrier['sha256']}"
    assert f"`{locator}`" in content, "the footer names the exact snapshot the council saw"

    from ouroboros.tools.plan_dialogue import plan_chat_reader

    read = plan_chat_reader(harness.drive, "task-1")
    assert "Use the agreed outline" in read(f"1@{carrier['sha256']}")["text"]
    assert read("1@" + "0" * 64) is None  # a snapshot nobody recorded stays not found
    # The mind attaches the exact lines a participant named: the next ask carries them.
    _call(ctx, ask="council", attach=[f"{locator}::lines=2-2"])
    second = sub.calls[1]["request"].slot_messages["s1"]
    attached = _user_text(second[-1]["content"])
    assert "Use the agreed outline" in attached.split("## ATTACHED EVIDENCE")[-1] and "chat_not_found" not in attached
    assert _record(harness)["asks"][1]["omissions"] == []
    # The source closure follows the carrier, so promotion and cleanup keep the snapshot.
    from ouroboros.review_source_closure import source_carrier

    assert source_carrier({}, "dialogue_ref", "plan_asks") == "plan_source_entry"
    assert source_carrier({}, "ref", "plan_source_entry") == "metadata"


def test_a_task_without_a_room_records_no_snapshot_carrier(harness):
    harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    _write(harness.make_ctx(), ask="council")
    assert "dialogue_ref" not in _record(harness)["asks"][0]


# ---------------------------------------------------------------- (j) the settled frame tells the truth

def test_the_settled_frame_names_an_unresolved_seat_instead_of_calling_everyone_settled(harness):
    rows = [{"seat_id": "s1", "status": "answered"}, {"seat_id": "s2", "status": "failed"},
            {"seat_id": "s3", "status": "pending", "unresolved": True}]
    text = plan_packet.settled_frame("a1", rows, [])
    assert text == ("Plan ask a1: custody has nothing more to deliver — 1 answered, 1 failed, 1 unresolved (s3: outcome unknown — "
                    "neither a failure nor a reply; nothing waits on it, a new ask may pay twice); no open objection from this ask. "
                    "plan_task() reads the answers.")
    clean = plan_packet.settled_frame("a1", [{"seat_id": "s1", "status": "answered"}, {"seat_id": "s2", "status": "answered"}],
                                   [{"ask_id": "a1", "id": "o1"}, {"ask_id": "a0", "id": "old"}])
    assert clean == "Plan ask a1: every participant settled — 2 answered; 1 open objection from this ask. plan_task() reads the answers."
    # Through the custody hook: the lost seat stays pending and unresolved in the record, and the frame says so.
    pending = {"status": "pending", "raw_text": "", "operation_state": "pending_dispatch", "late_result_pending": True}
    sub = harness.install({"s1": CLEAN, "s2": pending, "s3": CLEAN})
    ctx = harness.make_ctx()
    _write(ctx, ask="council")
    request, slots = sub.calls[0]["request"], {s.slot_id: s for s in sub.calls[0]["slots"]}
    lost = ReviewActorRecord(slot_id="s2", model="m/b", status="error", error="custody lost after the barrier",
                             operation_id="op-s2-1", operation_state="custody_lost")
    plan_ask.settle_released_reply(ctx, request=request, task_id="task-1", actor=lost, slot=slots["s2"],
                                   settled_wave={"s1": "ok", "s2": "custody_lost", "s3": "ok"}, roster_size=3)
    seat = _record(harness)["asks"][0]["participants"][1]
    assert seat["status"] == "pending" and seat["unresolved"] is True
    frames = _frames(harness, "a1")
    assert len(frames) == 1 and "custody has nothing more to deliver — 2 answered, 1 unresolved (s2: outcome unknown" in frames[0]
    assert "every participant settled" not in frames[0]


# ---------------------------------------------------------------- (e) + (B) honest records

def _failed_row(**over):
    row = {"status": "error", "error": COACH, "failure_code": "run_failed", "reported_cause": CAUSE, "http_status": 503,
           "transport_status": "unavailable", "reset_at": "2026-10-16T00:00:00+00:00",
           "usage": {"capability_delta": [{"kind": "capability_delta", "reason": "session_route_resolves_its_own_model"}],
                     "profile_continuity_receipt": {"verdict": "same_profile", "expected": "acct-1", "observed": "acct-1"},
                     "auth_route_receipt": {"route": "oauth", "profile": "acct-1"}, "resolved_model": "", "cost": 0.0}}
    row.update(over)
    return row


def test_a_failed_seats_typed_facts_and_receipts_reach_the_record_the_ledger_and_the_read_view(harness):
    from ouroboros.review_ledger import UNKNOWN, load_record

    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "m/b", "session"), ("s3", "m/c"))
    harness.install({"s1": CLEAN, "s2": _failed_row(), "s3": CLEAN})
    ctx = harness.make_ctx()
    _write(ctx, ask="council")
    record = _record(harness)
    seat = record["asks"][0]["participants"][1]
    assert seat["status"] == "failed" and seat["failure_code"] == "run_failed" and seat["reported_cause"] == CAUSE
    assert seat["http_status"] == 503 and seat["transport_status"] == "unavailable"
    assert seat["capability_delta"] == [{"kind": "capability_delta", "reason": "session_route_resolves_its_own_model"}]
    assert seat["profile_continuity_receipt"]["verdict"] == "same_profile" and seat["auth_route_receipt"]["route"] == "oauth"
    ledger = {r["seat_id"]: r for r in load_record(harness.drive, record["asks"][0]["record_id"])["rows"]}
    assert ledger["s2"]["capability_delta"] == seat["capability_delta"] and ledger["s2"]["status"] == "error"
    assert ledger["s2"]["receipts"]["profile_continuity_receipt"]["verdict"] == "same_profile"
    assert ledger["s2"]["receipts"]["auth_route_receipt"]["route"] == "oauth"
    assert ledger["s2"]["observed_model"] == UNKNOWN, "a session that reported no resolved model is not dressed up as its request"
    assert ledger["s1"]["observed_model"] == "m/a" and ledger["s1"]["capability_delta"] == [] and "receipts" in ledger["s1"]
    # The read view: typed code, reported cause and reset — never the engine's retry coach.
    text, _ = _call(ctx)
    assert f's2 (m/b): failed[run_failed] — reported cause: "{CAUSE}" (reset at 2026-10-16T00:00:00+00:00)' in text
    for coach in ("nextActions", "Retry the run", "Inspect the run", "safeMessage", "harness_error"):
        assert coach not in text, coach


def test_a_session_that_resolved_its_model_is_observed_as_that_model_and_a_wordless_failure_keeps_a_bounded_line(harness):
    from ouroboros.review_ledger import load_record

    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "m/b", "session"))
    resolved = {"usage": {"cost": 0.01, "resolved_model": "openai/gpt-6-astra", "applied_profile": "acct-9",
                          "capability_delta": [{"kind": "capability_delta", "reason": "session_route_resolves_its_own_model"}]}}
    harness.install({"s1": _failed_row(reported_cause="", usage={"cost": 0.0}), "s2": resolved})
    ctx = harness.make_ctx()
    text, _ = _write(ctx, ask="council")
    record = _record(harness)
    ledger = {r["seat_id"]: r for r in load_record(harness.drive, record["asks"][0]["record_id"])["rows"]}
    assert ledger["s2"]["observed_model"] == "openai/gpt-6-astra" and ledger["s2"]["effective"]["profile_id"] == "acct-9"
    assert ledger["s2"]["capability_delta"][0]["reason"] == "session_route_resolves_its_own_model"
    assert record["asks"][0]["participants"][1]["resolved_model"] == "openai/gpt-6-astra"
    # The other direction: no reported cause → the first line of the error, bounded, and no "reported cause".
    tag = plan_packet.failure_tag(record["asks"][0]["participants"][0])
    assert tag.startswith("failed[run_failed] — delegated review session run-e23 ended failed:") and "reported cause" not in tag
    assert len(tag) < 260 and "(reset at 2026-10-16T00:00:00+00:00)" in tag
    assert "s1 (m/a): failed[run_failed] — delegated review session" in text


# ---------------------------------------------------------------- (E) the «runs as» row of a settled seat

def test_a_seat_that_settles_after_the_barrier_gets_its_runs_as_row(harness):
    from ouroboros.reviewer_slot_config import reviewer_slot_last_executions

    harness.state["slots"] = _slots(("runs-as-seat", "m/r"))
    pending = {"status": "pending", "raw_text": "", "operation_state": "pending_dispatch", "late_result_pending": True}
    sub = harness.install({"runs-as-seat": pending})
    ctx = harness.make_ctx()
    _write(ctx, ask="council")
    assert "runs-as-seat" not in reviewer_slot_last_executions(), "released at the barrier: nothing ran yet"
    request, slots = sub.calls[0]["request"], {s.slot_id: s for s in sub.calls[0]["slots"]}
    actor = ReviewActorRecord(slot_id="runs-as-seat", model="m/r", status="ok", raw_text=_reply("Late answer."),
                              operation_id="op-r-1", operation_state="late_settled",
                              usage={"cost": 0.02, "resolved_model": "m/r", "verdict_method": "plan_reply"})
    plan_ask.settle_released_reply(ctx, request=request, task_id="task-1", actor=actor, slot=slots["runs-as-seat"],
                                   settled_wave={"runs-as-seat": "ok"}, roster_size=1)
    row = reviewer_slot_last_executions()["runs-as-seat"]
    assert row["surface"] == "plan_review" and row["status"] == "ok"
    assert row["effective"]["model"] == "m/r" and row["effective"]["verdict_method"] == "plan_reply"
    assert _record(harness)["asks"][0]["participants"][0]["status"] == "answered"
