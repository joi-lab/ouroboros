import test from 'node:test';
import assert from 'node:assert/strict';
import { initEvolution } from '../modules/evolution.js';
import { WS } from '../modules/ws.js';

const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

test('Evolution repaints mounted neutral chrome and releases its subscriptions', async () => {
    const saved = Object.fromEntries(['document', 'window', 'fetch', 'Chart', 'getComputedStyle'].map(k => [k, globalThis[k]]));
    const nodes = new Map();
    const el = () => Object.assign(new EventTarget(), { appendChild() {}, getContext: () => ({}) });
    const document = Object.assign(el(), {
        createElement: el, documentElement: {}, hidden: false,
        getElementById: id => { if (!nodes.has(id)) nodes.set(id, el()); return nodes.get(id); },
    });
    const window = new EventTarget();
    let text = '#a8b0bd';
    const charts = [];
    globalThis.document = document;
    globalThis.window = window;
    globalThis.getComputedStyle = () => ({ getPropertyValue: key => key === '--chart-text' ? text : '#999999' });
    globalThis.fetch = async url => Response.json(String(url).includes('evolution-data')
        ? { points: [{ tag: 'v1', date: '2026-09-18', code_lines: 100 }] } : {});
    globalThis.Chart = class {
        constructor(ctx, config) {
            this.options = config.options; this.data = config.data;
            this.scales = Object.fromEntries(Object.entries(config.options.scales).map(([id, options]) => [id, { options }]));
            this.updates = []; charts.push(this);
        }
        update(mode) { this.updates.push(mode); }
        destroy() { this.destroyed = true; }
    };
    try {
        const ws = new WS('ws://unused');
        const dispose = initEvolution({ ws, mount: el(), state: { activePage: 'dashboard', dashboardActiveSubtab: 'evolution' } });
        ws.emit('open');
        await flush();
        assert.equal(charts.length, 1);
        const chart = charts[0];
        const data = chart.data;
        text = '#4a515d';
        window.dispatchEvent(new Event('ouro:theme-changed'));
        assert.equal(chart.options.plugins.legend.labels.color, text);
        assert.equal(chart.scales.x.options.ticks.color, text);
        assert.equal(chart.scales.y.options.ticks.color, '#999999');
        assert.equal(chart.data, data);
        dispose(); dispose();
        assert.equal(chart.destroyed, true);
        assert.equal(ws.listeners.open.size, 0);
        const updates = chart.updates.length;
        window.dispatchEvent(new Event('ouro:theme-changed'));
        ws.emit('open');
        await flush();
        assert.equal(chart.updates.length, updates);
        assert.equal(charts.length, 1);
    } finally { Object.assign(globalThis, saved); }
});
