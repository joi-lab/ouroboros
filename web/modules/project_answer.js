/**
 * Project lifecycle rows in Main (docs/DESIGN.md "Project completion mirror").
 *
 * A Project root that ended with Ouroboros's own final answer reaches Main as an
 * ordinary Ouroboros message, folded when it is long. Every other lifecycle row (a
 * start, an ending with no answer, a row written before the answer rode the row)
 * keeps its System text. Either way the row ends with the same Project reference:
 * the voice of a row never chooses how the UI points at its Project.
 */
import { createSystemMessageActions } from './ui_helpers.js';
import { tr } from './i18n.js';
import { projectReference } from './project_reference.js';
import { terminalTimeNote } from './utils.js';

export const PROJECT_ROW_TYPES = new Set(['project_started', 'project_handoff', 'project_completion_summary']);

// Host-stamped Project lifecycle rows (§3 Main rows), live and replayed alike:
// a mirrored final answer is an ordinary assistant message, the rest System text.
export function appendProjectRow(addMessage, msg, text, opts) {
    const a = msg.completion_answer;
    return addMessage(a || text, a ? 'assistant' : 'system', !!(a || msg.markdown), msg.ts || null, false, {
        ...opts, systemType: msg.system_type, narration: msg.narration, projectId: msg.project_id || '',
        projectName: msg.project_name || '', taskName: msg.task_name || '', handoffId: msg.handoff_id || '', terminalTime: msg.terminal_time || null,
        taskId: msg.task_id || opts?.taskId || '', completionLimitation: msg.completion_reason || '', deliveryId: msg.delivery_id || '',
    });
}

// The fade says "there is more": it appears only when the fold really hides text. The clamp
// itself is unconditional CSS, so measuring the fade never moves layout. A row mounted while
// Chat is not the visible page has no layout box (clientHeight 0): it is left alone and
// measured when Chat is shown; a resize re-measures, because the width decides the height.
export function refreshProjectAnswerFold(bubble) {
    if (!bubble?.classList.contains('project-answer')) return;
    const message = bubble.querySelector('.message');
    if (!message || !message.clientHeight) return;
    const folded = message.scrollHeight > message.clientHeight + 1;
    bubble.classList.toggle('is-folded', folded);
    syncFold(bubble, message, folded);
}

// Folded text offers no keyboard stops (pointer clicks still reach visible links): the Project
// reference leads to the complete answer. The table enhancer respects the fold even
// when it registers later; removing its tabindex lets it restore normal keyboard use.
// Browsers also tab into overflow boxes without an explicit tabindex.
const FOCUSABLE = 'a[href], button, input, select, textarea, summary, [tabindex], pre, .katex-display, .md-mermaid, .md-chart';
function syncFold(bubble, message, collapsed) {
    for (const node of message.querySelectorAll(collapsed ? FOCUSABLE : '[data-fold-stop], .md-table-wrap[tabindex="-1"]')) {
        node.toggleAttribute('data-fold-stop', collapsed);
        if (collapsed) node.setAttribute('tabindex', '-1');
        else node.removeAttribute('tabindex');
    }
}

// One set of window listeners for the app's lifetime, however many rows exist: a row holds
// only listeners on its own nodes, so a released row needs no disposer here.
let watching = false;
let queued = false;
function afterLayout(work) {
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(work);
}
// A feed whose width is still moving lays out only the rows near the screen (chat_reading_position.js marks it
// [data-resizing]); measuring a row it skips would lay that row out again, on every frame of the move. Its rows are
// measured once the width holds.
function refreshFoldsSoon() {
    if (queued) return;
    queued = true;
    afterLayout(() => {
        queued = false;
        document.querySelectorAll('.chat-bubble.project-answer').forEach((bubble) => {
            if (!bubble.closest('[data-resizing]')) refreshProjectAnswerFold(bubble);
        });
    });
}
function watchFolds() {
    if (watching || typeof window === 'undefined') return;
    watching = true;
    window.addEventListener('ouro:page-shown', refreshFoldsSoon);
    window.addEventListener('resize', refreshFoldsSoon);
    window.addEventListener('ouro:feed-width-settled', refreshFoldsSoon);
}

export function decorateProjectRow(bubble, { role = 'system', projectId = '', projectName = '', terminalTime, addedAt,
    systemType = '', completion = systemType === 'project_completion_summary',
    completionLimitation = '', limitation = completionLimitation, fold = role === 'assistant' } = {}) {
    if (!bubble || !projectId) return null;
    if (fold) {
        bubble.classList.add('project-answer');
        watchFolds();
        afterLayout(() => refreshProjectAnswerFold(bubble));
    }
    const actions = createSystemMessageActions(projectReference({ id: projectId, name: projectName }));
    bubble.querySelector('.message')?.after(actions);
    if (completion) {
        if (limitation && role === 'assistant') {
            const warning = bubble.ownerDocument.createElement('div');
            warning.className = 'msg-provenance project-answer-limitation';
            warning.textContent = limitation;
            warning.title = limitation;
            bubble.insertBefore(warning, actions);
        }
        paintTerminalTime(bubble, terminalTimeNote(terminalTime, addedAt));
    }
    return actions;
}

// One compact time line (owner 17.10, Q7 A): the row's own time also says when its task ended, both facts only
// when their minutes differ; the full instants are its tooltip and what a screen reader hears.
function paintTerminalTime(bubble, { short, full }) {
    const doc = bubble.ownerDocument;
    const time = bubble.querySelector('.msg-time') || bubble.appendChild(doc.createElement('div'));
    time.className = 'msg-time';
    time.title = full;
    const shown = doc.createElement('span');
    shown.setAttribute('aria-hidden', 'true');
    shown.textContent = short;
    const spoken = doc.createElement('span');
    spoken.className = 'msg-time-full';
    spoken.textContent = full;
    time.replaceChildren(shown, spoken);
}

/** Kept beside the authored body; adoption never replaces the selected text. */
export function syncSavedProjectContext(bubble, projected, originId = '') {
    if (bubble && originId) bubble.dataset.originId = originId;
    if (!bubble || (projected && bubble.dataset.historyId)) return false;
    const prior = bubble.querySelector('.saved-project-context');
    if (!projected) {
        if (!prior) return false;
        prior.remove(); delete bubble.dataset.originProjected;
        return true;
    }
    if (prior) return false;
    const note = bubble.ownerDocument.createElement('div');
    note.className = 'msg-provenance saved-project-context';
    note.textContent = tr('project.saved_context_note', 'Saved project context — original request retained here; surrounding history may still need loading.');
    bubble.insertBefore(note, bubble.querySelector('.message')?.nextSibling || null);
    bubble.dataset.originProjected = '1';
    return true;
}
