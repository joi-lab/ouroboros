// The word-cut fitter's one shared ResizeObserver (text_fit.js) keeps no removed element alive: an element that left the
// page leaves the observer on its last size report, while a connected one is re-fitted. Its own file: the observer is
// created once per module instance.
import assert from 'node:assert/strict';
import test from 'node:test';
import { installDom, restoreDom } from './chat_dom_fixture.js';

test('a removed element leaves the shared observer; a connected one stays observed', async () => {
    const env = installDom();
    const unobserved = [];
    let report = null;
    globalThis.ResizeObserver = class {
        constructor(callback) { report = callback; }
        observe() {}
        unobserve(el) { unobserved.push(el); }
    };
    try {
        const { fitText, fullText } = await import('../modules/text_fit.js');
        const kept = document.createElement('div');
        const gone = document.createElement('div');
        env.mount.append(kept);
        fitText(kept, 'kept words', 2);
        fitText(gone, 'gone words', 2);
        report([{ target: gone }, { target: kept }]);
        assert.deepEqual(unobserved, [gone], 'only the element that left the page');
        assert.equal(fullText(gone), 'gone words', 'its own text stays readable');
        assert.equal(fullText(kept), 'kept words');
    } finally { restoreDom(env.prior); }
});

test('a width change re-fits on the next frame, never inside the observer, so the observer cannot loop in one', async () => {
    const env = installDom();
    const frames = [];
    let report = null;
    const prior = { raf: globalThis.requestAnimationFrame, ro: globalThis.ResizeObserver, style: globalThis.getComputedStyle };
    globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    globalThis.getComputedStyle = () => ({ lineHeight: '20px', fontSize: '14px', whiteSpace: 'normal' });
    globalThis.ResizeObserver = class {
        constructor(callback) { report = callback; }
        observe() {}
        unobserve() {}
    };
    try {
        const { fitText } = await import(`../modules/text_fit.js?frame=${Date.now()}`);
        const el = document.createElement('div');
        env.mount.append(el);
        const fits = [];
        fitText(el, 'words that would be fitted', 1, (cut) => fits.push(cut));
        const initial = fits.length;
        Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => 120 });
        Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 20 });  // it fits whole
        report([{ target: el }]);
        report([{ target: el }]);
        assert.equal(fits.length, initial, 'nothing is fitted inside the observer\'s callback');
        assert.equal(frames.length, 1, 'one frame for every width change reported before it');
        frames.splice(0).forEach((fn) => fn());
        assert.equal(fits.length, initial + 1, 'the element is fitted once, on that frame');
    } finally {
        globalThis.requestAnimationFrame = prior.raf;
        globalThis.ResizeObserver = prior.ro;
        globalThis.getComputedStyle = prior.style;
        restoreDom(env.prior);
    }
});

test('the frame that re-fits tells its listeners which elements it fitted; a disposed listener hears nothing', async () => {
    const env = installDom();
    const frames = [];
    let report = null;
    const prior = { raf: globalThis.requestAnimationFrame, ro: globalThis.ResizeObserver, style: globalThis.getComputedStyle };
    globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    globalThis.getComputedStyle = () => ({ lineHeight: '20px', fontSize: '14px', whiteSpace: 'normal' });
    globalThis.ResizeObserver = class {
        constructor(callback) { report = callback; }
        observe() {}
        unobserve() {}
    };
    try {
        const { fitText, onRefit } = await import(`../modules/text_fit.js?heard=${Date.now()}`);
        const el = document.createElement('div');
        env.mount.append(el);
        fitText(el, 'words that would be fitted', 1);
        const heard = [];
        const off = onRefit((els) => heard.push(els));
        let width = 120;
        Object.defineProperty(el, 'clientWidth', { configurable: true, get: () => width });
        Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 20 });
        report([{ target: el }]);
        assert.deepEqual(heard, [], 'nothing is heard inside the observer\'s callback');
        frames.splice(0).forEach((fn) => fn());
        assert.deepEqual(heard, [[el]], 'the frame that fitted it says which elements it fitted');
        off();
        width = 140;
        report([{ target: el }]);
        frames.splice(0).forEach((fn) => fn());
        assert.equal(heard.length, 1, 'a disposed listener hears nothing');
    } finally {
        globalThis.requestAnimationFrame = prior.raf;
        globalThis.ResizeObserver = prior.ro;
        globalThis.getComputedStyle = prior.style;
        restoreDom(env.prior);
    }
});

// A page whose layout is counted: writing a text dirties it, the first read after a write lays it out (once for every
// element, as a document does). A line holds `width / 10` characters of 20 px.
function countedPage() {
    const state = { dirty: false, layouts: 0 };
    const element = (width) => {
        let text = '';
        return {
            isConnected: true, clientWidth: width,
            get textContent() { return text; },
            set textContent(value) { text = String(value); state.dirty = true; },
            get scrollHeight() {
                if (state.dirty) { state.dirty = false; state.layouts += 1; }
                return Math.max(1, Math.ceil(text.length / Math.floor(this.clientWidth / 10))) * 20;
            },
        };
    };
    return { state, element };
}

test('elements fitted together are fitted in lockstep: a batch costs a few layouts however many lines it holds', async () => {
    const env = installDom();
    const frames = [];
    let report = null;
    const prior = { raf: globalThis.requestAnimationFrame, ro: globalThis.ResizeObserver, style: globalThis.getComputedStyle };
    globalThis.requestAnimationFrame = (fn) => { frames.push(fn); return frames.length; };
    globalThis.getComputedStyle = () => ({ lineHeight: '20px', fontSize: '14px', whiteSpace: 'normal' });
    globalThis.ResizeObserver = class {
        constructor(callback) { report = callback; }
        observe() {}
        unobserve() {}
    };
    try {
        const { fitText } = await import(`../modules/text_fit.js?lockstep=${Date.now()}`);
        const page = countedPage();
        const words = (n) => Array.from({ length: n }, (_, i) => `word${i}`).join(' ');
        const lines = Array.from({ length: 40 }, (_, i) => {
            const el = page.element(200);
            fitText(el, `${words(30 + i)}.`, 2);
            return el;
        });
        const alone = page.element(150);
        fitText(alone, `${words(45)}.`, 2);
        // Every line is shown again narrower: one frame fits them all.
        for (const el of lines) el.clientWidth = 150;
        report(lines.map((target) => ({ target })));
        const before = page.state.layouts;
        frames.splice(0).forEach((fn) => fn());
        const batch = page.state.layouts - before;
        assert.ok(batch <= 12, `40 lines in ${batch} layouts (one by one they took several hundred)`);
        assert.equal(lines[15].textContent, alone.textContent, 'a line fitted in the batch reads as one fitted alone');
        for (const el of lines) assert.ok(el.scrollHeight <= 41 && el.textContent.endsWith('…'), el.textContent);
    } finally {
        globalThis.requestAnimationFrame = prior.raf;
        globalThis.ResizeObserver = prior.ro;
        globalThis.getComputedStyle = prior.style;
        restoreDom(env.prior);
    }
});
