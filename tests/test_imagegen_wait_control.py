"""Image tool bounds through the real loop, registry, ledger and engine wire."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from ouroboros import loop_tool_execution as execution
from ouroboros import model_wait
from ouroboros.tools.registry import ToolRegistry
from tests.test_imagegen_subscription import ImageWire, engine as engine, gateway_factory as gateway_factory
from tests.test_imagegen_tool import PNG, _summary, configured as configured, ctx as ctx


def _call(ctx, registry):
    call = {"id": "image-call", "function": {"name": "generate_image", "arguments": json.dumps({
        "prompt": "fixture", "model": "claudexor::codex=requested-image"})}}
    return execution._execute_with_timeout(registry, call, ctx.drive_root / "logs", 30, ctx.task_id)


def _setup(ctx, monkeypatch):
    from ouroboros import config
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    write_task_result(ctx.drive_root, ctx.task_id, STATUS_RUNNING, chat_id=0)
    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 7200)
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    task = {"id": ctx.task_id, "deadline_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()}
    return registry, task


class _AfterCreate(ImageWire):
    """Advance a control only after the real accepted create, before its first poll."""

    def __init__(self, after_create):
        super().__init__()
        self.pending = True
        self.after_create = after_create

    def respond(self, request):
        response = super().respond(request)
        if request.method == "POST" and request.url.path == "/v2/model-operations":
            self.after_create()
        return response


def test_tool_deadline_returns_original_recovery_id_without_ending_task(engine, ctx, monkeypatch):
    from ouroboros import loop_round_limits, usage_accounting
    from ouroboros.observability import read_call_payload

    registry, task = _setup(ctx, monkeypatch)
    clock = [100.0]
    # Only the logical call clock advances. Wall time, task lifetime and the
    # Future's 30-second bounded wait remain real; no sleep or race wins this test.
    monkeypatch.setattr(model_wait, "monotonic_now", lambda *args: clock[0])
    monkeypatch.setattr(execution, "monotonic_now", lambda *args: clock[0])
    wire = engine(_AfterCreate(lambda: clock.__setitem__(0, 131.0)))
    terminal = None
    with model_wait.task_model_wait_scope(task=task, drive_root=ctx.drive_root,
                                         event_queue=None, worker_slot_held=False) as owner:
        try:
            result = _call(ctx, registry)
        except model_wait.ModelWaitInterrupted as error:
            # Exercise the actual consumer of a wrongly classified interruption,
            # exposing the terminal status rather than just asserting no raise.
            limits = loop_round_limits._RoundLimitContext(
                messages=[], llm=None, active_model="fixture", active_effort="", max_retries=0,
                drive_logs=ctx.drive_root / "logs", task_id=ctx.task_id, round_idx=1, event_queue=None,
                accumulated_usage={}, task_type="task", active_use_local=False, max_rounds=5,
                drive_root=ctx.drive_root)
            terminal = loop_round_limits._handle_model_wait_control(limits, error)
        assert owner.execution_window_remaining() > 7100
        assert owner.control_reason() is None, "the tool's private bound must not end the task"
        assert terminal is None, f"tool wait failed a live task: {terminal}"

    assert result["result_meta"]["tool_result_code"] == "IMAGE_OUTCOME_UNKNOWN"
    facts = _summary(result["result"])
    assert facts["automatic_retry"] is False and facts["operation_id"] == "op-one"
    request_id = facts["request_id"]
    messages, trace = [], {"reasoning_notes": [], "tool_calls": []}
    assert execution.process_tool_results([result], messages, trace, lambda _text: None, tools=registry) == 1
    assert messages[-1]["role"] == "tool" and request_id in messages[-1]["content"]
    assert "recover_image" in messages[-1]["content"]
    records = usage_accounting.read_usage_records(ctx.drive_root)
    assert len(records) == 1 and records[0]["attempt_id"] == request_id and records[0]["state"] == "unresolved"
    manifest, _payload, _ref = read_call_payload(ctx.drive_root, task_id=ctx.task_id,
                                               call_id=f"{request_id}_model_request")
    assert manifest["operation_id"] == "op-one"
    assert wire.upload_count == wire.generation_count == 1
    assert not any(call.url.path.endswith(("/control", "/ack")) for call in wire.calls)
    assert ctx.pending_events == []

    recovered = registry.execute_result("recover_image", {"request_id": request_id})
    assert recovered.code == "OK"
    recovered_facts = _summary(recovered.text)
    assert recovered_facts["request_id"] == request_id and recovered_facts["acknowledgement"] == "acknowledged"
    assert recovered_facts["delivery"] == "not_requested"
    from pathlib import Path
    assert all(Path(image["absolute_path"]).read_bytes() == PNG for image in recovered_facts["images"])
    records = usage_accounting.read_usage_records(ctx.drive_root)
    assert len(records) == 1 and records[0]["attempt_id"] == request_id and records[0]["state"] == "settled"
    assert wire.upload_count == wire.generation_count == 1
    assert len([call for call in wire.calls if call.url.path.endswith("/ack")]) == 1


@pytest.mark.parametrize("reason", ["cancelled", "deadline", "absolute_ceiling"])
def test_real_task_control_still_propagates_and_requests_cancel(engine, ctx, monkeypatch, reason):
    from ouroboros.cancel_intents import request_cancel
    from ouroboros import usage_accounting

    registry, task = _setup(ctx, monkeypatch)
    with model_wait.task_model_wait_scope(task=task, drive_root=ctx.drive_root,
                                         event_queue=None, worker_slot_held=False) as owner:
        def interrupt():
            if reason == "cancelled":
                request_cancel(ctx.drive_root, ctx.task_id, reason="owner Stop", source="owner", requested_by="owner")
            elif reason == "deadline":
                task["deadline_at"] = "2000-01-01T00:00:00+00:00"
            else:
                owner.started_monotonic -= 7201
        wire = engine(_AfterCreate(interrupt))
        with pytest.raises(model_wait.ModelWaitInterrupted) as raised:
            _call(ctx, registry)
        assert raised.value.control_reason == reason
    assert wire.upload_count == wire.generation_count == 1
    assert len([call for call in wire.calls if call.url.path.endswith("/control")]) == 1
    assert not any(call.url.path.endswith("/ack") for call in wire.calls)
    records = usage_accounting.read_usage_records(ctx.drive_root)
    assert len(records) == 1 and records[0]["state"] == "unresolved"


@pytest.mark.parametrize("before_dispatch", [True, False])
def test_owner_pause_refuses_new_images_but_allows_sent_result_custody(engine, ctx, monkeypatch, before_dispatch):
    from ouroboros import owner_pause

    registry, task = _setup(ctx, monkeypatch)
    def pause():
        owner_pause.install_fence(ctx.drive_root, ctx.task_id, request_id="owner-pause")
    wire = engine(_AfterCreate(lambda: None if before_dispatch else pause()))
    if before_dispatch:
        pause()
    with model_wait.task_model_wait_scope(task=task, drive_root=ctx.drive_root,
                                         event_queue=None, worker_slot_held=False) as owner:
        result = _call(ctx, registry)
        assert owner.pre_dispatch_pause() == "owner_pause"
        assert owner.control_reason() is None
    if before_dispatch:
        assert result["result_meta"]["tool_result_code"] == "OWNER_PAUSE_NOT_STARTED"
        assert wire.generation_count == wire.upload_count == 0
    else:
        assert result["result_meta"]["tool_result_code"] == "OK"
        assert _summary(result["result"])["acknowledgement"] == "acknowledged"
        assert wire.generation_count == wire.upload_count == 1
    assert not any(call.url.path.endswith("/control") for call in wire.calls)
