import test from 'node:test';
import assert from 'node:assert/strict';
import { activeModelWaits, createModelWaitController, isModelWaitReference, MODEL_WAIT_REASONS, mergeModelWaits, modelWaitAction, modelWaitRoleLabel } from '../modules/model_wait.js';
import { desiredLiveCardPhase } from '../modules/task_phase_chip.js';
import { computeDerivedChatStatus } from '../modules/chat_activity.js';

const row = (patch = {}) => ({ wait_id: 'wait-light', revision: 1, task_attempt: 1,
    role: 'light', model: 'claudexor::source-a=same-model', source: 'source-a',
    credential_harness: 'codex', credential_profile_id: 'personal', reason: 'quota',
    auto_continue: true, state: 'waiting', worker_slot_held: true, ...patch });

test('independent waiting roles keep their own account even on the same model', () => {
    const light = row();
    const main = row({ wait_id: 'wait-main', role: 'main', credential_profile_id: 'work' });
    const waits = mergeModelWaits({}, { [light.wait_id]: light, [main.wait_id]: main });
    assert.equal(activeModelWaits(waits).length, 2);
    assert.equal(waits['wait-light'].credential_profile_id, 'personal');
    assert.equal(waits['wait-main'].credential_profile_id, 'work');
    assert.deepEqual(activeModelWaits(waits, true), []);
});

test('out of order snapshots neither rewind auto-continue nor reopen a resolved episode', () => {
    const current = row({ revision: 4, auto_continue: false });
    const state = { [current.wait_id]: current };
    assert.equal(mergeModelWaits(state, { [current.wait_id]: row() })[current.wait_id], current);
    const resolved = row({ revision: 6, state: 'resolved', resolution: 'model_switched' });
    const next = mergeModelWaits(state, { [current.wait_id]: resolved });
    assert.deepEqual(activeModelWaits(next), []);
    assert.deepEqual(mergeModelWaits(next, { [current.wait_id]: row({ revision: 7 }) }), next);
});

test('a new task attempt does not display waits from its predecessor', () => {
    const old = row();
    const next = row({ wait_id: 'next-attempt', task_attempt: 2 });
    assert.deepEqual(activeModelWaits({ [old.wait_id]: old, [next.wait_id]: next }), [next]);
    assert.deepEqual(activeModelWaits({ [old.wait_id]: old }, false, 2), []);
});

test('current task attempt outranks historical waits even before its first wait', () => {
    const controller = createModelWaitController({ getRecord: () => null, doc: () => null });
    const old = row();
    controller.observe('task', { model_waits: { [old.wait_id]: old } });
    assert.equal(controller.waiting('task'), true);
    controller.observe('task', { activity_id: 'task', task_attempt: 2 });
    assert.equal(controller.waiting('task'), false);
    controller.observe('task', { model_waits: { [old.wait_id]: old } });
    controller.observe('task', { task_attempt: 1, status: 'completed' });
    assert.equal(controller.waiting('task'), false);
    const current = row({ wait_id: 'current-wait', task_attempt: 2 });
    controller.observe('task', { type: 'task_model_wait', ...current });
    assert.equal(controller.waiting('task'), true);
    controller.destroy();
});

test('same-revision acceptance is pending, not applied, and stale snapshots cannot erase it', () => {
    const initial = row();
    const pending = { ...initial, pending_action: { request_id: 'accepted', revision: initial.revision, action: 'retry' } };
    const state = mergeModelWaits({ [initial.wait_id]: initial }, { [initial.wait_id]: pending });
    assert.equal(state[initial.wait_id].pending_action.request_id, 'accepted');
    assert.equal(mergeModelWaits(state, { [initial.wait_id]: initial })[initial.wait_id], state[initial.wait_id]);
    const applied = row({ revision: 2, state: 'resolved', applied_request_id: 'accepted' });
    assert.deepEqual(activeModelWaits(mergeModelWaits(state, { [applied.wait_id]: applied })), []);
});

test('a malformed row cannot acquire actionable wait identity', () => {
    for (const patch of [{ revision: '1' }, { revision: 0 }, { task_attempt: null },
        { state: 'done' }, { reason: 'transport' }, { wait_id: 'different' }]) {
        assert.deepEqual(mergeModelWaits({}, { 'wait-light': row(patch) }), {});
    }
});

test('confirmed mixed access keeps an actionable replay row without inventing an account', () => {
    const mixed = row({ reason: 'auth_quota', credential_profile_id: '' });
    const state = mergeModelWaits({}, { [mixed.wait_id]: mixed });
    assert.deepEqual(activeModelWaits(state), [mixed]);
    assert.equal(state[mixed.wait_id].credential_profile_id, '');
    assert.deepEqual(mergeModelWaits({}, { [mixed.wait_id]: row({ reason: 'pool' }) }), {});
});

test('an engine-dated unavailable pool is its own actionable reason, not quota or sign-in', () => {
    const pool = row({ reason: 'unavailable', credential_profile_id: '', reset_at: '2099-01-01T00:00:00Z' });
    assert.deepEqual(activeModelWaits(mergeModelWaits({}, { [pool.wait_id]: pool })), [pool]);
    assert.ok(MODEL_WAIT_REASONS.includes('unavailable') && !MODEL_WAIT_REASONS.includes('transport'));
});

test('switch payload binds the exact wait revision and defaults to temporary role change', () => {
    const body = modelWaitAction('task-a', row(), 'switch', {
        model: 'openai::owner-model', credential_profile_id: '', ignored: 'never sent',
    }, 'request-a');
    assert.deepEqual(body, { request_id: 'request-a', decision_id: 'model_wait:task-a:wait-light',
        revision: 1, action: 'switch', model: 'openai::owner-model', credential_profile_id: '',
        use_local: false, persist_role: false });
    const saved = modelWaitAction('task-a', row(), 'switch', {
        model: row().model, credential_profile_id: 'work', persist_role: true,
    }, 'request-b');
    assert.equal(saved.persist_role, true);
    assert.equal(saved.credential_profile_id, 'work');
});

test('auto-continue and explicit retry are distinct text-free actions', () => {
    assert.deepEqual(modelWaitAction('t', row(), 'auto_continue', { auto_continue: false }, 'a'), {
        request_id: 'a', decision_id: 'model_wait:t:wait-light', revision: 1,
        action: 'auto_continue', auto_continue: false,
    });
    assert.deepEqual(modelWaitAction('t', row(), 'retry', {}, 'b'), {
        request_id: 'b', decision_id: 'model_wait:t:wait-light', revision: 1, action: 'retry',
    });
    assert.throws(() => modelWaitAction('t', row(), 'auto_continue', {}, 'c'));
});

test('role labels distinguish human-facing roles without model-name inference', () => {
    assert.equal(modelWaitRoleLabel('consciousness'), 'Background consciousness');
    assert.equal(modelWaitRoleLabel('fallback:2'), 'Fallback 3');
    assert.equal(modelWaitRoleLabel('reviewer:scope-a'), 'Reviewer · scope-a');
    assert.equal(modelWaitRoleLabel('subagent:writer'), 'Subagent · writer');
});

test('the typed history discriminator survives a chat transport wrapper', () => {
    assert.equal(isModelWaitReference({ type: 'chat', system_type: 'task_model_wait' }), true);
    assert.equal(isModelWaitReference({ type: 'task_model_wait' }), true);
    assert.equal(isModelWaitReference({ text: 'task_model_wait' }), false);
});

test('waiting has no computation animation and yields to real work and terminal truth', () => {
    assert.deepEqual(computeDerivedChatStatus({ waitingModelCount: 1, queuedManagedCount: 1 }), {
        kind: 'online', text: 'Waiting for access', showDots: false,
    });
    assert.equal(computeDerivedChatStatus({ waitingModelCount: 1, activeManagedCount: 1 }).text, 'Working...');
    assert.equal(desiredLiveCardPhase({ modelWaiting: true }).text, 'Waiting for access');
    assert.equal(desiredLiveCardPhase({ modelWaiting: true, cancelPendingPolicy: 'immediate' }).text, 'Cancelling…');
    assert.equal(desiredLiveCardPhase({ modelWaiting: true, finished: true }, 'done').text, 'Done');
});
