// The owner's hard Stop answered 202 pending — the durable stop is recorded and
// the supervisor finishes it (docs/CLIENT_CONTRACT.md §8). Driven through the
// real card control and the shared requestStop: an ok:true answer is accepted,
// task detail keeps the card «Cancelling…» and no «Cancel failed» toast is
// raised. The control case pins that this harness does see that toast for a
// Stop the server refuses, so the pending case is a real negative.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { ElementStub, installDom, restoreDom, walkCard } from './chat_dom_fixture.js';

// The flat fixture's querySelector does not descend; the card's Stop trigger
// lives in its actions row.
const originalQuery = ElementStub.prototype.querySelector;
ElementStub.prototype.querySelector = function (selector) {
    const direct = originalQuery.call(this, selector);
    if (direct) return direct;
    for (const child of this.children) { const found = child.querySelector(selector); if (found) return found; }
    return null;
};

const collect = (node, predicate, out = []) => {
    if (predicate(node)) out.push(node);
    for (const child of node?.children || []) collect(child, predicate, out);
    return out;
};

// A browser click: both controls stop propagation on their own event.
const press = (node) => {
    for (const fn of node.listeners.get('click') || []) fn({ target: node, stopPropagation() {}, preventDefault() {} });
};

async function stopNowAnsweredWith(taskId, cancelStatus, cancelBody) {
    const calls = [];
    const env = installDom(async (url, init = {}) => {
        calls.push(`${init.method || 'GET'} ${url}`);
        if (String(url) === `/api/tasks/${taskId}/cancel`) {
            return { ok: cancelStatus < 400, status: cancelStatus, json: async () => cancelBody };
        }
        if (String(url) === `/api/tasks/${taskId}`) {
            return { ok: true, status: 200,
                json: async () => ({ task_id: taskId, status: 'running', cancel_state: 'pending' }) };
        }
        return { ok: true, json: async () => ({ active_direct_turns: [] }) };
    });
    const priorObserver = globalThis.IntersectionObserver;
    globalThis.IntersectionObserver = class { observe() {} disconnect() {} takeRecords() { return []; } };
    const body = new ElementStub('body', globalThis.document);
    body.isConnected = true;
    globalThis.document.body = body;
    const handlers = new Map();
    let generation = 0;
    const instance = createChatInstance({
        ws: { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); },
            isConnected: () => true, send() {} },
        state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
        updateUnreadBadge() {},
        stateSnapshots: {
            begin: () => ({ generation: ++generation, requestedAt: Date.now() }),
            gate() { return Promise.resolve(this.begin()); },
            isCurrent: () => true, apply() {},
        },
        chatId: 1, idPrefix: 'chat', mountEl: env.mount,
    });
    try {
        handlers.get('chat')({
            chat_id: 1, task_id: taskId, role: 'assistant', is_progress: true, cancelable: true,
            content: 'Working on it.', ts: '2026-10-14T20:00:00Z',
        });
        const card = walkCard(globalThis.document.byId.get('chat-messages'), taskId);
        const trigger = card.querySelector('[data-cancel-run]');
        assert.ok(trigger, 'the host-attested marker offers Stop');
        press(trigger);
        const [stopNow] = collect(body, (node) => node?.dataset?.taskControl === 'stop_now');
        assert.ok(stopNow, 'the dropdown offers Stop now');
        press(stopNow);
        const detailRead = `GET /api/tasks/${taskId}`;
        for (let i = 0; i < 50 && !calls.includes(detailRead); i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 0));
        }
        await new Promise((resolve) => setTimeout(resolve, 0));
        const toasts = collect(body, (node) => node?.classList?.contains('toast')).map((node) => node.textContent);
        return {
            calls, toasts, triggerDisabled: trigger.disabled, finished: card.dataset.finished,
            phase: card.querySelector('[data-live-phase]')?.textContent,
        };
    } finally {
        instance.destroy();
        globalThis.IntersectionObserver = priorObserver;
        restoreDom(env.prior);
    }
}

test('a 202 pending Stop keeps the card Cancelling… with no Cancel failed toast', async () => {
    const seen = await stopNowAnsweredWith('pending-root', 202, {
        ok: true, task_id: 'pending-root', cancel_state: 'pending', stop_policy: 'immediate',
        request_id: 'ci_pending', reason_code: 'teardown_unconfirmed', cascade: true,
    });
    assert.ok(seen.calls.includes('POST /api/tasks/pending-root/cancel'), seen.calls);
    assert.ok(seen.calls.includes('GET /api/tasks/pending-root'), 'the card reconciles from task detail');
    assert.deepEqual(seen.toasts.filter((text) => /Cancel failed/.test(text)), []);
    assert.equal(seen.phase, 'Cancelling…');
    assert.equal(seen.finished, '0', 'only the settled task_done closes the card');
    assert.equal(seen.triggerDisabled, true, 'a hard Stop stays pending; the trigger is not re-armed');
});

test('control: a Stop the server refuses raises the Cancel failed toast in the same harness', async () => {
    const seen = await stopNowAnsweredWith('refused-root', 503, {
        error: 'durable cancel intent could not be recorded; nothing was cancelled — retry',
        task_id: 'refused-root', reason_code: 'cancel_intent_write_failed',
    });
    assert.ok(seen.calls.includes('POST /api/tasks/refused-root/cancel'), seen.calls);
    assert.equal(seen.toasts.filter((text) => /^Cancel failed: durable cancel intent could not be recorded/.test(text)).length, 1,
        seen.toasts);
});
