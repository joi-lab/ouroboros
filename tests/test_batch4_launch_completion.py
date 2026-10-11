"""Discriminators for Pause submission and producer-owned completion facts."""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from tests._budget_pause_exact_helpers import _install_queue
from tests.test_batch4_repair_compositions import _running

pytestmark = pytest.mark.serial


@pytest.mark.parametrize('asynchronous', [False, True])
def test_pause_after_accounting_claim_prevents_sender_entry(tmp_path, monkeypatch, asynchronous):
    from ouroboros import usage_accounting as ua
    from ouroboros.owner_pause import install_fence
    from ouroboros.task_results import write_task_result
    from ouroboros.llm_attempt import _deadline_checked_send

    monkeypatch.setenv('OUROBOROS_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TOTAL_BUDGET', '1000')
    write_task_result(tmp_path, 'root', 'running')
    claimed, sent = [], []
    original = ua.mark_dispatched
    def claim(*args, **kwargs):
        original(*args, **kwargs)
        claimed.append(True)
        install_fence(tmp_path, 'root', request_id='after-claim')
    monkeypatch.setattr(ua, 'mark_dispatched', claim)
    async def async_send():
        sent.append(True)
        return 'answer'
    send, prepare = _deadline_checked_send(async_send if asynchronous else lambda: sent.append(True), None)
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id='root', root_task_id='root')):
        request = ua.AttemptRequest(model='m', provider='test', reservation_usd=.01)
        with pytest.raises(Exception, match='owner_pause'):
            if asynchronous:
                asyncio.run(ua.execute_physical_attempt_async(request, send, before_dispatch=prepare))
            else:
                ua.execute_physical_attempt(request, send, before_dispatch=prepare)
    assert claimed == [True] and not sent
    assert ua.read_usage_records(tmp_path, final_only=True)[-1]['state'] == 'released'


def test_sleep_refusal_completes_without_hiding_real_blocker(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import write_task_result, load_task_result
    from supervisor.continuation_admission import conflicting_writers

    q, _, workers = _install_queue(tmp_path, monkeypatch)
    _running(tmp_path, workers)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    ctx = registry._ctx
    ctx.task_id = ctx.root_task_id = 'root'
    ctx.task_attempt = 1
    ctx.owner_wait_callback = lambda *_a: 'unknown'
    write_task_result(tmp_path, 'root', 'running', launch_handoffs={'real-blocker': {'state': 'claimed'}})
    refusal = registry.execute_result('await_messages', {'mode': 'cold', 'wake_after_sec': 3600})
    assert 'member_custody' in refusal.text
    assert list(load_task_result(tmp_path, 'root')['launch_handoffs']) == ['real-blocker']
    write_task_result(tmp_path, 'root', 'running', launch_handoffs={})
    retry = registry.execute_result('await_messages', {'mode': 'cold', 'wake_after_sec': 3600})
    assert 'sleep_armed' in retry.text
    assert not load_task_result(tmp_path, 'root').get('launch_handoffs')
    assert not any(b['kind'] == 'tool_handoff' for b in conflicting_writers(q, 'root'))


def test_foreground_nonzero_completion_is_not_unresolved_effect(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import load_task_result
    from supervisor.continuation_admission import conflicting_writers

    q, _, workers = _install_queue(tmp_path, monkeypatch)
    _running(tmp_path, workers)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = registry._ctx.root_task_id = 'root'
    result = registry.execute_result('run_command', {'cmd': [sys.executable, '-c', 'raise SystemExit(3)']})
    assert result.code == 'SHELL_EXIT_ERROR', result
    assert result.meta['exit_code'] == 3
    assert not load_task_result(tmp_path, 'root').get('launch_handoffs')
    assert not any(b['kind'] == 'tool_handoff' for b in conflicting_writers(q, 'root'))


@pytest.mark.parametrize('service', [False, True])
def test_pause_during_process_preparation_prevents_process_start(tmp_path, monkeypatch, service):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools import shell
    from ouroboros.owner_pause import install_fence
    from ouroboros.task_results import load_task_result

    _, _, workers = _install_queue(tmp_path, monkeypatch)
    _running(tmp_path, workers)
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = registry._ctx.root_task_id = 'root'
    marker = tmp_path / 'effect'
    def preparation(*_a, **_k):
        install_fence(tmp_path, 'root', request_id='during-preparation')
        return {}
    monkeypatch.setattr(shell, '_snapshot_declared_outputs', preparation)
    args = {'cmd': [sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']}
    if service:
        args['name'] = 'held'
    result = registry.execute_result('start_service' if service else 'run_command', args)
    assert not marker.exists(), result
    assert result.code == 'OWNER_PAUSE_NOT_STARTED', result
    assert not load_task_result(tmp_path, 'root').get('launch_handoffs')


@pytest.mark.parametrize('retirement', ['exact', 'other_consumer', 'other_attempt', 'missing', 'write_failed'])
def test_exact_consumer_retirement_releases_writer_not_cash(tmp_path, monkeypatch, retirement):
    import os
    from ouroboros import usage_accounting as ua, model_wait
    from ouroboros.task_results import write_task_result, load_task_result
    from supervisor.continuation_admission import conflicting_writers

    q, _, _workers = _install_queue(tmp_path, monkeypatch)
    write_task_result(tmp_path, 'root', 'running', root_task_id='root')
    monkeypatch.setenv('TOTAL_BUDGET', '1000')
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id='root', root_task_id='root')):
        with model_wait.task_model_wait_scope(task={'id': 'root', '_attempt': 7}, drive_root=tmp_path,
                                               event_queue=None, worker_slot_held=True) as owner:
            def send():
                raise RuntimeError('unknown send')
            with pytest.raises(RuntimeError, match='unknown send'):
                ua.execute_physical_attempt(ua.AttemptRequest(model='m', provider='test', reservation_usd=.2), send)
            before = ua.read_usage_records(tmp_path, final_only=True)[-1]
            assert before['state'] == 'unresolved'
            assert before['local_answer_owner_pid'] == os.getpid()
            assert before['local_answer_consumer_id'] == owner.answer_consumer_id
            assert any(b['kind'] == 'model_handoff' for b in conflicting_writers(q, 'root'))
            # A terminal file / empty RUNNING is not retirement.
            write_task_result(tmp_path, 'root', 'failed', reason_code='provider_unavailable')
            assert any(b['kind'] == 'model_handoff' for b in conflicting_writers(q, 'root'))
            if retirement == 'write_failed':
                monkeypatch.setattr(model_wait, 'update_json_locked', lambda *_a, **_k: (_ for _ in ()).throw(OSError('disk')))
        row = load_task_result(tmp_path, 'root')
        retired = row.get('retired_model_consumers', {})
        if retirement in {'other_consumer', 'other_attempt', 'missing'}:
            fact = retired.pop(owner.answer_consumer_id)
            if retirement == 'other_consumer':
                retired['unrelated'] = fact
            elif retirement == 'other_attempt':
                retired[owner.answer_consumer_id] = {**fact, 'task_attempt': 6}
            write_task_result(tmp_path, 'root', 'failed', retired_model_consumers=retired)
        blockers = conflicting_writers(q, 'root')
        assert any(b['kind'] == 'model_handoff' for b in blockers) == (retirement != 'exact')
        # Neither the still-alive PID nor platform liveness API decides this.
        assert ua.read_usage_records(tmp_path, final_only=True)[-1] == before
        assert ua.usage_projection(tmp_path, root_task_id='root')['cost_final'] is False


def test_bound_continue_offer_retries_exact_admission_after_snapshot_failure(tmp_path, monkeypatch):
    from tests.test_owner_continue import _interrupted, NONCE
    from ouroboros.owner_continue import continuation_offer
    from ouroboros.task_results import load_task_result
    from supervisor.continuation_admission import admit_continuation

    q, _, workers = _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path)
    persist = q.persist_queue_snapshot
    monkeypatch.setattr(q, 'persist_queue_snapshot', lambda **_k: False)
    failed = admit_continuation('pred-1', action_nonce=NONCE)
    assert not failed['ok'] and not workers.PENDING
    offer = continuation_offer(load_task_result(tmp_path, 'pred-1'), 'pred-1')
    assert offer['state'] == 'bound' and offer['action_nonce'] == NONCE
    monkeypatch.setattr(q, 'persist_queue_snapshot', persist)
    ack = admit_continuation('pred-1', action_nonce=offer['action_nonce'])
    assert ack['ok'] and ack['successor_task_id'] == failed['successor_task_id']
    assert len(workers.PENDING) == 1
    admitted = continuation_offer(load_task_result(tmp_path, 'pred-1'), 'pred-1')
    assert admitted['state'] == 'admitted' and 'action_nonce' not in admitted


@pytest.mark.parametrize('reason', ['worker_crash_signal', 'worker_crash_retry_exhausted'])
def test_worker_crash_is_continuable_but_owner_stop_stays_final(tmp_path, monkeypatch, reason):
    from tests.test_owner_continue import _interrupted
    from ouroboros.owner_continue import continuation_eligibility
    from ouroboros.task_results import load_task_result, write_task_result

    _install_queue(tmp_path, monkeypatch)
    _interrupted(tmp_path, reason_code=reason)
    assert continuation_eligibility(load_task_result(tmp_path, 'pred-1'), 'pred-1')['eligible']
    write_task_result(tmp_path, 'pred-1', 'failed', cancel_origin={'source': 'owner_stop'})
    assert not continuation_eligibility(load_task_result(tmp_path, 'pred-1'), 'pred-1')['eligible']


@pytest.mark.parametrize("existing", [False, True])
def test_standalone_registry_does_not_create_inadmissible_authority(tmp_path, existing):
    import json
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import task_result_path
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = registry._ctx.root_task_id = "standalone"
    path = task_result_path(tmp_path, "standalone")
    if existing:
        path.write_text(json.dumps({"task_id": "standalone", "owner_pause": {"state": "requested"}}))
        original = path.read_bytes()
    for _ in range(2):
        result = registry.execute_result("knowledge_read", {"not_an_argument": 1})
        assert result.code == ("OWNER_PAUSE_NOT_STARTED" if existing else "TOOL_ARG_ERROR")
    if existing:
        assert path.read_bytes() == original
    else:
        assert not path.exists()


def test_bound_tool_authority_loss_is_not_a_standalone_operation(tmp_path):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import task_result_path, write_task_result
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = 'managed'
    registry._ctx.task_lifecycle_bound = True
    for existed in (False, True):
        if existed:
            write_task_result(tmp_path, 'managed', 'running')
            task_result_path(tmp_path, 'managed').unlink()
        result = registry.execute_result('knowledge_read', {'not_an_argument': 1})
        assert result.code == 'OWNER_PAUSE_NOT_STARTED'
        assert not task_result_path(tmp_path, 'managed').exists()


def test_child_tool_reads_canonical_metadata_root_fence(tmp_path):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import write_task_result, task_result_path
    from ouroboros.owner_pause import install_fence
    canonical, fork = tmp_path / 'canonical', tmp_path / 'fork'
    write_task_result(canonical, 'root', 'running')
    write_task_result(canonical, 'child', 'running', root_task_id='root')
    install_fence(canonical, 'root', request_id='owner-pause')
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=fork)
    registry._ctx.task_id = 'child'
    registry._ctx.task_lifecycle_bound = True
    registry._ctx.task_metadata = {'root_task_id': 'root', 'budget_drive_root': str(canonical)}
    result = registry.execute_result('knowledge_read', {'not_an_argument': 1})
    assert result.meta['control_reason'] == 'owner_pause'
    assert not task_result_path(fork, 'child').exists()


@pytest.mark.parametrize('projection', ['model', 'owner', 'budget'])
def test_projection_writers_cannot_manufacture_lifecycle_authority(tmp_path, projection):
    from ouroboros import model_wait, owner_wait, budget_pause
    from ouroboros.task_results import task_result_path
    call = {'model': lambda: model_wait.mutate_wait(tmp_path, 'missing', 'wait', lambda _: {'state': 'waiting'}),
            'owner': lambda: owner_wait.set_owner_wait(tmp_path, 'missing', {'state': 'waiting'}),
            'budget': lambda: budget_pause.set_budget_pause(tmp_path, 'missing', {'state': 'pausing'})}[projection]
    with pytest.raises(ValueError, match="lifecycle owner"):
        call()
    assert not task_result_path(tmp_path, 'missing').exists()


def test_retired_answer_consumer_cannot_start_a_late_tool(tmp_path):
    from ouroboros.model_wait import TaskModelWait, operation_wait_scope
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.task_results import write_task_result
    write_task_result(tmp_path, 'task', 'running')
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = 'task'
    owner = TaskModelWait(task={'id': 'task'}, drive_root=tmp_path, event_queue=None, worker_slot_held=False)
    owner.close()
    with operation_wait_scope(owner):
        result = registry.execute_result('knowledge_read', {'not_an_argument': 1})
    assert result.meta['control_reason'] == 'operation_already_returned'


def test_authority_lock_unavailability_is_unsent_infrastructure_not_owner_pause(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros import owner_pause, llm_attempt, loop_round_limits, model_wait
    from contextlib import contextmanager
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = 'task'
    @contextmanager
    def unavailable(*_a, **_k):
        raise owner_pause.OwnerPauseRefused('owner_launch_authority_unavailable')
        yield
    monkeypatch.setattr(owner_pause, 'launch_lock', unavailable)
    result = registry.execute_result('knowledge_read', {'not_an_argument': 1})
    assert result.code == 'OWNER_LAUNCH_AUTHORITY_UNAVAILABLE'
    assert result.status == 'unavailable' and 'owner paused' not in result.text
    checks = []
    monkeypatch.setattr(llm_attempt, 'require_physical_dispatch_window', lambda: checks.append(True))
    @contextmanager
    def recovered(*_a, **_k):
        if len(checks) == 1:
            raise owner_pause.OwnerPauseRefused('owner_launch_authority_unavailable')
        yield
    monkeypatch.setattr(owner_pause, 'launch_admission', recovered)
    error = model_wait.ModelWaitInterrupted('owner_launch_authority_unavailable')
    assert loop_round_limits._handle_model_wait_control(SimpleNamespace(), error) is None
    assert len(checks) == 2


def test_enqueue_snapshot_and_first_reservation_keep_original_billing_without_lifecycle_upsert(tmp_path, monkeypatch):
    from ouroboros import usage_accounting as ua
    from ouroboros.usage_admission import task_billing_fields
    from ouroboros.task_results import load_task_result
    q, _, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setenv('OUROBOROS_PER_TASK_COST_USD', '7')
    row = q.enqueue_task({'id': 'initial-root', 'type': 'task', 'root_task_id': 'initial-root',
                          'text': 'Inspect the saved work', 'chat_id': 0, 'description': 'Inspect the saved work'})
    assert not row.get('_admission_blocked')
    original = dict(row['metadata']['billing_group'])
    assert original['billing_group_limit_usd'] == 7
    assert load_task_result(tmp_path, 'initial-root') is None
    assert q.persist_queue_snapshot(reason='fixture-admission') is True
    q.PENDING.clear()
    assert q.restore_pending_from_snapshot() == 1
    restored = q.PENDING[0]
    monkeypatch.setenv('OUROBOROS_PER_TASK_COST_USD', '70')
    binding = task_billing_fields(restored, 'initial-root', 70, tmp_path, pin_initial=True)
    assert {key: binding[key] for key in original} == original
    assert load_task_result(tmp_path, 'initial-root') is None
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id='initial-root', root_task_id='initial-root', **binding)):
        reservation = ua.reserve_attempt(ua.AttemptRequest(model='fixture', provider='fixture', reservation_usd=.1))
    pinned = ua.read_usage_records(tmp_path, final_only=True)[-1]
    assert {key: pinned[key] for key in original} == original
    ua.release_attempt(reservation, 'fixture did not submit')


def test_focused_node_control_consumers():
    import pathlib
    import shutil
    import subprocess
    root = pathlib.Path(__file__).resolve().parents[1]
    node = shutil.which('node')
    assert node, 'focused frontend verification requires the provided Node runtime'
    result = subprocess.run([node, '--test', 'web/tests/task_continue.test.js',
                             'web/tests/chat_activity_block.test.js', 'web/tests/activity_schedule_time.test.js'],
                            cwd=root, capture_output=True, text=True, timeout=60)
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr


def test_cleanup_lock_failure_retains_custody_without_claiming_no_send(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from ouroboros import owner_pause
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools.tool_result import ToolResult
    from ouroboros.task_results import write_task_result, load_task_result
    write_task_result(tmp_path, 'task', 'running')
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = 'task'
    entered = []
    @contextmanager
    def unavailable(*_a, **_k):
        raise owner_pause.OwnerPauseRefused('owner_launch_authority_unavailable')
        yield
    def operation(_name, _args, handoff=None):
        owner_pause.run_operation(registry._ctx, entered.append, True)
        handoff['builtin_returned'] = True
        monkeypatch.setattr(owner_pause, 'launch_lock', unavailable)
        return ToolResult(status='ok', code='OK', text='completed', meta={'operation_outcome': 'completed'})
    monkeypatch.setattr(registry, '_execute_admitted_text', operation)
    result = registry.execute_result('knowledge_read', {})
    assert entered == [True] and result.text == 'completed'
    assert load_task_result(tmp_path, 'task')['launch_handoffs']


def test_stop_conflict_refusal_settles_only_its_tool_claim(tmp_path):
    from ouroboros.cancel_intents import request_cancel, active_intent
    from ouroboros.task_results import write_task_result, load_task_result
    from ouroboros.tools.registry import ToolRegistry
    write_task_result(tmp_path, 'parent', 'running')
    write_task_result(tmp_path, 'child', 'running', parent_task_id='parent', root_task_id='parent')
    request_cancel(tmp_path, 'child', source='owner', stop_action_id='retained-action')
    original = active_intent(tmp_path, 'child')
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id = 'parent'
    result = registry.execute_result('cancel_task', {'task_id': 'child', 'stop_action_id': 'retained-action'})
    assert result.status == 'blocked' and result.code == 'STOP_ACTION_CONFLICT'
    assert result.meta['operation_outcome'] == 'completed_no_effect'
    assert not load_task_result(tmp_path, 'parent').get('launch_handoffs')
    assert active_intent(tmp_path, 'child') == original
