"""Root result admission and receptive waiting through real loop/tool consumers."""
from __future__ import annotations

import copy
import json

import pytest

from ouroboros import loop
from ouroboros.artifacts import read_actor_source_bytes, store_task_artifact_bytes
from ouroboros.tools.review import _handle_task_acceptance_review
from tests.test_acceptance_async_loop import ANSWER, call, keep
from tests.test_acceptance_async_loop import full_loop as _full_loop

full_loop = _full_loop
pytestmark = pytest.mark.serial


def nominate():
    return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "nominate")]}


@pytest.mark.parametrize("mode", ["auto", "required", "off"])
@pytest.mark.parametrize("full_loop", [False], indirect=True)
def test_expected_output_and_queue_do_not_buy_a_panel(full_loop, monkeypatch, mode):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", mode)
    f.ctx.task_contract.update(expected_output="A requested file", success_criteria=["the file exists"],
                               acceptance_claims=[{"claim": "file"}])
    calls = []
    def main(*_a, **_k):
        calls.append(True)
        assert len(calls) == 1, "an unproduced requested output bought an author round"
        return {"content": ANSWER}, 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    answer, _, trace = f.run()
    assert answer == ANSWER
    assert not f.review_sends and not f.waits
    assert not trace.get("acceptance_material", {}).get("produced")


def test_auto_does_not_review_an_actual_artifact_without_nomination(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    store_task_artifact_bytes(f.ctx.drive_root, f.ctx.task_id, "report.txt", b"Actual produced report")
    monkeypatch.setattr(loop, "call_llm_with_retry", lambda *_a, **_k: ({"content": ANSWER}, 0.0))
    answer, _, trace = f.run()
    assert answer == ANSWER and not trace["acceptance_material"]["produced"]
    assert not f.review_sends


@pytest.mark.parametrize("runtime,enforcement", [
    ("cyber_pro", "blocking"), ("cyber_pro", "advisory"),
    ("advanced", "advisory"), ("advanced", "blocking"),
])
@pytest.mark.parametrize("choice", [None, "wait", "finish"])
def test_typed_first_nomination_and_same_paid_wait(full_loop, monkeypatch, runtime, enforcement, choice):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", runtime)
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    expected_wait = choice != "finish" or (runtime != "cyber_pro" and enforcement == "blocking")
    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 5, f.progress
        if f.model_step == 1:
            return nominate(), 0.0
        if f.release.is_set():
            assert "Independent review" in str(messages)
        return keep(f, **({"pending_review": choice} if choice else {})), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    answer, _, trace = f.run()
    assert answer == ANSWER
    assert len(f.review_sends) == 1
    assert bool(f.waits) == expected_wait
    assert f.model_step == (3 if expected_wait else 2), 'quiet waiting must not poll Main'
    if expected_wait:
        assert f.release.is_set()
        assert any("Independent review" in str(messages) for messages in f.model_inputs[1:])
    else:
        assert not f.release.is_set()
        assert trace["acceptance_decision"]["review_pending"] is True
        assert trace["acceptance_decision"]["reason"] == "author_finish"


def test_cyber_plain_finished_prose_does_not_release_wait(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 5, f.progress
        if f.model_step == 1:
            return nominate(), 0.0
        if f.model_step == 2:
            return {"content": "I am finished."}, 0.0
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    answer, _, _trace = f.run()
    assert answer == ANSWER and f.waits and len(f.review_sends) == 1
    assert any("Independent review" in str(messages) for messages in f.model_inputs)


def test_owner_followup_keeps_the_paid_wait(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    original_park = f.ctx.owner_wait_callback
    wakes = []
    def park(ctx, checkpoint):
        wakes.append(copy.deepcopy(checkpoint))
        if len(wakes) == 1:
            f.incoming.put("Please retain the report and wait for that review.")
        else:
            original_park(ctx, checkpoint)
    f.ctx.owner_wait_callback = park
    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 6, f.progress
        if f.model_step == 1:
            return nominate(), 0.0
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    answer, _, _trace = f.run()
    assert answer == ANSWER and len(f.review_sends) == 1
    assert len(wakes) >= 2
    assert any("Please retain the report" in str(messages) for messages in f.model_inputs)
    assert any("Independent review" in str(messages) for messages in f.model_inputs)


@pytest.mark.parametrize("full_loop", [False], indirect=True)
def test_required_selected_answer_retained_before_real_dispatch(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    f.ctx.task_metadata["answer_protocol"] = "final_answer_line"
    candidate = "The calculation is complete.\nFINAL ANSWER: 42"
    def main(_llm, messages, *_a, **_k):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 4, f.progress
        if f.model_step == 1:
            return {"content": candidate}, 0.0
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    answer, _, trace = f.run()
    assert answer == candidate and len(f.review_requests) == 1
    selected = trace["acceptance_material"]["selected_answer"]
    assert selected["text"] == "42"
    assert read_actor_source_bytes(f.ctx.drive_root, f.ctx.task_id, selected["source_ref"]) == b"42"
    assert f.review_requests[0].evidence["acceptance_material"]["selected_answer"] == selected
    assert f.review_snapshots[0]["acceptance_material"]["selected_answer"] == selected


def test_off_manual_root_collects_without_new_dispatch(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    trace = {"review_runs": [{"authority": "host_root", "binding_hash": "paid", "actors": []}]}
    f.ctx._execution_trace = trace
    observed = []
    monkeypatch.setattr("ouroboros.review_dispatch.reconcile_pending_acceptance_runs",
                        lambda actual, **_kw: observed.append(actual))
    result = json.loads(_handle_task_acceptance_review(f.ctx, claim=ANSWER))
    assert result["reason"] == "review_mode_off" and observed == [trace]
    assert not f.review_sends


def test_off_does_not_suppress_a_child_critic(tmp_path, monkeypatch):
    from tests.test_task_acceptance_review_evidence_coercion import _ctx, _stub_review
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    handler, captured = _stub_review(monkeypatch)
    result = handler(_ctx(tmp_path), claim=ANSWER)
    assert "solved" in result and captured["agent_evidence"]["acceptance_request"]["claim"] == ANSWER


@pytest.mark.parametrize("runtime", ["advanced", "cyber_pro"])
@pytest.mark.parametrize("order", ["ready", "pending"])
def test_ready_or_pending_feedback_is_read_before_reusing_paid_nomination(full_loop, monkeypatch, runtime, order):
    from tests.test_loop_acceptance_gate import _order_acceptance_feedback
    f = full_loop
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", runtime)
    _order_acceptance_feedback(f, monkeypatch, ANSWER, order)
    repeated = []
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 5, f.progress
        if f.model_step == 1:
            return nominate(), 0.0
        if any(row.get('review_feedback') and (
                'Review verdict: PASS' in str(row.get('content'))
                or '- acceptance-one: PASS' in str(row.get('content'))) for row in messages) and not repeated:
            repeated.append(f.model_step)
            return nominate(), 0.0
        return keep(f), 0.0
    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    answer, _, trace = f.run()
    assert answer == ANSWER and repeated
    assert len(f.review_sends) == 1
    assert len([r for r in trace['review_runs'] if r.get('authority') == 'host_root']) == 1
    assert trace['acceptance_decision']['status'] == 'accepted'


@pytest.mark.parametrize('early_finish', [False, True])
def test_partial_feedback_does_not_waive_wait_without_explicit_choice(full_loop, monkeypatch, early_finish):
    import threading
    from ouroboros.review_records import ReviewSlot
    f = full_loop
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'cyber_pro')
    f.slots.append(ReviewSlot(slot_id='acceptance-two', model='fixture/reviewer', effort='high', timeout_sec=30))
    f.slots.append(ReviewSlot(slot_id='acceptance-three', model='fixture/reviewer', effort='high', timeout_sec=30))
    first, second = threading.Event(), threading.Event()
    f.slot_releases.update({'acceptance-one': first, 'acceptance-two': first, 'acceptance-three': second})
    wakes, partial_inputs = [], []
    def park(ctx, checkpoint):
        wakes.append(checkpoint)
        first.set()
        if len(wakes) == 1:
            with f.condition:
                assert f.condition.wait_for(lambda: f.settled_count == 2, timeout=10)
            f.incoming.put('Status while one critic is still working?')
        else:
            second.set()
            f.park(ctx, checkpoint)
    f.ctx.owner_wait_callback = park
    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        assert f.model_step <= 7, f.progress
        if f.model_step == 1:
            return nominate(), 0.0
        if first.is_set() and not second.is_set():
            assert 'Status while one critic' in str(messages)
            assert 'Independent review' in str(messages)
            partial_inputs.append(f.model_step)
            return keep(f, **({'pending_review': 'finish'} if early_finish else {})), 0.0
        return keep(f), 0.0
    monkeypatch.setattr(loop, 'call_llm_with_retry', main)
    answer, _, trace = f.run()
    assert answer == ANSWER and partial_inputs
    assert len(f.review_sends) == 3 and len(trace['review_runs']) == 1
    assert second.is_set() is (not early_finish)
    if early_finish:
        assert trace['acceptance_decision']['review_pending'] is True
        assert trace['acceptance_decision']['reason'] == 'author_finish'
        assert len(wakes) == 1
    else:
        assert len(wakes) == 2
        assert trace['acceptance_decision']['status'] == 'accepted'


@pytest.mark.parametrize('control', ['stop', 'panic'])
def test_native_paid_wait_retains_operation_when_owner_controls_end_wait(tmp_path, monkeypatch, control):
    from tests.test_native_owner_wait import native_context
    from ouroboros.owner_wait import wait_after_tools
    from ouroboros.task_results import load_task_result
    from ouroboros.budget_pause import _hold_control_reason
    from ouroboros.cancel_intents import request_cancel
    ctx = native_context(tmp_path)
    trace = {'review_runs': [{'authority': 'host_root', 'binding_hash': 'already-paid',
                             'actors': [{'operation_id': 'same-operation', 'operation_state': 'in_flight'}]}]}
    before = copy.deepcopy(trace)
    # This direct actor binds the existing owner control reader, with real
    # disposable-root cancel/flag facts. No live process or queue is touched.
    ctx.model_wait_context.owner_control = lambda: _hold_control_reason(ctx)
    if control == 'stop':
        request_cancel(tmp_path, ctx.task_id, reason='owner stopped', source='owner', requested_by='owner')
    else:
        (tmp_path / 'state').mkdir(exist_ok=True)
        (tmp_path / 'state' / 'panic_stop.flag').write_text('panic')
    monkeypatch.setattr('ouroboros.owner_wait.time.sleep', lambda *_: pytest.fail('control must precede sleep'))
    wait_after_tools(ctx, [], trace, {}, 1, [], set(), review_binding='already-paid')
    assert trace == before
    wait = load_task_result(tmp_path, ctx.task_id)['owner_wait']
    assert wait['review_binding'] == 'already-paid'
    assert wait['resume_reason'] == ('control:cancelled' if control == 'stop' else 'control:panic')


@pytest.mark.parametrize("choice", ["wait", "finish", None])
def test_cold_resume_keeps_the_current_choice_and_never_a_stale_finish(tmp_path, monkeypatch, choice):
    """The validated wait/finish choice rides the one continuation serializer through a JSON
    (cold) checkpoint and a fresh restore after the hold cleared its selection; a new
    nomination, a new turn and a new execution's working restore start from waiting."""
    from types import SimpleNamespace

    from ouroboros import loop_acceptance_review
    from ouroboros.acceptance_settlement import acceptance_wait_chosen
    from ouroboros.loop_delivery import completion_observation, consume_completion_request
    from ouroboros.owner_wait import continuation_state, restore_continuation_state
    from ouroboros.tools.control_runtime import stage_completion_request
    from tests.test_delivery_control_lineage import _start_control_episode

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "cyber_pro")
    loop_module, registry, ctx, trace, candidate = _start_control_episode(tmp_path)
    loop_module._arm_delivery_control(registry, ctx, trace)
    registry._ctx._completion_observation = completion_observation(registry._ctx, trace)
    request = {"action": "finish", "answer_sha256": candidate.content_sha256, **({"pending_review": choice} if choice else {})}
    assert json.loads(stage_completion_request(registry._ctx, request))["status"] == "completion_requested"
    assert consume_completion_request(registry, ctx, trace)
    expected = choice != "finish"
    assert acceptance_wait_chosen(registry._ctx) is expected
    registry._ctx._completion_selected = None  # the real hold clears the consumed selection
    state = json.loads(json.dumps(continuation_state(registry._ctx, ctx.messages, trace, {}, 2, [], set())))

    def restored(saved):
        tools = SimpleNamespace(_ctx=SimpleNamespace())
        restore_continuation_state(tools, saved, [], {}, {}, set())
        return tools

    cold = restored(state)
    assert acceptance_wait_chosen(cold._ctx) is expected, "a cold resume never turns an explicit finish into wait"
    cold._ctx._acceptance_request_pending = {"subject": candidate.full_text}
    monkeypatch.setattr(loop_acceptance_review, "_loop", lambda: SimpleNamespace(
        _replace_delivery_candidate=lambda *_a, **_k: None, _no_tool_final_answer=lambda *_a, **_k: None,
        _arm_delivery_control=lambda *_a, **_k: None))
    loop_acceptance_review.advance_explicit_acceptance(cold, SimpleNamespace(), {}, None, set(), lambda *_a, **_k: None)
    assert acceptance_wait_chosen(cold._ctx) is True, "a new nomination starts a fresh choice"
    turn = restored(state)
    loop._reset_turn_state(turn._ctx)
    assert acceptance_wait_chosen(turn._ctx) is True, "a new turn inherits no stale finish"
    retry = restored({**state, "acceptance": {}})  # resume_from_working's new-execution projection
    assert acceptance_wait_chosen(retry._ctx) is True
