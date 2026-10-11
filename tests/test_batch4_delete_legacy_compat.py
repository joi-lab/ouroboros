"""Deleting a continuation (owner 1A) and published rows after the update (owner 2A).

1A: a deleted schedule that continues a task stops firing at once, stays visibly
``delete_pending`` while that task can still need its row (Stop/Restart release,
``scheduled_start`` binding), and disappears once the task settles; Restore never
withdraws the delete implicitly. Generic Restore is an explicit cancellation;
exact hold release keeps the intent. Independent schedules keep published Delete.

2A: rows the published version wrote carry no relationship. An owner-door row is
independent; a model follow-up keeps its previous launch and own-root expense
rules, relationship honestly ``unknown``, until an actual Stop/manual Restart of
its origin holds it. Driven through the real tick, receipt, worker put and ledger.
"""
import copy
from types import SimpleNamespace

import pytest

from ouroboros import usage_accounting as ua
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.tools.followup import _handle_schedule_followup, _manage_schedules
from ouroboros.usage_admission import task_billing_fields
from supervisor import queue, queue_schedules as schedules
from supervisor.worker_assignment import _claim_worker_launch
from tests.test_g1_followup_policy import (  # noqa: F401
    world, register, row, restore, stop, ORIGIN, BINDING, DEADLINE, _assignment_world,
)

pytestmark = pytest.mark.serial
PAST = '2000-01-01T00:00:00+00:00'


def _worker(sent):
    return SimpleNamespace(in_q=SimpleNamespace(put=lambda task: sent.append(copy.deepcopy(task))))


def _delete(root, sid):
    return queue.mutate_scheduled_task('delete', sid, reason='Owner deleted it', actor='owner:test', drive_root=root)


def _views(root, sid):
    from supervisor.followup_policy import observed_store
    data = observed_store(root, schedules.load_schedule_store(root))
    activity = next(r for r in schedules.schedule_activity_projection(data)['tasks'] if r['id'] == sid)
    tool = next(r for r in schedules.schedule_tool_projection(data)['tasks'] if r['id'] == sid)
    return activity, tool


def _spend(root, task, limit=5.0):
    """Normal monetary admission of the fired task: its own ledger scope pays."""
    fields = task_billing_fields(task, task['id'], limit, root, pin_initial=True)
    with ua.usage_scope(ua.UsageScope(drive_root=root, task_id=task['id'], root_task_id=task['id'],
                                      source='test', **fields)):
        reservation = ua.reserve_attempt(ua.AttemptRequest(model='openai/gpt-5.2', provider='openai',
                                                           reservation_usd=1.0))
        ua.mark_dispatched(reservation)
        ua.settle_attempt(reservation, {}, cost_usd=0.5, cost_final=True)
    return fields


# --- 1A: Delete of a continuation -----------------------------------------------------

def test_deleted_related_cron_stops_firing_and_waits_visibly_until_its_task_settles(world, monkeypatch):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    ctx, root, pending = world
    answer = _handle_schedule_followup(ctx, relation='related', cron='* * * * *', objective='Keep checking')
    assert answer.startswith('FOLLOWUP_SCHEDULED'), answer
    sid = schedules.load_schedule_store(root)['tasks'][-1]['id']
    data = schedules.load_schedule_store(root)
    data['tasks'][-1]['next_run_at'] = PAST  # due now
    schedules._write_scheduled_tasks(data, root)
    queue.check_scheduled_tasks()
    [task] = pending
    assert _claim_worker_launch(queue, task, _worker(sent)) and len(sent) == 1
    pending.clear()
    workers.RUNNING[task['id']] = {'task': task, 'worker_id': 0}
    write_task_result(root, task['id'], 'running', result='working', metadata=task['metadata'])
    outcome = _delete(root, sid)
    assert outcome['ok'] and outcome['status'] == 'delete_deferred', outcome
    assert 'not settled' in outcome['detail']
    # Visible after reload on every consumer, never folded into history.
    activity, tool = _views(root, sid)
    for view in (activity, tool):
        assert view['status'] == 'delete_pending' and view['delete_pending'] is True
        assert view['retained'] is False
    assert _manage_schedules(ctx, action='list').count('delete_pending') >= 1
    from ouroboros.context import _scheduled_tasks_digest
    digest = _scheduled_tasks_digest(SimpleNamespace(drive_path=lambda rel: root / rel))
    assert [entry['id'] for entry in digest['delete_pending']] == [sid]
    # Fresh firings stop at once, however often the tick runs.
    for _ in range(2):
        data = schedules.load_schedule_store(root)
        next(r for r in data['tasks'] if r['id'] == sid)['next_run_at'] = PAST
        schedules._write_scheduled_tasks(data, root)
        queue.check_scheduled_tasks()
    assert pending == [] and row(root, sid)['last_task_id'] == task['id']
    # A save cannot silently withdraw Delete; a new explicit Restore can.
    before = row(root, sid)
    queue.upsert_scheduled_task({**copy.deepcopy(before), 'enabled': True}, drive_root=root)
    assert row(root, sid)['enabled'] is False and row(root, sid)['delete_requested_at']
    restored = queue.mutate_scheduled_task('restore', sid, reason='Cancel pending deletion',
                                          actor='owner:test', drive_root=root)
    assert restored['ok'] and restored['changed'] and restored['status'] == 'updated'
    assert row(root, sid)['enabled'] is True and not row(root, sid).get('delete_requested_at')
    assert row(root, sid)['last_task_id'] == task['id'] and len(sent) == 1
    assert _delete(root, sid)['status'] == 'delete_deferred'
    # The task settles and leaves the queue: the row goes by itself.
    write_task_result(root, task['id'], 'completed', result='done', metadata=task['metadata'])
    workers.RUNNING.pop(task['id'])
    queue.check_scheduled_tasks()
    assert sid not in {r['id'] for r in schedules.load_schedule_store(root)['tasks']}


def test_deleted_related_row_still_binds_its_admitted_task_through_a_resumed_handoff(world, monkeypatch):  # noqa: F811 - pytest fixture
    """Premature removal would leave ``scheduled_start`` refusing the related task."""
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    sid = register(world)['id']
    queue.check_scheduled_tasks()
    [task] = pending
    outcome = _delete(root, sid)
    assert outcome['status'] == 'delete_deferred' and 'accepted run is still owed' in outcome['detail']
    assert 'its task settles' in outcome['detail']
    assert _claim_worker_launch(queue, task, _worker(sent)) and len(sent) == 1
    pending.clear()
    queue.check_scheduled_tasks()  # the occurrence settles (possible dispatch)
    assert row(root, sid)['delete_requested_at'], 'possible dispatch is not task settlement'
    # The same task comes back (an exact pause, a custody retry) and starts again.
    resumed = copy.deepcopy(sent[0])
    assert _claim_worker_launch(queue, resumed, _worker(sent)) and len(sent) == 2
    assert sent[1]['id'] == task['id'] and sent[1]['metadata']['billing_group'] == BINDING
    write_task_result(root, task['id'], 'completed', result='done', metadata=task['metadata'])
    queue.check_scheduled_tasks()
    assert sid not in {r['id'] for r in schedules.load_schedule_store(root)['tasks']}


def test_exact_hold_release_on_a_pending_delete_keeps_the_delete_and_lets_the_task_start(world, monkeypatch):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    sid = register(world)['id']
    queue.check_scheduled_tasks()
    [task] = pending
    stop(root)
    assert not _claim_worker_launch(queue, task, _worker(sent)) and not sent
    assert _delete(root, sid)['status'] == 'delete_deferred'
    queue.check_scheduled_tasks()
    held = row(root, sid)['followup_hold']['hold_id']
    activity, _tool = _views(root, sid)
    assert activity['status'] == 'delete_pending' and activity['restorable'] is True
    released = restore(root, sid, held)
    assert released['status'] == 'hold_released', released
    current = row(root, sid)
    assert current['enabled'] is False and current['delete_requested_at'] and not current.get('followup_hold')
    # A stale replay never withdraws the delete either.
    replay = restore(root, sid, held)
    assert not replay['changed'] and row(root, sid)['delete_requested_at']
    assert _claim_worker_launch(queue, task, _worker(sent)) and [t['id'] for t in sent] == [task['id']]
    assert row(root, sid)['last_task_id'] == task['id'] and pending == [task]


@pytest.mark.parametrize('result', ['collected', 'unreadable'])
def test_a_pending_delete_is_not_kept_forever_by_collected_history(world, monkeypatch, result):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    sid = register(world)['id']
    queue.check_scheduled_tasks()
    [task] = pending
    assert _claim_worker_launch(queue, task, _worker(sent))
    pending.clear()
    queue.check_scheduled_tasks()
    assert _delete(root, sid)['status'] == 'delete_deferred'
    path = root / 'task_results' / (task['id'] + '.json')
    if result == 'collected':
        path.unlink()  # retention removed settled history; no queue holds the task
    else:
        path.write_text('{torn')  # unknown stays unknown
    queue.check_scheduled_tasks()
    kept = {r['id'] for r in schedules.load_schedule_store(root)['tasks']}
    assert (sid in kept) is (result == 'unreadable')


def test_positively_unstarted_continuation_claim_is_deleted_at_once_from_a_worker(world, monkeypatch):  # noqa: F811 - pytest fixture
    _, root, _ = world
    sid = register(world)['id']
    data = schedules.load_schedule_store(root)
    data['tasks'][0]['occurrence'] = {'phase': 'claimed', 'token': 'tok', 'task_id': 'never-started'}
    schedules._write_scheduled_tasks(data, root)
    monkeypatch.setenv('OUROBOROS_IN_WORKER', '1')  # no fresh queue snapshot: queue unknown
    outcome = _delete(root, sid)
    assert outcome['status'] == 'deleted' and outcome['running_or_queued'] is None
    assert sid not in {r['id'] for r in schedules.load_schedule_store(root)['tasks']}


def test_independent_owner_delete_keeps_its_published_lifecycle(world, monkeypatch):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    queue.upsert_scheduled_task({'id': 'owner', 'name': 'Owner check', 'enabled': True,
                                 'trigger': {'type': 'once', 'run_at': PAST},
                                 'task': {'type': 'task', 'text': 'check',
                                          'metadata': {'resource_intent': {'kind': 'system_repo'}}}},
                                drive_root=root)
    queue.check_scheduled_tasks()
    [task] = pending
    outcome = _delete(root, 'owner')
    assert outcome['status'] == 'delete_deferred' and outcome['detail'].endswith('once that run starts')
    assert _claim_worker_launch(queue, task, _worker(sent))
    pending.clear()
    queue.check_scheduled_tasks()
    # Removed once its run started; the running task itself is not cancelled.
    assert 'owner' not in {r['id'] for r in schedules.load_schedule_store(root)['tasks']}
    assert load_task_result(root, task['id'])['status'] == 'running'


# --- 2A: published rows after the update ----------------------------------------------

@pytest.mark.parametrize('trigger', ['once', 'cron'])
def test_published_owner_row_fires_once_as_independent_work_with_its_own_wallet(world, monkeypatch, trigger):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    published = {'id': 'owner-legacy', 'name': 'Nightly check', 'enabled': True, 'timezone': '',
                 'trigger': ({'type': 'once', 'run_at': PAST} if trigger == 'once'
                             else {'type': 'cron', 'expr': '0 3 * * *'}),
                 'task': {'type': 'task', 'text': 'check', 'metadata': {'resource_intent': {'kind': 'system_repo'}}},
                 **({'next_run_at': PAST} if trigger == 'cron' else {})}
    schedules._write_scheduled_tasks({'tasks': [published]}, root)
    stop(root)  # an unrelated Stop elsewhere never touches an owner schedule
    queue.check_scheduled_tasks()
    current = row(root, 'owner-legacy')
    assert not current.get('followup_hold') and not current.get('followup_wait')
    [task] = pending
    receipt = load_task_result(root, task['id'])['schedule_admission']
    assert receipt['token'] == task['metadata']['schedule_occurrence']['token']
    assert _claim_worker_launch(queue, task, _worker(sent)) and len(sent) == 1
    assert sent[0]['metadata']['followup_relation']['kind'] == 'independent'
    fields = _spend(root, task)
    assert fields['billing_group_id'] == task['id']
    queue.upsert_scheduled_task({**copy.deepcopy(row(root, 'owner-legacy')), 'name': 'Renamed'}, drive_root=root)
    assert not row(root, 'owner-legacy').get('followup_hold')


def test_published_followup_keeps_prior_launch_and_own_root_money_until_its_origin_stops(world, monkeypatch):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    write_task_result(root, 'other-origin', 'running', root_task_id='other-origin')

    def published(sid, origin):
        return {'id': sid, 'name': sid, 'enabled': True, 'source': 'task_followup',
                'trigger': {'type': 'once', 'run_at': PAST},
                'task': {'type': 'task', 'text': 'look again',
                         'metadata': {'origin_task_id': origin, 'origin_root_task_id': origin,
                                      'resource_intent': {'kind': 'system_repo'}}}}

    schedules._write_scheduled_tasks({'tasks': [published('mine', ORIGIN), published('theirs', 'other-origin')]}, root)
    queue.check_scheduled_tasks()
    by_schedule = {t['metadata']['schedule_id']: t for t in pending}
    assert set(by_schedule) == {'mine', 'theirs'}, 'no blanket upgrade hold'
    for task in by_schedule.values():
        # Honest unknown relationship; admission pinned the task's OWN root group.
        assert task['metadata']['followup_relation'] == {'kind': 'unknown'}
        assert task['metadata']['billing_group']['billing_group_id'] == task['id']
    assert 'followup_relation' not in row(root, 'mine'), 'never stamped independent or related'
    # Its origin is Stopped after admission, before the physical put: it holds there,
    # same task and token, while the unrelated origin's follow-up still starts.
    stop(root)
    mine, theirs = by_schedule['mine'], by_schedule['theirs']
    token = mine['metadata']['schedule_occurrence']['token']
    assert not _claim_worker_launch(queue, mine, _worker(sent))
    assert _claim_worker_launch(queue, theirs, _worker(sent))
    assert [t['id'] for t in sent] == [theirs['id']]
    queue.check_scheduled_tasks()
    assert row(root, 'mine')['followup_hold']['reason'] == 'relationship_unknown'
    assert not row(root, 'theirs').get('followup_hold')
    assert mine['metadata']['schedule_occurrence']['token'] == token and mine in pending
    # The started one pays through its own root, not the original task's cap.
    fields = _spend(root, theirs)
    assert fields['billing_group_id'] == theirs['id'] and fields['billing_group_id'] != BINDING['billing_group_id']


def test_previously_accepted_published_occurrence_republishes_same_identity_and_wallet(world, monkeypatch):  # noqa: F811 - pytest fixture
    world, workers, sent = _assignment_world(world, monkeypatch)
    _, root, pending = world
    token, tid = 'pre-update-token', 'preupdate1'
    frozen = {'id': tid, 'type': 'task', 'text': 'accepted before the update', 'chat_id': 0,
              'root_task_id': tid, 'metadata': {'schedule_id': 'legacy', 'origin_task_id': ORIGIN,
                                                'schedule_occurrence': {'schedule_id': 'legacy', 'token': token},
                                                'resource_intent': {'kind': 'system_repo'}}}
    write_task_result(root, tid, 'scheduled', metadata=copy.deepcopy(frozen['metadata']), schedule_id='legacy',
                      schedule_admission={'schedule_id': 'legacy', 'token': token, 'dispatch': 'none',
                                          'task': copy.deepcopy(frozen), 'due_at': PAST})
    legacy = {'id': 'legacy', 'name': 'legacy', 'enabled': False, 'source': 'task_followup',
              'trigger': {'type': 'once', 'run_at': PAST}, 'completed_at': PAST, 'last_task_id': tid,
              'occurrence': {'token': token, 'task_id': tid, 'phase': 'admitted', 'due_at': PAST},
              'task': {'type': 'task', 'text': 'accepted before the update',
                       'metadata': {'origin_task_id': ORIGIN, 'origin_root_task_id': ORIGIN}}}
    schedules._write_scheduled_tasks({'tasks': [legacy]}, root)
    queue.check_scheduled_tasks()
    [task] = pending
    assert task['id'] == tid and task['metadata']['schedule_occurrence']['token'] == token
    assert _claim_worker_launch(queue, task, _worker(sent)) and [t['id'] for t in sent] == [tid]
    assert load_task_result(root, tid)['schedule_admission']['token'] == token
    fields = _spend(root, task)
    assert fields['billing_group_id'] == tid
