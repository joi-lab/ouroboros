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
