// Replies of one chat (chat_reply.js) over a DOM double: a row's quote and Reply control, the relay panel (shape F:
// one Ouroboros bubble, his words above the note), the host-derived quote that repeats nothing directly under its
// message, the composer strip and what the frame carries, the owner's echo adopting its address, the question
// card's own answer door, and a missing fact leaving today's bubble.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatReplies } from '../modules/chat_reply.js';
import { installDomDouble } from './dom_double.js';

function fixture() {
    const env = installDomDouble();
    const { doc, el } = env;
    const feed = el('div', { className: 'chat-messages' });
    const input = el('textarea', { attrs: { id: 'chat-input' } });
    const textRow = el('div', { className: 'chat-text-row' }, input);
    const wrap = el('div', { className: 'chat-input-wrap' }, textRow);
    doc.body.append(feed, wrap);
    const positions = [];
    const replies = createChatReplies({ chatId: 1, feed, input, key: 'ouro_chat_reply',
        reading: () => ({ cancel() { positions.push('cancel'); }, scroll() { positions.push('scroll'); }, stick: true }) });
    const bubble = (role, text, { address = '', taskId = '' } = {}) => {
        const node = el('div', { className: `chat-bubble ${role}`, dataset: { ...(taskId ? { taskId } : {}) } },
            el('div', { className: 'sender', text: role === 'user' ? 'You' : 'Ouroboros' }),
            el('div', { className: 'message', text }),
            el('div', { className: 'msg-time', text: '09:40' }));
        if (address) node.dataset.ts = String(Date.parse('2026-10-16T09:40:00Z'));
        return node;
    };
    const quote = (node) => node.children.find((child) => child.classList.contains('chat-quote')) || null;
    const strip = () => wrap.children.find((child) => child.classList.contains('chat-quote--composer')) || null;
    return { ...env, feed, input, textRow, wrap, replies, bubble, quote, strip, positions,
        close() { replies.destroy(); env.restore(); } };
}

const owner = (address, text = 'please check the logs') => ({ row_address: address, role: 'user', text });

test('a saved row gets Reply beside Copy; a row without an address (an unsaved echo, an older host) stays as today', () => {
    const f = fixture();
    try {
        const saved = f.bubble('user', 'please check the logs');
        const copy = f.el('button', { className: 'chat-message-copy' });
        saved.append(copy);
        f.feed.append(saved);
        f.replies.decorate(saved, owner('row:1@a#1'));
        assert.equal(saved.dataset.rowAddress, 'row:1@a#1');
        const actions = saved.children.find((child) => child.classList.contains('chat-message-actions'));
        assert.deepEqual(actions.children.map((child) => child.className), ['chat-message-reply', 'chat-message-copy'],
            'one control row: Reply, then Copy');
        assert.equal(actions.children[0].getAttribute('aria-label'), 'Reply', 'an icon-only control has a name');
        assert.ok(saved.classList.contains('has-reply'));
        const legacy = f.bubble('assistant', 'an answer');
        f.feed.append(legacy);
        f.replies.decorate(legacy, { role: 'assistant', text: 'an answer' });
        assert.equal(legacy.dataset.rowAddress, undefined);
        assert.equal(legacy.children.some((child) => child.classList.contains('chat-message-actions')), false);
        assert.equal(f.quote(legacy), null);
    } finally { f.close(); }
});

test('Reply stages the strip and the next frame carries only the address; Esc or × cancels; a send clears it', () => {
    const f = fixture();
    try {
        const saved = f.bubble('assistant', 'The audit found three stale projects.');
        f.feed.append(saved);
        f.replies.decorate(saved, { row_address: 'row:1@b#2', role: 'assistant' });
        saved.querySelector('.chat-message-reply').click();
        const strip = f.strip();
        assert.ok(strip, 'the strip stands above the field');
        assert.equal(f.wrap.children.indexOf(strip) < f.wrap.children.indexOf(f.textRow), true);
        assert.equal(f.doc.activeElement, f.input, 'the field takes focus');
        assert.match(f.input.getAttribute('aria-describedby'), /reply$/);
        assert.deepEqual(f.replies.frameField(), { reply_to: 'row:1@b#2' }, 'the host makes the snippet');
        assert.equal(f.replies.echoRow().reply_to.address, 'row:1@b#2', 'the local echo draws the same quote');
        assert.equal(JSON.parse(f.storage.get('ouro_chat_reply')).address, 'row:1@b#2', 'kept for a reload');
        f.textRow.emit('keydown', { key: 'Escape' });
        assert.equal(f.strip(), null, 'Esc cancels');
        assert.deepEqual(f.replies.frameField(), {});
        assert.equal(f.storage.has('ouro_chat_reply'), false);
        saved.querySelector('.chat-message-reply').click();
        f.strip().querySelector('.chat-quote-cancel').click();
        assert.equal(f.strip(), null, '× cancels');
        saved.querySelector('.chat-message-reply').click();
        f.replies.clear();
        assert.equal(f.strip(), null, 'a send clears it');
        assert.equal(f.replies.echoRow(), null);
    } finally { f.close(); }
});

test('a reply to a still-open question card answers in the card\'s own field, never a second path', () => {
    const f = fixture();
    try {
        const field = f.el('textarea', { className: 'chat-quiz-comment' });
        const card = f.el('div', { className: 'chat-bubble assistant', dataset: { rowAddress: 'row:1@q#3' } },
            f.el('div', { className: 'chat-quiz-card' }, f.el('div', { className: 'chat-quiz-question', text: 'Which project?' }), field));
        f.feed.append(card);
        f.replies.start(f.replies.bubbleRef(card));
        assert.equal(f.doc.activeElement, field);
        assert.equal(f.strip(), null);
        assert.equal(f.replies.bubbleRef(card).kind, 'question');
        field.disabled = true;  // settled: the card takes no answer any more
        f.replies.start(f.replies.bubbleRef(card));
        assert.ok(f.strip(), 'a settled question is quoted like any row');
    } finally { f.close(); }
});

test('the owner\'s reply shows his quote; an unresolved address says so; a quote jumps to its loaded row', () => {
    const f = fixture();
    try {
        const target = f.bubble('assistant', 'earlier answer');
        target.dataset.rowAddress = 'row:1@c#4';
        const reply = f.bubble('user', 'what about this?');
        f.feed.append(target, reply);
        f.replies.decorate(reply, { role: 'user', row_address: 'row:1@c#5',
            reply_to: { address: 'row:1@c#4', role: 'assistant', author_label: 'Ouroboros', kind: 'message', snippet: 'earlier answer' } });
        const panel = f.quote(reply);
        assert.equal(panel.dataset.author, 'assistant', 'the quoted author\'s ink');
        assert.equal(panel.dataset.derived, undefined, 'the owner\'s own reference is always shown');
        panel.querySelector('.chat-quote-main').click();
        assert.equal(f.doc.activeElement, target, 'the jump lands on the quoted row');
        assert.ok(target.classList.contains('is-quote-target'));
        const gone = f.bubble('user', 'and this?');
        f.feed.append(gone);
        f.replies.decorate(gone, { role: 'user', reply_to: { address: 'row:9@x#0', unresolved: true } });
        assert.match(f.quote(gone).querySelector('.chat-quote-text').textContent, /not available/);
        assert.equal(f.quote(gone).dataset.kind, 'message');
    } finally { f.close(); }
});

test('a host-derived quote on an answer repeats nothing directly under its message and shows once something came between', () => {
    const f = fixture();
    try {
        const ask = f.bubble('user', 'how many projects?');
        ask.dataset.rowAddress = 'row:1@d#1';
        const answer = f.bubble('assistant', 'Seven.', { taskId: 'turn-1' });
        const derived = { row_address: 'row:1@d#2', role: 'assistant',
            reply_to: { address: 'row:1@d#1', role: 'user', author_label: 'You', snippet: 'how many projects?' } };
        f.feed.append(ask, answer);
        f.replies.decorate(answer, derived);
        f.replies.syncDerivedQuotes(answer);
        assert.equal(f.quote(answer).dataset.derived, '1');
        assert.equal(f.quote(answer).hidden, true, 'directly under the message it answers');
        // The answer's own work card is part of the answer, not something between.
        const ownCard = f.el('div', { className: 'chat-live-card', dataset: { taskId: 'turn-1' } });
        f.feed.insertBefore(ownCard, answer);
        f.replies.syncDerivedQuotes(ownCard);
        assert.equal(f.quote(answer).hidden, true);
        // Another owner message (or another work's card) came between: the quote says what is answered.
        const other = f.bubble('user', 'and the logs?');
        f.feed.insertBefore(other, ownCard);
        f.replies.syncDerivedQuotes(other);
        assert.equal(f.quote(answer).hidden, false);
        other.remove();
        f.replies.syncDerivedQuotes();
        assert.equal(f.quote(answer).hidden, true, 'the full pass re-reads every derived quote');
    } finally { f.close(); }
});

test('relay shape F: one Ouroboros bubble, his words in a relay panel above the note, files named', () => {
    const f = fixture();
    try {
        const entry = f.bubble('assistant', 'Anton writes from Main: check the stalled projects.');
        f.feed.append(entry);
        f.replies.decorate(entry, { row_address: 'row:5@e#1', role: 'assistant', type: 'relay_entry', relay: {
            relay_id: 'r-1', text: 'проверь прерванные проекты\nи верни их к работе',
            attachments: [{ filename: 'shot.png', display_name: 'shot.png' }, { name: 'plan.md' }],
            source: { chat_id: 1, label: 'Main', ts: '2026-10-16T09:37:00Z', address: 'row:1@m#7' } },
            reply_to: { address: 'row:1@m#7', role: 'user', snippet: 'ignored: the relay panel already quotes him' } });
        const panels = entry.children.filter((child) => child.classList.contains('chat-quote'));
        assert.equal(panels.length, 1, 'one panel: the relay, never a second quote beside it');
        const [panel] = panels;
        assert.equal(panel.dataset.mode, 'relay');
        assert.equal(panel.dataset.relayId, 'r-1');
        assert.equal(entry.children.indexOf(panel) < entry.children.indexOf(entry.querySelector('.message')), true,
            'his words stand above the note');
        assert.equal(panel.querySelector('.chat-quote-text').textContent, 'проверь прерванные проекты\nи верни их к работе',
            'his complete words, verbatim');
        assert.equal(panel.querySelector('.chat-quote-files').textContent, 'Attached: shot.png, plan.md');
        assert.equal(panel.querySelector('.chat-quote-author').textContent, 'You');
        assert.match(panel.querySelector('.chat-quote-when').textContent, /Main/);
        assert.equal(entry.classList.contains('user'), false, 'voices never mixed: the bubble stays Ouroboros\'s');
        const owners = f.bubble('user', 'mine');
        f.feed.append(owners);
        f.replies.decorate(owners, { role: 'user', relay: { text: 'not a relay row' } });
        assert.equal(f.quote(owners), null, 'only an Ouroboros row carries a relay');
    } finally { f.close(); }
});

test('the owner\'s echo adopts the saved row\'s address and with it Reply; Send as new prepares the composer', () => {
    const f = fixture();
    try {
        const echo = f.bubble('user', 'hello');
        echo.dataset.clientMessageId = 'cm-1';
        f.feed.append(echo);
        f.replies.decorate(echo, null);
        assert.equal(echo.querySelector('.chat-message-reply'), null, 'an unsaved echo has no address yet');
        assert.equal(f.replies.adopt({ role: 'user', client_message_id: 'cm-1', row_address: 'row:1@f#1' }), true);
        assert.equal(echo.dataset.rowAddress, 'row:1@f#1');
        assert.ok(echo.querySelector('.chat-message-reply'));
        assert.equal(f.replies.adopt({ role: 'user', client_message_id: 'cm-1', row_address: 'row:1@f#1' }), false, 'once');
        assert.equal(f.replies.adopt({ role: 'assistant', client_message_id: 'cm-1', row_address: 'x' }), false);
        f.input.value = 'my draft';
        const typed = [];
        f.input.addEventListener('input', () => typed.push(f.input.value));
        f.replies.compose('the lost words');
        assert.equal(f.input.value, 'my draft\n\nthe lost words', 'joins the draft, never replaces it');
        assert.deepEqual(typed, [f.input.value], 'the composer\'s own sizing and draft keeping hear it');
        assert.equal(f.doc.activeElement, f.input);
    } finally { f.close(); }
});

test('a work card offers Reply first and quotes the work by its task address', () => {
    const f = fixture();
    try {
        const actions = f.el('div', { className: 'chat-live-actions' });
        const root = f.el('div', { className: 'chat-live-card', dataset: { taskId: 't-9' } }, actions);
        f.feed.append(root);
        const record = { groupId: 't-9', root, footEl: null, footEndEl: null, titleEl: f.el('div', { text: 'Audit' }),
            activityEl: f.el('div', { text: 'Reading the third project' }), phaseEl: f.el('div', { text: 'Working' }) };
        const stop = f.el('button', { text: 'Stop…' });
        actions.append(stop);
        assert.equal(f.replies.syncCard(record, true), true);
        assert.deepEqual(actions.children.map((child) => child.textContent), ['Reply', 'Stop…']);
        assert.equal(record.cardReplyEl.getAttribute('aria-label'), 'Reply to this work');
        assert.equal(root.dataset.rowAddress, 't-9'.replace(/^/, 'task:'));
        record.cardReplyEl.click();
        assert.deepEqual(f.replies.frameField(), { reply_to: 'task:t-9' });
        const staged = JSON.parse(f.storage.get('ouro_chat_reply'));
        assert.equal(staged.kind, 'work_card');
        assert.equal(staged.author_label, 'Audit');
        assert.equal(staged.snippet, 'Reading the third project');
        assert.equal(f.replies.syncCard(record, false), true, 'not a root work card any more: no Reply');
        assert.equal(record.cardReplyEl, null);
    } finally { f.close(); }
});
