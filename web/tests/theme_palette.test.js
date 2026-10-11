import test from 'node:test';
import assert from 'node:assert/strict';

import { applyChartTheme, chartChrome, onThemeChange } from '../modules/theme_palette.js';

/* Chart.js is not loaded here: the module only reads `options`/`scales` and calls
   `update`, so a shape-accurate stand-in is enough — and it lets the test watch
   for the one thing that must NEVER happen, a rebuild. */
function fakeChart({ options = {}, scales = {} } = {}) {
    const chart = { options, scales, updates: [] };
    chart.update = (mode) => chart.updates.push(mode);
    return chart;
}

function withTokens(text, grid, run) {
    const priorDocument = globalThis.document;
    const priorStyle = globalThis.getComputedStyle;
    globalThis.document = { documentElement: {} };
    globalThis.getComputedStyle = () => ({
        getPropertyValue: (key) => ({ '--chart-text': text, '--chart-grid': grid }[key] ?? ''),
    });
    try { run(); } finally {
        globalThis.document = priorDocument;
        globalThis.getComputedStyle = priorStyle;
    }
}

test('chrome comes from the tokens, and a client without CSSOM still gets ink', () => {
    withTokens(' #4a515d ', ' rgba(0,0,0,.14) ', () => {
        assert.deepEqual(chartChrome(), { text: '#4a515d', grid: 'rgba(0,0,0,.14)' });
    });
    // No document at all (module loaded outside a page) must not throw.
    assert.equal(typeof chartChrome().text, 'string');
    withTokens('', '', () => {
        assert.equal(chartChrome().text, '#a8b0bd', 'an unset token falls back, not to empty');
    });
});

test('a repaint re-tints chrome and axes in place, keeping the data', () => {
    const x = { options: {} };
    const y = { options: { ticks: { stepSize: 5 } } };
    const chart = fakeChart({
        options: { responsive: true },
        scales: { x, y },
    });
    chart.data = { datasets: [{ data: [1, 2, 3] }] };
    withTokens('#4a515d', '#dddddd', () => applyChartTheme(chart));
    assert.equal(chart.options.color, '#4a515d');
    assert.equal(chart.options.borderColor, '#dddddd');
    assert.equal(chart.options.plugins.legend.labels.color, '#4a515d');
    assert.equal(chart.options.plugins.title.color, '#4a515d');
    assert.equal(y.options.ticks.color, '#4a515d');
    assert.equal(y.options.ticks.stepSize, 5, 'an unrelated scale option is untouched');
    assert.deepEqual([x.options.grid.color, x.options.grid.tickColor], ['#dddddd', '#dddddd']);
    // 'none' is the whole point: the chart is re-tinted, not re-entered.
    assert.deepEqual(chart.updates, ['none']);
    assert.deepEqual(chart.data.datasets[0].data, [1, 2, 3]);
});

test('colours the author actually chose survive a theme switch', () => {
    const authored = { color: '#ff0000', plugins: { legend: { display: false } }, scales: { y: {} } };
    const scale = { options: { ticks: { color: '#00ff00' } } };
    const chart = fakeChart({
        options: { color: '#ff0000', plugins: { legend: { display: false } } },
        scales: { y: scale },
    });
    withTokens('#4a515d', '#dddddd', () => applyChartTheme(chart, authored));
    assert.equal(chart.options.color, '#ff0000');
    assert.equal(chart.options.plugins.legend.display, false);
    assert.equal(chart.options.plugins.legend.labels, undefined, 'declaring plugins claims the group');
    assert.equal(scale.options.ticks.color, '#00ff00');
    // borderColor was NOT authored, so it still follows the theme: the claim is
    // per group, not all-or-nothing.
    assert.equal(chart.options.borderColor, '#dddddd');
});

test('a chart destroyed mid-switch is not an error, and a scale-less type is skipped', () => {
    const doughnut = fakeChart({ options: {}, scales: {} });
    withTokens('#fff', '#000', () => applyChartTheme(doughnut));
    assert.deepEqual(doughnut.updates, ['none']);
    const dead = fakeChart();
    dead.update = () => { throw new Error('Cannot read properties of null'); };
    withTokens('#fff', '#000', () => applyChartTheme(dead));
    // Nothing to assert but the absence of a throw: a view unmounting while the
    // theme changes is ordinary, not a failure the owner should ever see.
    applyChartTheme(null);
    applyChartTheme({});
});

test('the subscription hands back an unsubscribe that really detaches', () => {
    const prior = globalThis.window;
    const listeners = new Map();
    globalThis.window = {
        addEventListener: (key, fn) => listeners.set(key, fn),
        removeEventListener: (key, fn) => { if (listeners.get(key) === fn) listeners.delete(key); },
    };
    try {
        let seen = 0;
        const off = onThemeChange(() => { seen += 1; });
        listeners.get('ouro:theme-changed')();
        assert.equal(seen, 1);
        off();
        assert.equal(listeners.size, 0, 'a disposed view must not keep re-tinting dead charts');
    } finally { globalThis.window = prior; }
});
