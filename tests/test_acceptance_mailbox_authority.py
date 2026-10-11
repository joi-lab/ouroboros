"""Mailbox wakeups and unread owner authority are separate acceptance facts."""
from __future__ import annotations

import copy

import pytest

from ouroboros import loop
from ouroboros.loop_acceptance import _end_task_acceptance_fence
from ouroboros.loop_messages import (
    acknowledge_acceptance_observation,
    capture_acceptance_observation,
    owner_source_sha256,
)
from ouroboros.loop_transport import _owner_signal_pending
from ouroboros.owner_mailbox import (
    OwnerMailboxPeek,
    drain_owner_entries,
    write_owner_message,
    write_task_message,
)
from tests.test_acceptance_async_loop import ANSWER, full_loop as _full_loop
from tests.test_acceptance_semantic_subject import case as _semantic_case

full_loop = _full_loop
case = _semantic_case


def _fence(tool_ctx):
    tool_ctx._task_acceptance_fence_token = "final-fence"
    tool_ctx._task_acceptance_owner_generation = tool_ctx.owner_message_admission_agent._owner_message_generation
    calls = []

    def end(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "status": "released" if kwargs["outcome"] == "revision" else "sealed"}

    tool_ctx.end_acceptance_fence = end
    return calls


@pytest.mark.parametrize("provenance", ["system", "descendant_task", "independent_task"])
@pytest.mark.parametrize("cached", [False, True])
def test_context_mail_wakes_without_reopening_acceptance(case, provenance, cached):
    tool_ctx, _tools, ctx, _trace, _candidate, _run = case
    before = owner_source_sha256(tool_ctx)
    calls = _fence(tool_ctx)
    assert write_task_message(tool_ctx.drive_root, "Review slot answered PASS.", "root",
                              source_task_id="source", provenance=provenance, msg_id="context")
    peek = OwnerMailboxPeek() if cached else None
    assert _owner_signal_pending(ctx.incoming_messages, tool_ctx.drive_root, "root", set(), 1, peek)
    assert _end_task_acceptance_fence(tool_ctx, outcome="terminal")
    assert calls[-1]["outcome"] == "terminal"
    assert not tool_ctx._task_acceptance_fence_generation_mismatch
    assert owner_source_sha256(tool_ctx) == before
    assert tool_ctx._loop_mailbox_seen_ids == set()
    assert drain_owner_entries(tool_ctx.drive_root, "root", set(), 1)[0]["msg_id"] == "context"


@pytest.mark.parametrize("provenance", ["system", "descendant_task", "independent_task"])
def test_context_mail_does_not_hide_observation_or_refuse_ack(case, provenance):
    tool_ctx, _tools, ctx, trace, _candidate, _run = case
    observed = capture_acceptance_observation(tool_ctx, trace, ctx.incoming_messages)
    assert write_task_message(tool_ctx.drive_root, "Context only.", "root", source_task_id="source",
                              provenance=provenance, msg_id="context")
    assert acknowledge_acceptance_observation(tool_ctx, observed["owner_source_sha256"])
    assert capture_acceptance_observation(tool_ctx, trace, ctx.incoming_messages) == observed
    assert tool_ctx._loop_mailbox_seen_ids == set()


@pytest.mark.parametrize("kind", ["owner_text", "quiz_answer", "hurry", "finalize_now",
                                 "ancestor_task", "peer_via_ancestor", "legacy"])
def test_unread_owner_or_control_keeps_finalization_open(case, kind):
    tool_ctx, _tools, ctx, trace, _candidate, _run = case
    observed = capture_acceptance_observation(tool_ctx, trace, ctx.incoming_messages)
    calls = _fence(tool_ctx)
    if kind in {"ancestor_task", "peer_via_ancestor"}:
        assert write_task_message(tool_ctx.drive_root, "Principal input.", "root",
                                  source_task_id="principal", provenance=kind, msg_id="authority")
    elif kind == "legacy":
        from ouroboros.owner_mailbox import _mailbox_path
        from ouroboros.utils import append_jsonl

        assert append_jsonl(_mailbox_path(tool_ctx.drive_root, "root"),
                            {"msg_id": "authority", "text": "Legacy owner text."})
    else:
        assert write_owner_message(tool_ctx.drive_root, "Owner input.", "root",
                                   msg_id="authority", kind=kind)
    assert not acknowledge_acceptance_observation(tool_ctx, observed["owner_source_sha256"])
    assert capture_acceptance_observation(tool_ctx, trace, ctx.incoming_messages) == {}
    assert _end_task_acceptance_fence(tool_ctx, outcome="terminal")
    assert calls[-1]["outcome"] == "revision"
    assert tool_ctx._task_acceptance_fence_generation_mismatch
    assert tool_ctx._loop_mailbox_seen_ids == set()


def test_direct_incoming_owner_input_still_prevents_final_seal(case):
    tool_ctx, _tools, ctx, _trace, _candidate, _run = case
    calls = _fence(tool_ctx)
    ctx.incoming_messages.put("One more owner message.")
    assert _end_task_acceptance_fence(tool_ctx, outcome="terminal")
    assert calls[-1]["outcome"] == "revision"
    assert not ctx.incoming_messages.empty()


@pytest.mark.parametrize("arrival", ["none", "system", "owner"])
def test_real_loop_terminal_admission_distinguishes_settlement_from_owner(full_loop, monkeypatch, arrival):
    f = full_loop
    f.release.set()
    f.ctx.owner_wait_callback = None  # Settle synchronously; no timing sleeps in this test.
    original = loop._end_task_acceptance_fence
    injected = []
    owner_text = "How is the report going?"

    class OwnerInputReceived(Exception):
        pass

    def end(ctx, **kwargs):
        if not injected and kwargs.get("outcome") == "terminal":
            injected.append(True)
            if arrival == "system":
                from ouroboros.acceptance_settlement import announce_acceptance_settlement

                slot = f.slots[0].slot_id
                announce_acceptance_settlement(ctx, f.review_requests[0], {
                    "slots": {slot: "ok"}, "total": 1,
                    "verdicts": {slot: {"verdict": "PASS", "note": "settled"}},
                })
            elif arrival == "owner":
                assert write_owner_message(ctx.drive_root, owner_text, ctx.task_id, msg_id="late-owner")
        return original(ctx, **kwargs)

    monkeypatch.setattr(loop, "_end_task_acceptance_fence", end)

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 2, f.progress
        if f.model_step == 1:
            return {"content": ANSWER}, 0.0
        assert arrival == "owner", "a system settlement must not force another Main round"
        assert owner_text in str(messages)
        # The new owner turn must run before finalization. Its subsequent
        # authored response is covered by the full-loop owner-dialogue tests.
        raise OwnerInputReceived()

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    if arrival == "owner":
        with pytest.raises(OwnerInputReceived):
            f.run()
        assert f.model_step == 2 and len(f.review_sends) == 1
        assert any("owner follow-up arrived" in text for text in f.progress)
        return
    result, _usage, trace = f.run()
    assert result == ANSWER and len(f.review_sends) == 1
    assert f.model_step == 1
    assert not f.ctx.owner_message_admission_agent._accepting_owner_messages
    assert trace["acceptance_decision"]["status"] == "accepted"
    assert not any("owner follow-up arrived" in text for text in f.progress)
