import assert from 'node:assert/strict';
import test from 'node:test';
import {
    barSegments,
    coverageSentence,
    formatBytes,
    renderStorage,
    storageBreakdown,
    storageState,
} from '../modules/storage.js';

// renderStorage escapes text through document.createElement, as a browser's innerHTML does.
globalThis.document = globalThis.document || {
    createElement: () => ({
        set textContent(value) {
            this.innerHTML = String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    }),
};

const measurement = (overrides = {}) => ({
    finished_at: '2026-10-05T10:11:12Z',
    duration_seconds: 1.2,
    measured_bytes: 300,
    complete: false,
    categories: [
        { id: 'observability', bytes: 100, files: 2, coverage: 'complete', errors: 0, stopped: '', skipped: 0 },
        { id: 'results', bytes: 200, files: 1, coverage: 'partial', errors: 1, stopped: 'entry_limit', skipped: 0 },
        { id: 'task_drives', bytes: null, files: 0, coverage: 'unreadable', errors: 1, stopped: '', skipped: 0 },
    ],
    not_measured: ['worktrees'],
    ...overrides,
});

const group = (body, id) => body.match(new RegExp(`<section class="storage-group" data-storage-group="${id}"[\\s\\S]*?</section>`))?.[0] || '';

test('storage breakdown keeps unknown categories out of bytes and bars', () => {
    const breakdown = storageBreakdown(measurement());
    assert.equal(breakdown.counted, 300);
    assert.deepEqual(breakdown.groups.map((group) => [group.id, group.bytes]), [['exact', 100], ['kept', 200]]);
    assert.deepEqual(breakdown.unmeasured.map((row) => row.id), ['task_drives', 'worktrees']);
    assert.deepEqual(barSegments(breakdown).map(({ id, share }) => [id, share]), [['observability', 33.33], ['results', 66.67]]);
});

test('storage presentation distinguishes no answer, failure, partial and unknown', () => {
    assert.equal(formatBytes(null), '');
    assert.equal(storageState(null), 'loading');
    assert.equal(storageState({ measuring: false }), 'never');
    assert.equal(storageState({ error: { message: 'offline' } }), 'unavailable');
    assert.match(coverageSentence(measurement()), /partly counted/);
    assert.match(coverageSentence(measurement()), /could not be read/);
});

test('a failed read during a first measurement is said, not shown as a fresh "Measuring"', () => {
    const running = { measuring: true, measurement: null, error: null };
    assert.match(renderStorage(running).summary, /Measuring…[\s\S]*Reading file sizes\. Nothing is changed\./);
    const stale = renderStorage(running, { readFailed: true });
    assert.match(stale.summary, /Measuring…/);
    assert.match(stale.summary, /data-tone="warn">Storage status could not be read\. The last answer was still measuring; checking again\./);
    assert.doesNotMatch(stale.summary, /Reading file sizes/);
    assert.deepEqual(stale.action, { label: 'Measuring…', busy: true }, 'the running walk is not offered again');
    // A POST in flight is its own fresh answer; an earlier failed read does not qualify it.
    assert.doesNotMatch(renderStorage(null, { pending: true, readFailed: true }).summary, /could not be read/);
});

test('storage sizes keep neighbouring rows distinguishable', () => {
    assert.equal(formatBytes(999), '999 bytes');
    assert.equal(formatBytes(1000), '1.0 KB');
    assert.equal(formatBytes(1.0e6), '1.0 MB');
    assert.equal(formatBytes(1.4e6), '1.4 MB');
    assert.equal(formatBytes(12.3e6), '12 MB');
    assert.equal(formatBytes(999_960), '1.0 MB', 'the unit follows the rounded value');
    assert.equal(formatBytes(2.4e9), '2.4 GB');
});

test('a group with an unreadable member is only a lower bound', () => {
    const view = measurement({
        measured_bytes: 200,
        categories: [
            { id: 'results', bytes: 200, files: 1, coverage: 'complete', errors: 0, stopped: '', skipped: 0 },
            { id: 'archives', bytes: null, files: 0, coverage: 'unreadable', errors: 1, stopped: '', skipped: 0 },
        ],
        not_measured: [],
    });
    const kept = storageBreakdown(view).groups.find((entry) => entry.id === 'kept');
    assert.equal(kept.partial, true);
    const { body, summary } = renderStorage({ measuring: false, error: null, measurement: view });
    assert.match(group(body, 'kept'), /At least 200 bytes/);
    assert.match(group(body, 'unmeasured'), /Archives and backups[\s\S]*Could not be read\./);
    assert.match(summary, /Measured data[\s\S]*At least 200 bytes/);
});

test('the unknown marker stays for a partly counted picture, and its row says why', () => {
    const view = measurement({
        measured_bytes: 300,
        categories: [
            { id: 'observability', bytes: 100, files: 2, coverage: 'complete', errors: 0, stopped: '', skipped: 0 },
            { id: 'results', bytes: 200, files: 1, coverage: 'partial', errors: 0, stopped: '', skipped: 2 },
        ],
        not_measured: [],
    });
    const { body } = renderStorage({ measuring: false, error: null, measurement: view });
    assert.match(body, /class="storage-bar-seg is-unmeasured" title="No measured size"/);
    assert.match(group(body, 'kept'), /2 items were not counted; the complete size is unknown\./);
    const complete = renderStorage({ measuring: false, error: null, measurement: measurement({
        complete: true, measured_bytes: 100, not_measured: [],
        categories: [{ id: 'observability', bytes: 100, files: 2, coverage: 'complete', errors: 0, stopped: '', skipped: 0 }],
    }) });
    assert.doesNotMatch(complete.body, /is-unmeasured/);
});

test('titles and accessible names are fixed chrome: no size or name list is minted per measurement', () => {
    // The language overlay translates every title and aria-label by its English text, so a value
    // that changes with the measurement would be a new miss each time.
    const attributes = (view) => [...renderStorage({ measuring: false, error: null, measurement: view }).body
        .matchAll(/\s(title|aria-label)="([^"]*)"/g)].map(([, name, value]) => `${name}=${value}`);
    const small = attributes(measurement());
    const large = attributes(measurement({
        measured_bytes: 3.3e9,
        categories: [
            { id: 'observability', bytes: 1.2e9, files: 9000, coverage: 'complete', errors: 0, stopped: '', skipped: 0 },
            { id: 'results', bytes: 2.1e9, files: 40, coverage: 'partial', errors: 3, stopped: 'time_limit', skipped: 5 },
            { id: 'task_drives', bytes: null, files: 0, coverage: 'unreadable', errors: 1, stopped: '', skipped: 0 },
        ],
        not_measured: ['engine', 'worktrees', 'project_folders'],
    }));
    assert.ok(small.includes('title=No measured size'), small);
    assert.ok(small.some((entry) => entry.startsWith('aria-label=Counted bytes')), small);
    assert.deepEqual(large, small);
    for (const entry of small) assert.doesNotMatch(entry, /\d|At least/, entry);
});

test('excluded locations are named as excluded, wherever they are configured', () => {
    const { body } = renderStorage({ measuring: false, error: null, measurement: measurement({ not_measured: ['engine', 'worktrees', 'project_folders'] }) });
    const excluded = group(body, 'unmeasured');
    assert.match(excluded, /Not measured here/);
    assert.match(excluded, /Excluded location: kept by the agent engine\./);
    assert.match(excluded, /Excluded location, configured separately\./);
    assert.doesNotMatch(body, /Outside the data folder/);
});
