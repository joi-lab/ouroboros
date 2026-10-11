import assert from 'node:assert/strict';
import test from 'node:test';

import { WS } from '../modules/ws.js';

// Pure listener-registry semantics (no DOM, no socket): the WS constructor is
// DOM-free and on()/emit() touch only `this.listeners`.

function makeWs() {
    return new WS('ws://unused');
}

test('on() returns an unsubscribe function that removes exactly that listener', () => {
    const ws = makeWs();
    const calls = [];
    const offA = ws.on('evt', () => calls.push('a'));
    ws.on('evt', () => calls.push('b'));
    assert.equal(typeof offA, 'function');
    ws.emit('evt');
    assert.deepEqual(calls, ['a', 'b']);
    offA();
    ws.emit('evt');
    assert.deepEqual(calls, ['a', 'b', 'b']);
    // Disposing twice is a harmless no-op.
    offA();
    ws.emit('evt');
    assert.deepEqual(calls, ['a', 'b', 'b', 'b']);
});

test('listeners fire in insertion order', () => {
    const ws = makeWs();
    const calls = [];
    ws.on('evt', () => calls.push(1));
    ws.on('evt', () => calls.push(2));
    ws.on('evt', () => calls.push(3));
    ws.emit('evt');
    assert.deepEqual(calls, [1, 2, 3]);
});

test('disposing a later listener during emit does not skip the others', () => {
    const ws = makeWs();
    const calls = [];
    let offC = null;
    ws.on('evt', () => {
        calls.push('a');
        offC();  // remove a not-yet-called neighbor mid-emit
    });
    ws.on('evt', () => calls.push('b'));
    offC = ws.on('evt', () => calls.push('c'));
    ws.emit('evt');
    // Snapshot iteration: b still fires; the already-snapshotted c fires too,
    // but is gone from the NEXT emit.
    assert.deepEqual(calls, ['a', 'b', 'c']);
    ws.emit('evt');
    assert.deepEqual(calls, ['a', 'b', 'c', 'a', 'b']);
});

test('a listener added during emit does not fire in that emit', () => {
    const ws = makeWs();
    const calls = [];
    ws.on('evt', () => {
        calls.push('first');
        if (calls.length === 1) ws.on('evt', () => calls.push('late'));
    });
    ws.emit('evt');
    assert.deepEqual(calls, ['first']);
    ws.emit('evt');
    assert.deepEqual(calls, ['first', 'first', 'late']);
});

test('subscribing the same function twice collapses to a single call', () => {
    const ws = makeWs();
    let count = 0;
    const fn = () => { count += 1; };
    const off1 = ws.on('evt', fn);
    const off2 = ws.on('evt', fn);
    ws.emit('evt');
    assert.equal(count, 1);
    // Either disposer removes the single registration.
    off2();
    ws.emit('evt');
    assert.equal(count, 1);
    off1();
    ws.emit('evt');
    assert.equal(count, 1);
});

test('listener errors propagate to the emitter', () => {
    const ws = makeWs();
    ws.on('evt', () => { throw new Error('boom'); });
    assert.throws(() => ws.emit('evt'), /boom/);
});

test('emit with data passes the payload to every listener', () => {
    const ws = makeWs();
    const seen = [];
    ws.on('evt', (data) => seen.push(data));
    ws.on('evt', (data) => seen.push(data));
    const payload = { type: 'evt', value: 42 };
    ws.emit('evt', payload);
    assert.deepEqual(seen, [payload, payload]);
});
