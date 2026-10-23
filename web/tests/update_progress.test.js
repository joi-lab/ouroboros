import assert from 'node:assert/strict';
import test from 'node:test';
import { dismissUpdateAttempt, initUpdateStatus, updateAttemptView } from '../modules/update_status.js';
import { installDomDouble } from './dom_double.js';
import { bindUpdateRefreshEvents, updateVerdict } from '../modules/updates.js';

const observed = (stage, extra = {}) => ({
    managed: true,
    update_progress: { operation_id: 'operation-a', generation: 'server-a', stage, active: true, ...extra },
});

test('server stages refine the pending apply and survive reopening before a transaction exists', () => {
    for (const phase of ['updating', '', 'loading']) {
        const verdict = updateVerdict(observed('stopping_workers'), phase);
        assert.equal(verdict.headline, 'Stopping worker processes…');
        assert.equal(verdict.state, 'updating');
        assert.equal(verdict.action.disabled, true);
    }
    assert.equal(updateVerdict(observed('checking'), 'updating').headline, 'Checking the update and dependencies…');
});

test('durable recovery outranks an earlier running observation', () => {
    for (const recovery of ['corrupt', 'gate_blocked', 'marker_cleanup_retry']) {
        const data = { ...observed('stopping_workers'), update_tx: { active: true, phase: recovery } };
        const verdict = updateVerdict(data, 'updating');
        assert.equal(verdict.tone, 'error');
        assert.notEqual(verdict.headline, 'Stopping worker processes…');
    }
});

test('terminal failure without a transaction remains actionable after reopen', () => {
    const failure = observed('stopping_services', { active: false, result: 'failed', error: 'service remains alive', restart_required: true });
    assert.equal(updateVerdict(failure).state, 'restart_needed');
    assert.equal(updateVerdict(failure).action.id, 'restart');
    assert.match(updateVerdict(failure).hint, /service remains alive/);
    const retry = observed('preparing', { active: false, result: 'failed', error: 'target moved' });
    assert.equal(updateVerdict(retry).action.id, 'check');
});

test('current restart outcomes and surviving transactions outrank an old failed observation', () => {
    const old = observed('preparing', { active: false, result: 'failed', error: 'old refusal' });
    for (const phase of ['restart_needed', 'restart_required']) {
        assert.equal(updateVerdict(old, phase).state, phase);
    }
    const stashing = { ...old, update_tx: { active: true, phase: 'stashing_local_work' } };
    assert.equal(updateVerdict(stashing).state, 'resolving');
    assert.match(updateVerdict(stashing).hint, /stashing_local_work/);
});

test('retained failure does not replace a new request that is still pending', () => {
    const old = observed('preparing', { active: false, result: 'failed', error: 'old refusal' });
    for (const phase of ['checking', 'preflighting', 'updating']) {
        const verdict = updateVerdict(old, phase);
        assert.equal(verdict.state, phase);
        assert.equal(verdict.action.disabled, true);
    }
});

test('reconnect preserves a preflight or apply whose request is still pending', () => {
    for (const phase of ['preflighting', 'updating']) {
        const listeners = new Map(), reads = [];
        const ws = { on(name, fn) { listeners.set(name, fn); return () => listeners.delete(name); } };
        const binding = bindUpdateRefreshEvents({ ws, getPhase: () => phase,
            loadStatus: (value) => reads.push(value), reconcileRestart: () => assert.fail('not restarting') });
        listeners.get('open')({ previouslyConnected: true });
        assert.deepEqual(reads, [{ fetchRemote: false, preservePhase: true }]);
        binding.dispose();
    }
});

test('assisted handoff and fresh generation use existing durable state', () => {
    const data = { ...observed('applying', { active: false, result: 'assisted_started' }),
        update_tx: { active: true, phase: 'assisted_resolution', task_id: 'resolver-a' } };
    assert.match(updateVerdict(data).hint, /resolver-a/);
    assert.match(updateVerdict({ managed: true, update_tx: { active: true, phase: 'pending_boot_smoke' } }).headline, /waiting for a restart/);
    assert.notEqual(updateVerdict({ managed: true, update_progress: {} }).state, 'updating');
});

test('progress invalidation refreshes an apply but cannot certify boot completion', () => {
    const listeners = new Map();
    const ws = { on(name, fn) { listeners.set(name, fn); return () => listeners.delete(name); } };
    let phase = 'updating';
    const reads = [], restart = [];
    const binding = bindUpdateRefreshEvents({ ws, getPhase: () => phase,
        loadStatus: (value) => reads.push(value), reconcileRestart: (value) => restart.push(value) });
    listeners.get('update_progress_changed')();
    assert.deepEqual(reads, [{ fetchRemote: false, preservePhase: true }]);
    phase = 'restarting';
    binding.beginRestarting();
    listeners.get('update_progress_changed')();
    assert.equal(restart.length, 0);
    listeners.get('open')({ previouslyConnected: true });
    listeners.get('update_progress_changed')();
    assert.deepEqual(restart, [{ afterBootNotice: false }, { afterBootNotice: false }]);
    binding.dispose();
    assert.equal(listeners.size, 0);
});

const refusal = {
    operation_id: 'attempt-a', generation: 'server-a', current_generation: true, stage: 'draining_writers',
    active: false, result: 'failed', outcome: 'not_applied', outcome_at: '2026-10-21T00:20:00Z',
    error: 'Could not prove every repository writer stopped.', reason: 'update_writer_fence_blocked',
    blockers: ['active:Main conversation'], work_stop: 'none', concluded_by: 'executor', restart_required: false,
    base_sha: 'f'.repeat(40), target_sha: 'b'.repeat(40),
};
const settle = async () => {
    for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

test('a refusal before any teardown is not applied, stopped nobody, and names its blocker', () => {
    const view = updateAttemptView(refusal);
    assert.deepEqual([view.terminal, view.pill, view.tone, view.headline],
        [true, 'Update not applied', 'error', 'No update was applied.']);
    assert.equal(view.text, 'Work was still finishing: Main conversation. This attempt stopped no running work. Try again when it finishes.');
    assert.deepEqual(view.details.find((row) => row.label === 'Reason'), { label: 'Reason', value: 'update_writer_fence_blocked' });
    assert.equal(view.details.some((row) => row.label === 'Saved local work'), false, 'no ref is ever manufactured');
});

test('after teardown or a restart the attempt never claims it stopped nobody', () => {
    for (const work_stop of ['invoked', '']) {
        assert.doesNotMatch(updateAttemptView({ ...refusal, blockers: ['service:mcp'], work_stop }).text, /stopped no running work/);
    }
    assert.match(updateAttemptView({ ...refusal, blockers: [], work_stop: 'invoked' }).text, /may have been interrupted/);
});

test('only the restarted server concludes success; reconnect, version and SHA are not evidence', () => {
    const awaiting = { ...refusal, outcome: '', result: 'restart_requested', stage: 'restart_requested',
        current_generation: false, work_stop: '', blockers: [] };
    const view = updateAttemptView(awaiting);
    assert.deepEqual([view.terminal, view.pill], [false, 'Confirming update…']);
    assert.doesNotMatch(view.headline, /completed|applied/i);
    const unknown = updateAttemptView({ ...awaiting, outcome: 'unknown', reason: 'no_completion_record' });
    assert.deepEqual([unknown.pill, unknown.tone, unknown.terminal], ['Update outcome unconfirmed', 'neutral', true]);
    assert.match(unknown.text, /running version alone does not establish the outcome/);
    const applied = updateAttemptView({ ...awaiting, outcome: 'applied', concluded_by: 'boot' });
    assert.deepEqual([applied.pill, applied.tone], ['Update completed', 'ok']);
    assert.equal(updateAttemptView({ ...awaiting, outcome: 'unknown', reason: 'startup_unconfirmed' }).text,
        'The restarted server did not confirm its startup checks.');
});

test('a rollback and a preserved Replace ref read as facts, not actions', () => {
    const rolled = updateAttemptView({ ...refusal, outcome: 'rolled_back', error: 'pre-restart smoke failed', blockers: [],
        work_stop: 'invoked' });
    assert.deepEqual([rolled.pill, rolled.headline], ['Previous version restored', 'The update failed; the previous version was restored.']);
    const kept = updateAttemptView({ ...refusal, outcome: 'applied', concluded_by: 'boot', blockers: [], work_stop: '',
        keep_branch: 'local-keep-20261021_032000-a1b2c3' });
    assert.match(kept.text, /kept on branch local-keep-20261021_032000-a1b2c3/);
    assert.equal(kept.details.find((row) => row.label === 'Saved local work').value, 'local-keep-20261021_032000-a1b2c3');
});

test('a failed save discloses retention uncertainty without changing the actual outcome or stage', () => {
    for (const active of [false, true]) {
        const progress = { ...refusal, active, retention_unconfirmed: true };
        const view = updateAttemptView(progress);
        const ordinary = updateAttemptView({ ...progress, retention_unconfirmed: false });
        assert.deepEqual([view.pill, view.headline, view.terminal], [ordinary.pill, ordinary.headline, ordinary.terminal]);
        assert.match(view.text, /could not be saved.*after restart/);
        assert.doesNotMatch(ordinary.text, /could not be saved/);
    }
});

test('a retained outcome never takes the primary action: Update stays an explicit retry', () => {
    const data = { managed: true, check_ok: true, available: true, safe_to_apply: true, latest_version: '7.1.0',
        update_tx: { active: false }, update_progress: refusal };
    const verdict = updateVerdict(data);
    assert.deepEqual([verdict.state, verdict.action.id, verdict.action.label], ['available', 'update', 'Update to 7.1.0']);
    assert.deepEqual([updateVerdict({ ...data, available: false }).state, updateVerdict({ ...data, available: false }).action.id],
        ['current', 'check']);
});

test('a restart need belongs to the server that had it', () => {
    const failure = { ...refusal, outcome: 'failed', restart_required: true, blockers: ['service:mcp'], work_stop: 'invoked' };
    assert.equal(updateVerdict({ managed: true, update_progress: failure }).state, 'restart_needed');
    assert.notEqual(updateVerdict({ managed: true, update_progress: { ...failure, current_generation: false } }).state,
        'restart_needed');
    assert.doesNotMatch(updateAttemptView({ ...failure, current_generation: false }).text, /needs a restart/);
});

test('the indicator shows the stage, then the outcome until dismissed, then the next attempt', async () => {
    const env = installDomDouble();
    const stored = new Map();
    const priorStorage = globalThis.localStorage;
    let controller;
    globalThis.localStorage = { getItem: (key) => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, String(value)) };
    try {
        env.doc.body.append(env.el('span', { attrs: { id: 'nav-version' } }));
        const listeners = new Map();
        const ws = { on(name, fn) { listeners.set(name, fn); return () => listeners.delete(name); } };
        const opened = [];
        let reads = 0;
        let status = { managed: true, available: false,
            update_progress: { ...refusal, active: true, outcome: '', result: '', stage: 'stopping_workers', work_stop: 'invoked' } };
        controller = initUpdateStatus({ ws, showPage: (page) => opened.push(page), openDashboardTab: (tab) => opened.push(tab),
            readStatus: async () => { reads += 1; if (!status) throw new Error('offline'); return structuredClone(status); } });
        await settle();
        const pill = env.doc.getElementById('update-pill');
        assert.deepEqual([pill.hidden, pill.textContent, pill.dataset.tone], [false, 'Stopping work…', 'neutral']);
        status = { ...status, update_progress: refusal };
        listeners.get('update_progress_changed')();
        await settle();
        assert.deepEqual([pill.textContent, pill.dataset.tone], ['Update not applied', 'error']);
        assert.match(pill.title, /No update was applied\./);
        pill.click();
        assert.deepEqual(opened, ['updates'], 'subtab navigation owns its one guarded page transition');
        const before = reads;
        dismissUpdateAttempt('attempt-a');
        globalThis.window.emit('ouro:update-attempt-dismissed');
        assert.equal(pill.hidden, true, 'hidden: no available update to point at');
        assert.equal(reads, before, 'dismissing sends no request');
        status = { ...status, update_progress: { ...refusal, operation_id: 'attempt-b', outcome: 'unknown',
            reason: 'no_completion_record', work_stop: '' } };
        listeners.get('update_status_ready')();
        await settle();
        assert.deepEqual([pill.hidden, pill.textContent, pill.dataset.tone], [false, 'Update outcome unconfirmed', 'neutral']);
        status = null;
        listeners.get('open')({ previouslyConnected: true });
        await settle();
        assert.equal(pill.textContent, 'Update outcome unconfirmed', 'a failed read hides nothing already known');
    } finally {
        controller?.dispose();
        globalThis.localStorage = priorStorage;
        env.restore();
    }
});

test('the indicator coalesces notices, never renders a superseded read, and disposes every subscription', async () => {
    const env = installDomDouble();
    try {
        const listeners = new Map();
        const ws = { on(name, fn) { listeners.set(name, fn); return () => listeners.delete(name); } };
        const pending = [];
        const windowListeners = globalThis.window.listenerCount();
        const controller = initUpdateStatus({ ws, readStatus: () => new Promise((resolve) => pending.push(resolve)) });
        listeners.get('update_progress_changed')();
        listeners.get('update_progress_changed')();
        assert.equal(pending.length, 1, 'one read in flight; later notices queue one more');
        pending.shift()({ managed: true, available: false, update_progress: { ...refusal, operation_id: 'stale' } });
        await settle();
        assert.equal(env.doc.getElementById('update-pill'), null, 'the superseded answer is not rendered');
        assert.equal(pending.length, 1, 'the queued read runs');
        pending.shift()({ managed: true, available: false, update_progress: refusal });
        await settle();
        assert.equal(env.doc.getElementById('update-pill').textContent, 'Update not applied');
        controller.dispose();
        assert.equal(listeners.size, 0);
        assert.equal(globalThis.window.listenerCount(), windowListeners);
    } finally {
        env.restore();
    }
});
