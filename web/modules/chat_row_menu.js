// The row menu of one chat feed (docs/DESIGN.md "Replies"): what a bubble or a root work card offers, reached
// without hover — a right-click, Shift+F10 or the ContextMenu key on the focused row, a long press (no movement)
// on touch. Its items are the row's own doors, never second ones: Reply, Copy, Open project (when the row has
// one) and, on a work card, Resume / Pause / Stop… exactly when the card itself offers them (Pause is the item of
// the card's Stop… menu, offered by the same rule). Esc and an outside press close it; keyboard focus enters the
// menu and returns to the row. A right-click on a link, a field or selected words keeps the browser's own menu,
// so selected text stays copyable.
import { tr } from './i18n.js';
import { bindMenu } from './ui_interactions.js';
import { ACTION_PAUSE, pauseTaskAction, taskControlActions } from './task_control_menu.js';
import { copyTextWithToast } from './ui_helpers.js';
import { fullText } from './text_fit.js';

const LONG_PRESS_MS = 450;
const MOVE_TOLERANCE_PX = 10;
const ROW = '.chat-bubble, .chat-live-card:not(.subagent)';
const NATIVE = 'a[href], input, textarea, select, [contenteditable="true"], video, audio';

const shown = (node) => Boolean(node?.isConnected && !node.hidden && !node.disabled);

/**
 * `feed` is the chat's transcript, `replies` its reply controller (chat_reply.js), `cardOf(taskId)` its root card
 * record, `activityOf(record)` the census row that card's controls read. Returns `{ open, destroy }`.
 */
export function createChatRowMenu({ feed, replies, cardOf, activityOf }) {
    const doc = feed.ownerDocument;
    let current = null;  // { menu, binding, row }
    let press = null;  // a touch that may become (or became) a long press
    let swallowClickUntil = 0;
    let rightPress = null;  // what was selected when a right button went down, before its own default selected a word

    const selectedIn = (row) => {
        const selection = doc.getSelection?.();
        return Boolean(selection && !selection.isCollapsed && row.contains(selection.anchorNode));
    };

    const rowOf = (node) => {
        const row = node?.closest?.(ROW);
        return row && feed.contains(row) ? row : null;
    };

    function bubbleItems(row) {
        const items = [];
        const ref = replies.bubbleRef(row);
        if (ref) items.push(['reply', tr('chat.reply.reply', 'Reply'), () => replies.start(ref)]);
        const copy = row.querySelector(':scope > .chat-message-copy, :scope > .chat-message-actions > .chat-message-copy');
        if (copy) items.push(['copy', tr('code.copy', 'Copy'), () => copy.click(), true]);
        return items;
    }

    function cardItems(row, record) {
        const items = [];
        if (shown(record.cardReplyEl)) items.push(['reply', tr('chat.reply.reply', 'Reply'), () => record.cardReplyEl.click()]);
        const words = [record.titleEl?.textContent, fullText(record.activityEl)].map((text) => String(text || '').trim());
        if (words.some(Boolean)) items.push(['copy', tr('code.copy', 'Copy'),
            () => copyTextWithToast(words.filter(Boolean).join('\n')), true]);
        if (shown(record.resumeRunBtn)) items.push(['resume', tr('task.control.resume', 'Resume'), () => record.resumeRunBtn.click(), true]);
        if (shown(record.cancelRunBtn)) {
            // The card's own Stop… offers Pause on the same facts; its menu re-checks before any effect.
            const activity = activityOf(record);
            if (taskControlActions({ cancelPending: Boolean(record.cancelPendingPolicy),
                budgetPaused: activity?.phase === 'budget_paused' }).includes(ACTION_PAUSE)) {
                items.push(['pause', tr('task.control.pause', 'Pause'), () => pauseTaskAction(record.groupId), true]);
            }
            items.push(['stop', record.cancelRunBtn.textContent, () => record.cancelRunBtn.click()]);
        }
        return items;
    }

    function itemsFor(row) {
        const record = row.matches('.chat-live-card') ? cardOf(row.dataset.taskId || '') : null;
        const items = record ? cardItems(row, record) : bubbleItems(row);
        // The row's own Project reference (a portal's footer, the reference under an answer or a receipt).
        const reference = [...row.querySelectorAll('[data-intent="open-project"]')]
            .find((node) => shown(node) && rowOf(node) === row && !node.closest('.chat-live-card.subagent'));
        if (reference) items.splice(Math.min(items.length, 2), 0,
            ['project', tr('chat.row.open_project', 'Open project'), () => reference.click()]);
        return items;
    }

    function close(options) {
        current?.binding.close(options);
    }

    /** Open the menu of `row`, at a pointer `point` or under the focused `anchor`. False when the row offers nothing. */
    function open(row, { point, anchor = row } = {}) {
        close();
        const items = itemsFor(row);
        if (!items.length) return false;
        // Focus returns to the row: a bubble or card that is not itself focusable takes focus like a quote's target.
        if (anchor === row && row.tabIndex < 0 && !row.hasAttribute('tabindex')) row.tabIndex = -1;
        const menu = doc.createElement('div');
        menu.className = 'ui-popup chat-row-menu';
        menu.setAttribute('role', 'menu');
        menu.setAttribute('aria-label', row.matches('.chat-live-card') ? tr('chat.row.work_actions', 'Work actions')
            : tr('chat.row.message_actions', 'Message actions'));
        for (const [kind, label, run, keepFocus] of items) {
            const item = doc.createElement('button');
            item.type = 'button';
            item.setAttribute('role', 'menuitem');
            item.dataset.rowAction = kind;
            item.textContent = label;
            item.addEventListener('click', (event) => {
                event.stopPropagation();
                // An item that moves focus on its own (Reply to the composer, Stop… into its menu) is not given it back.
                close({ restoreFocus: Boolean(keepFocus), reason: 'action' });
                run();
            });
            menu.append(item);
        }
        doc.body.append(menu);
        const binding = bindMenu(menu, { anchor, point, onClose: () => {
            menu.remove();
            if (current?.menu === menu) current = null;
        } });
        current = { menu, binding, row };
        return true;
    }

    function onContextMenu(event) {
        const row = rowOf(event.target);
        if (!row) return;
        // A long press already answered (Android also fires contextmenu for it).
        if (press?.fired || Date.now() < swallowClickUntil || current?.row === row) { event.preventDefault(); return; }
        // Words the owner selected keep the browser's menu (Copy them). A right button on macOS selects the word under
        // it before this event; that selection is the gesture's own, not the owner's, and goes with the row menu.
        const owned = rightPress?.row === row ? rightPress.selected : selectedIn(row);
        rightPress = null;
        if (event.target.closest(NATIVE) || owned) return;
        if (!open(row, { point: { x: event.clientX, y: event.clientY } })) return;
        event.preventDefault();
        if (selectedIn(row)) doc.getSelection().removeAllRanges();
    }

    function onKeydown(event) {
        if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
        const focused = doc.activeElement;
        const row = rowOf(focused);
        if (!row || focused.closest(NATIVE)) return;
        if (open(row, { anchor: focused })) event.preventDefault();
    }

    function cancelPress() {
        if (press) clearTimeout(press.timer);
        press = null;
    }

    function onPointerDown(event) {
        cancelPress();
        const row = rowOf(event.target);
        rightPress = event.button === 2 && row ? { row, selected: selectedIn(row) } : null;
        if (event.pointerType !== 'touch' || !event.isPrimary) return;
        if (!row || event.target.closest(NATIVE)) return;
        const { clientX: x, clientY: y } = event;
        const touch = { id: event.pointerId, x, y, fired: false };
        touch.timer = setTimeout(() => { touch.fired = open(row, { point: { x, y } }); }, LONG_PRESS_MS);
        press = touch;
    }

    function onPointerMove(event) {
        if (press && !press.fired && event.pointerId === press.id
            && Math.hypot(event.clientX - press.x, event.clientY - press.y) > MOVE_TOLERANCE_PX) cancelPress();
    }

    // A touch is captured by its first target, so its end arrives here even when the menu lies under the finger:
    // the tap that ends a long press is not also a press of whatever is there (a menu item included).
    function onPointerEnd(event) {
        if (press?.fired && event.pointerId === press.id) swallowClickUntil = Date.now() + 400;
        cancelPress();
    }

    function onClickCapture(event) {
        if (Date.now() >= swallowClickUntil) return;
        swallowClickUntil = 0;
        event.preventDefault();
        event.stopPropagation();
    }

    const listeners = [['contextmenu', onContextMenu], ['keydown', onKeydown], ['pointerdown', onPointerDown],
        ['pointermove', onPointerMove], ['pointerup', onPointerEnd], ['pointercancel', onPointerEnd], ['scroll', cancelPress]];
    for (const [type, handler] of listeners) feed.addEventListener(type, handler);
    doc.addEventListener('click', onClickCapture, true);

    return {
        open,
        destroy() {
            cancelPress();
            close();
            for (const [type, handler] of listeners) feed.removeEventListener(type, handler);
            doc.removeEventListener('click', onClickCapture, true);
        },
    };
}
