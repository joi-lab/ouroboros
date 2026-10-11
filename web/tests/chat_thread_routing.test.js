import assert from 'node:assert/strict';
import test from 'node:test';

import { mainLogFrameAccepts, mainThreadAccepts } from '../modules/chat_activity.js';

const known = new Set([60193030]);

test('main adopts its own and transport-shaped frames but rejects explicit panel zero', () => {
    assert.equal(mainThreadAccepts({ chat_id: 1 }, known), true);
    assert.equal(mainThreadAccepts({}, known), true);               // missing chat_id -> main
    assert.equal(mainThreadAccepts({ chat_id: 0 }, known), false);
    assert.equal(mainThreadAccepts({ chat_id: -1001 }, known), false);
    // External transport (Telegram-shaped big id) is never a registry member
    // and is never stamped: Main keeps adopting it.
    assert.equal(mainThreadAccepts({ chat_id: 197422551 }, known), true);
    assert.equal(mainThreadAccepts(null, known), true);
});

test('legacy absent log identity stays Main while explicit inner panel zero is rejected', () => {
    assert.equal(mainLogFrameAccepts({ chat_id: 0, data: { type: 'task_started' } }, known), true);
    assert.equal(mainLogFrameAccepts({ chat_id: 0, data: { type: 'review_reference', chat_id: 0 } }, known), false);
    assert.equal(mainLogFrameAccepts({ chat_id: 1, data: { type: 'review_reference', chat_id: 0 } }, known), false);
    assert.equal(mainLogFrameAccepts({ chat_id: 1, data: { type: 'task_started', chat_id: -1001 } }, known), false);
    assert.equal(mainLogFrameAccepts({ chat_id: -1001, data: { type: 'task_started' } }, known), false);
    assert.equal(mainLogFrameAccepts({ chat_id: 0, data: { type: 'task_started', chat_id: 1 } }, known), true);
    assert.equal(mainLogFrameAccepts({ project_thread: true, data: { chat_id: 1, project_thread: false } }, known), false);
});

test('main rejects frames of a project it already knows', () => {
    assert.equal(mainThreadAccepts({ chat_id: 60193030 }, known), false);
});

test('server stamp rejects a project frame the client has not learned yet', () => {
    // The race: fresh project, projectChatIds still empty (or stale).
    assert.equal(mainThreadAccepts({ chat_id: 77777777, project_thread: true }, new Set()), false);
    assert.equal(mainThreadAccepts({ chat_id: 77777777, project_thread: true }, null), false);
    assert.equal(mainThreadAccepts({ chat_id: 77777777 }, new Set()), true); // unstamped unknown id: unchanged behaviour
});

test('stamp is authoritative regardless of chat_id shape', () => {
    assert.equal(mainThreadAccepts({ chat_id: 1, project_thread: true }, known), false);
    assert.equal(mainThreadAccepts({ project_thread: true }, known), false);
});
