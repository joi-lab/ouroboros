// Dashboard -> Storage: the last finished count of everything Ouroboros owns (the data folder with the
// engine home, working copies, project folders, Deliverables, the installed program), its date, the growth
// since the count before it and the free space of the data folder's disk. `POST /api/storage/measure`
// starts one complete count in a server child; `GET /api/storage` is its passive projection, polled only
// while counting, listing or freeing (including trash deletion, not an entry the maintenance child reported
// stuck) AND this tab is on screen. The bar is drawn only for a finished
// count (the owner's choice «Полоса и группы», 2026-10-05): five group shares that sum to the headline; a
// running, stopped or failed count never replaces the saved one, so the bar and the list stay while a new
// count runs. Unknown is never zero: a place that could not be listed has no size. The second card ends
// in the Temporary space tray (storage_free.js): a pure preview of what the app's own retirers would
// remove, one danger capsule last on the page, the shared confirm dialog, progress and result in place.

import { apiClient } from './api_client.js';
import { openConfirmDialog } from './confirm_dialog.js';
import { fmt } from './i18n.js';
import { showToast } from './toast.js';
import { escapeHtmlAttr, escapeHtmlText } from './utils.js';
import { DOTS, durationLabel, formatBytes, isSize, number, plural, signedBytes as signed, whenLabel } from './storage_format.js';
import { confirmCopy, doneSentences, groupsOf, notStarted, trayHtml, trayState } from './storage_free.js';

export { durationLabel, formatBytes, whenLabel };

const POLL_MS = 1000;
const text = escapeHtmlText;
const attr = escapeHtmlAttr;

/** The five groups of the bar and the list, in display order (server `place.group`). */
export const STORAGE_GROUPS = [
    { id: 'records', label: 'Model call records' },
    { id: 'engine', label: 'Agent engine' },
    { id: 'work', label: 'Work and history' },
    { id: 'temporary', label: 'Task work folders and copies' },
    { id: 'other', label: 'App state and the rest' },
];

// Server place id -> the words this screen says about it. `parts` offers the "Largest parts" disclosure,
// `split` the engine's three parts (`projects` and `profiles` read by name: the server lists them whatever
// their rank). A place this client does not know is shown by its id.
const PLACE_WORDS = {
    model_calls: { label: 'Model call records', why: 'The exact prompts and answers behind every decision. Kept; this screen cannot delete them.', parts: true },
    engine: { label: 'Agent engine', why: 'Kept by the engine. Not a cache.', split: true },
    task_results: { label: 'Task results and files', why: 'Kept with each task.' },
    archives: { label: 'Archives', why: 'Kept. Rotated logs and backups.' },
    memory_skills: { label: 'Memory, notes and skills', why: 'Kept until you delete that project or skill.' },
    project_folders: { label: 'Project folders', why: 'Your project files. Not deleted here.' },
    deliverables: { label: 'Deliverables', why: 'Files Ouroboros made for you. Not deleted here.' },
    work_folders: { label: 'Task work folders', why: 'Removed by the app’s own cleanup {days} days after a task finishes.' },
    working_copies: { label: 'Working copies', why: 'Copies of code that helpers work in. Removed {days} days after they were made, once their task has finished.' },
    app_state: { label: 'App state', why: 'The task database, settings, caches and working state.', parts: true },
    data_other: { label: 'Everything else in the data folder', why: 'Logs, attachments and anything not listed above.', parts: true },
    program: { label: 'Ouroboros program', why: 'The installed app and its history. Updated by the app.' },
};

// ---------------------------------------------------------------------------
// Pure presentation
// ---------------------------------------------------------------------------

const words = (place) => PLACE_WORDS[place?.id] || { label: String(place?.id || ''), why: '' };

function engineSplit(place) {
    const part = (name) => place.parts.find((row) => row.name === name)?.bytes || 0;
    const history = part('projects');
    const accounts = part('profiles');
    return [
        fmt('Run history {size}', { size: formatBytes(history) }),
        fmt('Account data {size}', { size: formatBytes(accounts) }),
        fmt('Engine files {size}', { size: formatBytes(Math.max(0, (place.bytes || 0) - history - accounts)) }),
    ].join(' · ');
}

/** The count's places as rows in server order (absent places omitted), each with its words, growth and
 *  the parts its disclosure lists. `days` is the server's retention for the two temporary rows. */
export function placeRows(count, days = 7) {
    return (Array.isArray(count?.places) ? count.places : []).filter((place) => place && place.present !== false).map((place) => {
        const said = words(place);
        const parts = Array.isArray(place.parts) ? place.parts : [];
        return {
            id: String(place.id),
            group: STORAGE_GROUPS.some((group) => group.id === place.group) ? place.group : 'other',
            label: fmt(said.label),
            why: said.why ? fmt(said.why, { days }) : '',
            bytes: isSize(place.bytes) ? place.bytes : null,
            unreadable: isSize(place.unreadable) ? place.unreadable : 0,
            growth: isSize(place.growth_bytes) ? place.growth_bytes : null,
            caveat: place.growth_flag === 'unreadable',
            otherDisk: place.same_volume === false,
            split: said.split && parts.length ? engineSplit({ ...place, parts }) : '',
            parts: said.parts ? parts.filter((row) => isSize(row?.bytes)) : [],
            rest: said.parts && place.rest && place.rest.count > 0 ? place.rest : null,
        };
    });
}

/** The groups with members, in display order: bytes summed over the known members (unknown when none is
 *  known: never zero), growth summed only when every sized member has one. */
export function groupRows(rows) {
    return STORAGE_GROUPS.map((group) => {
        const members = rows.filter((row) => row.group === group.id);
        const sized = members.filter((row) => row.bytes !== null);
        return {
            id: group.id,
            label: fmt(group.label),
            members,
            bytes: sized.length ? sized.reduce((sum, row) => sum + row.bytes, 0) : null,
            growth: sized.length && sized.every((row) => row.growth !== null) ? sized.reduce((sum, row) => sum + row.growth, 0) : null,
            caveat: members.some((row) => row.caveat),
        };
    }).filter((group) => group.members.length);
}

/** One segment per group with bytes, its share of the finished headline (the shares sum to 100). */
export function barSegments(count, days = 7) {
    const groups = groupRows(placeRows(count, days)).filter((group) => group.bytes > 0);
    const total = groups.reduce((sum, group) => sum + group.bytes, 0);
    if (!(total > 0)) return [];
    return groups.map((group) => ({ group: group.id, bytes: group.bytes, share: Number(((group.bytes / total) * 100).toFixed(2)) }));
}

/** 'loading' before the first answer; 'counting' while a count runs; 'counted' with a saved count;
 *  'failed' after a failure with nothing saved; 'never' before any count. */
export function storageState(response) {
    if (!response) return 'loading';
    if (response.counting) return 'counting';
    if (response.count) return 'counted';
    return response.error && !response.error.stopped ? 'failed' : 'never';
}

function factsLine(count, now) {
    const parts = [fmt('Last count: {when}', { when: whenLabel(count.finished_at, now) })];
    const took = durationLabel(count.duration_seconds);
    if (took) parts.push(fmt('took {duration}', { duration: took }));
    if (isSize(count.growth_bytes) && count.growth_since) {
        const when = whenLabel(count.growth_since, now);
        const growth = count.growth_bytes === 0 ? fmt('no change since {when}', { when })
            : fmt(count.growth_bytes > 0 ? '+{size} since {when}' : '−{size} since {when}', { size: formatBytes(Math.abs(count.growth_bytes)), when });
        parts.push(count.growth_flag === 'unreadable' ? `${growth} ${fmt('(some items could not be read)')}` : growth);
    }
    return parts.join(' · ');
}

function progressHtml(counting) {
    const sentence = fmt('Measuring {place} · {current} · {size} so far · {done} of {total} places. You can leave this tab.', {
        place: words({ id: counting.place }).label ? fmt(words({ id: counting.place }).label) : '',
        current: counting.current || '',
        size: formatBytes(counting.bytes || 0),
        done: Math.min(Number(counting.places_done || 0) + 1, Number(counting.places_total || 0)),
        total: number(counting.places_total),
    });
    return `<p class="storage-progress" data-i18n-fmt="storage.progress">${DOTS}<span>${text(sentence)}</span></p>`;
}

/** The places already finished in a first count, under the progress line; nothing is summed into a headline. */
function firstCountHtml(counting) {
    const done = (Array.isArray(counting.done) ? counting.done : []).filter((row) => isSize(row?.bytes) && PLACE_WORDS[row.name]);
    if (!done.length) return '';
    return `<ul class="storage-first-count">${done.map((row) => `<li><span>${text(fmt(PLACE_WORDS[row.name].label))}</span>
        <span data-i18n-fmt="storage.size">${text(formatBytes(row.bytes))}</span></li>`).join('')}</ul>`;
}

function statusSentence(error, count, now) {
    const when = count ? whenLabel(count.finished_at, now) : '';
    const reason = String(error.message || '').replace(/\.$/, '');  // the server's reason is a sentence
    if (error.stopped) return count ? fmt('Stopped before it finished. Showing the count from {when}.', { when }) : fmt('Stopped before it finished.');
    return count ? fmt('This count stopped: {reason}. Showing the count from {when}.', { reason, when })
        : fmt('This count stopped: {reason}.', { reason });
}

function statusHtml(error, count, now) {
    return `<p class="ui-status storage-status" data-tone="warn" data-i18n-fmt="storage.error">${text(statusSentence(error, count, now))}</p>`;
}

/** One stable sentence per phase for the live region: a screen reader hears a count start, end, stop or
 *  fail, and the tray's phases, never the per-second figures of the progress line. */
export function announceSentence(response, { readFailed = false, now = new Date(), tray = null } = {}) {
    const parts = [];
    if (readFailed) parts.push(fmt('Storage status could not be read.'));
    if (response) {
        const { count, counting, error } = response;
        if (counting) parts.push(fmt('Measuring. You can leave this tab.'));
        else if (error) parts.push(statusSentence(error, count, now));
        else if (count) parts.push(fmt('Ouroboros uses {size}.', { size: formatBytes(count.bytes) }));
        else parts.push(fmt('Not counted yet.'));
        const free = freeOf(response);
        const state = trayState(free, tray?.dismissed);
        if (state === 'listing') parts.push(fmt('Checking what can go. Nothing is removed.'));
        else if (state === 'running') parts.push(fmt('Freeing space. You can leave this tab.'));
        else if (state === 'done') parts.push(doneSentences(free)[0]);
        else if (state === 'failed') parts.push(fmt('Temporary space could not be listed. Nothing was removed.'));
    }
    return parts.filter(Boolean).join(' ');
}

function summaryHtml(response, { readFailed, now }) {
    const kicker = '<div class="storage-kicker">Ouroboros uses</div>';
    if (!response) {
        return `${kicker}<div class="storage-total is-unknown">${readFailed ? 'Could not be read' : 'Loading…'}</div>`;
    }
    const { count, counting, error, volume } = response;
    const failed = Boolean(error && !error.stopped && !count && !counting);
    const total = count ? formatBytes(count.bytes) : failed ? fmt('Could not be counted') : fmt('Not counted yet');
    const lines = [];
    if (isSize(volume?.free_bytes) && isSize(volume?.total_bytes)) {
        lines.push(`<div class="storage-meta" data-i18n-fmt="storage.free">${text(fmt('{free} free of {total} on the disk the data folder is on', {
            free: formatBytes(volume.free_bytes), total: formatBytes(volume.total_bytes),
        }))}</div>`);
    }
    if (count) {
        lines.push(`<div class="storage-meta" data-i18n-fmt="storage.facts">${text(factsLine(count, now))}</div>`);
        if (count.unreadable > 0) {
            lines.push(`<div class="storage-meta" data-i18n-fmt="storage.unreadable">${text(plural(count.unreadable, '{n} item could not be read', '{count} items could not be read'))}</div>`);
        }
    }
    if (counting) {
        lines.push(progressHtml(counting));
        if (!count) lines.push(firstCountHtml(counting));
    } else if (error) {
        lines.push(statusHtml(error, count, now));
    } else if (!count) {
        lines.push('<div class="storage-meta">Measuring reads file sizes only and changes nothing. It can take a while on a large data folder.</div>');
    }
    if (readFailed) lines.push('<p class="ui-status storage-status" data-tone="warn">Storage status could not be read. Showing the last answer.</p>');
    return `${kicker}<div class="storage-total${count ? '' : ' is-unknown'}" data-i18n-fmt="storage.total">${text(total)}</div>${lines.join('')}`;
}

function sizeCells(row) {
    const size = row.bytes === null ? '' : formatBytes(row.bytes);
    const growth = row.growth === null || row.growth === 0 ? '' : signed(row.growth);
    return `<span class="storage-place-size${size ? '' : ' is-unknown'}" data-i18n-fmt="storage.size">${text(size)}</span>
        <span class="storage-place-delta" data-i18n-fmt="storage.growth">${text(growth)}</span>`;
}

/** The reason, then what qualifies the row: unreadable, on another disk, a growth some items escaped. */
function whyLine(row) {
    const notes = [row.why];
    if (row.bytes === null) notes.push(fmt('Could not be read.'));
    if (row.otherDisk) notes.push(fmt('on another disk'));
    if (row.caveat && row.growth) notes.push(fmt('(some items could not be read)'));
    return `<span class="storage-place-why" data-i18n-fmt="storage.why">${text(notes.filter(Boolean).join(' · '))}</span>`;
}

function partsHtml(row) {
    if (!row.parts.length) return '';
    const items = row.parts.map((part) => `<li><span data-i18n-authored>${text(part.name)}</span><span data-i18n-fmt="storage.size">${text(formatBytes(part.bytes))}</span></li>`);
    if (row.rest) items.push(`<li class="storage-parts-rest" data-i18n-fmt="storage.rest">${text(fmt('and {n} more, {size}', { n: number(row.rest.count), size: formatBytes(row.rest.bytes) }))}</li>`);
    return `<details class="activity-history storage-parts" data-key="parts-${attr(row.id)}">
            <summary>Largest parts</summary>
            <ul class="storage-parts-list">${items.join('')}</ul>
        </details>`;
}

function memberHtml(row) {
    return `<li class="storage-place is-member" data-storage-place="${attr(row.id)}">
            <span class="storage-place-label">${text(row.label)}</span>${sizeCells(row)}
            ${whyLine(row)}
            ${row.split ? `<span class="storage-place-split" data-i18n-fmt="storage.split">${text(row.split)}</span>` : ''}
            ${partsHtml(row)}
        </li>`;
}

function groupHtml(group) {
    const head = `<div class="storage-group-head"><span class="storage-key" data-group="${attr(group.id)}"></span>
        <span class="storage-group-label">${text(group.label)}</span>${sizeCells(group)}</div>`;
    // A group of one place carries that place's words under its heading; a larger group lists its places.
    if (group.members.length === 1) {
        const row = group.members[0];
        return `<li class="storage-group" data-storage-group="${attr(group.id)}" data-storage-place="${attr(row.id)}">${head}
            <div class="storage-group-body">${whyLine(row)}
            ${row.split ? `<span class="storage-place-split" data-i18n-fmt="storage.split">${text(row.split)}</span>` : ''}
            ${partsHtml(row)}</div></li>`;
    }
    return `<li class="storage-group" data-storage-group="${attr(group.id)}">${head}
        <ul class="storage-group-places">${group.members.map(memberHtml).join('')}</ul></li>`;
}

function placesHtml(response) {
    const count = response?.count;
    if (!count) return '';
    const days = response.retention_days ?? 7;
    // The bar pictures the last finished count only; its label is fixed chrome, the list carries the names
    // and sizes (the language overlay translates an aria-label by its English text).
    const segments = barSegments(count, days).map((segment) => `<span class="storage-bar-seg" data-group="${attr(segment.group)}" data-share="${segment.share}"></span>`);
    const bar = segments.length ? `<div class="storage-bar" role="img" aria-label="Share of each group; sizes are listed below">${segments.join('')}</div>` : '';
    return `<h3 class="storage-card-title">Where it is</h3>${bar}
        <ul class="storage-groups">${groupRows(placeRows(count, days)).map(groupHtml).join('')}</ul>`;
}

export const freeOf = (response) => response?.free || { state: 'idle', groups: [], run: null, error: null, done: 0, total: 0 };

/** The whole view for one response: markup per region, the measure action's label and busy flag, whether
 *  Stop is offered, and the tray for the owner's checklist state (`tray: {checked, dismissed}`). */
export function renderStorage(response, { pending = false, readFailed = false, now = new Date(), tray = null } = {}) {
    const state = storageState(response);
    const counting = state === 'counting' || (pending && !response?.count);
    const busy = Boolean(pending || response?.counting);
    const label = counting ? 'Measuring…' : response?.count ? 'Measure again' : state === 'failed' ? 'Try again' : 'Measure now';
    return {
        state,
        summary: summaryHtml(response, { readFailed, now }),
        announce: announceSentence(response, { readFailed, now, tray }),
        places: placesHtml(response),
        tray: response ? trayHtml(freeOf(response), tray || { checked: new Set(), dismissed: false }, response.retention_days ?? 7) : '',
        measure: { label, busy },
        stop: Boolean(response?.counting),
    };
}

// ---------------------------------------------------------------------------
// The Dashboard tab
// ---------------------------------------------------------------------------

/** Replace a region only when its markup changed, keeping open disclosures and the focused control by key. */
function patch(node, html, painted) {
    if (painted.get(node) === html) return;
    painted.set(node, html);
    const open = Array.from(node.querySelectorAll('details[open][data-key]'), (element) => element.dataset.key);
    const focused = node.contains(document.activeElement) ? document.activeElement?.dataset?.key : '';
    node.innerHTML = html;
    open.forEach((key) => node.querySelector(`details[data-key="${key}"]`)?.setAttribute('open', ''));
    if (focused) node.querySelector(`[data-key="${focused}"]`)?.focus();
    // A bar share is a measured runtime value: one narrowly named custom property.
    node.querySelectorAll('[data-share]').forEach((element) => element.style?.setProperty('--storage-share', element.dataset.share));
}

export function initStorage({ state, mount } = {}) {
    if (!mount) return null;
    const page = document.createElement('div');
    page.id = 'page-storage';
    page.className = 'settings-embedded-content';
    page.innerHTML = `
        <div class="costs-scroll">
            <div class="storage-root">
                <section class="storage-summary ui-card" aria-label="Storage summary">
                    <p class="storage-announce" data-storage-live role="status" aria-live="polite"></p>
                    <div class="storage-summary-head">
                        <div class="storage-summary-text" data-storage-summary></div>
                        <div class="storage-actions storage-summary-actions">
                            <button type="button" class="btn btn-default btn-sm storage-refresh" data-storage-action="measure">Measure now</button>
                            <button type="button" class="btn btn-default btn-sm" data-storage-action="stop" hidden>Stop</button>
                        </div>
                    </div>
                </section>
                <section class="storage-panel ui-card" aria-label="Where it is" data-storage-panel hidden>
                    <div data-storage-places></div>
                    <div class="storage-tray" data-storage-tray></div>
                </section>
            </div>
        </div>
    `;
    mount.appendChild(page);
    const regions = {
        summary: page.querySelector('[data-storage-summary]'),
        places: page.querySelector('[data-storage-places]'),
        tray: page.querySelector('[data-storage-tray]'),
    };
    // The one live region, never replaced: it carries one sentence per phase (announceSentence), so a
    // screen reader hears phase changes and not the figures the progress line repaints every second.
    const live = page.querySelector('[data-storage-live]');
    const panel = page.querySelector('[data-storage-panel]');
    // The owner's checklist: a new preview starts with every group checked, a group listed later too; the
    // choices hold until the next preview. Cancel hides the list without asking the server anything.
    const ui = { checked: new Set(), known: new Set(), checkedFor: null, dismissed: false };
    // The measure node is never replaced, and a running count marks it aria-disabled rather than disabled,
    // so keyboard focus stays on it through a count and every repaint.
    const measureButton = page.querySelector('[data-storage-action="measure"]');
    const stopButton = page.querySelector('[data-storage-action="stop"]');
    const painted = new Map();
    let response = null;
    let readFailed = false;
    let pending = false;  // any POST out: a second one is dropped until it answers
    let measuring = false;  // the measure POST out: the one that reads "Measuring…" before the server does
    let disposed = false;
    let generation = 0;
    let controller = null;
    let timer = 0;

    const onScreen = () => !document.hidden && state?.activePage === 'dashboard' && state?.dashboardActiveSubtab === 'storage';

    function paint() {
        const free = freeOf(response);
        if (free.started_at !== ui.checkedFor) {
            ui.checkedFor = free.started_at;
            ui.checked = new Set();
            ui.known = new Set();
        }
        for (const group of groupsOf(free)) {
            if (ui.known.has(group.id)) continue;
            ui.known.add(group.id);
            ui.checked.add(group.id);
        }
        const view = renderStorage(response, { pending: measuring, readFailed, tray: ui });
        patch(regions.summary, view.summary, painted);
        if (live.textContent !== view.announce) live.textContent = view.announce;
        patch(regions.places, view.places, painted);
        patch(regions.tray, view.tray, painted);
        panel.hidden = !response;
        if (measureButton.textContent !== view.measure.label) measureButton.textContent = view.measure.label;
        measureButton.setAttribute('aria-disabled', view.measure.busy ? 'true' : 'false');
        if (view.measure.busy) measureButton.setAttribute('aria-busy', 'true');
        else measureButton.removeAttribute('aria-busy');
        stopButton.hidden = !view.stop;
    }

    function stopPolling() {
        clearTimeout(timer);
        timer = 0;
    }

    // A passive GET is only useful while this panel is the visible tab. Abort it as soon as the owner
    // leaves the panel so a hidden read cannot repaint a later view, and advance the generation first.
    function cancelRead() {
        generation += 1;
        controller?.abort();
        controller = null;
        stopPolling();
    }

    const working = () => Boolean(response?.counting) || ['listing', 'running'].includes(freeOf(response).state)
        || freeOf(response).run?.deleting > 0;

    function schedule() {
        stopPolling();
        if (!disposed && working() && onScreen()) timer = setTimeout(refresh, POLL_MS);
    }

    /** A read that names the count it holds gets no `count`: the held one stays. */
    function apply(next) {
        response = { ...next, count: 'count' in next ? next.count : (response?.count ?? null) };
        readFailed = false;
        paint();
        schedule();
    }

    async function refresh() {
        if (disposed) return;
        stopPolling();
        controller?.abort();
        const mine = ++generation;
        const abort = new AbortController();
        controller = abort;
        try {
            const next = await apiClient.storage({ signal: abort.signal, count: response?.count?.finished_at || '' });
            if (!disposed && mine === generation) apply(next);
        } catch {
            if (disposed || mine !== generation || abort.signal.aborted) return;
            readFailed = true;
            paint();
            schedule();
        } finally {
            if (controller === abort) controller = null;
        }
    }

    /** A tray action replaces the control it came from; keyboard focus moves to the tray's next
     *  control (Stop while it works, the door afterwards) instead of falling to the body. */
    function refocusTray() {
        if (disposed || regions.tray.contains(document.activeElement)) return;
        (regions.tray.querySelector('[data-free-stop]') || regions.tray.querySelector('button'))?.focus();
    }

    /** One POST whose answer is the same envelope as the GET; `measure` marks the measure POST. */
    async function act(request, failure, { measure = false } = {}) {
        if (disposed || pending) return;
        pending = true;
        measuring = measure;
        paint();
        controller?.abort();
        const mine = ++generation;
        try {
            const next = await request();
            if (disposed) return;
            // A newer read or a hide moved the generation while the POST was out: its answer is stale,
            // and a hidden tab reads nothing until it is shown again (onSubtab/onPage).
            if (mine === generation) apply(next);
            else if (onScreen()) void refresh();
        } catch (error) {
            if (disposed) return;
            showToast(error?.message || fmt(failure), 'warn');
            if (mine === generation && onScreen()) void refresh();
        } finally {
            pending = false;
            measuring = false;
            if (!disposed) paint();
        }
    }

    function measure() {
        if (response?.counting || measureButton.getAttribute('aria-disabled') === 'true') return Promise.resolve();
        return act(() => apiClient.measureStorage(), 'Storage could not be measured.', { measure: true });
    }

    const stopCount = () => act(() => apiClient.cancelStorageMeasure(), 'The count could not be stopped.');
    const stopFree = () => act(() => apiClient.cancelStorageFree(), 'Freeing could not be stopped.').then(refocusTray);

    function preview() {
        ui.dismissed = false;
        return act(() => apiClient.previewStorageFree(), 'Temporary space could not be listed. Nothing was removed.').then(refocusTray);
    }

    /** The shared confirm dialog, then one POST naming the plan and the checked groups. */
    async function confirmFree() {
        const free = freeOf(response);
        const groups = groupsOf(free).filter((group) => ui.checked.has(group.id)).map((group) => group.id);
        if (trayState(free, ui.dismissed) !== 'ready' || !groups.length || pending) return;
        const confirmed = await openConfirmDialog(confirmCopy(free, ui.checked));
        if (!confirmed || disposed) return;
        await act(async () => {
            const next = await apiClient.freeStorage(free.plan, groups);
            if (next?.started === false) showToast(notStarted(next.reason), 'warn');
            return next;
        }, 'Nothing was removed.');
        refocusTray();
    }

    const onClick = (event) => {
        if (event.currentTarget === measureButton) void measure();
        else if (event.currentTarget === stopButton) void stopCount();
    };
    const onTrayClick = (event) => {
        const target = event.target?.closest?.('button');
        if (!target || target.disabled) return;
        if (target.hasAttribute('data-free-preview')) void preview();
        else if (target.hasAttribute('data-free-cancel')) {
            ui.dismissed = true;
            paint();
            refocusTray();
        } else if (target.hasAttribute('data-free-go')) void confirmFree();
        else if (target.hasAttribute('data-free-stop')) void stopFree();
    };
    const onTrayChange = (event) => {
        const box = event.target?.closest?.('[data-free-group]');
        if (!box) return;
        if (box.checked) ui.checked.add(box.dataset.freeGroup);
        else ui.checked.delete(box.dataset.freeGroup);
        paint();
    };
    const onSubtab = (event) => {
        if (event?.detail?.tab === 'storage' && onScreen()) void refresh();
        else cancelRead();
    };
    const onPage = () => {
        if (onScreen()) void refresh();
        else cancelRead();
    };
    measureButton.addEventListener('click', onClick);
    stopButton.addEventListener('click', onClick);
    regions.tray.addEventListener('click', onTrayClick);
    regions.tray.addEventListener('change', onTrayChange);
    window.addEventListener('ouro:dashboard-subtab-shown', onSubtab);
    window.addEventListener('ouro:page-shown', onPage);
    document.addEventListener('visibilitychange', onPage);

    function destroy() {
        if (disposed) return;
        disposed = true;
        cancelRead();
        measureButton.removeEventListener('click', onClick);
        stopButton.removeEventListener('click', onClick);
        regions.tray.removeEventListener('click', onTrayClick);
        regions.tray.removeEventListener('change', onTrayChange);
        window.removeEventListener('ouro:dashboard-subtab-shown', onSubtab);
        window.removeEventListener('ouro:page-shown', onPage);
        document.removeEventListener('visibilitychange', onPage);
    }

    paint();
    if (onScreen()) void refresh();
    return { refresh, measure, destroy };
}
