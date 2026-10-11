"""Every paid predecessor is CONTINUED per reviewer: a packet slot re-sends its exact
recorded transcript plus the new packet when both fit its window, otherwise it goes out
fresh with the cause disclosed on THAT row alone; a session slot resumes its sticky thread
and the host never states a guess about its continuity. The first cycle discloses nothing."""
from __future__ import annotations

import json

from tests.test_plan_review_engine import (  # noqa: F401
    CLEAN, DECK_SPEC, _call, _control, _finding, _slots, _state, _user_text, harness,
)

_CONTINUATION = "continuation of prior transcript"


def _deltas(actor: dict) -> list:
    return [d for d in actor.get("capability_delta") or [] if d.get("requested") == _CONTINUATION]


def _first_wave(h, answers=None):
    sub = h.install(answers or {"s1": json.dumps([_finding("f1", "blocking", breaks="claim_1")]), "s2": CLEAN, "s3": CLEAN})
    ctx = h.make_ctx()
    assert _control(_call(ctx)) == {"outcome": "REVIEW_REQUIRED", "closed": False}
    return ctx, sub


def test_first_cycle_carries_no_continuation_delta(harness):  # noqa: F811
    _first_wave(harness)
    assert all(not _deltas(a) for a in _state(harness)["waves"][-1]["actors"])


def test_second_cycle_continues_the_api_transcript_without_a_requested_locator(harness, monkeypatch):  # noqa: F811
    """The prior finding asked for no document, yet cycle 2 continues each packet slot's exact
    recorded transcript: the second send begins with the cycle-1 request, then the reviewer's
    own cycle-1 answer, then the new packet — and no row carries a restart delta."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    ctx, sub = _first_wave(harness)
    from ouroboros.tools.plan_review_artifacts import authority_wave

    exact = authority_wave(harness.drive, ctx.task_id, _state(harness)["waves"][-1])
    recorded = {r["slot_id"]: r for r in exact["reviewer_outputs"]}
    assert recorded["s1"]["request_messages"], "cycle 1 recorded its packet"
    sub2 = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    second = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]})
    assert _control(second) == {"outcome": "GREEN", "closed": True}
    sent = sub2.calls[0]["request"].slot_messages
    for sid in ("s1", "s2", "s3"):
        prior = recorded[sid]["request_messages"]
        assert sent[sid][:len(prior)] == prior, f"{sid} continues its exact recorded request"
        assert sent[sid][len(prior)] == {"role": "assistant", "content": recorded[sid]["text"]}
        assert sent[sid][-1]["role"] == "user" and "the corrected claim" in json.dumps(sent[sid][-1]["content"])
    assert all(not _deltas(a) for a in _state(harness)["waves"][-1]["actors"])


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
                               manifest={}, slot_messages={"small": prior, "large": prior}, native_mandatory_chars=0)
    assert out["continuation_restarted"] == {"small": "prior_transcript_exceeds_slot_window"}
    assert out["slot_messages"]["large"][:3] == prior[:3] and len(out["slot_messages"]["large"]) == 4
    assert len(out["slot_messages"]["small"]) == 2 and out["slot_messages"]["small"][0]["role"] == "system"
    assert out["slot_prompt_chars"]["small"] <= 400 * 4, "the fresh packet fits the small window"


def test_resumed_session_rows_carry_no_fresh_session_delta(harness, monkeypatch):  # noqa: F811
    """The live shape: cycle-1 session slot B recorded no thread id; in cycle 2 the API rows
    continue their transcripts and the session row resumes its sticky thread — no row, and
    never a session row, carries a "fresh session" delta."""
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    harness.state["slots"] = _slots(("s1", "m/a"), ("s2", "m/b", "session"), ("s3", "m/c"))
    ctx, _sub = _first_wave(harness)
    harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    second = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["the corrected claim"]})
    assert _control(second) == {"outcome": "GREEN", "closed": True}
    actors = {a["slot_id"]: a for a in _state(harness)["waves"][-1]["actors"]}
    assert actors["s2"]["route"] == "agent_session" and not _deltas(actors["s2"])
    assert not _deltas(actors["s1"]) and not _deltas(actors["s3"])


def test_a_per_slot_miss_restarts_only_that_slot(harness, monkeypatch):  # noqa: F811
    """A wave-level miss touches every packet slot; a per-slot miss touches one: with s2's
    recorded transcript invalid, s2 goes out fresh with its cause and s1/s3 continue."""
    from ouroboros.tools.plan_review_artifacts import continuation_inputs, persist_wave, slot_row

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "5")
    slots = harness.state["slots"]
    exact = {"schema_version": 2, "cycle_index": 1, "request_fingerprint": "f" * 64,
             "slots": [slot_row(s) for s in slots],
             "reviewer_outputs": [
                 {"slot_id": "s1", "request_messages": [{"role": "user", "content": "p1"}], "text": "[]"},
                 {"slot_id": "s2", "request_messages": [{}], "text": "[]"},
                 {"slot_id": "s3", "request_messages": [{"role": "user", "content": "p3"}], "text": "[]"}]}
    ref = persist_wave(harness.drive, "task-1", exact)
    _slots_out, messages, _threads, causes = continuation_inputs(
        harness.drive, "task-1", {"wave_artifact": ref}, slots, user_content="next")
    assert causes == {"s2": "prior_api_transcript_invalid:s2"}
    assert set(messages) == {"s1", "s3"} and messages["s1"][0] == {"role": "user", "content": "p1"}
