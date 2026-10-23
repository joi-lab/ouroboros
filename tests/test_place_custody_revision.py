"""A place handoff joins the author's serialized intake, including its final drain."""
from dataclasses import replace
from types import SimpleNamespace
import json
import queue
import threading

import pytest

from ouroboros import conversation_lease as leases
from ouroboros.place_intake import offer_to_author, settle_heard, decide_attention
from ouroboros.presence_runner import capture_presence_event, presence_turn_replay
from tests.test_place_focus import focus as focus, task_for


def intake(author):
    return SimpleNamespace(_owner_message_admission_lock=threading.RLock(), _accepting_owner_messages=True,
                           _busy=True, _current_task_id=author)


@pytest.mark.parametrize("ending", ["released", "intake_closed"])
def test_handoff_cannot_settle_after_the_author_closes(focus, monkeypatch, ending):
    from ouroboros.owner_mailbox import _mailbox_path
    root, _, admission, event = focus
    task = capture_presence_event(admission, event, drive_root=root)
    lease = leases.PresenceTurnGate(state_root=root / "state").acquire(event.conversation_key)
    lease.bind_author("finishing-author")
    agent = lease.intake_agent = intake("finishing-author")
    read_author = leases.live_author
    def ends_after_census(*args, **kwargs):
        author = read_author(*args, **kwargs)
        if ending == "released":
            lease.release()
        else:
            with agent._owner_message_admission_lock:
                agent._accepting_owner_messages = False
        return author
    monkeypatch.setattr(leases, "live_author", ends_after_census)
    try:
        assert not offer_to_author(root, task, event)
        assert not _mailbox_path(root, "finishing-author").exists()
        assert presence_turn_replay(root, task["id"], event.conversation_key, task["metadata"]["presence_event_identity"]) is None
        assert decide_attention(root, admission, event)[0] == "focus"
    finally:
        lease.release()


def test_intake_close_and_mail_acceptance_share_one_lock(focus, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from ouroboros import owner_mailbox
    root, _, admission, event = focus
    task = capture_presence_event(admission, event, drive_root=root)
    lease = leases.PresenceTurnGate(state_root=root / "state").acquire(event.conversation_key)
    lease.bind_author("author")
    agent = lease.intake_agent = intake("author")
    append_entered, append_release, close_entered, closed = (threading.Event() for _ in range(4))
    append = owner_mailbox.write_task_message
    def paused_append(*args, **kwargs):
        append_entered.set()
        assert append_release.wait(10)
        assert not closed.is_set()
        return append(*args, **kwargs)
    def close():
        close_entered.set()
        with agent._owner_message_admission_lock:
            agent._accepting_owner_messages = False
            closed.set()
    monkeypatch.setattr(owner_mailbox, "write_task_message", paused_append)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            offered = pool.submit(offer_to_author, root, task, event)
            try:
                assert append_entered.wait(10)
                closing = pool.submit(close)
                assert close_entered.wait(10)
                assert not closed.is_set()
            finally:
                append_release.set()
            assert offered.result(timeout=10)
            closing.result(timeout=10)
        assert not offer_to_author(root, task, event)
        assert len(owner_mailbox._mailbox_path(root, "author").read_text().splitlines()) == 1
    finally:
        append_release.set()
        lease.release()


@pytest.mark.parametrize("arrival", [False, True])
def test_final_drain_returns_accepted_place_context_to_the_author(focus, monkeypatch, arrival):
    from ouroboros import loop
    from ouroboros.tools.registry import ToolRegistry
    root, _, admission, event = focus
    task = task_for(focus)
    incoming = replace(event, source_event_id="event2", text="A newly accepted place event")
    captured = capture_presence_event(admission, incoming, drive_root=root)
    lease = leases.PresenceTurnGate(state_root=root / "state").acquire(event.conversation_key)
    lease.bind_author(task["id"])
    agent = lease.intake_agent = intake(task["id"])
    registry = ToolRegistry(repo_dir=root, drive_root=root)
    ctx = registry._ctx
    ctx.task_metadata, ctx.task_contract, ctx.is_direct_chat = task["metadata"], task["task_contract"], True
    ctx.owner_message_admission_agent = agent
    ctx.owner_message_admission_lock = agent._owner_message_admission_lock
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr("ouroboros.presence_context.confirmed_delivery_receipts", lambda _: frozenset())
    offered, calls = [], []
    def before_close(*_args):
        if arrival and not offered:
            offered.append(offer_to_author(root, captured, incoming))
            assert offered == [True]
            assert settle_heard(root, captured, incoming, "current_author").outcome == "silent"
        return False
    monkeypatch.setattr("ouroboros.tools.review_result.result_review_at_delivery", before_close)
    def respond(_llm, messages, *_args, **_kwargs):
        calls.append(messages.copy())
        assert len(calls) <= 2
        return {"content": None, "tool_calls": [{"id": "finish" + str(len(calls)), "type": "function",
            "function": {"name": "finish_task", "arguments": json.dumps({"action": "finish", "answer": "Hello"})}}]}, 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", respond)
    try:
        text, _, _ = loop.run_llm_loop([{"role": "user", "content": "Hello"}], registry,
            SimpleNamespace(default_model=lambda: "test-model"), root / "logs", lambda *_a, **_k: None,
            queue.Queue(), task_id=task["id"], drive_root=root)
        assert text == "Hello" and len(calls) == (2 if arrival else 1)
        if arrival:
            assert "newly accepted place event" in str(calls[-1])
            assert "never an owner directive" in str(calls[-1])
            assert not any("newly accepted" in str(r) for r in getattr(ctx, "_owner_directives", []))
        assert not agent._accepting_owner_messages
    finally:
        lease.release()
