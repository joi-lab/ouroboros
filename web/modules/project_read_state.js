// When a Project room has READ a visible revision (DESIGN "Project unread dot").
//
// A revision is read only when a history read covering it has crossed a real
// browser paint while the room stayed shown AND the reader is at the newest
// message — the one that arrived last, on screen (`isAtNewestMessage`), normally
// near the bottom of the conversation. Opening or refreshing a room while its
// reader is elsewhere is not reading; the chat instance reports each arrival at
// the newest message (a scroll, a page shown again, an older page applied) so the
// withheld acknowledgement is retried then, and app.js's existing state snapshots
// offer it again — no timer of this module's own. This module owns the paint
// generation, the highest covered revision, which message is the newest and that
// arrival edge; the chat instance owns reading history, and app.js alone posts
// the acknowledgement.

import { sameHistoryChain } from './chat_history.js';
import { historyNodeOnScreen } from './chat_history_replay.js';

/**
 * @param {{
 *   read: (fresh: boolean) => Promise<boolean>,  // true when the recent read succeeded
 *   isShown: () => boolean,
 *   isReadingLatest: (latest: undefined|null|object) => boolean,  // the reader is at `latest`
 *   onReadingLatest?: () => void,
 * }} facts
 */
export function createProjectReadReceipt({ read, isShown, isReadingLatest, onReadingLatest = () => {} }) {
    let generation = 0;
    let coveredRevision = 0;
    let readingLatest = false;
    let settledLatest;  // the newest message the previous settle named
    let recentRead = {};  // the newest admitted recent read: its window and coverage
    let found = null;  // what an older page of a quiet chain found: the newest message, or none
    // The recent read names the newest message (null: unknown). When its bounded
    // search ran out first, the older pages of its quiet chain carry it on
    // (history_paging.latest_arrival): the message they name, or the absence the
    // one reaching the start of the chat proves, still holds while this read found
    // none from its `latest_before` up to that chain's frozen upper, read through
    // the same room view on one chain of byte coordinates. A newer arrival, a
    // source gap or another membership breaks that.
    const newest = () => {
        const { window, coverage } = recentRead;
        return window?.latest_message === null && found && window.latest_before <= found.upper
            && coverage?.view === found.view && sameHistoryChain(coverage?.spans?.chat, found.span)
            ? found.message : window?.latest_message;
    };
    // Scroll edges report arrivals only; a discrete change (the page or the
    // window shown again, another newest message) reports the current position once.
    const note = ({ discrete = false } = {}) => {
        const latest = isReadingLatest(newest());
        if (latest && (discrete || !readingLatest)) onReadingLatest();
        readingLatest = latest;
    };
    // Another newest message than the previous settle named moves where the
    // reader must be: the edge is taken again without a scroll.
    const settle = () => {
        const identity = JSON.stringify(newest()) ?? '';
        if (settledLatest !== undefined && identity !== settledLatest) note({ discrete: true });
        settledLatest = identity;
    };
    return {
        cancel() { generation += 1; },
        // Only a revision newer than any covered one needs a fresh read; the
        // receipt is still taken after this call's own paint either way. A read
        // that cannot name the newest message covers nothing, so the same revision
        // is read again: a repaired source can name it without a new revision.
        async refresh({ revision = 0 } = {}) {
            const own = ++generation;
            const target = Math.max(0, Number(revision) || 0);
            const covered = await read(target > coveredRevision) && newest() !== null;
            if (covered && target > coveredRevision) coveredRevision = target;
            if (!covered || own !== generation || !isShown()) return { painted: false, revision: target };
            // Two frames cover layout followed by paint/composite.
            await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const painted = own === generation && isShown();
            const atLatest = painted && isReadingLatest(newest());
            // A withheld read lowers the edge, so the next arrival retries it.
            if (painted && !atLatest) readingLatest = false;
            return { painted, read: atLatest, revision: target };
        },
        note,
        // Each admitted recent read, before its rows are drawn. One that cannot
        // name the newest message leaves no revision covered, so the next refresh
        // reads again: a healed source names it without a new revision.
        recent({ window, coverage }) {
            recentRead = { window, coverage };
            if (newest() === null) coveredRevision = 0;
        },
        // An older page, once drawn, can show the newest message or, on a quiet
        // chain, name it or prove there is none.
        page({ window, coverage }) {
            const absent = window?.latest_absent === true;
            if ((absent || window?.latest_message?.history_id) && Number.isSafeInteger(coverage?.upper?.chat)) {
                found = { message: absent ? undefined : window.latest_message, upper: coverage.upper.chat,
                    span: coverage.spans?.chat, view: coverage.view };
            }
            requestAnimationFrame(() => { settle(); note(); });
        },
        settle,
    };
}

// The feed the reader can see: its viewport less the chrome drawn over it, the
// header above and the composer below; chrome covering it all leaves an empty
// band, where nothing is on screen. Eviction keeps the whole viewport.
function readBand(viewport, header, composer) {
    let { top, bottom } = viewport.getBoundingClientRect();
    if (header?.getClientRects?.().length) top = Math.max(top, header.getBoundingClientRect().bottom);
    if (composer?.getClientRects?.().length) bottom = Math.min(bottom, composer.getBoundingClientRect().top);
    return { top, bottom };
}

// What shows a node: itself, or, inside a collapsed task card (no boxes of its
// own), the nearest enclosing card that is drawn. Collapsed cards need not be expanded.
function shownBy(node) {
    for (let shown = node; shown; shown = shown.parentElement?.closest?.('.chat-live-card')) {
        if (shown.getClientRects?.().length) return shown;
    }
    return node;
}

/**
 * Whether the reader is at the room's newest message. `latest` is the receipt's
 * newest message (the recent read's `window.latest_message`, or where an older
 * page carried its search on), the standalone message that ARRIVED last:
 * absent names nothing (the room holds no standalone message), so the
 * bottom of the conversation decides; null (its arrival unknown) is never read.
 * A named message — in its ordinary place or not — must have reached the page
 * and must itself be on screen, clear of the header and composer drawn over the
 * feed. Being at the bottom never stands in for it: later card rows and the
 * owner's own messages can push it above the fold, and a late answer keeping its
 * original time (`out_of_order`) is not at the bottom at all. A named message
 * with no node on the page is not on screen.
 *
 * @param {undefined|null|{history_id: string, out_of_order?: boolean}} latest
 * @param {{ delivered: (id: string) => boolean, nodes: (id: string) => Array<Element>,
 *   viewport: Element, header?: Element|null, composer?: Element|null, atBottom: () => boolean }} facts
 */
export function isAtNewestMessage(latest, { delivered, nodes, viewport, header, composer, atBottom }) {
    if (latest === undefined) return atBottom();
    if (!latest?.history_id || !delivered(latest.history_id)) return false;
    const band = readBand(viewport, header, composer);
    return band.bottom > band.top
        && nodes(latest.history_id).some((node) => historyNodeOnScreen(shownBy(node), viewport, band));
}
