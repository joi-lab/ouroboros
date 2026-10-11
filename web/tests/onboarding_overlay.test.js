import assert from 'node:assert/strict';
import test from 'node:test';

import { escapeHtmlAttr } from '../modules/utils.js';
import { FRAME_SANDBOX, OVERLAY_ID, initOnboardingOverlay } from '../modules/onboarding_overlay.js';

// --- Minimal DOM ----------------------------------------------------------
// Enough of the shape the overlay actually uses: element creation, attributes,
// child lists, removal, and attribute/class selectors. Nothing is parsed from
// innerHTML, because the paths under test build their nodes with createElement.

function makeElement(tag) {
    const el = {
        tagName: String(tag).toUpperCase(),
        children: [],
        attributes: {},
        parentElement: null,
        listeners: {},
        className: '',
        id: '',
        innerHTML: '',
        textContent: '',
        setAttribute(name, value) { this.attributes[name] = String(value); },
        getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; },
        appendChild(child) {
            child.parentElement = this;
            this.children.push(child);
            return child;
        },
        remove() {
            const parent = this.parentElement;
            if (!parent) return;
            parent.children = parent.children.filter((node) => node !== this);
            this.parentElement = null;
        },
        addEventListener(name, handler) {
            (this.listeners[name] = this.listeners[name] || []).push(handler);
        },
        matches(selector) {
            if (selector.startsWith('[') && selector.endsWith(']')) {
                return selector.slice(1, -1) in this.attributes;
            }
            if (selector.startsWith('.')) return this.className.split(/\s+/).includes(selector.slice(1));
            return this.tagName === selector.toUpperCase();
        },
        querySelector(selector) {
            for (const child of this.children) {
                if (child.matches(selector)) return child;
                const found = child.querySelector(selector);
                if (found) return found;
            }
            return null;
        },
        click() { (this.listeners.click || []).forEach((handler) => handler()); },
    };
    return el;
}

function installDom() {
    const body = makeElement('body');
    const previous = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch };
    globalThis.document = {
        body,
        createElement: makeElement,
        getElementById(id) {
            const walk = (node) => {
                for (const child of node.children) {
                    if (child.id === id) return child;
                    const found = walk(child);
                    if (found) return found;
                }
                return null;
            };
            return walk(body);
        },
    };
    globalThis.window = {
        location: { origin: 'http://127.0.0.1:8765', reload() {}, replace() {} },
        addEventListener() {},
    };
    return {
        body,
        restore() {
            globalThis.document = previous.document;
            globalThis.window = previous.window;
            globalThis.fetch = previous.fetch;
        },
    };
}

function overlayIn(body) {
    return body.children.find((node) => node.id === OVERLAY_ID) || null;
}

// --- The blocking surface -------------------------------------------------

test('a readiness transport failure leaves a blocking overlay with a retry, not a bare console error', async () => {
    const dom = installDom();
    const errors = [];
    const previousError = console.error;
    console.error = (...args) => errors.push(args);
    try {
        globalThis.fetch = async () => { throw new Error('Failed to fetch'); };

        await initOnboardingOverlay();

        const overlay = overlayIn(dom.body);
        // The reported defect: overlay_elements_created === 0, so after a
        // cancelled desktop wizard (or a headless first run) the ordinary UI was
        // exposed with no onboarding and no way back to it.
        assert.ok(overlay, 'the blocking overlay must still be mounted');
        assert.equal(overlay.getAttribute('aria-modal'), 'true');
        const retry = overlay.querySelector('[data-onboarding-retry]');
        assert.ok(retry, 'a failed readiness check must offer a retry');
        assert.ok(overlay.querySelector('.onboarding-overlay-backdrop'), 'the scrim comes with the answer');
        assert.equal(overlay.querySelector('.onboarding-frame'), null);
        assert.equal(errors.length, 1, 'the failure is still logged');
    } finally {
        console.error = previousError;
        dom.restore();
    }
});

test('retrying after a failure reaches the wizard once the probe answers', async () => {
    const dom = installDom();
    const previousError = console.error;
    console.error = () => {};
    try {
        let attempt = 0;
        globalThis.fetch = async () => {
            attempt += 1;
            if (attempt === 1) throw new Error('Failed to fetch');
            return { status: 200, ok: true };
        };

        await initOnboardingOverlay();
        const overlay = overlayIn(dom.body);
        overlay.querySelector('[data-onboarding-retry]').click();
        await new Promise((resolve) => setImmediate(resolve));

        const frame = overlay.querySelector('.onboarding-frame');
        assert.ok(frame, 'a successful retry mounts the wizard frame');
        assert.equal(frame.src, '/onboarding');
        assert.equal(overlay.querySelector('[data-onboarding-retry]'), null);
    } finally {
        console.error = previousError;
        dom.restore();
    }
});

test('only a verified 204 takes the blocking overlay down, and it never dims a configured app', async () => {
    const dom = installDom();
    let scrimSeen = false;
    try {
        globalThis.fetch = async () => {
            // Observed mid-probe: the shell is already up (nothing can slip past
            // it) but the dark scrim has not been painted, so a configured
            // install does not flash a dimmed screen on every load.
            const overlay = overlayIn(dom.body);
            assert.ok(overlay, 'the shell exists before the answer');
            scrimSeen = Boolean(overlay.querySelector('.onboarding-overlay-backdrop'));
            return { status: 204, ok: true };
        };

        await initOnboardingOverlay();

        assert.equal(scrimSeen, false);
        assert.equal(overlayIn(dom.body), null, '204 means configured: no overlay is due');
    } finally {
        dom.restore();
    }
});

test('a non-2xx readiness answer blocks too, instead of falling through to the app', async () => {
    const dom = installDom();
    const previousError = console.error;
    console.error = () => {};
    try {
        globalThis.fetch = async () => ({ status: 503, ok: false });

        await initOnboardingOverlay();

        const overlay = overlayIn(dom.body);
        assert.ok(overlay);
        assert.ok(overlay.querySelector('[data-onboarding-retry]'));
    } finally {
        console.error = previousError;
        dom.restore();
    }
});

test('an unconfigured install still frames the one onboarding host', async () => {
    const dom = installDom();
    try {
        globalThis.fetch = async () => ({ status: 200, ok: true });

        await initOnboardingOverlay();

        const frame = overlayIn(dom.body).querySelector('.onboarding-frame');
        assert.equal(frame.src, '/onboarding');
        // The policy itself is owned by FRAME_SANDBOX (the login step's
        // popup capability is decided there, on the completion branch); this
        // pins only that the frame is actually sandboxed by it.
        assert.equal(frame.getAttribute('sandbox'), FRAME_SANDBOX);
        assert.ok(FRAME_SANDBOX.includes('allow-scripts'));
    } finally {
        dom.restore();
    }
});

// --- The shared escape helper the onboarding modules now import ------------

test('the shared escape helper both onboarding modules import neutralizes every HTML meta', () => {
    assert.equal(
        escapeHtmlAttr(`&<>"'\``),
        '&amp;&lt;&gt;&quot;&#39;&#96;',
    );
    // Ampersand first: any other order double-encodes the entity numbers.
    assert.equal(escapeHtmlAttr('&lt;'), '&amp;lt;');
    assert.equal(escapeHtmlAttr(null), '');
});
