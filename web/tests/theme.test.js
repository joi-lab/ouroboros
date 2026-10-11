import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../theme.js', import.meta.url), 'utf8');

/* A fake just wide enough for the controller: it renders its own buttons, so the
   harness has to hand back real child nodes rather than one stubbed element. */
function boot({ saved = null, storage = true, systemLight = false, media = true } = {}) {
    const handlers = new Map();
    const events = [];
    const root = { dataset: {} };
    const status = { textContent: '' };
    let host;

    const makeButton = () => {
        const classes = new Set();
        const el = {
            dataset: {}, attrs: {}, classes, tabIndex: null, textContent: '', type: '', focused: false,
            set className(value) { value.split(' ').forEach((name) => classes.add(name)); },
            get className() { return [...classes].join(' '); },
            setAttribute(key, value) { el.attrs[key] = value; },
            classList: { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) },
            focus() { el.focused = true; },
            closest: (selector) => (selector === '[data-theme-control]' ? host : el),
        };
        return el;
    };

    host = {
        children: [], attrs: {}, classes: new Set(),
        classList: { add: (...names) => names.forEach((name) => host.classes.add(name)) },
        setAttribute(key, value) { host.attrs[key] = value; },
        replaceChildren(...nodes) { host.children = nodes; },
        querySelector: (selector) => host.children.find((child) => child.dataset.themeChoice
            === (selector.match(/"(.*)"/)?.[1] ?? child.dataset.themeChoice)) || null,
        querySelectorAll: () => host.children,
    };

    const document = {
        documentElement: root,
        createElement: makeButton,
        querySelectorAll: (selector) => (selector === '[data-theme-control]' ? [host] : [status]),
        addEventListener: (key, fn) => handlers.set(key, fn),
        removeEventListener: (key) => handlers.delete(key),
    };
    const query = {
        matches: systemLight,
        addEventListener: (key, fn) => handlers.set(`media:${key}`, fn),
        removeEventListener: (key) => handlers.delete(`media:${key}`),
    };
    const window = {
        addEventListener: (key, fn) => handlers.set(key, fn),
        removeEventListener: (key) => handlers.delete(key),
        dispatchEvent: (event) => events.push(event),
        ...(media ? { matchMedia: () => query } : {}),
    };
    let store = saved;
    const localStorage = {
        getItem() { if (!storage) throw new Error('disabled'); return store; },
        setItem(key, value) { if (!storage) throw new Error('disabled'); store = value; },
    };
    vm.runInNewContext(source, {
        document, window, localStorage,
        CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    });
    const button = (value) => host.children.find((child) => child.dataset.themeChoice === value);
    return {
        root, host, status, handlers, events, query, window,
        saved: () => store,
        setSaved: (value) => { store = value; },
        buttons: () => host.children.map((child) => child.dataset.themeChoice),
        click: (value) => handlers.get('click')({ target: { closest: () => button(value) } }),
        key: (value, key) => handlers.get('keydown')({
            key, target: { closest: () => button(value) }, preventDefault() { this.prevented = true; },
        }),
        button,
    };
}

test('a new client defaults to System and follows the OS', () => {
    assert.equal(boot().root.dataset.theme, 'dark');
    assert.equal(boot().root.dataset.themeChoice, 'system');
    assert.equal(boot({ systemLight: true }).root.dataset.theme, 'light');
    // No OS signal at all is a real client, not a crash: System means Dark there.
    assert.equal(boot({ media: false }).root.dataset.theme, 'dark');
    assert.match(boot({ media: false }).status.textContent, /no OS appearance/);
});

test('an already saved explicit choice keeps its exact old meaning', () => {
    // The owner picked Light before Light/Dark/System existed; the upgrade must
    // not quietly re-read that as "follow the OS".
    const pinned = boot({ saved: 'light', systemLight: false });
    assert.equal(pinned.root.dataset.theme, 'light');
    assert.equal(pinned.root.dataset.themeChoice, 'light');
    assert.equal(boot({ saved: 'dark', systemLight: true }).root.dataset.theme, 'dark');
    assert.equal(boot({ saved: 'bogus' }).root.dataset.themeChoice, 'system');
});

test('the control renders Light / Dark / System and marks exactly one', () => {
    const s = boot({ saved: 'dark' });
    assert.deepEqual(s.buttons(), ['light', 'dark', 'system']);
    assert.equal(s.host.attrs.role, 'radiogroup');
    assert.deepEqual(s.host.children.map((b) => b.attrs['aria-checked']), ['false', 'true', 'false']);
    assert.deepEqual(s.host.children.map((b) => b.tabIndex), [-1, 0, -1]);
    assert.equal(s.host.children.every((b) => b.attrs.role === 'radio'), true);
});

test('choosing persists and repaints; arrows move through the group', () => {
    const s = boot({ saved: 'dark' });
    s.click('light');
    assert.equal(s.saved(), 'light');
    assert.equal(s.root.dataset.theme, 'light');
    assert.equal(s.events.length, 1);
    s.key('light', 'ArrowRight');
    assert.equal(s.saved(), 'dark');
    assert.equal(s.button('dark').focused, true);
    s.key('dark', 'End');
    assert.equal(s.saved(), 'system');
    assert.equal(boot({ saved: s.saved() }).root.dataset.themeChoice, 'system');
});

test('a choice that repaints nothing does not churn mounted views', () => {
    // Dark -> System on a dark OS changes the stored choice and the pressed
    // button, but not one pixel — charts and diagrams must not be told to redraw.
    const s = boot({ saved: 'dark', systemLight: false });
    s.click('system');
    assert.equal(s.saved(), 'system');
    assert.equal(s.root.dataset.themeChoice, 'system');
    assert.equal(s.root.dataset.theme, 'dark');
    assert.equal(s.events.length, 0);
    assert.equal(s.button('system').attrs['aria-checked'], 'true');
});

test('System tracks a later OS switch, a pinned choice ignores it', () => {
    const following = boot({ saved: 'system', systemLight: false });
    following.query.matches = true;
    following.handlers.get('media:change')();
    assert.equal(following.root.dataset.theme, 'light');
    assert.equal(following.events.length, 1);
    const pinned = boot({ saved: 'dark', systemLight: false });
    pinned.query.matches = true;
    pinned.handlers.get('media:change')();
    assert.equal(pinned.root.dataset.theme, 'dark');
    assert.equal(pinned.events.length, 0);
});

test('unavailable storage stays usable and says so', () => {
    const s = boot({ storage: false });
    s.click('light');
    assert.equal(s.root.dataset.theme, 'light');
    assert.match(s.status.textContent, /until this window reloads/);
});

test('another window of the same client, and lifecycle cleanup', () => {
    const s = boot({ saved: 'dark' });
    // The handler re-reads storage rather than trusting event.newValue, so a
    // sibling window's clear() is covered by the same path as its setItem().
    s.setSaved('light');
    s.handlers.get('storage')({ key: 'ouroboros.theme' });
    assert.equal(s.root.dataset.theme, 'light');
    assert.equal(s.button('light').attrs['aria-checked'], 'true');
    s.setSaved(null);
    s.handlers.get('storage')({ key: 'ouroboros.theme' });
    assert.equal(s.root.dataset.themeChoice, 'system');
    // A bfcache-restorable hide must not tear the controller down.
    s.handlers.get('pagehide')({ persisted: true });
    assert.ok(s.handlers.has('click'));
    s.handlers.get('pagehide')({ persisted: false });
    assert.equal(s.handlers.size, 0);
});

test('mount() paints a control injected after boot', () => {
    // Settings is rendered long after theme.js ran, so the published mounter is
    // the only thing that can fill its control.
    const s = boot({ saved: 'light' });
    s.host.children = [];
    assert.equal(s.buttons().length, 0);
    s.window.ouroTheme.mount();
    assert.deepEqual(s.buttons(), ['light', 'dark', 'system']);
    assert.equal(s.button('light').attrs['aria-checked'], 'true');
});

test('both first-party documents load the controller before CSS', () => {
    for (const name of ['index.html', 'onboarding_template.html']) {
        const html = fs.readFileSync(new URL(`../${name}`, import.meta.url), 'utf8');
        assert.ok(html.indexOf('/static/theme.js') < html.indexOf('/static/ui.css'));
    }
    const index = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8');
    // The sidebar button moved to Settings -> Appearance; onboarding keeps a
    // control because it runs before Settings exists.
    assert.doesNotMatch(index, /data-theme-toggle|data-theme-control/);
    assert.match(fs.readFileSync(new URL('../onboarding_template.html', import.meta.url), 'utf8'), /data-theme-control/);
    assert.match(fs.readFileSync(new URL('../modules/settings_ui.js', import.meta.url), 'utf8'), /data-theme-control/);
});
