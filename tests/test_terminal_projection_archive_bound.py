"""The Done card's duplicate scan is bounded by rotation time, and the card settles on the ack.

Owner (21:09 #3, 18:56): never re-read the whole archived chat (95 MB) before writing
Done — check only what can hold the row. ``_already_in_chat`` reads the live segment and
the archives rotated at or after the second the readiness was minted (the rotator takes
its stamp under the append lock, so an archive named before that second was sealed
before the row could exist); it never trusts row timestamps (Astra R3: a delayed worker
row sits behind a newer summary) and never opens older segments. A readiness minted in
the same call skips the scan. The delivery ack re-enters the settle owner, so a card held
behind its owed answer follows that answer without waiting for a sweep.
"""
from __future__ import annotations

import json
import pathlib
from collections import deque
from types import SimpleNamespace

from ouroboros import terminal_projection as tp
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.utils import append_jsonl
from supervisor import events_chat_delivery as delivery
from supervisor import terminal_delivery as td

TOKEN = "ready-token"
SUMMARY = {"type": "task_summary", "summary_id": "task-terminal:T", "terminal_projection_token": TOKEN,
           "ts": "2026-10-14T10:01:00+00:00", "text": "Done. Root task T."}


def _write(path: pathlib.Path, *rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _opened(monkeypatch) -> list:
    opened: list = []
    original = pathlib.Path.open

    def spy(self, *args, **kwargs):
        if self.name.endswith(".jsonl"):
            opened.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", spy)
    return opened


def test_a_backdated_live_row_does_not_hide_a_rotated_summary(tmp_path):
    """Astra R3: the summary sits in an archive rotated at 10:02; a delayed 09:59 worker row
    was appended to the live file afterwards; readiness was minted at 10:00."""
    _write(tmp_path / "archive" / "chat_20261014T100200.jsonl", SUMMARY)
    _write(tmp_path / "logs" / "chat.jsonl", {"ts": "2026-10-14T09:59:00+00:00", "text": "late"})
    assert tp._already_in_chat(tmp_path, SUMMARY, since_ts="2026-10-14T10:00:00+00:00") == SUMMARY


def test_archives_rotated_before_the_mint_are_never_opened(tmp_path, monkeypatch):
    """Bytes read are bounded: an older segment holding the very same token is not even opened
    (a sealed segment cannot hold a row appended after its rotation)."""
    _write(tmp_path / "archive" / "chat_20261014T095900.jsonl", SUMMARY)
    _write(tmp_path / "archive" / "chat_20261014T095000.jsonl", {"text": "ancient"})
    _write(tmp_path / "archive" / "chat_20261014T100100.jsonl", {"text": "newer, no summary"})
    _write(tmp_path / "logs" / "chat.jsonl", {"text": "live"})
    opened = _opened(monkeypatch)
    assert tp._already_in_chat(tmp_path, SUMMARY, since_ts="2026-10-14T10:00:00.250000+00:00") is None
    assert opened == ["chat.jsonl", "chat_20261014T100100.jsonl"], "stops at the first older segment"
    opened.clear()
    # The bound is inclusive at second resolution: a segment stamped at the mint second is read.
    assert tp._already_in_chat(tmp_path, SUMMARY, since_ts="2026-10-14T09:59:00.999999+00:00") == SUMMARY
    assert opened == ["chat.jsonl", "chat_20261014T100100.jsonl", "chat_20261014T095900.jsonl"]
    opened.clear()
    # Without a mint time (legacy readiness) every segment is opened, newest first.
    assert tp._already_in_chat(tmp_path, SUMMARY) == SUMMARY
    assert opened == ["chat.jsonl", "chat_20261014T100100.jsonl", "chat_20261014T095900.jsonl",
                      "chat_20261014T095000.jsonl"]


def test_an_unparsable_archive_name_is_read_and_a_suffix_keeps_its_stamp(tmp_path):
    _write(tmp_path / "archive" / "chat_legacy.jsonl", SUMMARY)
    _write(tmp_path / "logs" / "chat.jsonl", {"text": "live"})
    assert tp._already_in_chat(tmp_path, SUMMARY, since_ts="2026-10-14T10:00:00+00:00") == SUMMARY
    (tmp_path / "archive" / "chat_legacy.jsonl").unlink()
    _write(tmp_path / "archive" / "chat_20261014T100000_1.jsonl", SUMMARY)
    assert tp._already_in_chat(tmp_path, SUMMARY, since_ts="2026-10-14T10:00:00+00:00") == SUMMARY
    assert tp._already_in_chat(tmp_path, SUMMARY, since_ts="2026-10-14T10:00:01+00:00") is None


def test_the_rotator_stamps_its_archive_under_the_append_lock(tmp_path, monkeypatch):
    """Astra R2 rotation repro, with a clock that moves: the summary appended (and its readiness
    minted) after the rotator decided to rotate but before it took the lock lands in an archive
    stamped AFTER the mint, so the bounded scan still finds it."""
    from supervisor import state

    live = tmp_path / "logs" / "chat.jsonl"
    append_jsonl(live, {"type": "message", "text": "old"})
    clock = iter(["2026-10-14T10:00:00.000000+00:00", "2026-10-14T10:00:03.000000+00:00"])
    monkeypatch.setattr(state, "utc_now_iso", lambda: next(clock))
    actual_acquire = state.acquire_exclusive_file_lock
    minted: dict = {}

    def interleave_before_rotation_lock(*args, **kwargs):
        # Another actor reads the same clock to mint readiness and appends the summary
        # before the rotator takes the lock. A rotator that chose its stamp BEFORE the
        # lock would have consumed the earlier tick and named the archive before this mint.
        minted["ts"] = state.utc_now_iso()
        append_jsonl(live, {**SUMMARY, "ts": minted["ts"]})
        return actual_acquire(*args, **kwargs)

    monkeypatch.setattr(state, "acquire_exclusive_file_lock", interleave_before_rotation_lock)
    state.rotate_jsonl_log_if_needed(tmp_path, "chat.jsonl", "chat", max_bytes=1)
    summary = {**SUMMARY, "ts": minted["ts"]}
    assert minted["ts"].startswith("2026-10-14T10:00:00")
    (segment,) = list((tmp_path / "archive").glob("chat_*.jsonl"))
    assert segment.name == "chat_20261014T100003.jsonl", "the stamp is taken under the lock"
    assert summary in [json.loads(line) for line in segment.read_text(encoding="utf-8").splitlines()]
    assert tp._already_in_chat(tmp_path, summary, since_ts=minted["ts"]) == summary


def test_a_readiness_minted_in_the_same_call_skips_the_scan(tmp_path, monkeypatch):
    from ouroboros.project_dialogue import append_terminal_task_projection

    scans: list = []
    monkeypatch.setattr(tp, "_already_in_chat", lambda *a, **k: scans.append(k) or None)
    stored = write_task_result(tmp_path, "fresh", "completed", _terminal_observed=True, chat_id=1, result="Answer.")
    assert append_terminal_task_projection(tmp_path, "fresh", {"id": "fresh", "chat_id": 1}, stored,
                                           {"status": "completed", "chat_id": 1})
    assert scans == [], "no row can carry a token minted in this very call"
    # A later settle of readiness recorded earlier (an append/receipt crash) does scan, bounded.
    row = load_task_result(tmp_path, "fresh")
    write_task_result(tmp_path, "fresh", "completed", canonical_terminal_projection=None)
    assert tp.settle_terminal_projection(tmp_path, "fresh") in {tp.SETTLEMENT_SETTLED, tp.SETTLEMENT_DEFERRED}
    assert scans and scans[0]["since_ts"] == row["canonical_terminal_projection_ready"]["task_done_ts"]


def test_the_delivery_ack_settles_a_card_held_behind_its_answer(tmp_path, monkeypatch):
    """Ack after ready: task_done minted readiness while the answer was still owed (the card
    deferred); the real send's registration re-enters the settle owner and the card follows."""
    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    tid = "ack-after-ready"
    event = {"type": "send_message", "chat_id": 1, "task_id": tid, "text": "Answer.",
             "delivery_id": td.delivery_id_for(tid, "Answer.")}
    assert td.register_pending_delivery(tmp_path, event)
    write_task_result(tmp_path, tid, "completed", _terminal_observed=True, chat_id=1, result="Answer.")
    assert tp.settle_terminal_projection(tmp_path, tid) == tp.SETTLEMENT_DEFERRED  # readiness minted, owed holds
    assert isinstance(load_task_result(tmp_path, tid)["canonical_terminal_projection_ready"], dict)
    chat = tmp_path / "logs" / "chat.jsonl"
    sent = []

    def send(chat_id, body, **kwargs):
        sent.append(body)
        append_jsonl(chat, {"ts": "2026-10-15T00:00:00Z", "chat_id": chat_id, "text": body, "task_id": tid})

    host = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, append_jsonl=append_jsonl, send_with_budget=send)
    delivery._handle_send_message(event, host)
    rows = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()]
    assert [row.get("text") for row in rows][0] == "Answer."
    assert rows[-1]["summary_id"] == f"task-terminal:{tid}" and rows[-1]["outcome_phase"] == "done"
    assert isinstance(load_task_result(tmp_path, tid)["canonical_terminal_projection"], dict)
    # The buffered duplicate is suppressed and re-enters the same owner: nothing doubles.
    delivery._handle_send_message(event, host)
    rows = [json.loads(line) for line in chat.read_text(encoding="utf-8").splitlines()]
    assert sent == ["Answer."] and len([row for row in rows if row.get("summary_id")]) == 1


def test_an_ack_before_the_row_is_terminal_settles_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "_DELIVERED_MESSAGE_IDS", deque(maxlen=256))
    tid = "ack-before-ready"
    event = {"type": "send_message", "chat_id": 1, "task_id": tid, "text": "Answer.",
             "delivery_id": td.delivery_id_for(tid, "Answer.")}
    assert td.register_pending_delivery(tmp_path, event)
    write_task_result(tmp_path, tid, "running", result="working")
    host = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, append_jsonl=append_jsonl,
                           send_with_budget=lambda *a, **k: None)
    delivery._handle_send_message(event, host)
    assert td.terminal_answer_receipts(tmp_path, tid)["state"] == "delivered"
    stored = load_task_result(tmp_path, tid)
    assert "canonical_terminal_projection_ready" not in stored, "readiness is task_done's to mint"
    assert not (tmp_path / "logs" / "chat.jsonl").exists() or not any(
        json.loads(line).get("summary_id") for line in (tmp_path / "logs" / "chat.jsonl").read_text().splitlines())
