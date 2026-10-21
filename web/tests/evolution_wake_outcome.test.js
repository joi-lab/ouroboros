import assert from 'node:assert/strict';
import test from 'node:test';
import { initEvolution, runtimePillTone } from '../modules/evolution.js';
import { applyPayload } from '../modules/i18n.js';
import { WS } from '../modules/ws.js';

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

test('enabled consciousness keeps historical pause and unknown wake outcomes visible instead of off', async () => {
    const saved = Object.fromEntries(['document', 'window', 'fetch'].map(key => [key, globalThis[key]]));
    const nodes = new Map();
    const element = () => Object.assign(new EventTarget(), { appendChild() {} });
    globalThis.document = Object.assign(element(), { createElement: element, documentElement: {}, hidden: false,
        getElementById: id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); } });
    globalThis.window = new EventTarget();
    let status = 'wake_paused';
    const details = {
        wake_paused: 'The last wake-up returned while paused; its task card shows the current state. Next wake check at 18:00.',
        wake_outcome_unknown: "The last wake-up's outcome is unconfirmed; next check at 18:00.",
    };
    globalThis.fetch = async url => Response.json(String(url).includes('evolution-data') ? { points: [] } : {
        bg_consciousness_enabled: status !== 'disabled', bg_consciousness_state: { status, detail: details[status] || '' },
    });
    let dispose;
    try {
        const ws = new WS('ws://unused');
        dispose = initEvolution({ ws, mount: element(), state: { activePage: 'dashboard', dashboardActiveSubtab: 'evolution' } });
        // A paused or unknown wake-up goes on by itself: the plain pill, never the owner's amber.
        for (const [next, label, tone] of [['wake_paused', 'paused', 'offline'],
            ['wake_outcome_unknown', 'unknown', 'offline'], ['sleeping', 'sleeping', 'online'], ['disabled', 'off', 'offline']]) {
            status = next;
            ws.emit('open');
            await flush();
            assert.equal(nodes.get('evo-bg-pill').textContent, `Consciousness ${label}`);
            assert.equal(nodes.get('evo-bg-pill').className, `evo-runtime-pill ${tone}`);
            if (details[status]) assert.ok(nodes.get('evo-runtime-detail').textContent.includes(details[status]), 'historical wording stays intact');
        }
        applyPayload({ language: 'ru', english: false, entries: {
            'code:consciousness.wake_paused': { text: 'приостановлено', provenance: 'imported' },
            'code:consciousness.wake_outcome_unknown': { text: 'исход неизвестен', provenance: 'imported' },
        } });
        for (const [next, label] of [['wake_paused', 'приостановлено'], ['wake_outcome_unknown', 'исход неизвестен']]) {
            status = next; ws.emit('open'); await flush();
            assert.equal(nodes.get('evo-bg-pill').textContent, `Consciousness ${label}`);
        }
    } finally { dispose?.(); applyPayload({ language: 'en', english: true }); Object.assign(globalThis, saved); }
});

test("a runtime pill is amber only for the owner's move: a first conversation, the owner's chat, a spending stop", () => {
    const tones = {
        online: ['running', 'queued', 'idle_ready', 'thinking', 'sleeping'],
        owner: ['waiting_for_first_conversation', 'waiting_for_owner_chat', 'budget_blocked', 'budget_stopped'],
        error: ['paused_failures', 'error_backoff', 'allowance_exhausted', 'allowance_unknown', 'wake_failed', 'wake_rejected'],
        // What goes on by itself, or is off, keeps the plain pill.
        offline: ['waiting_for_idle', 'waiting_for_restart_verify', 'paused', 'starting', 'state_unknown', 'unknown',
            'wake_paused', 'wake_outcome_unknown', 'disabled', 'stopped', ''],
    };
    for (const [tone, statuses] of Object.entries(tones)) {
        for (const status of statuses) assert.equal(runtimePillTone(status), tone, status);
    }
});
