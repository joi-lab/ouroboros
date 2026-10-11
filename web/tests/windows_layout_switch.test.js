import assert from 'node:assert/strict';
import test from 'node:test';

import { installAltMenuSuppression, shouldSuppressWindowsAltMenu } from '../modules/ui_helpers.js';

test('Windows layout switch: standalone Alt suppresses menu activation when focus is in textarea or input', () => {
    const inputEl = { tagName: 'INPUT', isContentEditable: false };
    const textareaEl = { tagName: 'TEXTAREA', isContentEditable: false };
    const contentEditableEl = { tagName: 'DIV', isContentEditable: true };

    // Alt key events during layout switch
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', ctrlKey: false }, textareaEl), true);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltRight', ctrlKey: false }, inputEl), true);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', shiftKey: true, ctrlKey: false }, textareaEl), true);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', ctrlKey: false }, contentEditableEl), true);
});

test('Windows layout switch: AltGr (Ctrl+Alt) is preserved and NOT suppressed', () => {
    const textareaEl = { tagName: 'TEXTAREA', isContentEditable: false };

    // AltGr in browsers fires with ctrlKey=true and altKey=true (key='Alt' or 'AltGraph', code='AltRight')
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltRight', ctrlKey: true, altKey: true }, textareaEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'AltGraph', code: 'AltRight', ctrlKey: true, altKey: true }, textareaEl), false);
    // Firefox reports AltGr WITHOUT ctrlKey — excluded by key name, composition untouched
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'AltGraph', code: 'AltRight', ctrlKey: false, altKey: true }, textareaEl), false);
});

test('Windows layout switch: regular keys, shortcuts, and Enter/Shift+Enter are NOT suppressed', () => {
    const textareaEl = { tagName: 'TEXTAREA', isContentEditable: false };

    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Enter', code: 'Enter', ctrlKey: false, altKey: false }, textareaEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Enter', code: 'Enter', shiftKey: true, ctrlKey: false, altKey: false }, textareaEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'c', code: 'KeyC', ctrlKey: true, altKey: false }, textareaEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'v', code: 'KeyV', ctrlKey: true, altKey: false }, textareaEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'a', code: 'KeyA', ctrlKey: false, altKey: false }, textareaEl), false);
});

test('Windows layout switch: Alt when focus is outside editable elements is NOT suppressed', () => {
    const buttonEl = { tagName: 'BUTTON', isContentEditable: false };
    const selectEl = { tagName: 'SELECT', isContentEditable: false };
    const bodyEl = { tagName: 'BODY', isContentEditable: false };

    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', ctrlKey: false }, buttonEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', ctrlKey: false }, selectEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', ctrlKey: false }, bodyEl), false);
    assert.equal(shouldSuppressWindowsAltMenu({ key: 'Alt', code: 'AltLeft', ctrlKey: false }, null), false);
    assert.equal(shouldSuppressWindowsAltMenu(null, bodyEl), false);
});

test('installAltMenuSuppression guards keydown AND keyup in capture phase on the given window', () => {
    const listeners = {};
    const fakeWindow = {
        document: { activeElement: { tagName: 'TEXTAREA', isContentEditable: false } },
        addEventListener(type, fn, opts) { listeners[type] = { fn, opts }; },
    };
    installAltMenuSuppression(fakeWindow);

    // Firefox-style hosts activate the menu on Alt keyup, Chromium on keydown —
    // the same guard must sit on both phases, in capture, on the target window.
    assert.deepEqual(Object.keys(listeners).sort(), ['keydown', 'keyup']);
    for (const type of ['keydown', 'keyup']) {
        assert.equal(listeners[type].opts.capture, true);
        let prevented = 0;
        listeners[type].fn({ key: 'Alt', code: 'AltLeft', ctrlKey: false, preventDefault() { prevented += 1; } });
        assert.equal(prevented, 1, `standalone Alt ${type} must be suppressed`);
        listeners[type].fn({ key: 'Enter', code: 'Enter', ctrlKey: false, preventDefault() { prevented += 1; } });
        assert.equal(prevented, 1, `Enter ${type} must pass through`);
    }

    // Focus outside an editable element leaves the default action alone.
    fakeWindow.document.activeElement = { tagName: 'BODY', isContentEditable: false };
    let prevented = 0;
    listeners.keydown.fn({ key: 'Alt', code: 'AltLeft', ctrlKey: false, preventDefault() { prevented += 1; } });
    assert.equal(prevented, 0);
});
