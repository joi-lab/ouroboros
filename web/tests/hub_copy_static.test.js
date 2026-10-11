// Frozen §7.4 copy pins (plan hubflow-sprint-20260823, reworded for issue
// #1314): the exact user-facing strings the Use Hub version / Update confirm
// dialogs and hub cards ship. A wording change is a deliberate plan edit,
// never a drive-by.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'modules', 'ouroboroshub.js'), 'utf8');

const FROZEN = [
    'No local publish record for this name - the hub skill may belong to someone else.',
    'Local publish record is unreadable.',
    'Replace the local copy (',
    'including any local edits, ',
    'with the current OuroborosHub copy?',
    "'Its saved data, enablement and review history stay; '",
    "'the new files are reviewed again and may need access granted again.'",
    "confirmLabel: 'Use Hub version'",
    "confirmLabel: 'Update'",
    '>Use Hub version${',
    "'unknown version'",
    'Hub version unknown.',
    "{ label: 'Last seen in Hub'",
    'Local copy${',
    'Not in the Hub catalog',
    '<summary>Submission history</summary>',
    'Local record only; the PR remains on GitHub.',
    'Name taken by a local skill',
    'Catalog entry conflict',
    'Hub facts unavailable',
    'Adopting a ClawHub-installed skill is not supported yet.',
];

for (const needle of FROZEN) {
    test(`frozen copy present: ${needle.slice(0, 40)}`, () => {
        assert.ok(src.includes(needle), `missing frozen copy: ${needle}`);
    });
}

test('stale-retry guard checks the fresh verdict for every action', () => {
    assert.ok(src.includes('verdict.action !== action'), 'runAction must revalidate ALL actions');
});

test('Update rides the update endpoint, never install?overwrite', () => {
    assert.ok(src.includes('/api/marketplace/ouroboroshub/update/'));
    assert.ok(!src.includes("{ slug, overwrite: true"), 'no overwrite-install update path');
});

test('retired waiting copy stays retired: a receipt never draws a waiting state', () => {
    for (const gone of ['wait_pr', 'submitted_pr', 'Waiting for the hub', 'Adopt hub version']) {
        assert.ok(!src.includes(gone), `retired copy returned: ${gone}`);
    }
});

test('the Hub tab reads the whole catalog and never judges absence from a search result', () => {
    assert.ok(src.includes("fetchJson('/api/marketplace/ouroboroshub/catalog')"));
    assert.ok(!src.includes('catalog?${'), 'no server-filtered catalog read in the Hub tab');
    assert.ok(src.includes('official: !listingOnly'));
});
