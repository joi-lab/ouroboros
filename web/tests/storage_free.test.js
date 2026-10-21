import assert from 'node:assert/strict';
import test from 'node:test';
import { confirmCopy, doneSentences, freeSum, goLabel, notStarted, sumComplete, trayHtml, trayState } from '../modules/storage_free.js';

// Text is escaped through document.createElement, as a browser's innerHTML does.
globalThis.document = globalThis.document || {
    createElement: () => ({
        set textContent(value) {
            this.innerHTML = String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
    }),
};

const GB = 1e9;
const group = (id, overrides = {}) => ({
    id, count: 1629, inside_wait: 1620, in_use: 11, unreadable: 0, bytes: 6.84 * GB, bytes_complete: true,
    items: [{ id: 'direct:cde295b6', name: 'Run the QA wave on branch…', bytes: 1.966 * GB }], ...overrides,
});
const free = (state, overrides = {}) => ({
    state, plan: 'a1b2', started_at: '2026-10-17T16:10:03Z', finished_at: null, done: 0, total: 0, groups: [], run: null, error: null,
    ...overrides,
});
const both = () => [group('work_folders'), group('working_copies', { count: 284, inside_wait: 0, in_use: 3, bytes: 2.1 * GB,
    items: [{ id: 'plain:t9', name: 'Port the header', bytes: 2.6 * GB }] })];
const run = (overrides = {}) => ({
    groups: ['work_folders', 'working_copies'], selected: 1601, done: 1601, removed: 1584, removed_bytes: 8.2 * GB, deleting: 3,
    deleting_bytes: 0.3 * GB, kept: [{ reason: 'results_pending', count: 11 }, { reason: 'in_use', count: 3 }],
    free_before: 140 * GB, free_after: 148 * GB, stopped: false, error: null, ...overrides,
});
const ui = (checked = ['work_folders', 'working_copies'], dismissed = false) => ({ checked: new Set(checked), dismissed });
const AVOIDED = /At least|Excluded|System data|Junk|Clean\b|Prune|%/;

test('the tray rests with one neutral door, lists with the counts first, and names the sum once ready', () => {
    const rest = trayHtml(free('idle'), ui());
    assert.match(rest, /Temporary space[\s\S]*Copies the app already removes on its own\. Most of Ouroboros is kept on purpose\.[\s\S]*data-free-preview[^>]*>Show what can go</);
    assert.doesNotMatch(rest, /btn-danger/);
    const listing = trayHtml(free('listing', { done: 1240, total: 1661, groups: [group('work_folders', { bytes: 5.1 * GB, bytes_complete: false }), group('working_copies', { bytes: null, bytes_complete: false })] }), ui());
    assert.match(listing, /Checking what can go · 1,240 of 1,661 folders · 5\.1 GB so far\. Nothing is removed\./);
    assert.match(listing, /5\.1 GB so far[\s\S]*listing…/);
    assert.match(listing, /data-free-stop[^>]*>Stop</);
    const ready = trayHtml(free('ready', { groups: both() }), ui());
    assert.match(ready, /8\.9 GB can be freed/);
    assert.match(ready, /Nothing here holds results, messages or model history\./);
    assert.match(ready, /Finished tasks’ work folders[\s\S]*6\.8 GB[\s\S]*Scratch files and helper logs of finished tasks\. Results, files, messages and the exact model history are already saved\. 1,620 of 1,629 folders finished less than 7 days ago\. 11 are in use and stay\./);
    assert.match(ready, /Working copies of finished work[\s\S]*2\.1 GB[\s\S]*Ready-to-use copies of code\. Their results and commits are kept\. 3 are in use and stay\./);
    assert.match(ready, /Largest items \(2 of 1,913\)[\s\S]*Freeing removes every checked group, not only these\.[\s\S]*Port the header[\s\S]*2\.6 GB/);
    assert.match(ready, /Gone for good\. Sizes are what the listed folders occupy; the space freed can be smaller\./);
    assert.match(ready, /data-free-cancel[^>]*>Cancel<[\s\S]*btn-danger[^>]*data-free-go[^>]*>Free 8\.9 GB</);
    assert.equal((ready.match(/btn-danger/g) || []).length, 1, 'one danger capsule, last');
    assert.ok(ready.lastIndexOf('data-free-go') > ready.lastIndexOf('data-free-cancel'));
    assert.equal((ready.match(/type="checkbox"/g) || []).length, 2);
    assert.doesNotMatch(ready, AVOIDED);
});

test('the checkbox column disappears for one offered group; the live sum follows the checked rows', () => {
    const one = trayHtml(free('ready', { groups: [group('work_folders')] }), ui(['work_folders']));
    assert.doesNotMatch(one, /type="checkbox"/);
    assert.match(one, /is-single/);
    assert.match(one, /Free 6\.8 GB/);
    const partial = trayHtml(free('ready', { groups: both() }), ui(['working_copies']));
    assert.match(partial, /Free 2\.1 GB/);
    assert.equal(freeSum(free('ready', { groups: both() }), new Set(['working_copies'])), 2.1 * GB);
    const none = trayHtml(free('ready', { groups: both() }), ui([]));
    assert.match(none, /Nothing selected/);
    assert.doesNotMatch(none, /btn-danger/);
    const dismissed = trayHtml(free('ready', { groups: both() }), ui([], true));
    assert.equal(trayState(free('ready', { groups: both() }), true), 'rest');
    assert.match(dismissed, /Show what can go/);
});

test('an unknown or incomplete size is never zero: the row, the headline, the capsule and the dialog say so', () => {
    const unknown = group('working_copies', { count: 4, inside_wait: 0, in_use: 0, bytes: null, bytes_complete: false, items: [] });
    const partial = group('work_folders', { bytes: 5.1 * GB, bytes_complete: false });
    const alone = trayHtml(free('ready', { groups: [unknown] }), ui(['working_copies']));
    assert.match(alone, /Size unknown/);
    assert.match(alone, /storage-free-size[^>]*>size unknown</);
    assert.match(alone, /btn-danger[^>]*data-free-go[^>]*data-i18n-fmt="storage\.free\.go"[^>]*>Free, size unknown</, 'the folders are selected: the capsule stays');
    assert.doesNotMatch(alone, /Nothing selected|0 bytes|listing…/);
    const mixed = trayHtml(free('ready', { groups: [partial, unknown] }), ui());
    assert.match(mixed, /5\.1 GB or more can be freed/);
    assert.match(mixed, /5\.1 GB \(some could not be read\)/);
    assert.match(mixed, /data-free-go[^>]*>Free 5\.1 GB or more</);
    assert.equal(freeSum(free('ready', { groups: [partial, unknown] })), 5.1 * GB);
    assert.equal(sumComplete(free('ready', { groups: [partial, unknown] })), false);
    assert.equal(sumComplete(free('ready', { groups: both() })), true);
    assert.equal(goLabel(free('ready', { groups: both() }), new Set(['work_folders'])), 'Free 6.8 GB');
    const copy = confirmCopy(free('ready', { groups: [partial, unknown] }), new Set(['work_folders', 'working_copies']));
    assert.match(copy.body, /^This removes the checked folders for good; some of their sizes could not be read\. Results, messages/);
    assert.deepEqual(copy.details.rows.map((row) => row.value), ['5.1 GB (some could not be read) · 1,629 folders', 'size unknown · 4 copies']);
    assert.equal(copy.confirmLabel, 'Free 5.1 GB or more');
    // While listing the same rows read as progress, and the sum is a checked group's complete size.
    const listing = trayHtml(free('listing', { done: 1, total: 5, groups: [partial, unknown] }), ui());
    assert.match(listing, /5\.1 GB so far[\s\S]*listing…/);
    assert.doesNotMatch(listing, /size unknown|could not be read/);
    const ready = trayHtml(free('ready', { groups: both() }), ui());
    assert.match(ready, /data-free-go[^>]*data-i18n-fmt="storage\.free\.go"[^>]*>Free 8\.9 GB</);
});

test('counts of one read as one: in use, kept, deleting and stuck', () => {
    const one = trayHtml(free('ready', { groups: [group('work_folders', { count: 2, inside_wait: 0, in_use: 1, unreadable: 1 })] }), ui(['work_folders']));
    assert.match(one, /1 is in use and stays\. 1 stays: its task record could not be read\./);
    const many = trayHtml(free('ready', { groups: [group('work_folders', { count: 5, inside_wait: 0, in_use: 2, unreadable: 3 })] }), ui(['work_folders']));
    assert.match(many, /2 are in use and stay\. 3 stay: their task record could not be read\./);
    assert.deepEqual(doneSentences(free('done', { run: run({ removed: 1, removed_bytes: 1e9, deleting: 1, deleting_bytes: 1e6, stuck: 1,
        kept: [{ reason: 'in_use', count: 1 }], free_before: null, free_after: null }) })), [
        'Removed 1.0 GB (1 folder).', '1 was kept: still in use.', '1 is still being deleted.',
        '1 could not be deleted yet; the app retries at its next maintenance run.', 'The next count will show the new total.',
    ]);
    assert.deepEqual(doneSentences(free('done', { run: run({ deleting: 0, deleting_bytes: 0, stuck: 2, free_before: null, free_after: null }) })).slice(3, 4),
        ['2 could not be deleted yet; the app retries at its next maintenance run.']);
});

test('an empty preview says so and keeps the door', () => {
    const empty = trayHtml(free('ready', { groups: [group('work_folders', { count: 0, inside_wait: 0, in_use: 0, bytes: 0, items: [] })] }), ui());
    assert.match(empty, /Nothing temporary is waiting\.[\s\S]*Most of Ouroboros is kept on purpose\.[\s\S]*Show what can go/);
});

test('running, done, stopped and failed read in the owner\'s words', () => {
    const running = trayHtml(free('running', { total: 1630, groups: both(), run: run({ done: 412, removed: 0, removed_bytes: 0, deleting: 412, deleting_bytes: 2.1 * GB, kept: [] }) }), ui());
    assert.match(running, /Freeing space · 0 bytes removed · 412 of 1,630 folders · You can leave this tab\. 412 are still being deleted\./);
    assert.match(running, /data-free-stop[^>]*>Stop</);
    assert.doesNotMatch(running, /btn-danger|type="checkbox"/);
    const done = trayHtml(free('done', { groups: both(), run: run() }), ui());
    assert.match(done, /Removed 8\.2 GB \(1,584 folders\)\./);
    assert.match(done, /11 were kept: their results are not copied yet\. 3 were kept: still in use\. 3 are still being deleted\. 140 GB free is now 148 GB\. The next count will show the new total\./);
    assert.match(done, /Show what can go/);
    assert.deepEqual(doneSentences(free('done', { run: run({ removed: 1, removed_bytes: 1e9, deleting: 0, deleting_bytes: 0, kept: [], free_before: null, free_after: null }) })),
        ['Removed 1.0 GB (1 folder).', 'The next count will show the new total.']);
    const stopped = trayHtml(free('stopped', { groups: both(), run: run({ stopped: true, removed_bytes: 1e9, deleting_bytes: 0.2e9, deleting: 2 }) }), ui());
    assert.match(stopped, /Removed 1\.0 GB, then stopped\. The rest was left as it was\./);
    assert.match(stopped, /2 are still being deleted\./);
    const failed = trayHtml(free('failed', { error: 'The sizes could not be read.' }), ui());
    assert.match(failed, /data-tone="warn"[^>]*>Temporary space could not be listed: The sizes could not be read\. Nothing was removed\./);
    assert.match(failed, /Try again/);
    assert.equal(notStarted('preview_outdated'), 'Nothing was removed. The list changed; check again.');
    for (const html of [running, done, stopped, failed]) assert.doesNotMatch(html, AVOIDED);
});

test('a failed free run preserves partial removals and reports its error', () => {
    const partial = trayHtml(free('failed', { run: run({ error: 'OSError' }) }), ui());
    assert.match(partial, /Removed 8\.2 GB \(1,584 folders\)\./);
    assert.match(partial, /3 are still being deleted\. Freeing stopped: OSError\./);
    assert.doesNotMatch(partial, /Nothing was removed|could not be listed/);
    const running = trayHtml(free('running', { total: 1, run: run({ done: 1, removed: 1, removed_bytes: 1e9, deleting: 0, deleting_bytes: 0 }) }), ui());
    assert.match(running, /1\.0 GB removed/);
    assert.doesNotMatch(running, /still being deleted/);
});

test('the confirm dialog carries the sum, the loss sentence and one detail row per checked group', () => {
    const copy = confirmCopy(free('ready', { groups: both() }), new Set(['work_folders', 'working_copies']));
    assert.equal(copy.title, 'Free temporary space?');
    assert.equal(copy.body, 'This removes 8.9 GB for good. Results, messages and the exact model history are kept. Continuing one of these tasks later starts without its scratch files.');
    assert.deepEqual(copy.details.rows, [
        { label: 'Finished tasks’ work folders', value: '6.8 GB · 1,629 folders' },
        { label: 'Working copies of finished work', value: '2.1 GB · 284 copies' },
    ]);
    assert.deepEqual([copy.confirmLabel, copy.danger], ['Free 8.9 GB', true]);
    const one = confirmCopy(free('ready', { groups: both() }), new Set(['working_copies']));
    assert.equal(one.details.rows.length, 1);
    assert.equal(one.confirmLabel, 'Free 2.1 GB');
});

test('titles and accessible names are fixed chrome: nothing minted per preview', () => {
    const attributes = (html) => [...html.matchAll(/\s(title|aria-label)="([^"]*)"/g)].map(([, name, value]) => `${name}=${value}`);
    assert.deepEqual(attributes(trayHtml(free('ready', { groups: both() }), ui())), []);
    assert.deepEqual(attributes(trayHtml(free('done', { groups: both(), run: run() }), ui())), []);
});
