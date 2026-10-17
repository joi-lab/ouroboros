// Dashboard -> Storage (issue #1418): what the data folder holds, measured only when the owner asks.
// `POST /api/storage/measure` starts one bounded walk on the server; `GET /api/storage` is its
// passive projection, polled only while a walk runs AND this tab is on screen. The view reports
// and has no cleanup or settings control. Unknown is never zero: an unreadable or unmeasured part
// has no size and no bar length, and a partly counted total or group reads "At least".

import { apiClient } from './api_client.js';
import { fmt } from './i18n.js';
import { strokeIcon } from './page_icons.js';
import { showToast } from './toast.js';
import { escapeHtmlAttr, escapeHtmlText } from './utils.js';

const POLL_MS = 1000;

/** Why each part is kept: the bar's hue and the list's grouping, in display order. */
export const STORAGE_GROUPS = [
    {
        id: 'exact',
        label: 'Model call records',
        why: 'Kept indefinitely by design: the exact prompts, answers and tool results, so any decision can be traced back to what the model saw.',
    },
    {
        id: 'kept',
        label: 'Your work and history',
        why: 'Results, memory, archives and installed skills. Each part is kept by its own lifecycle; this screen makes no deletion decision.',
    },
    {
        id: 'temporary',
        label: 'Task work folders',
        why: 'Working folders of tasks. The existing cleanup removes one after the retention period, once its task has settled and its results and sources are saved.',
    },
    {
        id: 'other',
        label: 'Other app data',
        why: 'Settings, logs, attachments, caches and service state. Each part follows its own lifecycle.',
    },
];

// Server category id -> [group, label]; an id this client does not know joins "other" by name.
const CATEGORY_LABELS = {
    observability: ['exact', 'Model call records'],
    results: ['kept', 'Task results and files'],
    memory: ['kept', 'Memory and project notes'],
    archives: ['kept', 'Archives and backups'],
    skills: ['kept', 'Skills'],
    task_drives: ['temporary', 'Task work folders'],
    other: ['other', 'Other app data'],
};

// Excluded locations: never walked, wherever they are configured, and never part of the total.
const NOT_MEASURED_ROWS = {
    engine: ['Agent engine data', 'Excluded location: kept by the agent engine.'],
    worktrees: ['Subagent working copies', 'Excluded location, configured separately.'],
    project_folders: ['Project folders', 'Excluded location, configured separately.'],
};

// The house stroke vector beside the words it marks; the sentence carries the meaning.
const INFO_ICON = `<span class="storage-icon" aria-hidden="true">${strokeIcon('<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>', 14)}</span>`;

// ---------------------------------------------------------------------------
// Pure presentation
// ---------------------------------------------------------------------------

const isCounted = (row) => typeof row?.bytes === 'number' && Number.isFinite(row.bytes);
const groupOf = (row) => CATEGORY_LABELS[row?.id]?.[0] || 'other';
const labelOf = (row) => CATEGORY_LABELS[row?.id]?.[1] || String(row?.id || '');
const count = (n, one, many) => (Number(n) === 1 ? fmt(one, { n }) : fmt(many, { n, count: Number(n).toLocaleString() }));

/** Decimal units, one decimal below 10 of a unit (1.0 MB, 1.4 MB, 12 MB) so neighbouring rows stay
 *  distinguishable. Null or non-numeric stays empty: unknown is never zero. */
export function formatBytes(bytes) {
    if (typeof bytes !== 'number' || !Number.isFinite(bytes)) return '';
    if (bytes < 1e3) return count(bytes, '{n} byte', '{count} bytes');
    // The unit is chosen after rounding, so 999.96 KB reads 1.0 MB rather than 1000 KB.
    const [unit, size] = bytes >= 999.5e6 ? ['GB', 1e9] : bytes >= 999.5e3 ? ['MB', 1e6] : ['KB', 1e3];
    const value = bytes / size;
    return `${value < 9.95 ? value.toFixed(1) : Math.round(value)} ${unit}`;
}

const atLeast = (bytes, partial) => (partial ? fmt('At least {size}', { size: formatBytes(bytes) }) : formatBytes(bytes));

/** 'loading' before the first answer, then 'measured', 'unavailable' (a failure, nothing kept) or 'never'. */
export function storageState(response) {
    if (!response) return 'loading';
    if (response.measurement) return 'measured';
    return response.error ? 'unavailable' : 'never';
}

/** Counted rows under their group in display order; unreadable and excluded parts apart, with no size.
 *  A group with any partly counted OR unreadable member is itself only a lower bound. */
export function storageBreakdown(measurement) {
    const rows = Array.isArray(measurement?.categories) ? measurement.categories : [];
    const groups = STORAGE_GROUPS.map((group) => {
        const all = rows.filter((row) => groupOf(row) === group.id);
        const members = all.filter(isCounted).map((row) => ({ ...row, label: labelOf(row) }));
        return {
            ...group,
            rows: members,
            bytes: members.reduce((total, row) => total + row.bytes, 0),
            partial: all.some((row) => !isCounted(row) || row.coverage !== 'complete'),
        };
    }).filter((group) => group.rows.length);
    const unmeasured = [
        ...rows.filter((row) => !isCounted(row)).map((row) => ({ id: row.id, label: labelOf(row), note: 'Could not be read.' })),
        ...(Array.isArray(measurement?.not_measured) ? measurement.not_measured : []).map((id) => ({
            id, label: NOT_MEASURED_ROWS[id]?.[0] || String(id), note: NOT_MEASURED_ROWS[id]?.[1] || '',
        })),
    ];
    return { groups, counted: groups.reduce((total, group) => total + group.bytes, 0), unmeasured };
}

/** One bar segment per counted row, its share taken of the COUNTED bytes only. */
export function barSegments(breakdown) {
    const total = breakdown?.counted || 0;
    if (!(total > 0)) return [];
    return breakdown.groups.flatMap((group) => group.rows.filter((row) => row.bytes > 0).map((row) => ({
        group: group.id,
        id: row.id,
        label: row.label,
        bytes: row.bytes,
        partial: row.coverage !== 'complete',
        share: Number(((row.bytes / total) * 100).toFixed(2)),
    })));
}

/** One sentence saying how incomplete the counted rows are, or '' when every row is complete. */
export function coverageSentence(measurement) {
    const rows = Array.isArray(measurement?.categories) ? measurement.categories : [];
    const partial = rows.filter((row) => row.coverage === 'partial').length;
    const unreadable = rows.filter((row) => row.coverage === 'unreadable').length;
    const parts = [
        partial ? count(partial, '{n} part was only partly counted', '{count} parts were only partly counted') : '',
        unreadable ? count(unreadable, '{n} part could not be read', '{count} parts could not be read') : '',
    ].filter(Boolean);
    return parts.length ? fmt('Partial picture: {parts}. The complete total is unknown.', { parts: parts.join('; ') }) : '';
}

/** Why one counted row is only a lower bound, every reason at once, or '' when it is complete. */
function rowNote(row) {
    const reasons = [
        row.stopped === 'time_limit' ? fmt('Counting stopped at the time limit') : '',
        row.stopped === 'entry_limit' ? fmt('Counting stopped at the file limit') : '',
        row.errors > 0 ? count(row.errors, '{n} item could not be read', '{count} items could not be read') : '',
        row.skipped > 0 ? count(row.skipped, '{n} item was not counted', '{count} items were not counted') : '',
    ].filter(Boolean);
    return reasons.length ? fmt('{reasons}; the complete size is unknown.', { reasons: reasons.join('; ') }) : '';
}

export function whenLabel(iso, now = new Date()) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (date.toDateString() === now.toDateString()) return fmt('today at {time}', { time });
    return fmt('{date} at {time}', { date: date.toLocaleDateString([], { day: 'numeric', month: 'short' }), time });
}

function durationLabel(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return '';
    return fmt('took {seconds} s', { seconds: value < 10 ? value.toFixed(1) : String(Math.round(value)) });
}

const text = escapeHtmlText;
const attr = escapeHtmlAttr;

function summaryHtml(state, response, { busy, pending, readFailed, now }) {
    // The headline is the counted bytes, never an implied total of the data folder: excluded
    // locations and unread parts are outside it.
    const kicker = '<div class="storage-kicker">Measured data</div>';
    if (state === 'measured') {
        const measurement = response.measurement;
        const facts = [
            fmt('Measured {when}', { when: whenLabel(measurement.finished_at, now) }),
            durationLabel(measurement.duration_seconds),
            typeof measurement.free_bytes === 'number' ? fmt('{size} free on this disk', { size: formatBytes(measurement.free_bytes) }) : '',
        ].filter(Boolean);
        return `${kicker}
            <div class="storage-total" data-i18n-fmt="storage.total">${text(atLeast(measurement.measured_bytes, !measurement.complete))}</div>
            <div class="storage-meta" data-i18n-fmt="storage.meta">${text(facts.join(' · '))}</div>`;
    }
    if (busy) {
        // A failed poll leaves only the last answer's "measuring": it says so while the passive
        // poll keeps asking. A POST in flight is a fresh answer of its own.
        const meta = readFailed && !pending
            ? '<div class="ui-status storage-meta" data-tone="warn">Storage status could not be read. The last answer was still measuring; checking again.</div>'
            : '<div class="storage-meta">Reading file sizes. Nothing is changed.</div>';
        return `${kicker}<div class="storage-total is-unknown">Measuring…</div>
            ${meta}`;
    }
    if (state === 'unavailable' || readFailed) {
        const reason = response?.error?.message || fmt('Storage status could not be read.');
        return `${kicker}<div class="storage-total is-unknown">Unavailable</div>
            <div class="storage-meta" data-i18n-fmt="storage.error">${text(fmt('{reason} No size is shown rather than a wrong one.', { reason }))}</div>`;
    }
    if (state === 'loading') return `${kicker}<div class="storage-total is-unknown">Loading…</div>`;
    return `${kicker}<div class="storage-total is-unknown">Not measured yet</div>
        <div class="storage-meta">Measuring reads file sizes only and changes nothing. It can take a little while on a large data folder.</div>`;
}

function notesHtml(state, response, { readFailed, now }) {
    if (state !== 'measured') return '';
    const notes = [];
    const coverage = coverageSentence(response.measurement);
    if (coverage) notes.push(`<p class="storage-coverage">${INFO_ICON}<span data-i18n-fmt="storage.coverage">${text(coverage)}</span></p>`);
    if (response.error) {
        const failed = fmt('The last refresh failed ({when}): {reason} Showing the earlier measurement.', {
            when: whenLabel(response.error.at, now), reason: response.error.message,
        });
        notes.push(`<p class="ui-status storage-status" data-tone="warn" data-i18n-fmt="storage.refresh">${text(failed)}</p>`);
    } else if (readFailed) {
        notes.push('<p class="ui-status storage-status" data-tone="warn">Storage status could not be read. Showing the last answer.</p>');
    }
    return notes.join('');
}

function rowHtml(row, size, why, coverage) {
    return `<li class="storage-row" data-storage-category="${attr(row.id)}" data-coverage="${attr(coverage)}">
                <span class="storage-row-label">${text(row.label)}</span>
                <span class="storage-row-why" data-i18n-fmt="storage.row">${text(why)}</span>
                <span class="storage-row-size${size ? '' : ' is-unknown'}" data-i18n-fmt="storage.size">${text(size || fmt('Not measured'))}</span>
            </li>`;
}

function groupHtml(group) {
    // A group that is its own single row needs no list under it: its count and note join the head.
    const self = group.rows.length === 1 && group.rows[0].label === group.label ? group.rows[0] : null;
    const head = `<div class="storage-group-head">
                <span class="storage-key" data-group="${attr(group.id)}"></span>
                <h4 class="storage-group-title">${text(group.label)}${self ? `<span class="storage-group-count" data-i18n-fmt="storage.files">${text(count(self.files, '{n} file', '{count} files'))}</span>` : ''}</h4>
                <span class="storage-group-size" data-i18n-fmt="storage.size">${text(atLeast(group.bytes, group.partial))}</span>
            </div>
            <p class="storage-group-why">${text(group.why)}</p>`;
    const note = self ? rowNote(self) : '';
    const body = self
        ? (note ? `<p class="storage-group-note" data-i18n-fmt="storage.note">${text(note)}</p>` : '')
        : `<ul class="storage-list">${group.rows.map((row) => rowHtml(row, atLeast(row.bytes, row.coverage !== 'complete'),
            [count(row.files, '{n} file', '{count} files'), rowNote(row)].filter(Boolean).join(' · '), row.coverage)).join('')}</ul>`;
    return `<section class="storage-group" data-storage-group="${attr(group.id)}" data-coverage="${self ? attr(self.coverage) : ''}">${head}${body}</section>`;
}

function bodyHtml(state, response) {
    if (state !== 'measured') return '';
    const breakdown = storageBreakdown(response.measurement);
    // The language overlay translates every title and aria-label as fixed chrome, so the bar's are
    // fixed English: a size or a list of names would mint a new string per measurement. The groups
    // below carry the sizes and names.
    const segments = barSegments(breakdown).map((segment) => `<span class="storage-bar-seg" data-group="${attr(segment.group)}"
        data-share="${segment.share}" title="${attr(segment.label)}"></span>`);
    // The dashed marker stands for everything with no counted size: excluded or unread parts and the
    // uncounted rest of partly counted rows. It has a fixed width and is never part of a share.
    const uncounted = breakdown.unmeasured.length > 0
        || breakdown.groups.some((group) => group.rows.some((row) => row.coverage !== 'complete'));
    if (uncounted) segments.push('<span class="storage-bar-seg is-unmeasured" title="No measured size"></span>');
    const groups = breakdown.groups.map(groupHtml);
    if (breakdown.unmeasured.length) {
        groups.push(`<section class="storage-group" data-storage-group="unmeasured">
            <div class="storage-group-head">
                <span class="storage-key is-unmeasured"></span>
                <h4 class="storage-group-title">Not measured here</h4>
                <span class="storage-group-size is-unknown">Size unknown</span>
            </div>
            <p class="storage-group-why">Excluded locations and parts that could not be read. Not part of the total above.</p>
            <ul class="storage-list">${breakdown.unmeasured.map((row) => rowHtml(row, '', row.note ? fmt(row.note) : '', 'not_measured')).join('')}</ul>
        </section>`);
    }
    return `<section class="storage-panel ui-card" aria-label="What is taking space">
            <p class="storage-meta">Counted bytes only. A dashed marker has no measured size.</p>
            <div class="storage-bar" role="img" aria-label="Counted bytes by kind of data; sizes are listed below">${segments.join('')}</div>
            <div class="storage-groups">${groups.join('')}</div>
        </section>`;
}

/** The whole view for one response, as markup per region plus the action's label and busy flag. */
export function renderStorage(response, { pending = false, readFailed = false, now = new Date() } = {}) {
    const state = storageState(response);
    const busy = Boolean(pending || response?.measuring);
    const options = { busy, pending, readFailed, now };
    const label = busy ? 'Measuring…'
        : state === 'measured' ? 'Refresh'
            : state === 'unavailable' || readFailed ? 'Try again' : 'Measure now';
    return {
        state,
        summary: summaryHtml(state, response, options),
        notes: notesHtml(state, response, options),
        body: bodyHtml(state, response),
        action: { label, busy },
    };
}

// ---------------------------------------------------------------------------
// The Dashboard tab
// ---------------------------------------------------------------------------

export function initStorage({ state, mount } = {}) {
    if (!mount) return null;
    const page = document.createElement('div');
    page.id = 'page-storage';
    page.className = 'settings-embedded-content';
    page.innerHTML = `
        <div class="costs-scroll">
            <div class="storage-root">
                <section class="storage-summary ui-card" aria-label="Storage summary">
                    <div class="storage-summary-head">
                        <div class="storage-summary-text" data-storage-summary aria-live="polite"></div>
                        <button type="button" class="btn btn-default btn-sm storage-refresh" data-storage-action="measure">Measure now</button>
                    </div>
                    <div class="storage-notes" data-storage-notes></div>
                </section>
                <div class="storage-body" data-storage-body></div>
                <p class="storage-footnote">This view only reports. It never removes or changes anything.</p>
            </div>
        </div>
    `;
    mount.appendChild(page);
    const regions = {
        summary: page.querySelector('[data-storage-summary]'),
        notes: page.querySelector('[data-storage-notes]'),
        body: page.querySelector('[data-storage-body]'),
    };
    // The one action node is never replaced, and a running walk marks it aria-disabled rather
    // than disabled, so keyboard focus stays on it through a measurement and every repaint.
    const button = page.querySelector('[data-storage-action="measure"]');
    const painted = {};
    let response = null;
    let readFailed = false;
    let pending = false;
    let disposed = false;
    let generation = 0;
    let controller = null;
    let timer = 0;

    const onScreen = () => !document.hidden && state?.activePage === 'dashboard' && state?.dashboardActiveSubtab === 'storage';

    function paint() {
        const view = renderStorage(response, { pending, readFailed });
        for (const [name, node] of Object.entries(regions)) {
            if (painted[name] === view[name]) continue;
            painted[name] = view[name];
            node.innerHTML = view[name];
        }
        // A bar share is a measured runtime value: one narrowly named custom property.
        regions.body.querySelectorAll('[data-share]').forEach((node) => node.style?.setProperty('--storage-share', node.dataset.share));
        if (button.textContent !== view.action.label) button.textContent = view.action.label;
        button.setAttribute('aria-disabled', view.action.busy ? 'true' : 'false');
        if (view.action.busy) button.setAttribute('aria-busy', 'true');
        else button.removeAttribute('aria-busy');
    }

    function stopPolling() {
        clearTimeout(timer);
        timer = 0;
    }

    // A passive GET is only useful while this panel is the visible tab. Abort it
    // as soon as the owner leaves the panel so a hidden read cannot repaint a
    // later view, and advance the generation before the abort settles.
    function cancelRead() {
        generation += 1;
        controller?.abort();
        controller = null;
        stopPolling();
    }

    function schedule() {
        stopPolling();
        if (!disposed && response?.measuring && onScreen()) timer = setTimeout(refresh, POLL_MS);
    }

    function apply(next) {
        response = next;
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
            const next = await apiClient.storage({ signal: abort.signal });
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

    async function measure() {
        if (disposed || pending || response?.measuring) return;
        pending = true;
        paint();
        controller?.abort();
        const mine = ++generation;
        try {
            const next = await apiClient.measureStorage();
            if (disposed) return;
            // A newer read or a hide moved the generation while the POST was out: its answer is
            // stale, and a hidden tab reads nothing until it is shown again (onSubtab/onPage).
            if (mine === generation) apply(next);
            else if (onScreen()) void refresh();
        } catch (error) {
            if (disposed) return;
            showToast(error?.message || fmt('Storage could not be measured.'), 'warn');
            if (mine === generation && onScreen()) void refresh();
        } finally {
            pending = false;
            if (!disposed) paint();
        }
    }

    const onClick = () => {
        if (button.getAttribute('aria-disabled') !== 'true') void measure();
    };
    const onSubtab = (event) => {
        if (event?.detail?.tab === 'storage' && onScreen()) void refresh();
        else cancelRead();
    };
    const onPage = () => {
        if (onScreen()) void refresh();
        else cancelRead();
    };
    button.addEventListener('click', onClick);
    window.addEventListener('ouro:dashboard-subtab-shown', onSubtab);
    window.addEventListener('ouro:page-shown', onPage);
    document.addEventListener('visibilitychange', onPage);

    function destroy() {
        if (disposed) return;
        disposed = true;
        cancelRead();
        button.removeEventListener('click', onClick);
        window.removeEventListener('ouro:dashboard-subtab-shown', onSubtab);
        window.removeEventListener('ouro:page-shown', onPage);
        document.removeEventListener('visibilitychange', onPage);
    }

    paint();
    if (onScreen()) void refresh();
    return { refresh, measure, destroy };
}
