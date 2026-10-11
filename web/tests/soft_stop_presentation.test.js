// S3 stream-gate fixes: MAJOR-A (owner decision №8/Q3) — an owner-requested
// finalization renders as factual SUCCESS "Done", never as a warning — while
// the owner-request marker stays in details. MINOR 7 (Q4): cancel_receipt
// keeps the 📋 System render style, never assistant-styled.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    OWNER_STOP_DETAIL_MARKER,
    summarizeChatLiveEvent,
    summarizeLogEvent,
    taskOutcomeSeverity,
    taskStoppedWithSummary,
    taskTerminalPhase,
    taskTerminalSummary,
} from '../modules/log_events.js';
import { senderLabel, buildTimelineItemHtml } from '../modules/chat_activity.js';

const chat = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');

// The terminal frame shape a soft-stopped task actually publishes: best_effort
// execution (the generic warn trigger) plus the typed owner-requested reason.
const softStop = {
    type: 'task_done',
    status: 'done',
    reason_code: 'owner_requested_finalization',
    outcome_axes: { execution: { status: 'best_effort' } },
};

// --- MAJOR-A: success severity, never warn-styled ---

test('owner-requested finalization classifies as done, not warn', () => {
    assert.equal(taskStoppedWithSummary(softStop), true);
    assert.equal(taskOutcomeSeverity(softStop), 'done');
    assert.equal(taskTerminalPhase(softStop), 'done');
    // Without the owner-requested reason the same best_effort axes still warn —
    // the special case is scoped to exactly this reason code.
    assert.equal(taskOutcomeSeverity({ ...softStop, reason_code: 'deadline' }), 'warn');
});

test('chat live card headline reads factual "Done" with the owner marker', () => {
    const view = summarizeChatLiveEvent(softStop);
    assert.equal(view.headline, 'Done');
    assert.equal(view.phase, 'done');                     // NOT warn-styled
    assert.equal(view.terminal, true);
    assert.ok(view.body.includes(OWNER_STOP_DETAIL_MARKER));
    assert.match(OWNER_STOP_DETAIL_MARKER, /owner's request/);
    assert.match(OWNER_STOP_DETAIL_MARKER, /best available result/);
    assert.doesNotMatch(view.headline, /Finished with warnings/);
});

test('logs surface shows the same factual headline and marker instead of the raw code', () => {
    const view = summarizeLogEvent(softStop);
    assert.equal(view.headline, 'Done');
    assert.equal(view.phase, 'done');                     // NOT warn-styled
    assert.ok(view.meta.includes(OWNER_STOP_DETAIL_MARKER));
    assert.ok(!view.meta.includes('owner_requested_finalization'));
});

test('an expiry kill still reads Cancelled — honesty outranks the soft-stop label', () => {
    // When the grace ran out and custody hard-killed, lifecycle=cancelled must
    // win over the soft-stop presentation (the summary was NOT delivered).
    const expired = { ...softStop, status: 'cancelled' };
    assert.equal(taskOutcomeSeverity(expired), 'cancelled');
    assert.equal(summarizeChatLiveEvent(expired).headline, 'Cancelled');
});

test('the chat terminal seam shares one note with the owner marker in its body', () => {
    assert.match(chat, /const summary = taskTerminalSummary\(\{ \.\.\.msg, task_id: taskId \}\)/);
    const summary = taskTerminalSummary(softStop);
    assert.equal(summary.phase, 'done');
    assert.equal(summary.headline, 'Done');
    assert.equal(summary.body, OWNER_STOP_DETAIL_MARKER);
    assert.equal(summary.visible, true);
    assert.equal(summary.terminal, true);
    assert.deepEqual(summarizeChatLiveEvent(softStop), summary);
    const priorDocument = globalThis.document;
    globalThis.document = { createElement: () => ({ textContent: '', get innerHTML() { return this.textContent; } }) };
    try {
        const html = buildTimelineItemHtml({ ...summary, lineKey: 'terminal' }, { expandedLineKeys: new Set() });
        assert.match(html, /owner's request/);
        assert.match(html, /best available result/);
        assert.match(html, /chat-live-line done/);
    } finally { globalThis.document = priorDocument; }
});

// --- MINOR 7 (Q4): cancel_receipt rendered as 📋 System, not assistant ---

test('a system cancel_receipt row renders the 📋 System sender label', () => {
    // The receipt is transported role="system", system_type="cancel_receipt"
    // (supervisor/terminal_delivery.py). The extracted shared mapping has no
    // receipt special case, so it must fall through to the generic system label.
    assert.equal(senderLabel('system', false, 'cancel_receipt'), '📋 System');
});

test('the bubble keeps the system style class and the system_type marker', () => {
    // Rendered style, not just transported role: the bubble class is derived
    // from the role (`chat-bubble system`, never the assistant class), and the
    // system_type lands on the dataset for targeted styling.
    assert.match(chat, /bubble\.className = `chat-bubble \$\{role\}`/);
    assert.match(chat, /if \(systemType\) bubble\.dataset\.systemType = systemType;/);
    // History replay forwards system_type through to the renderer.
    assert.match(chat, /systemType: msg\.system_type \|\| ''/);
});
