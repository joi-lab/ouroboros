"""Focused contract tests for durable Plan Review refresh references."""

from __future__ import annotations

import json
import queue
from hashlib import sha256
from types import SimpleNamespace

import pytest

from ouroboros import task_results
from ouroboros.tools import plan_review_references


def _revision(state: dict) -> str:
    serialized = json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(serialized.encode("utf-8")).hexdigest()


def _reference_rows(events: queue.Queue) -> list[dict]:
    rows = []
    while not events.empty():
        event = events.get_nowait()
        if event.get("type") == "log_event" and event.get("data", {}).get("type") == "review_reference":
            rows.append(event["data"])
    return rows


def test_plan_reference_uses_shared_best_effort_log_event_seam(monkeypatch):
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, current_chat_id=23)
    state = {"current_attempt": {"fingerprint": "review-fingerprint"}, "waves": []}
    calls = []

    def capture(event_queue, payload, **kwargs):
        calls.append((event_queue, payload, kwargs))

    monkeypatch.setattr(plan_review_references, "emit_log_event", capture)
    plan_review_references._emit_plan_review_reference(ctx, "task-1", state)

    assert len(calls) == 1
    event_queue, payload, kwargs = calls[0]
    assert event_queue is events
    assert kwargs == {"log_label": "plan-review state reference"}
    assert payload == {
        "type": "review_reference",
        "surface": "plan_review",
        "task_id": "task-1",
        "chat_id": 23,
        "presentation_owner_task_id": "task-1",
        "review_fingerprint": "review-fingerprint",
        "state_revision": _revision(state),
        "ts": payload["ts"],
    }


def test_review_references_without_a_room_live_in_the_hidden_partition(monkeypatch):
    """Doctrine, not a default: a run with no owner-visible room keeps its
    review rows in the hidden partition. Main was a hard fallback, which put
    plan-review rows of unrelated runs into the owner's conversation."""
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events)
    state = {"current_attempt": {"fingerprint": "review-fingerprint"}, "waves": []}
    calls = []

    monkeypatch.setattr(
        plan_review_references,
        "emit_log_event",
        lambda _queue, payload, **_kwargs: calls.append(payload),
    )
    plan_review_references._emit_plan_review_reference(ctx, "task-1", state)

    assert calls[0]["chat_id"] == plan_review_references.HIDDEN_CHAT_ID == 0


def test_plan_reference_preserves_explicit_panel_chat_zero(monkeypatch):
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, current_chat_id=0)
    state = {"current_attempt": {"fingerprint": "review-fingerprint"}, "waves": []}
    calls = []

    monkeypatch.setattr(
        plan_review_references,
        "emit_log_event",
        lambda _queue, payload, **_kwargs: calls.append(payload),
    )
    plan_review_references._emit_plan_review_reference(ctx, "task-1", state)

    assert calls[0]["chat_id"] == 0


def test_review_reference_addresses_the_bound_project_chat(tmp_path):
    """The binding outranks the context chat on BOTH rails: a task turned into a
    project mid-run keeps its origin chat on ctx, so a row addressed from ctx
    alone lands outside the room that holds the work."""
    from ouroboros.projects_registry import bind_task_to_project

    binding = bind_task_to_project(
        tmp_path, "task-bound", "review-ref-proj", 7373, origin={"absent": "system"},
    )
    task_results.write_task_result(tmp_path, "task-bound", "running", result="running")
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, current_chat_id=1, drive_root=tmp_path)

    plan_review_references._record_plan_review_attempt_with_reference(
        ctx, tmp_path, "task-bound", fingerprint="a" * 64, status="open",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert binding["project_chat_id"] == 7373
    assert rows[0]["chat_id"] == 7373
    assert _reference_rows(events)[0]["chat_id"] == 7373


def test_review_reference_addresses_a_corrupt_bindings_store_like_no_binding(tmp_path):
    """The REAL read failure, not a synthetic raise: resolve_project_chat swallows
    every error and answers 0, so a corrupt store behaves exactly like "no binding"
    (D6-6 fail-open). A run with no room of its own lands in the hidden partition;
    a caller that named a chat keeps it, as it did before this seam existed."""
    from supervisor.log_addressing import resolve_project_chat

    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "project_task_bindings.json").write_text("{ not json", encoding="utf-8")
    state = {"current_attempt": {"fingerprint": "review-fingerprint"}, "waves": []}
    assert resolve_project_chat(tmp_path, "task-1", "", "") == 0  # never raises

    roomless: queue.Queue = queue.Queue()
    plan_review_references._emit_plan_review_reference(
        SimpleNamespace(event_queue=roomless, drive_root=tmp_path), "task-1", state,
    )
    assert _reference_rows(roomless)[0]["chat_id"] == plan_review_references.HIDDEN_CHAT_ID

    addressed: queue.Queue = queue.Queue()
    plan_review_references._emit_plan_review_reference(
        SimpleNamespace(event_queue=addressed, current_chat_id=23, drive_root=tmp_path),
        "task-1", state,
    )
    assert _reference_rows(addressed)[0]["chat_id"] == 23


def test_attempt_helper_publishes_immediately_after_the_canonical_write(monkeypatch):
    ctx = SimpleNamespace(event_queue=queue.Queue())
    calls = []
    state = {"current_attempt": {"fingerprint": "a" * 64}}

    monkeypatch.setattr(
        plan_review_references, "record_plan_review_attempt",
        lambda *args, **kwargs: calls.append("attempt") or state,
    )
    monkeypatch.setattr(
        plan_review_references, "_emit_plan_review_reference",
        lambda *args, **kwargs: calls.append("reference"),
    )

    result = plan_review_references._record_plan_review_attempt_with_reference(
        ctx, None, "task-1", fingerprint="a" * 64,
    )

    assert result is state
    assert calls == ["attempt", "reference"]


def test_raw_request_helper_publishes_immediately_after_the_canonical_write(monkeypatch):
    ctx = SimpleNamespace(event_queue=queue.Queue())
    calls = []

    monkeypatch.setattr(
        plan_review_references, "record_raw_plan_request_attempt",
        lambda *args, **kwargs: calls.append("raw") or "a" * 64,
    )
    monkeypatch.setattr(
        plan_review_references, "_emit_plan_review_reference",
        lambda *args, **kwargs: calls.append("reference"),
    )

    fingerprint = plan_review_references._record_raw_plan_request_with_reference(
        ctx, None, "task-1", {"plan": "invalid"}, reason="plan_input_invalid",
    )

    assert fingerprint == "a" * 64
    assert calls == ["raw", "reference"]


def test_cycles_exhausted_helper_publishes_each_write_before_the_typed_event(monkeypatch):
    ctx = SimpleNamespace(event_queue=queue.Queue())
    calls = []
    marked = {"request_fingerprint": "a" * 64, "cycles_exhausted": True}
    attempt = {"current_attempt": {"fingerprint": "b" * 64}}

    monkeypatch.setattr(
        plan_review_references, "mark_plan_review_cycles_exhausted",
        lambda *args, **kwargs: calls.append("mark") or marked,
    )
    monkeypatch.setattr(
        plan_review_references, "record_plan_review_attempt",
        lambda *args, **kwargs: calls.append("attempt") or attempt,
    )
    monkeypatch.setattr(
        plan_review_references, "_emit_plan_review_reference",
        lambda *args, **kwargs: calls.append("reference"),
    )

    result = plan_review_references._record_cycles_exhausted_with_references(
        ctx, None, "task-1", wave_fingerprint="a" * 64,
        attempt_fingerprint="b" * 64, cycles_paid=2, cap=2,
    )

    assert result is marked
    assert calls == ["mark", "reference", "attempt", "reference"]


def test_first_cycles_exhausted_revision_is_published_if_second_write_fails(
    tmp_path, monkeypatch,
):
    fingerprint = "a" * 64
    task_results.write_task_result(tmp_path, "task-1", "running", result="running")
    task_results.record_plan_review_attempt(
        tmp_path, "task-1", fingerprint=fingerprint,
    )
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, current_chat_id=23)

    def fail_second_write(*args, **kwargs):
        raise OSError("second canonical write failed")

    monkeypatch.setattr(plan_review_references, "record_plan_review_attempt", fail_second_write)

    with pytest.raises(OSError, match="second canonical write failed"):
        plan_review_references._record_cycles_exhausted_with_references(
            ctx, tmp_path, "task-1", wave_fingerprint=fingerprint,
            attempt_fingerprint=fingerprint, cycles_paid=2, cap=2,
        )

    durable = task_results.load_plan_review_state(tmp_path, "task-1")
    refs = _reference_rows(events)
    assert len(refs) == 1
    assert refs[0]["review_fingerprint"] == fingerprint
    assert refs[0]["state_revision"] == _revision(durable)


def test_plan_reference_is_durable_on_progress_rail_and_state_stays_authority(tmp_path):
    task_results.write_task_result(tmp_path, "task-1", "running", result="running")
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(
        event_queue=events, current_chat_id=23, drive_root=tmp_path,
    )

    state = plan_review_references._record_plan_review_attempt_with_reference(
        ctx, tmp_path, "task-1", fingerprint="c" * 64, status="open",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0] == {
        "type": "review_reference",
        "surface": "plan_review",
        "task_id": "task-1",
        "chat_id": 23,
        "presentation_owner_task_id": "task-1",
        "review_fingerprint": "c" * 64,
        "state_revision": _revision(state),
        "ts": rows[0]["ts"],
        "direction": "out",
        "is_progress": True,
        "user_id": 0,
        "text": "",
        "content": "",
        "format": "",
    }
    assert task_results.load_plan_review_state(tmp_path, "task-1") == state


def test_plan_reference_append_failure_is_not_misreported_as_durable(tmp_path, monkeypatch):
    task_results.write_task_result(tmp_path, "task-1", "running", result="running")
    ctx = SimpleNamespace(event_queue=queue.Queue(), drive_root=tmp_path)
    monkeypatch.setattr(plan_review_references, "append_jsonl", lambda *_a, **_k: False)

    plan_review_references._record_plan_review_attempt_with_reference(
        ctx, tmp_path, "task-1", fingerprint="d" * 64, status="open",
    )

    durable = task_results.load_plan_review_state(tmp_path, "task-1")
    assert durable["current_attempt"]["fingerprint"] == "d" * 64
    assert not (tmp_path / "logs" / "progress.jsonl").exists()
    assert _reference_rows(ctx.event_queue)[0]["review_fingerprint"] == "d" * 64


def test_plan_reference_append_exception_keeps_live_invalidation(monkeypatch, tmp_path):
    task_results.write_task_result(tmp_path, "task-1", "running", result="running")
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, drive_root=tmp_path)

    def fail_append(*_args, **_kwargs):
        raise OSError("progress rail unavailable")

    monkeypatch.setattr(plan_review_references, "append_jsonl", fail_append)
    plan_review_references._record_plan_review_attempt_with_reference(
        ctx, tmp_path, "task-1", fingerprint="e" * 64, status="open",
    )

    durable = task_results.load_plan_review_state(tmp_path, "task-1")
    assert durable["current_attempt"]["fingerprint"] == "e" * 64
    assert _reference_rows(events)[0]["review_fingerprint"] == "e" * 64


def test_cycles_exhausted_writes_continue_when_reference_rail_fails(tmp_path, monkeypatch):
    fingerprint = "f" * 64
    task_results.write_task_result(tmp_path, "task-1", "running", result="running")
    task_results.record_plan_review_wave(
        tmp_path, "task-1", {
            "request_fingerprint": fingerprint, "aggregate": "REVIEW_REQUIRED",
            "closed": False, "paid": True, "cycle_index": 1,
            "spec": {}, "findings": [], "dispositions": [],
        },
    )
    events: queue.Queue = queue.Queue()
    ctx = SimpleNamespace(event_queue=events, drive_root=tmp_path)
    monkeypatch.setattr(plan_review_references, "append_jsonl", lambda *_a, **_k: False)

    plan_review_references._record_cycles_exhausted_with_references(
        ctx, tmp_path, "task-1", wave_fingerprint=fingerprint,
        attempt_fingerprint=fingerprint, cycles_paid=1, cap=1,
    )

    durable = task_results.load_plan_review_state(tmp_path, "task-1")
    assert durable["waves"][-1]["cycles_exhausted"] is True
    assert durable["current_attempt"]["status"] == "cycles_exhausted"
    assert len(_reference_rows(events)) == 2
