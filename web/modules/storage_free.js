// Dashboard -> Storage -> Temporary space (layout B1, owner decision Q3 = A): the tray that closes the
// "Where it is" card. "Show what can go" asks the server for a pure preview of the finished work the
// app's own retirers would remove now; the owner checks groups, reads what is lost and what stays, and
// one danger capsule, last on the page, frees exactly the previewed items through the shared confirm
// dialog. Progress and the result stay in place. Pure rendering over the server's `free` envelope
// (`ouroboros.storage_free.StorageFree`); the controller in storage.js owns the requests.

import { fmt } from './i18n.js';
import { escapeHtmlAttr, escapeHtmlText } from './utils.js';
import { DOTS, formatBytes, isSize, number, plural } from './storage_format.js';

const text = escapeHtmlText;
const attr = escapeHtmlAttr;

/** The two groups the server offers, with their words: what is lost, what stays, the unit. */
export const FREE_GROUPS = {
    work_folders: {
        label: 'Finished tasks’ work folders',
        loss: 'Scratch files and helper logs of finished tasks. Results, files, messages and the exact model history are already saved.',
        unit: ['{n} folder', '{count} folders'],
    },
    working_copies: {
        label: 'Working copies of finished work',
        loss: 'Ready-to-use copies of code. Their results and commits are kept.',
        unit: ['{n} copy', '{count} copies'],
    },
};

/** The server's typed kept reasons in the owner's words. */
export const KEPT_REASONS = {
    results_pending: 'their results are not copied yet',
    in_use: 'still in use',
    busy: 'busy right now',
    unreadable_record: 'their task record could not be read',
    restart: 'stopped by a restart',
    other: 'for another reason',
};

const NOT_STARTED = {
    preview_outdated: 'The list changed; check again.',
    busy: 'Something else is still running.',
    unknown_group: 'The list changed; check again.',
    nothing_selected: 'Nothing was selected.',
};

export const groupsOf = (free) => (Array.isArray(free?.groups) ? free.groups : []);
const words = (group) => FREE_GROUPS[group?.id] || { label: String(group?.id || ''), loss: '', unit: ['{n} folder', '{count} folders'] };
const groupBytes = (group) => (isSize(group?.bytes) ? group.bytes : 0);
const chosen = (free, checked = null) => groupsOf(free).filter((group) => !checked || checked.has(group.id));
/** The sum of the sizes known for the (checked) groups: unknown is never zero, so see `sumComplete`. */
export const freeSum = (free, checked = null) => chosen(free, checked).reduce((total, group) => total + groupBytes(group), 0);
/** Every (checked) group with folders was sized whole: the sum is the whole truth. */
export const sumComplete = (free, checked = null) => chosen(free, checked)
    .every((group) => !group.count || (isSize(group.bytes) && group.bytes_complete !== false));
export const unitOf = (group, n) => plural(n, ...words(group).unit);

/** 'rest' | 'listing' | 'ready' | 'empty' | 'running' | 'done' | 'failed' (a dismissed list reads 'rest'). */
export function trayState(free, dismissed = false) {
    const state = free?.state || 'idle';
    if (state === 'ready') return dismissed ? 'rest' : groupsOf(free).some((group) => group.count > 0) ? 'ready' : 'empty';
    if (state === 'stopped') return free.run ? 'done' : 'rest';
    if (state === 'failed' && free.run) return 'done';
    return ['listing', 'running', 'done', 'failed'].includes(state) ? state : 'rest';
}

function groupWhy(group, days) {
    const said = words(group);
    return [
        said.loss ? fmt(said.loss) : '',
        group.inside_wait ? fmt('{n} of {total} folders finished less than {days} days ago.', { n: number(group.inside_wait), total: number(group.count), days }) : '',
        group.in_use ? plural(group.in_use, '{n} is in use and stays.', '{count} are in use and stay.') : '',
        group.unreadable ? plural(group.unreadable, '{n} stays: its task record could not be read.', '{count} stay: their task record could not be read.') : '',
    ].filter(Boolean).join(' ');
}

/** A group's size while listing ("so far") and once listed: a folder the sizing could not read leaves
 *  the size unknown or incomplete, never zero. */
function groupSize(group, listing) {
    if (!isSize(group.bytes)) return listing ? fmt('listing…') : fmt('size unknown');
    if (group.bytes_complete === false) {
        return listing ? fmt('{size} so far', { size: formatBytes(group.bytes) }) : fmt('{size} (some could not be read)', { size: formatBytes(group.bytes) });
    }
    return formatBytes(group.bytes);
}

function checklistHtml(free, checked, days, listing = false) {
    const groups = groupsOf(free);
    const single = groups.length === 1;  // one offered group needs no checkbox column
    return `<ul class="storage-free-groups${single ? ' is-single' : ''}">${groups.map((group) => `<li class="storage-free-group">
            ${single ? '' : `<input type="checkbox" class="ui-checkbox" id="storage-free-${attr(group.id)}" data-free-group="${attr(group.id)}" data-key="check-${attr(group.id)}"${checked.has(group.id) ? ' checked' : ''}>`}
            <label class="storage-free-label" for="storage-free-${attr(group.id)}">${text(fmt(words(group).label))}</label>
            <span class="storage-free-size" data-i18n-fmt="storage.size">${text(groupSize(group, listing))}</span>
            <p class="storage-free-why" data-i18n-fmt="storage.free.why">${text(groupWhy(group, days))}</p>
        </li>`).join('')}</ul>`;
}

function largestHtml(free) {
    const items = groupsOf(free)
        .flatMap((group) => (Array.isArray(group.items) ? group.items : []).map((item) => ({ ...item, group: fmt(words(group).label) })))
        .filter((item) => isSize(item.bytes))
        .sort((a, b) => b.bytes - a.bytes);
    if (!items.length) return '';
    const total = groupsOf(free).reduce((sum, group) => sum + (group.count || 0), 0);
    return `<details class="activity-history storage-largest" data-key="largest">
            <summary data-i18n-fmt="storage.free.largest">${text(fmt('Largest items ({shown} of {total})', { shown: number(items.length), total: number(total) }))}</summary>
            <p class="storage-meta">Freeing removes every checked group, not only these.</p>
            <ul class="storage-largest-list">${items.map((item) => `<li class="storage-largest-item">
                <span class="storage-largest-name" data-i18n-authored>${text(item.name)}</span>
                <span class="storage-largest-size" data-i18n-fmt="storage.size">${text(formatBytes(item.bytes))}</span>
                <span class="storage-largest-group">${text(item.group)}</span>
            </li>`).join('')}</ul>
        </details>`;
}

// `data` is the button's one data attribute (`data-free-go`); it doubles as the focus key. A label
// composed with `fmt` names its key so the language overlay leaves the rendered text alone.
const button = (label, data, tone = 'btn-default', key = '') => `<button type="button" class="btn ${tone} btn-sm" ${data} data-key="${attr(data)}"${key ? ` data-i18n-fmt="${attr(key)}"` : ''}>${text(label)}</button>`;

const deletingSentence = (run) => (run?.deleting ? plural(run.deleting, '{n} is still being deleted.', '{count} are still being deleted.') : '');
const stuckSentence = (run) => (run?.stuck ? plural(run.stuck, '{n} could not be deleted yet; the app retries at its next maintenance run.', '{count} could not be deleted yet; the app retries at its next maintenance run.') : '');

function runningHtml(run, total) {
    const sentence = fmt('Freeing space · {size} removed · {done} of {total} folders · You can leave this tab.', {
        size: formatBytes(run?.removed_bytes || 0), done: number(run?.done), total: number(total),
    });
    return `<p class="storage-progress" data-i18n-fmt="storage.free.running">${DOTS}<span>${text([sentence, deletingSentence(run)].filter(Boolean).join(' '))}</span></p>
        <div class="storage-actions">${button('Stop', 'data-free-stop')}</div>`;
}

function listingHtml(free, checked, days) {
    const sentence = fmt('Checking what can go · {done} of {total} folders · {size} so far. Nothing is removed.', {
        done: number(free.done), total: number(free.total), size: formatBytes(freeSum(free)),
    });
    return `${groupsOf(free).length ? checklistHtml(free, checked, days, true) : ''}
        <p class="storage-progress" data-i18n-fmt="storage.free.listing">${DOTS}<span>${text(sentence)}</span></p>
        <div class="storage-actions">${button('Stop', 'data-free-stop')}</div>`;
}

/** The result in the owner's words: what was removed, what was kept and why, the disk before and after. */
export function doneSentences(free) {
    const run = free?.run;
    if (!run) return [];
    const removed = run.removed_bytes || 0;
    const lead = run.stopped
        ? fmt('Removed {size}, then stopped. The rest was left as it was.', { size: formatBytes(removed) })
        : fmt('Removed {size} ({n}).', { size: formatBytes(removed), n: plural(run.removed || 0, '{n} folder', '{count} folders') });
    const kept = (run.kept || []).filter((row) => row.count > 0).map((row) => {
        const reason = fmt(KEPT_REASONS[row.reason] || KEPT_REASONS.other);
        return plural(row.count, '{n} was kept: {reason}.', '{count} were kept: {reason}.').replace('{reason}', reason);
    });
    const deleting = [deletingSentence(run), stuckSentence(run)].filter(Boolean);
    const disk = isSize(run.free_before) && isSize(run.free_after)
        ? [fmt('{before} free is now {after}.', { before: formatBytes(run.free_before), after: formatBytes(run.free_after) })] : [];
    const error = run.error ? [fmt('Freeing stopped: {reason}.', { reason: String(run.error).replace(/\.$/, '') })] : [];
    return [lead, ...kept, ...deleting, ...error, ...disk, fmt('The next count will show the new total.')];
}

function headHtml(sum = '') {
    return `<div class="storage-tray-head"><h3 class="storage-card-title">Temporary space</h3>${sum
        ? `<span class="storage-tray-sum" data-i18n-fmt="storage.free.sum">${text(sum)}</span>` : ''}</div>`;
}

/** The danger capsule's label: the sum when every checked folder was sized, else the honest rest. */
export function goLabel(free, checked) {
    const sum = freeSum(free, checked);
    if (sumComplete(free, checked)) return fmt('Free {size}', { size: formatBytes(sum) });
    return sum > 0 ? fmt('Free {size} or more', { size: formatBytes(sum) }) : fmt('Free, size unknown');
}

/** The tray's headline once listed: the sum, qualified when a folder could not be read whole. */
function sumLabel(free) {
    const sum = freeSum(free);
    if (sumComplete(free)) return fmt('{size} can be freed', { size: formatBytes(sum) });
    return sum > 0 ? fmt('{size} or more can be freed', { size: formatBytes(sum) }) : fmt('Size unknown');
}

/** The closing controls: Cancel, then the danger capsule carrying the live sum, last on the page. */
function actionsHtml(free, checked) {
    const selected = chosen(free, checked).some((group) => group.count > 0);
    const go = selected ? button(goLabel(free, checked), 'data-free-go', 'btn-danger', 'storage.free.go')
        : '<span class="storage-meta">Nothing selected</span>';
    return `<div class="storage-actions">${button('Cancel', 'data-free-cancel')}${go}</div>`;
}

/** The whole tray for one `free` envelope; `ui` is the checklist state ({checked: Set, dismissed}). */
export function trayHtml(free, ui, days = 7) {
    const state = trayState(free, ui?.dismissed);
    const checked = ui?.checked || new Set();
    if (state === 'listing') return `${headHtml()}${listingHtml(free, checked, days)}`;
    if (state === 'ready') {
        return `${headHtml(sumLabel(free))}
            <p class="storage-meta">Nothing here holds results, messages or model history.</p>
            ${checklistHtml(free, checked, days)}
            ${largestHtml(free)}
            <p class="storage-meta">Gone for good. Sizes are what the listed folders occupy; the space freed can be smaller.</p>
            ${actionsHtml(free, checked)}`;
    }
    if (state === 'empty') {
        return `${headHtml()}<p class="storage-tray-text">Nothing temporary is waiting.</p>
            <p class="storage-meta">Most of Ouroboros is kept on purpose.</p>
            <div class="storage-actions">${button('Show what can go', 'data-free-preview')}</div>`;
    }
    if (state === 'running') return `${headHtml()}${runningHtml(free.run, free.total)}`;
    if (state === 'done') {
        const [lead, ...rest] = doneSentences(free);
        return `${headHtml()}<p class="storage-tray-text" data-i18n-fmt="storage.free.done">${text(lead)}</p>
            <p class="storage-meta" data-i18n-fmt="storage.free.done.rest">${text(rest.join(' '))}</p>
            <div class="storage-actions">${button('Show what can go', 'data-free-preview')}</div>`;
    }
    if (state === 'failed') {
        const reason = String(free.error || '').replace(/\.$/, '');  // the server's reason is a sentence
        return `${headHtml()}<p class="ui-status storage-status" data-tone="warn" data-i18n-fmt="storage.free.failed">${text(fmt('Temporary space could not be listed: {reason}. Nothing was removed.', { reason }))}</p>
            <div class="storage-actions">${button('Try again', 'data-free-preview')}</div>`;
    }
    return `${headHtml()}<p class="storage-meta">Copies the app already removes on its own. Most of Ouroboros is kept on purpose.</p>
        <div class="storage-actions">${button('Show what can go', 'data-free-preview')}</div>`;
}

/** The sentence for a run the server refused (`started: false`). */
export const notStarted = (reason) => fmt('Nothing was removed. {reason}', { reason: fmt(NOT_STARTED[reason] || 'Try again.') });

/** What the confirm dialog shows for the checked groups: the body and one detail row per group. */
export function confirmCopy(free, checked) {
    const groups = groupsOf(free).filter((group) => checked.has(group.id));
    const size = formatBytes(freeSum(free, checked));
    const removes = sumComplete(free, checked) ? fmt('This removes {size} for good.', { size })
        : fmt('This removes the checked folders for good; some of their sizes could not be read.');
    return {
        title: fmt('Free temporary space?'),
        body: `${removes} ${fmt('Results, messages and the exact model history are kept. Continuing one of these tasks later starts without its scratch files.')}`,
        details: {
            summary: fmt('Show details'),
            rows: groups.map((group) => ({ label: fmt(words(group).label), value: `${groupSize(group)} · ${unitOf(group, group.count)}` })),
        },
        confirmLabel: goLabel(free, checked),
        danger: true,
    };
}
