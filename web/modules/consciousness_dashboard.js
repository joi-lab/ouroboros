// Dashboard › Consciousness: what Ouroboros does when nobody asked, now and lately. The status with Stop
// (the same /bg command the Activity row and the chat header send), where its money went by cause, the own
// tasks MAX_TASKS counts, its places (each opens its sheet; Connect a place) and the recent wakes with their
// cause, where, how fast it answered, what the Safety Supervisor did and what each cost. Facts come from
// `GET /api/consciousness`; while a server does not answer that, `/api/state` and `/api/places` still give
// the status and the places, and the wakes say they are not reported. Unknown is said, never zero.
import { escapeHtmlAttr as escapeHtml } from './ui_primitives.js';
import { statusMarkHtml } from './status_mark.js';
import { strokeIcon } from './page_icons.js';
import { chipHtml, skillIconHtml } from './skill_presentation.js';
import { allowanceLabel } from './utils.js';
import { PLACES_CHANGED_EVENT, readConsciousness, readConsciousnessFromState, readPlaces } from './consciousness_api.js';
import { CHECK_CHIPS, clockText, durationText, placeStatusText, usdText } from './consciousness_words.js';
import { connectPlaceButtonHtml, openConnectPlace, openPlaceSheet } from './place_sheet.js';

const SEP = ' · ';
const REFRESH_MS = 20000;
// Its own time: the house brain glyph (Lucide geometry), beside the words that say it.
const OWN_GLYPH = strokeIcon('<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/>', 14);
// Work running now: the house status mark's moving dots, in the surface's own ink.
const dots = () => statusMarkHtml('consciousness-mark', { shape: 'moving', hue: 'ink' });
const facts = (parts) => parts.filter(Boolean).map((part) => `<span class="consciousness-fact">${part}</span>`).join(SEP);

/** The wake running now: a focus in a place first (a person waits there), else its own clock. */
export function liveWake(snapshot) {
    const open = (snapshot.recentWakes || []).filter((wake) => !wake.finishedAt);
    return open.find((wake) => wake.cause === 'focus') || open.find((wake) => wake.cause === 'own_clock')
        || (snapshot.liveWakeTaskId ? { cause: 'own_clock', taskId: snapshot.liveWakeTaskId } : null);
}

// A line never starts with a separator: the dot keeps to the word before it («Slack · Acme» wraps before «Slack»).
const keepDots = (text) => String(text || '').replace(/ · /g, '\u00a0· ');

/** The headline: what it is doing now, in words, with the moving mark only while work runs. */
export function headlineView(snapshot) {
    const live = snapshot.enabled === false ? null : liveWake(snapshot);
    if (live?.cause === 'focus') return { text: `Awake now in ${keepDots(live.placeLabel || 'a place')}`, moving: true };
    if (live) return { text: 'Awake now on its own clock', moving: true };
    if (snapshot.enabled === null) return { text: 'State unknown', moving: false };
    if (!snapshot.enabled) return { text: 'Off', moving: false };
    if (snapshot.status === 'allowance_exhausted') {
        const frees = clockText(snapshot.allowanceResetsAt);
        return { text: `Waiting for the allowance${frees ? ` · frees at ${frees}` : ''}`, moving: false };
    }
    const next = clockText(snapshot.nextWakeAt);
    return { text: next ? `Sleeping until ${next}` : 'Sleeping', moving: false };
}

const causeWord = (row) => (row.cause === 'own' ? 'own' : row.cause === 'started' ? 'started work' : row.label || row.cause.replace(/^place:/, ''));

/** «on its own clock · 3 places · own $0.62 · Slack · Acme $0.88 · started work $0.48 · unmetered 3 calls» (the rolling 24 hours). */
export function factsLineParts(snapshot, placeCount = null) {
    const parts = [snapshot.enabled === false ? 'off' : snapshot.enabled === null ? '' : 'on its own clock'];
    if (Number.isInteger(placeCount)) parts.push(placeCount === 1 ? '1 place' : `${placeCount} places`);
    const spend = snapshot.spendByCause.filter((row) => row.usd !== null);
    spend.forEach((row) => parts.push(`${escapeHtml(causeWord(row))} <span class="consciousness-num">${escapeHtml(usdText(row.usd))}</span>`));
    const unmetered = snapshot.spendByCause.reduce((sum, row) => sum + row.unmeteredCalls, 0);
    if (unmetered > 0) parts.push(`unmetered ${unmetered} ${unmetered === 1 ? 'call' : 'calls'}`);
    return parts.filter(Boolean);
}

/** The own tasks MAX_TASKS counts, each with its cause; work the owner asked for never counts. */
export function countedRootsText(snapshot) {
    if (snapshot.maxTasks === null && !snapshot.countedRoots.length) return '';
    const running = snapshot.countedRoots.length || snapshot.tasksRunning || 0;
    const head = `Its own tasks: ${running}${snapshot.maxTasks !== null ? ` of ${snapshot.maxTasks}` : ''}`;
    const names = snapshot.countedRoots.map((root) => `${root.title || root.taskId}${root.causeLabel ? ` (${root.causeLabel})` : ''}`);
    return [head, ...names].join(' · ');
}

export function summaryHtml(snapshot, { placeCount = null, hasSocket = true } = {}) {
    const headline = headlineView(snapshot);
    const kicker = snapshot.enabled === null ? 'Consciousness · state unknown' : `Consciousness · ${snapshot.enabled ? 'on' : 'off'}`;
    const money = allowanceLabel(snapshot.spentUsd, snapshot.dailyUsd, snapshot.unknownUnmetered, snapshot.integrityDegraded);
    // The host's own sentence, where it says more than the headline (a refusal, a failure, a first-run wait).
    const quiet = ['sleeping', 'thinking', 'disabled', 'allowance_exhausted', ''].includes(snapshot.status) ? '' : snapshot.detail;
    const roots = countedRootsText(snapshot);
    const toggle = snapshot.enabled === null ? '' : `<button type="button" class="btn btn-ghost btn-sm consciousness-stop" data-cx-act="toggle" data-enabled="${snapshot.enabled ? '1' : '0'}"${hasSocket ? '' : ' disabled'}>${snapshot.enabled ? 'Stop' : 'Start'}</button>`;
    return `<div class="storage-summary-head">
            <div class="storage-summary-text consciousness-summary-text">
                <div class="storage-kicker">${escapeHtml(kicker)}</div>
                <div class="consciousness-headline"><div class="storage-total">${escapeHtml(headline.text)}${headline.moving ? dots() : ''}</div>${toggle}</div>
                ${quiet ? `<p class="consciousness-why">${escapeHtml(quiet)}</p>` : ''}
                ${roots ? `<p class="consciousness-next">${escapeHtml(roots)}</p>` : ''}
            </div>
        </div>
        <div class="consciousness-summary-foot">
            ${money ? `<span class="storage-meta consciousness-total">${escapeHtml(money)} in the last 24 hours</span>` : ''}
            <span class="consciousness-causes">${facts(factsLineParts(snapshot, placeCount))}</span>
        </div>`;
}

export function dashboardPlaceRowHtml(place, { awake = false } = {}) {
    const spend = usdText(place.spent24hUsd);
    return `<li><button type="button" class="consciousness-dash-place" data-place-open="${escapeHtml(place.id)}"
            aria-label="${escapeHtml(`Open ${place.label}${awake ? ', awake here now' : ''}: ${placeStatusText(place)}; ${spend === '—' ? 'nothing spent' : `${spend} spent`} in 24 hours`)}">
        <span class="consciousness-dash-place-main"><span class="place-glyph" aria-hidden="true">${skillIconHtml(place.icon, 18)}</span><span class="consciousness-place-name">${escapeHtml(place.label)}</span>${awake ? dots() : ''}</span>
        <span class="consciousness-dash-place-meta">${escapeHtml(placeStatusText(place))}${place.status?.text ? `${SEP}${escapeHtml(place.status.text)}` : ''}</span>
        <span class="consciousness-spend${spend === '—' ? ' is-none' : ''}">${escapeHtml(spend)}</span>
    </button></li>`;
}

export function placesCardHtml(places, { error = '', awakeIn = '' } = {}) {
    const body = error ? `<p class="ui-status" data-tone="warn">${escapeHtml(error)}</p>`
        : !places ? '<p class="storage-meta">Reading places…</p>'
            : !places.length ? '<p class="consciousness-copy">No place yet. Connect one from an installed connector.</p>'
                : `<ul class="consciousness-dash-places">${places.map((place) => dashboardPlaceRowHtml(place, { awake: place.id === awakeIn })).join('')}</ul>`;
    return `<div class="consciousness-card-head">
            <div class="consciousness-card-head-text">
                <h4 class="storage-card-title">Places</h4>
                <p class="consciousness-copy">A connector you install can connect a place here: Connect a place.</p>
            </div>
            ${connectPlaceButtonHtml()}
        </div>
        ${body}
        <p class="consciousness-foot">Allowance, own tasks and concurrent conversations: <button type="button" class="consciousness-inline-link ui-hit-area" data-cx-act="settings">Settings ›</button></p>`;
}

const OUTCOME_WORDS = {
    message: 'replied', silent: 'stayed silent', tool_delivered: 'sent through its tools', deferred: 'started work',
    completed: 'done', failed: 'failed', cancelled: 'stopped',
};

/** One recent wake: when, the cause and where, how fast it answered, what it did, the checks, the cost. */
export function wakeRowHtml(wake) {
    const live = !wake.finishedAt;
    const where = wake.cause === 'focus'
        ? `<span class="consciousness-wake-where">${escapeHtml(wake.placeLabel || 'A place')}</span>`
        : `<span class="consciousness-wake-own"><span class="place-glyph" aria-hidden="true">${OWN_GLYPH}</span>Own time</span>`;
    const answered = durationText(wake.startedAt, wake.firstReplyAt);
    const did = [
        answered ? `answered in ${answered}` : '',
        live ? `working${dots()}` : escapeHtml(OUTCOME_WORDS[wake.outcome] || wake.outcome || ''),
    ].filter(Boolean);
    const seen = new Set();
    const checks = wake.checks.filter((check) => !seen.has(check.chip) && seen.add(check.chip)).map((check) => {
        const chip = CHECK_CHIPS[check.chip] || { label: check.chip.replace(/_/g, ' '), tone: 'neutral' };
        return chipHtml(chip.label, chip.tone);
    }).join(' ');
    const cost = wake.costUsd !== null ? `up to ${usdText(wake.costUsd)}` : wake.unmetered ? 'unmetered' : '—';
    return `<li class="consciousness-wake${live ? ' is-live' : ''}">
        <time class="consciousness-wake-time"${wake.startedAt ? ` datetime="${escapeHtml(wake.startedAt)}"` : ''}>${escapeHtml(clockText(wake.startedAt) || '—')}</time>
        <div class="consciousness-wake-main">
            <div class="consciousness-wake-head">${where}${wake.summary ? `${SEP}<span>${escapeHtml(wake.summary)}</span>` : ''}</div>
            ${did.length ? `<div class="consciousness-wake-did">${did.join(SEP)}</div>` : ''}
            ${checks ? `<div class="consciousness-wake-checks">${checks}</div>` : ''}
        </div>
        <span class="consciousness-wake-cost">${escapeHtml(cost)}</span>
    </li>`;
}

export function wakesCardHtml(wakes) {
    const count = Array.isArray(wakes) ? wakes.length : 0;
    const body = !Array.isArray(wakes)
        ? '<p class="consciousness-copy">This server does not report its recent wakes yet.</p>'
        : !count ? '<p class="consciousness-copy">No wakes yet.</p>'
            : `<ol class="consciousness-wakes">${wakes.map(wakeRowHtml).join('')}</ol>`;
    return `<div class="consciousness-card-head"><div class="consciousness-card-head-text">
            <h4 class="storage-card-title">Recent wakes</h4>
            ${count ? `<p class="storage-meta">${count === 1 ? '1 wake' : `${count} wakes`} · newest first</p>` : ''}
        </div></div>${body}`;
}

/** Mount the tab into its Dashboard panel. */
export function initConsciousnessDashboard({ mount, ws, state, openSettingsTab, read = readConsciousness,
    readFallback = readConsciousnessFromState, readPlacesFn = readPlaces } = {}) {
    if (!mount) return { refresh: () => {}, destroy() {} };
    mount.innerHTML = `<div class="settings-embedded-content"><div class="costs-scroll scroll-fade-y">
        <div class="storage-root consciousness-root">
            <p class="ui-status consciousness-read-status" role="status" data-cx-status></p>
            <section class="storage-summary ui-card consciousness-summary" aria-label="Consciousness" data-cx-summary><p class="storage-meta">Reading…</p></section>
            <section class="ui-card consciousness-card" aria-label="Places" data-cx-places></section>
            <section class="ui-card consciousness-card" aria-label="Recent wakes" data-cx-wakes></section>
        </div></div></div>`;
    const regions = {
        status: mount.querySelector('[data-cx-status]'), summary: mount.querySelector('[data-cx-summary]'),
        places: mount.querySelector('[data-cx-places]'), wakes: mount.querySelector('[data-cx-wakes]'),
    };
    // The page binds every `.scroll-fade-y` scroller once, after the panels mount (app.js).
    let snapshot = null;
    let places = null;
    let generation = 0;
    let timer = 0;
    let destroyed = false;
    const shown = () => !document.hidden && state?.activePage === 'dashboard' && state?.dashboardActiveSubtab === 'consciousness';

    // A refresh that found nothing new leaves a region's nodes alone, so focus and reading position stay.
    const painted = new Map();
    const put = (region, html) => {
        if (painted.get(region) === html) return;
        painted.set(region, html);
        region.innerHTML = html;
    };
    function paint({ placesError = '' } = {}) {
        if (snapshot) put(regions.summary, summaryHtml(snapshot, { placeCount: places ? places.length : null, hasSocket: Boolean(ws?.send) }));
        const awake = snapshot ? liveWake(snapshot) : null;
        put(regions.places, placesCardHtml(places, { error: placesError, awakeIn: awake?.cause === 'focus' ? awake.placeId : '' }));
        put(regions.wakes, wakesCardHtml(snapshot ? snapshot.recentWakes : null));
    }

    async function refresh() {
        if (destroyed) return;
        const current = ++generation;
        regions.status.textContent = snapshot ? '' : 'Reading…';
        let next = null;
        let readError = '';
        try { next = await read(); } catch {
            try { next = await readFallback(); } catch (error) { readError = error?.message || String(error); }
        }
        let placesError = '';
        let nextPlaces = next?.places || null;
        if (next && !nextPlaces) {
            try { nextPlaces = (await readPlacesFn()).places; } catch (error) { placesError = `Places could not be read: ${error?.message || error}`; }
        }
        if (destroyed || current !== generation) return;
        if (next) { snapshot = next; places = nextPlaces ?? places; }
        regions.status.textContent = readError ? `Could not read consciousness: ${readError}. ${snapshot ? 'Earlier values are shown.' : ''}` : '';
        regions.status.dataset.tone = readError ? 'error' : '';
        if (snapshot) paint({ placesError });
        schedule();
    }

    function schedule() {
        clearTimeout(timer);
        if (!destroyed && shown()) timer = setTimeout(refresh, REFRESH_MS);
    }

    const onClick = async (event) => {
        const row = event.target.closest('[data-place-open]');
        if (row) {
            const place = (places || []).find((entry) => entry.id === row.dataset.placeOpen);
            if (place) openPlaceSheet(place, { returnFocus: row });
            return;
        }
        const connect = event.target.closest('[data-place-connect]');
        if (connect) { openConnectPlace({ returnFocus: connect }); return; }
        const button = event.target.closest('[data-cx-act]');
        if (!button || button.disabled) return;
        const act = button.dataset.cxAct;
        if (act === 'settings') {
            await openSettingsTab?.('behavior');
            document.getElementById('settings-consciousness')?.scrollIntoView({ block: 'start' });
            return;
        }
        if (act === 'toggle') {
            button.disabled = true;
            // The existing direct control command (the Activity row and the chat header send the same).
            ws?.send?.({ type: 'command', cmd: `/bg ${button.dataset.enabled === '1' ? 'stop' : 'start'}` });
            await new Promise((resolve) => setTimeout(resolve, 400));
            await refresh();
        }
    };
    const onShown = (event) => { if (event?.detail?.tab === 'consciousness') void refresh(); else clearTimeout(timer); };
    const onChanged = () => { if (shown()) void refresh(); };
    mount.addEventListener('click', onClick);
    window.addEventListener('ouro:dashboard-subtab-shown', onShown);
    window.addEventListener(PLACES_CHANGED_EVENT, onChanged);
    if (shown()) void refresh();
    return {
        refresh,
        destroy() {
            destroyed = true;
            clearTimeout(timer);
            mount.removeEventListener('click', onClick);
            window.removeEventListener('ouro:dashboard-subtab-shown', onShown);
            window.removeEventListener(PLACES_CHANGED_EVENT, onChanged);
        },
    };
}
