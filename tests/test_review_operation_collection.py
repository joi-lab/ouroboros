"""Pure collection of already-paid review operations and their late evidence.

Collection branches BEFORE the ordinary runner: exact producer CAS, a live
local worker, or an attach-only read of a proven delegated run, parsed locally.
Spies prove no model call, send, re-post, daemon ensure, cancel or retirement.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import delegate_custody as custody
from ouroboros import review_operation
from ouroboros.loop_acceptance_review import acceptance_run_pending
from ouroboros.review_dispatch import collect_task_acceptance_run, reconcile_pending_acceptance_runs
from ouroboros.review_execution import ReviewRouteKind
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from ouroboros.task_results import load_task_result, write_task_result

TASK = "collect-root"
PASS = json.dumps({"verdict": "PASS", "findings": [], "summary": "Independent review passes"})


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@pytest.fixture
def forbid_effects(monkeypatch):
    """Every path that could buy, send, start, cancel, acknowledge or retire fails loudly."""
    def forbidden(name):
        def fail(*_args, **_kwargs):
            pytest.fail(f"pure collection reached {name}")
        return fail

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", forbidden("the ordinary runner"))
    monkeypatch.setattr("ouroboros.review_substrate.ReviewCoordinator.run", forbidden("the coordinator"))
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat", forbidden("a model call"))
    monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway", forbidden("daemon ensure/start"))
    monkeypatch.setattr("ouroboros.review_verdict_extraction._extract_verdict_via_light_model",
                        forbidden("Light extraction"))


def _ctx(root):
    return SimpleNamespace(task_id=TASK, task_attempt=1, drive_root=root, budget_drive_root=root,
                           task_metadata={}, pending_events=[], event_queue=queue.Queue())


def test_terminal_handoff_collects_mailbox_only_settlement(tmp_path, monkeypatch):
    from ouroboros import acceptance_settlement as settlement, model_wait
    from ouroboros.review_projection import publish_acceptance_checkpoint
    from tests.test_review_operation_lifetime import until

    ctx, calls, release = _ctx(tmp_path), [], threading.Event()
    write_task_result(tmp_path, TASK, 'running', chat_id=3)
    request = ReviewRequest(surface='task_acceptance', task_id=TASK, subject='answer A', goal='goal',
                            retry_key='terminal-handoff', drain_deadline=time.monotonic())
    slot = ReviewSlot(slot_id='one', model='model/a', timeout_sec=30)

    class Held:
        def chat(self, **kwargs):
            calls.append(kwargs)
            assert release.wait(10)
            return {'content': PASS}, {'prompt_tokens': 1, 'completion_tokens': 1}

    operation = None
    try:
        with model_wait.task_model_wait_scope(task={'id': TASK, 'chat_id': 3, '_attempt': 1},
                                             drive_root=tmp_path, event_queue=ctx.event_queue, worker_slot_held=False):
            first = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx, llm=Held())
        operation = next(op for op in review_operation._LIVE.values() if op.task_id == TASK)
        # Main's last collection has returned pending; terminal publication is held here.
        run = {**dataclasses.asdict(first), 'authority': 'host_root', 'binding_hash': 'b' * 64}
        publish_acceptance_checkpoint(ctx, {'review_runs': [run]}, task_id=TASK)
        release.set()
        until(lambda: operation.closed)
        until(lambda: load_task_result(tmp_path, TASK)['review_operations'][operation.owner_id]['state']
              not in {'retained', 'dispatched'})
        assert load_task_result(tmp_path, TASK)['status'] == 'running'
        # A mailbox write is not consumption. Lose all process-local settlement hints.
        settlement._LATE_UNPUBLISHED.discard((TASK, request.retry_key))
        ctx._execution_trace = None
        write_task_result(tmp_path, TASK, 'completed', chat_id=3, result='answer B')
        monkeypatch.setattr('supervisor.workers.get_event_q', lambda: ctx.event_queue)
        monkeypatch.setattr('ouroboros.review_substrate.ReviewCoordinator.run',
                            lambda *a, **kw: pytest.fail('maintenance started another review'))
        report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
        assert len(report['settled']) == 1, report
        stored = load_task_result(tmp_path, TASK)
        panel = stored['review_projection']['panels'][0]
        assert panel['aggregate_signal'] == 'PASS' and panel['late_settlement']['reviewer_outputs']
        assert stored['result'] == 'answer B'
        assert stored['review_operations'][operation.owner_id]['state'] == 'collected'
        assert len(calls) == 1
        assert review_operation.recover_orphaned_acceptance_operations(tmp_path)['settled'] == []
    finally:
        release.set()
        if operation:
            until(lambda: operation.closed)


@pytest.mark.parametrize('failure', ['refused', 'raised'])
def test_settlement_publication_holds_operation_until_retry_duty_is_durable(tmp_path, monkeypatch, failure):
    from ouroboros import acceptance_settlement as settlement, model_wait, review_custody
    from ouroboros.review_projection import publish_acceptance_checkpoint
    from supervisor import terminal_delivery
    from tests.test_review_operation_lifetime import until

    gates = {key: threading.Event() for key in ('a', 'b', 'a_publish', 'b_publish')}
    a_settled, b_publishing, a_released = threading.Event(), threading.Event(), threading.Event()
    ctx, calls = _ctx(tmp_path), []
    write_task_result(tmp_path, TASK, 'running', chat_id=3)
    request = ReviewRequest(surface='task_acceptance', task_id=TASK, subject='answer A', goal='goal',
                            retry_key='publication-race', policy={'min_successful_slots': 2},
                            drain_deadline=time.monotonic())
    slots = [ReviewSlot(slot_id=key, model=key, timeout_sec=30) for key in ('a', 'b')]

    class Held:
        def chat(self, model, **kwargs):
            calls.append(model)
            assert gates[model].wait(10)
            return {'content': PASS}, {'prompt_tokens': 1, 'completion_tokens': 1}

    emit, announce, release = review_custody._emit_operation, settlement.announce_acceptance_settlement, review_custody.release_review_operation

    def hold_a(*args, **kwargs):
        if kwargs['slot'].slot_id == 'a' and kwargs['phase'] == 'finished':
            a_settled.set()
            assert gates['a_publish'].wait(10)
        return emit(*args, **kwargs)

    def hold_b(*args, **kwargs):
        b_publishing.set()
        assert gates['b_publish'].wait(10)
        return announce(*args, **kwargs)

    def released(entry):
        release(entry)
        if entry.actor.slot_id == 'a':
            a_released.set()

    monkeypatch.setattr(review_custody, '_emit_operation', hold_a)
    monkeypatch.setattr(settlement, 'announce_acceptance_settlement', hold_b)
    monkeypatch.setattr(review_custody, 'release_review_operation', released)
    operation = None
    try:
        with model_wait.task_model_wait_scope(task={'id': TASK, 'chat_id': 3, '_attempt': 1},
                                             drive_root=tmp_path, event_queue=ctx.event_queue, worker_slot_held=False):
            first = run_review_request(request, slots=slots, drive_root=tmp_path, usage_ctx=ctx, llm=Held())
        operation = next(op for op in review_operation._LIVE.values() if op.task_id == TASK)
        run = {**dataclasses.asdict(first), 'authority': 'host_root', 'binding_hash': 'b' * 64}
        publish_acceptance_checkpoint(ctx, {'review_runs': [run]}, task_id=TASK)
        write_task_result(tmp_path, TASK, 'completed', chat_id=3, result='answer B')
        gates['a'].set()
        assert a_settled.wait(5)
        gates['b'].set()
        assert b_publishing.wait(5)
        gates['a_publish'].set()
        assert a_released.wait(5)
        assert not operation.closed, 'final publication still owns the paid operation'
        # Rejoin observes the same paid actors without buying another call.
        again = run_review_request(request, slots=slots, drive_root=tmp_path, usage_ctx=ctx, llm=Held())
        assert [a['operation_id'] for a in again.actors] == [a['operation_id'] for a in first.actors]
        assert sorted(calls) == ['a', 'b']
        with monkeypatch.context() as fault:
            if failure == 'refused':
                fault.setattr(terminal_delivery, 'register_pending_delivery', lambda *a, **kw: False)
            else:
                fault.setattr(terminal_delivery, 'enqueue_terminal_delivery_outcome',
                              lambda *a, **kw: (_ for _ in ()).throw(OSError('outbox unavailable')))
            gates['b_publish'].set()
            until(lambda: operation.closed)
            until(lambda: load_task_result(tmp_path, TASK)['review_operations'][operation.owner_id]['state'] == 'unpublished')
        before = load_task_result(tmp_path, TASK)['review_projection']['panels'][0]['late_settlement']
        ctx.event_queue = queue.Queue()
        settlement._LATE_UNPUBLISHED.discard((TASK, request.retry_key))
        monkeypatch.setattr('supervisor.workers.get_event_q', lambda: ctx.event_queue)
        report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
        assert report['settled'][0]['status'] == 'announced', report
        stored = load_task_result(tmp_path, TASK)
        assert stored['review_operations'][operation.owner_id]['state'] == 'collected'
        assert stored['review_projection']['panels'][0]['late_settlement'] == before
        assert [row['delivery_id'] for row in list(ctx.event_queue.queue)] == ['acceptance-late:publication-race']
        assert sorted(calls) == ['a', 'b']
    finally:
        for gate in gates.values():
            gate.set()
        if operation:
            until(lambda: operation.closed)


def _session_run(slot_id="s", operation_id="op-session", controller=None):
    request = ReviewRequest(surface="task_acceptance", task_id=TASK, goal="goal", subject="reviewed answer A",
                            evidence={"requirement": "exact"}, retry_key="wave-session")
    slot = ReviewSlot(slot_id=slot_id, model="codex", route=ReviewRouteKind.AGENT_SESSION,
                      session_target="codex")
    return json.loads(json.dumps({
        "authority": "host_root", "request": dataclasses.asdict(request), "slot_roster": [dataclasses.asdict(slot)],
        "panel_id": "panel_session", "aggregate_signal": "DEGRADED",
        "actors": [{"slot_id": slot_id, "model": "codex", "status": "error", "operation_id": operation_id,
                    "operation_state": "pending_dispatch", "late_result_pending": True,
                    "usage": {"review_controller": controller or {"pid": _dead_pid(), "session": "gone"}}}]}))


class ObservingGateway:
    """The owned daemon as the collector may see it: reads only."""

    def __init__(self, detail, calls):
        self.detail, self.calls = detail, calls

    def get_run(self, run_id, *, timeout_sec=None):
        self.calls.append(("get_run", run_id))
        return self.detail

    def get_run_artifact(self, run_id, path):
        self.calls.append(("artifact", path))
        return b""

    def close(self):
        self.calls.append(("close",))

    def __getattr__(self, name):
        pytest.fail(f"pure collection called gateway.{name}")


def _started(root, *, operation_id="op-session", slot_id="s", run_id=""):
    custody.emit(root, custody.START_REQUESTED, {
        "invocation_id": f"inv-{operation_id}", "task_id": TASK, "operation_id": operation_id, "slot_id": slot_id,
        "surface": "task_acceptance", "root_task_id": TASK, "request": {"prompt": "review", "harnesses": ["codex"]}})
    if run_id:
        custody.emit(root, custody.STARTED, {"run_id": run_id, "invocation_id": f"inv-{operation_id}",
                                             "task_id": TASK, "surface": "task_acceptance"})


def _observe(monkeypatch, detail):
    calls = []
    monkeypatch.setattr("ouroboros.claudexor_daemon.read_owned_gateway",
                        lambda: (calls.append(("handshake",)), ObservingGateway(detail, calls))[1])
    return calls


def test_pending_invocation_without_run_is_unknown_and_never_reposted(tmp_path, forbid_effects, monkeypatch):
    calls = _observe(monkeypatch, {})
    _started(tmp_path)
    result = collect_task_acceptance_run(_session_run(), drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    actor = result.actors[0]
    assert actor["operation_state"] == "custody_lost" and "never re-posted" in actor["error"]
    assert result.collection["facts"] == {"s": "unavailable"}
    assert calls == [], "an unbound invocation is never even observed, let alone re-POSTed"
    assert not acceptance_run_pending(result), "a gap is not a live wait"


def test_absent_start_request_is_unknown_and_only_a_definite_refusal_proves_no_send(tmp_path, forbid_effects, monkeypatch):
    calls = _observe(monkeypatch, {})
    result = collect_task_acceptance_run(_session_run(), drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    actor = result.actors[0]
    assert actor["operation_state"] == "custody_lost" and "cannot prove nothing was sent" in actor["error"]
    assert result.collection["facts"] == {"s": "unavailable"} and calls == [], "absence is never proof"
    # The daemon's own definite refusal of this exact invocation is a positive receipt.
    _started(tmp_path)
    custody.emit(tmp_path, custody.START_FAILED, {"invocation_id": "inv-op-session", "task_id": TASK,
                                                  "definite": True, "error": "route refused"})
    refused = collect_task_acceptance_run(_session_run(), drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    assert refused.actors[0]["operation_state"] == "not_dispatched"
    assert refused.collection["facts"] == {"s": "never_dispatched"} and calls == []


def test_an_unverifiable_controller_is_never_presumed_dead(tmp_path, forbid_effects, monkeypatch):
    """A live pid whose birth was never recorded may still be the controller: only
    exact proofs settle its rows; nothing becomes an unknown-outcome gap early."""
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        unverifiable = {"pid": worker.pid, "session": "other"}
        calls = _observe(monkeypatch, {"summary": {"state": "running"}})
        pending = collect_task_acceptance_run(_session_run(controller=unverifiable), drive_root=tmp_path,
                                              usage_ctx=_ctx(tmp_path))
        assert pending.collection["facts"] == {"s": "deferred"} and calls == [], "no start on record: still owed"
        _started(tmp_path, run_id="run-proven")
        pending = collect_task_acceptance_run(_session_run(controller=unverifiable), drive_root=tmp_path,
                                              usage_ctx=_ctx(tmp_path))
        assert pending.collection["facts"] == {"s": "deferred"} and ("get_run", "run-proven") in calls
        api = _session_run(controller=unverifiable)
        api["slot_roster"][0]["route"] = ReviewRouteKind.API_CHAT.value
        api["actors"][0]["operation_id"] = "op-api"
        assert collect_task_acceptance_run(api, drive_root=tmp_path, usage_ctx=_ctx(tmp_path)).collection[
            "facts"] == {"s": "deferred"}
    finally:
        worker.kill()
        worker.wait()


@pytest.mark.parametrize("answer, method", [(PASS, "strict"), ("Looks fine overall, no blocking issues.", "parse_unavailable")])
def test_proven_existing_run_is_read_attach_only_and_parsed_locally(tmp_path, forbid_effects, monkeypatch, answer, method):
    detail = {"summary": {"state": "succeeded", "outputConformance": ""},
              "primaryOutput": {"text": answer, "truncated": False}}
    calls = _observe(monkeypatch, detail)
    _started(tmp_path, run_id="run-live")
    result = collect_task_acceptance_run(_session_run(), drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    actor = result.actors[0]
    assert calls == [("handshake",), ("get_run", "run-live"), ("close",)], \
        "one disclosed protocol handshake, then reads only"
    assert actor["usage"]["collection"] == "attach_only_observation"
    assert actor["usage"]["verdict_method"] == method and actor["raw_text"] == answer, "the answer is kept whole"
    if method == "strict":
        assert actor["parsed"]["verdict"] == "PASS" and result.collection["facts"] == {"s": "collected"}
    else:
        assert actor["parse_status"] == "parse_unavailable" and not actor.get("semantic_verdict")
    assert not acceptance_run_pending(result)


@pytest.mark.parametrize('telemetry', ['observed', 'missing', 'wrong_run', 'duplicate_attempt'])
def test_cold_collection_preserves_only_final_attempt_identity(tmp_path, forbid_effects, monkeypatch, telemetry):
    from ouroboros.review_projection import publish_acceptance_checkpoint

    final = tmp_path / 'run' / 'final'
    final.mkdir(parents=True)
    last = {'attempt_id': 'a02', 'harness_id': 'actual-harness',
            'observed_model': 'actual-model', 'profile_id': 'actual-profile'}
    record = {'run_id': 'wrong' if telemetry == 'wrong_run' else 'run-live', 'final_attempt_id': 'a02',
              'attempts': [{'attempt_id': 'a01', 'harness_id': 'prior-harness', 'observed_model': 'prior-model'}, last]}
    if telemetry == 'duplicate_attempt':
        record['attempts'].append(last)
    if telemetry != 'missing':
        (final / 'telemetry.yaml').write_text(json.dumps(record), encoding='utf-8')
    detail = {'summary': {'state': 'succeeded', 'runDir': str(final.parent), 'model': 'request-echo'},
              'primaryOutput': {'text': PASS, 'truncated': False}}
    calls = _observe(monkeypatch, detail)
    _started(tmp_path, run_id='run-live')
    run = _session_run()
    run['slot_roster'][0].update(model='codex=requested-model', session_target='codex=requested-model')
    result = collect_task_acceptance_run(run, drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    actor = result.actors[0]
    usage = actor['usage']
    assert actor['parsed']['verdict'] == 'PASS'
    expected = telemetry == 'observed'
    assert usage['resolved_model'] == ('actual-model' if expected else '')
    assert usage['applied_profile'] == ('actual-profile' if expected else '')
    assert usage['delegated_route'] == ('actual-harness' if expected else '')
    assert usage['observed_attempt'] == ({'attempt_id': 'a02', 'harness_id': 'actual-harness',
                                          'model': 'actual-model', 'profile_id': 'actual-profile'} if expected else {})
    reasons = {delta['reason'] for delta in usage['capability_delta']}
    assert reasons == ({'session_ran_off_pinned_route', 'session_route_resolves_its_own_model'} if expected
                       else {'session_route_observation_unavailable'})
    write_task_result(tmp_path, TASK, 'completed')
    published = {**dataclasses.asdict(result), 'authority': 'host_root', 'binding_hash': 'c' * 64}
    publish_acceptance_checkpoint(_ctx(tmp_path), {'review_runs': [published]}, task_id=TASK)
    projected = load_task_result(tmp_path, TASK)['review_projection']['panels'][0]['actors'][0]
    assert projected['model'] == ('actual-model' if expected else '')
    assert projected['executions'] == ([{'kind': 'harness', 'harness_id': 'actual-harness', 'model': 'actual-model'}]
                                       if expected else [{'kind': 'harness'}])
    assert calls == [('handshake',), ('get_run', 'run-live'), ('close',)]


def test_a_run_still_in_flight_stays_deferred_and_is_never_cancelled(tmp_path, forbid_effects, monkeypatch):
    calls = _observe(monkeypatch, {"summary": {"state": "running"}})
    _started(tmp_path, run_id="run-busy")
    run = _session_run()
    result = collect_task_acceptance_run(run, drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    assert result.collection["facts"] == {"s": "deferred"} and acceptance_run_pending(result)
    assert ("get_run", "run-busy") in calls and not [c for c in calls if c[0] not in {"handshake", "get_run", "close"}]
    assert result.actors[0]["usage"]["review_controller"] == run["actors"][0]["usage"]["review_controller"]


def test_live_process_controller_keeps_the_row_deferred_without_reading_anything(tmp_path, forbid_effects, monkeypatch):
    from ouroboros.platform_layer import process_start_time

    calls = _observe(monkeypatch, {})
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _started(tmp_path, run_id="run-owned-elsewhere")
        exact = {"pid": worker.pid, "birth": process_start_time(worker.pid), "session": "other"}
        result = collect_task_acceptance_run(_session_run(controller=exact),
                                             drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
        reused = collect_task_acceptance_run(_session_run(controller={**exact, "birth": "another process"}),
                                             drive_root=tmp_path, usage_ctx=_ctx(tmp_path))
    finally:
        worker.kill()
        worker.wait()
    assert result.collection["facts"] == {"s": "deferred"}
    # Only the reused-pid row (its recorded controller proven gone) was read, attach-only;
    # the live exact controller's row was not touched at all.
    assert calls == [("handshake",), ("get_run", "run-owned-elsewhere"), ("close",)]
    assert reused.collection["facts"] == {"s": "deferred"}, "the run is still running: deferred, never cancelled"


class _HeldModel:
    def __init__(self):
        self.release, self.calls = threading.Event(), 0

    def chat(self, **_kwargs):
        self.calls += 1
        assert self.release.wait(10)
        return {"content": PASS}, {"prompt_tokens": 1, "completion_tokens": 1}


def _released_panel(root, ctx, model, *, retry_key="wave-cas", subject="reviewed answer A"):
    request = ReviewRequest(surface="task_acceptance", task_id=TASK, goal="goal", subject=subject,
                            evidence={"requirement": "exact"}, retry_key=retry_key, drain_deadline=time.monotonic())
    first = run_review_request(request, slots=[ReviewSlot(slot_id="a", model="model/a", timeout_sec=20)],
                               drive_root=root, usage_ctx=ctx, llm=model)
    return {**json.loads(json.dumps(dataclasses.asdict(first))), "authority": "host_root",
            "panel_id": f"panel_{retry_key}", "binding_hash": f"binding-{retry_key}",
            "candidate_hash": hashlib.sha256(subject.encode()).hexdigest()}


def _settle(model, run):
    from ouroboros.review_custody import _ACTIVE, _ACTIVE_LOCK

    model.release.set()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with _ACTIVE_LOCK:
            if not any(entry.operation_id == run["actors"][0]["operation_id"] for entry in _ACTIVE.values()):
                return
        time.sleep(0.01)
    raise AssertionError("worker did not settle")


def test_exact_cas_collection_crosses_processes_and_a_lost_api_worker_is_unknown(tmp_path, monkeypatch):
    model = _HeldModel()
    write_task_result(tmp_path, TASK, "running", chat_id=3)
    run = _released_panel(tmp_path, _ctx(tmp_path), model)
    assert run["actors"][0]["usage"]["review_controller"]["pid"] > 0, "a still-owed row names its controller"
    _settle(model, run)
    forbidden = pytest.fail
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", lambda *a, **k: forbidden("runner"))
    # A cold context of another process: the exact producer CAS is the whole answer.
    cold = SimpleNamespace(task_id=TASK, task_attempt=1, drive_root=tmp_path, budget_drive_root=tmp_path, task_metadata={})
    result = collect_task_acceptance_run(run, drive_root=tmp_path, usage_ctx=cold)
    assert result.actors[0]["parsed"]["verdict"] == "PASS" and result.collection["facts"] == {"a": "collected"}
    # An API worker killed with its process left no producer: unknown, never re-sent.
    lost = json.loads(json.dumps(run))
    lost["actors"][0].update(operation_id="op-killed", recovery_binding={})
    lost["actors"][0]["usage"]["review_controller"] = {"pid": _dead_pid(), "session": "gone"}
    gap = collect_task_acceptance_run(lost, drive_root=tmp_path, usage_ctx=cold)
    assert gap.actors[0]["operation_state"] == "custody_lost" and "never re-sent" in gap.actors[0]["error"]
    assert model.calls == 1


def test_an_older_snapshot_never_overwrites_a_newer_published_verdict(tmp_path):
    from ouroboros.review_projection import publish_acceptance_checkpoint

    write_task_result(tmp_path, TASK, "completed", chat_id=3)
    request = {"surface": "task_acceptance", "retry_key": "wave-x", "task_id": TASK}
    pending = {"authority": "host_root", "request": request, "panel_id": "panel_x", "panel_index": 0,
               "aggregate_signal": "DEGRADED",
               "actors": [{"slot_id": "a", "operation_state": "pending_dispatch", "late_result_pending": True}]}
    settled = {**pending, "aggregate_signal": "PASS",
               "actors": [{"slot_id": "a", "operation_state": "late_settled", "status": "ok",
                           "parsed": {"verdict": "PASS"}, "semantic_verdict": "PASS"}],
               "late_settlement": {"note": "On the delivered version of this answer, reviewers later passed it.",
                                   "settled_at": "2026-09-26T00:00:00+00:00"}}
    ctx = SimpleNamespace(task_id=TASK, drive_root=tmp_path, budget_drive_root=tmp_path, task_metadata={})
    # The worker's own trace publishes the pending snapshot twice (local revisions 1, 2)...
    worker_trace = {"review_runs": [json.loads(json.dumps(pending))]}
    publish_acceptance_checkpoint(ctx, worker_trace, task_id=TASK, drive_root=tmp_path)
    # ...a late collector in another process publishes the settled verdict...
    publish_acceptance_checkpoint(ctx, {"review_runs": [settled]}, task_id=TASK, drive_root=tmp_path, partial_trace=True)
    stored = load_task_result(tmp_path, TASK)["review_projection"]
    assert stored["panels"][0]["aggregate_signal"] == "PASS"
    newest = stored["panels"][0]["publication_revision"]
    # ...and the delayed worker snapshot arrives last with a stale local counter.
    worker_trace["review_runs"][0]["actors"][0]["note"] = "moved"
    publish_acceptance_checkpoint(ctx, worker_trace, task_id=TASK, drive_root=tmp_path)
    stored = load_task_result(tmp_path, TASK)["review_projection"]
    panel = stored["panels"][0]
    assert panel["aggregate_signal"] == "PASS" and panel["late_settlement"]["settled_at"]
    assert panel["publication_revision"] == newest, "a stale snapshot is never ordered past the verdict"


def test_evicted_trace_falls_back_to_the_canonical_published_source(tmp_path, monkeypatch):
    """A worker rebound to another task (or trace eviction) still announces the wave once."""
    from ouroboros.acceptance_settlement import attach_late_acceptance_settlement
    from ouroboros.review_projection import publish_acceptance_checkpoint

    model = _HeldModel()
    write_task_result(tmp_path, TASK, "running", chat_id=3)
    ctx = _ctx(tmp_path)
    run = _released_panel(tmp_path, ctx, model)
    # The loop published its pending panel, then the trace left memory.
    publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=TASK, drive_root=tmp_path)
    ctx._acceptance_settlement_traces = {}
    write_task_result(tmp_path, TASK, "completed", chat_id=3, result="reviewed answer A")
    events = []
    monkeypatch.setattr("ouroboros.acceptance_settlement.attach_late_acceptance_settlement",
                        lambda *a, **k: events.append("in-process-settlement") or False)
    _settle(model, run)
    monkeypatch.undo()
    assert events == ["in-process-settlement"], "the settlement thread reached the late path"
    stored = load_task_result(tmp_path, TASK)
    assert attach_late_acceptance_settlement(ctx, SimpleNamespace(retry_key="wave-cas", task_id=TASK),
                                             {"slots": {"a": "ok"}, "total": 1}, result=stored)
    rows = [ctx.event_queue.get_nowait() for _ in range(ctx.event_queue.qsize())]
    late = [row for row in rows if row.get("system_type") == "acceptance_late_settlement"]
    assert len(late) == 1 and late[0]["progress_meta"]["late_evidence"]["source_ref"]["kind"] == "task_source"
    panel = load_task_result(tmp_path, TASK)["review_projection"]["panels"][0]
    assert panel["aggregate_signal"] == "PASS" and panel["late_settlement"]["settled_after_terminal"] is True
    # A duplicate settlement event creates no second row and no action.
    again = load_task_result(tmp_path, TASK)
    assert not attach_late_acceptance_settlement(ctx, SimpleNamespace(retry_key="wave-cas", task_id=TASK),
                                                 {"slots": {"a": "ok"}, "total": 1}, result=again)
    assert ctx.event_queue.empty() and model.calls == 1


@pytest.mark.parametrize("retry_root", [False, True])
@pytest.mark.parametrize("checkpoint_format", ["current", "landed_bb27"])
def test_maintenance_collects_a_dead_controllers_operation_once(tmp_path, monkeypatch, retry_root, checkpoint_format):
    """The controller died after dispatch and before any publication: the existing
    maintenance pass finds the retained checkpoint and settles it at $0, once."""
    from ouroboros import model_wait
    from ouroboros.artifacts import read_actor_source_bytes

    if checkpoint_format == "landed_bb27":
        # bb27's actual _write_operation_pointer source is this exact asdict
        # envelope without paid_authority. Produce it BEFORE the paid stamp;
        # never retrofit a new field into immutable legacy checkpoint bytes.
        writer = review_operation._write_operation_pointer
        def legacy_writer(*a, **kw):
            kw.pop("paid_authority", None)
            return writer(*a, **kw)
        monkeypatch.setattr(review_operation, "_write_operation_pointer", legacy_writer)

    delivered = queue.Queue()
    monkeypatch.setattr("supervisor.workers.get_event_q", lambda: delivered)
    class StampedModel(_HeldModel):
        def chat(self, **kwargs):
            from ouroboros.review_dispatch import invoke_bound_api_review_paid_stamp
            invoke_bound_api_review_paid_stamp()
            return super().chat(**kwargs)

    model = StampedModel()
    paid = hashlib.sha256(b"paid material").hexdigest()
    retry_key = f"task_acceptance:{paid}"
    from ouroboros.review_projection import build_review_binding
    from ouroboros.review_dispatch import bind_task_acceptance_paid_dispatch

    accounting = "original-root" if retry_root else TASK
    metadata = ({"root_task_id": accounting, "delegation_role": "root", "parent_task_id": "",
                 "original_task_id": accounting, "timeout_retry_from": accounting} if retry_root else {
                     "root_task_id": TASK, "delegation_role": "root", "parent_task_id": ""})
    binding = {**build_review_binding(candidate="reviewed answer A", evidence={"requirement": "exact"},
                                      fence_token_or_state="original"), "paid_identity": paid}
    write_task_result(tmp_path, accounting, "running", chat_id=3, task_contract={
        "schema_version": 1, "deadline_at": "", "budget_profile": {"improvement_policy": "fixed", "max_improvement_passes": None}})
    write_task_result(tmp_path, TASK, "running", chat_id=3, **metadata)
    ctx = _ctx(tmp_path)
    ctx.task_metadata = metadata
    admission = SimpleNamespace(tools=SimpleNamespace(_ctx=ctx), task_id=TASK, drive_root=tmp_path,
                                review_binding=binding)
    # Inject controller death at the identity source; pointer and retained bytes
    # keep the exact same identity, as after a real process exit.
    dead = {"pid": _dead_pid(), "birth": "gone", "session": "gone"}
    monkeypatch.setattr(review_operation, "controller_identity", lambda: dead)
    with model_wait.task_model_wait_scope(task={"id": TASK, "chat_id": 3, "_attempt": 1, "metadata": metadata},
                                          drive_root=tmp_path, event_queue=None, worker_slot_held=False):
        with bind_task_acceptance_paid_dispatch(admission):
            run = _released_panel(tmp_path, ctx, model, retry_key=retry_key)
        # An advisory child/off-mode review through the same seam: no wallet claim.
        advisory = _released_panel(tmp_path, ctx, model, retry_key="task_acceptance:advisory-evidence")
    operations = load_task_result(tmp_path, TASK)["review_operations"]
    owner_id = next(key for key, row in operations.items() if row["retry_key"] == retry_key)
    advisory_id = next(key for key, row in operations.items() if row["retry_key"] != retry_key)
    legacy_bytes = read_actor_source_bytes(tmp_path, TASK, operations[owner_id]['source_ref'])
    if checkpoint_format == "landed_bb27":
        assert set(json.loads(legacy_bytes)) == {"schema_version", "owner_id", "task_id", "surface",
            "retry_key", "task_attempt", "controller", "operations", "recorded_at", "request", "slot_roster"}
    _settle(model, run)
    _settle(model, advisory)
    # A worker leaves _ACTIVE before its operation closes its own pointer; a death
    # simulated before that close lands would be overwritten by the live controller.
    _until(lambda: all(load_task_result(tmp_path, TASK)["review_operations"][key]["state"]
                       not in {"retained", "dispatched"} for key in (owner_id, advisory_id)))
    # Death after dispatch: nothing published, the pointer still says dispatched, the pid is gone.
    def died(rows):
        return {**rows, **{key: {**rows[key], "state": "dispatched", "controller": dead}
                           for key in (owner_id, advisory_id)}}

    review_operation._update_operations(tmp_path, TASK, died)
    write_task_result(tmp_path, TASK, "failed", chat_id=3, result="")
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", lambda *a, **k: pytest.fail("runner"))
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert read_actor_source_bytes(tmp_path, TASK, operations[owner_id]['source_ref']) == legacy_bytes
    if checkpoint_format == "landed_bb27" and retry_root:
        assert not report['settled'] and not report['errors']
        assert {row['owner_id']: row['status'] for row in report['pending']} == {
            owner_id: 'unavailable', advisory_id: 'unavailable'}
        assert not load_task_result(tmp_path, TASK).get('review_projection')
        assert delivered.empty() and model.calls == 2
        return
    assert {row["owner_id"]: row["status"] for row in report["settled"]} == {owner_id: "announced"}, report
    assert {row["owner_id"]: row["status"] for row in report["pending"]} == {advisory_id: "unavailable"}
    stored = load_task_result(tmp_path, TASK)
    assert stored["review_operations"][owner_id]["state"] == "collected"
    assert stored["review_operations"][advisory_id]["state"] == "dispatched", \
        "never republished as a host panel, and never marked unavailable for good"
    panels = [p for p in stored["review_projection"]["panels"] if p.get("late_settlement")]
    assert len(panels) == 1 and panels[0]["aggregate_signal"] == "PASS"
    assert panels[0]["actors"][0]["semantic_verdict"] == "PASS"
    assert panels[0]["late_settlement"]["reviewed_subject"]["binding_hash"] == binding["binding_hash"]
    rows = [delivered.get_nowait() for _ in range(delivered.qsize())]
    assert [row["delivery_id"] for row in rows] == [f"acceptance-late:{retry_key}"]
    again = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert again["settled"] == [] and [row["owner_id"] for row in again["pending"]] == [advisory_id]
    assert delivered.empty() and model.calls == 2


@pytest.fixture
def fresh_sends(monkeypatch):
    """The send handler's process-local dedupe starts empty for each test."""
    import collections

    from supervisor import events_chat_delivery as chat

    monkeypatch.setattr(chat, "_DELIVERED_MESSAGE_IDS", collections.deque(maxlen=256))


def _send_ctx(root, sends, *, fail=False):
    from ouroboros.utils import append_jsonl

    def send(chat_id, text, **_kwargs):
        if fail:
            raise RuntimeError("chat transport down")
        sends.append((chat_id, text))

    return SimpleNamespace(DRIVE_ROOT=root, RUNNING={}, send_with_budget=send, append_jsonl=append_jsonl)


def _answer_event(text):
    from supervisor.terminal_delivery import delivery_id_for

    return {"type": "send_message", "chat_id": 3, "task_id": TASK, "text": text, "format": "markdown",
            "delivery_id": delivery_id_for(TASK, text)}


_REVIEWED_A = {"request": {"subject": "answer A", "retry_key": "wave"}, "panel_id": "p", "aggregate_signal": "FAIL",
               "superseded_by_revision": True,
               "actors": [{"slot_id": "a", "semantic_verdict": "FAIL", "operation_state": "settled"}]}


@pytest.mark.parametrize("sent, expected", [("answer B", "different"), ("answer A", "delivered")])
def test_late_fact_compares_the_reviewed_bytes_with_the_exact_sent_bytes(tmp_path, monkeypatch, fresh_sends,
                                                                         sent, expected):
    """The real send handler is the producer: owed is not delivered; after the send its
    receipt names the exact text and the ROUTED room, retained in the immutable store."""
    from ouroboros.acceptance_settlement import (
        _late_settlement_text, emitted_answer_fact, late_acceptance_facts, late_evidence_fact,
    )
    from ouroboros.artifacts import read_actor_source_bytes
    from supervisor import events_chat_delivery as chat
    from supervisor.terminal_delivery import register_pending_delivery

    # The mutable result and the recorded rewrite both point at A; neither is byte proof.
    write_task_result(tmp_path, TASK, "completed", chat_id=3, result="answer A")
    event = _answer_event(sent)
    assert register_pending_delivery(tmp_path, event)
    owed = emitted_answer_fact(tmp_path, TASK)
    assert owed["state"] == "owed" and owed["delivered"] == []
    assert late_evidence_fact(_REVIEWED_A, owed, settled_at="t")["reviewed_revision"] == "unknown"
    # The task was bound to a project room after admission: the send goes there.
    monkeypatch.setattr(chat, "_bound_project_chat_id", lambda *_args: 42)
    sends = []
    chat._handle_send_message(dict(event), _send_ctx(tmp_path, sends))
    assert sends == [(42, sent)]
    emitted = emitted_answer_fact(tmp_path, TASK)
    assert emitted["state"] == "delivered" and emitted["owed_delivery_ids"] == []
    receipt = emitted["delivered"][0]
    assert receipt["chat_id"] == 42 and receipt["text_sha256"] == hashlib.sha256(sent.encode()).hexdigest()
    retained = json.loads(read_actor_source_bytes(tmp_path, TASK, receipt["source_ref"]))
    assert (retained["text"], retained["chat_id"], retained["delivery_id"]) == (sent, 42, event["delivery_id"])
    fact = late_evidence_fact(_REVIEWED_A, emitted, settled_at="2026-09-26T12:00:00+00:00")
    assert fact["reviewed_revision"] == expected and fact["reviewed_is_emitted"] is (expected == "delivered")
    assert fact["reviewed_subject"]["subject_sha256"] == hashlib.sha256(b"answer A").hexdigest()
    assert fact["reviewed_superseded"] is True, "the recorded rewrite stays its own fact"
    text = _late_settlement_text(_REVIEWED_A, {"slots": {"a": "ok"}}, fact)
    assert text.startswith({"different": "On a version of this answer other than the one delivered",
                            "delivered": "On the delivered version"}[expected]) and "rejected it." in text
    facts = late_acceptance_facts({"task_id": TASK, "review_projection": {"panels": [
        {"surface": "task_acceptance", "panel_id": "p", "aggregate_signal": "FAIL",
         "applied_source_ref": {"kind": "task_source"}, "late_settlement": {"note": text, **fact}}]}})
    assert facts[0]["settled_at"] == "2026-09-26T12:00:00+00:00" and facts[0]["source_ref"] == {"kind": "task_source"}


def test_an_unconfirmed_or_uncaptured_send_proves_no_bytes_and_a_replay_captures_the_same(tmp_path, monkeypatch,
                                                                                         fresh_sends):
    from ouroboros.acceptance_settlement import emitted_answer_fact
    from supervisor import events_chat_delivery as chat
    from supervisor import terminal_delivery as registry

    write_task_result(tmp_path, TASK, "completed", chat_id=3, result="answer B")
    event = _answer_event("answer B")
    registry.register_pending_delivery(tmp_path, event)
    # The send raised: nothing is registered, the answer stays owed, the version unknown.
    chat._handle_send_message(dict(event), _send_ctx(tmp_path, [], fail=True))
    assert emitted_answer_fact(tmp_path, TASK)["state"] == "owed"
    # The send landed but its bytes could not be retained: dedupe still holds, proof does not.
    sends = []
    with monkeypatch.context() as patched:
        patched.setattr("ouroboros.artifacts.store_actor_source_bytes",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("store unavailable")))
        chat._handle_send_message(dict(event), _send_ctx(tmp_path, sends))
    fact = emitted_answer_fact(tmp_path, TASK)
    assert sends == [(3, "answer B")] and registry.already_delivered(tmp_path, event["delivery_id"])
    assert fact["state"] == "unknown" and fact["unverified_delivery_ids"] == [event["delivery_id"]]
    # An owed row replayed through the ordinary outbox reaches the same handler and receipt.
    other = TASK + "-replayed"
    write_task_result(tmp_path, other, "completed", chat_id=3, result="answer C")
    replay = {**_answer_event("answer C"), "task_id": other,
              "delivery_id": registry.delivery_id_for(other, "answer C")}
    registry.register_pending_delivery(tmp_path, replay)
    monkeypatch.setattr(registry, "_replay_due", lambda _row: True)
    outbox = queue.Queue()
    assert registry.replay_pending_deliveries(tmp_path, event_queue=outbox) == [replay["delivery_id"]]
    chat._handle_send_message(outbox.get_nowait(), _send_ctx(tmp_path, sends))
    replayed = emitted_answer_fact(tmp_path, other)
    assert replayed["state"] == "delivered"
    assert replayed["delivered"][0]["text_sha256"] == hashlib.sha256(b"answer C").hexdigest()
    assert emitted_answer_fact(tmp_path, TASK)["state"] == "unknown", "receipts are per task and survive rewrites"


def test_equal_pending_counts_never_hide_a_settled_actor(tmp_path):
    """Per-slot compare under the canonical record: a snapshot that settles one slot while
    regressing another, or that disagrees with a settled producer, is rejected — never a
    stale-verdict overwrite — while a pure advance lands."""
    from ouroboros.review_projection import publish_acceptance_checkpoint

    write_task_result(tmp_path, TASK, "completed", chat_id=3)
    ctx = SimpleNamespace(task_id=TASK, drive_root=tmp_path, budget_drive_root=tmp_path, task_metadata={})

    def actor(slot, verdict=None, op=None):
        if verdict is None:
            return {"slot_id": slot, "operation_id": op or f"op-{slot}", "operation_state": "pending_dispatch",
                    "late_result_pending": True}
        return {"slot_id": slot, "operation_id": op or f"op-{slot}", "operation_state": "late_settled",
                "status": "ok", "parsed": {"verdict": verdict}, "semantic_verdict": verdict}

    def publish(*actors):
        run = {"authority": "host_root", "panel_id": "panel_x", "panel_index": 0, "aggregate_signal": "DEGRADED",
               "request": {"surface": "task_acceptance", "retry_key": "wave-x", "task_id": TASK},
               "actors": list(actors)}
        return publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=TASK, drive_root=tmp_path)

    def stored():
        panel = load_task_result(tmp_path, TASK)["review_projection"]["panels"][0]
        return {row["slot_id"]: row["semantic_verdict"] or row["operation_state"] for row in panel["actors"]}

    assert publish(actor("a", "PASS"), actor("b"))["rejected"] == []
    mixed = publish(actor("a"), actor("b", "FAIL"))
    assert mixed["status"] == "published" and mixed["rejected"] == ["panel_x"]
    assert stored() == {"a": "PASS", "b": "pending_dispatch"}, "equal pending counts, different settled actors"
    assert publish(actor("a", "FAIL"), actor("b"))["rejected"] == ["panel_x"], "a settled verdict never flips"
    assert publish(actor("a", "PASS", op="op-other"), actor("b"))["rejected"] == ["panel_x"]
    assert publish(actor("a"), actor("b"))["rejected"] == [] and stored()["a"] == "PASS", "a stale snapshot keeps it"
    assert publish(actor("a", "PASS"), actor("b", "FAIL"))["rejected"] == []
    assert stored() == {"a": "PASS", "b": "FAIL"}, "a pure advance lands"


def test_reconcile_passes_the_known_controller_to_rows_without_one(tmp_path, forbid_effects, monkeypatch):
    calls = _observe(monkeypatch, {})
    run = _session_run()
    run["actors"][0]["usage"] = {}
    advanced = reconcile_pending_acceptance_runs({"review_runs": [run]}, drive_root=tmp_path,
                                                 usage_ctx=_ctx(tmp_path),
                                                 controller={"pid": _dead_pid(), "session": "gone"})
    assert advanced == 1 and run["actors"][0]["operation_state"] == "custody_lost"
    assert run["collection"]["facts"] == {"s": "unavailable"} and calls == []


def test_concurrent_collectors_of_one_wave_publish_one_verdict_and_owe_one_row(tmp_path, monkeypatch):
    """The in-process settlement and the maintenance pass may collect one wave at once:
    both are pure, the projection keeps one settled panel, and the owed row is one."""
    from ouroboros.acceptance_settlement import settle_acceptance_operation
    from ouroboros.review_projection import publish_acceptance_checkpoint
    from supervisor.terminal_delivery import pending_deliveries

    model = _HeldModel()
    write_task_result(tmp_path, TASK, "running", chat_id=3)
    ctx = _ctx(tmp_path)
    run = _released_panel(tmp_path, ctx, model, retry_key="wave-race")
    publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=TASK, drive_root=tmp_path)
    write_task_result(tmp_path, TASK, "completed", chat_id=3, result="reviewed answer A")
    monkeypatch.setattr("ouroboros.acceptance_settlement.attach_late_acceptance_settlement", lambda *a, **k: False)
    _settle(model, run)
    monkeypatch.undo()
    stored = load_task_result(tmp_path, TASK)
    barrier, queues, outcomes = threading.Barrier(2, timeout=5), [queue.Queue(), queue.Queue()], []

    def collector(sink):
        collector_ctx = SimpleNamespace(task_id=TASK, task_attempt=1, drive_root=tmp_path, budget_drive_root=tmp_path,
                                        task_metadata={}, event_queue=sink)
        barrier.wait()
        outcomes.append(settle_acceptance_operation(collector_ctx, retry_key="wave-race", task_id=TASK, result=stored))

    threads = [threading.Thread(target=collector, args=(sink,)) for sink in queues]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(outcomes) == 2 and set(outcomes) <= {"announced", "published"}
    rows = [row for sink in queues for row in list(sink.queue) if row.get("system_type") == "acceptance_late_settlement"]
    assert [row["delivery_id"] for row in rows] == ["acceptance-late:wave-race"], \
        "the second collector finds the notice owed and queues no live copy"
    assert [row["delivery_id"] for row in pending_deliveries(tmp_path)] == ["acceptance-late:wave-race"]
    panels = load_task_result(tmp_path, TASK)["review_projection"]["panels"]
    assert len(panels) == 1 and panels[0]["aggregate_signal"] == "PASS" and panels[0]["late_settlement"]
    assert model.calls == 1


def _until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("expected state did not arrive")


def test_a_late_settlement_that_did_not_land_is_never_announced_and_maintenance_retries_it(tmp_path, monkeypatch):
    from ouroboros import model_wait
    from ouroboros.review_projection import publish_acceptance_checkpoint

    delivered = queue.Queue()
    monkeypatch.setattr("supervisor.workers.get_event_q", lambda: delivered)
    model = _HeldModel()
    write_task_result(tmp_path, TASK, "running", chat_id=3)
    ctx = _ctx(tmp_path)
    with model_wait.task_model_wait_scope(task={"id": TASK, "chat_id": 3, "_attempt": 1}, drive_root=tmp_path,
                                          event_queue=None, worker_slot_held=False):
        run = _released_panel(tmp_path, ctx, model, retry_key="wave-unpublished")
    publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=TASK, drive_root=tmp_path)
    owner_id = next(iter(load_task_result(tmp_path, TASK)["review_operations"]))
    write_task_result(tmp_path, TASK, "completed", chat_id=3, result="reviewed answer A")

    def refused(*_args, **_kwargs):
        raise ValueError("the canonical record refused this publication")

    with monkeypatch.context() as patched:
        patched.setattr("ouroboros.review_projection._keep_newer_producer_facts", refused)
        _settle(model, run)
        _until(lambda: load_task_result(tmp_path, TASK)["review_operations"][owner_id]["state"] == "unpublished")
    assert [e for e in list(ctx.event_queue.queue) if e.get("system_type") == "acceptance_late_settlement"] == [], \
        "a settlement the record did not take is never announced"
    panel = load_task_result(tmp_path, TASK)["review_projection"]["panels"][0]
    assert "late_settlement" not in panel and panel["actors"][0]["transport_status"] == "awaiting", \
        "the stored panel still shows the reviewer owed"
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["settled"] == [{"task_id": TASK, "owner_id": owner_id, "status": "announced"}], report
    stored = load_task_result(tmp_path, TASK)
    assert stored["review_operations"][owner_id]["state"] == "collected"
    assert stored["review_projection"]["panels"][0]["late_settlement"]["settled_after_terminal"] is True
    assert [row["delivery_id"] for row in list(delivered.queue)] == ["acceptance-late:wave-unpublished"]
    assert model.calls == 1


def _checkpointed_operation(root, *, legacy=False):
    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.review_projection import build_review_binding
    from ouroboros.task_results import claim_task_acceptance_review_cycle, resolve_task_lineage

    paid = hashlib.sha256(b"paid material").hexdigest()
    retry_key = f"task_acceptance:{paid}"
    request = ReviewRequest(surface="task_acceptance", task_id=TASK, task_attempt=1, goal="goal", subject="answer",
                            evidence={"requirement": "exact"}, retry_key=retry_key)
    binding = {**build_review_binding(candidate=request.subject, evidence=request.evidence,
                                      fence_token_or_state="original"), "paid_identity": paid}
    source = {"schema_version": 1, "task_id": TASK, "owner_id": "review-operation-" + "e" * 32,
              "surface": "task_acceptance", "retry_key": retry_key,
              "recorded_at": "2026-09-26T10:00:00+00:00",
              "request": dataclasses.asdict(request), "operations": {"a": "op-a"}, "task_attempt": 1,
              "slot_roster": [dataclasses.asdict(ReviewSlot(slot_id="a", model="model/a"))],
              "controller": {"pid": _dead_pid(), "birth": "gone", "session": "gone"},
              "paid_authority": {"schema_version": 1, "authority": "host_root",
                                 "lineage": resolve_task_lineage(TASK), "binding": binding}}
    if legacy:
        source.pop('paid_authority')  # old producer shape, before immutable storage
    ref = store_actor_source_bytes(root, TASK, category="context_checkpoints", source_id="acceptance-operation",
                                   data=json.dumps(source, default=str).encode("utf-8"), extension="json")
    write_task_result(root, TASK, "completed", result="answer", root_task_id=TASK,
                      delegation_role="root", parent_task_id="", task_contract={
        "schema_version": 1, "deadline_at": "", "budget_profile": {"improvement_policy": "fixed", "max_improvement_passes": None}})
    claim_task_acceptance_review_cycle(root, TASK, binding, claimed_by_task_id=TASK)
    checkpoint = {**{key: source[key] for key in ("retry_key", "owner_id", "controller", "operations", "task_attempt")},
                  "source_ref": ref}
    return checkpoint, binding


def test_an_unreadable_published_source_is_never_duplicated_from_the_checkpoint(tmp_path):
    from ouroboros.acceptance_settlement import canonical_acceptance_trace, settle_acceptance_operation

    checkpoint, binding = _checkpointed_operation(tmp_path)
    retry_key, ref = checkpoint["retry_key"], checkpoint["source_ref"]
    result = load_task_result(tmp_path, TASK)
    missing = {**ref, "path": ref["path"].replace("acceptance-operation", "acceptance-missing"), "sha256": "0" * 64}
    ours = {**result, "review_projection": {"panels": [
        {"surface": "task_acceptance", "panel_id": "panel_pub", "binding_hash": binding["binding_hash"], "applied_source_ref": missing}]}}
    assert canonical_acceptance_trace(tmp_path, TASK, retry_key, result=ours, checkpoint=checkpoint) == {
        "review_runs": [], "source_status": "unreadable", "unreadable_panels": ["panel_pub"]}
    ctx = SimpleNamespace(task_id=TASK, drive_root=tmp_path, budget_drive_root=tmp_path, task_metadata={},
                          event_queue=queue.Queue())
    assert settle_acceptance_operation(ctx, retry_key=retry_key, task_id=TASK, result=ours,
                                       checkpoint=checkpoint) == "source_unreadable"
    assert ctx.event_queue.empty()
    # Another operation's unreadable panel does not hide this one's retained checkpoint.
    theirs = {**ours, "review_projection": {"panels": [{**ours["review_projection"]["panels"][0],
                                                         "binding_hash": "d" * 64}]}}
    trace = canonical_acceptance_trace(tmp_path, TASK, retry_key, result=theirs, checkpoint=checkpoint)
    assert [run["panel_id"] for run in trace["review_runs"]] == ["panel_" + trace["review_runs"][0]["binding_hash"][:16]]


def test_recovery_defers_an_unverifiable_controller_without_marking_it(tmp_path):
    write_task_result(tmp_path, TASK, "completed", chat_id=3, review_operations={
        "review-operation-legacy": {"state": "dispatched", "surface": "task_acceptance", "retry_key": "task_acceptance:x",
                                    "controller": {"pid": os.getpid(), "session": "no-birth-recorded"}}})
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["settled"] == [] and report["pending"] == []
    assert report["deferred"] == [{"task_id": TASK, "owner_id": "review-operation-legacy",
                                   "reason": "controller_unverifiable"}]
    assert load_task_result(tmp_path, TASK)["review_operations"]["review-operation-legacy"]["state"] == "dispatched"


def test_notice_registration_and_live_enqueue_failure_retain_retry_duty(tmp_path, monkeypatch):
    from ouroboros import acceptance_settlement, model_wait
    from ouroboros.review_projection import publish_acceptance_checkpoint

    class RefusingQueue:
        def put(self, _event):
            raise OSError("live queue unavailable")

    delivered, model = queue.Queue(), _HeldModel()
    monkeypatch.setattr("supervisor.workers.get_event_q", lambda: delivered)
    write_task_result(tmp_path, TASK, "running", chat_id=3)
    ctx = _ctx(tmp_path)
    with model_wait.task_model_wait_scope(task={"id": TASK, "chat_id": 3, "_attempt": 1}, drive_root=tmp_path,
                                          event_queue=None, worker_slot_held=False):
        run = _released_panel(tmp_path, ctx, model, retry_key="wave-notice-failed")
    publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=TASK, drive_root=tmp_path)
    owner_id = next(iter(load_task_result(tmp_path, TASK)["review_operations"]))
    write_task_result(tmp_path, TASK, "completed", chat_id=3, result="reviewed answer A")
    ctx.event_queue = RefusingQueue()
    with monkeypatch.context() as patched:
        patched.setattr("supervisor.terminal_delivery.register_pending_delivery", lambda *_a, **_kw: False)
        _settle(model, run)
        _until(lambda: load_task_result(tmp_path, TASK)["review_operations"][owner_id]["state"] in {"closed", "unpublished"})
    stored = load_task_result(tmp_path, TASK)
    panel = stored["review_projection"]["panels"][0]
    assert panel["late_settlement"] and panel["applied_source_ref"]  # critique publication survived
    assert stored["review_operations"][owner_id]["state"] == "unpublished"
    # A fresh collector cannot rely on the author's process-local retry set.
    with acceptance_settlement._LATE_LOCK:
        acceptance_settlement._LATE_UNPUBLISHED.clear()
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["settled"] == [{"task_id": TASK, "owner_id": owner_id, "status": "announced"}], report
    after = load_task_result(tmp_path, TASK)
    assert after["review_projection"]["panels"][0]["late_settlement"] == panel["late_settlement"]
    assert [e["delivery_id"] for e in list(delivered.queue)] == ["acceptance-late:wave-notice-failed"]
    assert model.calls == 1
