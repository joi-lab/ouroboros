/**
 * The room's work status in its bar (DESIGN "Room work status"). The bar's one status control, right after
 * the room's title, paints the word, tone and dots the chat's producer gives it and opens this room's current
 * work as one Shade under the bar, in Main and a Project alike. Presentation only, over the chat instance's
 * own owners; it keeps no task state, phase vocabulary, poller, pager, question index or permission:
 *   membership  this room's census (`activeDirectActivities`), the owner's unconfirmed sends
 *               (`pendingSubmissions`) and unfinished foreground cards the census does not list
 *               (`liveCardRecords`); a Project with nothing else keeps its latest represented root card
 *   words       task_phase_chip.js: a loaded card's own chip, else the census presenter
 *   controls    task_control_menu.openTaskControls for the row's exact activity id, offered only while
 *               the current census confirms it; a loaded card stops through its own card path
 *   question    the instance's revealQuestion (chat_decision.js); it navigates and never answers
 *   reading     the instance's reading intent and timeline anchors (`Back to reading`); an unloaded row reads
 *               the page around its task through the instance's pager (`around`), Back its own page (`revisit`)
 */
import { fetchTaskDetail, fetchTaskDetailStrict } from './api_client.js';
import { bindContentButton, isForegroundLiveCard } from './chat_activity.js';
import { compareHistoryPosition } from './chat_history_replay.js';
import { createHistoryReveal } from './chat_history_reveal.js';
import { fmt, tr } from './i18n.js';
import { taskCancelPending } from './log_events.js';
import {
    activityWaitPhase, censusTaskFacts, censusTaskPhase, desiredLiveCardPhase, replayTerminalPhase,
} from './task_phase_chip.js';
import {
    REUSABLE_TASK_IDS, closeTaskControlMenu, createTaskResumeButton, openTaskControls, requestStop, resumeRunEligibility,
    taskControlActions, taskControlBusy,
} from './task_control_menu.js';
import { showToast } from './toast.js';

const NAME_CHARS = 160;
const REPLY_CHARS = 60;
// A known outcome keeps its status colour; an unfinished lifecycle (Finalizing…) is said beside it.
const OUTCOME_TONES = { done: 'done', warn: 'caution', cancelled: 'caution', error: 'failed', timeout: 'failed', lifecycle_error: 'failed' };
const GROUP_RANK = { needs: 0, progress: 1, waiting: 2, unconfirmed: 3, latest: 4 };
const NO_CONTROLS = Object.freeze({ menu: false, resume: false });
const clean = (value) => String(value ?? '').replace(/\s+/g, ' ').trim();
const clip = (text, max) => (text.length > max ? `${text.slice(0, max - 1).trimEnd()}…` : text);
let sequence = 0;

// An old parked turn must not read as today's: another day names its date.
function clock(seconds) {
    const date = new Date(Number(seconds) * 1000);
    if (!Number(seconds) || !Number.isFinite(date.getTime())) return '';
    return new Date().toDateString() === date.toDateString()
        ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** The latest represented root card: the newest unfinished one, else the newest of all. */
export function latestRepresentedRoot(records = []) {
    const roots = records.filter((record) => !record.isSubagent && record.root?.isConnected)
        .sort((a, b) => Number(a.root.dataset?.ts || 0) - Number(b.root.dataset?.ts || 0)
            || compareHistoryPosition(a.historyPosition, b.historyPosition));
    return roots.filter((record) => !record.finished).at(-1) || roots.at(-1) || null;
}

/** A row's one colour, from the presenter's own fields and the row's facts, never from its words. */
export function roomWorkTone(view = {}, { confirmed = true, settled = false } = {}) {
    const outcome = OUTCOME_TONES[view.phase];
    if (outcome) return outcome;
    if (settled) return 'ended';
    if (!confirmed || view.phase === 'unknown') return 'unknown';
    if (view.waiting || view.phase === 'waiting' || view.phase === 'paused') return 'wait';
    if (view.phase === 'queued') return 'queued';
    return view.motion ? 'live' : 'quiet';
}

/** The question a confirmed census row waits on, by its exact quiz id; none without positive wait evidence. */
export function waitedQuestion(activity) {
    if (!activity || activity._activityUnconfirmed || activityWaitPhase(activity) !== 'owner_wait') return null;
    const quizId = String(activity.owner_wait?.quiz_id || activity.required_question?.quiz_id || '');
    return quizId ? { quizId } : null;
}

/** What a row may offer: only what the current census confirms for that exact id. A loaded card answers
 *  with its own Stop predicate; stale, offline or reusable rows offer nothing. */
export function rowControls({ id = '', record = null, activity = null, confirmed = false, stopEligible = () => false } = {}) {
    const key = String(id || '').trim();
    if (!confirmed || !activity || !key) return NO_CONTROLS;
    const resume = Boolean(resumeRunEligibility(record || { groupId: key }, activity));
    const stop = record ? Boolean(stopEligible(record)) : !REUSABLE_TASK_IDS.has(key);
    return { menu: stop || resume, resume };
}

/**
 * Mount the status control and its list for one chat instance. The caller passes its own owners by their
 * names; `update()` repaints on the next frame after any of them changed.
 */
export function createRoomWork({
    page, asPanel = false, ws = null, statusBadge, messagesDiv, reading, stateSnapshots = null, revealQuestion = null,
    activeDirectActivities: activities = new Map(), liveCardRecords: records = new Map(),
    pendingSubmissions: pending = new Map(), pendingSuggestedNames: names = new Map(), modelWaits = null,
    stopEligible = () => false, cancelRunFromCard = null, refreshHeaderControlState = async () => {},
    captureVisibleTimelineAnchor = () => null, restoreVisibleTimelineAnchor = () => false, hostReady = () => true,
    historyPager = null, showLatest = null,
} = {}) {
    if (!page || !statusBadge || !messagesDiv) return null;
    const doc = page.ownerDocument;
    const uid = `room-work-${(sequence += 1)}`;
    const scope = asPanel ? 'project' : 'main';
    const frame = globalThis.requestAnimationFrame || ((fn) => setTimeout(fn, 16));
    // The one freshness predicate: the socket today; a shared connection epoch replaces it here.
    const connected = () => ws?.isConnected?.() !== false;
    const refresh = () => refreshHeaderControlState(true);
    const element = (tag, className = '', text = '') => {
        const node = doc.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    };
    const decorative = (node) => { node.setAttribute('aria-hidden', 'true'); return node; };
    const marker = (className) => {
        const node = decorative(element('span', `nav-activity-marker chat-live-typing ${className}`));
        for (let i = 0; i < 3; i += 1) node.appendChild(element('span'));
        return node;
    };

    // ---- the status control: the room's real status badge inside one disclosure button
    const trigger = element('button', 'rw-trigger ui-chrome-control');
    trigger.type = 'button';
    trigger.dataset.scope = scope;
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('aria-controls', `${uid}-surface`);
    // The status's one mark follows its word, as on the sidebar's rows: three dots or one.
    const dots = marker('rw-dots');
    const dot = decorative(element('span', 'rw-mark'));
    // Said as well as drawn: work with a question waiting names the wait in the control's name.
    const waitNote = element('span', 'rw-sr', 'A question waits for your answer');
    const caret = decorative(element('span', 'rw-caret'));
    const badgeParent = statusBadge.parentNode;
    const badgeNext = statusBadge.nextSibling || null;
    const panel = asPanel ? page.closest?.('.project-panel') || null : null;
    // One bar per room: the Project panel bar, or Main's header. The status follows the room's own title in the
    // bar's title row, so a changing word takes the title's room and never moves a neighbour.
    const bar = panel?.querySelector?.('.project-panel-bar') || (asPanel ? null : page.querySelector('.chat-page-header'));
    trigger.append(statusBadge, dot, dots, waitNote, caret);
    const titleRow = bar?.querySelector('.app-page-title-row');
    if (titleRow) titleRow.appendChild(trigger);
    else badgeParent?.insertBefore(trigger, badgeNext);

    // ---- the surface: one Shade in every room, unfolding from the bottom edge of the room's bar
    const surface = element('div', 'rw-surface rw-shade');
    surface.id = `${uid}-surface`;
    surface.hidden = true;
    surface.tabIndex = -1;  // where a pointer open lands: a landing point, never a control
    surface.dataset.scope = scope;
    surface.setAttribute('role', 'dialog');
    // Names are authored text: the chrome in here is translated at the producer, so the overlay never walks it.
    surface.setAttribute('data-i18n-authored', '');
    const list = element('div', 'rw-list');
    const note = element('p', 'rw-note');
    note.hidden = true;
    surface.append(list, note);
    // A zero-height anchor under the bar: the Shade follows the bar's real height at every width. Main's
    // header floats over the transcript, so its anchor is the header's own last child at its bottom edge.
    const shadeAnchor = element('div', 'rw-shade-anchor');
    shadeAnchor.appendChild(surface);
    if (asPanel) (bar || page.querySelector('.chat-panel-statusbar'))?.insertAdjacentElement('afterend', shadeAnchor);
    else (bar || page).appendChild(shadeAnchor);
    // The element that says "open" for CSS: the Project panel, or Main's header.
    const openHost = panel || (asPanel ? null : bar);

    // ---- Back to reading: one explicit return to the passage a navigation left
    const back = element('button', 'rw-back');
    back.type = 'button';
    back.hidden = true;
    const backGlyph = decorative(element('span', 'rw-back-glyph'));
    backGlyph.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/></svg>';
    const backLabel = element('span', 'rw-back-label', 'Back to reading');
    back.append(backGlyph, backLabel);
    const live = element('span', 'rw-sr');
    live.setAttribute('role', 'status');
    (page.querySelector('.chat-input-wrap') || page).appendChild(back);
    page.appendChild(live);

    // ---- state
    let open = false;
    let destroyed = false;
    let rows = [];
    let frozen = null;          // key -> { row, slot } while open: order and slots hold until close
    let order = [];
    let backAnchor = null;
    let backPlace = null;       // where Back returns when the passage was let go: its page, or the present
    let backTimer = 0;
    let focusTimer = 0;
    let outsideClickTimer = 0;
    let outsideClick = null;
    let scheduled = 0;
    let lastInside = false;
    let latestLabel = null;
    let detailSequence = 0;
    let menuIntent = 0;
    let navigationIntent = 0;
    const expanded = new Set();  // keys whose details are open (cleared on close)
    const lookups = new Map();   // id -> what a press found for an unloaded row, and for which census facts (cleared on close)
    const history = historyPager && createHistoryReveal({ pager: historyPager, reading, feed: messagesDiv, records,
        present: () => !destroyed && !page.hidden && page.isConnected });
    const views = new Map();     // key -> painted row
    const detailNames = new Map();
    const detailReads = new Set();
    const disposers = [];
    const listen = (target, type, fn, options) => {
        if (!target?.addEventListener) return;
        target.addEventListener(type, fn, options);
        disposers.push(() => target.removeEventListener?.(type, fn, options));
    };

    // ---- model
    // The owner's own message for a direct turn or an unconfirmed send, by its exact client id.
    function bubbleFor(cmid) {
        if (!cmid) return null;
        for (const bubble of messagesDiv.querySelectorAll('.chat-bubble.user')) {
            if (bubble.dataset?.clientMessageId === cmid) return bubble;
        }
        return null;
    }

    // `text` is the bounded visible name; `full` is the whole one, for the accessible name and the details.
    function nameOf(item, bubble) {
        const named = (full, source) => ({ text: clip(full, NAME_CHARS), full, source });
        const own = clean(item.record?.suggestedName || item.record?.titleEl?.textContent);
        if (own) return named(own, 'card');
        const coined = clean(names.get(item.id));
        if (coined) return named(coined, 'task_named');
        const read = clean(detailNames.get(item.id));
        if (read) return named(read, 'task_detail');
        const said = clean(bubble?.querySelector?.('.message')?.textContent);
        if (said) return { text: fmt('Reply to “{text}”', { text: clip(said, REPLY_CHARS) }),
            full: fmt('Reply to “{text}”', { text: said }), source: 'owner_message' };
        const when = clock(item.startedAt);
        if (item.kind === 'task') return named(when ? fmt('Task started {time}', { time: when }) : tr('room_work.name.task', 'Task'), 'neutral');
        // A turn with no owner message of its own (a consciousness wake-up) is not a reply.
        if (!item.cmid) return named(when ? fmt('Turn started {time}', { time: when }) : tr('room_work.name.turn', 'Turn in this chat'), 'neutral');
        return named(tr('room_work.name.earlier', 'Reply to an earlier message'), 'neutral');
    }

    function collect() {
        const online = connected();
        const items = [];
        for (const [id, activity] of activities) {
            const record = records.get(id) || null;
            if (record?.isSubagent || censusTaskFacts(activity).ended) continue;
            const census = censusTaskPhase(activity, null, online, Boolean(modelWaits?.waiting?.(id)));
            // Words are the loaded card's own chip; motion and waiting stay the census presenter's.
            const view = record && !record.finished ? { ...desiredLiveCardPhase(record),
                motion: census.motion, waiting: census.waiting, secondaryMotion: census.secondaryMotion } : census;
            items.push({ id, kind: String(activity.kind || '') === 'managed_task' ? 'task' : 'direct', activity, record, view,
                confirmed: online && hostReady() && !activity._activityUnconfirmed,
                cmid: String(activity.clientMessageId || activity.client_message_id || ''),
                startedAt: Number(activity.started_at) || 0, attempt: activity.task_attempt ?? null });
        }
        // An unfinished foreground card the census does not list: offered, unconfirmed, with no controls.
        for (const [id, record] of records) {
            if (activities.has(id) || record.isSubagent || !isForegroundLiveCard(record)) continue;
            items.push({ id, kind: record.direct ? 'direct' : 'task', activity: null, record,
                view: desiredLiveCardPhase(record), confirmed: false, cmid: '', startedAt: 0, attempt: null });
        }
        for (const [cmid, sent] of pending) {
            items.push({ id: `pending:${cmid}`, kind: 'pending', activity: null, record: null, confirmed: true, cmid,
                startedAt: Math.floor((Number(sent?.timestamp) || 0) / 1000), attempt: null,
                view: { phase: 'working', text: tr('room_work.sending', 'Sending…'), motion: true, waiting: false } });
        }
        // A Project with nothing else keeps its way to the latest represented root card.
        if (asPanel && !items.length) {
            const latest = latestRepresentedRoot([...records.values()]);
            if (latest) items.push({ id: latest.groupId, kind: 'task', activity: null, record: latest, latest: true,
                view: desiredLiveCardPhase(latest, replayTerminalPhase(latest)), confirmed: false, cmid: '',
                startedAt: 0, attempt: null });
        }
        return items.map(decorate);
    }

    function decorate(item) {
        const { view, record } = item;
        const card = record?.root?.isConnected ? record.root : null;
        const bubble = item.cmid && !card ? bubbleFor(item.cmid) : null;
        const name = nameOf(item, bubble);
        const finished = Boolean(item.latest && record?.finished);
        const group = item.latest ? 'latest' : !item.confirmed || view.phase === 'unknown' ? 'unconfirmed'
            : view.phase === 'waiting' ? 'needs' : view.motion && !view.waiting ? 'progress' : 'waiting';
        const sub = view.phase === 'queued' ? 1 : view.phase === 'paused' ? 3 : view.waiting ? 2 : 0;
        const controls = item.kind === 'pending' || item.latest ? NO_CONTROLS
            : rowControls({ id: item.id, record, activity: item.activity, confirmed: item.confirmed, stopEligible });
        return {
            key: item.id, id: item.id, kind: item.kind, name: name.text, fullName: name.full, nameSource: name.source,
            view, group, slot: group, rank: GROUP_RANK[group] * 10 + sub, confirmed: item.confirmed,
            latest: Boolean(item.latest), settled: finished, outcomeKnown: finished, attempt: item.attempt,
            card: Boolean(card), target: card || bubble, loaded: Boolean(card || bubble),
            startedAt: item.startedAt, cmid: item.cmid, canMenu: controls.menu, canResume: controls.resume,
            question: item.confirmed ? waitedQuestion(item.activity) : null,
            tone: roomWorkTone(view, { confirmed: item.confirmed, settled: finished }),
        };
    }

    // A press's answer (loading|absent|moved|unknown) holds while the work's census facts stay as they were:
    // work that changed may have a message now, so its row claims nothing again until a press asks.
    const lookupFacts = (row) => `${row.view?.phase}|${row.attempt ?? ''}|${row.cmid || ''}`;
    function lookupOf(row) {
        const found = lookups.get(row.id);
        if (found && found.state !== 'loading' && found.facts !== lookupFacts(row)) lookups.delete(row.id);
        return lookups.get(row.id)?.state || '';
    }

    // Work that left the census while the list is open: its row stays, settled, until close. Finish withdraws
    // the controls, never the way to its name, card or details; the word is its card's own final one, if known.
    function settled(row) {
        const record = records.get(row.id);
        const outcome = record?.finished ? desiredLiveCardPhase(record, replayTerminalPhase(record)) : null;
        const card = record?.root?.isConnected ? record.root : null;
        const target = card || (row.cmid ? bubbleFor(row.cmid) : null);
        const view = outcome || { phase: 'ended', text: tr('room_work.ended', 'No longer listed as active') };
        const name = nameOf({ ...row, record }, target);
        const refreshedName = name.source === 'neutral' ? {} : {
            name: name.text, fullName: name.full, nameSource: name.source,
        };
        return { ...row, ...refreshedName, view, group: 'ended', settled: true, outcomeKnown: Boolean(outcome), canMenu: false,
            canResume: false, question: null, card: Boolean(card), target, loaded: Boolean(target),
            tone: roomWorkTone(view, { settled: true }) };
    }

    const sorter = (a, b) => (a.rank - b.rank) || (b.startedAt - a.startedAt) || a.key.localeCompare(b.key);

    // While open, order and each row's slot hold; words, verbs and motion update in place, so no existing
    // node moves under the keyboard or the pointer. New rows append.
    function arrange(collected) {
        if (!open || !frozen) return [...collected].sort(sorter);
        const byKey = new Map(collected.map((row) => [row.key, row]));
        const out = [];
        for (const key of [...order]) {
            const kept = frozen.get(key);
            let fresh = byKey.get(key);
            if (!fresh && kept.row.kind === 'pending' && kept.row.cmid) {
                // The owner's send that the host turned into a turn takes the same place.
                // A progress card may already own the turn's slot before its census arrives.
                // In that case keep that view; the old send settles normally until close.
                fresh = [...byKey.values()].find((row) => row.kind !== 'pending'
                    && row.cmid === kept.row.cmid && !order.includes(row.key));
                if (fresh) {
                    order[order.indexOf(key)] = fresh.key;
                    frozen.delete(key);
                    // Identity changes, not the reader's node: preserve selection and focus in this slot.
                    const view = views.get(key);
                    if (view) { views.delete(key); views.set(fresh.key, view); }
                }
            }
            if (fresh) {
                byKey.delete(fresh.key);
                out.push({ ...fresh, slot: kept.slot });
            } else {
                out.push(settled(kept.row));
            }
        }
        for (const row of [...byKey.values()].sort(sorter)) {
            order.push(row.key);
            out.push(row);
        }
        for (const row of out) if (!row.settled || !frozen.has(row.key)) frozen.set(row.key, { row, slot: row.slot });
        return out;
    }

    // What a press on an unloaded row found out about where its message is (DESIGN "Room work status").
    function whereText(row) {
        const ended = row.settled || ['done', 'caution', 'failed'].includes(row.tone);
        return { loading: tr('room_work.lookup.loading', 'Loading its message…'),
            absent: ended ? tr('room_work.lookup.absent_ended', 'No message saved in this chat.') : tr('room_work.lookup.absent', 'No message yet.'),
            moved: tr('room_work.lookup.moved', 'This work is in another room.'),
            unknown: tr('room_work.lookup.unknown', 'Its message could not be checked.') }[row.lookup] || '';
    }

    function describe(row) {
        const lines = [];
        if (row.settled && !row.latest) lines.push(row.outcomeKnown
            ? [tr('room_work.detail.finished', 'Finished'),
                tr('room_work.detail.finished_text', 'Its recorded outcome is shown above. This row stays until the list closes.')]
            : [tr('room_work.detail.activity', 'Activity'),
                tr('room_work.detail.ended_text', 'No longer listed as active; its outcome is not known here. This row stays until the list closes.')]);
        const where = whereText(row);
        if (where) lines.push([tr('room_work.detail.where', 'Where'), where]);
        if (!row.confirmed && !row.settled && !row.latest) lines.push([tr('room_work.detail.why', 'Why no controls'),
            tr('room_work.detail.why_text', 'The latest update did not confirm this work, so nothing here can be paused or stopped.')]);
        const what = row.kind === 'pending' ? tr('room_work.what.pending', 'Your message, being sent')
            : row.kind === 'direct' ? (row.cmid ? tr('room_work.what.reply', 'A reply to your message')
                : tr('room_work.what.turn', 'A turn in this chat'))
                : tr('room_work.what.task', 'A task');
        lines.push([tr('room_work.detail.what', 'What'), what]);
        return lines;
    }

    // ---- rows
    function viewFor(row) {
        let view = views.get(row.key);
        if (view) return view;
        const el = element('div', 'rw-row');
        el.dataset.id = row.id;
        // main: the row's one primary target, the way to its card or (no card loaded) its details
        const main = element('div', 'rw-main');
        const nameEl = element('span', 'rw-name');
        const state = element('span', 'rw-state');
        const rowDots = marker('rw-row-dots');
        const rowLead = decorative(element('span', 'rw-lead'));
        const word = element('span', 'rw-word');
        const secondary = element('span', 'rw-secondary');
        const where = element('span', 'rw-where');
        const chev = decorative(element('span', 'rw-chev'));
        where.append(chev);
        state.append(rowDots, rowLead, word, secondary, where);
        main.append(nameEl, state);
        const verb = element('span', 'rw-verb');
        const moreSlot = element('span', 'rw-more-slot');
        const detail = element('div', 'rw-detail');
        detail.id = `${uid}-detail-${(detailSequence += 1)}`;
        detail.hidden = true;
        el.append(main, verb, moreSlot, detail);
        view = { el, main, nameEl, dots: rowDots, lead: rowLead, word, secondary, where, chev, verb, moreSlot,
            detail, question: null, resume: null, more: null, row: null, full: null, detailKey: '', attempt: undefined };
        // The name stays selectable text: a drag that selected it does not navigate.
        bindContentButton(main, () => activateRow(view.row), () => rowIsTarget(view.row));
        // Keyboard focus shows the whole name (CSS unclamps it). Not :focus-within: a press on Resume or ⋯
        // must not grow the row under the pointer before mouse-up.
        main.addEventListener('focus', () => {
            let keyboard = false;
            try { keyboard = main.matches?.(':focus-visible') === true; } catch { /* an engine without the selector */ }
            el.dataset.reading = keyboard ? '1' : '0';
        });
        main.addEventListener('blur', () => { el.dataset.reading = '0'; });
        views.set(row.key, view);
        return view;
    }

    const rowIsTarget = (row) => Boolean(row) && (Boolean(row.target?.isConnected) || row.kind !== 'pending');

    function activateRow(row) {
        if (!row) return;
        if (row.target?.isConnected) navigate(row);
        else if (row.kind === 'pending') return;
        else if (history && !['absent', 'moved', 'loading'].includes(row.lookup)) void reveal(row);
        else toggleDetail(row.key);
    }

    function toggleDetail(key) {
        if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
        const row = rows.find((candidate) => candidate.key === key);
        if (row) paintRow(row);
    }

    function paintRow(row) {
        const view = viewFor(row);
        view.row = row;
        const { el, main } = view;
        const navigable = Boolean(row.target?.isConnected);
        // An unloaded row asks the room's history on its press: until then it claims nothing about where its message is.
        const lookable = !navigable && row.kind !== 'pending' && Boolean(history) && !['absent', 'moved'].includes(row.lookup);
        // Opened details survive a census change; a press then asks again where its message is.
        const detailable = !navigable && row.kind !== 'pending' && (!history || Boolean(row.lookup) || expanded.has(row.key));
        const isOpen = detailable && expanded.has(row.key);
        Object.assign(el.dataset, {
            id: row.id,
            slot: row.slot, kind: row.kind, tone: row.tone, settled: row.settled ? '1' : '0', open: isOpen ? '1' : '0',
        });
        const full = row.fullName || row.name;
        // Every row says its state under its name (5A); where an unloaded row's message is, its press finds out.
        const state = [row.view.text, row.view.secondary].filter(Boolean).join(' · ');
        if (navigable || lookable || detailable) {
            main.setAttribute('role', 'button');
            main.tabIndex = 0;
            main.setAttribute('aria-label', navigable
                ? fmt(row.card ? 'Go to task: {name}, {state}' : 'Go to message: {name}, {state}', { name: full, state })
                : lookable ? fmt('Open: {name}, {state}', { name: full, state })
                    : fmt('Details: {name}, {state}', { name: full, state }));
            if (row.lookup === 'loading') main.setAttribute('aria-busy', 'true');
            else main.removeAttribute('aria-busy');
            if (detailable) {
                main.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
                main.setAttribute('aria-controls', view.detail.id);
            } else {
                main.removeAttribute('aria-expanded');
                main.removeAttribute('aria-controls');
            }
        } else {
            for (const name of ['role', 'tabindex', 'aria-label', 'aria-expanded', 'aria-controls', 'aria-busy']) main.removeAttribute(name);
        }
        main.dataset.target = navigable ? 'card' : lookable ? 'history' : detailable ? 'details' : 'none';
        // The whole name is in the DOM; CSS clamps it to two lines, focus and open details show it whole.
        if (view.full !== full) { view.full = full; view.nameEl.textContent = full; }
        // Three dots are work (moving only for confirmed motion, still when queued); one dot is a single fact.
        const dotted = row.tone === 'live' || row.tone === 'queued';
        view.dots.hidden = !dotted;
        view.dots.dataset.state = row.tone === 'live' ? 'working' : 'queued';
        view.lead.hidden = dotted || row.tone === 'quiet';
        if (view.word.textContent !== row.view.text) view.word.textContent = row.view.text;
        const secondary = String(row.view.secondary || '');
        if (view.secondary.textContent !== secondary) view.secondary.textContent = secondary;
        view.secondary.hidden = !secondary;
        view.chev.hidden = !detailable;
        view.where.hidden = !detailable;
        view.detail.hidden = !isOpen;
        if (isOpen) {
            const lines = describe(row);
            const key = lines.map((line) => line.join('\u0000')).join('\u0001');
            if (view.detailKey !== key) {
                view.detailKey = key;
                view.detail.textContent = '';
                for (const [label, text] of lines) {
                    const line = element('p', 'rw-detail-line');
                    line.append(element('span', 'rw-detail-label', label), element('span', '', text));
                    view.detail.appendChild(line);
                }
            }
        }
        // A new episode of the same work never inherits a menu opened for the previous one.
        if (view.attempt !== undefined && view.attempt !== row.attempt
            && view.more?.getAttribute('aria-expanded') === 'true') closeTaskControlMenu();
        view.attempt = row.attempt;
        reconcileActions(view, row);
    }

    function reconcileActions(view, row) {
        const keepFocus = (node, fallback) => {
            if (node && doc.activeElement === node) (fallback || view.more || view.main).focus?.({ preventScroll: true });
        };
        const full = row.fullName || row.name;
        const wantQuestion = Boolean(row.question) && !row.settled;
        if (wantQuestion && !view.question) {
            const button = element('button', 'rw-action rw-pill rw-pill-question', tr('room_work.view_question', 'View question'));
            button.type = 'button';
            button.addEventListener('click', (event) => {
                event?.stopPropagation?.();
                if (view.row?.question) void viewQuestion(view.row);
            });
            view.question = button;
            view.verb.prepend(button);
        } else if (!wantQuestion && view.question) {
            keepFocus(view.question);
            view.question.remove();
            view.question = null;
        }
        // It goes to the question; it never sends, selects or fills in an answer.
        view.question?.setAttribute('aria-label', fmt('View question: {name}', { name: full }));
        if (view.question) {
            const label = tr('room_work.view_question', 'View question');
            if (view.question.textContent !== label) view.question.textContent = label;
        }
        const wantResume = row.canResume && !row.settled;
        if (wantResume && !view.resume) {
            // The shared button re-checks eligibility at press time; its id never changes.
            view.resume = createTaskResumeButton(row.id, {
                canResume: () => currentControls(row.id, view.row?.attempt).resume, onSettled: refresh,
            });
            view.resume.classList.add('rw-action', 'rw-pill', 'rw-pill-resume');
            view.verb.append(view.resume);
        } else if (!wantResume && view.resume) {
            keepFocus(view.resume);
            view.resume.remove();
            view.resume = null;
        }
        if (view.resume) {
            const label = tr('task.control.resume', 'Resume');
            if (view.resume.textContent !== label) view.resume.textContent = label;
        }
        // The ⋯ is the one door to every other action: the shared task controls, aimed at this exact id.
        const wantMore = row.canMenu && !row.settled;
        const actionKey = wantMore ? taskControlActions({
            cancelPending: Boolean(records.get(row.id)?.cancelPendingPolicy),
            budgetPaused: activities.get(row.id)?.phase === 'budget_paused',
            resumeVisible: row.canResume,
        }).join(',') : '';
        // A row may keep its menu door while the actions behind it change (Working → Paused).
        // Withdraw the old portal immediately, not just its ability to dispatch stale actions.
        if (view.actionKey !== actionKey && view.more?.getAttribute('aria-expanded') === 'true') {
            menuIntent += 1;
            closeTaskControlMenu();
        }
        view.actionKey = actionKey;
        if (wantMore && !view.more) {
            const button = element('button', 'rw-more');
            button.type = 'button';
            button.dataset.id = row.id;  // Pause reads its exact target here, never the nearest card
            button.innerHTML = '<svg class="rw-more-glyph" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true" focusable="false"><circle cx="3" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="13" cy="8" r="1.4"/></svg>';
            button.setAttribute('aria-haspopup', 'menu');
            button.setAttribute('aria-expanded', 'false');
            button.addEventListener('click', (event) => {
                event?.stopPropagation?.();
                if (view.row?.canMenu) void openControls(view.row, button);
            });
            view.more = button;
            view.moreSlot.append(button);
        } else if (!wantMore && view.more) {
            if (view.more.getAttribute('aria-expanded') === 'true') closeTaskControlMenu();
            keepFocus(view.more, view.main.tabIndex >= 0 ? view.main : null);
            view.more.remove();
            view.more = null;
        }
        view.more?.setAttribute('aria-label', fmt('Task actions: {name}', { name: full }));
        view.el.dataset.verb = view.question ? 'question' : view.resume ? 'resume' : '';
    }

    // The shared controls for one exact id. A loaded card stops through its own card path, so its chip
    // follows at once; a row without a card first reads the durable record, as Activity does, so a stop
    // already pending is not offered again as a fresh Wrap up.
    function currentControls(id, attempt) {
        const activity = activities.get(id);
        const confirmed = open && !destroyed && !page.hidden && page.isConnected && connected() && hostReady()
            && activity && !activity._activityUnconfirmed && !censusTaskFacts(activity).ended
            && (activity.task_attempt ?? null) === attempt;
        return rowControls({ id, record: records.get(id), activity, confirmed, stopEligible });
    }

    async function openControls(row, anchor) {
        const intent = ++menuIntent;
        if (!currentControls(row.id, row.attempt).menu) return;
        const record = records.get(row.id) || null;
        let cancelPending = Boolean(record?.cancelPendingPolicy);
        let stop = record && !record.finished && cancelRunFromCard ? (action) => cancelRunFromCard(record, action) : null;
        if (!stop) {
            let stored = null;
            try { stored = await fetchTaskDetailStrict(row.id); } catch (exc) {
                if (exc?.status !== 404) {
                    showToast(`Could not refresh task state: ${exc?.message || exc}`, 'error');
                    return;
                }
            }
            if (intent !== menuIntent || !anchor.isConnected || !currentControls(row.id, row.attempt).menu) return;
            cancelPending = taskCancelPending(stored);
            stop = (action) => requestStop(row.id, action).then(() => refresh(), (exc) => {
                if (exc?.status !== 404) showToast(`Cancel failed: ${exc?.message || exc}`, 'error');
                return refresh();
            });
        }
        const canAct = (action) => {
            const current = currentControls(row.id, row.attempt);
            if (intent !== menuIntent || !anchor.isConnected || !current.menu || taskControlBusy(row.id)) return false;
            return taskControlActions({
                cancelPending: cancelPending || Boolean(records.get(row.id)?.cancelPendingPolicy),
                budgetPaused: activities.get(row.id)?.phase === 'budget_paused',
                resumeVisible: current.resume,
            }).includes(action);
        };
        openTaskControls(anchor, row.id, { record, activity: activities.get(row.id), cancelPending, canAct, stop });
    }

    // ---- paint
    function render() {
        if (destroyed) return;
        // Main mounts before the install's language arrives; the authored subtree has no overlay.
        const label = asPanel ? tr('room_work.scope.project', 'Work in this project')
            : tr('room_work.scope.main', 'Work in this chat');
        if (surface.getAttribute('aria-label') !== label) surface.setAttribute('aria-label', label);
        rows = arrange(collect()).map((row) => Object.assign(row, { lookup: lookupOf(row) }));
        paintTrigger();
        paintNote();
        if (!open) return;
        const keys = new Set(rows.map((row) => row.key));
        for (const [key, view] of views) {
            if (!keys.has(key)) { view.el.remove(); views.delete(key); expanded.delete(key); }
        }
        const children = [];
        rows.forEach((row, index) => {
            prefetchName(row);
            paintRow(row);
            const { el } = views.get(row.key);
            el.style.setProperty('--rw-i', String(Math.min(index, 6)));
            // Only the Project's latest root is labelled, so it never reads as current work.
            if (row.slot === 'latest' && !children.includes(latestLabel)) {
                latestLabel ||= element('div', 'rw-group');
                const label = tr('room_work.latest', 'Latest task');
                if (latestLabel.textContent !== label) latestLabel.textContent = label;
                children.push(latestLabel);
            }
            children.push(el);
        });
        if (latestLabel && !children.includes(latestLabel)) latestLabel.remove();
        // Nodes move only where the sequence differs, so a focused row is never detached.
        children.forEach((node, index) => {
            if (list.children[index] !== node) list.insertBefore(node, list.children[index] || null);
        });
    }

    function paintTrigger() {
        const text = clean(statusBadge.textContent);
        const current = rows.filter((row) => !row.settled && !row.latest);
        const listed = rows.length > 0;
        // Word, tone and dots come together from the chat's producer (computeDerivedChatStatus via setStatus);
        // the control paints them and never ranks row tones or reads a class name for a second opinion.
        // Before the producer's first answer the room is not online yet: an unknown tone paints as offline.
        const tone = statusBadge.dataset.tone || 'offline';
        const dotsMode = statusBadge.dataset.dots || 'one';
        trigger.disabled = !listed;
        trigger.dataset.tone = tone;
        trigger.dataset.open = open ? '1' : '0';
        trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
        trigger.title = text;  // the whole status word, never clipped away (an extra, not the only path)
        caret.hidden = !listed;
        // Owner 15.10 (3C): live work keeps three moving dots, queued work three still dots; a fact is one dot.
        dots.hidden = dotsMode === 'one';
        dots.dataset.state = dotsMode === 'moving' ? 'working' : 'queued';
        dot.hidden = !dots.hidden;
        // One mark, one more fact (owner 16.10): the mark turns amber while a question waits for the owner's answer
        // and the word does not already say so — beside Working, Queued, a pause or an access wait alike.
        const mixed = !['offline', 'starting', 'wait'].includes(tone) && current.some((row) => row.question);
        trigger.dataset.wait = mixed ? '1' : '0';
        waitNote.hidden = !mixed;
        if (!listed && open) close({ restoreFocus: false });
    }

    function paintNote() {
        const census = stateSnapshots?.latest?.();
        const message = !connected() ? tr('room_work.note.reconnecting', 'Reconnecting — showing the last known state.')
            : !hostReady() ? tr('room_work.note.starting', 'Ouroboros is starting…')
                : census?.active_chat_activities_complete === false
                    ? tr('room_work.note.partial', 'Not all current work could be confirmed; this list may be incomplete.') : '';
        if (note.textContent !== message) note.textContent = message;
        note.hidden = !message || !open;
    }

    // ---- behaviour
    function openSurface(viaKeyboard = false) {
        if (open || destroyed || trigger.disabled || trigger.hidden) return;
        open = true;
        frozen = new Map();
        order = [];
        surface.hidden = false;
        openHost?.setAttribute('data-rw-open', '');
        render();
        // A pointer open lands on the (ring-less) list; a keyboard open enters its first real control.
        const first = viaKeyboard ? list.querySelector('.rw-main[tabindex="0"], .rw-action, .rw-more') : null;
        (first || surface).focus?.({ preventScroll: true });
    }

    function close({ restoreFocus = true } = {}) {
        menuIntent += 1;
        if (!open) return;
        open = false;
        closeTaskControlMenu();
        surface.hidden = true;
        openHost?.removeAttribute('data-rw-open');
        frozen = null;
        order = [];
        expanded.clear();
        // A list dismissed while a row still looks drops that look: the reader left before it answered.
        if ([...lookups.values()].some((found) => found.state === 'loading')) { navigationIntent += 1; live.textContent = ''; }
        lookups.clear();
        // Reconcile against the closed-list policy before restoring focus. The final retained
        // Main row may just have ended; disabling its trigger on the next frame would strand focus.
        render();
        if (restoreFocus && trigger.isConnected && !trigger.hidden) {
            if (!trigger.disabled) trigger.focus?.({ preventScroll: true });
            else (page.querySelector('#chat-input') || page.querySelector('.chat-input'))?.focus?.({ preventScroll: true });
        }
    }

    function prefetchName(row) {
        if (row.nameSource !== 'neutral' || row.kind !== 'task' || detailReads.has(row.id)) return;
        detailReads.add(row.id);
        fetchTaskDetail(row.id)
            .then((detail) => { if (!destroyed) detailNames.set(row.id, clean(detail?.suggested_name || detail?.title)); })
            .catch(() => {})
            .finally(() => schedule());
    }

    function scrollToNode(node) {
        reading.cancel();
        // Main's header floats over the feed: the target lands just below it, not under it.
        const reserve = asPanel ? 0 : parseFloat(page.style?.getPropertyValue?.('--chat-header-reserve')) || 0;
        messagesDiv.scrollTop += node.getBoundingClientRect().top - messagesDiv.getBoundingClientRect().top - reserve;
        reading.stick = false;
        reading.scroll();
    }

    // Going to a card closes the list: the reader came to read it. Back to reading undoes the trip.
    function navigate(row) {
        const target = row?.target;
        if (!target?.isConnected) return;
        navigationIntent += 1;
        arrive(row, target, departure());
    }

    const departure = () => (history ? history.departure(captureVisibleTimelineAnchor())
        : { anchor: captureVisibleTimelineAnchor() });

    function arrive(row, target, place) {
        close({ restoreFocus: false });
        scrollToNode(target);
        showBack(place.anchor, place);
        const summary = records.get(row.id)?.summaryButtonEl;
        const focus = summary?.isConnected ? summary : target;
        if (focus === target && !target.hasAttribute?.('tabindex')) target.setAttribute('tabindex', '-1');
        focus.focus?.({ preventScroll: true });
    }

    // An unloaded row: the chat reads the page around its task and lands there (DESIGN "Room work status"); the
    // list stays open while it looks, and a history with no message of it here, or a read that cannot tell, says so.
    async function reveal(row) {
        const intent = ++navigationIntent;
        const place = departure();
        lookups.set(row.id, { state: 'loading', intent });
        expanded.add(row.key);
        live.textContent = tr('room_work.lookup.loading', 'Loading its message…');
        schedule();
        const found = await history.task(row.id, () => intent === navigationIntent);
        if (!found) {
            if (intent === navigationIntent) live.textContent = '';
            if (lookups.get(row.id)?.intent === intent) { lookups.delete(row.id); expanded.delete(row.key); schedule(); }
            return;
        }
        lookups.delete(row.id);
        if (found.target) { arrive(row, found.target, place); return; }
        if (found.left) showBack(place.anchor, place);  // its page was drawn yet shows nothing of it: Back undoes that
        // The owner's own message is a message: a row that carries one is never told it has none.
        const state = found.state === 'absent' && row.cmid ? 'unknown' : found.state;
        lookups.set(row.id, { state, facts: lookupFacts(row) });
        live.textContent = whereText({ ...row, lookup: state });
        schedule();
    }

    // Goes to the exact waited question and focuses the question itself, never its answer field.
    async function viewQuestion(row) {
        const intent = ++navigationIntent;
        const anchor = captureVisibleTimelineAnchor();
        close({ restoreFocus: false });
        const shown = await revealQuestion?.(row.id, row.question.quizId, { focus: 'question' });
        if (shown && !destroyed && intent === navigationIntent && !page.hidden && page.isConnected) showBack(anchor);
    }

    function showBack(anchor, place = null) {
        backAnchor = anchor || null;
        backPlace = backAnchor ? place : null;
        clearTimeout(backTimer);
        backLabel.textContent = 'Back to reading';
        back.hidden = !backAnchor;
    }

    function hideBack() {
        // Hand focus to the passage, without moving it. A later dismissal must not steal focus
        // from a reader who already moved elsewhere while the missing-passage notice was shown.
        if (doc.activeElement === back && !destroyed && !page.hidden && page.isConnected) {
            if (!messagesDiv.hasAttribute('tabindex')) messagesDiv.setAttribute('tabindex', '-1');
            messagesDiv.focus?.({ preventScroll: true });
        }
        back.hidden = true;
    }

    async function goBack() {
        if (!backAnchor) return;
        const anchor = backAnchor, place = backPlace;
        const intent = ++navigationIntent;
        clearTimeout(backTimer);
        // The reader was following the present: Back is the return to it.
        if (place?.present && showLatest) { backAnchor = null; hideBack(); await showLatest(); return; }
        reading.cancel();
        const restore = () => (restoreVisibleTimelineAnchor(anchor, { exact: true }) ? 'exact'
            : restoreVisibleTimelineAnchor(anchor, { cardOnly: true }) ? 'near' : '');
        let returned = restore();
        if (!returned && place?.cursor && history) {  // the page the reader left was let go: read it again
            const drawn = await history.revisit(place.cursor, () => intent === navigationIntent);
            if (drawn === null) return;
            if (drawn === false) {  // the read failed for now: Back stays for another try
                live.textContent = 'The earlier reading place could not be read. Try again.';
                return;
            }
            if (drawn === true) returned = restore();
        }
        reading.stick = false;
        reading.scroll();
        backAnchor = null;
        if (!returned) {
            backLabel.textContent = 'Earlier place is no longer loaded';
            live.textContent = 'The earlier reading place is no longer loaded.';
            backTimer = setTimeout(hideBack, 4000);
            return;
        }
        live.textContent = returned === 'exact' ? 'Returned to where you were reading.' : 'Returned to the nearest loaded place.';
        hideBack();
    }

    // ---- wiring
    listen(globalThis.window, 'ouro:language-changed', schedule);
    // A click with detail 0 is Enter or Space on the focused control: a keyboard open lands in the list.
    listen(trigger, 'click', (event) => { if (open) close(); else openSurface(event?.detail === 0); });
    listen(trigger, 'keydown', (event) => {
        if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !open && !trigger.disabled) {
            event.preventDefault();
            openSurface(true);
        }
    });
    listen(back, 'click', goBack);
    listen(doc, 'pointerdown', (event) => {
        const target = event.target;
        // The shared task-controls menu is a portal outside the list but part of its interaction.
        if (target?.closest?.('.task-control-menu')) return;
        lastInside = surface.contains(target) || trigger.contains(target);
        if (!open || lastInside) return;
        close({ restoreFocus: false });
        // The first outside press only dismisses the list: a press on the dimmed Main must not also close the room.
        if (target?.closest?.('#project-panel-backdrop')) {
            if (outsideClick) doc.removeEventListener('click', outsideClick, true);
            clearTimeout(outsideClickTimer);
            outsideClick = (click) => { click.stopPropagation(); click.preventDefault(); };
            doc.addEventListener('click', outsideClick, { capture: true, once: true });
            outsideClickTimer = setTimeout(() => {
                doc.removeEventListener('click', outsideClick, true);
                outsideClick = null;
                outsideClickTimer = 0;
            }, 600);
        }
    }, true);
    listen(doc, 'focusin', (event) => {
        if (!event.target?.closest?.('.task-control-menu')) lastInside = surface.contains(event.target) || trigger.contains(event.target);
    }, true);
    // Some engines (WebKit) do not focus a pressed button, so after a press in the list the document holds
    // focus: the list still owns Escape when the reader's last press or focus was in it.
    listen(doc, 'keydown', (event) => {
        if (event.key !== 'Escape' || !open || event.defaultPrevented) return;
        const active = doc.activeElement;
        const nowhere = !active || active === doc.body;
        if (!surface.contains(active) && active !== trigger && !(nowhere && lastInside)) return;
        event.preventDefault();
        close();
    });
    listen(surface, 'focusout', (event) => {
        if (!open) return;
        const next = event.relatedTarget;
        // Tabbing out of the list (not into its own task-controls menu) dismisses it, like any dropdown.
        if (next && !surface.contains(next) && !trigger.contains(next) && !next.closest?.('.task-control-menu')) {
            close({ restoreFocus: false });
            return;
        }
        // A focused verb can vanish under the keyboard (Resume disables itself while its request runs and
        // Chromium drops focus to the document): keep the reader in the same row, else on the control.
        const lost = event.target;
        const rowEl = lost?.closest?.('.rw-row');
        if (next || !rowEl) return;
        clearTimeout(focusTimer);
        focusTimer = setTimeout(() => {
            focusTimer = 0;
            if (!open || destroyed) return;
            const active = doc.activeElement;
            if ((active && active !== doc.body) || (lost.isConnected && !lost.disabled)) return;
            const view = [...views.values()].find((candidate) => candidate.el === rowEl);
            const stop = [view?.more, view?.main].find((node) => node?.isConnected && !node.disabled && node.tabIndex >= 0);
            (stop || trigger).focus?.({ preventScroll: true });
        }, 0);
    });
    listen(surface, 'keydown', (event) => {
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
        const stops = [...surface.querySelectorAll('.rw-main[tabindex="0"]')];
        if (!stops.length) return;
        event.preventDefault();
        const index = stops.indexOf(event.target?.closest?.('.rw-main'));
        const step = event.key === 'ArrowDown' ? 1 : -1;
        const next = stops[index < 0 ? (step > 0 ? 0 : stops.length - 1) : Math.max(0, Math.min(stops.length - 1, index + step))];
        next?.focus?.({ preventScroll: true });
        next?.scrollIntoView?.({ block: 'nearest' });
    });
    // The Project panel's own close dismisses a Shade that lives outside the room's page.
    listen(doc, 'click', (event) => {
        if (open && event.target?.closest?.('#project-panel-close')) close({ restoreFocus: false });
    }, true);

    // A room hidden without being destroyed (a Project kept for its pending files) takes its control along.
    const Observer = globalThis.MutationObserver;
    const watcher = typeof Observer === 'function' ? new Observer(() => {
        const away = Boolean(page.hidden) || !page.isConnected;
        if (away) { navigationIntent += 1; close({ restoreFocus: false }); showBack(null); }
        trigger.hidden = away;
    }) : null;
    watcher?.observe(page, { attributes: true, attributeFilter: ['hidden'] });

    function schedule() {
        if (destroyed || scheduled) return;
        scheduled = -1;
        const handle = frame(() => { scheduled = 0; render(); });
        if (scheduled) scheduled = handle || -1;  // a synchronous frame (the node fixture) has already rendered
    }

    render();
    return {
        update: schedule,
        close: () => close({ restoreFocus: false }),
        // The room leaves the screen: the list closes and the saved passage no longer applies.
        leave() {
            navigationIntent += 1;
            // Main stays mounted behind a Project. Cancel the shared reading claim too, so an
            // outstanding canonical question read cannot scroll or focus that obscured room.
            reading.cancel();
            close({ restoreFocus: false });
            showBack(null);
        },
        isOpen: () => open,
        destroy() {
            if (destroyed) return;
            if (open) closeTaskControlMenu();
            destroyed = true;
            menuIntent += 1;
            navigationIntent += 1;
            if (scheduled > 0) globalThis.cancelAnimationFrame?.(scheduled);
            clearTimeout(backTimer);
            clearTimeout(focusTimer);
            clearTimeout(outsideClickTimer);
            if (outsideClick) doc.removeEventListener('click', outsideClick, true);
            watcher?.disconnect();
            for (const dispose of disposers.splice(0)) dispose();
            openHost?.removeAttribute('data-rw-open');
            // The badge returns where the product put it; this module's nodes go.
            if (badgeParent?.isConnected) badgeParent.insertBefore(statusBadge, badgeNext?.parentNode === badgeParent ? badgeNext : null);
            trigger.remove();
            (shadeAnchor || surface).remove();
            back.remove();
            live.remove();
            views.clear();
        },
    };
}
