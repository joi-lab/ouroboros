/** One stack of layers over the room (docs/DESIGN.md "Shell", plan P2).
 *
 * A layer is anything that lies over the page and is dismissed as one thing: the phone drawer and the Project panel.
 * Dialogs, menus and the Shade keep their own focus traps and Escape. Every way of leaving ends in the same place:
 * Escape, the scrim, × call `pop()`, which closes exactly one layer, the top one, through the layer's own `onClose`;
 * the browser's or Android's Back and Telegram's Back call `back()`, which first gives an open dialog over the layer
 * its Escape (the dialog is what is on top) and otherwise pops. The owner of a layer's
 * state keeps owning it: it calls `push` when it opens and `remove` when it closes (both idempotent), and `onClose` is
 * where the stack asks the owner to close.
 *
 * History: while at least one layer is open there is exactly one same-URL history entry (`pushState`), so Back
 * closes a layer before it leaves the app; `popstate` pops one layer; closing the last layer programmatically
 * consumes that entry (`history.back()`) and the popstate it causes is swallowed. The entry is reconciled once per
 * task, so "close the drawer, open the Project" never leaves a stray entry (the b2 mockup's finding). No URL is ever
 * written: the desktop shell and the Telegram mini app have no address bar (tests/test_navigation_shell_static.py).
 *
 * Inert: a modal layer makes the rest of the shell inert (siblings along its ancestor path inside the shell's
 * container, never `<body>`'s other children, where dialogs, menus and toasts opened over the layer live; what the page
 * had already made inert is left alone); a non-modal layer (the docked Project panel) leaves the sidebar live. Focus goes
 * into the layer on open and returns to the opener on close. Every listener has a disposer. */

const LAYER_STATE = { ouroLayers: 1 };

export function createLayerStack({ win = globalThis.window, doc = globalThis.document, history = win?.history } = {}) {
    const stack = [];
    const listeners = new Set();
    const inerted = new Set();
    const hist = { armed: false, skip: 0, queued: false };
    let disposed = false;

    const top = () => stack[stack.length - 1] || null;
    const has = (id) => stack.some((layer) => layer.id === id);
    const get = (id) => stack.find((layer) => layer.id === id) || null;

    function emit() {
        const snapshot = { size: stack.length, top: top()?.id || null, ids: stack.map((layer) => layer.id) };
        listeners.forEach((fn) => { try { fn(snapshot); } catch (err) { console.error('layers: listener failed', err); } });
    }

    // ---- inert: the topmost modal layer's siblings along its path up to the shell's container, and everything below it
    function syncInert() {
        for (const el of inerted) el.inert = false;
        inerted.clear();
        const modalIndex = stack.map((layer) => layer.modal).lastIndexOf(true);
        if (modalIndex < 0 || !doc?.body) return;
        const modal = stack[modalIndex];
        const above = new Set(stack.slice(modalIndex + 1).map((layer) => layer.el).filter(Boolean));
        for (const el of stack.slice(modalIndex).flatMap((layer) => layer.live || [])) above.add(el);
        const keep = (node) => node === modal.el || [...above].some((el) => el === node || node.contains?.(el) || el.contains?.(node));
        let node = modal.el;
        while (node?.parentElement && node.parentElement !== doc.body) {
            for (const sibling of node.parentElement.children) {
                if (sibling === node || keep(sibling) || sibling.inert || sibling.tagName === 'SCRIPT') continue;
                sibling.inert = true;
                inerted.add(sibling);
            }
            node = node.parentElement;
        }
    }

    // ---- history: one entry while the stack is not empty, reconciled once per task
    function reconcileHistory() {
        if (hist.queued || !history || typeof history.pushState !== 'function') return;
        hist.queued = true;
        Promise.resolve().then(() => {
            hist.queued = false;
            if (disposed) return;
            try {
                if (stack.length && !hist.armed) {
                    history.pushState(LAYER_STATE, '');
                    hist.armed = true;
                } else if (!stack.length && hist.armed) {
                    hist.armed = false;
                    hist.skip += 1;
                    history.back();
                }
            } catch {
                hist.skip = 0;  // a sandboxed document: layers still work, Back does not reach them
            }
        });
    }
    const onPopState = () => {
        if (hist.skip > 0) { hist.skip -= 1; return; }
        hist.armed = false;
        if (stack.length) back('history');
    };

    // ---- Escape closes the top layer unless something inside already took the key (a menu, the Shade, a dialog)
    const onKeydown = (event) => {
        if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || !stack.length) return;
        if (doc?.querySelector?.('dialog[open]')) return;  // a native modal dialog owns Escape until it adapts
        event.preventDefault();
        pop('escape');
    };

    function focusInto(layer) {
        if (layer.focus === false) return;
        if (typeof layer.focus === 'function') { layer.focus(); return; }
        const el = layer.el;
        if (!el || typeof el.focus !== 'function') return;
        if (!el.hasAttribute?.('tabindex')) el.tabIndex = -1;
        try { el.focus({ preventScroll: true }); } catch { /* detached */ }
    }
    function restoreFocus(layer) {
        const opener = layer.opener;
        if (!opener || opener.isConnected === false || typeof opener.focus !== 'function') return;
        if (opener.closest?.('[inert]')) return;
        try { opener.focus({ preventScroll: true }); } catch { /* gone */ }
    }

    function settle() {
        syncInert();
        reconcileHistory();
        emit();
    }

    /** Open: `{ id, el, modal = true, onClose(reason), opener = activeElement, focus, live }` (`live`: elements
     * beside the layer that stay usable under a modal one, its scrim). A known id keeps its
     * place in the stack and takes the new facts (an owner that syncs its state calls push on every sync). */
    function push(layer) {
        if (disposed || !layer || !layer.id) return false;
        const existing = get(layer.id);
        if (existing) {
            Object.assign(existing, layer);
            settle();
            return true;
        }
        const entry = {
            modal: true,
            opener: doc?.activeElement && doc.activeElement !== doc.body ? doc.activeElement : null,
            ...layer,
        };
        stack.push(entry);
        settle();
        focusInto(entry);
        return true;
    }

    /** The owner closed a layer itself (state already updated): drop it, restore focus, reconcile history. */
    function remove(id) {
        const index = stack.findIndex((layer) => layer.id === id);
        if (index < 0) return false;
        const [layer] = stack.splice(index, 1);
        settle();
        restoreFocus(layer);
        return true;
    }

    /** THE dismiss: exactly one layer, the top one, through its owner's onClose. */
    function pop(reason = 'close') {
        const layer = top();
        if (!layer) return false;
        try { layer.onClose?.(reason); } finally {
            if (has(layer.id)) remove(layer.id);  // an owner that did not call remove
        }
        return true;
    }

    /** Back (system, browser, Telegram): a dialog the page opened over the layers is on top, so it takes the press
     * as its Escape (the dialogs close on Escape through ui_interactions.bindDialogFocus; a bare native dialog that
     * nothing answered closes as its own Escape would close it) and the layer stays; otherwise the top layer closes. */
    function back(reason = 'history') {
        const dialogs = [...(doc?.querySelectorAll?.('dialog[open], [aria-modal="true"]') || [])]
            .filter((el) => el.getClientRects?.().length && !stack.some((layer) => layer.el === el));
        const dialog = dialogs[dialogs.length - 1];
        if (!dialog || !win?.KeyboardEvent) return pop(reason);
        const target = dialog.contains(doc.activeElement) ? doc.activeElement : dialog;
        const escape = new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
        target.dispatchEvent(escape);
        if (!escape.defaultPrevented && dialog.tagName === 'DIALOG') dialog.close?.();
        reconcileHistory();
        return true;
    }

    function onChange(fn) {
        listeners.add(fn);
        return () => listeners.delete(fn);
    }

    win?.addEventListener?.('popstate', onPopState);
    win?.addEventListener?.('keydown', onKeydown);

    function destroy() {
        disposed = true;
        win?.removeEventListener?.('popstate', onPopState);
        win?.removeEventListener?.('keydown', onKeydown);
        for (const el of inerted) el.inert = false;
        inerted.clear();
        listeners.clear();
        stack.length = 0;
    }

    return {
        push, remove, pop, back, onChange, destroy,
        top: () => top()?.id || null,
        has,
        size: () => stack.length,
        ids: () => stack.map((layer) => layer.id),
        /** The history sentinel state, for tests and the host adapter. */
        historyArmed: () => hist.armed,
    };
}
