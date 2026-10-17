"""Every participant is CONTINUED on its own: a packet seat re-sends its exact retained
transcript plus the new turn when both fit its window, otherwise it goes out fresh with the
cause disclosed for THAT seat alone; a session seat resumes its own recorded thread; a seat
whose earlier delivery cannot be read goes out fresh while the others continue; the first
ask continues nobody."""
from __future__ import annotations

from ouroboros.tools.plan_packet import PLAN_REPLY_CONTRACT
from tests.test_plan_task_tool import CLEAN, _call, _record, _slots, _write
from tests.test_plan_task_tool import harness as _plan_task_harness

harness = _plan_task_harness  # the plan_task fixture, under the name its tests take


def test_over_window_continuation_restarts_only_that_slot(monkeypatch):
    """Two packet slots continue; the small window cannot hold its transcript plus the new
    packet, so THAT slot goes out fresh (typed cause) while the large one continues — and
    neither is dropped as oversize."""
    from ouroboros.review_substrate import ReviewSlot
    from ouroboros.tools import review_synthesis
    from ouroboros.tools.plan_dialogue import dialogue_slot_inputs

    monkeypatch.setattr(review_synthesis, "per_slot_input_token_limits", lambda *a, **k: {"small": 400, "large": 200_000})
    slots = [ReviewSlot(slot_id="small", model="m/small"), ReviewSlot(slot_id="large", model="m/large")]
    prior = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x" * 2000},  # > 400 tokens * 4 chars
             {"role": "assistant", "content": "[]"}, {"role": "user", "content": "the new packet"}]
    out = dialogue_slot_inputs(slots, system_prompt="sys", user_content="the new packet", session_task="",
                               manifest={}, slot_messages={"small": prior, "large": prior}, native_mandatory_chars=0,
                               output_contract=PLAN_REPLY_CONTRACT)
    assert out["continuation_restarted"] == {"small": "prior_transcript_exceeds_slot_window"}
    assert out["slot_messages"]["large"][:3] == prior[:3] and len(out["slot_messages"]["large"]) == 4
    assert len(out["slot_messages"]["small"]) == 2 and out["slot_messages"]["small"][0]["role"] == "system"
    assert out["slot_prompt_chars"]["small"] <= 400 * 4, "the fresh packet fits the small window"


def _answer_with_thread(thread_id: str) -> dict:
    return {"raw_text": CLEAN, "usage": {"prompt_tokens": 10, "completion_tokens": 5, "physical_attempt_state": "settled",
                                         "cost": 0.0, "review_thread_id": thread_id}}


def _continued(h, ask_index: int) -> dict:
    return {p["seat_id"]: p["continued"] for p in _record(h)["asks"][ask_index]["participants"]}


def test_a_session_participant_resumes_its_own_recorded_thread_and_nobody_continues_the_first_ask(harness):
    """The first ask continues nobody. On a later ask the packet seat continues its own
    transcript, the session seat whose answer recorded a thread resumes exactly that thread,
    and a session seat whose answer recorded none starts a new one (no thread is invented)."""
    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "cursor=grok", "session"), ("s3", "cursor=fable", "session"))
    substrate = harness.install({"s1": CLEAN, "s2": _answer_with_thread("thread-7"), "s3": CLEAN})
    ctx = harness.make_ctx()
    _write(ctx, ask="council")
    assert substrate.calls[0]["request"].session_threads == {}
    assert _continued(harness, 0) == {"s1": False, "s2": False, "s3": False}
    assert _record(harness)["asks"][0]["participants"][1]["review_thread_id"] == "thread-7"
    substrate = harness.install({})
    _call(ctx, ask="council", message="I revised nothing; what do you make of each other's answers?")
    request = substrate.calls[0]["request"]
    assert request.session_threads == {"s2": "thread-7"}
    assert [m["role"] for m in request.slot_messages["s1"]] == ["system", "user", "assistant", "user"]
    assert _continued(harness, 1) == {"s1": True, "s2": True, "s3": False}


def test_a_seat_whose_retained_delivery_cannot_be_read_goes_out_fresh_alone(harness):
    """A packet seat continues from the transcript its earlier ask retained. When that source
    cannot be read the seat is sent fresh (no continuation claimed) and its peers still continue."""
    from ouroboros.artifacts import task_artifact_dir_path

    substrate = harness.install({})
    ctx = harness.make_ctx()
    _write(ctx, ask="council")
    seat = next(p for p in _record(harness)["asks"][0]["participants"] if p["seat_id"] == "s2")
    retained = task_artifact_dir_path(harness.drive, seat["delivery_ref"]["task_id"]) / seat["delivery_ref"]["ref"]["path"]
    assert retained.is_file()
    retained.unlink()
    substrate = harness.install({})
    _call(ctx, ask="council", message="again")
    sent = substrate.calls[0]["request"].slot_messages
    assert [m["role"] for m in sent["s2"]] == ["system", "user"]
    assert [m["role"] for m in sent["s1"]] == [m["role"] for m in sent["s3"]] == ["system", "user", "assistant", "user"]
    assert _continued(harness, 1) == {"s1": True, "s2": False, "s3": True}
