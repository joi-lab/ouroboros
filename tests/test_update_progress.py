"""Progress belongs to the running executor, independently of its HTTP await."""

import asyncio
import threading

import pytest
from starlette.responses import JSONResponse

from ouroboros.gateway import control, update_progress as progress


@pytest.fixture(autouse=True)
def isolated_progress(monkeypatch):
    monkeypatch.setattr(progress, "_current", {})
    notices = []
    monkeypatch.setattr(progress, "_notify", lambda: notices.append(progress.snapshot()))
    return notices


def test_progress_is_passive_and_stage_notifications_are_not_boot_ready(isolated_progress):
    assert progress.snapshot() == {}
    progress.begin()
    progress.advance("stopping_workers")
    progress.advance("stopping_workers")
    view = progress.snapshot()
    assert view["active"] and view["stage"] == "stopping_workers"
    assert view["operation_id"] and view["generation"] and view["stage_started_at"]
    assert "owner_thread" not in view
    view["stage"] = "changed by reader"
    assert progress.snapshot()["stage"] == "stopping_workers"
    assert len(isolated_progress) == 2


@pytest.mark.parametrize("body,code,result", [
    ({"error": "writers remain", "restart_required": True}, 409, "failed"),
    ({"status": "restart_required", "error": "callback unavailable"}, 200, "restart_required"),
    ({"status": "assisted_started", "task_id": "resolver"}, 200, "assisted_started"),
    ({"status": "ok", "restarting": True}, 200, "restart_requested"),
])
def test_executor_result_survives_panel_reopen(body, code, result):
    @progress.observe_result
    def execute():
        progress.begin()
        progress.advance("checking")
        return JSONResponse(body, status_code=code)

    response = execute()
    view = progress.snapshot()
    assert response.status_code == code
    assert view["result"] == result and not view["active"]
    assert view["error"] == body.get("error", "")
    assert view["restart_required"] == (result == "restart_required" or body.get("restart_required", False))


@pytest.mark.serial
def test_cancelled_http_await_does_not_finish_the_running_executor(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    @progress.observe_result
    def execute(*_args, **_kwargs):
        progress.begin()
        progress.advance("stopping_workers")
        entered.set()
        try:
            assert release.wait(5)
            return JSONResponse({"status": "assisted_started"})
        finally:
            finished.set()

    monkeypatch.setattr(control, "_apply_smart_update_fenced", execute)

    async def exercise():
        task = asyncio.create_task(control._apply_smart_update(None, expected_base_sha="a", expected_target_sha="b"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert progress.snapshot()["active"] is True
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 5)

    asyncio.run(exercise())
    assert progress.snapshot()["result"] == "assisted_started"
    assert progress.snapshot()["active"] is False


@pytest.mark.serial
def test_rejected_competing_executor_cannot_clear_or_advance_owner():
    progress.begin()
    progress.advance("stopping_workers")
    owner = progress.snapshot()

    @progress.observe_result
    def refused():
        progress.advance("applying")
        return JSONResponse({"error": "update lock is held"}, status_code=409)

    thread = threading.Thread(target=refused)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert progress.snapshot() == owner


def test_telemetry_failure_does_not_change_update_outcome(monkeypatch):
    def fail():
        raise RuntimeError("socket unavailable")

    monkeypatch.setattr(progress, "_notify", fail)

    @progress.observe_result
    def execute():
        progress.begin()
        progress.advance("applying")
        return JSONResponse({"status": "ok", "restarting": True})

    assert execute().status_code == 200
    assert progress.snapshot()["result"] == "restart_requested"


def test_generation_restart_starts_with_no_observation(monkeypatch):
    progress.begin()
    progress.advance("stopping_workers")
    monkeypatch.setattr(progress, "_current", {})  # Fresh module in the next server process.
    assert progress.snapshot() == {}


@pytest.mark.parametrize("restart_required", [False, True])
def test_explicit_recheck_dismisses_only_a_retryable_failure(monkeypatch, restart_required):
    @progress.observe_result
    def failed():
        progress.begin()
        return JSONResponse({"error": "stopped", "restart_required": restart_required}, status_code=409)

    failed()
    monkeypatch.setattr(control, "_managed_update_payload", lambda **_: {"check_ok": True})
    asyncio.run(control.api_update_check(None))
    assert bool(progress.snapshot()) is restart_required


def test_explicit_check_does_not_clear_a_running_executor(monkeypatch):
    progress.begin()
    owner = progress.snapshot()
    monkeypatch.setattr(control, "_managed_update_payload", lambda **_: {"check_ok": True})
    asyncio.run(control.api_update_check(None))
    assert progress.snapshot() == owner
