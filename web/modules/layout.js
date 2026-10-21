/** The shell's layout facts, read through one door.
 *
 * `web/theme.js` owns the switch: it runs before CSS, so the first paint is already the
 * right shell, writes `html[data-layout="docked"|"stack"]`, keeps it current on resize and
 * rotation, and publishes its number once as `window.ouroLayout`. Everything else
 * (app.js, client_surface.js, the layer stack, the drawer drag) asks this module and never
 * writes a media query of its own; the shell's CSS keys off the attribute
 * (tests/test_shell_layout_static.py). */

export const LAYOUTS = Object.freeze(['docked', 'stack']);

const win = () => globalThis.window ?? globalThis;
const rootOf = (doc = globalThis.document) => doc?.documentElement ?? null;

/** The published number and the current layout; theme.js must have run. */
export function layoutFacts(w = win()) {
    const facts = w?.ouroLayout;
    if (!facts) throw new Error('web/theme.js has not published window.ouroLayout');
    return facts;
}

/** `docked` or `stack`: the attribute theme.js painted, else what it resolves right now; a document with
 * neither (a test's fake DOM) is a window, so docked, as theme.js itself falls back. */
export function currentLayout({ root = rootOf(), w = win() } = {}) {
    const painted = root?.dataset?.layout;
    if (LAYOUTS.includes(painted)) return painted;
    const resolved = w?.ouroLayout?.layout;
    return LAYOUTS.includes(resolved) ? resolved : 'docked';
}

export function isStackLayout(options) {
    return currentLayout(options) === 'stack';
}

/** Hear the switch move; returns the disposer. The handler gets the new layout name. */
export function onLayoutChange(handler, { w = win() } = {}) {
    const listener = (event) => handler(event?.detail?.layout || currentLayout({ w }));
    w.addEventListener('ouro:layout-changed', listener);
    return () => w.removeEventListener('ouro:layout-changed', listener);
}
