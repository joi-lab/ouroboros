// Replies of one chat instance (docs/DESIGN.md "Replies"): the Reply control beside Copy in a bubble's
// control row, the quote above the replying bubble's text, the strip above the composer while the owner
// is replying, the jump from a quote to the message it quotes, and the owner's words a relay carries into a
// work's room (one Ouroboros bubble: his words in a quote panel above the note). A reply is a reference the
// host resolves: this module sends only the quoted row's address and never decides who reads the words.
import { tr } from './i18n.js';
import { showToast } from './toast.js';
import { ensureLiveActionsEl } from './chat_activity.js';
import { authorOf, createRelayQuote, createReplyQuote, normalizeReplyRef, replySnippet } from './chat_reply_quote.js';
import { fullText } from './text_fit.js';

const REPLY_ICON = '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"'
    + ' stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M6 11.5 2.5 8 6 4.5"></path><path d="M13.5 12v-1A3 3 0 0 0 10.5 8h-8"></path></svg>';
const TARGET_MS = 1600;
const HEADER_CLEARANCE = 72;
const ROLE_NAMES = ['user', 'assistant', 'system'];
const ROWS = '.chat-bubble, .chat-live-card';

/** The row a host-derived quote would repeat: the visible row right above `node` (its own work card is part of it). */
function rowAbove(node) {
    let row = node?.previousElementSibling;
    while (row && (row.hidden || !row.matches(ROWS) || (row.matches('.chat-live-card') && row.dataset.taskId
        && row.dataset.taskId === node.dataset.taskId))) row = row.previousElementSibling;
    return row;
}

/**
 * `feed` holds the bubbles (each stamped `data-row-address`), `input` is the composer field (the strip goes before its
 * row), `key` names the sessionStorage slot of a staged reply. `reading()` is the room's reading-position owner,
 * resolved when a quote is pressed; `onDomWrite` is the instance's stable-viewport seam for a clamp's toggle.
 */
export function createChatReplies({ chatId, feed, input, key = '', reading, onDomWrite = (write) => write() }) {
    // The strip stands right before the field's row (below the pills); a bare field (a test fixture) takes it itself.
    const textRow = input.closest('.chat-text-row') || input;
    let storage = null;
    try { storage = sessionStorage; } catch { /* storage blocked: a staged reply lives for this page only */ }
    const storageKey = key;
    let staged = null;
    let strip = null;
    let targetTimer = 0;

    const remember = () => {
        if (!storage || !storageKey) return;
        try {
            if (staged) storage.setItem(storageKey, JSON.stringify(staged));
            else storage.removeItem(storageKey);
        } catch { /* a refused write leaves the strip for this page only */ }
    };

    const rowOf = (address) => (address ? [...feed.querySelectorAll('[data-row-address]')]
        .find((node) => node.dataset.rowAddress === address) : null) || null;

    function markTarget(target) {
        clearTimeout(targetTimer);
        feed.querySelectorAll('.is-quote-target').forEach((old) => old.classList.remove('is-quote-target'));
        void target.offsetWidth;
        target.classList.add('is-quote-target');
        if (target.tabIndex < 0) target.tabIndex = -1;
        target.focus({ preventScroll: true });
        targetTimer = setTimeout(() => target.classList.remove('is-quote-target'), TARGET_MS);
    }

    /** Scrolls to the quoted message when this room has it loaded; says so when it does not. */
    function jump(ref) {
        const target = rowOf(ref.address);
        if (!target) {
            showToast(tr('chat.reply.not_loaded', 'That message is not loaded here'), 'info');
            return false;
        }
        const position = reading();
        position.cancel();
        feed.scrollTop += target.getBoundingClientRect().top - feed.getBoundingClientRect().top - HEADER_CLEARANCE;
        position.stick = false;
        position.scroll();
        markTarget(target);
        return true;
    }

    function paint() {
        strip?.remove();
        strip = staged ? createReplyQuote(staged, { composer: true, onJump: jump, onCancel: () => cancel() }) : null;
        if (strip) {
            const main = strip.querySelector('.chat-quote-main');
            main.id = `${input.id}-reply`;
            textRow.before(strip);
            // The field announces what it answers whenever it takes focus.
            input.setAttribute('aria-describedby', main.id);
        } else input.removeAttribute('aria-describedby');
    }

    /**
     * Reply to one row. A still-open question card answers in its own field (the existing answer door, never a
     * second path); anything else stages the strip above the composer.
     */
    function start(raw) {
        const ref = normalizeReplyRef(raw);
        if (!ref?.address) return;
        const door = rowOf(ref.address)?.querySelector('.chat-quiz-comment:not(:disabled):not([readonly])');
        if (door) { door.focus(); return; }
        staged = ref;
        paint();
        remember();
        input.focus({ preventScroll: true });
    }

    /** Drops the strip and keeps whatever the owner already typed. */
    function cancel() {
        if (!staged) return;
        staged = null;
        paint();
        remember();
        input.focus({ preventScroll: true });
    }

    /** After a send: the next message answers nothing. */
    function clear() {
        if (!staged) return;
        staged = null;
        paint();
        remember();
    }

    function onKeydown(event) {
        if (event.key !== 'Escape' || !staged || event.isComposing) return;
        event.preventDefault();
        cancel();
    }
    // One listener on the composer wrap: Esc from the field and from the strip's own buttons lands here.
    const keyHost = textRow.parentElement || input;
    keyHost?.addEventListener('keydown', onKeydown);

    /** What a Reply on this bubble quotes (its own address, author, words and time), or null without an address. */
    function bubbleRef(bubble) {
        const address = bubble?.dataset?.rowAddress || '';
        if (!address) return null;
        const role = ROLE_NAMES.find((name) => bubble.classList.contains(name)) || '';
        const words = bubble.querySelector(':scope > .message')?.textContent
            || bubble.querySelector('.chat-quiz-question')?.textContent || '';
        return { address, chat_id: chatId, role, author_label: bubble.querySelector(':scope > .sender')?.textContent || '',
            kind: bubble.querySelector('.chat-quiz-card') ? 'question' : 'message', snippet: replySnippet(words),
            ts: bubble.dataset.ts ? new Date(Number(bubble.dataset.ts)).toISOString() : '', task_id: bubble.dataset.taskId || '' };
    }

    /** The Reply button, placed with the bubble's Copy in one control row. Needs a row address: an unsaved echo has none. */
    function mountControl(bubble) {
        if (!bubbleRef(bubble)) return null;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'chat-message-reply';
        button.innerHTML = REPLY_ICON;
        button.title = tr('chat.reply.reply', 'Reply');
        button.setAttribute('aria-label', tr('chat.reply.reply', 'Reply'));
        button.dataset.rowReply = '1';
        button.addEventListener('click', () => start(bubbleRef(bubble)));
        const copy = bubble.querySelector(':scope > .chat-message-copy');
        const row = document.createElement('div');
        row.className = 'chat-message-actions';
        if (copy) copy.replaceWith(row); else bubble.append(row);
        row.append(button);
        if (copy) row.append(copy);
        bubble.classList.add('has-reply');
        return button;
    }

    try {
        const saved = storage && storageKey ? storage.getItem(storageKey) : '';
        if (saved) { staged = normalizeReplyRef(JSON.parse(saved)); paint(); }
    } catch { staged = null; }

    /**
     * What a reply to a work card quotes: the work itself (its task id is its durable address; for a Continue
     * card, the current episode's), named by its title, with its latest line or, settled, its chip's words.
     */
    function cardRef(record) {
        const line = fullText(record.activityEl).trim();
        const chip = record.phaseEl?.textContent?.trim() || '';
        return {
            address: `task:${record.groupId}`, chat_id: chatId, role: 'assistant', kind: 'work_card',
            author_label: record.titleEl?.textContent?.trim() || authorOf('assistant'),
            snippet: replySnippet(line || chip), ts: record.latestActivityRaw || '', task_id: record.groupId,
        };
    }

    /**
     * The card's own Reply (docs/DESIGN.md "Replies"): neutral, never the primary, first in the card's action row,
     * on running and settled work alike. `wanted` is false for anything that is not a root work card.
     * The card becomes a quote's jump target by the same address.
     */
    function syncCard(record, wanted) {
        const existing = record?.cardReplyEl;
        if (!wanted) {
            if (existing) { existing.remove(); record.cardReplyEl = null; return true; }
            return false;
        }
        const address = `task:${record.groupId}`;
        if (record.root.dataset.rowAddress !== address) record.root.dataset.rowAddress = address;
        const actions = ensureLiveActionsEl(record);
        if (!actions) return false;
        let button = existing;
        if (!button) {
            button = document.createElement('button');
            button.type = 'button';
            button.className = 'btn btn-sm btn-default';
            button.dataset.cardReply = '1';
            button.textContent = tr('chat.reply.reply', 'Reply');
            button.setAttribute('aria-label', tr('chat.reply.reply_to_work', 'Reply to this work'));
            button.addEventListener('click', () => start(cardRef(record)));
            record.cardReplyEl = button;
        }
        if (actions.firstElementChild !== button) { actions.prepend(button); return true; }
        return !existing;
    }

    /**
     * A quote the host derived for an Ouroboros row (the owner message its turn answers) repeats nothing when that
     * message is the row right above it; it shows once another message or card came between (a late answer, a
     * return from a Project). History fills in above, so a landing row re-reads itself and the row below it.
     */
    function syncDerivedQuote(bubble) {
        const quote = bubble?.querySelector?.(':scope > .chat-quote[data-derived="1"]');
        if (!quote) return false;
        const hidden = rowAbove(bubble)?.dataset.rowAddress === quote.dataset.address;
        if (quote.hidden === hidden) return false;
        quote.hidden = hidden;
        return true;
    }

    function syncDerivedQuotes(around = null) {
        if (!around) {
            return [...feed.querySelectorAll('.chat-bubble > .chat-quote[data-derived="1"]')]
                .reduce((changed, quote) => syncDerivedQuote(quote.parentElement) || changed, false);
        }
        // The first bubble below (past cards, which may be that bubble's own work) may now repeat or stop repeating.
        let below = around.nextElementSibling;
        while (below && (below.hidden || !below.matches('.chat-bubble'))) below = below.nextElementSibling;
        return Boolean(syncDerivedQuote(around) | syncDerivedQuote(below));
    }

    return {
        /**
         * One call per text bubble, after Copy is mounted. `row` is the history row or live frame the bubble was built
         * from: its `reply_to` becomes the quote above the text (the owner's own reference, or on an Ouroboros row the
         * host-derived one), an Ouroboros row's `relay` the owner's words in a relay panel, and its `row_address` makes
         * the bubble a jump target with a Reply control (author, role and task come from the bubble itself).
         */
        decorate(bubble, row) {
            const address = String(row?.row_address || '');
            if (address) bubble.dataset.rowAddress = address;
            const sender = bubble.querySelector('.sender');
            const role = ROLE_NAMES.find((name) => bubble.classList.contains(name));
            const relay = role === 'assistant' ? createRelayQuote(row?.relay, { onDomWrite }) : null;
            const quote = relay || createReplyQuote(row?.reply_to, { onJump: jump });
            if (quote && !relay && role !== 'user') {
                quote.dataset.derived = '1';
                quote.dataset.address = normalizeReplyRef(row.reply_to)?.address || '';
            }
            if (quote) sender?.after(quote);
            mountControl(bubble);
        },
        /** A row built outside addMessage (media, files, links, a question card): its address makes it a reply target. */
        stamp(bubble, row) {
            const address = String(row?.row_address || '');
            if (bubble && address && bubble.dataset.rowAddress !== address) bubble.dataset.rowAddress = address;
        },
        /** The host's saved row for the owner's own echo: the bubble takes its address and, with it, its Reply. */
        adopt(row) {
            const cmid = String(row?.client_message_id || '');
            const address = String(row?.row_address || '');
            if (row?.role !== 'user' || !cmid || !address) return false;
            const bubble = [...feed.querySelectorAll('.chat-bubble.user[data-client-message-id]')]
                .find((node) => node.dataset.clientMessageId === cmid);
            if (!bubble || bubble.dataset.rowAddress === address) return false;
            bubble.dataset.rowAddress = address;
            if (!bubble.querySelector(':scope > .chat-message-actions > .chat-message-reply')) mountControl(bubble);
            syncDerivedQuotes(bubble);
            return true;
        },
        /** What the row menu's Reply on this bubble quotes, or null. */
        bubbleRef,
        start,
        syncCard,
        syncDerivedQuotes,
        /**
         * "Send as a new message" under a delivery doubt: the owner's words join this composer's draft (sent, they are
         * a new message with a fresh id); nothing is sent here.
         */
        compose(words) {
            const draft = input.value.trimEnd();
            input.value = draft ? `${draft}\n\n${words}` : words;
            input.dispatchEvent?.(new Event('input', { bubbles: true }));  // the composer's own sizing and draft keeping
            input.focus({ preventScroll: true });
        },
        /** The field of the chat frame: only the address travels, the host makes the snippet. */
        frameField: () => (staged ? { reply_to: staged.address } : {}),
        /** The local echo of a reply, shaped like a row so it draws the same quote (it has no row address yet). */
        echoRow: () => (staged ? { reply_to: staged } : null),
        clear,
        destroy() {
            clearTimeout(targetTimer);
            keyHost?.removeEventListener('keydown', onKeydown);
            strip?.remove();
            strip = null;
            staged = null;
        },
    };
}
