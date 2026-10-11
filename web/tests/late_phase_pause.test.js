import assert from 'node:assert/strict';
import test from 'node:test';

// Owner D10: an answered root whose late work the owner paused. The delivered
// answer owns the card's chip; the late phase states itself beside it, and the
// Activity census row offers Resume only once the remainder is saved.
globalThis.document = globalThis.document || { createElement: () => ({}) };
const { isTerminalTaskDetail, summarizeChatLiveEvent, taskDoneIsTerminal } = await import('../modules/log_events.js');
const { desiredLiveCardPhase } = await import('../modules/task_phase_chip.js');
const { liveActivityRowHtml } = await import('../modules/activity.js');

test('a paused late phase keeps the answered card open with its outcome known', () => {
    for (const status of ['completed', 'failed']) {
        const record = { type: 'task_done', status, root_phase_checkpoint: { post_task_synthesis: 'paused' } };
        assert.equal(isTerminalTaskDetail(record), false, status);
        assert.equal(taskDoneIsTerminal(record), false, status);
        assert.equal(summarizeChatLiveEvent(record).observedOutcome, status === 'failed' ? 'error' : 'done');
    }
    // Positive control: a settled late phase closes the card.
    for (const post_task_synthesis of ['completed', 'degraded']) {
        assert.equal(isTerminalTaskDetail({ status: 'completed', root_phase_checkpoint: { post_task_synthesis } }), true);
    }
});

test('the outcome owns the chip and the late Pause is its quieter second fact', () => {
    const card = { finalizingHold: true, observedOutcome: 'done' };
    assert.deepEqual(desiredLiveCardPhase(card), {
        phase: 'done', text: 'Done', className: 'chat-live-phase done', secondary: 'Finalizing…' });
    assert.equal(desiredLiveCardPhase({ ...card, parkedPhase: 'budget_pausing' }).secondary, 'Pausing…');
    const paused = desiredLiveCardPhase({ ...card, parkedPhase: 'budget_paused' });
    assert.equal(paused.text, 'Done', 'the delivered answer stays Done');
    assert.equal(paused.secondary, 'Paused');
    // Before any outcome frame arrived, the parked late phase still reads Paused, never Working.
    assert.equal(desiredLiveCardPhase({ finalizingHold: true, parkedPhase: 'budget_paused' }).text, 'Paused');
    assert.equal(desiredLiveCardPhase({ finalizingHold: true }).text, 'Finalizing…');
    // Terminal truth still wins once the late phase settled.
    assert.equal(desiredLiveCardPhase({ ...card, parkedPhase: 'budget_paused', finished: true }, 'done').text, 'Done');
});

test('the census-only Activity row offers Resume only for a saved remainder', () => {
    const now = 1_700_000_100_000;
    const paused = liveActivityRowHtml({ activity_id: 'late-root', kind: 'managed_task', phase: 'budget_paused',
        started_at: 1_700_000_000 }, now);
    assert.match(paused, /data-act="task-control" data-id="late-root" data-root="1" data-budget-paused="1"/);
    assert.match(paused, /budget_paused · 100s/);
    const pausing = liveActivityRowHtml({ activity_id: 'late-root', kind: 'managed_task', phase: 'budget_pausing' }, now);
    assert.doesNotMatch(pausing, /data-budget-paused/, 'sent work is still settling: Stop only, no Resume');
    assert.match(pausing, /data-root="1"/);
});

test('Resume of late work says it continues in place, never that it returns to the queue', async () => {
    const { resumeTaskAction } = await import('../modules/task_control_menu.js');
    const toasts = [];
    const toast = (message, tone) => toasts.push([message, tone]);
    await resumeTaskAction('late-root', { resume: async () => ({ ok: true, late_phase: 'resumed' }), toast });
    await resumeTaskAction('loop-root', { resume: async () => ({ ok: true, exact_continuation: true }), toast });
    assert.deepEqual(toasts, [
        ['Resuming: the work left after the delivered answer continues.', 'info'],
        ['Resuming: the task returns to the queue.', 'info'],
    ]);
});
