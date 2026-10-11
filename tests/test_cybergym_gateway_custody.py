"""Gateway admission, cancellation, transport and deadline custody tests.

These drive the normal and cancelled gateway paths through the concrete executor,
using the existing shared configuration fixture and a deterministic wait clock.
"""

from __future__ import annotations

import json
import time as _time

import pytest

from devtools.benchmarks.cybergym.cybergym_executor import (
    CyberGymExecutor,
    ExecutorFailure,
)
from devtools.benchmarks.cybergym.cybergym_wire import GatewayTransportError
from tests.test_cybergym_executor import _config, dataclasses_replace

def test_unknown_gateway_attempt_blocks_campaign_cleanup(tmp_path):
    config = _config(tmp_path)
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        raise AssertionError("cleanup must not run while gateway custody is unknown")

    executor = CyberGymExecutor(dataclasses_replace(config, command_runner=command))
    executor.started = True
    executor.server_id = "server-123"
    executor.network_id = "network-123"
    executor._task_containers = {"workspace-agent-aaaaaaaaaaaaaaaaaaaaaaaa": "workspace-123"}
    executor._gateway_attempts = {
        "cybergym-attempt": {
            "gateway_task_id": "cybergym-attempt",
            "status": "admission_unknown",
            "checkpoint": str(config.run_root / "checkpoint.json"),
        }
    }

    report = executor.close()
    assert report["ok"] is False
    assert report["status"] == "custody_pending"
    assert executor.custody_blocked is True
    assert executor.server_id == "server-123"
    assert executor.network_id == "network-123"
    assert calls == []
    assert (config.run_root / "custody_pending.json").is_file()


def test_gateway_admission_transport_error_registers_durable_custody(tmp_path):
    config = _config(tmp_path, provider_probe=False)
    seen = {}

    def failing_http(*args, **kwargs):
        seen.update(kwargs)
        raise ExecutorFailure("HTTP POST transport failed")

    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=failing_http))
    checkpoint = config.run_root / "checkpoint.json"
    body = {"task_id": "cybergym-opaque-attempt", "description": "test"}
    with pytest.raises(ExecutorFailure, match="transport failed"):
        executor._gateway_wait(body, checkpoint)
    assert "cybergym-opaque-attempt" in executor._gateway_attempts
    assert executor._gateway_attempts["cybergym-opaque-attempt"]["status"] == "admission_unknown"
    assert seen["headers"]["Idempotency-Key"].startswith("cybergym-")
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["custody_required"] is True
    assert saved["status"] == "admission_unknown"


def test_gateway_definitive_admission_rejection_releases_phantom_custody(tmp_path):
    config = _config(tmp_path, provider_probe=False)
    executor = CyberGymExecutor(
        dataclasses_replace(
            config,
            http_runner=lambda *args, **kwargs: {
                "status_code": 400,
                "body": {"detail": "invalid task"},
            },
        )
    )
    checkpoint = config.run_root / "checkpoint.json"
    body = {"task_id": "cybergym-rejected-attempt", "description": "test"}
    with pytest.raises(ExecutorFailure, match="HTTP 400"):
        executor._gateway_wait(body, checkpoint)
    assert executor._gateway_attempts == {}
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "admission_rejected"
    assert saved["custody_required"] is False


def test_gateway_malformed_admission_keeps_unknown_custody(tmp_path):
    config = _config(tmp_path, provider_probe=False)
    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=lambda *args, **kwargs: {})
    )
    checkpoint = config.run_root / "checkpoint.json"
    body = {"task_id": "cybergym-malformed-attempt", "description": "test"}
    with pytest.raises(ExecutorFailure, match="no task id"):
        executor._gateway_wait(body, checkpoint)
    assert "cybergym-malformed-attempt" in executor._gateway_attempts
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "admission_unknown_response"
    assert saved["custody_required"] is True


def test_gateway_waits_for_final_cost_after_completed_status(tmp_path):
    config = _config(tmp_path, provider_probe=False, task_timeout_sec=10)
    task_id = "cybergym-cost-pending"
    calls = []
    status_rows = iter(
        (
            {
                "task_id": task_id,
                "status": "completed",
                "result": {"cost_final": False},
            },
            {
                "task_id": task_id,
                "status": "completed",
                "result": {"cost_final": True},
            },
        )
    )

    def http(method, url, **kwargs):
        calls.append(method)
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        return next(status_rows)

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    result = executor._gateway_wait(
        {"task_id": task_id, "description": "test"},
        config.run_root / "checkpoint.json",
    )

    assert result["result"]["cost_final"] is True
    assert calls == ["POST", "GET", "GET"]


def test_gateway_cost_finality_conflict_keeps_polling(tmp_path):
    config = _config(tmp_path, provider_probe=False, task_timeout_sec=10)
    task_id = "cybergym-cost-conflict"
    calls = []
    status_rows = iter(
        (
            {
                "task_id": task_id,
                "status": "completed",
                "cost_final": True,
                "cost_breakdown": {"cost_final": False},
            },
            {
                "task_id": task_id,
                "status": "completed",
                "cost_final": True,
                "cost_breakdown": {"cost_final": True},
            },
        )
    )

    def http(method, _url, **_kwargs):
        calls.append(method)
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        return next(status_rows)

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    result = executor._gateway_wait(  # noqa: SLF001 - accounting contract
        {"task_id": task_id, "description": "test"},
        config.run_root / "checkpoint.json",
    )

    assert result["cost_breakdown"]["cost_final"] is True
    assert calls == ["POST", "GET", "GET"]




def test_cancel_503_recovers_terminal_gateway_payload(tmp_path):
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-503"
    terminal = {
        "task_id": task_id,
        "status": "failed",
        "cost_usd": 0.060914,
        "accounted_upper_bound_usd": 0.060914,
        "unresolved_upper_bound_usd": 0.020062,
        "cost_final": False,
    }
    calls = []
    responses = iter(
        (
            {"status_code": 503, "body": {"detail": "teardown still live"}},
            {"status_code": 200, "body": terminal},
        )
    )

    def http(method, _url, **_kwargs):
        calls.append(method)
        return next(responses)

    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=http))
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    checkpoint = config.run_root / "checkpoint.json"
    result = executor._cancel_gateway_task(task_id, checkpoint)  # noqa: SLF001

    assert result == terminal
    assert calls == ["POST", "GET"]
    assert task_id not in executor._gateway_attempts
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["cancel_status_code"] == 503
    assert saved["result"]["accounted_upper_bound_usd"] == pytest.approx(0.060914)


def test_cancel_auth_failure_does_not_fallback_to_get(tmp_path):
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-auth"
    calls = []

    def http(method, _url, **_kwargs):
        calls.append(method)
        return {"status_code": 401, "body": {"detail": "unauthorized"}}

    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=http))
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    with pytest.raises(ExecutorFailure, match="cancellation request failed"):
        executor._cancel_gateway_task(task_id, config.run_root / "checkpoint.json")  # noqa: SLF001
    assert calls == ["POST"]
    assert task_id in executor._gateway_attempts


def test_cancel_503_with_get_failure_keeps_custody_block(tmp_path):
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-no-terminal"
    calls = []

    def http(method, _url, **_kwargs):
        calls.append(method)
        if method == "POST":
            return {"status_code": 503, "body": {"detail": "teardown still live"}}
        raise ExecutorFailure("status transport failed")

    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=http))
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    with pytest.raises(ExecutorFailure, match="status transport failed"):
        executor._cancel_gateway_task(task_id, config.run_root / "checkpoint.json")  # noqa: SLF001
    assert calls == ["POST", "GET"]
    assert task_id in executor._gateway_attempts


def test_gateway_poll_rides_out_transient_transport_errors(tmp_path):
    # A ~95 s isolate event-loop stall used to kill a healthy paid task via a
    # single 60 s poll timeout.  The poll now retries transport failures
    # within a bounded budget; the terminal answer after the stall wins.
    config = _config(tmp_path, provider_probe=False, task_timeout_sec=60)
    task_id = "cybergym-transient-stall"
    calls = []

    def http(method, _url, **_kwargs):
        calls.append(method)
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        if calls.count("GET") <= 3:
            raise GatewayTransportError("HTTP GET transport failed")
        return {"task_id": task_id, "status": "completed", "result": {"cost_final": True}}

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    result = executor._gateway_wait(  # noqa: SLF001 - transport recovery contract
        {"task_id": task_id, "description": "test"},
        config.run_root / "checkpoint.json",
    )

    assert result["status"] == "completed"
    assert calls == ["POST", "GET", "GET", "GET", "GET"]


def test_gateway_poll_transport_budget_exhaustion_still_fails(tmp_path, monkeypatch):
    # The retry budget is bounded: a gateway that never answers within it
    # still produces the circuit-breaker transport row.
    config = _config(tmp_path, provider_probe=False, task_timeout_sec=3600)
    task_id = "cybergym-dead-gateway"
    monkeypatch.setattr(
        "devtools.benchmarks.cybergym.cybergym_custody.GATEWAY_TRANSPORT_RETRY_BUDGET_SEC",
        0.0,
    )

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        raise GatewayTransportError("HTTP GET transport failed")

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    with pytest.raises(GatewayTransportError):
        executor._gateway_wait(  # noqa: SLF001 - transport recovery contract
            {"task_id": task_id, "description": "test"},
            config.run_root / "checkpoint.json",
        )


def test_cancel_rides_out_transient_transport_errors(tmp_path):
    # The cancel intent usually lands server-side and only the response is
    # starved by an isolate stall; a duplicate POST is idempotent.  The
    # bounded retry turns a stall into a delayed cancel instead of a
    # written-off paid attempt.
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-stall"
    terminal = {
        "task_id": task_id,
        "status": "failed",
        "cost_usd": 0.060914,
        "accounted_upper_bound_usd": 0.060914,
        "unresolved_upper_bound_usd": 0.020062,
        "cost_final": False,
    }
    calls = []

    def http(method, _url, **_kwargs):
        calls.append(method)
        if method == "POST" and calls.count("POST") <= 2:
            raise GatewayTransportError("HTTP POST transport failed")
        if method == "POST":
            return {"status_code": 200, "body": {"task_id": task_id, "status": "cancel_requested"}}
        return {"status_code": 200, "body": terminal}

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    checkpoint = config.run_root / "checkpoint.json"
    result = executor._cancel_gateway_task(task_id, checkpoint)  # noqa: SLF001

    assert result == terminal
    assert calls == ["POST", "POST", "POST", "GET"]
    assert task_id not in executor._gateway_attempts


def test_cancel_custody_get_rides_out_transient_transport_errors(tmp_path):
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-poll-stall"
    terminal = {
        "task_id": task_id,
        "status": "failed",
        "cost_usd": 0.2,
        "cost_final": True,
    }
    calls = []

    def http(method, _url, **_kwargs):
        calls.append(method)
        if method == "POST":
            return {
                "status_code": 202,
                "body": {"task_id": task_id, "status": "cancel_requested"},
            }
        if calls.count("GET") <= 3:
            raise GatewayTransportError("HTTP GET transport failed")
        return {"status_code": 200, "body": terminal}

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    result = executor._cancel_gateway_task(  # noqa: SLF001
        task_id, config.run_root / "checkpoint.json"
    )

    assert result == terminal
    assert calls == ["POST", "GET", "GET", "GET", "GET"]
    assert task_id not in executor._gateway_attempts


def test_cancel_custody_get_transport_budget_exhaustion_keeps_custody(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-poll-dead"
    monkeypatch.setattr(
        "devtools.benchmarks.cybergym.cybergym_custody.GATEWAY_TRANSPORT_RETRY_BUDGET_SEC",
        0.0,
    )

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {
                "status_code": 202,
                "body": {"task_id": task_id, "status": "cancel_requested"},
            }
        raise GatewayTransportError("HTTP GET transport failed")

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    checkpoint = config.run_root / "checkpoint.json"
    with pytest.raises(GatewayTransportError):
        executor._cancel_gateway_task(task_id, checkpoint)  # noqa: SLF001

    assert task_id in executor._gateway_attempts
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "cancel_poll_error"
    assert saved["cancel_error"] == "GatewayTransportError"


def test_cancel_transport_budget_exhaustion_keeps_custody(tmp_path, monkeypatch):
    # A cancel that never gets through within the budget keeps the original
    # fail-closed behaviour: typed checkpoint evidence and retained custody.
    config = _config(tmp_path, poll_interval_sec=0)
    task_id = "cybergym-cancel-dead"
    monkeypatch.setattr(
        "devtools.benchmarks.cybergym.cybergym_custody.GATEWAY_TRANSPORT_RETRY_BUDGET_SEC",
        0.0,
    )

    def http(method, _url, **_kwargs):
        raise GatewayTransportError("HTTP POST transport failed")

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }
    checkpoint = config.run_root / "checkpoint.json"
    with pytest.raises(GatewayTransportError):
        executor._cancel_gateway_task(task_id, checkpoint)  # noqa: SLF001

    assert task_id in executor._gateway_attempts
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "cancel_request_failed"
    assert saved["cancel_error"] == "GatewayTransportError"


def test_gateway_poll_transport_error_at_deadline_goes_to_cancel(tmp_path, monkeypatch):
    # A transport failure riding into the task's own deadline must exit to the
    # cancel path, not raise a transport row: the deadline, not the network,
    # decides the task's fate.
    config = _config(tmp_path, provider_probe=False, task_timeout_sec=1)
    task_id = "cybergym-deadline-stall"
    calls = []

    def http(method, _url, **_kwargs):
        calls.append(method)
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        raise GatewayTransportError("HTTP GET transport failed")

    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=lambda _seconds: None)
    )
    sentinel = {"status": "cancelled", "via": "cancel_path"}
    monkeypatch.setattr(
        executor, "_cancel_gateway_task", lambda *_a, **_k: dict(sentinel)
    )
    result = executor._gateway_wait(  # noqa: SLF001 - transport recovery contract
        {"task_id": task_id, "description": "test"},
        config.run_root / "checkpoint.json",
    )

    assert result == sentinel
    assert "GET" in calls


def test_cancel_custody_window_covers_deadline_wave_settle(tmp_path, monkeypatch):
    # The post-cancel custody poll must outlast a deadline-wave settle: the
    # old ~34 s bound wrote off paid tasks whose cancel had already landed.
    config = _config(tmp_path, poll_interval_sec=3.0)
    task_id = "cybergym-cancel-window"
    captured = {}

    def http(method, _url, **_kwargs):
        return {
            "status_code": 200,
            "body": {"task_id": task_id, "status": "cancel_requested"},
        }

    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=http))
    executor._gateway_attempts[task_id] = {  # noqa: SLF001 - custody assertion
        "gateway_task_id": task_id,
        "status": "submitted",
    }

    def fake_custody(*_args, **kwargs):
        captured["custody_seconds"] = kwargs.get("custody_seconds")
        return {}

    monkeypatch.setattr(executor, "_poll_gateway_custody", fake_custody)
    executor._cancel_gateway_task(task_id, config.run_root / "checkpoint.json")  # noqa: SLF001

    assert captured["custody_seconds"] >= 300.0




class _FakeClock:
    """Deterministic ``time`` stand-in for the gateway wait loop."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_700_000_000.0 + self.now

    def sleep(self, seconds: float) -> None:
        self.now += float(seconds)

    strftime = staticmethod(_time.strftime)
    gmtime = staticmethod(_time.gmtime)


def _deadline_executor(tmp_path, monkeypatch, http, *, task_timeout_sec):
    from devtools.benchmarks.cybergym import cybergym_custody

    clock = _FakeClock()
    monkeypatch.setattr(cybergym_custody, "time", clock)
    config = _config(
        tmp_path,
        provider_probe=False,
        task_timeout_sec=task_timeout_sec,
        poll_interval_sec=1.0,
    )
    executor = CyberGymExecutor(
        dataclasses_replace(config, http_runner=http, sleep=clock.sleep)
    )
    sentinel = {"status": "cancelled", "via": "cancel_path"}
    monkeypatch.setattr(
        executor, "_cancel_gateway_task", lambda *_a, **_k: dict(sentinel)
    )
    return executor, clock, config.run_root / "checkpoint.json", sentinel


def test_gateway_deadline_is_anchored_at_observed_run_start(tmp_path, monkeypatch):
    # full1507: tasks queued ~1 h behind a finalization backlog were cancelled
    # after ~1 h of runtime because the deadline was anchored at submit. The
    # clock must start when the gateway first reports a non-queued status.
    task_id = "cybergym-observed-start"
    # Each poll costs 6 s + 1 s poll interval: ~14 s queued (inside the 20 s
    # queue cap), then the run starts at ~21 s, past a submit-anchored 20 s
    # deadline, and completes at ~35 s -- inside the observed-start deadline.
    frames = iter(
        (
            {"task_id": task_id, "status": "scheduled"},
            {"task_id": task_id, "status": "scheduled"},
            {"task_id": task_id, "status": "running"},
            {"task_id": task_id, "status": "running"},
            {
                "task_id": task_id,
                "status": "completed",
                "result": {"cost_final": True},
            },
        )
    )
    clock_ref = {}

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        clock_ref["clock"].now += 6.0
        return next(frames)

    executor, clock, checkpoint, sentinel = _deadline_executor(
        tmp_path, monkeypatch, http, task_timeout_sec=20
    )
    submitted_at = clock.now
    clock_ref["clock"] = clock
    result = executor._gateway_wait(  # noqa: SLF001 - deadline contract
        {"task_id": task_id, "description": "test"}, checkpoint
    )

    assert result["status"] == "completed", "queued time was charged against the deadline"
    assert clock.now - submitted_at > 20, "scenario must outlive a submit-anchored deadline"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["deadline_basis"] == "observed_start"
    assert saved["observed_start_at"].endswith("Z")


def test_gateway_queue_wait_cap_still_bounds_a_never_started_task(tmp_path, monkeypatch):
    task_id = "cybergym-queue-cap"
    polls = []

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        polls.append(_kwargs.get("timeout"))
        return {"task_id": task_id, "status": "scheduled"}

    executor, clock, checkpoint, sentinel = _deadline_executor(
        tmp_path, monkeypatch, http, task_timeout_sec=10
    )
    result = executor._gateway_wait(  # noqa: SLF001 - deadline contract
        {"task_id": task_id, "description": "test"}, checkpoint
    )

    assert result == sentinel
    assert 9 <= len(polls) <= 12
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["deadline_basis"] == "queue_wait_cap"
    assert "observed_start_at" not in saved


def test_gateway_run_deadline_carries_grace_behind_server_ceiling(tmp_path, monkeypatch):
    from devtools.benchmarks.cybergym.cybergym_custody import TASK_DEADLINE_GRACE_SEC

    task_id = "cybergym-run-deadline"

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        return {"task_id": task_id, "status": "running"}

    executor, clock, checkpoint, sentinel = _deadline_executor(
        tmp_path, monkeypatch, http, task_timeout_sec=10
    )
    started = clock.now
    result = executor._gateway_wait(  # noqa: SLF001 - deadline contract
        {"task_id": task_id, "description": "test"}, checkpoint
    )

    assert result == sentinel
    elapsed = clock.now - started
    assert 10 + TASK_DEADLINE_GRACE_SEC <= elapsed < 10 + TASK_DEADLINE_GRACE_SEC + 2.0




# r9 (2026-09-04): nine finished, paid tasks were cancelled by the launcher at
# deadline+grace while the server was still finalizing their workspace
# artifacts (projected `running` / `artifact_status=finalizing`); the cancel path
# re-ran the same finalization and the 300 s custody window expired 1-15 min
# before the completed results landed. A finalizing frame now buys the task a
# bounded finalization grace on both the wait and the custody paths.


def test_gateway_wait_grants_finalization_grace_instead_of_cancelling(tmp_path, monkeypatch):
    from devtools.benchmarks.cybergym.cybergym_wire import FINALIZATION_GRACE_SEC

    task_id = "cybergym-finalizing"
    finalizing = {
        "task_id": task_id, "status": "running", "artifact_status": "finalizing",
        "child_status": "completed", "cost_final": True,
    }
    state = {"polls": 0}

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        state["polls"] += 1
        # Runs to its deadline, then finalizes for a while, then delivers.
        if state["polls"] < 12:
            return {"task_id": task_id, "status": "running"}
        if state["polls"] < 20:
            return dict(finalizing)
        return {"task_id": task_id, "status": "completed", "result": {"cost_final": True}}

    executor, clock, checkpoint, sentinel = _deadline_executor(
        tmp_path, monkeypatch, http, task_timeout_sec=5
    )
    result = executor._gateway_wait(  # noqa: SLF001 - deadline contract
        {"task_id": task_id, "description": "test"}, checkpoint
    )
    assert result["status"] == "completed", "a finalizing task must not be cancelled"
    assert result != sentinel
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert FINALIZATION_GRACE_SEC > 60


def test_gateway_wait_finalization_grace_is_granted_once(tmp_path, monkeypatch):
    from devtools.benchmarks.cybergym import cybergym_custody

    task_id = "cybergym-finalizing-forever"

    def http(method, _url, **_kwargs):
        if method == "POST":
            return {"task_id": task_id, "status": "scheduled"}
        return {"task_id": task_id, "status": "running", "artifact_status": "finalizing", "child_status": "completed"}

    monkeypatch.setattr(cybergym_custody, "FINALIZATION_GRACE_SEC", 20.0)
    executor, clock, checkpoint, sentinel = _deadline_executor(
        tmp_path, monkeypatch, http, task_timeout_sec=5
    )
    started = clock.now
    result = executor._gateway_wait(  # noqa: SLF001 - deadline contract
        {"task_id": task_id, "description": "test"}, checkpoint
    )
    assert result == sentinel, "finalization that outlives the grace is still cancelled"
    elapsed = clock.now - started
    assert 5 + 300 + 20 <= elapsed < 5 + 300 + 20 + 3
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["deadline_basis"] == "finalization_grace"


def test_custody_poll_outlasts_its_window_while_the_task_is_finalizing(tmp_path, monkeypatch):
    from devtools.benchmarks.cybergym import cybergym_custody

    task_id = "cybergym-custody-finalizing"
    clock = _FakeClock()
    monkeypatch.setattr(cybergym_custody, "time", clock)
    monkeypatch.setattr(cybergym_custody, "FINALIZATION_GRACE_SEC", 120.0)
    state = {"polls": 0}

    def http(method, _url, **_kwargs):
        state["polls"] += 1
        clock.now += 10.0
        if state["polls"] < 8:  # 80 s of finalizing: past a 30 s custody window
            return {"task_id": task_id, "status": "running", "artifact_status": "finalizing", "child_status": "completed"}
        return {"task_id": task_id, "status": "completed", "result": {"cost_final": True}}

    config = _config(tmp_path, provider_probe=False, poll_interval_sec=1.0)
    executor = CyberGymExecutor(dataclasses_replace(config, http_runner=http, sleep=clock.sleep))
    monkeypatch.setattr(executor, "_terminalize_gateway_attempt", lambda *_a, **_k: None)
    checkpoint = config.run_root / "custody.json"
    result = executor._poll_gateway_custody(  # noqa: SLF001 - custody contract
        task_id, checkpoint, cancel_response=None, cancel_status_code=503, custody_seconds=30.0
    )
    assert result["status"] == "completed"
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
