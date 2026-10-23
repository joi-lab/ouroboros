// Notes between works (chat_relay_deck.js) over the DOM double: a relay row's bubble is dressed as a note card and
// keeps its row (address, history identity, actions); a run of notes is one deck with the latest card in front and the
// older ones tucked; a blue mark names tucked cards with the owner's words; a row between notes splits the run and taking it out
// joins it again, still open; a note without the teller's line opens with its own words in the meta ink; the deck fans
// out and folds back; a note opens as written; a quote's jump to a tucked note opens its deck. Motion and layout run in
// the browser lane.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createRelayDecks } from '../modules/chat_relay_deck.js';
import { createChatReplies } from '../modules/chat_reply.js';
import { installDomDouble } from './dom_double.js';

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function fixture(options = {}) {
    const env = installDomDouble();
    const { el } = env;
    const feed = el('div', { className: 'chat-messages' });
    env.doc.body.append(feed);
    const decks = createRelayDecks({ feed, lookupProject: async () => null, ...options });
    let seq = 0;
    const note = (text, { summary = '', sender = { name: 'Main', chat_id: 1 }, quote = '', source = null } = {}) => {
        seq += 1;
        const ts = `2026-10-21T23:${String(10 + seq).padStart(2, '0')}:00Z`;
        const bubble = el('div', { className: 'chat-bubble assistant',
            dataset: { systemType: 'relay_entry', ts: String(Date.parse(ts)), rowAddress: `row:${seq}@r#${seq}` } },
        el('div', { className: 'sender', text: 'Ouroboros' }),
        ...(quote ? [el('div', { className: 'chat-quote', dataset: { mode: 'relay' }, text: quote })] : []),
        el('div', { className: 'message', text }),
        el('div', { className: 'msg-time', text: '23:10' }),
        el('div', { className: 'chat-message-actions' }, el('button', { className: 'chat-message-reply' }), el('button', { className: 'chat-message-copy' })));
        const relay = { relay_id: `r${seq}`, text: quote, quotes: quote ? [{ text: quote }] : [],
            ...(summary ? { summary } : {}), ...(sender ? { sender } : {}), ...(source ? { source } : {}) };
        decks.adopt(bubble, { text, ts, relay }, Boolean(quote));  // dressed before it lands, as decorate does
        feed.append(bubble);
        return bubble;
    };
    const row = (text) => {
        const bubble = el('div', { className: 'chat-bubble user', text });
        feed.append(bubble);
        return bubble;
    };
    const visible = (bubble) => bubble.dataset.deckTucked !== '1';
    const act = (bubble) => bubble.querySelector('.chat-relay-act');
    const more = (bubble) => bubble.querySelector('.chat-relay-more');
    return { env, feed, decks, note, row, visible, act, more, close() { decks.destroy(); env.restore(); } };
}

test('a run of notes is one deck of the rows themselves, the latest note in front, the older tucked', async () => {
    const f = fixture();
    try {
        const rows = Array.from({ length: 7 }, (_, i) => f.note(`raw${i}:glued-work-words`, { summary: `Line ${i} for the owner` }));
        await settle();
        const front = rows[6];
        assert.equal(f.feed.children.length, 7, 'no element is added to the feed: the rows are the cards');
        assert.ok(rows.every((bubble) => bubble.classList.contains('chat-relay-note') && !bubble.hidden), 'every row stays a row');
        assert.ok(rows.every((bubble, i) => bubble.dataset.rowAddress === `row:${i + 1}@r#${i + 1}`), 'addresses unchanged');
        assert.ok(rows.slice(0, 6).every((bubble) => bubble.dataset.deckTucked === '1'), 'the older notes are tucked');
        assert.equal(f.visible(front), true);
        assert.equal(front.dataset.deckEdges, '2', 'the front card draws two edges behind it');
        assert.equal(front.querySelector('.chat-relay-from').textContent, 'Main');
        assert.equal(front.querySelector('.chat-relay-gist').textContent, 'Line 6 for the owner');
        assert.equal(f.act(front).textContent, 'Show 7 notes');
        assert.equal(f.act(front).getAttribute('aria-expanded'), 'false');
        assert.equal(front.dataset.raw, '0', 'the note as written waits for a press');
    } finally { f.close(); }
});

test('the owner\'s words join a deck, while any other row ends a run', async () => {
    const f = fixture();
    try {
        const first = f.note('first');
        const second = f.note('second');
        f.row('owner says something');
        const quoted = f.note('third, passing his words', { quote: 'please check the logs', summary: 'Asks the work to check the logs' });
        const fourth = f.note('fourth');
        await settle();
        assert.equal(first.dataset.deckTucked, '1');
        assert.equal(second.dataset.deckEdges, '1', 'two notes: one edge behind the front card');
        assert.equal(quoted.dataset.deckTucked, '1', 'his words join the next note in a deck');
        const kids = quoted.children;
        const quoteAt = kids.indexOf(quoted.querySelector('.chat-quote'));
        assert.ok(quoteAt > kids.indexOf(quoted.querySelector('.chat-relay-card-head'))
            && quoteAt < kids.indexOf(quoted.querySelector('.chat-relay-excerpt')), 'his words between the head and the line');
        assert.equal(fourth.dataset.deck, 'closed');
        assert.equal(f.act(fourth).textContent, 'Show 2 notes');
        assert.equal(fourth.querySelector('.chat-relay-words-inside').textContent, 'Your words in 1 earlier note');
    } finally { f.close(); }
});

test('a later note comes to the front; a row landing between notes splits the run and taking it out joins it, still open', async () => {
    const f = fixture();
    try {
        const one = f.note('one');
        const two = f.note('two');
        await settle();
        const three = f.note('three');
        await settle();
        assert.equal(three.dataset.deckEdges, '2');
        assert.equal(two.dataset.deckTucked, '1', 'the previous front card joins the deck behind the new one');
        assert.equal(f.act(three).textContent, 'Show 3 notes');
        const between = f.env.el('div', { className: 'chat-bubble user', text: 'between' });
        f.feed.insertBefore(between, two);
        f.decks.schedule();
        await settle();
        assert.equal(one.dataset.deck, undefined, 'one note before the row is a card on its own');
        assert.equal(f.visible(one), true);
        assert.equal(two.dataset.deckTucked, '1');
        assert.equal(three.dataset.deckEdges, '1');
        f.act(three).click();
        assert.equal(three.dataset.deck, 'open');
        between.remove();
        f.decks.schedule();
        await settle();
        assert.ok([one, two, three].every((bubble) => bubble.dataset.deck === 'open'), 'the joined deck stays open');
        assert.equal(one.dataset.deckFirst, '1', 'the caption moves to the new first card');
        assert.match(one.querySelector('.chat-relay-deck-top').textContent, /^3 notes/);
    } finally { f.close(); }
});

test('an ordinary reconcile rewrites nothing: a focused Hide keeps its focus, a front card keeps its count node', async () => {
    const f = fixture();
    try {
        const rows = [f.note('a'), f.note('b'), f.note('c')];
        await settle();
        const count = f.act(rows[2]).querySelector('.chat-relay-count');
        f.decks.schedule();
        await settle();
        assert.equal(f.act(rows[2]).querySelector('.chat-relay-count'), count, 'an unchanged deck keeps its nodes');
        f.act(rows[2]).focus();
        f.act(rows[2]).click();
        assert.equal(f.env.doc.activeElement, f.more(rows[2]), 'focus stays on the visible front card');
        const hide = rows[0].querySelector('.chat-relay-hide');
        hide.focus();
        f.row('an answer lands');
        f.decks.schedule();
        await settle();
        assert.equal(rows[0].querySelector('.chat-relay-hide'), hide, 'the caption stays the same node');
        assert.equal(f.env.doc.activeElement, hide, 'and Hide keeps the focus');
    } finally { f.close(); }
});

test('a focused Hide follows the caption when an older note becomes the first card of an open deck', async () => {
    const f = fixture();
    try {
        const later = [f.note('b'), f.note('c')];
        await settle();
        f.act(later[1]).click();
        later[0].querySelector('.chat-relay-hide').focus();
        const older = f.note('a, older');
        f.feed.insertBefore(older, later[0]);
        f.decks.schedule();
        await settle();
        assert.equal(older.dataset.deckFirst, '1');
        assert.equal(later[0].querySelector('.chat-relay-deck-top'), null, 'the caption left the old first card');
        assert.equal(f.env.doc.activeElement, older.querySelector('.chat-relay-hide'), 'and Hide kept the focus');
    } finally { f.close(); }
});

test('a note read on its own opens the deck it joins, so the passage stays; Hide keeps a deck shut', async () => {
    const f = fixture();
    try {
        const first = f.note('a long note being read');
        await settle();
        f.more(first).click();
        assert.equal(first.dataset.raw, '1');
        const second = f.note('a second note arrives');
        await settle();
        assert.equal(first.dataset.deck, 'open', 'the deck it joins opens');
        assert.equal(f.visible(first), true, 'the note being read stays on screen');
        first.querySelector('.chat-relay-hide').click();
        assert.equal(first.dataset.deckTucked, '1');
        f.note('a third note');
        await settle();
        assert.equal(second.dataset.deck, 'closed', 'after Hide a new note keeps the deck shut');
        assert.equal(first.dataset.raw, '1', 'its own open text stays open behind the front card');
    } finally { f.close(); }
});

test('a note without the teller\'s line opens with its own words, in the meta ink; a summary leads otherwise', async () => {
    const f = fixture();
    try {
        const raw = f.note('a0e1:research-done,no-safe-restore');
        f.row('break');
        const summarized = f.note('a0e1:research-done', { summary: 'Research is done; no safe fix yet' });
        await settle();
        assert.equal(raw.querySelector('.chat-relay-gist').dataset.raw, '1');
        assert.equal(raw.querySelector('.chat-relay-gist').textContent, 'a0e1:research-done,no-safe-restore');
        assert.equal(summarized.querySelector('.chat-relay-gist').dataset.raw, undefined, 'the teller\'s line reads in the voice\'s ink');
        assert.equal(summarized.querySelector('.chat-relay-gist').textContent, 'Research is done; no safe fix yet');
    } finally { f.close(); }
});

test('a note saved before its room was recorded says Main only when it came from Main', async () => {
    const f = fixture();
    try {
        const main = f.note('from main', { sender: null, source: { chat_id: 1, label: 'Main' } });
        f.row('break');
        const unknown = f.note('from a project', { sender: null, source: { chat_id: 52601710, label: 'Main' } });
        f.row('break');
        const named = f.note('named', { sender: { name: 'Claudexor 478', chat_id: 52601710 } });
        await settle();
        assert.equal(main.querySelector('.chat-relay-from').textContent, 'Main');
        assert.equal(main.dataset.kind, 'main');
        assert.equal(unknown.querySelector('.chat-relay-from').textContent, 'Another work', 'never an invented room');
        assert.equal(unknown.dataset.kind, 'project');
        assert.equal(named.querySelector('.chat-relay-from').textContent, 'Claudexor 478');
        assert.equal(named.dataset.kind, 'project');
    } finally { f.close(); }
});

test('Show N notes fans the deck out; more opens the note with the row\'s actions; Hide folds it', async () => {
    const f = fixture();
    try {
        const rows = [f.note('w1'), f.note('w2:as-written'), f.note('w3')];
        await settle();
        f.act(rows[2]).click();
        assert.ok(rows.every((bubble) => bubble.dataset.deck === 'open' && f.visible(bubble)), 'every card stands');
        assert.equal(rows[0].dataset.deckFirst, '1');
        assert.match(rows[0].querySelector('.chat-relay-deck-top').textContent, /^3 notes · /);
        assert.equal(rows[2].dataset.deckEdges, undefined, 'open, no edges');
        const middle = rows[1];
        assert.equal(f.more(middle).textContent, 'more');
        f.more(middle).click();
        assert.equal(middle.dataset.raw, '1', 'the note as written and its Reply and Copy show');
        assert.equal(f.more(middle).textContent, 'less');
        assert.equal(f.more(middle).getAttribute('aria-expanded'), 'true');
        f.more(middle).click();
        assert.equal(middle.dataset.raw, '0');
        rows[0].querySelector('.chat-relay-hide').click();
        assert.ok(rows.slice(0, 2).every((bubble) => bubble.dataset.deckTucked === '1'));
        assert.equal(f.act(rows[2]).textContent, 'Show 3 notes');
        assert.equal(rows[0].querySelector('.chat-relay-deck-top'), null, 'folded, no caption');
    } finally { f.close(); }
});

test('a quote\'s jump to a tucked note opens its deck; a visible note or any other row stays as it is', async () => {
    const f = fixture();
    try {
        const first = f.note('one');
        const second = f.note('two');
        const owner = f.row('mine');
        await settle();
        assert.equal(f.decks.surface(second), null, 'the front card is already visible');
        assert.equal(f.decks.surface(first), first);
        assert.equal(first.dataset.deck, 'open');
        assert.equal(f.visible(first), true);
        assert.equal(f.decks.surface(owner), null);
    } finally { f.close(); }
});

test('a relay row the room decorates is dressed as a note; a bubble without the relay type stays a bubble', async () => {
    const env = installDomDouble();
    const feed = env.el('div', { className: 'chat-messages' });
    const input = env.el('textarea', { attrs: { id: 'chat-input' } });
    env.doc.body.append(feed, env.el('div', { className: 'chat-text-row' }, input));
    const replies = createChatReplies({ chatId: 7, feed, input, reading: () => ({ cancel() {}, scroll() {}, remember() {}, stick: true }) });
    try {
        const make = (dataset) => env.el('div', { className: 'chat-bubble assistant', dataset },
            env.el('div', { className: 'sender', text: 'Ouroboros' }), env.el('div', { className: 'message', text: 'words' }));
        const relayRow = make({ systemType: 'relay_entry' });
        const plain = make({});
        const relay = { relay_id: 'r', text: '', quotes: [], summary: 'Tells the work what to check', sender: { name: 'Main', chat_id: 1 } };
        replies.decorate(relayRow, { row_address: 'row:1@a#1', role: 'assistant', text: 'words', relay });
        replies.decorate(plain, { row_address: 'row:2@a#2', role: 'assistant', text: 'words', relay });
        feed.append(relayRow, plain);
        await settle();
        assert.equal(relayRow.classList.contains('chat-relay-note'), true);
        assert.equal(relayRow.querySelector('.chat-relay-gist').textContent, 'Tells the work what to check');
        assert.equal(relayRow.querySelector('.chat-message-reply') !== null, true, 'the row keeps its own Reply');
        assert.equal(plain.classList.contains('chat-relay-note'), false, 'only a relay row becomes a note');
    } finally { replies.destroy(); env.restore(); }
});


test('the blue mark counts earlier cards, excluding the front, and follows live arrivals and splits', async () => {
    const f = fixture();
    try {
        const one = f.note('first', { quote: 'one' });
        const two = f.note('second', { quote: 'two' });
        const front = f.note('latest', { quote: 'front words' });
        await settle();
        assert.equal(front.querySelector('.chat-relay-words-inside').textContent, 'Your words in 2 earlier notes');
        f.more(front).click();
        assert.equal(front.dataset.deck, 'closed', 'more opens only this card, not the deck');
        assert.equal(front.dataset.raw, '1');
        f.act(front).click();
        assert.equal(front.querySelector('.chat-relay-words-inside'), null);
        one.querySelector('.chat-relay-hide').click();
        assert.equal(f.env.doc.activeElement, f.act(front));
        const latest = f.note('new arrival');
        await settle();
        assert.equal(latest.querySelector('.chat-relay-words-inside').textContent, 'Your words in 3 earlier notes');
        const boundary = f.row('boundary');
        f.feed.insertBefore(boundary, two);
        f.decks.schedule();
        await settle();
        assert.equal(latest.querySelector('.chat-relay-words-inside').textContent, 'Your words in 2 earlier notes');
        assert.equal(one.dataset.deck, undefined);
    } finally { f.close(); }
});

test('hidden rows and typing are transparent, and a quoted tucked row is surfaced by a jump', async () => {
    const f = fixture();
    try {
        const quoted = f.note('first', { quote: 'owner words' });
        f.row('hidden').hidden = true;
        f.feed.append(f.env.el('div', { className: 'typing-bubble' }));
        const front = f.note('second', { sender: { name: 'Another work', chat_id: 42 } });
        await settle();
        assert.equal(quoted.dataset.deckTucked, '1');
        assert.equal(front.dataset.deckEdges, '1');
        assert.equal(f.decks.surface(quoted), quoted);
        assert.equal(quoted.dataset.deckTucked, undefined);
    } finally { f.close(); }
});

test('known sender rooms use the Project reference, unknown rooms remain plain and lookups are shared', async () => {
    const lookups = [];
    const project = { id: 'sender', chat_id: 42, name: 'Sender room' };
    const f = fixture({ lookupProject: async id => { lookups.push(id); return id === 42 ? project : null; } });
    try {
        const known = f.note('known', { sender: { name: 'Saved name', chat_id: 42 } });
        f.note('same room', { sender: { name: 'Saved name', chat_id: 42 } });
        const absent = f.note('absent', { sender: { name: 'Gone room', chat_id: 99 } });
        const old = f.note('old', { sender: { task_id: 'missing-task' }, source: { chat_id: 42 } });
        await settle();
        const reference = known.querySelector('[data-intent="open-project"]');
        assert.equal(reference.getAttribute('aria-label'), 'Open project Sender room');
        assert.equal(reference.querySelector('.chat-live-project-name').textContent, 'Sender room');
        assert.equal(known.querySelector('.chat-relay-tile'), null);
        assert.equal(absent.querySelector('.chat-relay-from').textContent, 'Gone room');
        assert.equal(absent.querySelector('[data-intent="open-project"]'), null);
        assert.equal(old.querySelector('.chat-relay-from').textContent, 'Another work');
        assert.deepEqual(lookups, [42, 99], 'a quote source is never used as the sender destination');
    } finally { f.close(); }
});

test('disposing a room aborts a pending sender lookup and prevents a late DOM write', async () => {
    let resolve, signal;
    const f = fixture({ lookupProject: (_id, pending) => { signal = pending; return new Promise(done => { resolve = done; }); } });
    try {
        const note = f.note('pending', { sender: { name: 'Known name', chat_id: 42 } });
        await settle();
        f.decks.destroy();
        assert.equal(signal.aborted, true);
        resolve({ id: 'known', chat_id: 42, name: 'Resolved room' });
        await settle();
        assert.equal(note.querySelector('[data-intent="open-project"]'), null);
    } finally { f.close(); }
});
