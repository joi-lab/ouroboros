/**
 * Status marks (docs/DESIGN.md "Status marks"): the one small family of marks every surface uses to say what a work or
 * a room is doing — a Project row, Main's row, the Projects header, the phone menu button, a room's status, a Shade
 * row, a task card's chip, a question's head.
 *
 * Two facts, two channels. The SHAPE says what the work does; for projected work the server names it (`mark`:
 * moving | still | pause | ring | none, gateway/work_status.py `mark_of`). The HUE says whether there is something
 * for the owner: the owner's move (amber), new messages (crimson), an outcome beside its word, or nothing (the
 * surface's ink). This module only combines and draws them: it owns no words (callers put the word beside the mark and
 * into the accessible name) and decides no activity of its own.
 */

export const MARK_SHAPES = Object.freeze(['moving', 'still', 'pause', 'ring', 'dot', 'none']);
export const MARK_HUES = Object.freeze(['ink', 'unread', 'owner', 'ok', 'neutral', 'failed']);

// A known outcome is a dot in its tone, always beside its word: Done and Done with warnings succeed (the word says the
// warnings), Stopped was someone's decision, the rest failed.
const OUTCOME_HUES = Object.freeze({
    done: 'ok', warn: 'ok', cancelled: 'neutral', error: 'failed', timeout: 'failed', lifecycle_error: 'failed',
});

/**
 * The mark for one place. `mark` is the work's shape, `owner` whether something waits for the owner (a question or
 * an action: the server's `turn`, or a proved census fact), `unread` whether the place has new messages, `outcome` a
 * known result shown beside its word. The owner's move outranks everything and stands alone; an outcome is a dot in
 * its tone; new messages tint the work's shape, or are a dot where nothing works; else the shape in the ink.
 */
export function markView({ mark = 'none', owner = false, unread = false, outcome = '' } = {}) {
    const shape = MARK_SHAPES.includes(mark) && mark !== 'dot' ? mark : 'none';
    if (owner) return { shape: 'dot', hue: 'owner' };
    if (outcome && OUTCOME_HUES[outcome]) return { shape: 'dot', hue: OUTCOME_HUES[outcome] };
    if (unread) return { shape: shape === 'none' ? 'dot' : shape, hue: 'unread' };
    return { shape, hue: 'ink' };
}

const MARK_CLASS = 'status-mark';

/** One mark's anatomy: a decorative span with three dots (enough for the dots, the two pause bars and the ring). */
export function createStatusMark(doc = globalThis.document, extraClass = '') {
    const node = doc.createElement('span');
    node.className = extraClass ? `${MARK_CLASS} ${extraClass}` : MARK_CLASS;
    node.setAttribute('aria-hidden', 'true');
    node.dataset.shape = 'none';
    node.dataset.hue = 'ink';
    node.hidden = true;
    for (let i = 0; i < 3; i += 1) node.appendChild(doc.createElement('i'));
    return node;
}

/** The same anatomy as markup, for a template built as a string; `view` paints it at once (else it starts hidden). */
export function statusMarkHtml(extraClass = '', view = null) {
    const shape = MARK_SHAPES.includes(view?.shape) ? view.shape : 'none';
    const hue = MARK_HUES.includes(view?.hue) ? view.hue : 'ink';
    return `<span class="${MARK_CLASS}${extraClass ? ` ${extraClass}` : ''}" aria-hidden="true" data-shape="${shape}" `
        + `data-hue="${hue}"${shape === 'none' ? ' hidden' : ''}><i></i><i></i><i></i></span>`;
}

/** Paint `view` ({ shape, hue }) on a mark; a mark already showing it is not rewritten. Returns whether it changed. */
export function paintStatusMark(node, view = {}) {
    if (!node) return false;
    const shape = MARK_SHAPES.includes(view.shape) ? view.shape : 'none';
    const hue = MARK_HUES.includes(view.hue) ? view.hue : 'ink';
    const hidden = shape === 'none';
    if (node.dataset.shape === shape && node.dataset.hue === hue && node.hidden === hidden) return false;
    node.dataset.shape = shape;
    node.dataset.hue = hue;
    node.hidden = hidden;
    return true;
}
