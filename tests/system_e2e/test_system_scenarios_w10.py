"""Wave 10 — S40: an idle-timeout retry keeps the owner's Pause and Resume (#1729 / #1730).

A keyless real server runs from a byte-faithful checkout of the tree under test (S33's
``s33_candidate``: working-tree edits included, its process tree held until proven gone; a
HEAD clone only for a clean tree) with ONE real worker process. The owner acts only through the HTTP doors the UI posts
to (Pause, Resume, Stop); the queue ``put``, the idle rail, the reaper and its kill, worker
respawn and assignment are the shipped ones.

Doubled, and only these: the model is the loopback stub; the hung worker is a real worker
frozen with SIGSTOP while its round sleeps out a provider 503's retry backoff, so no model or
tool call is in flight and the idle rail sees what a wedged worker shows it, a live and silent
process; the idle window and grace are shortened through their own settings (121 s, 10 s).

``after_owner_resume``: the owner Pauses, Resumes (the attempt consumes its grant), then the
worker hangs. The retry must dispatch without the spent Resume and continue the saved work
(#1729). ``ordinary``: the hang alone. Both then Pause the RUNNING retry while its model round
is held: the Pause names the logical root, a model file write served after the acceptance never
lands while a write before it did, one Resume reopens the tree, and Stop ends it (#1730).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import uuid

import pytest

from devtools.benchmarks.common.server_runner import _api_status
from tests.system_e2e.harness import (
    LANE_MOCK,
    ArtifactOracle,
    ModelGate,
    ScriptedStubModel,
    StubHttpError,
    keyless_settings,
    require_lane,
    start_server,
    submit_running,
    wait_durable_result,
    wait_until,
)
from tests.system_e2e.test_presence_resilience import s33_candidate  # noqa: F401 - pytest fixture

KEEPALIVE = {"tool": "list_files", "arguments": {"path": "."}}
CONTROL = {"tool": "write_file", "arguments": {"root": "task_drive", "path": "s40-control.txt",
                                               "content": "written before the Pause"}}
SENTINEL = {"tool": "write_file", "arguments": {"root": "task_drive", "path": "s40-sentinel.txt",
                                                "content": "a task effect after the accepted Pause"}}
# The idle window is max(idle setting, per-call ceiling + 120 s): both at their floors, and one worker.
SETTINGS = {"OUROBOROS_MAX_WORKERS": 1, "OUROBOROS_TASK_IDLE_TIMEOUT_SEC": 60,
            "OUROBOROS_PER_CALL_TIMEOUT_CEILING_SEC": 1, "OUROBOROS_FINALIZATION_GRACE_SEC": 10}
REAP_WINDOW = 121 + 10 + 120  # idle, grace, then the reap job, its kill, a respawn and readiness


class _RetryStub(ScriptedStubModel):
    """Keepalive rounds; the scenario queues one-shot steps or answers agent rounds with a 503."""

    def __init__(self) -> None:
        super().__init__(latency_sec=0.2)
        self.steps, self.served = [], []
        self.failing, self.failed_at = False, 0.0

    def _next_step(self, _body):
        if self.steps:
            step = self.steps.pop(0)
            self.served.append((time.time(), step["arguments"]["path"]))
            return step
        return dict(KEEPALIVE)

    def _completion(self, body):
        if self.failing and body.get("tools"):
            self.failed_at = self.failed_at or time.time()
            raise StubHttpError(503, "stub upstream unavailable")
        return super()._completion(body)


def _post(server, path: str, body: dict | None = None) -> dict:
    return _api_status(server.base_url, "POST", path, body or {}, timeout=60)


def _pause_row(data, task_id: str) -> dict:
    from ouroboros.budget_pause import budget_pause_row

    try:
        return budget_pause_row(data, task_id) or {}
    except Exception as exc:  # an unreadable row is a fact for the forensics, not a crash of the reader
        return {"unreadable": repr(exc)}


def _rows(oracle) -> list:
    snapshot = oracle.queue_snapshot()
    return [dict(row.get("task") or {}, id=row.get("id"), queue=state)
            for state in ("pending", "running") for row in snapshot.get(state) or [] if isinstance(row, dict)]


def _retry_row(oracle, origin: str) -> dict:
    """The new-ID attempt of ``origin``: its own row whose logical root is the origin."""
    return next((row for row in _rows(oracle) if row["id"] != origin and row.get("root_task_id") == origin), {})


def _alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _descends(pid: int, ancestor: int) -> bool:
    while pid > 1:
        if pid == ancestor:
            return True
        parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
        if not parent.strip():
            return False
        pid = int(parent)
    return False


def _worker_pid(oracle, server) -> int:
    """The one live worker of this server, by its own boot row (pid), proven a descendant."""
    live = sorted({int(row["pid"]) for row in oracle.events("worker_boot") if row.get("pid")
                   and _alive(int(row["pid"])) and _descends(int(row["pid"]), server.proc.pid)})
    assert len(live) == 1, (live, oracle.events("worker_boot"))
    return live[0]


def _found(data, name: str) -> list:
    return sorted(str(path) for path in data.rglob(name))


def _facts(oracle, data, origin: str, stub, **extra) -> dict:
    from ouroboros.owner_pause import read_fence

    retry = _retry_row(oracle, origin)
    ids = [origin] + ([retry["id"]] if retry else [])
    keys = ("id", "queue", "_attempt", "root_task_id", "_budget_pause", "_budget_pause_resume",
            "_budget_pause_hold", "timeout_retry_from")
    return {
        "rows": [{key: row.get(key) for key in keys if row.get(key) is not None} for row in _rows(oracle)],
        "pause_rows": {task_id: {key: _pause_row(data, task_id).get(key) for key in (
            "state", "reason", "pause_id", "task_attempt", "unreadable")} for task_id in ids},
        "fences": {task_id: read_fence(data, task_id) for task_id in ids},
        "results": {task_id: {key: oracle.task_result(task_id).get(key) for key in ("status", "reason_code")}
                    for task_id in ids},
        "stub_calls": len(stub.calls), "stub_served": stub.served, **extra,
    }


@pytest.mark.integration
@pytest.mark.serial
# Evaluated before any fixture: the hang is SIGSTOP/SIGKILL on a real worker read through ``ps``.
@pytest.mark.skipif(os.name != "posix", reason="freezes a real worker with SIGSTOP/SIGKILL and ps: POSIX only")
@pytest.mark.parametrize("history", ["after_owner_resume", "ordinary"])
def test_s40_an_idle_retry_keeps_the_owner_pause_and_resume(s33_candidate, tmp_path_factory, history):  # noqa: F811
    require_lane(LANE_MOCK)
    from ouroboros.budget_pause import STATE_PAUSED, STATE_RESUMED
    from ouroboros.owner_pause import read_fence

    root = tmp_path_factory.mktemp(f"s40_{history}")
    frozen, gate = 0, None
    with _RetryStub() as stub:
        server = start_server(s33_candidate, root, keyless_settings(stub, **SETTINGS))
        data, oracle = server.data_root, ArtifactOracle(server.data_root)
        facts = {"history": history, "tree": getattr(s33_candidate, "identity", str(s33_candidate))}
        try:
            origin = submit_running(server, "Keep listing the repository files. [S40]")
            assert wait_until(lambda: len(stub.calls) >= 3, 60), stub.kinds()
            if history == "after_owner_resume":
                paused = _post(server, f"/api/tasks/{origin}/pause", {"request_id": f"s40-{uuid.uuid4().hex}"})
                assert paused["status"] in (200, 202), paused
                # Settled: the exact row, the durable fence and the queue all say paused.
                assert wait_until(lambda: _pause_row(data, origin).get("state") == STATE_PAUSED
                                  and read_fence(data, origin).get("state") == "paused"
                                  and origin not in oracle.running_ids(), 120), _facts(oracle, data, origin, stub)
                stub.failing = True  # the resumed attempt's round meets the provider's 503
                granted = _post(server, f"/api/tasks/{origin}/resume")
                assert granted["status"] == 200 and granted["body"].get("ok"), granted
            else:
                stub.failing = True
            assert wait_until(lambda: stub.failed_at, 120), _facts(oracle, data, origin, stub)
            time.sleep(1.5)  # inside the 4 s retry backoff, the failed call's events delivered
            if history == "after_owner_resume":
                assert _pause_row(data, origin).get("state") == STATE_RESUMED, "the attempt consumed its grant"
            frozen = _worker_pid(oracle, server)
            os.kill(frozen, signal.SIGSTOP)
            stub.failing = False
            at_freeze = len(stub.calls)
            facts.update(origin=origin, frozen_pid=frozen, frozen_at=time.time())

            # The shipped idle rail and reaper: a real kill, then ONE new-ID attempt of the same root.
            retry = wait_until(lambda: _retry_row(oracle, origin), REAP_WINDOW, interval=2)
            assert retry, _facts(oracle, data, origin, stub)
            retry_id = retry["id"]
            facts.update(retry=retry_id, reaped_after_sec=round(time.time() - facts["frozen_at"]), retry_row={
                key: retry.get(key) for key in ("queue", "_attempt", "_budget_pause_resume", "_budget_pause_hold")})
            assert not retry.get("_budget_pause_resume"), "the spent Resume is not the retry's authority"
            assert wait_until(lambda: not _alive(frozen), 60), "the reaper's kill never reached the frozen worker"
            assert oracle.task_result(origin).get("status") != "failed", _facts(oracle, data, origin, stub)
            # #1729: it dispatches — no spent Resume holds it — and continues the saved work.
            running = wait_until(lambda: retry_id in oracle.running_ids(), 180, interval=1)
            assert running, _facts(oracle, data, origin, stub)
            assert wait_until(lambda: len(stub.calls) > at_freeze, 60), _facts(oracle, data, origin, stub)
            with stub._lock:
                body = next(body for _kind, body in stub.calls[at_freeze:])
            earlier = {f"call_{seq}" for seq in range(1, at_freeze + 1)}
            history_ids = {call.get("id") for message in body.get("messages") or [] if isinstance(message, dict)
                           for call in message.get("tool_calls") or []} | {
                message.get("tool_call_id") for message in body.get("messages") or [] if isinstance(message, dict)}
            assert history_ids & earlier, "the retry restarted instead of continuing the saved work"

            # #1730: a write before the Pause lands (the door is real), then the round is held.
            stub.steps.append(dict(CONTROL))
            facts["control"] = wait_until(lambda: _found(data, "s40-control.txt"), 60)
            assert facts["control"], _facts(oracle, data, origin, stub)
            gate = stub.gate = ModelGate(lambda body: bool(body.get("tools")), timeout=120)
            assert gate.arrived.wait(60), _facts(oracle, data, origin, stub)
            accepted = _post(server, f"/api/tasks/{retry_id}/pause", {"request_id": f"s40-{uuid.uuid4().hex}"})
            assert accepted["status"] in (200, 202), accepted
            facts["pause_of_retry"] = accepted["body"]
            stub.steps.append(dict(SENTINEL))  # the model asks for a task effect after the acceptance
            gate.release.set()
            # Resume observes settled authority: requested -> paused during its census must refuse.
            parked = wait_until(lambda: (_pause_row(data, retry_id).get("state") == STATE_PAUSED
                                        and read_fence(data, origin).get("state") == "paused")
                                or _found(data, "s40-sentinel.txt"), 90)
            facts.update(after_pause=_facts(oracle, data, origin, stub))
            assert any(path == "s40-sentinel.txt" for _ts, path in stub.served), "the write was never asked for"
            assert not _found(data, "s40-sentinel.txt"), facts
            assert accepted["body"].get("root_task_id") == origin, accepted
            assert parked and retry_id not in oracle.running_ids(), facts
            fence = read_fence(data, origin)
            assert fence.get("state") == "paused" and not read_fence(data, retry_id), facts

            # One Resume of the visible retry reopens the logical tree; Stop ends it.
            resumed = _post(server, f"/api/tasks/{retry_id}/resume")
            assert resumed["status"] == 200 and resumed["body"].get("root_task_id") == origin, resumed
            assert wait_until(lambda: read_fence(data, origin).get("state") == "released", 60), \
                _facts(oracle, data, origin, stub)
            assert wait_until(lambda: retry_id in oracle.running_ids(), 120), _facts(oracle, data, origin, stub)
            stopped = server.cancel_task(retry_id)
            assert stopped["status"] in (200, 202), stopped
            facts["retry_terminal"] = wait_durable_result(oracle, retry_id, timeout=180).get("status")
            assert facts["retry_terminal"] == "cancelled", facts
            assert not _found(data, "s40-sentinel.txt"), "no task effect of the paused attempt appeared later"
        finally:
            if gate is not None:
                gate.release.set()
            if frozen and _alive(frozen):
                os.kill(frozen, signal.SIGKILL)
            print("S40_FACTS", json.dumps(facts, default=str))
            server.stop()
