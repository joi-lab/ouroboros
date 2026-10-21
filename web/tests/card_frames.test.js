// Card frames (card_frames.js): an app runs in an opaque frame built from its text — sandbox allow-scripts only, a
// policy with no network, the host's head first — and talks only through a nonce-bound bridge whose submit fills the
// owner's draft. Frames in a message follow the inline size policy: 120 px up to 70 % of the viewport, Expand beyond.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
    CARD_APP_CSP, CARD_APP_SANDBOX, CARD_STATE_MAX_BYTES, INLINE_FRAME_FLOOR, cardAppSrcdoc, cardSubmitLine,
    createCardAppBridge, createInlineFit, inlineFrameHeight, mountCardApp,
} from '../modules/card_frames.js';
import { WIDGET_FRAME_MAX_HEIGHT } from '../modules/widget_module.js';
import { installCardDom } from './helpers/card_dom.js';

const THEME = { name: 'light', css: ':root { color-scheme: light; }' };

test('the app document puts the host head first: the no-network policy, the theme, the bridge, then the app', () => {
    const doc = cardAppSrcdoc('<!doctype html><html><head><script>go()</script></head><body>App</body></html>',
        { nonce: 'n-1', theme: THEME, saved: '{"note":"</script><b>"}' });
    assert.equal(CARD_APP_SANDBOX, 'allow-scripts');
    assert.equal(CARD_APP_CSP, "default-src 'none'; script-src 'unsafe-inline' blob: data:; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:");
    assert.doesNotMatch(CARD_APP_CSP, /connect-src|https?:|'self'/, 'no network and no origin at all');
    const head = doc.slice(0, doc.indexOf('</head>'));
    assert.ok(head.startsWith(`<!doctype html><html data-theme="light"><head><meta http-equiv="Content-Security-Policy" content="${CARD_APP_CSP}">`));
    assert.ok(head.includes('<style id="ouro-card-theme">:root { color-scheme: light; }</style>'));
    assert.ok(head.indexOf('window.OuroborosCard') > 0, 'the bridge is in the head');
    assert.ok(doc.indexOf('go()') > doc.indexOf('</head>'), 'the app comes after the host head');
    assert.equal(head.match(/<\/script/gi).length, 1, 'saved state cannot close the bridge script');
    assert.ok(head.includes('"n-1"'));
});

test('the bridge acts only on this frame\'s messages carrying this mount\'s nonce, within its bounds', () => {
    const frame = {};
    const calls = [];
    const handle = createCardAppBridge({
        nonce: 'n-2', source: () => frame,
        resize: (h) => calls.push(['resize', h]), save: (text) => calls.push(['save', text]), submit: (v, s) => calls.push(['submit', v, s]),
    });
    const from = (data, source = frame) => handle({ source, data });
    assert.equal(from({ type: 'ouro-card-resize', height: 300, nonce: 'n-2' }, {}), false, 'another window');
    assert.equal(from({ type: 'ouro-card-resize', height: 300, nonce: 'other' }), false, 'a foreign nonce');
    assert.equal(from({ type: 'ouro-card-resize', height: 300 }), false, 'no nonce');
    assert.equal(from({ type: 'ouro-card-resize', height: -4, nonce: 'n-2' }), false);
    assert.equal(from({ type: 'ouro-card-resize', height: 'tall', nonce: 'n-2' }), false);
    assert.equal(from({ type: 'ouro-card-resize', height: 300, nonce: 'n-2' }), true);
    assert.equal(from({ type: 'ouro-card-state', state: JSON.stringify('x'.repeat(CARD_STATE_MAX_BYTES)), nonce: 'n-2' }), false, 'over 32 KiB');
    assert.equal(from({ type: 'ouro-card-state', state: '{broken', nonce: 'n-2' }), false, 'not JSON');
    assert.equal(from({ type: 'ouro-card-state', state: '{"step":2}', nonce: 'n-2' }), true);
    assert.equal(from({ type: 'ouro-card-submit', value: { total: 42 }, summary: 'Total 42', nonce: 'n-2' }), true);
    assert.equal(from({ type: 'ouro-card-fetch', url: '/api/state', nonce: 'n-2' }), false, 'three messages, nothing else');
    assert.deepEqual(calls, [['resize', 300], ['save', '{"step":2}'], ['submit', { total: 42 }, 'Total 42']]);
});

test('a submit becomes one draft line: the title, the summary and the value as compact JSON when it is small', () => {
    assert.equal(cardSubmitLine('Loan', { rate: 4.5, years: 30 }, 'Monthly payment\n 1 520'), 'From «Loan»: Monthly payment 1 520 {"rate":4.5,"years":30}');
    assert.equal(cardSubmitLine('Loan', 'Plan B', 'Plan B'), 'From «Loan»: Plan B', 'a value that repeats the summary is not said twice');
    assert.equal(cardSubmitLine('Loan', { rows: 'x'.repeat(400) }, 'Big table'), 'From «Loan»: Big table', 'a large value stays out');
    assert.equal(cardSubmitLine('Loan', null, ''), 'From «Loan»: (no summary)');
    assert.doesNotMatch(cardSubmitLine('A\nB', { a: 'line\nbreak' }, 'x'), /\n/, 'one line');
});

test('a frame in a message is at least 120 px, at most 70 % of the viewport until Expand, then the frame ceiling', () => {
    assert.equal(inlineFrameHeight(10, { viewport: 800 }), INLINE_FRAME_FLOOR);
    assert.equal(inlineFrameHeight(400, { viewport: 800 }), 400);
    assert.equal(inlineFrameHeight(2000, { viewport: 800 }), 560);
    assert.equal(inlineFrameHeight(2000, { viewport: 800, expanded: true }), 2000);
    assert.equal(inlineFrameHeight(99999, { viewport: 800, expanded: true }), WIDGET_FRAME_MAX_HEIGHT);
    assert.equal(inlineFrameHeight(Number.NaN, { viewport: 800 }), INLINE_FRAME_FLOOR);
    assert.equal(inlineFrameHeight(150, { viewport: 100 }), INLINE_FRAME_FLOOR, 'a tiny window still keeps the floor');
});

test('Expand shows only while the frame would cut its content and switches the height both ways', () => {
    const dom = installCardDom({ innerHeight: 800 });
    try {
        const host = document.createElement('div');
        const toggle = document.createElement('button');
        const fit = createInlineFit(host, toggle);
        fit.fit(300);
        assert.equal(host.style.getPropertyValue('--widget-frame-height'), '300px');
        assert.equal(toggle.hidden, true);
        fit.fit(1500);
        assert.equal(host.style.getPropertyValue('--widget-frame-height'), '560px');
        assert.equal(toggle.hidden, false);
        assert.equal(toggle.textContent, 'Expand');
        toggle.click();
        assert.equal(host.style.getPropertyValue('--widget-frame-height'), '1500px');
        assert.equal(toggle.textContent, 'Collapse');
        assert.equal(toggle.getAttribute('aria-expanded'), 'true');
        toggle.click();
        assert.equal(host.style.getPropertyValue('--widget-frame-height'), '560px');
        fit.dispose();
        toggle.click();
        assert.equal(host.style.getPropertyValue('--widget-frame-height'), '560px', 'disposed: the toggle no longer acts');
    } finally {
        dom.restore();
    }
});

test('a mounted app: an opaque frame, its state kept per card and app, its submit a draft line, never a send', () => {
    const dom = installCardDom();
    try {
        const host = document.createElement('div');
        const lines = [];
        const fitted = [];
        const fit = { fit: (h) => fitted.push(h) };
        sessionStorage.setItem('ouro_card_state:card_e:abc', '{"step":3}');
        const dispose = mountCardApp(host, { cardId: 'card_e', title: 'Quiz', sha256: 'abc', html: '<p>Hi</p>', fit, onSubmit: (line) => lines.push(line) });
        const frame = host.querySelector('iframe');
        assert.equal(frame.getAttribute('sandbox'), 'allow-scripts');
        assert.equal(frame.hasAttribute('allow'), false);
        assert.equal(frame.getAttribute('title'), 'Quiz');
        assert.match(frame.srcdoc, /let saved = "\{\\"step\\":3\}";/, 'the saved state is handed back at mount');
        const nonce = frame.srcdoc.match(/const nonce = "([^"]+)"/)[1];
        frame.contentWindow = {};
        const post = (data) => window.emit('message', { source: frame.contentWindow, data: { ...data, nonce } });
        post({ type: 'ouro-card-resize', height: 333 });
        post({ type: 'ouro-card-state', state: '{"step":4}' });
        post({ type: 'ouro-card-submit', value: 7, summary: 'Score' });
        window.emit('message', { source: {}, data: { type: 'ouro-card-submit', value: 1, summary: 'forged', nonce } });
        assert.deepEqual(fitted, [333]);
        assert.equal(sessionStorage.getItem('ouro_card_state:card_e:abc'), '{"step":4}');
        assert.deepEqual(lines, ['From «Quiz»: Score 7']);
        dispose();
        assert.equal(host.querySelector('iframe'), null);
        assert.equal(window.listeners('message'), 0, 'the bridge listener goes with the frame');
    } finally {
        dom.restore();
    }
});

test('an app that navigates its frame away is removed at its second document', () => {
    const dom = installCardDom();
    try {
        const host = document.createElement('div');
        let left = 0;
        mountCardApp(host, { cardId: 'card_n', title: 'Nav', sha256: 'n', html: '', fit: { fit() {} }, onSubmit() {}, onLeave: () => { left += 1; } });
        const frame = host.querySelector('iframe');
        frame.dispatch('load');
        assert.equal(host.querySelector('iframe'), frame, 'its own document loading is fine');
        assert.equal(left, 0);
        frame.dispatch('load');
        assert.equal(host.querySelector('iframe'), null, 'a second document: the frame is gone');
        assert.equal(left, 1);
        assert.equal(window.listeners('message'), 0);
    } finally {
        dom.restore();
    }
});
