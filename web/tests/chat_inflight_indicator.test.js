import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    computeDerivedChatStatus,
    computeHydratedDirectActivities,
} from '../modules/chat.js';
import {
    chatStatusCounts,
    createStateSnapshotSequencer,
    isForegroundLiveCard,
    routingAnnotationText,
    unkeyedFrameEndsTurn,
} from '../modules/chat_activity.js';

const chatSource = readFileSync(new URL('../modules/chat.js', import.meta.url), 'utf8');

test('review-only owner anchors do not advertise foreground task activity', () => {
    const root = { isConnected: true };
    assert.equal(isForegroundLiveCard({ root, groupId: 'owner', finished: false }), true);
    assert.equal(isForegroundLiveCard({
        root, groupId: 'owner', finished: false, reviewAnchor: true,
    }), false);
});

test('unkeyed terminal incidents do not clear unrelated live turns', () => {
    // The cleanup guard also requires positive terminal evidence. Keep this
    // source contract resilient to the guard's explicit conjunction while
    // still checking the carve-out below it (the typed kinds that end nothing).
    const cleanupStart = chatSource.indexOf('if (!finalizing && (!explicitTaskId || typedTerminal))');
    const finalCleanup = chatSource.slice(
        cleanupStart,
        chatSource.indexOf("if (msg.system_type === 'task_summary')", cleanupStart),
    );
    assert.match(
        finalCleanup,
        /} else if \(unkeyedFrameEndsTurn\(msg\)\) \{[^}]*?activeDirectActivities\.clear\(\);[^}]*?pendingSubmissions\.clear\(\);\s*}\s*}/,
    );
    // Ordinary unkeyed finals retain their existing global cleanup semantics.
    assert.match(finalCleanup, /activeDirectActivities\.clear\(\);/);
    assert.match(finalCleanup, /pendingSubmissions\.clear\(\);/);
});

test('a note or skill notice arriving mid-turn keeps Thinking/Sending; an ordinary unkeyed final still ends it', () => {
    // What the live handler does with the unscoped turn for each frame (the branch pinned above).
    const turn = { isConnected: true, hasActiveLiveCard: false, activeDirectCount: 1, pendingSubmissionsCount: 1 };
    const after = (frame) => computeDerivedChatStatus(unkeyedFrameEndsTurn(frame)
        ? { ...turn, activeDirectCount: 0, pendingSubmissionsCount: 0 } : turn).text;
    for (const kind of ['reminder', 'skill_notice', 'terminal_incident']) {
        assert.equal(after({ role: 'system', system_type: kind, task_id: '', content: 'Call mother' }), 'Thinking...', kind);
    }
    assert.equal(after({ role: 'assistant', task_id: '', content: 'Done.' }), 'Online');
    assert.equal(after({ role: 'system', system_type: 'startup_notice', task_id: '' }), 'Online');
    // The kind decides, never the words: a reply that quotes a reminder still ends the turn.
    assert.equal(after({ role: 'assistant', task_id: '', content: 'Reminder · Ouroboros · written Oct 3 14:05' }), 'Online');
});

test('routing receipts display the event-time label while keeping raw target metadata', () => {
    const annotation = {
        action: 'steer_task',
        status: 'delivered',
        target: 'opaque-task-id',
        target_label: 'Launch 🚀 › Ship release',
    };
    assert.equal(routingAnnotationText(annotation), 'Steered task · Launch 🚀 › Ship release');
    assert.equal(annotation.target, 'opaque-task-id');
});

test('legacy routing receipts and manual choices use neutral labels, never raw ids', () => {
    assert.equal(routingAnnotationText({
        action: 'steer_task', status: 'delivered', target: 'opaque-task-id',
    }), 'Steered task · Task');
    assert.equal(routingAnnotationText({
        action: 'project_route', status: 'delivered', target: 'opaque-project-id',
    }), 'Project routing · Project');
    const manual = routingAnnotationText({
        action: 'manual', status: 'needs_manual_target', options: [
            { action: 'steer_task', task_id: 'opaque-option-id' },
            { action: 'project_route', project_id: 'opaque-project-id' },
        ],
    });
    assert.equal(manual, 'Choose a target · Task / Project');
    assert.equal(manual.includes('opaque-'), false);
});

test('a refusal row with neither options nor a cause reads «Not routed», never «Choose a target»', () => {
    // Such a row is a receipt written before the host sentence existed (or by a
    // producer that bypasses the receipt rail): it carries nothing to click, so
    // inviting a choice would be a lie about what the owner can do.
    assert.equal(routingAnnotationText({
        action: 'promote_chat_to_task', status: 'needs_manual_target',
        target: 'opaque-project-id', target_label: 'Аудит', options: [],
    }), 'Not routed · Аудит');
    assert.equal(routingAnnotationText({
        action: 'manual', status: 'needs_manual_target',
    }), 'Not routed');
});

test('a host cause on a refused routing receipt outranks the status matrix; an empty cause changes nothing', () => {
    // The host authors the owner sentence for a REFUSED act (`cause`); the
    // client never derives one from `reason`. The field is absent/empty on
    // scheduled, delivered and pending rows and on the picker frame, so those
    // keep their labels (the options case above stays «Choose a target · …»).
    const cause = 'Not started: the working folder can\'t be used';
    const refused = routingAnnotationText({
        action: 'promote_chat_to_task', status: 'needs_manual_target',
        target: 'x', target_label: 'Аудит', cause,
    });
    assert.equal(refused, cause);
    assert.equal(refused.includes('Choose a target'), false);
    assert.equal(routingAnnotationText({
        action: 'steer_task', status: 'delivered', target: 'opaque-task-id',
        target_label: 'Launch', cause: '',
    }), 'Steered task · Launch');
});

test('state snapshots and failure authority stay monotonic across reversed completion', () => {
    const applied = [];
    const requestTimes = [100, 200];
    const snapshots = createStateSnapshotSequencer(
        (data, requestedAt) => applied.push({ data, requestedAt }),
        () => requestTimes.shift(),
    );
    const older = snapshots.begin();
    const newer = snapshots.begin();

    assert.deepEqual(older, { generation: 1, requestedAt: 100 });
    assert.deepEqual(newer, { generation: 2, requestedAt: 200 });
    assert.equal(snapshots.isCurrent(older), true);
    assert.equal(snapshots.apply(newer, { activities: [] }), true);
    // An older non-OK/catch path can no longer regress the newer header.
    assert.equal(snapshots.isCurrent(older), false);
    assert.equal(snapshots.apply(older, { activities: ['stale-root'] }), false);
    assert.deepEqual(applied, [{ data: { activities: [] }, requestedAt: 200 }]);
});

test('computeDerivedChatStatus: offline state when ws is disconnected', () => {
    const status = computeDerivedChatStatus({
        isConnected: false,
        hasActiveLiveCard: true,
        activeDirectCount: 1,
        pendingSubmissionsCount: 1,
    });
    assert.deepEqual(status, {
        kind: 'offline',
        text: 'Reconnecting...',
        showDots: false,
    });
});

test('computeDerivedChatStatus: working state when active live card is present', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: true,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Working...',
        showDots: false,
    });
});

test('computeDerivedChatStatus: thinking state with dots when direct activities are active', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 1,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Thinking...',
        showDots: true,
    });
});

test('computeDerivedChatStatus: sending state with dots when local submissions are pending', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 1,
    });
    assert.deepEqual(status, {
        kind: 'thinking',
        text: 'Sending...',
        showDots: true,
    });
});

test('computeDerivedChatStatus: idle server status remains Online', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'online',
        text: 'Online',
        showDots: false,
    });
});

test('computeDerivedChatStatus: online idle state by default', () => {
    const status = computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    });
    assert.deepEqual(status, {
        kind: 'online',
        text: 'Online',
        showDots: false,
    });
});

test('computeHydratedDirectActivities: filters turns by chatId', () => {
    const turns = [
        { activity_id: 'act-main-1', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-proj-2', chat_id: 2, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-main-2', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const mapChat1 = computeHydratedDirectActivities(new Map(), turns, 1);
    assert.equal(mapChat1.size, 2);
    assert.ok(mapChat1.has('act-main-1'));
    assert.ok(mapChat1.has('act-main-2'));
    assert.ok(!mapChat1.has('act-proj-2'));

    const mapChat2 = computeHydratedDirectActivities(new Map(), turns, 2);
    assert.equal(mapChat2.size, 1);
    assert.ok(mapChat2.has('act-proj-2'));
});

test('computeHydratedDirectActivities: removes completed activities not in snapshot', () => {
    const initialMap = new Map([
        ['act-old', { activityId: 'act-old', kind: 'direct_chat', phase: 'thinking' }],
        ['act-keep', { activityId: 'act-keep', kind: 'direct_chat', phase: 'thinking' }],
    ]);

    const turns = [
        { activity_id: 'act-keep', chat_id: 1, kind: 'direct_chat', phase: 'working' },
        { activity_id: 'act-new', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const updated = computeHydratedDirectActivities(initialMap, turns, 1);
    assert.equal(updated.size, 2);
    assert.ok(!updated.has('act-old'));
    assert.ok(updated.has('act-keep'));
    assert.equal(updated.get('act-keep').phase, 'working');
    assert.ok(updated.has('act-new'));
});

test('computeHydratedDirectActivities: an incomplete census deletes nothing', () => {
    const initialMap = new Map([
        ['act-held', { activityId: 'act-held', kind: 'direct_chat', phase: 'thinking' }],
    ]);
    const partial = computeHydratedDirectActivities(initialMap, [], 1, null, false);
    assert.equal(partial.size, 1);
    assert.ok(partial.has('act-held'));
});

test('computeHydratedDirectActivities: correctly handles chat_id=0 without coercing to 1', () => {
    const turns = [
        { activity_id: 'act-sys-0', chat_id: 0, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-main-1', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];

    const mapChat0 = computeHydratedDirectActivities(new Map(), turns, 0);
    assert.equal(mapChat0.size, 1);
    assert.ok(mapChat0.has('act-sys-0'));

    const mapChat1 = computeHydratedDirectActivities(new Map(), turns, 1);
    assert.equal(mapChat1.size, 1);
    assert.ok(mapChat1.has('act-main-1'));
});

test('computeHydratedDirectActivities: a complete census deletes kind-less entries too', () => {
    const initialMap = new Map([
        // Kind-less entries were exempt while typing frames wrote this map. The
        // census is now its only writer, so its complete listing ends them too.
        ['managed-1', { activityId: 'managed-1', kind: '', phase: 'thinking' }],
        ['direct-1', { activityId: 'direct-1', kind: 'direct_chat', phase: 'thinking' }],
        ['direct-2', { activityId: 'direct-2', kind: 'direct_chat', phase: 'thinking' }],
    ]);

    assert.equal(computeHydratedDirectActivities(initialMap, [], 1).size, 0);
});

test('computeHydratedDirectActivities: a concluded turn is never resurrected by a snapshot captured while it still ran', () => {
    const concluded = new Map([['act-done', 12345]]);
    // Snapshot was requested mid-turn, its response arrived AFTER the turn's
    // keyed final concluded the activity client-side (project panels hydrate
    // once at creation, so without the ledger this insert would be permanent).
    const turns = [
        { activity_id: 'act-done', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
        { activity_id: 'act-live', chat_id: 1, kind: 'direct_chat', phase: 'thinking' },
    ];
    const updated = computeHydratedDirectActivities(new Map(), turns, 1, concluded);
    assert.equal(updated.size, 1);
    assert.ok(!updated.has('act-done'));
    assert.ok(updated.has('act-live'));
});

test('computeDerivedChatStatus: priority order is preserved (offline > live card > direct thinking > sending > online)', () => {
    // 1. Disconnected beats everything
    assert.equal(computeDerivedChatStatus({
        isConnected: false,
        hasActiveLiveCard: true,
        activeDirectCount: 5,
        pendingSubmissionsCount: 3,
    }).text, 'Reconnecting...');

    // 2. Active live card beats direct thinking & sending & attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: true,
        activeDirectCount: 5,
        pendingSubmissionsCount: 3,
    }).text, 'Working...');

    // 3. Direct thinking beats local pending submissions & attention
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 2,
        pendingSubmissionsCount: 3,
    }).text, 'Thinking...');

    // 4. Local pending submissions beat idle
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 1,
    }).text, 'Sending...');

    // 5. No server activity means Online
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    }).text, 'Online');

    // 6. Clean idle state
    assert.equal(computeDerivedChatStatus({
        isConnected: true,
        hasActiveLiveCard: false,
        activeDirectCount: 0,
        pendingSubmissionsCount: 0,
    }).text, 'Online');
});

test('computeDerivedChatStatus: paused work is not Working or Queued (#322)', () => {
    // The census phase is shared by a budget pause, the owner's Pause and a
    // Restart hold (Batch4), so the header states the pause, not a cause.
    assert.deepEqual(
        computeDerivedChatStatus({ pausedManagedCount: 2 }),
        { kind: 'online', text: 'Paused', showDots: false },
    );
    // Runnable queued work outranks the paused disclosure.
    assert.equal(
        computeDerivedChatStatus({ pausedManagedCount: 1, queuedManagedCount: 1 }).text,
        'Queued...',
    );
    // Active work outranks both.
    assert.equal(
        computeDerivedChatStatus({ pausedManagedCount: 1, activeManagedCount: 1 }).text,
        'Working...',
    );
});

test('sequenced failures stop stale replies without replacing a newer accepted census', () => {
    const events = [];
    const sequencer = createStateSnapshotSequencer(
        (data) => events.push(data), () => 123, () => events.push('unavailable'));
    const older = sequencer.begin();
    const failed = sequencer.begin();
    assert.equal(sequencer.fail(failed), true);
    assert.equal(sequencer.apply(older, 'stale complete empty'), false);
    assert.equal(sequencer.fail(failed), false);
    const newer = sequencer.begin();
    assert.equal(sequencer.apply(newer, 'fresh'), true);
    assert.equal(sequencer.fail(older), false);
    assert.deepEqual(events, ['unavailable', 'fresh']);
});

for (const kind of ['managed_task', 'direct_chat']) {
test(`Batch4: a paused or pausing ${kind} card does not drive working header state`, () => {
    const root = { isConnected: true };
    const paused = { root, groupId: 'p', finished: false, parkedPhase: 'budget_paused' };
    const pausing = { root, groupId: 's', finished: false, parkedPhase: 'budget_pausing' };
    const both = chatStatusCounts(new Map([
        ['p', { kind, phase: 'budget_paused' }],
        ['s', { kind, phase: 'budget_pausing' }],
    ]), [paused, pausing]);
    assert.equal(both.hasActiveLiveCard, false);
    assert.equal(computeDerivedChatStatus(both).text, 'Pausing…');
    const saved = chatStatusCounts(new Map([['p', { kind, phase: 'budget_paused' }]]), [paused]);
    assert.equal(computeDerivedChatStatus(saved).text, 'Paused');
    assert.equal(computeDerivedChatStatus(saved).showDots, false);
    // Resumed: the census says working again and the released card drives Working.
    const resumed = chatStatusCounts(new Map([['p', { kind, phase: 'working' }]]),
        [{ root, groupId: 'p', direct: kind === 'direct_chat', finished: false, parkedPhase: '' }]);
    assert.equal(computeDerivedChatStatus(resumed).text, kind === 'direct_chat' ? 'Thinking...' : 'Working...');
});
}
