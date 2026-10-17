"""Answers keep their addressee through holds, Continue, last drains and crashes."""
import queue
from types import SimpleNamespace

import pytest

from ouroboros.gateway import task_decision as td
from ouroboros.owner_quiz import record_asked, record_publication, quiz_states
from ouroboros.task_store import patch_document
from ouroboros.question_answers import admitted_successor
from tests.test_quiz_answer import _post
from tests.test_quiz_late_answer import _late_bridge, _inbox


def runtime(tmp_path, monkeypatch, *, running=True):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from supervisor import queue as q
    task = {"id": "asker", "chat_id": 1, "drive_root": str(tmp_path)}
    monkeypatch.setattr(q, "RUNNING", {"asker": {"task": task}} if running else {})
    monkeypatch.setattr(q, "PENDING", [])
    monkeypatch.setattr(q, "BUDGET_ROOT_FENCES", {})
    monkeypatch.setattr(q, "_task_drive_for_task", lambda task, tid: tmp_path)
    monkeypatch.setattr(td, "request_drive_root", lambda request: tmp_path)
    from ouroboros.task_results import write_task_result
    write_task_result(tmp_path, "asker", status="running" if running else "completed", chat_id=1)
    record_asked(tmp_path, "asker", quiz_id="q", question="Which database?", options=["sqlite", "postgres"],
                 assumption="sqlite", chat_id=1)
    record_publication(tmp_path, "asker", "q", 1)
    bridge, frames = _late_bridge(tmp_path, monkeypatch)
    app = Starlette(routes=[Route("/api/decisions", endpoint=td.api_decision_answer, methods=["POST"])])
    return app, bridge, q, task


def answer(app):
    return _post(app, {"decision_id": "quiz:asker:q", "request_id": "r", "option_index": 1, "comment": "exact answer"})


def debt(root):
    return quiz_states(root, "asker")["q"]["delivery"]


def drain(root, task_id):
    from ouroboros.loop_round_limits import _drain_incoming_messages
    ctx = SimpleNamespace(task_id=task_id, task_metadata={}, drive_root=root, budget_drive_root=root)
    messages = []
    _drain_incoming_messages(messages, queue.Queue(), root, task_id, None, set(), owner_ctx=ctx)
    return ctx, messages


def continue_to(root, state="admitted"):
    from ouroboros.owner_continue import binding_sha
    from ouroboros.task_results import write_task_result
    write_task_result(root, "successor", status="running", chat_id=1)
    binding = {"predecessor_task_id": "asker", "successor_task_id": "successor", "action_nonce": "nonce"}
    admission = {"binding": binding, "binding_sha256": binding_sha(binding)}
    patch_document(root, "asker", lambda row: {**row, "continued_by": {
        **admission, "state": state, "successor_task_id": "successor"}})
    patch_document(root, "successor", lambda row: {**row, "continuation_admission": admission})


def test_live_answer_is_owed_until_saved_read_and_keeps_only_owner_words(tmp_path, monkeypatch):
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    response = answer(app)
    assert response.status_code == 200, response.text
    assert debt(tmp_path)["state"] == "owed"
    assert debt(tmp_path)["recipient_task_id"] == "asker" and debt(tmp_path)["msg_id"]
    ctx, messages = drain(tmp_path, "asker")
    assert debt(tmp_path)["state"] == "delivered"
    assert "Which database?" in str(messages) and "postgres" in str(messages)
    assert [r["content"] for r in ctx._owner_directives] == ["exact answer"]
    from ouroboros.owner_relay import quote_rows
    [handle] = ctx._held_owner_messages
    [quoted], error = quote_rows(ctx, [handle])
    assert not error and quoted["text"] == "exact answer"
    assert "Which database?" in quoted["question_frame"]
    assert _inbox(bridge) == []


def test_only_admitted_successor_can_receive_an_answer(tmp_path, monkeypatch):
    app, bridge, q, task = runtime(tmp_path, monkeypatch, running=False)
    continue_to(tmp_path)
    q.RUNNING["successor"] = {"task": {**task, "id": "successor"}}
    response = answer(app)
    assert response.status_code == 200, response.text
    assert debt(tmp_path)["recipient_task_id"] == "successor"
    assert _inbox(bridge) == []
    drain(tmp_path, "successor")
    assert debt(tmp_path)["state"] == "delivered"


def test_bound_claim_and_unrelated_room_reader_cannot_take_the_answer(tmp_path, monkeypatch):
    app, bridge, q, task = runtime(tmp_path, monkeypatch, running=False)
    continue_to(tmp_path, "bound")
    q.RUNNING["successor"] = {"task": {**task, "id": "successor"}}
    assert admitted_successor(tmp_path, "asker") == ""
    response = answer(app)
    assert response.status_code == 200 and response.json()["forwarded"] is True, response.text
    [message] = _inbox(bridge)
    assert message["task_metadata"]["late_answer"]["task_id"] == "asker"
    assert message["chat_id"] == 1 and debt(tmp_path)["route"] == "fresh_head"
    assert answer(app).status_code == 200 and _inbox(bridge) == []


def test_held_asker_keeps_hold_and_gets_no_mailbox_answer(tmp_path, monkeypatch):
    from ouroboros.owner_mailbox import drain_owner_entries
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    task["_owner_hold"] = True
    response = answer(app)
    assert response.status_code == 200, response.text
    assert task["_owner_hold"] is True
    assert not drain_owner_entries(tmp_path, "asker")
    assert len(_inbox(bridge)) == 1


def test_last_drain_race_recovers_unread_answer_once(tmp_path, monkeypatch):
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    assert answer(app).status_code == 200
    assert debt(tmp_path)["state"] == "owed"
    q.RUNNING.clear()
    patch_document(tmp_path, "asker", lambda row: {**row, "status": "completed"})
    response = answer(app)
    assert response.status_code == 200 and response.json()["forwarded"], response.text
    assert len(_inbox(bridge)) == 1
    assert answer(app).status_code == 200 and not _inbox(bridge)


def test_crash_after_mailbox_append_retries_same_message_identity(tmp_path, monkeypatch):
    from ouroboros import question_answers as qa
    from ouroboros.owner_mailbox import drain_owner_entries
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    real = qa._update_delivery
    def lose_ack(root, asker, quiz, changes):
        if changes.get("mailbox_at"):
            raise OSError("crash after append")
        return real(root, asker, quiz, changes)
    monkeypatch.setattr(qa, "_update_delivery", lose_ack)
    assert answer(app).status_code == 503
    identity = debt(tmp_path)["msg_id"]
    monkeypatch.setattr(qa, "_update_delivery", real)
    assert answer(app).status_code == 200
    assert [r["msg_id"] for r in drain_owner_entries(tmp_path, "asker")] == [identity]
    drain(tmp_path, "asker")
    assert debt(tmp_path)["state"] == "delivered" and not _inbox(bridge)


def sleeping_asker(tmp_path, monkeypatch, hold=""):
    """An asker parked in its own cold sleep (the rail ``continue_in_background`` uses)."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from ouroboros.owner_pause import install_fence
    from supervisor.events_budget import HOLD_OWNER_RESTART, hold_budget_row
    from tests._budget_pause_exact_helpers import _install_queue
    from tests.test_model_sleep import _cold_park
    q, state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setattr(state, "budget_remaining", lambda _st, **_k: 5.0)
    _cold_park(tmp_path, monkeypatch, workers, task_id="asker", wake_at="2099-01-01T00:00:00+00:00")
    assert [row["_budget_pause"]["reason"] for row in q.PENDING] == ["sleep"]
    if hold == "restart_hold":
        hold_budget_row(q.PENDING[0], reason=HOLD_OWNER_RESTART, detail="owner Restart", result_root=tmp_path)
    elif hold == "owner_pause":
        install_fence(tmp_path, "asker", request_id="owner-pause")
    record_asked(tmp_path, "asker", quiz_id="q", question="Which database?", options=["sqlite", "postgres"],
                 assumption="sqlite", chat_id=1)
    record_publication(tmp_path, "asker", "q", 1)
    bridge, _frames = _late_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(td, "request_drive_root", lambda request: tmp_path)
    return Starlette(routes=[Route("/api/decisions", endpoint=td.api_decision_answer, methods=["POST"])]), bridge, q


def mail(root, kind):
    from ouroboros.owner_mailbox import drain_owner_entries
    return [entry["msg_id"] for entry in drain_owner_entries(root, "asker") if entry["kind"] == kind]


@pytest.mark.parametrize("hold", ["", "restart_hold", "owner_pause"])
def test_cold_sleeping_asker_reads_its_own_answer_unless_held(tmp_path, monkeypatch, hold):
    from ouroboros.budget_pause import budget_pause_row
    from ouroboros.owner_pause import fence_closed, read_fence
    from supervisor.events_budget import budget_hold_fact
    from supervisor.sleep_wake import wake_ready_sleepers
    app, bridge, q = sleeping_asker(tmp_path, monkeypatch, hold)
    response = answer(app)
    assert response.status_code == 200, response.text
    woke = [outcome for outcome in wake_ready_sleepers(q) if outcome.get("ok")]
    if not hold:
        # Its own answer is its mail: one copy, no second head, and its readiness wakes it.
        assert mail(tmp_path, "quiz_answer") == ["quiz_answer:asker:q"] and _inbox(bridge) == []
        assert debt(tmp_path)["recipient_task_id"] == "asker"
        assert woke and budget_pause_row(tmp_path, "asker")["sleep_ready"]["reason"] == "owner_text"
        return
    # A held or owner-paused sleeper is not the reader: a fresh head answers and the hold stays.
    assert mail(tmp_path, "quiz_answer") == [] and len(_inbox(bridge)) == 1
    assert debt(tmp_path)["route"] == "fresh_head" and not woke
    if hold == "restart_hold":
        assert budget_hold_fact(q.PENDING[0])["reason"] == "owner_restart_hold"
    else:
        assert fence_closed(read_fence(tmp_path, "asker"))


@pytest.mark.parametrize("held", [False, True])
def test_cold_sleeping_asker_learns_the_owner_closure_unless_held(tmp_path, monkeypatch, held):
    from supervisor.sleep_wake import wake_ready_sleepers
    app, _bridge, q = sleeping_asker(tmp_path, monkeypatch, "restart_hold" if held else "")
    response = _post(app, {"decision_id": "quiz:asker:q", "request_id": "close", "action": "close"})
    assert response.status_code == 200, response.text
    assert mail(tmp_path, "quiz_closed") == ([] if held else ["quiz_closed:q"])
    assert any(outcome.get("ok") for outcome in wake_ready_sleepers(q)) is not held


@pytest.mark.parametrize("pause", ["owner_hold", "money_pause", "queued"])
def test_retried_answer_stays_owed_to_its_unread_live_recipient(tmp_path, monkeypatch, pause):
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    assert answer(app).status_code == 200
    assert debt(tmp_path)["recipient_task_id"] == "asker" and debt(tmp_path)["mailbox_at"]
    # Paused (or parked) after the mailbox write, before its next drain.
    if pause == "queued":
        q.PENDING.append(q.RUNNING.pop("asker")["task"])
    else:
        task.update({"_owner_hold": True} if pause == "owner_hold" else {"_budget_pause": {"reason": "budget"}})
    response = answer(app)  # the same request id, retried
    assert response.status_code == 200, response.text
    assert _inbox(bridge) == [] and mail(tmp_path, "quiz_answer") == ["quiz_answer:asker:q"]
    assert debt(tmp_path)["recipient_task_id"] == "asker"
    assert debt(tmp_path)["reason_code"] == "awaiting_recipient_read"
    # Resume: the same work reads its one copy, exactly once.
    task.pop("_owner_hold", None)
    task.pop("_budget_pause", None)
    if pause == "queued":
        q.RUNNING["asker"] = {"task": q.PENDING.pop()}
    ctx, _messages = drain(tmp_path, "asker")
    assert [r["content"] for r in ctx._owner_directives] == ["exact answer"]
    assert debt(tmp_path)["state"] == "delivered"
    assert answer(app).status_code == 200 and _inbox(bridge) == []
    assert getattr(drain(tmp_path, "asker")[0], "_owner_directives", []) == []


def test_paused_recipient_keeps_its_saved_answer_and_reads_it_once(tmp_path, monkeypatch):
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    assert answer(app).status_code == 200
    task["_owner_hold"] = True
    assert answer(app).status_code == 200, "a retried answer stays with the work that holds its copy"
    assert _inbox(bridge) == [] and debt(tmp_path)["recipient_task_id"] == "asker"
    assert task["_owner_hold"] is True
    task.pop("_owner_hold")
    ctx, _ = drain(tmp_path, "asker")
    assert [r["content"] for r in ctx._owner_directives] == ["exact answer"]
    assert answer(app).status_code == 200 and _inbox(bridge) == []
    assert getattr(drain(tmp_path, "asker")[0], "_owner_directives", []) == []


def test_an_appended_answer_whose_receipt_failed_stays_with_its_paused_recipient(tmp_path, monkeypatch):
    from ouroboros import question_answers as qa
    app, bridge, q, task = runtime(tmp_path, monkeypatch)
    real = qa._update_delivery
    def lose_receipt(root, asker, quiz, changes):
        if changes.get("mailbox_at"):
            raise OSError("mailbox append saved; delivery receipt unavailable")
        return real(root, asker, quiz, changes)
    monkeypatch.setattr(qa, "_update_delivery", lose_receipt)
    assert answer(app).status_code == 503
    monkeypatch.setattr(qa, "_update_delivery", real)
    task["_owner_hold"] = True
    assert answer(app).status_code == 200
    assert _inbox(bridge) == [], "the copy in its mailbox is the receipt: no fresh head"
    assert debt(tmp_path)["mailbox_at"], "the missing receipt is repaired from the mailbox"
    assert task["_owner_hold"] is True
    task.pop("_owner_hold")
    ctx, _ = drain(tmp_path, "asker")
    assert [r["content"] for r in ctx._owner_directives] == ["exact answer"]
