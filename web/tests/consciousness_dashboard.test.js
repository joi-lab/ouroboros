// Dashboard › Consciousness against the contract's shapes: the headline says what it does now, the facts
// line says where the money went by cause, the counted roots, the place rows, the recent wakes with their
// check chips, and the fallback to /api/state while the full snapshot is not served.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

import { normalizeConsciousness, normalizePlacesResponse } from '../modules/consciousness_api.js';
import {
    countedRootsText, dashboardPlaceRowHtml, factsLineParts, headlineView, initConsciousnessDashboard, liveWake,
    placesCardHtml, summaryHtml, wakeRowHtml, wakesCardHtml,
} from '../modules/consciousness_dashboard.js';

const FIXTURE = JSON.parse(fs.readFileSync(new URL('./fixtures/consciousness_contract.json', import.meta.url), 'utf8'));
const full = () => normalizeConsciousness(FIXTURE.consciousness);
const fromState = () => normalizeConsciousness(FIXTURE.state.bg_consciousness_state, { enabled: true });
const strip = (html) => html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();

test('the headline says what it does now; work in a place outranks its own clock', () => {
    const snapshot = full();
    assert.equal(liveWake(snapshot).taskId, 'w-live');
    assert.deepEqual(headlineView(snapshot), { text: 'Awake now in Slack\u00a0· Acme', moving: true }, 'the dot keeps to its word');
    const own = { ...fromState(), liveWakeTaskId: 'w-9' };
    assert.deepEqual(headlineView(own), { text: 'Awake now on its own clock', moving: true });
    assert.match(headlineView(fromState()).text, /^Sleeping until \d\d:\d\d$/);
    assert.deepEqual(headlineView({ ...fromState(), enabled: false }), { text: 'Off', moving: false });
    assert.deepEqual(headlineView(normalizeConsciousness({})), { text: 'State unknown', moving: false });
    const spent = { ...fromState(), status: 'allowance_exhausted', allowanceResetsAt: '2026-10-20T15:40:00Z' };
    assert.match(headlineView(spent).text, /^Waiting for the allowance · frees at \d\d:\d\d$/);
    assert.equal(headlineView({ ...spent, allowanceResetsAt: '' }).text, 'Waiting for the allowance');
});

test('the facts line: on its own clock, its places, the rolling 24 hours by cause, unmetered calls', () => {
    const parts = factsLineParts(fromState(), 3).map(strip);
    assert.deepEqual(parts, ['on its own clock', '3 places', 'own $0.62', 'Slack · Acme $0.88', 'started work $0.48', 'unmetered 3 calls']);
    assert.deepEqual(factsLineParts({ ...fromState(), enabled: false, spendByCause: [] }, 1).map(strip), ['off', '1 place']);
});

test('the counted roots are its own tasks with their causes; the owner\'s work never counts', () => {
    assert.equal(countedRootsText(fromState()), 'Its own tasks: 1 of 2 · Release notes draft (Slack · Acme)');
    assert.equal(countedRootsText({ ...fromState(), countedRoots: [], tasksRunning: 0 }), 'Its own tasks: 0 of 2');
    assert.equal(countedRootsText(normalizeConsciousness({})), '');
});

test('the summary: status, Stop, the 24-hour money, the host\'s sentence only where it adds something', () => {
    const html = summaryHtml(fromState(), { placeCount: 3 });
    assert.match(html, /Consciousness · on/);
    assert.match(html, /data-cx-act="toggle" data-enabled="1">Stop</);
    assert.match(strip(html), /\$2\.32 \/ \$20\.00 in the last 24 hours/);
    assert.doesNotMatch(html, /Sleeping until 17:30\./, 'the plain sleeping detail repeats the headline');
    const failed = summaryHtml({ ...fromState(), status: 'wake_failed', detail: 'The last wake-up failed (runner error).' });
    assert.match(failed, /The last wake-up failed \(runner error\)\./);
    assert.match(summaryHtml({ ...fromState(), enabled: false }), /data-enabled="0">Start</);
    assert.match(summaryHtml(fromState(), { hasSocket: false }), /data-enabled="1" disabled>Stop</);
    assert.doesNotMatch(summaryHtml(normalizeConsciousness({})), /data-cx-act="toggle"/, 'an unknown control offers no toggle');
});

test('a place row: icon, label, state and wakes in words, the day\'s spend; unknown spend is the dash', () => {
    const { places } = normalizePlacesResponse(FIXTURE.places);
    const slack = dashboardPlaceRowHtml(places[0], { awake: true });
    assert.match(strip(slack), /Slack · Acme active · wakes for direct messages, mentions and replies · 2 unanswered · connected \$0\.88/);
    assert.match(slack, /class="status-mark consciousness-mark"/);
    assert.match(slack, /aria-label="Open Slack · Acme, awake here now: /);
    assert.match(dashboardPlaceRowHtml(places[1]), /class="consciousness-spend is-none">—</);
    assert.match(strip(dashboardPlaceRowHtml(places[1])), /^Telegram · Ouroboros community silent · wakes for mentions —$/, 'Silent still wakes for its list');
    assert.match(strip(dashboardPlaceRowHtml(places[2])), /^Mail · inbox paused \$0\.00$/);
    assert.match(dashboardPlaceRowHtml(places[2]), />\$0\.00</);
    assert.match(placesCardHtml(places), /data-place-connect>Connect a place</, "the one Connect control");
    assert.match(placesCardHtml([]), /No place yet\. Connect one from an installed connector\./);
    assert.match(placesCardHtml([]), /A connector you install can connect a place here: Connect a place\./);
    assert.doesNotMatch(placesCardHtml(places), /Each connector you install adds/);
    assert.match(placesCardHtml(null, { error: 'Places could not be read: HTTP 404' }), /data-tone="warn"/);
});

test('a recent wake: when, cause and where, how fast it answered, the check chips, the cost', () => {
    const [live, own, lena, held] = full().recentWakes;
    assert.match(strip(wakeRowHtml(lena)), /Slack · Acme · Лена wrote in a direct message answered in 40 s · replied checked sent despite the check up to \$0\.04/);
    assert.match(wakeRowHtml(lena), /data-tone="warn"[^>]*>sent despite the check</);
    assert.match(wakeRowHtml(lena), /data-tone="neutral"[^>]*>checked</);
    assert.match(strip(wakeRowHtml(own)), /Own time · read 4 places, wrote 2 memory pages done not checked up to \$0\.21/);
    assert.match(wakeRowHtml(live), /is-live/);
    assert.match(strip(wakeRowHtml(live)), /working/);
    assert.match(strip(wakeRowHtml(held)), /stayed silent held unmetered$/);
    assert.match(wakesCardHtml(full().recentWakes), /4 wakes · newest first/);
    assert.match(wakesCardHtml(null), /does not report its recent wakes yet/);
    assert.match(wakesCardHtml([]), /No wakes yet/);
});

function fakeMount() {
    const regions = new Map();
    return {
        innerHTML: '',
        regions,
        querySelector(selector) {
            if (!regions.has(selector)) regions.set(selector, { innerHTML: '', textContent: '', dataset: {} });
            return regions.get(selector);
        },
        addEventListener() {}, removeEventListener() {},
    };
}

test('while the full snapshot is not served, /api/state and /api/places still give status and places', async () => {
    const priorWindow = globalThis.window;
    const priorDocument = globalThis.document;
    globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
    globalThis.document = { hidden: false };
    const mount = fakeMount();
    const notServed = Object.assign(new Error('HTTP 404'), { status: 404 });
    const controller = initConsciousnessDashboard({
        mount, ws: { send() {} }, state: { activePage: 'dashboard', dashboardActiveSubtab: 'other' },
        read: async () => { throw notServed; },
        readFallback: async () => fromState(),
        readPlacesFn: async () => normalizePlacesResponse(FIXTURE.places),
    });
    try {
        await controller.refresh();
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /Consciousness · on/);
        assert.equal((mount.regions.get('[data-cx-places]').innerHTML.match(/data-place-open=/g) || []).length, 3);
        assert.match(mount.regions.get('[data-cx-wakes]').innerHTML, /does not report its recent wakes yet/);
        assert.equal(mount.regions.get('[data-cx-status]').textContent, '');
    } finally {
        controller.destroy();
        globalThis.window = priorWindow;
        globalThis.document = priorDocument;
    }
});

test('a failed read keeps the earlier values and says so', async () => {
    const priorWindow = globalThis.window;
    const priorDocument = globalThis.document;
    globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
    globalThis.document = { hidden: false };
    const mount = fakeMount();
    let fail = false;
    const controller = initConsciousnessDashboard({
        mount, state: { activePage: 'chat' },
        read: async () => { if (fail) throw new Error('offline'); return full(); },
        readFallback: async () => { throw new Error('offline'); },
        readPlacesFn: async () => normalizePlacesResponse(FIXTURE.places),
    });
    try {
        await controller.refresh();
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /Awake now in Slack\u00a0· Acme/);
        fail = true;
        await controller.refresh();
        assert.match(mount.regions.get('[data-cx-status]').textContent, /Could not read consciousness: offline\. Earlier values are shown\./);
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /Awake now in Slack\u00a0· Acme/);
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /data-enabled="1" disabled>Stop</, 'no socket, no toggle press');
    } finally {
        controller.destroy();
        globalThis.window = priorWindow;
        globalThis.document = priorDocument;
    }
});
