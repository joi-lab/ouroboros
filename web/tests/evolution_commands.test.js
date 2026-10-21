import assert from 'node:assert/strict';
import test from 'node:test';
import { initEvolution } from '../modules/evolution.js';
import { WS } from '../modules/ws.js';

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

function element(doc) {
    const node = Object.assign(new EventTarget(), {
        children: [], attributes: {}, disabled: false, textContent: '', className: '',
        appendChild(child) { node.children.push(child); return child; },
        setAttribute(key, value) { node.attributes[key] = String(value); },
        classList: { add() {} },
        focus() { doc.activeElement = node; },
    });
    return node;
}

test('Pause stays disabled while its command is in flight, a refused send is said, and light mode keeps Start off', async (t) => {
    t.mock.timers.enable({ apis: ['setTimeout'] });  // the toast's own dismissal timer
    const saved = Object.fromEntries(['document', 'window', 'fetch'].map((key) => [key, globalThis[key]]));
    const nodes = new Map();
    const doc = Object.assign(new EventTarget(), { documentElement: {}, hidden: false });
    Object.assign(doc, {
        createElement: () => element(doc), body: element(doc),
        getElementById: (id) => { if (!nodes.has(id)) nodes.set(id, element(doc)); return nodes.get(id); },
    });
    const commands = [];
    let answer;
    globalThis.document = doc;
    globalThis.window = new EventTarget();
    globalThis.fetch = async (url, init) => {
        if (String(url) === '/api/command') {
            commands.push(JSON.parse(init.body).cmd);
            return new Promise((resolve) => { answer = resolve; });
        }
        return Response.json(String(url).includes('evolution-data') ? { points: [] } : { runtime_mode: 'light' });
    };
    let dispose;
    try {
        const ws = new WS('ws://unused');
        dispose = initEvolution({ ws, mount: element(doc), state: { activePage: 'dashboard', dashboardActiveSubtab: 'evolution' } });
        ws.emit('open');
        await flush();
        const start = nodes.get('evo-start');
        const pause = nodes.get('evo-stop');
        assert.equal(start.disabled, true, 'light mode blocks a campaign start');
        assert.equal(pause.disabled, false);

        pause.focus();
        pause.dispatchEvent(new Event('click'));
        await flush();
        assert.deepEqual([pause.disabled, start.disabled], [true, true], 'both controls wait for the gateway');
        pause.dispatchEvent(new Event('click'));
        await flush();
        assert.deepEqual(commands, ['/evolve off'], 'a second press while in flight sends nothing');

        doc.activeElement = doc.body;  // what a browser does when the pressed button is disabled
        answer(Response.json({ error: 'Complete provider setup before sending this command.' }, { status: 409 }));
        await flush();
        assert.equal(pause.disabled, false);
        assert.equal(start.disabled, true, 'light mode still blocks Start after the command settles');
        assert.equal(doc.activeElement, pause, 'the pressed control gets keyboard focus back');
        const [toast] = nodes.get('toast-stack').children;
        assert.equal(toast.textContent, 'Could not send /evolve off: Complete provider setup before sending this command.');
        assert.equal(toast.attributes.role, 'alert');
    } finally {
        dispose?.();
        Object.assign(globalThis, saved);
    }
});
