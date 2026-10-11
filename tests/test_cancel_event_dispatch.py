"""A held cancellation file pass does not hold supervisor intake or another Stop."""

import threading
from types import SimpleNamespace

from supervisor import events_runtime_controls as controls, queue


def test_cancel_dispatch_is_off_intake_and_deduplicates_only_current_task(tmp_path, monkeypatch):
    entered, release, other = threading.Event(), threading.Event(), threading.Event()
    calls, threads = [], []
    real_thread = threading.Thread

    def tracked_thread(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(controls, "threading", SimpleNamespace(Thread=tracked_thread))
    monkeypatch.setattr(controls, "_cancel_events_in_flight", set())

    def drive(task_id):
        calls.append(task_id)
        if task_id == "slow":
            entered.set()
            assert release.wait(5)
        else:
            other.set()
        return queue.CANCEL_CANCELLED

    monkeypatch.setattr(queue, "drive_cancel_intent_scope", drive)
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, load_state=lambda: {"owner_chat_id": 1},
                          send_with_budget=lambda *a, **k: None)
    try:
        controls._handle_cancel_task({"task_id": "slow"}, ctx)
        assert entered.wait(5)
        controls._handle_cancel_task({"task_id": "slow", "requested_task_id": "same"}, ctx)
        controls._handle_cancel_task({"task_id": "other"}, ctx)
        assert other.wait(5), "another Stop queued behind the held file-copy operation"
        assert calls.count("slow") == 1 and len(threads) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert not controls._cancel_events_in_flight


def test_failed_dispatch_releases_local_latch_for_existing_watchdog(tmp_path, monkeypatch):
    monkeypatch.setattr(controls, "_cancel_events_in_flight", set())
    attempts = []

    def fail_start(**kwargs):
        attempts.append(True)
        raise RuntimeError("cannot create thread")

    monkeypatch.setattr(controls, "threading", SimpleNamespace(Thread=fail_start))
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path)
    controls._handle_cancel_task({"task_id": "held"}, ctx)
    controls._handle_cancel_task({"task_id": "held"}, ctx)
    assert attempts == [True, True]
    assert not controls._cancel_events_in_flight
