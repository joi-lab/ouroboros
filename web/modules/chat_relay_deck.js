// Notes between works (DESIGN "Replies"): each relay keeps its own row, address, actions and reading anchors.
// Every consecutive relay joins a deck, including those carrying the owner's words. The latest card stands in
// front; a blue mark names the earlier cards with his words. Show N notes / Hide controls the deck, while one
// more / less control opens a card's note and all its quotations together inside a stable-viewport write.
import { fmt, tr } from './i18n.js';
import { formatMsgTime } from './utils.js';
import { fitText, fullText } from './text_fit.js';
import { motionMs, reducedMotion } from './motion.js';
import { projectReference } from './project_reference.js';
import { fetchJson } from './api_client.js';

let nextNoteId = 0;
const EDGES = 2;   // at most two edges behind the front card

const node = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text != null) el.textContent = text;
    return el;
};
const control = (cls, text) => {
    const el = node('button', `ui-hit-area ${cls}`, text);
    el.type = 'button';
    return el;
};
const setData = (el, key, value) => {
    if (value == null || value === false) delete el.dataset[key];
    else el.dataset[key] = String(value);
};
const isNote = (el) => Boolean(el?.__relayNote);
const transparent = (el) => el.hidden || Boolean(el.classList?.contains('typing-bubble'));

/**
 * One instance per room feed (chat_reply.js creates it beside the replies). `onDomWrite` is the room's stable-viewport
 * seam: every change that moves heights runs inside it.
 */
export function createRelayDecks({ feed, onDomWrite = (write) => write(), formatTime = formatMsgTime,
    lookupProject = async (chatId, signal) => {
        const data = await fetchJson(`/api/projects?chat_id=${encodeURIComponent(chatId)}`, { signal });
        return data?.projects?.length === 1 ? data.projects[0] : null;
    } } = {}) {
    const rooms = new Map(), lookup = new AbortController();
    const fresh = new Set();     // notes adopted since the last reconcile
    let scheduled = false, settled = false, destroyed = false;

    const timing = () => {
        let ease = '';
        try {
            const win = globalThis.window;
            ease = win.getComputedStyle(win.document.documentElement).getPropertyValue('--ease-sheet').trim();
        } catch { ease = ''; }
        return { open: motionMs('--motion-open'), close: motionMs('--motion-close'), ease: ease || 'cubic-bezier(0.2, 0.8, 0.2, 1)' };
    };
    // Position and opacity only, on the sheet curve; reduced motion or no engine plays nothing.
    const play = (el, frames, duration, delay = 0) => {
        if (!el || typeof el.animate !== 'function' || reducedMotion() || !duration) return null;
        try { return el.animate(frames, { duration, delay, easing: timing().ease, fill: 'backwards' }); } catch { return null; }
    };

    /* ---- a note: the relay bubble dressed as a card ------------------------------------------------------------- */
    function noteOf(row, quoted, bubble) {
        const relay = row?.relay && typeof row.relay === 'object' ? row.relay : {};
        const sender = relay.sender && typeof relay.sender === 'object' ? relay.sender : null;
        // A note saved before its room was recorded: Main when it came from Main, never an invented room.
        const fromMain = sender ? Number(sender.chat_id) === 1 : !quoted && Number(relay.source?.chat_id) === 1;
        const time = formatTime(row?.ts) || null;
        return {
            quoted, chatId: Number(sender?.chat_id),
            words: String(row?.text ?? bubble.querySelector(':scope > .message')?.textContent ?? '').trim(),
            summary: String(relay.summary || '').trim(),
            name: String(sender?.name || '').trim() || (fromMain ? 'Main' : tr('chat.relay.another_work', 'Another work')),
            kind: fromMain ? 'main' : 'project', time: time?.short || '', when: time?.full || '',
        };
    }

    function dress(bubble, note) {
        bubble.classList.add('chat-relay-note');
        bubble.dataset.kind = note.kind;
        bubble.dataset.raw = '0';
        const head = node('div', 'chat-relay-card-head');
        const from = node('span', 'chat-relay-from', note.name);
        const sub = node('span', 'chat-relay-sub');
        const time = node('time', 'chat-relay-time', note.time);
        if (note.when) time.title = note.when;
        sub.append(node('span', '', tr('chat.relay.to_this_work', 'note to this work')), node('span', '', ' · '), time);
        const act = control('chat-relay-act', '');
        act.hidden = true;
        head.append(from, sub, act);
        // Only a host-resolved Project gets a reference. A missing/failed lookup stays an honest plain name.
        // Cache per room feed, and cancel pending reads when that feed is disposed.
        if (Number.isInteger(note.chatId) && note.chatId > 1) {
            if (!rooms.has(note.chatId)) rooms.set(note.chatId, Promise.resolve()
                .then(() => lookupProject(note.chatId, lookup.signal)).catch(() => null));
            rooms.get(note.chatId).then((project) => {
                if (destroyed || !project?.id || Number(project.chat_id) !== note.chatId) return;
                onDomWrite(() => { from.replaceWith(projectReference(project)); return true; });
            });
        }
        const sender = bubble.querySelector(':scope > .sender');
        bubble.insertBefore(head, sender || bubble.firstElementChild);
        const excerpt = node('div', 'chat-relay-excerpt');
        const gist = node('p', 'chat-relay-gist');
        if (!note.summary) gist.dataset.raw = '1';
        const more = control('chat-relay-more', tr('chat.relay.more', 'more'));
        more.setAttribute('aria-expanded', 'false');
        const id = `chat-relay-text-${++nextNoteId}`;
        gist.id = `${id}-gist`;
        const message = bubble.querySelector(':scope > .message');
        if (message) message.id ||= id;
        const quotes = [...bubble.querySelectorAll('.chat-quote[data-mode="relay"] > .chat-quote-text')];
        quotes.forEach((text, i) => { text.id ||= `${id}-quote-${i}`; });
        more.setAttribute('aria-controls', [gist.id, message?.id, ...quotes.map(text => text.id)].filter(Boolean).join(' '));
        bubble.__relayNote.more = more;
        excerpt.append(gist, more);
        const quote = bubble.querySelector(':scope > .chat-quote, :scope > .chat-relay-quotes');
        (quote || sender || head).after(excerpt);
        fitText(gist, note.summary || note.words, 3);
        if (message) bubble.insertBefore(node('div', 'chat-relay-raw-label', tr('chat.relay.as_written', 'Note as written')), message);
        bubble.addEventListener('click', (event) => onPress(bubble, event));
    }

    const actOf = (bubble) => bubble.querySelector(':scope > .chat-relay-card-head .chat-relay-act');

    // `Show 7 notes`: one label, the figure in its own box so it can roll; the button's flex gap never touches the words.
    function setCount(act, n) {
        const [lead = '', tail = ''] = fmt('Show {n} notes', { n }).split(String(n));
        const figure = node('span', 'chat-relay-count');
        figure.append(node('span', 'chat-relay-count-value', String(n)));
        const label = node('span', 'chat-relay-act-label');
        label.append(node('span', '', lead), figure, node('span', '', tail));
        act.replaceChildren(label);
        act.setAttribute('aria-label', fmt('Show {n} notes', { n }));
    }

    function roll(act, from, to) {
        const figure = act.querySelector('.chat-relay-count');
        const current = figure?.querySelector('.chat-relay-count-value');
        const { open } = timing();
        if (!current || typeof current.animate !== 'function' || reducedMotion() || !open) return;
        current.textContent = String(from);
        const next = node('span', 'chat-relay-count-value', String(to));
        figure.append(next);
        const out = current.animate([{ transform: 'translateY(0)', opacity: 1 }, { transform: 'translateY(-100%)', opacity: 0 }],
            { duration: Math.round(open * 0.76), easing: timing().ease, fill: 'forwards' });
        out.finished.then(() => current.remove(), () => current.remove());
        play(next, [{ transform: 'translateY(100%)', opacity: 0 }, { transform: 'translateY(0)', opacity: 1 }], Math.round(open * 0.94), 40);
    }

    const range = (run) => {
        const a = run[0].__relayNote.note.time, b = run[run.length - 1].__relayNote.note.time;
        return a && b && a !== b ? `${a}–${b}` : (b || a || '');
    };

    function caption(run) {
        const top = node('div', 'chat-relay-deck-top');
        const text = node('div', 'chat-relay-deck-caption');
        const line = node('span', 'chat-relay-deck-line');
        line.append(node('b', '', fmt('{n} notes', { n: run.length })), node('span', '', range(run) ? ` · ${range(run)}` : ''));
        text.append(line);
        top.append(text, control('chat-relay-hide', tr('chat.relay.hide', 'Hide')));
        return top;
    }

    // Everything that follows from a run and its state: who is in front, what is tucked, the edges, the labels. It is
    // idempotent: a note whose place and state did not change keeps every node (a focused Hide, a rolling count), so
    // the room's ordinary reconciles write nothing here.
    function paint(run, open) {
        const n = run.length;
        const deck = n > 1;
        const span = deck && open ? range(run) : '';
        const quoted = run.slice(0, -1).filter(bubble => bubble.__relayNote.note.quoted).length;
        // A focused Hide follows its caption when the caption moves to another first card (an older note joined).
        const heldHide = run.some((bubble) => bubble.querySelector(':scope > .chat-relay-deck-top')?.contains(document.activeElement));
        let changed = false;
        run.forEach((bubble, i) => {
            const front = i === n - 1;
            bubble.__relayRun = run;
            if (deck) bubble.__relayOpen = open;  // a note split off on its own keeps its deck's intent for a later join
            const key = [n, i, deck && open ? 1 : 0, bubble.dataset.raw, quoted, i === 0 ? span : ''].join('|');
            if (bubble.__relayPainted === key) return;
            bubble.__relayPainted = key;
            changed = true;
            const tucked = deck && !open && !front;
            setData(bubble, 'deck', deck ? (open ? 'open' : 'closed') : null);
            setData(bubble, 'deckTucked', tucked ? '1' : null);
            setData(bubble, 'deckEdges', deck && !open && front ? Math.min(n - 1, EDGES) : null);
            setData(bubble, 'deckFirst', deck && open && i === 0 ? '1' : null);
            const top = bubble.querySelector(':scope > .chat-relay-deck-top');
            if (deck && open && i === 0) {
                const held = Boolean(top?.contains(document.activeElement));
                const fresh1 = caption(run);
                if (top) top.replaceWith(fresh1); else bubble.prepend(fresh1);
                if (held) fresh1.querySelector('.chat-relay-hide')?.focus({ preventScroll: true });
            } else top?.remove();
            const oldMark = bubble.querySelector(':scope > .chat-relay-words-inside');
            if (deck && !open && front && quoted) {
                const label = quoted === 1 ? tr('chat.relay.earlier_words_one', 'Your words in 1 earlier note')
                    : fmt('Your words in {n} earlier notes', { n: quoted });
                if (oldMark) oldMark.textContent = label;
                else bubble.querySelector(':scope > .chat-relay-card-head').after(node('div', 'chat-relay-words-inside', label));
            } else oldMark?.remove();
            const act = actOf(bubble);
            if (act) {
                act.hidden = !(deck && !open && front);
                if (!act.hidden) { setCount(act, n); act.setAttribute('aria-expanded', 'false'); }
            }
            const raw = bubble.dataset.raw === '1';
            const more = bubble.__relayNote.more;
            more.textContent = raw ? tr('chat.relay.less', 'less') : tr('chat.relay.more', 'more');
            more.setAttribute('aria-expanded', String(raw));
            more.setAttribute('aria-label', raw ? fmt('Collapse note from {name}', { name: bubble.__relayNote.note.name })
                : fmt('Read full note and quoted words from {name}', { name: bubble.__relayNote.note.name }));

        });
        if (heldHide && !run[0].querySelector(':scope > .chat-relay-deck-top')?.contains(document.activeElement)) {
            run[0].querySelector(':scope > .chat-relay-deck-top .chat-relay-hide')?.focus({ preventScroll: true });
        }
        return changed;
    }

    /* ---- presses ---------------------------------------------------------------------------------------------------- */
    function onPress(bubble, event) {
        const run = bubble.__relayRun || [bubble];
        const deck = run.length > 1;
        const open = bubble.dataset.deck === 'open';
        if (event.target.closest('.chat-relay-hide')) {
            setOpen(run, false);
            actOf(run[run.length - 1])?.focus({ preventScroll: true });
            return;
        }
        if (event.target.closest('.chat-relay-act')) {
            const held = document.activeElement === actOf(bubble);
            setOpen(run, true);
            if (held) bubble.__relayNote.more.focus({ preventScroll: true });
            return;
        }
        if (event.target.closest('.chat-relay-more')) {
            setRaw(bubble, bubble.dataset.raw !== '1');
            return;
        }
        // The closed deck answers a press anywhere on its front card (never on its quote, a control, a link or a selection).
        if (deck && !open && !event.target.closest('.chat-quote, .chat-relay-quotes, button, a, .chat-message-actions')
            && !String(globalThis.getSelection?.() || '')) {
            setOpen(run, true);
        }
    }

    function setRaw(bubble, on) {
        onDomWrite(() => {
            bubble.dataset.raw = on ? '1' : '0';
            const { note, more } = bubble.__relayNote;
            const held = document.activeElement === more;
            fitText(bubble.querySelector('.chat-relay-gist'), note.summary || note.words, on ? 0 : 3);
            for (const text of bubble.querySelectorAll('.chat-quote[data-mode="relay"] > .chat-quote-text')) {
                setData(text, 'expanded', on ? '1' : null);
                fitText(text, fullText(text), on ? 0 : 3);
            }
            const message = bubble.querySelector(':scope > .message');
            if (on && message) message.after(more);
            else bubble.querySelector('.chat-relay-excerpt').append(more);
            if (held) more.focus({ preventScroll: true });
            paint(bubble.__relayRun || [bubble], bubble.dataset.deck === 'open');
            return true;
        });
        if (on) {
            const { open } = timing();
            for (const part of bubble.querySelectorAll(':scope > .chat-relay-raw-label, :scope > .message, :scope > .chat-message-actions')) {
                play(part, [{ opacity: 0, transform: 'translateY(-4px)' }, { opacity: 1, transform: 'none' }], open);
            }
        }
    }

    // The deck fans out and folds back: the new heights land at once inside one viewport write; then the cards that come
    // out of the deck slide in from just above, and the front card glides from where it stood only when that is above
    // where it lands. A card never starts below its place (see the note in the loop).
    function setOpen(run, open, { instant = false } = {}) {
        if (run.length < 2) return;
        const front = run[run.length - 1];
        const from = front.getBoundingClientRect();
        onDomWrite(() => { paint(run, open); return true; });
        if (instant) return;
        const { open: openMs, close } = timing();
        const shown = open ? run : [front];
        shown.forEach((bubble, i) => {
            const emerging = open && bubble !== front;
            const dy = emerging ? -6 : from.top - bubble.getBoundingClientRect().top;
            // A card never starts below where it lands: moving content would reach past the feed's end, and a write in
            // the meantime would read the reader as having left the newest message.
            if (dy > 0) return;
            play(bubble, [{ transform: `translateY(${dy}px)`, opacity: emerging ? 0 : 1 },
                { transform: 'none', opacity: 1 }], open ? openMs : close, emerging ? Math.min(i, 6) * 18 : 0);
        });
        if (open) play(run[0].querySelector(':scope > .chat-relay-deck-top'), [{ opacity: 0 }, { opacity: 1 }], openMs, 90);
    }

    /* ---- the feed ------------------------------------------------------------------------------------------------ */
    // Runs of consecutive notes in feed order: hidden rows and the typing row are transparent, any other row ends a
    // run. The presence of the owner's words never splits a run.
    function runs() {
        const out = [];
        let run = [];
        const flush = () => { if (run.length) out.push(run); run = []; };
        for (const el of [...feed.children]) {
            if (isNote(el)) {
                run.push(el);
            } else if (!transparent(el)) flush();
        }
        flush();
        return out;
    }

    // A note arriving live lands at the newest end: no visible row after it.
    const atNewest = (bubble) => {
        for (let el = bubble.nextElementSibling; el; el = el.nextElementSibling) if (!transparent(el)) return false;
        return true;
    };

    function reconcile() {
        scheduled = false;
        if (destroyed) return;
        const arrived = [...fresh].filter((bubble) => bubble.parentElement === feed);
        fresh.clear();
        if (!feed.querySelector(':scope > .chat-relay-note')) { settled = true; return; }
        // One note landing at the newest end of a room already drawn is an arrival; a history page is not.
        const live = settled && arrived.length === 1 && atNewest(arrived[0]) ? arrived[0] : null;
        settled = true;
        const before = live?.__relayRun?.length || 0;
        let joined = null;
        onDomWrite(() => {
            let changed = false;
            for (const run of runs()) {
                // A deck stays open while any of its notes was open (a run joined by a removed row keeps it).
                // A note being read on its own (its text open) opens the deck it joins; Hide keeps a deck shut.
                const open = run.length > 1 && run.some((bubble) => bubble.__relayOpen
                    || (!bubble.dataset.deck && bubble.dataset.raw === '1'));
                const previous = run.length > 1 && live && run[run.length - 1] === live ? run[run.length - 2].__relayRun : null;
                if (previous) joined = { run, count: previous.length };
                changed = paint(run, open) || changed;
            }
            return changed;
        });
        if (!live) return;
        const { open } = timing();
        play(live, [{ transform: 'translateY(-8px) scale(0.985)', opacity: 0 }, { transform: 'none', opacity: 1 }], open);
        if (joined && joined.run.length > 1 && live.dataset.deck === 'closed') roll(actOf(live), joined.count || before, joined.run.length);
    }

    function schedule() {
        if (scheduled || destroyed) return;
        scheduled = true;
        queueMicrotask(reconcile);
    }

    // A row inserted between two notes, or taken out between them, changes the runs: the feed's child list says so where
    // an observer exists (the browser); everywhere else each adopted note and each landed row asks for it.
    const watcher = typeof MutationObserver === 'function' ? new MutationObserver(schedule) : null;
    watcher?.observe(feed, { childList: true });

    return {
        /** A relay row's bubble once its quote is decorated: `row` is the history row or live frame, `quoted` whether it
         * passes the owner's words. The bubble is dressed as a card at once (it lands already drawn) and joins its deck
         * once it stands in the feed. */
        adopt(bubble, row, quoted) {
            if (!bubble || !row || bubble.__relayNote) return;
            const note = noteOf(row, Boolean(quoted), bubble);
            bubble.__relayNote = { note };
            dress(bubble, note);
            fresh.add(bubble);
            schedule();
        },
        schedule,
        /** A quote's jump to a tucked note opens its deck first, so the row it lands on is visible; else null. */
        surface(target) {
            if (!isNote(target) || target.dataset.deckTucked !== '1') return null;
            setOpen(target.__relayRun, true, { instant: true });
            return target;
        },
        destroy() {
            destroyed = true;
            watcher?.disconnect();
            lookup.abort();
            rooms.clear();
            fresh.clear();
        },
    };
}
