"""The host's card placement rides a task-keyed System row from live frame to replay.

A host fact about a task — an open-delegation custody notice, a late acceptance
verdict — renders inside that task's card. The host names the placement on the
row itself (``card_row``), so no surface has to keep its own list of system
types, and ``card_row_id`` keeps the row one row across live delivery, outbox
replay and history.
"""

import asyncio
import json
from types import SimpleNamespace

from ouroboros.gateway.history import make_chat_history_endpoint
from supervisor import message_bus


NOTICE = "Open delegated execution: the reviewer is still running."
VERDICT = "Acceptance settled after the answer was delivered."
ROW_ID = "final:t1:abc:custody_notice"


def _replayed_rows(tmp_path, chat_id: int = 1) -> dict:
    response = asyncio.run(
        make_chat_history_endpoint(tmp_path)(SimpleNamespace(query_params={"chat_id": str(chat_id)}))
    )
    return {row["text"]: row for row in json.loads(response.body)["messages"]}


def test_card_row_survives_the_stored_row_into_history_replay(tmp_path):
    """The stored row keeps the placement and its identity; a row without a
    placement keeps neither key, and an unknown placement is dropped whole —
    an id alone names nothing the client could attach."""
    def _log(text, meta):
        message_bus.log_chat("system", 1, 7, text, record_type="custody_notice",
                             task_id="t1", message_meta=meta, drive_root=tmp_path)

    _log(NOTICE, {"card_row": "timeline", "card_row_id": ROW_ID})
    _log(VERDICT, {"card_row": "reviews", "card_row_id": ROW_ID})
    _log("An ordinary system line.", {})
    _log("An unknown placement.", {"card_row": "sidebar", "card_row_id": ROW_ID})
    _log("A placement with an oversized id.", {"card_row": "reviews", "card_row_id": "x" * 201})

    rows = _replayed_rows(tmp_path)

    assert rows[NOTICE]["system_type"] == "custody_notice"
    assert rows[NOTICE]["card_row"] == "timeline"
    assert rows[NOTICE]["card_row_id"] == ROW_ID
    assert rows[VERDICT]["card_row"] == "reviews"
    assert rows[VERDICT]["card_row_id"] == ROW_ID
    for text in ("An ordinary system line.", "An unknown placement."):
        assert "card_row" not in rows[text], f"{text} must replay without a placement"
        assert "card_row_id" not in rows[text], f"{text} must replay without a row identity"
    oversized = rows["A placement with an oversized id."]
    assert oversized["card_row"] == "reviews" and "card_row_id" not in oversized


def test_live_chat_frame_carries_the_card_row_placement(tmp_path, monkeypatch):
    """The live frame names the same placement the stored row keeps, so the
    card shows the fact in place on the first delivery, not only on reload."""
    bridge = message_bus.LocalChatBridge({})
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_kw: None)

    bridge.send_message(1, VERDICT, task_id="t1", role="system",
                        system_type="acceptance_late_settlement",
                        progress_meta={"card_row": "reviews", "card_row_id": ROW_ID})

    chats = [row for row in frames if row.get("type") == "chat"]
    assert len(chats) == 1
    assert chats[0]["role"] == "system"
    assert chats[0]["system_type"] == "acceptance_late_settlement"
    assert chats[0]["card_row"] == "reviews"
    assert chats[0]["card_row_id"] == ROW_ID
