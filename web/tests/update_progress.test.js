import assert from 'node:assert/strict';
import test from 'node:test';
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
