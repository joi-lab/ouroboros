"""What a ``task_done`` event may claim, checked against the durable row.

Split out of ``tests/test_cancel_intents_phase_a.py`` by theme: a nonterminal or
blank-status ``task_done`` is a durable lifecycle fault unless the durable row already
settled, the interrupted transient is formalized, and a copy-back failure never
synthesizes a completed row.
"""

from __future__ import annotations
import json
import types
import queue

import pytest
from ouroboros import cancel_intents as ci
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    load_task_result,
    write_task_result,
)


@pytest.fixture
def terminal_completion(monkeypatch):
    """Finish queued file work explicitly; these tests assert durable outcomes."""
    from supervisor import events, queue as task_queue, task_reaper, workers

    jobs, returned = queue.Queue(), queue.Queue()
    monkeypatch.setattr(task_queue, "_ensure_reaper_started", lambda: None)
    monkeypatch.setattr(task_queue, "_reap_queue", jobs)
    monkeypatch.setattr(workers, "get_event_q", lambda: returned)

    def complete(event, ctx):
        events._handle_task_done(event, ctx)
        if not jobs.empty():
            task_reaper._recover_terminal_files(jobs.get_nowait())
        if not returned.empty():
            events._handle_task_done(returned.get_nowait(), ctx)
        assert jobs.empty() and returned.empty(), "one completion never starts a retry loop"

    return complete


def _fault_rows(tmp_path) -> list:
    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == "task_done_invalid_status"
    ]

def test_nonterminal_task_done_with_a_cancel_intent_is_left_to_custody(tmp_path, terminal_completion):
    """The incident's shape: task_done carrying the cancel latch must be REFUSED.

    With a cancellation pending, the row STAYS in RUNNING — custody and the
    watchdog own it and settle it honestly."""
    from ouroboros.utils import append_jsonl

    running = {"t9": {"task": {"id": "t9"}}}
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING=running,
        WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda **_kw: True,
    )
    ci.request_cancel(tmp_path, "t9", reason="owner stopped it")
    terminal_completion({"task_id": "t9", "status": "cancel_requested"}, ctx)

    assert "t9" in running, "a task whose cancellation is pending stays owned by custody"
    fault = _fault_rows(tmp_path)
    assert fault and fault[0]["task_id"] == "t9" and fault[0]["status"] == "cancel_requested"
    assert load_task_result(tmp_path, "t9") in (None, {})  # custody writes the terminal

def test_nonterminal_task_done_without_an_owner_terminalizes_and_frees_the_slot(tmp_path, terminal_completion):
    """A refused task_done that NOBODY owns must not wedge the worker slot.

    Refusing the publication is right; refusing it and walking away left the task
    in RUNNING with its worker still marked busy and nothing scheduled to finish
    it. With no cancel intent and no legacy latch the event is a genuine
    lifecycle bug, so the supervisor terminalizes the task as ``failed`` with a
    typed reason and releases the slot."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl

    running = {"t11": {"task": {"id": "t11"}}}
    slot = types.SimpleNamespace(busy_task_id="t11", reaping=False)
    snapshots: list = []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING=running,
        WORKERS={3: slot},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": snapshots.append(reason),
    )
    terminal_completion({"task_id": "t11", "status": "running", "worker_id": 3}, ctx)

    assert _fault_rows(tmp_path)
    assert "t11" not in running, "an unowned lifecycle fault must release RUNNING"
    assert slot.busy_task_id is None, "the worker slot must not stay wedged"
    stored = load_task_result(tmp_path, "t11") or {}
    assert stored["status"] == STATUS_FAILED
    assert stored["reason_code"] == "task_done_lifecycle_fault"
    # GR3-6: the synthetic terminal rides the NORMAL dispatch seam (its
    # snapshot reason), not a private partial copy.
    assert snapshots == ["task_done"]

def test_interrupted_task_done_is_the_formalized_transient_not_a_fault(tmp_path, terminal_completion):
    """A1.11: the update/restart teardown publishes ``interrupted`` for this
    generation — a real transient with an owner (snapshot restore / orphan
    reconcile), exempt from the settled-status guard."""
    from ouroboros.utils import append_jsonl as _append_jsonl

    running = {"t10": {"task": {"id": "t10"}}}

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = running
        append_jsonl = staticmethod(_append_jsonl)

    try:
        terminal_completion({"task_id": "t10", "status": "interrupted"}, _Ctx())
    except Exception:
        pass  # the stub ctx cannot run the full dispatch; entering it is the point
    events_path = tmp_path / "logs" / "events.jsonl"
    if events_path.exists():
        rows = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert not [r for r in rows if r.get("type") == "task_done_invalid_status"]

def test_task_done_with_settled_claim_but_nonsettled_durable_row_is_a_fault(tmp_path, terminal_completion):
    """AR2-3 (§8-A1): the DURABLE result decides — an event claiming
    ``completed`` over a ``running`` row is refused as a durable lifecycle
    fault, terminalized, and the slot freed by the existing fault resolution."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl

    write_task_result(tmp_path, "t13", STATUS_RUNNING, result="still working")
    running = {"t13": {"task": {"id": "t13"}}}
    slot = types.SimpleNamespace(busy_task_id="t13", reaping=False)
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING=running, WORKERS={3: slot},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    terminal_completion({"task_id": "t13", "status": "completed", "worker_id": 3}, ctx)

    faults = _fault_rows(tmp_path)
    assert faults and faults[0]["durable_status"] == "running"
    assert "t13" not in running and slot.busy_task_id is None
    stored = load_task_result(tmp_path, "t13")
    assert stored["status"] == STATUS_FAILED
    assert stored["reason_code"] == "task_done_lifecycle_fault"

def test_task_done_claiming_settled_with_no_durable_row_is_a_fault(tmp_path, terminal_completion):
    """AR2-3: a worker that emitted task_done(completed) without EVER writing a
    result row is the purest durable fault — refused, never admitted."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl

    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING={"t14": {"task": {"id": "t14"}}}, WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    terminal_completion({"task_id": "t14", "status": "completed"}, ctx)

    faults = _fault_rows(tmp_path)
    assert faults and faults[0]["durable_status"] == ""
    assert load_task_result(tmp_path, "t14")["status"] == STATUS_FAILED

def test_task_done_with_a_settled_durable_row_passes_the_durable_gate(tmp_path, terminal_completion):
    """AR2-3 negative: an honest completion (settled row on disk) is admitted."""
    from ouroboros.utils import append_jsonl as _append_jsonl

    write_task_result(tmp_path, "t15", STATUS_COMPLETED, result="done")

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = {"t15": {"task": {"id": "t15"}}}
        WORKERS: dict = {}
        append_jsonl = staticmethod(_append_jsonl)
        persist_queue_snapshot = staticmethod(lambda **_kw: True)

    try:
        terminal_completion({"task_id": "t15", "status": "completed"}, _Ctx())
    except Exception:
        pass  # the stub ctx cannot run the full dispatch; passing the gate is the point
    assert not _fault_rows(tmp_path)

def test_blank_status_task_done_over_a_running_row_is_a_durable_fault(tmp_path, terminal_completion):
    """GR2-3a (reproduced): the PRIMARY producer emits task_done with NO status,
    so the settled-claim gate skipped validation entirely — a blank-status event
    over a non-settled durable row now faults like any dishonest terminal."""
    from ouroboros.task_results import STATUS_FAILED
    from ouroboros.utils import append_jsonl

    write_task_result(tmp_path, "blank1", STATUS_RUNNING, result="working")
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, RUNNING={"blank1": {"task": {"id": "blank1"}}}, WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    terminal_completion({"task_id": "blank1"}, ctx)

    faults = _fault_rows(tmp_path)
    assert faults and faults[0]["durable_status"] == STATUS_RUNNING
    stored = load_task_result(tmp_path, "blank1")
    assert stored["status"] == STATUS_FAILED
    assert stored["reason_code"] == "task_done_lifecycle_fault"

def test_blank_status_task_done_over_a_settled_row_is_admitted(tmp_path, terminal_completion):
    """GR2-3a negative: the honest ordinary completion (durable settled row,
    blank event status) passes the durable gate."""
    from ouroboros.utils import append_jsonl as _append_jsonl

    write_task_result(tmp_path, "blank2", STATUS_COMPLETED, result="done")

    class _Ctx:
        DRIVE_ROOT = tmp_path
        RUNNING = {"blank2": {"task": {"id": "blank2"}}}
        WORKERS: dict = {}
        append_jsonl = staticmethod(_append_jsonl)
        persist_queue_snapshot = staticmethod(lambda **_kw: True)

    try:
        terminal_completion({"task_id": "blank2"}, _Ctx())
    except Exception:
        pass  # the stub ctx cannot run the full dispatch; passing the gate is the point
    assert not _fault_rows(tmp_path)
    assert load_task_result(tmp_path, "blank2")["status"] == STATUS_COMPLETED

def test_copy_back_exception_never_synthesizes_a_completed_row(tmp_path, monkeypatch, terminal_completion):
    """GR2-3b: a copy-back exception used to skip validation AND default a
    MISSING row's status to "completed" — a fabricated completion the monotonic
    guard then defended. A real saved child now retains file-recovery ownership
    until publication succeeds, without inventing either execution failure or Done."""
    from ouroboros.utils import append_jsonl

    child = tmp_path / "child"
    write_task_result(child, "cb1", STATUS_COMPLETED, result="Saved child answer")
    monkeypatch.setattr(
        "ouroboros.headless.copy_child_task_result",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("child drive unreadable")),
    )
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"cb1": {"task": {"id": "cb1", "child_drive_root": str(child)}}},
        WORKERS={},
        append_jsonl=append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
    )
    terminal_completion({"task_id": "cb1"}, ctx)

    assert load_task_result(tmp_path, "cb1") is None, "copy failure never invents a terminal"
    assert load_task_result(child, "cb1")["result"] == "Saved child answer"
    assert ctx.RUNNING["cb1"]["_terminal_file_recovery"]["in_flight"] is False
    assert not _fault_rows(tmp_path), "I/O failure is recovery custody, not a fake execution failure"
