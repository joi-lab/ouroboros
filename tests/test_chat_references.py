"""Reply facts and addresses name persisted rows, independently of their transport/view."""
import json

import pytest

from ouroboros.chat_chain import format_address, row_address
from ouroboros.chat_references import resolve_reply
from supervisor import message_bus
from tests.test_chat_attachments import _history, _web_bridge


def test_reply_is_host_resolved_and_replay_uses_the_same_address(tmp_path, monkeypatch):
    bridge, echoes = _web_bridge(tmp_path, monkeypatch)
    original = message_bus.log_chat("out", 1, 0, "  One\n\t" + "word " * 80, ts="2026-10-01T00:00:00Z")
    address = format_address(row_address(original))
    bridge.handle_web_message("this one", client_message_id="reply", task_metadata={"reply_to": address})
    row = json.loads((tmp_path / "logs/chat.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["reply_to"]["snippet"] == ("One " + "word " * 80)[:160]
    assert row["reply_to"]["role"] == "assistant"
    assert echoes[-1]["reply_to"] == row["reply_to"]
    assert echoes[-1]["row_address"] == format_address(row_address(row))
    replay = next(r for r in _history(tmp_path) if r.get("client_message_id") == "reply")
    assert replay["row_address"] == echoes[-1]["row_address"]
    assert replay["reply_to"] == row["reply_to"]
    queued = bridge._inbox.get_nowait()
    assert queued["task_metadata"]["reply_reference"] == row["reply_to"]
    bridge.handle_web_message("this one", client_message_id="reply", task_metadata={"reply_to": address})
    assert bridge._inbox.empty()
    with pytest.raises(ValueError, match="different message"):
        bridge.handle_web_message("this one", client_message_id="reply", task_metadata={"reply_to": "task:other"})


def test_reply_survives_rotation_and_missing_reference_stays_explicit(tmp_path):
    row = {"chat_id": 1, "ts": "2026-10-01T00:00:00Z", "direction": "in", "text": "yes"}
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive/chat_20261002T000000.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    address = format_address(row_address(row))
    assert resolve_reply(tmp_path, address)["snippet"] == "yes"
    assert resolve_reply(tmp_path, "task:missing") == {"address": "task:missing", "unresolved": True}
    assert resolve_reply(tmp_path, address[:-12] + "000000000000")["unresolved"] is True
    with pytest.raises(ValueError, match="string"):
        resolve_reply(tmp_path, {"snippet": "forged"})


def test_skill_ingress_shares_reply_and_address_with_web(tmp_path, monkeypatch):
    bridge, echoes = _web_bridge(tmp_path, monkeypatch)
    row = message_bus.log_chat("out", 1, 0, "Pick this", ts="2026-10-01T00:00:00Z")
    address = format_address(row_address(row))
    accepted, rejoined = message_bus.accept_local_message(
        bridge, tmp_path, "yes", chat_id=1, user_id=1, client_message_id="skill-reply",
        source="skill:telegram", task_metadata={"reply_to": address})
    assert not rejoined and accepted["reply_to"]["address"] == address
    queued = bridge._inbox.get_nowait()
    message_bus.record_inbound_message(bridge, queued, chat_id=1, user_id=1,
        client_message_id="skill-reply", text="yes", ts=accepted["ts"])
    assert echoes[-1]["row_address"] == format_address(row_address(accepted))
    assert echoes[-1]["reply_to"] == accepted["reply_to"]


def test_progress_address_resolves_without_hashing_projection_fields(tmp_path):
    from ouroboros.chat_references import projection_fields
    from ouroboros.utils import append_jsonl
    row = {"chat_id": 1, "ts": "2026-10-01T01:00:00Z", "task_id": "worker", "text": "Testing"}
    append_jsonl(tmp_path / "logs/progress.jsonl", row)
    address = projection_fields(row)["row_address"]
    assert resolve_reply(tmp_path, address)["snippet"] == "Testing"
    assert resolve_reply(tmp_path, address + "0")["unresolved"] is True


def test_answer_row_uses_the_turns_host_origin(tmp_path, monkeypatch):
    from ouroboros.project_dialogue import build_owner_message_ref
    from ouroboros.task_results import write_task_result
    bridge, echoes = _web_bridge(tmp_path, monkeypatch)
    origin = message_bus.log_chat("in", 1, 1, "Original request", client_message_id="origin")
    write_task_result(tmp_path, "work", "running", origin_message_ref=build_owner_message_ref(
        chat_id=1, client_message_id="origin", text=origin["text"], ts=origin["ts"]))
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    message_bus.send_with_budget(1, "Answer", task_id="work")
    row = json.loads((tmp_path / "logs/chat.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["reply_to"]["address"] == format_address(row_address(origin))
    assert echoes[-1]["row_address"] == format_address(row_address(row))
    assert echoes[-1]["reply_to"] == row["reply_to"]
