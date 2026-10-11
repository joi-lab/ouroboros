"""Owner Pause and Continue keep the ASGI loop free and keep their custody (Batch4).

``POST /api/tasks/{id}/pause`` and ``/continue`` take the supervisor queue lock
and write durable records (the root's fence under its launch lock, the
Continue claim, the queue snapshot). Called inline, a held lock or a slow disk
stalled the whole event loop behind them: health, Stop and Panic, and every
other owner control waited. The accept steps now run through the gateway's
``run_sync_to_completion``: consumer regressions below drive the REAL route
handlers on one event loop while another thread holds the queue lock or one
root's launch lock, and prove that health and unrelated controls answer
meanwhile. A request cancelled (asyncio or anyio, the way a dropped client or
server shutdown cancels it) while its acceptance stands on the held lock does
not return before that one acceptance settled; the retry with the same
``request_id``/nonce rejoins it instead of acting twice.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route

from ouroboros.gateway.state import api_health
from ouroboros.gateway.task_pause import api_task_pause, owner_tree_control_routes
from tests._budget_pause_exact_helpers import _install_queue
from tests.test_owner_continue import NONCE, _interrupted
from tests.test_task_http_custody import _cancel_while_held


def _app():
    return Starlette(routes=[Route("/api/health", api_health), *owner_tree_control_routes()])


def _live_root(tmp_path, workers, task_id):
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    write_task_result(tmp_path, task_id, STATUS_RUNNING, chat_id=0, root_task_id=task_id)
    workers.RUNNING[task_id] = {"task": {"id": task_id, "type": "task", "chat_id": 0, "root_task_id": task_id},
                                "worker_id": len(workers.RUNNING), "attempt": 1}


def _mark_entry(monkeypatch, module, name):
    """Flag the moment the handler hands its accept step over (the real step still runs)."""
    entered = threading.Event()
    original = getattr(module, name)

    def marked(*args, **kwargs):
        entered.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, marked)
    return entered


class _Holder:
    """Another thread holding a lock the accept step needs (the assignment tick, a launch)."""

    def __init__(self, acquire, limit, release=None):
        self.held, self.timed_out = threading.Event(), threading.Event()
        self.release = release or threading.Event()

        def hold():
            with acquire():
                self.held.set()
                if not self.release.wait(limit):
                    self.timed_out.set()

        self.thread = threading.Thread(target=hold, name="control-lock-holder", daemon=True)
        self.thread.start()
        assert self.held.wait(5)

    def finish(self):
        self.release.set()
        self.thread.join(10)


def _events(tmp_path, kind):
    path = tmp_path / "logs" / "events.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [row for row in rows if row.get("type") == kind]


async def _answered_while_held(client, pending, holder):
    """Health answers at once and the held control is still standing on its lock."""
    started = time.monotonic()
    health = await asyncio.wait_for(client.get("/api/health"), 3)
    elapsed = time.monotonic() - started
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert not holder.timed_out.is_set(), "the event loop was blocked until the lock holder gave up"
    assert elapsed < 2.0, f"health answered only after {elapsed:.2f}s"
    assert not pending.done(), "the control answered without its acceptance"


@pytest.mark.parametrize("control", ["pause", "continue"])
def test_a_held_queue_lock_never_stalls_health(tmp_path, monkeypatch, control):
    from supervisor import continuation_admission, owner_pause_control

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    if control == "pause":
        _live_root(tmp_path, workers, "root-a")
        entered = _mark_entry(monkeypatch, owner_pause_control, "request_owner_pause")
        url, body = "/api/tasks/root-a/pause", {"request_id": "press-a"}
    else:
        _interrupted(tmp_path)
        entered = _mark_entry(monkeypatch, continuation_admission, "admit_continuation")
        url, body = "/api/tasks/pred-1/continue", {"action_nonce": NONCE}
    holder = _Holder(lambda: queue._queue_lock, 6.0)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://owner.test") as client:
            pending = asyncio.create_task(client.post(url, json=body))
            assert await asyncio.to_thread(entered.wait, 5), "the control never reached its accept step"
            await _answered_while_held(client, pending, holder)
            holder.finish()
            return await asyncio.wait_for(pending, 10)

    try:
        response = asyncio.run(scenario())
    finally:
        holder.finish()
    assert not holder.timed_out.is_set()
    assert response.status_code == 200, response.text
    if control == "pause":
        assert response.json()["state"] == "requested"
        assert queue.BUDGET_ROOT_FENCES["root-a"]["cause"] == "owner_pause"
    else:
        successor = response.json()["successor_task_id"]
        assert [task["id"] for task in workers.PENDING] == [successor]


def test_a_held_root_launch_lock_never_stalls_health_or_another_roots_controls(tmp_path, monkeypatch):
    """One root's fence write stands on its launch lock (a launch registering, a slow
    disk). Health, the Pause of ANOTHER root and a Continue of an interrupted root
    are served meanwhile; the held Pause then lands its own fence."""
    from ouroboros.owner_pause import launch_lock, read_fence
    from supervisor import owner_pause_control

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    _live_root(tmp_path, workers, "root-a")
    _live_root(tmp_path, workers, "root-b")
    _interrupted(tmp_path)
    entered = _mark_entry(monkeypatch, owner_pause_control, "request_owner_pause")
    # The launch lock's own acquire gives up after 4s: release well before that.
    holder = _Holder(lambda: launch_lock(tmp_path, "root-a"), 3.5)

    async def scenario():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://owner.test") as client:
            held = asyncio.create_task(client.post("/api/tasks/root-a/pause", json={"request_id": "press-a"}))
            assert await asyncio.to_thread(entered.wait, 5)
            await _answered_while_held(client, held, holder)
            other = await asyncio.wait_for(
                client.post("/api/tasks/root-b/pause", json={"request_id": "press-b"}), 3)
            continued = await asyncio.wait_for(
                client.post("/api/tasks/pred-1/continue", json={"action_nonce": NONCE}), 3)
            assert not held.done() and not holder.timed_out.is_set(), "root-a's Pause did not wait for its lock"
            holder.finish()
            return await asyncio.wait_for(held, 10), other, continued

    try:
        held, other, continued = asyncio.run(scenario())
    finally:
        holder.finish()
    assert other.status_code == 200 and other.json()["root_task_id"] == "root-b"
    assert continued.status_code == 200 and continued.json()["successor_task_id"].startswith("pred-1-c")
    assert held.status_code == 200, held.text
    assert read_fence(tmp_path, "root-a")["fence_id"] == held.json()["fence_id"]
    assert read_fence(tmp_path, "root-b")["fence_id"] == other.json()["fence_id"]
    assert set(queue.BUDGET_ROOT_FENCES) == {"root-a", "root-b"}


def _request(path, task_id, body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}

    return Request({"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b"",
                    "path_params": {"task_id": task_id}, "app": SimpleNamespace(state=SimpleNamespace())},
                   receive)


def _continue_request():
    from ouroboros.gateway.task_continue import api_task_continue

    return api_task_continue(_request("/api/tasks/pred-1/continue", "pred-1", {"action_nonce": NONCE}))


@pytest.mark.parametrize("mode", ["asyncio", "anyio"])
def test_a_cancelled_pause_settles_its_one_fence_and_the_retry_rejoins_it(tmp_path, monkeypatch, mode):
    from ouroboros.owner_pause import read_fence
    from supervisor import owner_pause_control

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    _live_root(tmp_path, workers, "root-a")
    entered = _mark_entry(monkeypatch, owner_pause_control, "request_owner_pause")
    release = threading.Event()
    holder = _Holder(lambda: queue._queue_lock, 6.0, release)

    def nothing_acted_yet():
        assert read_fence(tmp_path, "root-a") == {} and "root-a" not in queue.BUDGET_ROOT_FENCES

    try:
        asyncio.run(_cancel_while_held(
            lambda: api_task_pause(_request("/api/tasks/root-a/pause", "root-a", {"request_id": "press-a"})),
            entered, release, mode, nothing_acted_yet))
    finally:
        holder.finish()
    assert not holder.timed_out.is_set()
    fence = read_fence(tmp_path, "root-a")
    assert fence["state"] == "requested" and fence["request_id"] == "press-a"
    assert queue.BUDGET_ROOT_FENCES["root-a"]["fence_id"] == fence["fence_id"]
    snapshot = json.loads(queue.QUEUE_SNAPSHOT_PATH.read_text())
    assert [row["fence_id"] for row in snapshot["budget_root_fences"]] == [fence["fence_id"]]
    assert [row["fence_id"] for row in _events(tmp_path, "owner_pause_requested")] == [fence["fence_id"]]

    async def retry():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://owner.test") as client:
            return await client.post("/api/tasks/root-a/pause", json={"request_id": "press-a"})

    again = asyncio.run(retry())
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert again.json()["fence_id"] == fence["fence_id"]


@pytest.mark.parametrize("mode", ["asyncio", "anyio"])
def test_a_cancelled_continue_settles_its_one_admission_and_the_retry_rejoins_it(tmp_path, monkeypatch, mode):
    from ouroboros.task_results import load_task_result
    from supervisor import continuation_admission

    queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path)
    entered = _mark_entry(monkeypatch, continuation_admission, "admit_continuation")
    release = threading.Event()
    holder = _Holder(lambda: queue._queue_lock, 6.0, release)

    def nothing_admitted_yet():
        assert "continued_by" not in load_task_result(tmp_path, "pred-1") and workers.PENDING == []

    try:
        asyncio.run(_cancel_while_held(
            _continue_request, entered, release, mode, nothing_admitted_yet))
    finally:
        holder.finish()
    assert not holder.timed_out.is_set()
    claim = load_task_result(tmp_path, "pred-1")["continued_by"]
    successor = claim["successor_task_id"]
    assert claim["state"] == "admitted" and [task["id"] for task in workers.PENDING] == [successor]
    assert load_task_result(tmp_path, successor)["status"] == "scheduled"
    assert len(_events(tmp_path, "owner_continue_admitted")) == 1

    async def retry():
        transport = httpx.ASGITransport(app=_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://owner.test") as client:
            same = await client.post("/api/tasks/pred-1/continue", json={"action_nonce": NONCE})
            other = await client.post("/api/tasks/pred-1/continue", json={"action_nonce": "a-new-press-77"})
            return same, other

    same, other = asyncio.run(retry())
    assert same.status_code == 200 and same.json()["replay"] is True
    assert same.json()["successor_task_id"] == successor
    assert other.status_code == 409 and other.json()["successor_task_id"] == successor
    assert [task["id"] for task in workers.PENDING] == [successor]
