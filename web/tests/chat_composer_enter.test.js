// The Main and Project composer is a message field (docs/DESIGN.md "Controls and editable
// choices"): Enter presses Send, so the key takes Send's own path (Swarm, pending upload,
// empty text); Shift+Enter is a line break, and a composing or held Enter sends nothing.
import assert from 'node:assert/strict';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

function composer({ chatId = 1, projectId = '' } = {}) {
    const env = installDom(async (url) => ({ ok: true, json: async () =>
        String(url).startsWith('/api/chat/history') ? { messages: [], window: { complete: true } }
            : { active_chat_activities: [], active_chat_activities_complete: true, supervisor_ready: true } }));
    const frames = [];
    let generation = 0;
    const instance = createChatInstance({
        ws: { on: () => () => {}, isConnected: () => true,
            send: (frame) => { frames.push(frame); return { status: 'sent', clientMessageId: `cm-${frames.length}` }; } },
        state: { activePage: 'chat', projectChatIds: new Set(chatId === 1 ? [] : [chatId]), unreadCount: 0 },
        updateUnreadBadge() {}, chatId, projectId, idPrefix: 'chat', mountEl: env.mount, asPanel: chatId !== 1,
        stateSnapshots: { begin: () => ({ generation: ++generation, requestedAt: Date.now() }),
            gate() { return Promise.resolve(this.begin()); },
            isCurrent: () => true, apply() {}, fail() {}, latest: () => null },
    });
    const byId = (suffix) => document.byId.get(`chat-${suffix}`);
    const input = byId('input');
    const press = (init = {}) => {
        const event = { key: 'Enter', target: input, preventDefault() { event.defaultPrevented = true; }, ...init };
        for (const listener of input.listeners.get('keydown')) listener(event);
        return event;
    };
    return { frames, input, byId, press, close() { instance.destroy(); restoreDom(env.prior); } };
}

for (const [room, options] of [['Main', {}], ['a Project', { chatId: 7, projectId: 'p7' }]]) {
    test(`${room} composer: Enter sends through Send, Shift+Enter and composition keep the draft`, () => {
        const c = composer(options);
        try {
            assert.equal(c.input.enterKeyHint, 'send');
            c.input.value = 'first line\nsecond line';
            assert.equal(c.press({ shiftKey: true }).defaultPrevented, undefined, 'Shift+Enter is the line break');
            assert.equal(c.press({ isComposing: true }).defaultPrevented, undefined);
            assert.equal(c.press({ keyCode: 229 }).defaultPrevented, undefined, 'WebKit commits IME this way');
            assert.deepEqual([c.frames.length, c.input.value], [0, 'first line\nsecond line']);
            assert.equal(c.press().defaultPrevented, true);
            assert.equal(c.frames.length, 1);
            assert.equal(c.frames[0].content, 'first line\nsecond line');
            assert.equal(c.frames[0].project_id, options.projectId, 'the room the button would send to');
            assert.equal(c.input.value, '');
            assert.equal(c.press({ repeat: true }).defaultPrevented, true, 'a held key adds no line breaks');
            c.press();
            assert.equal(c.frames.length, 1, 'an empty composer sends nothing');
        } finally { c.close(); }
    });
}

test('Enter follows Send: Swarm arms the next message, the existing chords send, a busy Send holds the draft', () => {
    const c = composer();
    try {
        c.byId('swarm').click();
        c.input.value = 'plan the migration';
        c.press({ metaKey: true });
        assert.equal(c.frames[0].force_plan, true);
        assert.equal(c.byId('swarm').dataset.armed, 'false', 'one-shot, exactly as a click on Send');
        c.input.value = 'and report back';
        c.press({ ctrlKey: true });
        assert.equal(c.frames[1].force_plan, false);
        c.byId('send').disabled = true;
        c.input.value = 'while uploading';
        assert.equal(c.press().defaultPrevented, true);
        assert.deepEqual([c.frames.length, c.input.value], [2, 'while uploading']);
    } finally { c.close(); }
});
