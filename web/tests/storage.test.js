import assert from 'node:assert/strict';
import test from 'node:test';
import {
    announceSentence,
    barSegments,
    durationLabel,
    formatBytes,
    groupRows,
    placeRows,
    renderStorage,
    storageState,
    whenLabel,
} from '../modules/storage.js';

// renderStorage escapes text through document.createElement, as a browser's innerHTML does.
globalThis.document = globalThis.document || {
    createElement: () => ({
        set textContent(value) {
            this.innerHTML = String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    }),
};

const GB = 1e9;
const place = (id, group, bytes, overrides = {}) => ({
    id, group, roots: [`/data/${id}`], present: true, same_volume: true, bytes, logical_bytes: bytes, files: 10,
    unreadable: 0, parts: [], rest: { count: 0, bytes: 0 }, ...overrides,
});
const volume = { free_bytes: 170 * GB, total_bytes: 4000 * GB, observed_at: '2026-10-17T15:40:12Z' };
const count = (overrides = {}) => ({
    started_at: '2026-10-17T15:21:40Z', finished_at: '2026-10-17T15:40:12Z', duration_seconds: 1112, io_priority: 'utility',
    basis: 'allocated', bytes: 400 * GB, logical_bytes: 390 * GB, files: 3_000_000, unreadable: 0, volume,
    places: [
        place('model_calls', 'records', 156 * GB, { growth_bytes: 20 * GB, parts: [{ name: 'blobs', bytes: 151 * GB, files: 9 }, { name: 'calls', bytes: 5 * GB, files: 1 }], rest: { count: 3, bytes: 2 * GB } }),
        place('engine', 'engine', 146 * GB, { growth_bytes: 4 * GB, parts: [{ name: 'projects', bytes: 76 * GB, files: 1 }, { name: 'profiles', bytes: 59 * GB, files: 1 }] }),
        place('task_results', 'work', 48 * GB, { growth_bytes: 18 * GB }),
        place('archives', 'work', 16 * GB, { growth_bytes: 0 }),
        place('deliverables', 'work', 0, { present: false }),
        place('work_folders', 'temporary', 7 * GB, { growth_bytes: 2 * GB }),
        place('working_copies', 'temporary', 27 * GB, { growth_bytes: 5 * GB, same_volume: false }),
        place('app_state', 'other', 0, { bytes: null, unreadable: 1, growth_bytes: null, growth_flag: 'unreadable' }),
        place('program', 'other', 0.4 * GB),
    ],
    growth_bytes: 52 * GB, growth_since: '2026-10-16T15:10:05Z',
    ...overrides,
});
const free = { state: 'idle', plan: null, started_at: null, finished_at: null, done: 0, total: 0, groups: [], run: null, error: null };
const response = (overrides = {}) => ({ count: null, counting: null, error: null, volume, retention_days: 7, free, ...overrides });
const counting = (overrides = {}) => ({
    started_at: '2026-10-17T16:05:10Z', place: 'engine', current: 'projects', places_done: 1, places_total: 12,
    bytes: 182 * GB, files: 1_200_000, done: [{ name: 'model_calls', bytes: 156 * GB, files: 9 }], ...overrides,
});
const NOW = new Date('2026-10-17T18:00:00Z');
const AVOIDED = /At least|Excluded|System data|Junk|Clean\b|Prune|%/;

test('storage sizes keep neighbouring rows distinguishable, up to the disk itself', () => {
    assert.equal(formatBytes(null), '');
    assert.equal(formatBytes(999), '999 bytes');
    assert.equal(formatBytes(1000), '1.0 KB');
    assert.equal(formatBytes(1.4e6), '1.4 MB');
    assert.equal(formatBytes(12.3e6), '12 MB');
    assert.equal(formatBytes(999_960), '1.0 MB', 'the unit follows the rounded value');
    assert.equal(formatBytes(2.4e9), '2.4 GB');
    assert.equal(formatBytes(4000 * GB), '4.0 TB');
    assert.equal(durationLabel(1112), '19 min');
    assert.equal(durationLabel(4.2), '4.2 s');
    assert.equal(durationLabel(7200), '2 h 0 min');
});

test('dates and times keep one format inside the English sentence, never the browser locale', () => {
    const then = new Date(2026, 9, 16, 18, 10);
    assert.equal(whenLabel(then.toISOString(), NOW), '16 Oct at 18:10');
    const today = new Date(NOW.getFullYear(), NOW.getMonth(), NOW.getDate(), 9, 5);
    assert.equal(whenLabel(today.toISOString(), NOW), 'today at 09:05');
    assert.equal(whenLabel('', NOW), '');
});

test('places keep server order under their five groups; an absent place is omitted; unknown is never zero', () => {
    const rows = placeRows(count(), 7);
    assert.deepEqual(rows.map((row) => row.id), ['model_calls', 'engine', 'task_results', 'archives', 'work_folders', 'working_copies', 'app_state', 'program']);
    assert.equal(rows.find((row) => row.id === 'work_folders').why, 'Removed by the app’s own cleanup 7 days after a task finishes.');
    assert.equal(rows.find((row) => row.id === 'app_state').bytes, null);
    assert.equal(rows.find((row) => row.id === 'working_copies').otherDisk, true);
    assert.match(rows.find((row) => row.id === 'engine').split, /^Run history 76 GB · Account data 59 GB · Engine files 11 GB$/);
    // The split reads its two parts by name, wherever the server ranks them among the listed parts.
    const ranked = [...Array(8)].map((_, index) => ({ name: `copy-${index}`, bytes: 1 * GB, files: 1 }));
    const engine = place('engine', 'engine', 146 * GB, { parts: [...ranked, { name: 'profiles', bytes: 59 * GB, files: 1 }, { name: 'projects', bytes: 76 * GB, files: 1 }] });
    assert.match(placeRows(count({ places: [engine] }), 7)[0].split, /^Run history 76 GB · Account data 59 GB · Engine files 11 GB$/);
    const groups = groupRows(rows);
    assert.deepEqual(groups.map((group) => [group.id, group.bytes, group.growth]), [
        ['records', 156 * GB, 20 * GB], ['engine', 146 * GB, 4 * GB], ['work', 64 * GB, 18 * GB],
        ['temporary', 34 * GB, 7 * GB], ['other', 0.4 * GB, null],
    ]);
    assert.equal(groups.find((group) => group.id === 'other').caveat, true, 'an unreadable member flags its group');
    assert.match(rows.find((row) => row.id === 'working_copies').why, /Removed 7 days after they were made, once their task has finished\.$/);
    // A group whose only member could not be listed has no size, never zero.
    const unknownOnly = groupRows(placeRows(count({ places: [place('app_state', 'other', null, { unreadable: 1 })] }), 7));
    assert.deepEqual(unknownOnly.map((group) => [group.id, group.bytes]), [['other', null]]);
    const { places: html } = renderStorage(response({ count: count({ places: [place('app_state', 'other', null, { unreadable: 1 })] }) }), { now: NOW });
    assert.match(html, /storage-group-head[\s\S]*storage-place-size is-unknown"[^>]*><\/span>/);
    assert.doesNotMatch(html, /0 bytes/);
    assert.deepEqual(barSegments(count({ places: [place('app_state', 'other', null, { unreadable: 1 })] })), []);
});

test('the live region carries one sentence per phase, never the progress figures', () => {
    const tray = { checked: new Set(), dismissed: false };
    assert.equal(announceSentence(null), '');
    assert.equal(announceSentence(response(), { now: NOW }), 'Not counted yet.');
    assert.equal(announceSentence(response({ count: count() }), { now: NOW }), 'Ouroboros uses 400 GB.');
    const early = announceSentence(response({ counting: counting() }), { now: NOW });
    const later = announceSentence(response({ counting: counting({ current: 'profiles', bytes: 190 * GB, places_done: 2 }) }), { now: NOW });
    assert.equal(early, 'Measuring. You can leave this tab.');
    assert.equal(later, early, 'a second of progress changes nothing a screen reader hears');
    assert.match(announceSentence(response({ count: count(), error: { at: 'x', message: 'Stopped before it finished.', stopped: true } }), { now: NOW }), /^Stopped before it finished\. Showing the count from today at \d\d:\d\d\.$/);
    assert.equal(announceSentence(response({ count: count(), free: { ...free, state: 'listing', total: 3 } }), { now: NOW, tray }), 'Ouroboros uses 400 GB. Checking what can go. Nothing is removed.');
    assert.equal(announceSentence(response({ count: count(), free: { ...free, state: 'running', total: 3, run: {} } }), { now: NOW, tray }), 'Ouroboros uses 400 GB. Freeing space. You can leave this tab.');
    assert.equal(announceSentence(response({ free: { ...free, state: 'failed', error: 'x' } }), { now: NOW, tray }), 'Not counted yet. Temporary space could not be listed. Nothing was removed.');
    assert.equal(announceSentence(response(), { readFailed: true, now: NOW }), 'Storage status could not be read. Not counted yet.');
    assert.equal(renderStorage(response({ counting: counting() }), { now: NOW }).announce, early);
});

test('the bar exists only for a finished count, its five shares sum to 100 and stay while a new count runs', () => {
    assert.deepEqual(barSegments(null), []);
    const segments = barSegments(count());
    assert.deepEqual(segments.map((segment) => segment.group), ['records', 'engine', 'work', 'temporary', 'other']);
    assert.equal(Math.round(segments.reduce((sum, segment) => sum + segment.share, 0)), 100);
    const never = renderStorage(response(), { now: NOW });
    assert.equal(never.places, '', 'no bar and no list before a finished count');
    assert.equal(renderStorage(response({ counting: counting() }), { now: NOW }).places, '', 'a first count sums nothing into a headline');
    const counted = renderStorage(response({ count: count() }), { now: NOW });
    assert.match(counted.places, /class="storage-bar" role="img" aria-label="Share of each group; sizes are listed below"/);
    assert.equal((counted.places.match(/storage-bar-seg/g) || []).length, 5);
    assert.doesNotMatch(counted.places, /is-unmeasured|title=/);
    const recount = renderStorage(response({ count: count(), counting: counting() }), { now: NOW });
    assert.equal(recount.places, counted.places, 'the saved bar and list stay exactly as they were');
    const stopped = renderStorage(response({ count: count(), error: { at: 'x', message: 'Stopped before it finished.', stopped: true } }), { now: NOW });
    assert.equal(stopped.places, counted.places);
});

test('the summary names the total, the free space, the growth and the unreadable items', () => {
    const view = renderStorage(response({ count: count({ unreadable: 12, growth_flag: 'unreadable' }) }), { now: NOW });
    assert.match(view.summary, /Ouroboros uses[\s\S]*400 GB/);
    assert.match(view.summary, /170 GB free of 4\.0 TB on the disk the data folder is on/);
    assert.match(view.summary, /Last count: today at \d\d:\d\d · took 19 min · \+52 GB since 16 Oct at \d\d:\d\d \(some items could not be read\)/);
    assert.match(view.summary, /12 items could not be read/);
    assert.deepEqual(view.measure, { label: 'Measure again', busy: false });
    assert.equal(view.stop, false);
    const flat = renderStorage(response({ count: count({ growth_bytes: 0 }) }), { now: NOW });
    assert.match(flat.summary, /no change since 16 Oct/);
    const less = renderStorage(response({ count: count({ growth_bytes: -3 * GB }) }), { now: NOW });
    assert.match(less.summary, /−3\.0 GB since 16 Oct/);
    const one = renderStorage(response({ count: count({ unreadable: 1 }) }), { now: NOW });
    assert.match(one.summary, /1 item could not be read/);
});

test('rows carry their size, growth, reason and the largest parts; growth never shows for a missing or zero delta', () => {
    const { places } = renderStorage(response({ count: count() }), { now: NOW });
    const row = (id) => places.split(/(?=data-storage-place=")/).find((chunk) => chunk.startsWith(`data-storage-place="${id}"`));
    assert.match(row('model_calls'), /156 GB[\s\S]*\+20 GB[\s\S]*The exact prompts and answers behind every decision\. Kept; this screen cannot delete them\./);
    assert.match(row('model_calls'), /<summary>Largest parts<\/summary>[\s\S]*blobs[\s\S]*151 GB[\s\S]*and 3 more, 2\.0 GB/);
    assert.match(row('engine'), /Run history 76 GB · Account data 59 GB · Engine files 11 GB/);
    assert.match(row('archives'), /16 GB/);
    assert.doesNotMatch(row('archives'), /\+0|−0/);
    assert.match(row('working_copies'), /on another disk/);
    assert.match(row('app_state'), /storage-place-size is-unknown"[^>]*><\/span>[\s\S]*Could not be read\./);
    assert.match(row('program'), /Ouroboros program[\s\S]*The installed app and its history\. Updated by the app\./);
    assert.doesNotMatch(places, AVOIDED);
    assert.doesNotMatch(renderStorage(response({ count: count() }), { now: NOW }).summary, AVOIDED);
});

test('a running count shows its progress sentence, Stop and the measure action busy; a first count lists finished places', () => {
    const first = renderStorage(response({ counting: counting() }), { now: NOW });
    assert.match(first.summary, /Not counted yet/);
    assert.match(first.summary, /Measuring Agent engine · projects · 182 GB so far · 2 of 12 places\. You can leave this tab\./);
    // The work that runs now is the shared moving mark in the line's ink (web/modules/status_mark.js).
    assert.match(first.summary, /<p class="storage-progress"[^>]*><span class="status-mark" aria-hidden="true" data-shape="moving" data-hue="ink"><i><\/i><i><\/i><i><\/i><\/span><span>Measuring/);
    assert.doesNotMatch(first.summary, /chat-live-typing/);
    assert.match(first.summary, /storage-first-count[\s\S]*Model call records[\s\S]*156 GB/);
    assert.deepEqual(first.measure, { label: 'Measuring…', busy: true });
    assert.equal(first.stop, true);
    const recount = renderStorage(response({ count: count(), counting: counting() }), { now: NOW });
    assert.match(recount.summary, /400 GB[\s\S]*Last count:[\s\S]*Measuring Agent engine/);
    assert.doesNotMatch(recount.summary, /storage-first-count/);
    assert.deepEqual(renderStorage(response(), { pending: true }).measure, { label: 'Measuring…', busy: true });
});

test('a stopped or failed count never replaces the saved one and says so; a first failure reads "Could not be counted"', () => {
    const stopped = renderStorage(response({ count: count(), error: { at: 'x', message: 'Stopped before it finished.', stopped: true } }), { now: NOW });
    assert.match(stopped.summary, /400 GB[\s\S]*data-tone="warn"[^>]*>Stopped before it finished\. Showing the count from today at \d\d:\d\d\./);
    assert.equal(stopped.measure.label, 'Measure again');
    const failed = renderStorage(response({ count: count(), error: { at: 'x', message: 'The count failed: OSError.', stopped: false } }), { now: NOW });
    assert.match(failed.summary, /This count stopped: The count failed: OSError\. Showing the count from today/);
    const nothing = renderStorage(response({ error: { at: 'x', message: 'The data folder could not be found.', stopped: false } }), { now: NOW });
    assert.match(nothing.summary, /Could not be counted[\s\S]*This count stopped: The data folder could not be found\./);
    assert.equal(nothing.measure.label, 'Try again');
    const stoppedFirst = renderStorage(response({ error: { at: 'x', message: 'Stopped before it finished.', stopped: true } }), { now: NOW });
    assert.match(stoppedFirst.summary, /Not counted yet[\s\S]*Stopped before it finished\.</);
    assert.equal(stoppedFirst.measure.label, 'Measure now');
    assert.equal(storageState(null), 'loading');
    assert.equal(storageState(response()), 'never');
    assert.equal(storageState(response({ counting: counting() })), 'counting');
});

test('before any count the empty state names what Measure does; a failed read keeps the last answer', () => {
    const never = renderStorage(response(), { now: NOW });
    assert.match(never.summary, /Not counted yet[\s\S]*Measuring reads file sizes only and changes nothing\. It can take a while on a large data folder\./);
    assert.deepEqual(never.measure, { label: 'Measure now', busy: false });
    assert.match(renderStorage(null).summary, /Loading…/);
    assert.match(renderStorage(null, { readFailed: true }).summary, /Could not be read/);
    const stale = renderStorage(response({ count: count() }), { readFailed: true, now: NOW });
    assert.match(stale.summary, /400 GB[\s\S]*Storage status could not be read\. Showing the last answer\./);
});

test('the second card carries the tray whenever an answer exists, idle before any count', () => {
    const never = renderStorage(response(), { now: NOW });
    assert.match(never.tray, /Temporary space[\s\S]*Show what can go/);
    assert.equal(renderStorage(null).tray, '');
    const counted = renderStorage(response({ count: count() }), { now: NOW, tray: { checked: new Set(), dismissed: false } });
    assert.match(counted.tray, /Copies the app already removes on its own/);
    assert.doesNotMatch(counted.tray, AVOIDED);
});

test('titles and accessible names are fixed chrome: no size or name is minted per count', () => {
    // The language overlay translates every title and aria-label by its English text, so a value
    // that changes with the count would be a new miss each time.
    const attributes = (view) => [...(view.summary + view.places).matchAll(/\s(title|aria-label)="([^"]*)"/g)].map(([, name, value]) => `${name}=${value}`);
    const small = attributes(renderStorage(response({ count: count() }), { now: NOW }));
    const large = attributes(renderStorage(response({ count: count({ bytes: 9e12, places: count().places.map((row) => ({ ...row, bytes: row.bytes === null ? null : row.bytes * 23 })) }) }), { now: NOW }));
    assert.deepEqual(small, ['aria-label=Share of each group; sizes are listed below']);
    assert.deepEqual(large, small);
    for (const entry of small) assert.doesNotMatch(entry, /\d/, entry);
});
