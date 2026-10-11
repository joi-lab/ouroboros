"""Historical feedback uses real task-result locks and complete producer CAS."""
from __future__ import annotations

import copy
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ouroboros import task_results as tr
from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.observability import call_manifest_path, persist_call
from ouroboros.review_dispatch import review_operation_binding
from ouroboros.review_records import ReviewActorRecord, ReviewRequest, ReviewSlot
from ouroboros.tools import plan_review_artifacts as artifacts
from ouroboros.tools import plan_review_collect as collect
from ouroboros.tools.plan_review_runtime import plan_reviewer_config_fingerprint

TASK = "history-task"
A, B, C = "a" * 64, "b" * 64, "c" * 64


def wave(root, fingerprint=A, *, cycle=1, closed=False, count=2, paid=False):
    slots = [ReviewSlot(f"s{i}", "fake/model") for i in range(count)]
    key = f"plan_review:{fingerprint}:{cycle}"
    request = ReviewRequest(
        surface="plan_review", goal="complete task", task_id=TASK, retry_key=key,
        call_type="plan_review", policy={"output_contract": "JSON findings array"},
        reconciliation_identity={"subject_hash": fingerprint, "epoch": key,
                                 "roster_hash": plan_reviewer_config_fingerprint(slots),
                                 "health_epoch": [], "root_task_id": TASK,
                                 "task_attempt": 1, "review_contract": "frozen-contract"},
    )
    rows = []
    for slot in slots:
        op = f"review-{fingerprint[:8]}-{cycle}-{slot.slot_id}"
        binding = review_operation_binding(request, slot, op)
        rows.append({"slot_id": slot.slot_id, "model": slot.model, "operation_id": op,
                     "operation_state": "pending_dispatch", "late_result_pending": True,
                     "recovery_binding": binding, "status": "error", "ok": False})
        persist_call(root, task_id=TASK, call_id=op + "_prompt", call_type="review_prompt",
                     payload={"request": asdict(request), "slot": asdict(slot)},
                     manifest={"review_operation_binding": binding})
    value = dict(request_fingerprint=fingerprint, cycle_index=cycle, retry_key=key,
                 aggregate="REVIEW_REQUIRED" if closed else "DEGRADED", closed=closed,
                 paid=paid, custody_pending=True, actors=rows, findings=[], spec={"goal": "complete task"},
                 dispositions=[], counts={"blocking": 0}, health_epoch=[],
                 reviewer_config_fingerprint=plan_reviewer_config_fingerprint(slots))
    from ouroboros.tools.plan_spec import spec_hash
    value["spec_hash"] = spec_hash(value["spec"])
    value["wave_artifact"] = artifacts.persist_wave(root, TASK, value)
    tr.record_plan_review_wave(root, TASK, value)
    return value, request, slots


def complete(root, value, request, slot, *, dispatched=True):
    row = next(r for r in value["actors"] if r["slot_id"] == slot.slot_id)
    op = row["operation_id"]
    actor = ReviewActorRecord(
        slot_id=slot.slot_id, model=slot.model,
        status="ok" if dispatched else "not_dispatched",
        operation_id=op, operation_state="settled" if dispatched else "not_dispatched",
        error="" if dispatched else "not started", recovery_binding=row["recovery_binding"],
    )
    text = json.dumps([{"id": "late", "class": "note", "summary": "Complete old feedback " * 3000,
                        "breaks": "", "locator": "", "recommendation": "keep this source"}])
    outcome = {k: v for k, v in asdict(actor).items()
               if k not in {"raw_text", "usage", "prompt_ref", "response_ref"}}
    actor.response_ref = persist_call(
        root, task_id=TASK, call_id=op + "_response", call_type="review_response",
        payload={"producer_outcome": outcome, "message": {"content": text},
                 "usage": {"physical_attempt_state": "settled" if dispatched else "released"}},
        manifest={"producer_complete": True, "review_operation_binding": row["recovery_binding"]},
    )
    return actor, text


def state(root):
    return tr.load_plan_review_state(root, TASK)


def original_authority(value):
    return {key: copy.deepcopy(value.get(key)) for key in
            ("actors", "findings", "aggregate", "closed", "dispositions", "wave_artifact", "counts")}


def test_a_b_late_a_preserves_authority_and_complete_source(tmp_path):
    a, req, slots = wave(tmp_path)
    b, _, _ = wave(tmp_path, B, cycle=2, closed=True, count=0, paid=True)
    before = state(tmp_path)
    actor, text = complete(tmp_path, a, req, slots[0])
    ctx = SimpleNamespace(drive_root=tmp_path, task_id=TASK, emit_progress_fn=lambda text: None)
    collect.announce_released_settlement(ctx, request=req, task_id=TASK,
                                        actor=actor, settled_wave={})
    after = state(tmp_path)
    old = tr.plan_review_wave(after, A)
    assert original_authority(old) == original_authority(a)
    assert tr.plan_review_wave(after, B) == tr.plan_review_wave(before, B)
    assert after["current_attempt"] == before["current_attempt"]
    assert old["custody_pending"] is True  # second slot has not settled
    from ouroboros.tools.plan_review_runtime import plan_pending_actors
    assert [r["slot_id"] for r in plan_pending_actors(old)] == [slots[1].slot_id]
    assert after["cycles_paid"] == 2
    supplement = json.loads(read_actor_source_bytes(
        tmp_path, TASK, old["historical_supplements"][0]["source_ref"]))
    assert supplement["result"]["text"] == text
    assert supplement["result"]["operation_id"] == actor.operation_id
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 0
    assert state(tmp_path)["cycles_paid"] == 2


@pytest.mark.parametrize("terminal", [False, True])
def test_closed_wave_and_terminal_root_accept_history_without_reopening(tmp_path, terminal):
    a, req, slots = wave(tmp_path, closed=True, count=1, paid=True)
    if terminal:
        tr.write_task_result(tmp_path, TASK, "completed", result="original answer")
    before = state(tmp_path)
    complete(tmp_path, a, req, slots[0])
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 1
    after = state(tmp_path)
    assert original_authority(tr.plan_review_wave(after, A)) == original_authority(a)
    assert after["current_attempt"] == before["current_attempt"] and after["cycles_paid"] == 1
    assert not tr.plan_review_wave(after, A)["custody_pending"]
    from ouroboros.tools.plan_review_runtime import plan_wave_has_in_flight
    assert not plan_wave_has_in_flight(tr.plan_review_wave(after, A))
    if terminal:
        result = tr.load_task_result(tmp_path, TASK)
        assert result["status"] == "completed" and result["result"] == "original answer"


def test_current_open_wave_is_collected_by_its_existing_owner(tmp_path):
    a, req, slots = wave(tmp_path, count=1)
    complete(tmp_path, a, req, slots[0])
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 0
    assert "historical_supplements" not in tr.plan_review_wave(state(tmp_path), A)


def test_two_concurrent_slots_are_once_only_paid_and_keep_new_current(tmp_path):
    a, req, slots = wave(tmp_path)
    wave(tmp_path, B, cycle=2, count=0, closed=True, paid=True)
    for slot in slots:
        complete(tmp_path, a, req, slot)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: collect.attach_historical_results(tmp_path, TASK, fingerprint=A), range(4)))
    actual = state(tmp_path)
    old = tr.plan_review_wave(actual, A)
    assert len(old["historical_supplements"]) == 2 and not old["custody_pending"]
    assert actual["cycles_paid"] == 2 and actual["current_attempt"]["fingerprint"] == B


def test_late_zero_send_closes_physical_custody_without_paying(tmp_path):
    a, req, slots = wave(tmp_path, count=1)
    wave(tmp_path, B, cycle=2, count=0, closed=True)
    complete(tmp_path, a, req, slots[0], dispatched=False)
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 1
    actual = state(tmp_path)
    assert actual["cycles_paid"] == 0
    assert not tr.plan_review_wave(actual, A)["custody_pending"]
    assert not tr.plan_review_wave(actual, A)["paid"]


@pytest.mark.parametrize("defect", ["missing", "partial", "wrong_cycle", "wrong_slot", "wrong_contract"])
def test_missing_or_mismatched_cas_never_changes_historical_wave(tmp_path, defect):
    a, req, slots = wave(tmp_path, count=1)
    wave(tmp_path, B, cycle=2, count=0, closed=True)
    actor, _ = complete(tmp_path, a, req, slots[0])
    manifest_path = call_manifest_path(tmp_path, TASK, actor.operation_id + "_response")
    manifest = json.loads(manifest_path.read_text())
    if defect == "missing":
        manifest_path.unlink()
    elif defect == "partial":
        manifest.pop("producer_complete")
        manifest_path.write_text(json.dumps(manifest))
    else:
        prompt_path = call_manifest_path(tmp_path, TASK, actor.operation_id + "_prompt")
        from ouroboros.observability import read_call_payload
        pm, payload, _ = read_call_payload(tmp_path, task_id=TASK, call_id=actor.operation_id + "_prompt")
        if defect == "wrong_cycle":
            payload["request"]["retry_key"] = f"plan_review:{A}:9"
        elif defect == "wrong_slot":
            payload["slot"]["slot_id"] = "unrelated"
        else:
            payload["request"]["reconciliation_identity"]["review_contract"] = "different"
        persist_call(tmp_path, task_id=TASK, call_id=actor.operation_id + "_prompt",
                     call_type="review_prompt", payload=payload,
                     manifest={"review_operation_binding": pm["review_operation_binding"]})
        assert prompt_path.exists()
    before = state(tmp_path)
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 0
    assert state(tmp_path) == before


def test_old_cycle_callback_does_not_attach_to_same_fingerprint_new_cycle(tmp_path):
    a, req, slots = wave(tmp_path, count=1)
    actor, _ = complete(tmp_path, a, req, slots[0])
    wave(tmp_path, A, cycle=2, count=1)
    wave(tmp_path, B, cycle=3, count=0, closed=True)
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A, operation_id=actor.operation_id) == 0
    assert "historical_supplements" not in tr.plan_review_wave(state(tmp_path), A)


def test_existing_record_seam_recovers_settlement_before_supersede_publication(tmp_path):
    a, req, slots = wave(tmp_path, count=1)
    complete(tmp_path, a, req, slots[0])
    # Early settlement cannot attach while A is still current. B's ordinary
    # exact-wave publication closes that race without a new queue or poller.
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 0
    b = {**a, "request_fingerprint": B, "cycle_index": 2, "retry_key": f"plan_review:{B}:2",
         "aggregate": "GREEN", "closed": True, "actors": [], "custody_pending": False, "paid": False}
    artifacts.record_exact_wave(tmp_path, TASK, b, b, need_evidence_seen=[], page_size=10)
    assert len(tr.plan_review_wave(state(tmp_path), A)["historical_supplements"]) == 1
    assert state(tmp_path)["current_attempt"]["fingerprint"] == B


def test_settlement_callback_never_waits_for_its_own_queue_or_active_lock(tmp_path, monkeypatch):
    from ouroboros import review_custody as custody
    a, req, slots = wave(tmp_path, count=1)
    wave(tmp_path, B, cycle=2, count=0, closed=True)
    actor, _ = complete(tmp_path, a, req, slots[0])
    entry = custody.ActiveReviewAttempt("isolated-late-entry", actor.operation_id, released_early=True)
    received = queue.Queue()
    ctx = SimpleNamespace(drive_root=tmp_path, task_id=TASK, emit_progress_fn=lambda text: None, event_queue=None)
    entered = threading.Event()
    original = collect.attach_historical_results
    def observed(*args, **kwargs):
        assert received.empty()  # the callback must not wait for this publication
        assert custody._ACTIVE_LOCK.acquire(blocking=False)
        custody._ACTIVE_LOCK.release()
        entered.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(collect, "attach_historical_results", observed)
    custody._settle_review_attempt(entry, slots[0], actor, usage_ctx=ctx, request=req,
                                   task_id=TASK, result_queue=received)
    assert entered.is_set() and received.get_nowait().operation_id == actor.operation_id
    assert len(tr.plan_review_wave(state(tmp_path), A)["historical_supplements"]) == 1


def test_supervisor_event_is_explicit_recovery_and_get_projection_is_readonly(tmp_path):
    from supervisor.cognitive_operations import _handle_review_late_result
    a, req, slots = wave(tmp_path, count=1)
    wave(tmp_path, B, cycle=2, count=0, closed=True)
    actor, _ = complete(tmp_path, a, req, slots[0])
    path = tr.task_result_path(tmp_path, TASK)
    before = path.read_bytes()
    state(tmp_path)
    assert path.read_bytes() == before
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, RUNNING={}, bridge=SimpleNamespace(push_log=lambda value: None))
    _handle_review_late_result({"type": "review_late_result", "surface": "plan_review", "task_id": TASK,
                               "operation_id": actor.operation_id}, ctx)
    assert len(tr.plan_review_wave(state(tmp_path), A)["historical_supplements"]) == 1


def test_compaction_and_explicit_replacement_keep_historical_source_refs(tmp_path):
    a, req, slots = wave(tmp_path, count=1)
    wave(tmp_path, B, cycle=2, count=0, closed=True)
    complete(tmp_path, a, req, slots[0])
    collect.attach_historical_results(tmp_path, TASK, fingerprint=A)
    recorded = tr.plan_review_wave(state(tmp_path), A)
    assert tr._compact_plan_review_wave(recorded)["historical_supplements"] == recorded["historical_supplements"]
    tr.record_plan_review_wave(tmp_path, TASK, {**a, "paid": True, "custody_pending": False})
    assert tr.plan_review_wave(state(tmp_path), A)["historical_supplements"] == recorded["historical_supplements"]
    assert state(tmp_path)["cycles_paid"] == 1


from tests.test_plan_review_engine import harness, _call, _control, _state, DECK_SPEC  # noqa: E402,F401


def test_real_substrate_late_a_after_b_never_reaggregates_a(harness, monkeypatch):  # noqa: F811
    from tests.test_plan_review_event_route import _install_real_substrate, _wait_until
    executor = _install_real_substrate(monkeypatch)
    ctx = harness.make_ctx()
    revised = {**DECK_SPEC, "in_scope": ["a 6-slide deck"]}
    try:
        _call(ctx)
        old = copy.deepcopy(_state(harness)["waves"][-1])
        _call(ctx, spec=revised)
        current_fp = _state(harness)["current_attempt"]["fingerprint"]
        assert current_fp != old["request_fingerprint"]
        # The ordinary pre-supersede collect still owns A until B is current.
        old = tr.plan_review_wave(_state(harness), old["request_fingerprint"])
        executor.release.set()
        def complete_history():
            stored = tr.plan_review_wave(_state(harness), old["request_fingerprint"])
            return len(stored.get("historical_supplements") or []) == 3
        assert _wait_until(complete_history)
        after = _state(harness)
        assert after["current_attempt"]["fingerprint"] == current_fp
        assert original_authority(tr.plan_review_wave(after, old["request_fingerprint"])) == original_authority(old)
        assert executor.execute_calls == 6
        assert _control(_call(ctx, spec=revised)) == {"outcome": "GREEN", "closed": True}
        assert _state(harness)["cycles_paid"] == 2 and executor.execute_calls == 6
    finally:
        executor.release.set()


def test_concurrent_new_current_is_not_restored_by_history_writer(tmp_path, monkeypatch):
    a, req, slots = wave(tmp_path, count=1)
    wave(tmp_path, B, cycle=2, count=0, closed=True)
    complete(tmp_path, a, req, slots[0])
    original = tr._update_plan_review_state
    def with_new_current(root, task_id, mutator):
        monkeypatch.setattr(tr, "_update_plan_review_state", original)
        tr.record_plan_review_attempt(root, task_id, fingerprint=C)
        return original(root, task_id, mutator)
    monkeypatch.setattr(tr, "_update_plan_review_state", with_new_current)
    assert collect.attach_historical_results(tmp_path, TASK, fingerprint=A) == 1
    assert state(tmp_path)["current_attempt"]["fingerprint"] == C


def test_unreadable_history_does_not_fail_new_wave_publication(tmp_path):
    a, _, _ = wave(tmp_path, count=1)
    from ouroboros.artifacts import task_artifact_dir_path
    ref = a["wave_artifact"]
    (task_artifact_dir_path(tmp_path, TASK, create=False) / ref["path"]).unlink()
    b = {**a, "request_fingerprint": B, "cycle_index": 2, "retry_key": f"plan_review:{B}:2",
         "aggregate": "GREEN", "closed": True, "actors": [], "custody_pending": False, "paid": False}
    actual = artifacts.record_exact_wave(tmp_path, TASK, b, b, need_evidence_seen=[], page_size=10)
    assert actual["closed"] and state(tmp_path)["current_attempt"]["fingerprint"] == B


def test_failed_historical_write_never_strands_worker_settlement(tmp_path, monkeypatch):
    from ouroboros import review_custody as custody
    a, req, slots = wave(tmp_path, count=1)
    actor, _ = complete(tmp_path, a, req, slots[0])
    entry = custody.ActiveReviewAttempt("isolated-failed-write", actor.operation_id, released_early=True)
    received = queue.Queue()
    def failed(*args, **kwargs):
        raise RuntimeError("fixture unavailable history writer")
    monkeypatch.setattr(collect, "attach_historical_results", failed)
    ctx = SimpleNamespace(drive_root=tmp_path, task_id=TASK, event_queue=None)
    custody._settle_review_attempt(entry, slots[0], actor, usage_ctx=ctx, request=req,
                                   task_id=TASK, result_queue=received)
    assert received.get_nowait().operation_id == actor.operation_id
    assert entry.event.is_set()
