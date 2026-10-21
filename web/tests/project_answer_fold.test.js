import assert from 'node:assert/strict';
import test from 'node:test';
import { ElementStub, installDom, restoreDom } from './chat_dom_fixture.js';
import { decorateProjectRow } from '../modules/project_answer.js';

// A long Project answer in Main is folded when its text overflows, and a resize re-measures it. While a feed's
// width keeps moving the browser lays out only the rows near the screen (chat_reading_position.js marks the feed
// [data-resizing]); measuring a row it skips would lay that row out again on every frame of the move, so the
// answers in a moving feed wait, and are measured once the reading owner says the width held.
test('answer folds in a feed whose width still moves wait until the width holds', () => {
    const { prior, mount } = installDom();
    const listeners = new Map(), frames = [];
    globalThis.window.addEventListener = (type, fn) => listeners.set(type, [...(listeners.get(type) || []), fn]);
    globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    const fire = (type) => { for (const fn of listeners.get(type) || []) fn(); frames.splice(0).forEach((fn) => fn()); };
    try {
        const first = new ElementStub('div', globalThis.document);
        first.className = 'chat-bubble';
        first.append(Object.assign(new ElementStub('div', globalThis.document), { className: 'message' }));
        mount.append(first);
        decorateProjectRow(first, { role: 'assistant', projectId: 'project-1', projectName: 'Build' });
        frames.splice(0).forEach((fn) => fn());
        // Two answers on the page: one in a feed at rest, one in a feed whose width is moving.
        const measured = [];
        const answer = (id, moving) => {
            const classes = new Set(['chat-bubble', 'project-answer']);
            const message = { clientHeight: 100, get scrollHeight() { measured.push(id); return 300; }, querySelectorAll: () => [] };
            return { classList: { contains: (name) => classes.has(name), toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) },
                querySelector: (selector) => (selector === '.message' ? message : null),
                closest: (selector) => (selector === '[data-resizing]' && moving() ? {} : null), classes };
        };
        let moving = true;
        const rows = [answer('at rest', () => false), answer('moving', () => moving)];
        globalThis.document.querySelectorAll = (selector) => (selector === '.chat-bubble.project-answer' ? rows : []);

        fire('resize');
        assert.deepEqual(measured, ['at rest'], 'a resize measures only the answers in a feed at rest');
        assert.equal(rows[1].classes.has('is-folded'), false);

        moving = false;
        fire('ouro:feed-width-settled');
        assert.deepEqual(measured, ['at rest', 'at rest', 'moving'], 'once the width holds every answer is measured');
        assert.equal(rows[1].classes.has('is-folded'), true, 'and the one whose text overflows is folded');
    } finally {
        restoreDom(prior);
    }
});
