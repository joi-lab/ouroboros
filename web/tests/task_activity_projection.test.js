import test from 'node:test';
import assert from 'node:assert/strict';

import { activeModelWaits, mergeModelWaits } from '../modules/model_wait.js';
import { summarizeProjectActivities } from '../modules/project_activity.js';
import { chatStatusCounts, computeDerivedChatStatus } from '../modules/chat_activity.js';
import { activityWaitPhase, censusTaskPhase, desiredLiveCardPhase, modelActivityPresentation, paintTaskPhase,
    setInertCardPresentation, setLiveCardPhase, syncParkedPhase, taskActivityPresentation } from '../modules/task_phase_chip.js';

function element(hidden = false) {
    let text = '';
    const attrs = new Map();
    return { dataset: {}, className: '', hidden, isConnected: true, writes: 0,
        get textContent() { return text; },
        set textContent(value) { text = value; this.writes += 1; },
        getAttribute(name) { return attrs.get(name) ?? null; },
        setAttribute(name, value) { attrs.set(name, value); this.writes += 1; } };
}

const waitingModel = row => activeModelWaits(mergeModelWaits({}, row.model_waits), false, row.task_attempt || 0).length > 0;
const flattened = view => [view.text, view.secondary].filter(Boolean).join(' · ');

for (const [patch, text, motion, waiting] of [
    [{ phase: 'preparing' }, 'Preparing', true, false],
    [{ phase: 'working' }, 'Working', true, false],
    [{ phase: 'thinking' }, 'Thinking', true, false],
    [{ phase: 'queued' }, 'Queued', false, false],
    [{ phase: 'budget_paused', pause_cause: 'owner' }, 'Paused · owner pause', false, true],
    [{ phase: 'budget_paused', pause_cause: 'authority' }, 'Paused · technical issue', false, true],
    [{ phase: 'budget_paused', pause_cause: 'owner', finishing_reviews: true }, 'Paused · owner pause · review work finishing', false, true],
    [{ phase: 'budget_pausing', pause_cause: 'owner', finishing_reviews: true }, 'Pausing… · owner pause', false, true],
    [{ phase: 'budget_pausing' }, 'Pausing…', false, true],
    [{ phase: 'unknown' }, 'Activity unconfirmed', false, false],
    [{ required_question_unavailable: true }, 'Activity unconfirmed', false, false],
    [{ owner_wait: { owner_wait_state: 'waiting', quiz_state: 'open' }, required_question_unavailable: true },
        'Waiting for your answer', false, true],
    [{ owner_wait: { owner_wait_state: 'waiting', quiz_state: 'answered' }, required_question_unavailable: true },
        'Working', true, false],
    [{ owner_wait: { owner_wait_state: 'resumed', quiz_state: 'open' }, required_question_unavailable: true },
        'Working', true, false],
    [{ project_admission_hold: { label: 'Waiting for Project verification' }, required_question_unavailable: true },
        'Waiting for Project verification', false, true],
]) {
    test(`census, full card, compact chip and sidebar share ${text} (${JSON.stringify(patch)})`, () => {
        const row = { activity_id: 'root', phase: 'working', ...patch };
        const view = censusTaskPhase(row, null, true, waitingModel(row));
        assert.equal(view.text, text);
        assert.equal(view.motion, motion);
        assert.equal(view.waiting, waiting);
        const full = { phaseEl: element(), phaseSecondaryEl: element(true),
            inlineTypingEl: { style: {}, isConnected: true }, projectHold: row.project_admission_hold?.label || '' };
        syncParkedPhase(full, row.phase, row);
        assert.equal(full.phaseEl.textContent, text);
        assert.equal(full.phaseEl.dataset.motion, motion ? '1' : '0');
        assert.equal(full.inlineTypingEl.style.display, motion ? '' : 'none');
        const compact = element();
        paintTaskPhase(compact, view);
        assert.equal(compact.textContent, text);
        assert.equal(compact.dataset.motion, full.phaseEl.dataset.motion);
        const sidebar = summarizeProjectActivities([row]);
        assert.equal(sidebar.label, text);
        assert.equal(sidebar.motion, motion);
        assert.equal(sidebar.waiting, waiting);
    });
}

// Owner 2026-10-12 (option B): the chip says the call's state in a word that keeps its
// meaning; the exact phase is the card's detail, read in its expanded details.
for (const [phase, text, motion, detail] of [
    ['queued', 'Queued', false, 'Request accepted · waiting for admission'],
    ['dispatching', 'Sending', true, 'Preparing request dispatch'],
    ['awaiting_response', 'Sending', true, 'Sending request / waiting for response'],
    ['response_ready', 'Working', true, 'Response available'],
    ['response_received', 'Working', true, 'Response received · processing'],
    ['rejoining', 'Rejoining', false, 'Rejoining the same model operation'],
    ['unknown', 'Activity unconfirmed', false, 'Model operation unconfirmed'],
]) {
    test(`observed model ${phase} reaches the same task presenters and loses to lifecycle controls`, () => {
        const row = { activity_id: 'root', phase: 'working', model_activity: { phase, operation_id: 'same' } };
        const full = { phaseEl: element(), inlineTypingEl: { style: {}, isConnected: true } };
        syncParkedPhase(full, row.phase, row);
        assert.equal(full.phaseEl.textContent, text);
        assert.equal(censusTaskPhase(row).text, text);
        assert.equal(summarizeProjectActivities([row]).label, text);
        assert.equal(desiredLiveCardPhase(full).detail, detail);
        assert.equal(full.phaseEl.dataset.motion, motion ? '1' : '0');
        assert.equal(censusTaskPhase(row, null, false).text, 'Activity unconfirmed');
        assert.equal(censusTaskPhase(row, null, true, true).text, 'Waiting for access');
        assert.equal(censusTaskPhase({ ...row, phase: 'budget_paused', pause_cause: 'owner' }).text, 'Paused · owner pause');
        full.cancelPendingPolicy = 'immediate';
        assert.equal(desiredLiveCardPhase(full).text, 'Cancelling…');
        const records = [{ ...full, root: { isConnected: true }, groupId: 'root', direct: true }];
        const counts = chatStatusCounts(new Map([['root', row]]), records);
        assert.equal(computeDerivedChatStatus(counts).text, 'Cancelling…');
        full.finished = true;
        assert.equal(syncParkedPhase(full, 'working', row), false);
        assert.equal(desiredLiveCardPhase(full, 'cancelled').text, 'Cancelled');
    });
}

test('closed/resumed questions defeat a stale required flag, and an unreadable question stays unknown', () => {
    for (const state of ['answered', 'superseded', 'expired_terminal']) {
        assert.equal(activityWaitPhase({ required_question: { state, wait_for_answer: true } }), '');
    }
    assert.equal(activityWaitPhase({ required_question: { quiz_state: 'open', wait_for_answer: true } }), 'owner_wait');
    assert.equal(activityWaitPhase({ required_question: { quiz_state: 'open' } }), 'unknown');
    assert.equal(censusTaskPhase({ phase: 'working', required_question: { quiz_state: 'open' } }).motion, false);
});

test('a known failed outcome remains static beside the actual late phase', () => {
    const base = { activity_id: 'failed', phase: 'finalizing', status: 'failed',
        root_phase_checkpoint: { post_task_synthesis: 'running' },
        outcome_axes: { lifecycle: { status: 'failed' }, execution: { status: 'infra_failed' } } };
    for (const [patch, connected, modelWaiting, secondary, lateMotion] of [
        [{}, true, false, 'Finalizing…', true],
        [{ phase: 'budget_paused' }, true, false, 'Paused', false],
        [{ phase: 'budget_pausing' }, true, false, 'Pausing…', false],
        [{}, true, true, 'Waiting for access', false],
        [{ owner_wait: { owner_wait_state: 'waiting', quiz_state: 'open' } }, true, false, 'Waiting for your answer', false],
        [{}, false, false, 'Activity unconfirmed', false],
    ]) {
        const row = { ...base, ...patch };
        const view = censusTaskPhase(row, null, connected, modelWaiting);
        assert.equal(view.text, 'Failed');
        assert.equal(view.phase, 'error');
        assert.equal(view.motion, false);
        assert.equal(view.secondary, secondary);
        assert.equal(view.secondaryMotion, lateMotion);
        const primary = element(), late = element(true);
        paintTaskPhase(primary, view, late);
        assert.equal(primary.dataset.motion, '0');
        assert.equal(late.dataset.motion, lateMotion ? '1' : '0');
        assert.equal(primary.getAttribute('aria-label'), `Task status: Failed, ${secondary}`);
    }
    const shared = censusTaskPhase(base);
    const full = { phaseEl: element(), phaseSecondaryEl: element(true),
        finalizingHold: true, observedOutcome: 'error' };
    const wanted = desiredLiveCardPhase(full);
    setLiveCardPhase(full, wanted.phase, wanted.text, wanted.className, wanted.secondary);
    assert.equal(full.phaseEl.dataset.motion, '0');
    assert.equal(full.phaseSecondaryEl.dataset.motion, '1');
    assert.equal(summarizeProjectActivities([base]).label, flattened(shared));
    assert.equal(summarizeProjectActivities([base]).motion, true);
    setInertCardPresentation(full, true);
    assert.equal(full.phaseEl.hidden, true);
    assert.equal(full.phaseEl.getAttribute('aria-label'), 'Task status: Failed, Finalizing…');
    assert.equal(full.phaseSecondaryEl.textContent, 'Finalizing…');
    assert.equal(full.phaseSecondaryEl.hidden, true);
    assert.equal(full.phaseSecondaryEl.dataset.motion, '0');
    setInertCardPresentation(full, false);
    assert.equal(full.phaseSecondaryEl.dataset.motion, '1');
});

test('the chat header says Finalizing… for ended work, never Working, while real work still wins', () => {
    // 12.10: a failed root writing its reflection kept the header on "Working" for ~19 min.
    const finalizing = { activity_id: 'failed', kind: 'managed_task', phase: 'finalizing', status: 'failed',
        root_phase_checkpoint: { post_task_synthesis: 'running' } };
    const heldCard = { root: { isConnected: true }, groupId: 'failed', finished: false,
        finalizingHold: true, observedOutcome: 'error' };
    const status = (activities, records) => computeDerivedChatStatus(chatStatusCounts(new Map(activities), records)).text;
    assert.equal(status([['failed', finalizing]], []), 'Finalizing…');
    assert.equal(status([], [heldCard]), 'Finalizing…');
    assert.equal(status([['failed', finalizing]], [heldCard]), 'Finalizing…');
    const working = { activity_id: 'w', kind: 'managed_task', phase: 'working' };
    assert.equal(status([['failed', finalizing], ['w', working]], [heldCard]), 'Working');
    const direct = { activity_id: 'd', kind: 'direct_turn', phase: 'working' };
    assert.notEqual(status([['failed', finalizing], ['d', direct]], [heldCard]), 'Finalizing…');
    assert.equal(status([], [{ root: { isConnected: true }, groupId: 'x', finished: false }]), 'Working');
    assert.equal(computeDerivedChatStatus({ ...chatStatusCounts(new Map([['failed', finalizing]]), []),
        isConnected: false }).text, 'Reconnecting…');
    // A direct-chat root wraps up the same way (gateway census kind direct_chat).
    const directFinalizing = { ...finalizing, activity_id: 'dr', kind: 'direct_chat' };
    const directCard = { ...heldCard, groupId: 'dr', direct: true };
    assert.equal(status([['dr', directFinalizing]], []), 'Finalizing…');
    assert.equal(status([['dr', directFinalizing]], [directCard]), 'Finalizing…');
    assert.equal(status([], [directCard]), 'Finalizing…');
});

test('terminal detail stays terminal offline; finalization cannot be ended by a stale detail response', () => {
    const done = { status: 'completed', root_phase_checkpoint: { post_task_synthesis: 'completed' } };
    assert.equal(censusTaskPhase(null, done, false).text, 'Done');
    assert.equal(censusTaskPhase(null, done, false).motion, false);
    const stillFinalizing = censusTaskPhase({ phase: 'finalizing' }, done);
    assert.equal(stillFinalizing.text, 'Done');
    assert.equal(stillFinalizing.secondary, 'Finalizing…');
    assert.equal(stillFinalizing.motion, false);
    assert.equal(stillFinalizing.secondaryMotion, true);
    for (const status of ['cancelled', 'rejected_duplicate']) {
        const ended = censusTaskPhase({ phase: 'finalizing', status,
            root_phase_checkpoint: { post_task_synthesis: 'running' } });
        assert.equal(ended.motion, false);
        assert.equal(ended.secondary, undefined, `${status} keeps its canonical immediate terminality`);
    }
    const open = { status: 'failed', root_phase_checkpoint: { post_task_synthesis: 'running' } };
    assert.equal(censusTaskPhase(null, open).secondary, 'Activity unconfirmed');
    assert.equal(censusTaskPhase(null, { status: 'running' }).text, 'Activity unconfirmed');
    assert.equal(censusTaskPhase({}).text, 'Activity unconfirmed');
    assert.equal(censusTaskPhase({ phase: 'working' }, null, false).motion, false);
});

test('the validated model wait affects only its task attempt and independent siblings keep motion', () => {
    const row = { activity_id: 'waiting', phase: 'working', task_attempt: 2,
        model_waits: { w: { wait_id: 'w', revision: 1, task_attempt: 1, state: 'waiting', reason: 'quota' } } };
    assert.equal(censusTaskPhase(row, null, true, waitingModel(row)).text, 'Working');
    row.model_waits.w.task_attempt = 2;
    const waiting = censusTaskPhase(row, null, true, waitingModel(row));
    assert.equal(waiting.text, 'Waiting for access');
    assert.equal(waiting.motion, false);
    const mixed = summarizeProjectActivities([row, { activity_id: 'other', phase: 'working' }]);
    assert.equal(mixed.motion, true);
    assert.equal(mixed.waiting, true);
    assert.equal(mixed.label, 'Working · Waiting for access');
    row.model_waits.w.state = 'resolved';
    assert.equal(censusTaskPhase(row, null, true, waitingModel(row)).motion, true);
});

test('an optional question gap does not erase a later positive model wait on the same card', () => {
    const row = { phase: 'working', required_question_unavailable: true };
    const full = { phaseEl: element(), inlineTypingEl: { style: {}, isConnected: true } };
    syncParkedPhase(full, row.phase, row);
    assert.equal(full.phaseEl.textContent, 'Activity unconfirmed');
    full.modelWaiting = true;
    const view = desiredLiveCardPhase(full);
    assert.equal(view.text, censusTaskPhase(row, null, true, true).text);
    assert.equal(view.text, 'Waiting for access');
    setLiveCardPhase(full, view.phase, view.text, view.className, view.secondary);
    assert.equal(full.phaseEl.dataset.motion, '0');
    full.modelWaiting = false;
    assert.equal(desiredLiveCardPhase(full).text, 'Activity unconfirmed');
});

test('same-phase census outcome enrichment reaches the existing card without erasing absent facts', () => {
    const full = { phaseEl: element(), phaseSecondaryEl: element(true) };
    const row = { phase: 'finalizing', status: 'completed', root_phase_checkpoint: { post_task_synthesis: 'running' } };
    syncParkedPhase(full, row.phase, row);
    assert.equal(full.phaseEl.textContent, 'Done');
    assert.equal(syncParkedPhase(full, row.phase, { ...row, outcome_axes: { execution: { status: 'degraded' } } }), true);
    assert.equal(full.phaseEl.textContent, 'Done with warnings');
    assert.equal(full.phaseSecondaryEl.textContent, 'Finalizing…');
    assert.equal(syncParkedPhase(full, row.phase, {}), false);
    assert.equal(full.phaseEl.textContent, 'Done with warnings');
    assert.equal(full.phaseSecondaryEl.dataset.motion, '1');
});

test('the common DOM writer neither rewrites nor re-announces an unchanged status', () => {
    const primary = element(), late = element(true);
    const view = censusTaskPhase({ phase: 'finalizing', status: 'failed' });
    assert.equal(paintTaskPhase(primary, view, late), true);
    const writes = primary.writes + late.writes;
    assert.equal(paintTaskPhase(primary, view, late), false);
    assert.equal(primary.writes + late.writes, writes);
    const paused = censusTaskPhase({ phase: 'budget_paused', status: 'failed',
        root_phase_checkpoint: { post_task_synthesis: 'paused' } });
    assert.equal(paintTaskPhase(primary, paused, late), true);
    assert.equal(late.dataset.motion, '0');
    assert.equal(primary.getAttribute('aria-live'), 'polite');
    assert.equal(primary.getAttribute('role'), 'status');
    assert.equal(primary.getAttribute('aria-label'), 'Task status: Failed, Paused');
});

test('making a finished card inert never replaces its recorded outcome with the default Done', () => {
    const full = { finished: true, phaseEl: element(), phaseSecondaryEl: element(true) };
    setLiveCardPhase(full, 'error', 'Failed');
    setInertCardPresentation(full, true);
    assert.equal(full.phaseEl.textContent, 'Failed');
    assert.equal(full.phaseEl.dataset.phase, 'error');
    assert.equal(full.phaseEl.dataset.motion, '0');
    setInertCardPresentation(full, false);
    assert.equal(full.phaseEl.getAttribute('aria-label'), 'Task status: Failed');
});

test('Stop retains its existing active-operation movement without animating a parked task', () => {
    for (const stopPolicy of ['finalize', 'immediate']) {
        const active = taskActivityPresentation({ phase: 'working', stopPolicy });
        assert.equal(active.motion, true);
        assert.equal(active.text, stopPolicy === 'finalize' ? 'Finalizing…' : 'Cancelling…');
        for (const phase of ['queued', 'budget_paused', 'budget_pausing', 'unknown']) {
            assert.equal(taskActivityPresentation({ phase, stopPolicy }).motion, false);
        }
        assert.equal(taskActivityPresentation({ phase: 'working', stopPolicy, modelWaiting: true }).motion, false);
        assert.equal(taskActivityPresentation({ phase: 'working', stopPolicy, ended: true, outcome: 'done' }).text, 'Done');
    }
});

// One producer for the header (owner 15.10, 3C/4A): the word, its tone and its dots come together, and the tone
// follows the call's operation, never its English word.
test('the header producer pins word, tone and dots; a stop policy passes its own word through', () => {
    const header = (fields) => {
        const { text, tone, dots } = computeDerivedChatStatus(fields);
        return [text, tone, dots];
    };
    const call = (phase) => header({ activeDirectCount: 1, modelActivity: modelActivityPresentation({ phase }) });
    for (const phase of ['preparing', 'dispatching', 'awaiting_response', 'response_ready', 'response_received']) {
        assert.equal(call(phase)[1], 'live', phase);
        assert.equal(call(phase)[2], 'moving', phase);
    }
    assert.deepEqual(call('queued'), ['Queued', 'queued', 'still']);
    // Rejoining waits on a call it already started: a single fact, one dot, never queued work.
    assert.deepEqual(call('rejoining'), ['Rejoining', 'held', 'one']);
    assert.deepEqual(call('unknown'), ['Activity unconfirmed', 'unknown', 'one']);
    // A stop policy's view carries no operation: its own word stays (Cancelling…, Finalizing…), never Working.
    for (const [stopPolicy, word] of [['immediate', 'Cancelling…'], ['finalize', 'Finalizing…']]) {
        const view = taskActivityPresentation({ phase: 'working', stopPolicy });
        assert.deepEqual(header({ activeManagedCount: 1, modelActivity: view }), [word, 'live', 'moving']);
    }
    assert.deepEqual(header({ queuedManagedCount: 1 }), ['Queued', 'queued', 'still']);
    assert.deepEqual(header({ waitingOwnerCount: 1 }), ['Waiting for your answer', 'wait', 'one']);
    // Amber belongs to the owner's question alone: pauses, access waits and holds are neutral facts.
    assert.deepEqual(header({ pausedManagedCount: 1, pausedCause: 'owner' }), ['Paused · owner pause', 'held', 'one']);
    assert.deepEqual(header({ pausingManagedCount: 1 }), ['Pausing…', 'held', 'one']);
    assert.deepEqual(header({ waitingModelCount: 1 }), ['Waiting for access', 'held', 'one']);
    assert.deepEqual(header({ projectWaitLabel: 'Waiting for Project verification' }), ['Waiting for Project verification', 'held', 'one']);
    assert.deepEqual(header({ supervisorStarting: true }), ['Starting…', 'starting', 'one']);
    assert.deepEqual(header({ isConnected: false, activeDirectCount: 1 }), ['Reconnecting…', 'offline', 'one']);
    assert.deepEqual(header({}), ['Online', 'online', 'one']);
});
