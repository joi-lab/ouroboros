import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

import { DESK, createInterfaceSize, mountInterfaceSize, sizeNote, stepped, zoomKey } from '../modules/interface_size.js';

const tick = () => new Promise((done) => setTimeout(done, 0));

function fakeWindow({ bridge = null, viewport = null, top = true, telegram = false, mac = true } = {}) {
    const listeners = new Map();
    const win = {
        navigator: { platform: mac ? 'MacIntel' : 'Win32', userAgent: '' },
        performance: { timeOrigin: 1000 },
        addEventListener(type, fn) { listeners.set(type, fn); },
        removeEventListener(type) { listeners.delete(type); },
        fire(type) { listeners.get(type)?.(); },
        ouroViewportScale: viewport,
        Telegram: telegram ? { WebApp: { initData: 'x' } } : undefined,
    };
    win.top = top ? win : {};
    if (bridge) win.pywebview = { api: bridge };
    return win;
}

function fakeDocument() {
    const listeners = new Map();
    return {
        documentElement: { dataset: {} },
        addEventListener(type, fn) { listeners.set(type, fn); },
        removeEventListener(type) { listeners.delete(type); },
        key(event) { listeners.get('keydown')?.(event); },
    };
}

function desktopBridge({ supported = true, percent = 100 } = {}) {
    const calls = [];
    return {
        calls,
        shell_info: async () => ({ interface_size: { supported, min: 50, max: 200, step: 5, percent } }),
        set_interface_size: async (value, page, sequence) => {
            calls.push({ value, page, sequence });
            return { ok: true, percent: value };
        },
    };
}

test('a zoom key is the physical key with the platform modifier, never with Alt', () => {
    assert.equal(zoomKey({ metaKey: true, code: 'Equal' }, { mac: true }), 'in');
    assert.equal(zoomKey({ metaKey: true, code: 'NumpadSubtract' }, { mac: true }), 'out');
    assert.equal(zoomKey({ metaKey: true, code: 'Digit0' }, { mac: true }), 'reset');
    assert.equal(zoomKey({ ctrlKey: true, code: 'Minus' }, { mac: false }), 'out');
    assert.equal(zoomKey({ ctrlKey: true, code: 'Equal' }, { mac: true }), '', 'Ctrl on a Mac is not the zoom key');
    assert.equal(zoomKey({ metaKey: true, altKey: true, code: 'Equal' }, { mac: true }), '');
    assert.equal(zoomKey({ metaKey: true, code: 'KeyA' }, { mac: true }), '');
    // A German keyboard: + and - sit where a US keyboard has ] and /; a French one types à on the 0 key.
    assert.equal(zoomKey({ metaKey: true, key: '+', code: 'BracketRight' }, { mac: true }), 'in');
    assert.equal(zoomKey({ metaKey: true, key: '-', code: 'Slash' }, { mac: true }), 'out');
    assert.equal(zoomKey({ ctrlKey: true, key: 'à', code: 'Digit0' }, { mac: false }), 'reset');
});

test('the keys step to the next whole ten inside the range', () => {
    assert.equal(stepped(100, 1), 110);
    assert.equal(stepped(105, 1), 110);
    assert.equal(stepped(105, -1), 100);
    assert.equal(stepped(100, -1), 90);
    assert.equal(stepped(DESK.max, 1), DESK.max);
    assert.equal(stepped(DESK.min, -1), DESK.min);
});

test('the desktop app is the size: its bridge answers, the slider and the keys set it there', async () => {
    const bridge = desktopBridge({ percent: 120 });
    const win = fakeWindow({ bridge });
    const doc = fakeDocument();
    const size = createInterfaceSize({ win, doc });
    await tick();
    assert.deepEqual(size.state(), { route: 'app', percent: 120, min: 50, max: 200, step: 5 });
    await size.set(133);
    assert.equal(bridge.calls.at(-1).value, 135, 'a 5 % grid');
    let prevented = false;
    doc.key({ metaKey: true, code: 'Equal', preventDefault: () => { prevented = true; } });
    await tick();
    assert.ok(prevented);
    assert.equal(bridge.calls.at(-1).value, 140);
    assert.ok(bridge.calls.at(-1).sequence > bridge.calls.at(-2).sequence, 'each call carries its order');
    doc.key({ metaKey: true, code: 'Digit0', preventDefault() {} });
    await tick();
    assert.equal(size.state().percent, 100);
    size.dispose();
});

test('quick key presses step on from each other, and a late answer never moves the size back', async () => {
    const answers = [];
    const calls = [];
    const bridge = {
        shell_info: async () => ({ interface_size: { supported: true, min: 50, max: 200, step: 5, percent: 100 } }),
        set_interface_size: (value) => new Promise((done) => { calls.push(value); answers.push(() => done({ ok: true, percent: value })); }),
    };
    const win = fakeWindow({ bridge });
    const doc = fakeDocument();
    const size = createInterfaceSize({ win, doc });
    await tick();
    const first = size.step(1);
    const second = size.step(1);
    assert.deepEqual(calls, [110, 120], 'the second press steps from the first, not from the answer it has not had');
    answers[1]();
    await second;
    answers[0]();  // the older call's answer arrives last
    await first;
    assert.equal(size.state().percent, 120);
    size.dispose();
});

test('a desktop app older than the bridge says it cannot change its size, and its keys stay the system\'s', async () => {
    const bridge = { shell_info: async () => ({}) };  // a launcher built before set_interface_size
    const win = fakeWindow({ bridge });
    const doc = fakeDocument();
    const size = createInterfaceSize({ win, doc });
    win.fire('pywebviewready');
    await tick();
    assert.equal(size.state().route, 'none');
    assert.equal(size.state().reason, 'app');
    let prevented = false;
    doc.key({ metaKey: true, code: 'Equal', preventDefault: () => { prevented = true; } });
    assert.equal(prevented, false);
    size.dispose();
});

test('a window whose engine has no page zoom says so, without promising a restart', async () => {
    const bridge = desktopBridge({ supported: false });  // Linux Qt, macOS before 11, the legacy Windows control
    const win = fakeWindow({ bridge });
    const size = createInterfaceSize({ win, doc: fakeDocument() });
    await tick();
    assert.deepEqual(size.state(), { route: 'none', reason: 'engine', percent: 100 });
    assert.doesNotMatch(sizeNote(size.state())[0], /Restart/);
    size.dispose();
});

test('a touch screen sets the size through the viewport; a browser keeps its own zoom; Telegram says so', async () => {
    let applied = null;
    const viewport = { state: 'active', percent: 100, min: 75, max: 120, step: 5,
        set: async (value) => { applied = value; viewport.percent = value; return 'active'; }, subscribe: () => () => {} };
    const touch = createInterfaceSize({ win: fakeWindow({ viewport }), doc: fakeDocument() });
    assert.equal(touch.state().route, 'screen');
    await touch.set(110);
    assert.equal(applied, 110);
    assert.equal(touch.state().percent, 110);

    const browser = createInterfaceSize({ win: fakeWindow({ viewport: { state: 'unavailable' } }), doc: fakeDocument() });
    assert.equal(browser.state().route, 'browser');
    assert.equal((await browser.set(150)).percent, 100, 'a page cannot set a browser\'s zoom');

    const telegram = createInterfaceSize({ win: fakeWindow({ viewport: { state: 'unavailable' }, telegram: true }), doc: fakeDocument() });
    assert.deepEqual(telegram.state(), { route: 'none', reason: 'telegram', percent: 100 });
});

test('the note names this surface\'s way and keys', () => {
    assert.match(sizeNote({ route: 'app' }, { mac: true })[1].zoomIn, /⌘ \+/);
    assert.match(sizeNote({ route: 'browser' }, { mac: false })[1].zoomOut, /Ctrl −/);
    assert.match(sizeNote({ route: 'app' })[0], /\{zoomIn\}, \{zoomOut\} and \{reset\}/, 'the "and" is the template\'s, so it is translated');
    assert.deepEqual(sizeNote({ route: 'screen', min: 75, max: 120 })[1], { min: 75, max: 120 });
    assert.match(sizeNote({ route: 'none', reason: 'telegram' })[0], /Telegram/);
    assert.doesNotMatch(sizeNote({ route: 'browser', touch: true })[0], /zoomIn|Ctrl|⌘/, 'a phone has no zoom keys to name');
});

function fakeBlock() {
    const handlers = {};
    const element = (extra = {}) => ({ hidden: false, disabled: false, textContent: '', dataset: {}, ...extra,
        addEventListener(type, fn) { handlers[`${extra.name}:${type}`] = fn; }, removeEventListener() {} });
    const row = element({ name: 'row' });
    const input = element({ name: 'input', min: '', max: '', step: '', value: '' });
    const output = element({ name: 'output' });
    const reset = element({ name: 'reset' });
    const note = element({ name: 'note' });
    const map = { '[data-interface-size-row]': row, '[data-interface-size-input]': input,
        '[data-interface-size-value]': output, '[data-interface-size-reset]': reset, '[data-interface-size-note]': note };
    const block = { querySelector: (selector) => map[selector] };
    const root = { querySelector: (selector) => (selector === '[data-interface-size]' ? block : null) };
    return { root, row, input, output, reset, note, handlers };
}

test('the slider shows the value while it moves and applies it only when let go', async () => {
    const bridge = desktopBridge({ percent: 100 });
    const win = fakeWindow({ bridge });
    const size = createInterfaceSize({ win, doc: fakeDocument() });
    await tick();
    const ui = fakeBlock();
    const dispose = mountInterfaceSize(ui.root, size, { win });
    assert.equal(ui.row.hidden, false);
    assert.equal(ui.input.min, '50');
    assert.equal(ui.input.max, '200');
    assert.equal(ui.reset.disabled, true, 'nothing to reset at 100 %');
    ui.input.value = '80';
    ui.handlers['input:input']();
    assert.equal(ui.output.textContent, '80%');
    assert.equal(bridge.calls.length, 0, 'no size while the slider moves');
    ui.handlers['input:change']();
    await tick();
    assert.equal(bridge.calls.at(-1).value, 80);
    assert.equal(ui.reset.disabled, false);
    dispose();
});

test('where the page cannot set the size the slider is hidden and the note says why', () => {
    const win = fakeWindow({ viewport: { state: 'unavailable' }, telegram: true });
    const size = createInterfaceSize({ win, doc: fakeDocument() });
    const ui = fakeBlock();
    mountInterfaceSize(ui.root, size, { win });
    assert.equal(ui.row.hidden, true);
    assert.match(ui.note.textContent, /Telegram/);
});

// theme.js's touch half, run as the page runs it: before CSS, with the viewport meta it rewrites.
const until = async (ok, ms = 2000) => {
    const end = Date.now() + ms;
    while (!ok() && Date.now() < end) await new Promise((done) => setTimeout(done, 5));
};

function bootViewport({ width = 390, stored = null, honoured = true, coarse = true, relativeScale = false,
    storage: shared = null, agent = 'phone-test', slowFrames = 0 } = {}) {
    let reports = relativeScale ? 'pinch' : 'page';  // what the visual viewport's scale is: a pinch's, or the page's
    let pinch = 1;
    let content = 'width=device-width, initial-scale=1.0, viewport-fit=cover';
    let layoutWidth = width;
    let writes = 0;
    let lagging = false;  // a rotation still under way: the host lays out a new meta only when it settles
    const contents = [];
    const attrs = new Set();
    let frames = 0;
    let layoutAt = 0;  // a slow first layout: the frame by which the host has laid the meta out
    const storage = shared || new Map(stored ? [['ouroboros.interface-size', String(stored)]] : []);
    const meta = {
        getAttribute: () => content,
        setAttribute: (_key, value) => {
            writes += 1;
            content = value;
            contents.push(value);
            if (slowFrames && !layoutAt) layoutAt = frames + slowFrames;
            else if (!lagging) layOut();
        },
    };
    // The screen shows the whole layout width at the page's scale (Chromium, WebKit); an engine that reported only a
    // pinch's own scale would make the layout width read as the screen's.
    const visualViewport = {
        get width() { return pinch === 1 ? layoutWidth : Math.round(layoutWidth / pinch); },  // whole px, as iOS reports
        get scale() { return (reports === 'pinch' ? 1 : width / layoutWidth) * pinch; },
    };
    const props = {};
    const listeners = new Map();
    const doc = {
        querySelector: (selector) => (selector === 'meta[name="viewport"]' ? meta : null),
        querySelectorAll: () => [],
        documentElement: { get clientWidth() { return layoutWidth; }, style: { setProperty: (k, v) => { props[k] = v; } }, dataset: {},
            toggleAttribute: (name, on) => { if (on) attrs.add(name); else attrs.delete(name); } },
        addEventListener() {}, removeEventListener() {},
    };
    const turns = [];
    const win = {
        matchMedia: (query) => ({ matches: query.includes('coarse') ? coarse : false,
            addEventListener: (type, fn) => { if (query.includes('orientation')) turns.push(fn); } }),
        addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener() {},
        get innerWidth() { return layoutWidth; },
        visualViewport,
    };
    win.top = win;
    const context = {
        window: win, document: doc,
        localStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: (k) => storage.delete(k) },
        requestAnimationFrame: (fn) => setTimeout(() => {
            frames += 1;
            if (layoutAt && frames >= layoutAt) { layoutAt = -1; layOut(); }
            fn();
        }, 0),
        CustomEvent: class {}, console, navigator: { userAgent: agent },
    };
    vm.runInNewContext(fs.readFileSync(new URL('../theme.js', import.meta.url), 'utf8'), context);
    const resize = (next) => {  // the screen's width changes and the window says so
        width = next;
        if (!/width=\d+/.test(content)) layoutWidth = next;
        listeners.get('resize')?.();
    };
    function layOut() {
        const asked = /width=(\d+)/.exec(content);
        layoutWidth = honoured && asked ? Number(asked[1]) : width;
    }
    const lag = (on) => { lagging = on; if (!on) { layOut(); listeners.get('resize')?.(); } };
    const rotate = (next) => {  // a turn: the screen's width changes and the orientation query says so
        width = next;
        if (!/width=\d+/.test(content)) layoutWidth = next;
        for (const fn of turns) fn();
    };
    const magnify = (factor) => { pinch = factor; listeners.get('resize')?.(); };  // a toolbar resize while pinched
    const report = (kind) => { reports = kind; };
    return { scale: win.ouroViewportScale, content: () => content, props, storage, resize, lag, rotate, contents, attrs,
        magnify, report, writes: () => writes };
}

test('a phone lays the page out at another width and keeps the size the host honours', async () => {
    const phone = bootViewport({ width: 390 });
    assert.equal(phone.scale.state, 'active');
    assert.equal(phone.scale.max, 120, '390 / 320 CSS px allows up to 120 %');
    assert.equal(await phone.scale.set(125), 'active');
    assert.equal(phone.scale.percent, 120, 'clamped to the width floor');
    assert.match(phone.content(), /^width=325, initial-scale=1.2,/);
    assert.equal(phone.props['--interface-scale'], '1.2');
    assert.equal(phone.storage.get('ouroboros.interface-size'), '120');
    await phone.scale.set(80);
    assert.match(phone.content(), /^width=488, initial-scale=0.79918,/, 'the screen shows all 488 px, not 487.5 of them');
    assert.equal(phone.props['--interface-scale'], '0.79918');
    await phone.scale.set(60);
    assert.equal(phone.scale.percent, 75, 'no smaller than 75 % under a finger');
});

test('a split screen keeps its orientation: a new screen width is measured again and the choice comes back', async () => {
    const phone = bootViewport({ width: 390 });
    await phone.scale.set(120);
    phone.resize(360);  // narrower than 120 % allows (360 / 320 = 112 %)
    await until(() => /^width=327,/.test(phone.content()) && phone.scale.percent === 110);
    assert.equal(phone.scale.percent, 110, 'the largest size the narrower screen allows');
    assert.match(phone.content(), /^width=327,/);
    phone.resize(390);
    await until(() => phone.scale.percent === 120);
    assert.match(phone.content(), /^width=325, initial-scale=1.2,/, 'the owner\'s choice, not the narrow screen\'s');
    assert.equal(phone.storage.get('ouroboros.interface-size'), '120');
});

test('a resize while a size is still being laid out is read once it is', async () => {
    const phone = bootViewport({ width: 390 });
    const setting = phone.scale.set(120);
    phone.resize(360);  // before the 120 % write has settled
    await setting;
    await until(() => /^width=327,/.test(phone.content()) && phone.scale.percent === 110);
    assert.match(phone.content(), /^width=327,/);
    assert.equal(phone.scale.percent, 110);
});

test('an engine whose visual viewport moves with the page is not watched: only a rotation measures there', async () => {
    const phone = bootViewport({ width: 390, relativeScale: true });
    await phone.scale.set(120);
    const writes = phone.writes();
    for (let i = 0; i < 5; i += 1) phone.resize(390);
    await new Promise((done) => setTimeout(done, 30));
    assert.equal(phone.writes(), writes, 'no measuring loop');
    assert.equal(phone.scale.percent, 120);
    assert.match(phone.content(), /^width=325,/);
});

test('a rotation keeps the size: a layout still moving is no refusal, and the page never flashes to 100 %', async () => {
    const phone = bootViewport({ width: 390 });
    await phone.scale.set(120);
    const before = phone.contents.length;
    phone.lag(true);
    phone.resize(844);  // landscape: the host lays out the new meta only once the turn has settled
    await until(() => /^width=703,/.test(phone.content()));
    await new Promise((done) => setTimeout(done, 30));
    assert.equal(phone.scale.state, 'active', 'a moving layout is not a host that ignores the meta');
    assert.equal(phone.scale.percent, 120);
    phone.lag(false);
    await until(() => phone.scale.max > 120);
    assert.match(phone.content(), /^width=703, initial-scale=1.200569,/);
    assert.ok(phone.contents.slice(before).every((value) => !value.startsWith('width=device-width')), 'no flash to 100 %');
    assert.equal(phone.storage.get('ouroboros.interface-size'), '120');
});

test('a slow first layout is no refusal: the page looks once more before it gives the size up', async () => {
    const phone = bootViewport({ width: 390, slowFrames: 3 });  // laid out on the third frame, one past the first look
    assert.equal(await phone.scale.set(120), 'active');
    assert.equal(phone.scale.percent, 120);
    assert.equal(phone.storage.get('ouroboros.interface-size-refused'), undefined, 'nothing is remembered as a refusal');
});

test('a turn right after the slider is let go keeps the new choice', async () => {
    const phone = bootViewport({ width: 390 });
    const setting = phone.scale.set(120);
    phone.rotate(844);  // before the 120 % write has settled
    await setting;
    await until(() => /^width=703,/.test(phone.content()) && phone.scale.percent === 120);
    assert.equal(phone.storage.get('ouroboros.interface-size'), '120', 'the choice, not the size the turn found');
    assert.match(phone.content(), /^width=703, initial-scale=1.200569,/);
});

test('only a page laid out wider than its screen turns text inflation off', async () => {
    const phone = bootViewport({ width: 390 });
    await phone.scale.set(80);
    assert.ok(phone.attrs.has('data-interface-smaller'));
    await phone.scale.set(100);
    assert.ok(!phone.attrs.has('data-interface-smaller'), 'at 100 % the phone\'s own text settings stand');
});

test('a reading that turns out to be the layout\'s is measured once, then no longer watched', async () => {
    const phone = bootViewport({ width: 390 });
    await phone.scale.set(120);  // read as the page's scale at the first size, so it is watched
    phone.report('pinch');  // then the engine shows it reports a pinch's scale only
    phone.resize(390);  // the reading equals the layout: measured at 100 %, the screen had not moved
    await until(() => phone.scale.percent === 120 && /^width=325,/.test(phone.content()));
    await new Promise((done) => setTimeout(done, 30));
    const writes = phone.writes();
    for (let i = 0; i < 5; i += 1) phone.resize(390);
    await new Promise((done) => setTimeout(done, 30));
    assert.equal(phone.writes(), writes, 'no further writes and no loop');
    assert.match(phone.content(), /^width=325,/);
});

test('a resize to a screen as wide as the layout is still a new screen', async () => {
    const tablet = bootViewport({ width: 1024 });
    await tablet.scale.set(125);  // laid out at 819 px
    for (const [screen, layout] of [[820, 656], [768, 614], [900, 720]]) {
        tablet.resize(screen);  // 820 reads as the layout's 819: measured, and it is a new screen
        await until(() => new RegExp(`^width=${layout},`).test(tablet.content()) && tablet.scale.percent === 125);
        assert.match(tablet.content(), new RegExp(`^width=${layout},`), `${screen} px at 125 %`);
    }
});

test('a resize deep in a pinch leaves the size alone', async () => {
    const phone = bootViewport({ width: 390 });
    await phone.scale.set(80);  // 488 px laid out
    const writes = phone.writes();
    phone.magnify(488 / 80.5);  // iOS reports 81 px visible: read as a screen, 392.4 px, more than the 2 px it allows
    await new Promise((done) => setTimeout(done, 30));
    assert.equal(phone.writes(), writes);
    assert.equal(phone.scale.percent, 80);
});

test('a stored size is applied before the first layout; a host that ignores the meta keeps 100 %', async () => {
    const stored = bootViewport({ width: 400, stored: 110 });
    await until(() => stored.scale.state !== 'pending');
    assert.equal(stored.scale.percent, 110);
    assert.match(stored.content(), /^width=364, initial-scale=1.098901,/, 'the exact ratio of the two widths');

    const ignored = bootViewport({ width: 390, stored: 110, honoured: false });  // a Mini App in Telegram for Android
    await until(() => ignored.scale.state !== 'pending');
    assert.equal(ignored.scale.state, 'unavailable');
    assert.equal(ignored.scale.percent, 100);
    assert.match(ignored.content(), /^width=device-width/, 'the original meta is back');

    const desk = bootViewport({ coarse: false });
    assert.equal(desk.scale.state, 'unavailable', 'a fine pointer keeps the browser\'s own zoom');
});

test('a host that ignored the meta is not offered the size again, until its user agent changes', async () => {
    const storage = new Map();
    const first = bootViewport({ storage, honoured: false, agent: 'Telegram-Android/11.0' });
    assert.equal(first.scale.state, 'active', 'nothing is known before the first try');
    assert.equal(await first.scale.set(120), 'unavailable');
    const again = bootViewport({ storage, honoured: false, agent: 'Telegram-Android/11.0' });
    assert.equal(again.scale.state, 'unavailable', 'the next start knows');
    const updated = bootViewport({ storage, honoured: true, agent: 'Telegram-Android/11.1' });
    assert.equal(updated.scale.state, 'active', 'an updated host is tried again');
});
