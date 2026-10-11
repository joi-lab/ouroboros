from __future__ import annotations

import concurrent.futures
import time
import pytest

from supervisor.active_activity import (
    DirectActivityRegistry,
    get_direct_activity_registry,
    track_direct_activity,
)


def test_registry_basic_lifecycle():
    registry = DirectActivityRegistry()
    assert registry.snapshot() == []

    entry = registry.register(
        "act-1",
        chat_id=1,
        client_message_id="msg-101",
        project_id="proj-alpha",
        kind="direct_chat",
        phase="thinking",
    )
    assert entry.activity_id == "act-1"
    assert entry.chat_id == 1
    assert entry.client_message_id == "msg-101"
    assert entry.project_id == "proj-alpha"
    assert entry.kind == "direct_chat"
    assert entry.phase == "thinking"

    snap = registry.snapshot()
    assert len(snap) == 1
    assert snap[0]["activity_id"] == "act-1"
    assert snap[0]["kind"] == "direct_chat"
    # Snapshot shape is the exact ActiveDirectTurn contract — no extra keys.
    assert set(snap[0].keys()) == {
        "activity_id",
        "chat_id",
        "project_id",
        "client_message_id",
        "kind",
        "phase",
        "started_at",
    }

    assert registry.get("act-1") is entry

    removed = registry.unregister("act-1")
    assert removed is not None
    assert removed.activity_id == "act-1"
    assert registry.get("act-1") is None
    assert registry.snapshot() == []


def test_registry_multi_chat_isolation():
    registry = DirectActivityRegistry()
    registry.register("act-main", chat_id=1, kind="direct_chat")
    registry.register("act-proj-2", chat_id=2, project_id="proj-2", kind="direct_chat")
    registry.register("act-proj-3", chat_id=3, project_id="proj-3", kind="direct_chat")

    assert len(registry.snapshot()) == 3
    assert len(registry.snapshot(chat_id=1)) == 1
    assert registry.snapshot(chat_id=1)[0]["activity_id"] == "act-main"
    assert len(registry.snapshot(chat_id=2)) == 1
    assert registry.snapshot(chat_id=2)[0]["activity_id"] == "act-proj-2"
    assert len(registry.snapshot(chat_id=3)) == 1
    assert len(registry.snapshot(chat_id=99)) == 0

    registry.unregister("act-proj-2")
    assert len(registry.snapshot()) == 2
    assert len(registry.snapshot(chat_id=2)) == 0
    assert len(registry.snapshot(chat_id=1)) == 1
    assert len(registry.snapshot(chat_id=3)) == 1


def test_track_direct_activity_context_manager():
    registry = get_direct_activity_registry()
    registry.clear()

    assert len(registry.snapshot(chat_id=1)) == 0

    with track_direct_activity(
        "act-cm-1",
        chat_id=1,
        client_message_id="msg-cm",
        kind="direct_chat",
        phase="thinking",
    ) as entry:
        assert entry.activity_id == "act-cm-1"
        assert len(registry.snapshot(chat_id=1)) == 1

    # Guaranteed cleanup on clean exit
    assert len(registry.snapshot(chat_id=1)) == 0

    # Guaranteed cleanup on exception
    with pytest.raises(RuntimeError, match="deliberate failure"):
        with track_direct_activity("act-cm-fail", chat_id=1, kind="direct_chat"):
            assert len(registry.snapshot(chat_id=1)) == 1
            raise RuntimeError("deliberate failure")

    assert len(registry.snapshot(chat_id=1)) == 0


def test_registry_concurrent_operations():
    registry = DirectActivityRegistry()

    def worker(worker_id: int):
        for i in range(50):
            aid = f"worker-{worker_id}-act-{i}"
            registry.register(aid, chat_id=worker_id % 5, kind="direct_chat")
            time.sleep(0.0001)
            registry.unregister(aid)

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker, i) for i in range(10)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    assert registry.snapshot() == []
