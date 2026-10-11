import assert from 'node:assert/strict';
import test from 'node:test';
import { createLoginCardController } from '../modules/harness_login_cards.js';
import { createClaudexorStatusStore } from '../modules/claudexor_status_store.js';
import { installDesktopShellLinkInterceptor } from '../modules/ui_helpers.js';

const json = (body) => ({ ok: true, status: 200, json: async () => body });
const URL = 'https://example.test/signin';

async function consumer(t, hostKind) {
    const calls = [], listeners = new Map(), docListeners = new Map();
    const parent = hostKind === 'framed-telegram'
        ? { Telegram: { WebApp: { openLink: (url) => calls.push(['telegram', url]) } } } : null;
    const win = { parent, location: { href: 'http://127.0.0.1:8765/' }, addEventListener() {},
        open: (...args) => { calls.push(['browser', ...args]); return null; } };
    if (hostKind === 'telegram') win.Telegram = { WebApp: { openLink: (url) => calls.push(['telegram', url]) } };
    if (hostKind === 'desktop') win.pywebview = { api: { open_external_url: (url) => {
        calls.push(['desktop', url]); return Promise.resolve({ ok: true });
    } } };
    const doc = { defaultView: win, hidden: false, documentElement: { dataset: {} },
        addEventListener(type, fn) { docListeners.set(type, fn); }, removeEventListener() {} };
    const host = { innerHTML: '', contains: () => false, querySelectorAll: () => [],
        querySelector(selector) {
            const marker = selector.match(/\[([^\]]+)\]/)?.[1];
            if (!marker || !this.innerHTML.includes(marker)) return null;
            return { addEventListener(type, fn) { listeners.set(`${selector}:${type}`, fn); } };
        } };
    const store = createClaudexorStatusStore({ doc, fetchImpl: async () => json({
        daemon: { state: 'running', engine_version: 'fixture', runtime: {} }, config_dir: '/fixture',
        harnesses: [{ id: 'codex' }], profiles: { profiles: [],
            harnessAccounts: [{ harness_id: 'codex', native_login_detected: false }] }, quota: [],
    }) });
    const ctl = createLoginCardController({ host, store, doc, fetchImpl: async () => json({
        job_id: 'fixture', job: { state: 'running', phase: 'awaiting_user' },
        deviceCode: { flow: 'device_code', verificationUrl: URL, userCode: 'TEST-CODE' },
    }) });
    t.after(() => { ctl.detach(); store.dispose(); });
    installDesktopShellLinkInterceptor({ win, doc });
    await ctl.start('codex', '');
    assert.ok(host.innerHTML.includes('data-open-signin'));
    const click = (extra = {}) => {
        const anchor = { hasAttribute: () => false,
            getAttribute: (name) => ({ target: '_blank', href: URL })[name] };
        const event = { button: 0, defaultPrevented: false,
            target: { closest: () => anchor }, preventDefault() { this.defaultPrevented = true; }, ...extra };
        listeners.get('[data-open-signin]:click')(event);
        docListeners.get('click')?.(event);
        return event;
    };
    return { calls, click };
}

for (const hostKind of ['browser', 'desktop', 'telegram', 'framed-telegram']) {
    test(`sign-in consumer opens once in ${hostKind} during the click`, async (t) => {
        const { calls, click } = await consumer(t, hostKind);
        assert.equal(click().defaultPrevented, true);
        assert.equal(calls.length, 1, 'no deferred call and no duplicate from the document interceptor');
        assert.deepEqual(calls[0].slice(0, 2), [hostKind.includes('telegram') ? 'telegram' : hostKind, URL]);
    });
}

test('sign-in consumer retains native browser modifiers and already-handled clicks', async (t) => {
    const { calls, click } = await consumer(t, 'browser');
    for (const extra of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
        assert.equal(click(extra).defaultPrevented, false);
    }
    assert.equal(click({ defaultPrevented: true }).defaultPrevented, true);
    assert.deepEqual(calls, []);
    assert.equal(click({ detail: 0 }).defaultPrevented, true, 'keyboard activation is an ordinary handled click');
    assert.equal(calls.length, 1);
});
