// A declarative extension settings form used to render the bare schema: every
// select showed its FIRST option and every text field was empty, so pressing
// Save posted that blank form over the stored values (the bundled Telegram
// skill unbound its owner chat id that way). The host now reads the current
// values from the same route it posts to; these pin the three outcomes of that
// read against the real renderer.
import assert from 'node:assert/strict';
import test from 'node:test';

import { readExtensionFormValues, renderExtensionSettingsSections } from '../modules/settings.js';

const UNREADABLE_NOTE = 'Current values could not be read; Save is disabled so it does not overwrite them. '
    + 'Use Reload Settings to retry.';

const FORM = {
    type: 'form',
    id: 'settings-form',
    route: 'save',
    submit_label: 'Save',
    fields: [
        {
            name: 'mode', label: 'Mode', type: 'select', default: 'safe',
            options: [{ label: 'Safe', value: 'safe' }, { label: 'Fast', value: 'fast' }],
        },
        { name: 'chat_id', label: 'Chat ID', type: 'text' },
        { name: 'token', label: 'Token', type: 'password' },
    ],
};

const sections = (component = FORM) => [{
    skill: 'demo', section_id: 'config', key: 'demo:config', title: 'Demo', render: { components: [component] },
}];

/** Minimal render target: the renderer only writes innerHTML and binds forms. */
function fakeHost() {
    const host = { innerHTML: '', querySelectorAll: () => [] };
    return { host, root: { querySelector: (sel) => (sel === '#extension-settings-sections' ? host : null) } };
}

function stubFetch(handler) {
    const calls = [];
    const prior = globalThis.fetch;
    globalThis.fetch = async (url, init) => {
        calls.push({ url, init });
        return handler(url, init);
    };
    return { calls, restore: () => { globalThis.fetch = prior; } };
}

const jsonResponse = (body, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
});

async function render(handler, { component = FORM, isCurrent } = {}) {
    const { host, root } = fakeHost();
    const fetcher = stubFetch(handler);
    try {
        await renderExtensionSettingsSections(root, sections(component), isCurrent ? { isCurrent } : undefined);
    } finally {
        fetcher.restore();
    }
    return { html: host.innerHTML, calls: fetcher.calls };
}

test('(a) a stored document hydrates the form, and a password never comes back', async () => {
    const { html, calls } = await render(() => jsonResponse({ mode: 'fast', chat_id: '42', token: 'leaked-secret' }));

    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/extensions/demo/save');
    assert.ok(!calls[0].init?.method, 'the hydration read is a plain GET on the POST route');
    assert.match(html, /<option value="fast" selected>Fast<\/option>/);
    assert.doesNotMatch(html, /<option value="safe" selected>/);
    assert.match(html, /name="chat_id"[^>]* value="42"/);
    // Passwords are never hydrated by renderSafeField; nothing may leak into the DOM.
    assert.doesNotMatch(html, /leaked-secret/);
    assert.match(html, /type="submit">Save<\/button>/);
    assert.doesNotMatch(html, /data-extension-settings-blocked/);
    assert.doesNotMatch(html, new RegExp(UNREADABLE_NOTE.slice(0, 30)));
});

test('(b) a plugin with no GET handler is unchanged: empty form, Save enabled', async () => {
    for (const status of [404, 405]) {
        const { html } = await render(() => jsonResponse({ error: 'nope' }, status));
        assert.match(html, /<option value="safe" selected>Safe<\/option>/, `status ${status}`);
        assert.match(html, /name="chat_id"[^>]* value=""/, `status ${status}`);
        assert.match(html, /type="submit">Save<\/button>/, `status ${status}`);
        assert.doesNotMatch(html, /data-extension-settings-blocked/, `status ${status}`);
    }
});

test('(c) an unreadable read disables Save and says why, in the shared warn tone', async () => {
    const unreadable = [
        ['5xx', () => jsonResponse({}, 500)],
        ['other 4xx', () => jsonResponse({}, 403)],
        ['non-JSON body', () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('not json'); } })],
        ['non-object body', () => jsonResponse([1, 2, 3])],
        ['network error', () => { throw new TypeError('Failed to fetch'); }],
    ];
    for (const [label, handler] of unreadable) {
        const { html } = await render(handler);
        assert.match(html, /data-extension-settings-blocked="1"/, label);
        assert.match(html, /type="submit" disabled>Save<\/button>/, label);
        assert.match(html, /data-extension-settings-status data-tone="warn">/, label);
        assert.ok(html.includes(UNREADABLE_NOTE), label);
        // The form still renders; only Save is withheld.
        assert.match(html, /<option value="safe" selected>Safe<\/option>/, label);
    }
});

test('a stale load never overwrites a newer render', async () => {
    const { html, calls } = await render(() => jsonResponse({ mode: 'fast' }), { isCurrent: () => false });
    assert.equal(calls.length, 1, 'the read still happens; only the write is withheld');
    assert.equal(html, '');
});

test('a component without a usable route is never read from', async () => {
    const { html, calls } = await render(() => jsonResponse({}), {
        component: { ...FORM, route: '../escape' },
    });
    assert.deepEqual(calls, []);
    assert.match(html, /Invalid extension settings route/);
});

test('readExtensionFormValues classifies every outcome of the read', async () => {
    const outcomes = [
        [jsonResponse({ mode: 'fast' }), { values: { mode: 'fast' }, blocked: false }],
        [jsonResponse({}, 404), { values: {}, blocked: false }],
        [jsonResponse({}, 405), { values: {}, blocked: false }],
        [jsonResponse({}, 500), { values: {}, blocked: true }],
        [jsonResponse(null), { values: {}, blocked: true }],
    ];
    for (const [response, expected] of outcomes) {
        const fetcher = stubFetch(() => response);
        try {
            assert.deepEqual(await readExtensionFormValues('demo', 'save'), expected);
        } finally {
            fetcher.restore();
        }
    }
});
