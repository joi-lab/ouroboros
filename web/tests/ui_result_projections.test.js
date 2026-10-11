import assert from 'node:assert/strict';
import test from 'node:test';
import { modelExecutionLabel, summarizeLogEvent, taskTerminalSummary } from '../modules/log_events.js';
import { clearStickyCardState, computeHydratedDirectActivities, reconcileHydratedDirectActivities } from '../modules/chat_activity.js';

test('partial activity keeps absent direct and managed rows while adding positive observations', () => {
    const existing = new Map([
        ['direct', { kind: 'direct_chat', clientMessageId: 'submitted' }],
        ['managed', { kind: 'managed_task' }],
    ]);
    const positive = [{ activity_id: 'new', chat_id: 1, kind: 'managed_task' }];
    const result = reconcileHydratedDirectActivities(existing, positive, 1, null, false);
    assert.deepEqual([...result.activities.keys()], ['direct', 'managed', 'new']);
    assert.deepEqual(result.departedManagedTaskIds, []);
    assert.deepEqual(result.disappearedManagedTaskIds, []);
    assert.deepEqual(result.concludedDirectActivities, []);
    assert.equal(computeHydratedDirectActivities(existing, [], 1, null, true).size, 0);
    // No wall-clock barrier survives: a complete census is total over every entry.
    const recent = new Map([['fresh', { kind: 'managed_task' }]]);
    assert.equal(computeHydratedDirectActivities(recent, [], 1, null, true).size, 0);
});

test('terminal presentation keeps one task key while new factual reason replaces its body', () => {
    const base = { task_id: 'task', status: 'completed', reason_code: 'final_message' };
    assert.equal(taskTerminalSummary(base).body, '');
    const refined = taskTerminalSummary({ ...base, reason_code: 'unknown_specific_reason' });
    assert.equal(refined.dedupeKey, taskTerminalSummary(base).dedupeKey);
    assert.match(refined.body, /unknown_specific_reason/);
    assert.equal(taskTerminalSummary({ ...base, task_phase: 'finalizing' }).terminal, false);
});

test('model execution distinguishes requested, usable and reported facts; absence is not a route', () => {
    assert.equal(modelExecutionLabel(undefined), '');
    assert.match(modelExecutionLabel({ source: 'not_observed', requested_model: 'model-a' }), /execution not observed/);
    const label = modelExecutionLabel({ source: 'usable_solve_response', requested_model: 'model-a',
        used_model: 'model-b', reported_model: 'reported-b', used_local: false, provider: 'provider' });
    assert.match(label, /initial request: model-a/);
    assert.match(label, /model-b/);
    assert.match(label, /^Last solve response: reported-b/);
    assert.match(label, /route: model-b/);
});

test('fan-out interval uses new-key presence and keeps measured zero and legacy reads', () => {
    const event = { type: 'swarm_fanout', task_id: 'root', subagent_count: 2 };
    assert.match(JSON.stringify(summarizeLogEvent({ ...event, fanout_interval_sec: 0, inter_wave_latency_sec: 9 })), /since previous fan-out 0s/);
    assert.match(JSON.stringify(summarizeLogEvent({ ...event, inter_wave_latency_sec: 9 })), /since previous fan-out 9s/);
    assert.doesNotMatch(JSON.stringify(summarizeLogEvent({ ...event, fanout_interval_sec: null, inter_wave_latency_sec: 9 })), /since previous/);
});


test('raw solve routes sharing a compact display name still disclose the original request', () => {
    const fact = {source: 'usable_solve_response', requested_model: 'openai::gpt-5.5', used_model: 'openai/gpt-5.5',
        reported_model: 'gpt-5.5', requested_use_local: false, used_local: false, provider: 'openrouter'};
    assert.match(modelExecutionLabel(fact), /initial request/);
    assert.doesNotMatch(modelExecutionLabel({...fact, requested_model: fact.used_model}), /initial request/);
});

test('clearing cycle state clears all added model, count and historical observations', () => {
    const record = {modelExecution: {source: 'usable_solve_response'}, toolCalls: 7,
        historicalUnavailable: true, historicalUnconfirmed: true, historicalTerminal: {phase: 'cancelled'}, lastLiveObservedAt: 99};
    assert.equal(clearStickyCardState(record), record);
    assert.equal(record.modelExecution, null);
    assert.equal(record.toolCalls, null);
    assert.equal(record.historicalUnavailable, false);
    assert.equal(record.historicalUnconfirmed, false);
    assert.equal(record.historicalTerminal, null);
    assert.equal(record.lastLiveObservedAt, 0);
});
