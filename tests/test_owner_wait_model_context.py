"""A cold owner wait keeps existing model choices and execution-clock meaning."""

import copy
from dataclasses import replace
import json
import queue as stdqueue
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import agent as agent_module, context, model_wait, owner_wait, task_pacing
from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.task_results import write_task_result
from ouroboros.tools.registry import ToolRegistry
from supervisor import queue, task_model_wait, workers
from tests._context_shared import _make_health_env
from tests.test_context_fit_v664 import _plan
from tests.test_llm_claudexor import MODEL
from tests.test_loop_transport_wait import _loop_kwargs


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Block provider/catalog calls without interfering with local event-loop IPC."""
    attempted = []

    def refuse(*args, **kwargs):
        attempted.append((args, kwargs))
        raise AssertionError("owner-wait model-state fixture attempted a provider/catalog call")

    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", refuse)
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat_async", refuse)
    monkeypatch.setattr("ouroboros.pricing._fetch_live_rows", refuse)
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "max")
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    monkeypatch.setenv("OUROBOROS_TASK_ABS_CEILING_SEC", "21600")
    yield
    assert not attempted


@pytest.fixture
def saved_wait(tmp_path, monkeypatch):
    now = time.time()
    clock = {"mono": 1000.0, "wall": now - 22000}
    monkeypatch.setattr(model_wait, "time", SimpleNamespace(
        monotonic=lambda: clock["mono"], time=lambda: clock["wall"], sleep=time.sleep))
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    ctx = registry._ctx
    ctx.task_id, ctx.task_attempt, ctx.task_started_at = "t-wait", 1, clock["wall"]
    ctx.active_model, ctx.active_effort = MODEL, "high"
    ctx.active_use_local, ctx.active_context_mode = False, "max"
    ctx._cost_ceiling = task_pacing.CostCeiling(state="disabled")
    ctx._owner_wait_requested = "question"
    ctx.context_fit_plan = replace(_plan(window=500000, known=True), model=MODEL,
                                   model_role="fallback:0", model_route={"credentialProfileId": "observed-only"})
    task = {"id": ctx.task_id, "type": "task", "chat_id": 1, "_attempt": 1, "text": "continue"}
    write_task_result(tmp_path, ctx.task_id, "running")
    events = stdqueue.Queue()
    with model_wait.task_model_wait_scope(task=task, drive_root=tmp_path, event_queue=events,
                                          worker_slot_held=True) as controller:
        ctx.model_wait_context = controller
        controller.tool_context = ctx
        controller.overrides = {
            "fallback:0": {"model": MODEL, "use_local": False, "model_account_override": "selected"},
            "vision": {"model": "vision-selected", "use_local": True, "model_account_override": ""},
        }
        controller.auto_continue = {"fallback:0": False, "vision": True}
        row = {"wait_id": "quota", "task_attempt": 1, "role": "main", "state": "waiting",
               "reason": "quota", "worker_slot_held": True}
        controller.quota_enter("quota", "slot-a")
        controller._publish(row)
        clock.update(mono=19000, wall=now - 4000)
        controller.quota_leave("quota", "slot-a")
        row["state"] = "resolved"
        controller._publish(row)
        clock.update(mono=23000, wall=now)
        original_state = controller.continuation_state()
        wait = owner_wait.checkpoint_owner_wait(ctx, ctx.context_fit_plan.messages_for("max"),
                                                {"tool_calls": []}, {"cost": 2.0}, 1, [], set())
        owner_wait.set_owner_wait(tmp_path, ctx.task_id, {**wait, "state": "waiting"})
        task["_owner_wait_resume"] = {**wait, "restart_transaction_id": "tx"}
        from ouroboros.delegate_recovery import _write_restart_transaction

        _write_restart_transaction(tmp_path, {"transaction_id": "tx", "status": "normal_exit_acknowledged",
                                             "task_ids": [ctx.task_id]})
        yield SimpleNamespace(root=tmp_path, task=task, ctx=ctx, wait=wait, state=original_state,
                              controller=controller, clock=clock, events=events)


def test_clock_state_preserves_quota_union_origin_and_calendar_deadline(saved_wait):
    case = saved_wait
    assert owner_wait.restore_owner_wait_allowed(case.root, case.task)
    assert case.wait["model_wait_quota_clock"]["elapsed_sec"] == 18000
    assert model_wait.quota_waited_seconds is task_model_wait.quota_waited_seconds
    # Time spent waiting for the owner (or restarting) is still execution time.
    case.clock["mono"] += 120
    case.clock["wall"] += 120
    with model_wait.task_model_wait_scope(task=case.task, drive_root=case.root, event_queue=None,
                                          worker_slot_held=True) as fresh:
        fresh.restore_continuation(case.state, started_at=case.ctx.task_started_at)
        assert fresh.paused_seconds() == 18000
        assert fresh.execution_window_remaining() == pytest.approx(17480)
        assert fresh.execution_window_remaining() == case.controller.execution_window_remaining()
        assert fresh.worker_slot_held is True and fresh.control_reason() is None
        assert fresh.overrides == case.controller.overrides
        assert fresh.auto_continue == case.controller.auto_continue
        with model_wait.calendar_scope("2000-01-01T00:00:00+00:00"):
            assert fresh.control_reason() == "deadline"


def test_actual_assignment_and_later_quota_publication_keep_one_clock(saved_wait, monkeypatch):
    from supervisor import evolution_lifecycle, state

    case = saved_wait
    pending, running = [case.task], {}
    for module in (workers, queue):
        monkeypatch.setattr(module, "DRIVE_ROOT", case.root)
        monkeypatch.setattr(module, "PENDING", pending)
        monkeypatch.setattr(module, "RUNNING", running)
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", case.root / "state/queue_snapshot.json")
    monkeypatch.setattr(queue, "ACCEPTANCE_FENCES", {})
    monkeypatch.setattr(queue, "BUDGET_ROOT_FENCES", {})
    monkeypatch.setattr(workers, "_WORKER_POOL_DISABLED_REASON", "")
    monkeypatch.setattr(workers, "load_state", lambda: {})
    monkeypatch.setattr(workers, "repo_writer_task_allowed", lambda task: True)
    monkeypatch.setattr(state, "budget_remaining", lambda *a, **kw: 100)
    monkeypatch.setattr(evolution_lifecycle, "evolution_block_reason", lambda: "")
    worker = SimpleNamespace(wid=0, busy_task_id=None, reaping=False, active_capacity=True,
                             in_q=stdqueue.Queue(), proc=None)
    monkeypatch.setattr(workers, "WORKERS", {0: worker})
    workers.assign_tasks()
    meta = running["t-wait"]
    assert task_model_wait.quota_waited_seconds(meta, time.time()) == 18000
    assert meta["started_at"] == case.ctx.task_started_at
    reaps = stdqueue.Queue()
    monkeypatch.setattr(queue, "_ensure_reaper_started", lambda: None)
    monkeypatch.setattr(queue, "_reap_queue", reaps)
    queue._enforce_task_timeouts_locked(workers, time.time(), 0, {})
    assert reaps.empty() and "t-wait" in running
    events = stdqueue.Queue()
    host = SimpleNamespace(RUNNING=running, DRIVE_ROOT=case.root,
                           append_jsonl=lambda *a: None, bridge=SimpleNamespace(push_log=lambda row: None))
    with model_wait.task_model_wait_scope(task=case.task, drive_root=case.root, event_queue=events,
                                          worker_slot_held=True) as fresh:
        fresh.restore_continuation(case.state, started_at=case.ctx.task_started_at)
        fresh.quota_enter("new-quota", "slot-new")
        row = {"wait_id": "new-quota", "task_attempt": 1, "role": "main", "state": "waiting"}
        fresh._publish(row)
        task_model_wait.handle_task_model_wait(events.get_nowait(), host)
        case.clock["mono"] += 10
        case.clock["wall"] += 10
        fresh.quota_leave("new-quota", "slot-new")
        row["state"] = "resolved"
        fresh._publish(row)
        task_model_wait.handle_task_model_wait(events.get_nowait(), host)
        assert meta["model_wait_quota_clock"]["revision"] == case.state["quota_clock"]["revision"] + 2
        assert task_model_wait.quota_waited_seconds(meta, case.clock["wall"]) == 18010


@pytest.mark.parametrize("pin", ["selected", ""])
def test_agent_restores_once_before_runtime_and_cold_dispatch_keeps_role_and_pin(saved_wait, monkeypatch, pin):
    from ouroboros import loop

    case = saved_wait
    case.controller.overrides["fallback:0"]["model_account_override"] = pin
    wait = owner_wait.checkpoint_owner_wait(case.ctx, case.ctx.context_fit_plan.messages_for("max"),
                                            {"tool_calls": []}, {"cost": 2.0}, 1, [], set())
    owner_wait.set_owner_wait(case.root, "t-wait", {**wait, "state": "waiting"})
    task = {**case.task, "_owner_wait_resume": {**wait, "restart_transaction_id": "tx"}}
    env = _make_health_env(case.root)
    env.branch_dev, env.budget_drive_root = "ouroboros", case.root
    registry = ToolRegistry(repo_dir=env.repo_dir, drive_root=case.root)
    agent = object.__new__(agent_module.OuroborosAgent)
    agent.__dict__.update(env=env, tools=registry, memory=None, _event_queue=None,
                          _current_chat_id=1, _current_task_type="task", _pending_events=[],
                          _task_started_ts=case.ctx.task_started_at, _owner_message_admission_lock=threading.Lock(),
                          owner_wait_callback=lambda *_: None)
    for name in ("_emit_live_log", "_emit_typing_start", "_emit_progress", "_capture_mutation_baseline"):
        monkeypatch.setattr(agent, name, lambda *a, **kw: None)
    monkeypatch.setattr(agent, "_run_delegate_preflight", lambda _logs, _task, dispatch: (dispatch, False))
    rebinds, dispatches = [], []

    def route(task, **kwargs):
        rebinds.append(copy.deepcopy(task))
        assert task["model_role"] == "fallback:0" and task["credential_profile_id"] == pin
        assert task["model_route"] == {}  # Startup/observed accounts are never new pins.
        return {"model": MODEL, "provider": "claudexor"}, SimpleNamespace(
            status="confirmed", stale=False, window_tokens=500000, route_fp="fresh-route",
            source_id="codex", credential_profile_id=pin or "fresh-auto-account",
            account_fingerprint="fresh-fingerprint", source="fixture")

    monkeypatch.setattr(context, "_context_fit_route", route)

    def runtime_messages(env, task, ctx, **kwargs):
        controller = ctx.model_wait_context
        assert controller.overrides == case.controller.overrides
        assert controller.auto_continue["fallback:0"] is False
        assert controller.paused_seconds() == 18000
        runtime = context.build_runtime_section(env, task, ctx=ctx)
        startup = replace(_plan(window=500000, known=True), model=MODEL, model_role="main",
                          model_route={"source": "codex", "model": "exact-model",
                                       "credentialProfileId": "wrong-startup-account", "accountFingerprint": "wrong"})
        projection = replace(startup.max_projection, system_content_json=json.dumps(runtime))
        ctx.context_fit_plan = replace(startup, max_projection=projection, low_projection=projection)
        return ctx.context_fit_plan.messages_for("max"), {}

    monkeypatch.setattr(agent_module, "build_llm_messages", runtime_messages)

    def response(*args, **kwargs):
        dispatches.append(kwargs)
        assert kwargs["model_role"] == "fallback:0" and kwargs["model_account_override"] == pin
        assert model_wait.current_model_wait().auto_continue["fallback:0"] is True
        assert model_wait.current_model_wait().overrides["vision"]["model"] == "changed-after-runtime"
        return {"role": "assistant", "content": "continued", "tool_calls": []}, 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", response)
    with model_wait.task_model_wait_scope(task=task, drive_root=case.root, event_queue=None,
                                          worker_slot_held=True) as fresh:
        ctx, messages, caps = agent._prepare_task_context(task)
        assert fresh.tool_context is ctx and ctx.model_wait_context is fresh
        fresh.auto_continue["fallback:0"] = True
        fresh.overrides["vision"]["model"] = "changed-after-runtime"
        result, _, _ = loop.run_llm_loop(**{**_loop_kwargs(case.root, registry, []),
            "messages": messages, "drive_logs": case.root / "logs", "budget_remaining_usd": caps["budget_remaining"]})
        assert result == "continued" and len(dispatches) == 1 and rebinds
        assert ctx.context_fit_plan.model_role == "fallback:0"
        assert fresh.paused_seconds() == 18000
    saved = json.loads(read_actor_source_bytes(case.root, "t-wait", wait["source_ref"]))
    assert saved["model_wait"]["auto_continue"]["fallback:0"] is False
    assert saved["model_wait"]["overrides"]["vision"]["model"] == "vision-selected"


def test_the_active_turn_token_never_enters_a_cold_owner_wait_checkpoint(saved_wait):
    """A live transport turn is process state; a cold restart opens a new one."""
    from ouroboros.llm_claudexor import ModelTurnState

    case = saved_wait
    token = "opaque-turn-in-flight"
    case.ctx.model_turn_state = ModelTurnState(
        {"route": {}, "format": "codex.turn.v1", "payload": {"turnState": token}})
    case.ctx._owner_wait_requested = "question"
    wait = owner_wait.checkpoint_owner_wait(
        case.ctx, case.ctx.context_fit_plan.messages_for("max"), {"tool_calls": []}, {"cost": 2.0}, 1, [], set())
    stored = read_actor_source_bytes(case.root, case.ctx.task_id, wait["source_ref"]).decode("utf-8")
    assert token not in json.dumps(wait) and token not in stored
    assert "model_turn_state" not in stored
