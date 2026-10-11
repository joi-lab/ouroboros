/**
 * F5: the Widgets list deadline, driven by a FAKE CLOCK.
 *
 * Every case runs the product's own composition (`requestWidgetListPayload`),
 * not a copy of it, so the deadline, the sibling abort, the preferences
 * tolerance and the cleared timer are all observed where they actually live.
 *
 * Headers and body are distinguished at the transport: the fake `fetch`
 * resolves a response whose `json()` is a separate, independently stallable
 * promise, which is exactly where a real body stall sits.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { requestWidgetCards, requestWidgetListPayload } from '../modules/widget_list.js';
import { WIDGET_REQUEST_TIMEOUT_MS } from '../modules/widget_job.js';
import { apiClient } from '../modules/api_client.js';

const DEADLINE = WIDGET_REQUEST_TIMEOUT_MS;

function abortable(signal) {
    return new Promise((_resolve, reject) => {
        if (signal.aborted) {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
            return;
        }
        signal.addEventListener('abort', () => {
            const error = new Error('aborted');
            error.name = 'AbortError';
            reject(error);
        }, { once: true });
    });
}

/**
 * A transport whose per-URL behaviour is declared as 'ok' | 'headers' (stall
 * before the response resolves) | 'body' (headers arrive, body stalls) |
 * {status}. It is installed as the global fetch that `apiFetch` calls.
 */
function installFetch(plan) {
    const seen = [];
    globalThis.fetch = (url, init = {}) => {
        const key = String(url).includes('preferences') ? 'prefs' : 'widgets';
        seen.push({ key, url: String(url), signal: init.signal });
        const behaviour = plan[key] || 'ok';
        if (behaviour === 'headers') return abortable(init.signal);
        if (behaviour === 'body') {
            return Promise.resolve({
                ok: true,
                status: 200,
                json: () => abortable(init.signal),
            });
        }
        if (typeof behaviour === 'object' && behaviour.status) {
            return Promise.resolve({
                ok: false,
                status: behaviour.status,
                json: async () => ({ error: behaviour.message || 'boom' }),
            });
        }
        return Promise.resolve({
            ok: true,
            status: 200,
            json: async () => (key === 'prefs'
                ? { widget_order: ['a'], widget_start_mode: {}, nested_subagents_expanded: false }
                : { ui_tabs: [{ skill: 's', tab_id: 't' }] }),
        });
    };
    return seen;
}

function isTimeout(error) {
    return error && error.code === 'WIDGET_REQUEST_TIMEOUT' && error.retryable === true;
}

// A settled-microtask barrier: the deadline rejects through several awaits.
const settle = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };

test('the list deadline is the shared widget request timeout, not a new knob', () => {
    assert.equal(DEADLINE, 25000);
});

test('no timeout before the deadline, abort exactly at it (headers stall)', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'headers', prefs: 'ok' });
    const controller = new AbortController();
    const pending = requestWidgetListPayload(apiClient, controller);
    let outcome = null;
    pending.then((value) => { outcome = { value }; }, (error) => { outcome = { error }; });

    t.mock.timers.tick(DEADLINE - 1);
    await settle();
    assert.equal(outcome, null, 'the list must not fail one millisecond early');
    assert.equal(controller.signal.aborted, false);

    t.mock.timers.tick(1);
    await settle();
    assert.ok(outcome && outcome.error, 'the deadline must fire at exactly 25000ms');
    assert.ok(isTimeout(outcome.error));
    assert.equal(controller.signal.aborted, true);
});

test('a stalled BODY after successful headers still hits the same deadline', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'body', prefs: 'ok' });
    const controller = new AbortController();
    const pending = requestWidgetListPayload(apiClient, controller);
    let outcome = null;
    pending.then((value) => { outcome = { value }; }, (error) => { outcome = { error }; });

    t.mock.timers.tick(DEADLINE - 1);
    await settle();
    assert.equal(outcome, null);

    t.mock.timers.tick(1);
    await settle();
    assert.ok(outcome && isTimeout(outcome.error));
    assert.equal(controller.signal.aborted, true);
});

test('a PREFERENCES-only stall still times the whole list out', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'ok', prefs: 'headers' });
    const controller = new AbortController();
    const pending = requestWidgetListPayload(apiClient, controller);
    let outcome = null;
    pending.then((value) => { outcome = { value }; }, (error) => { outcome = { error }; });

    t.mock.timers.tick(DEADLINE - 1);
    await settle();
    assert.equal(outcome, null);

    t.mock.timers.tick(1);
    await settle();
    assert.ok(outcome && isTimeout(outcome.error));
    assert.equal(controller.signal.aborted, true);
});

test('a preferences ERROR is tolerated: the cards still arrive', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'ok', prefs: { status: 500 } });
    const controller = new AbortController();
    const [data, prefs] = await requestWidgetListPayload(apiClient, controller);
    assert.deepEqual(data.ui_tabs, [{ skill: 's', tab_id: 't' }]);
    assert.equal(prefs, null, 'a failed preferences read degrades to null, it does not blank the page');
    assert.equal(controller.signal.aborted, false);
});

test('a list error aborts its preferences sibling and keeps its own identity', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    const seen = installFetch({ widgets: { status: 503, message: 'gateway down' }, prefs: 'headers' });
    const controller = new AbortController();
    await assert.rejects(
        requestWidgetListPayload(apiClient, controller),
        (error) => error.status === 503 && !isTimeout(error),
    );
    assert.equal(controller.signal.aborted, true, 'the stalled sibling must be cancelled');
    assert.ok(seen.some((entry) => entry.key === 'prefs'));
});

test('the timer is cleared on success: a later tick cannot abort a settled read', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'ok', prefs: 'ok' });
    const controller = new AbortController();
    const [data, prefs] = await requestWidgetListPayload(apiClient, controller);
    assert.ok(data.ui_tabs);
    assert.ok(prefs);
    t.mock.timers.tick(DEADLINE * 4);
    await settle();
    assert.equal(controller.signal.aborted, false, 'a cleared deadline must not fire later');
});

test('an explicit retry runs a fresh request with a fresh deadline', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'headers', prefs: 'ok' });
    const first = new AbortController();
    const failing = requestWidgetListPayload(apiClient, first);
    let firstOutcome = null;
    failing.then(() => {}, (error) => { firstOutcome = error; });
    t.mock.timers.tick(DEADLINE);
    await settle();
    assert.ok(isTimeout(firstOutcome));

    installFetch({ widgets: 'ok', prefs: 'ok' });
    const second = new AbortController();
    const retried = requestWidgetListPayload(apiClient, second);
    let retryOutcome = null;
    retried.then((value) => { retryOutcome = value; }, (error) => { retryOutcome = error; });
    t.mock.timers.tick(DEADLINE - 1);
    await settle();
    assert.ok(Array.isArray(retryOutcome) && retryOutcome[0].ui_tabs,
        'the retry must not inherit the previous generation deadline');
    assert.equal(second.signal.aborted, false);
});

test('navigating away aborts the owned request without a timeout verdict', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'headers', prefs: 'headers' });
    const controller = new AbortController();
    const pending = requestWidgetListPayload(apiClient, controller);
    let outcome = null;
    pending.then(() => {}, (error) => { outcome = error; });
    t.mock.timers.tick(10);
    controller.abort();                       // page hide / disposal
    await settle();
    assert.ok(outcome, 'the abort must settle the request');
    assert.equal(outcome.name, 'AbortError');
    assert.ok(!isTimeout(outcome), 'a disposal abort is not a timeout report');
});

// Scope-review finding on the merged head: a navigation abort that lands AFTER
// the headers (body still streaming) used to resolve through fetchJson as a
// `{error}` object, which the hidden-page reconcile read as an EMPTY authoritative
// list and stopped kept-running frames on. A cancellation must stay a rejection.
test('a navigation abort after headers is a rejection, never an empty list', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    installFetch({ widgets: 'body' });
    const controller = new AbortController();
    const pending = requestWidgetCards(apiClient, controller);
    let outcome = null;
    pending.then((value) => { outcome = { value }; }, (error) => { outcome = { error }; });
    t.mock.timers.tick(10);
    controller.abort();                       // leaving the Widgets page
    await settle();
    assert.ok(outcome && outcome.error, 'the post-header abort must reject, not resolve');
    assert.equal(outcome.error.name, 'AbortError');
    assert.ok(!isTimeout(outcome.error));
});

test('a cards reply without an ui_tabs array is refused, never read as no cards', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    globalThis.fetch = () => Promise.resolve({ ok: true, status: 200, json: async () => ({ error: 'non-json response (HTTP 200)' }) });
    const controller = new AbortController();
    let outcome = null;
    requestWidgetCards(apiClient, controller).then((value) => { outcome = { value }; }, (error) => { outcome = { error }; });
    await settle();
    assert.ok(outcome && outcome.error, 'a malformed list must be an error');
    assert.equal(outcome.error.code, 'WIDGET_LIST_MALFORMED');
    assert.ok(!isTimeout(outcome.error));
});
