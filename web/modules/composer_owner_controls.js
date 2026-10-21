// Each composer's saved room settings: Context and Effort, two inline capsules.
// Main and Projects read their own authoritative values and save through the owner
// endpoints. Swarm stays a per-message flag. (DESIGN: Composer owner controls.)
import { fmt } from './i18n.js';
import {
    EFFORT_OPTIONS, EFFORT_RANGE_DEFAULT, OWNER_TIERS, effortText, normalizeEffortRange, ownerLevelIndex,
} from './effort_levels.js';
import { beginMorph, stopMorph } from './capsule_morph.js';
import { haptic } from './haptics.js';
import { createComposerFold } from './composer_fold.js';

const N = OWNER_TIERS.length - 1;
const HANDLES = ['min', 'rec', 'max'];
const CONTEXT_MODES = ['nano', 'low', 'max'];
const CONTEXT_LABELS = Object.freeze({ nano: 'Nano', low: 'Low', max: 'Max' });
const CONTEXT_TITLE = 'Context mode (owner setting). Nano is the compact owner window, Low fits ~250K / local models, Max is full. Saves for new participants in this room; running work keeps its context.';
const contextModeOf = (mode) => (['nano', 'low', 'max'].includes(mode) ? mode : 'max');
const HOVER_OPEN_MS = 160;
// After the mouse leaves, the open strip waits this long before it closes, and a margin
// around it counts as still being there (a forgiving hover zone, like a menu's).
const HOVER_CLOSE_MS = 1000;
const HOVER_ZONE_PX = 32;
const HOVER_MEDIA = '(hover: hover) and (pointer: fine)';
// An inline strip collapses in 0.4 s (style.css); the closed capsule's width has settled by then.
const COLLAPSE_MS = 450;
// The segments' side padding (`--capsule-seg-pad`, px): up to var(--space-2) when there is room
// (Context's three words take a little more air), never below var(--space-1) beside the other
// controls, down to 3 px when the strip stands alone (from 4 px down the pill's corners tighten
// with the padding, so the word's box stays inside it: `data-tight`). Below that the strip
// scrolls inside itself.
const SEG_PAD = Object.freeze({ max: 8, beside: 4, alone: 3 });
// The fade at a scrolling strip's edge (var(--space-3)): what the owner moved stays clear of it.
const REVEAL_MARGIN_PX = 12;
const clamp = (value, lo, hi) => Math.min(hi, Math.max(lo, value));
const px = (value) => Number.parseFloat(value) || 0;
// Fractional widths: whole-pixel offsets lose up to a pixel per element, enough to wrap the row.
const widthOf = (node) => {
    const width = node?.getBoundingClientRect?.().width;
    return Number.isFinite(width) && width > 0 ? width : Number(node?.offsetWidth) || 0;
};

// Refitting changes the observed row/strip. Leave ResizeObserver delivery before writing
// layout, coalesce notifications, and release pending work with the composer's lifetime.
function observeLayout(win, nodes, layout) {
    if (!nodes[0] || typeof win?.ResizeObserver !== 'function') return () => {};
    let frame = 0;
    const observer = new win.ResizeObserver(() => {
        if (!frame) frame = win.requestAnimationFrame(() => { frame = 0; layout(); });
    });
    nodes.forEach((node) => observer.observe(node));
    return () => {
        observer.disconnect();
        if (frame) win.cancelAnimationFrame(frame);
        frame = 0;
    };
}

/** The primary level is independent; the helper brackets cannot cross each other. */
export function applyHandle(base, which, value) {
    const s = { ...base };
    const v = clamp(value, 0, N);
    if (which === 'min') s.min = Math.min(v, s.max);
    else if (which === 'max') s.max = Math.max(v, s.min);
    else s.rec = v;
    return s;
}

export function snapRange(v) {
    const s = { min: Math.round(v.min), rec: Math.round(v.rec), max: Math.round(v.max) };
    s.rec = clamp(s.rec, 0, N);
    return s;
}

const indicesOf = (range) => ({
    min: ownerLevelIndex(range.min), rec: ownerLevelIndex(range.recommended), max: ownerLevelIndex(range.max),
});
const sameIndices = (a, b) => Boolean(a && b) && HANDLES.every((key) => a[key] === b[key]);

/* The ring glyph: the range in miniature (a 300° arc; the band is min..max, the dot the recommended
   level). Context's ring is the same frame cut into three arcs, the chosen mode's arc lit: a
   position among three modes, never a fill level or a capacity. */
const RING = { cx: 92, cy: 92, r: 70, a0: 120, sweep: 300 };
const CONTEXT_ARCS = { nano: [120, 200], low: [230, 310], max: [340, 420] };
const ringPoint = (angle) => ({
    x: RING.cx + RING.r * Math.cos((angle * Math.PI) / 180), y: RING.cy + RING.r * Math.sin((angle * Math.PI) / 180),
});
const ringAngle = (level) => RING.a0 + (level / N) * RING.sweep;
function ringArc(a0, a1) {
    const hi = Math.max(a1, a0 + 0.02);
    const p0 = ringPoint(a0);
    const p1 = ringPoint(hi);
    return `M${p0.x.toFixed(2)} ${p0.y.toFixed(2)} A${RING.r} ${RING.r} 0 ${hi - a0 > 180 ? 1 : 0} 1 ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}`;
}

/* A capsule's head: the ring, the setting's name and its value (the value gives way to the strip). */
const capsuleHead = (glyph, name, headClass, valueAttrs = '') => `<button type="button" class="chat-capsule-head ui-hit-area ${headClass}" aria-expanded="false">`
    + `<span class="chat-capsule-glyph" aria-hidden="true"><svg viewBox="0 0 184 184">${glyph}</svg></span>`
    + `<span class="chat-capsule-name">${name}</span><span class="chat-capsule-value"${valueAttrs}></span></button>`;

function effortRangeMarkup() {
    const segments = EFFORT_OPTIONS.map((option) =>
        `<span class="chat-effort-seg" data-effort-seg="${option.value}"><span class="chat-effort-seg-text">${option.label}</span></span>`).join('');
    const ring = `<path class="chat-effort-glyph-rail" data-ring-rail d="${ringArc(RING.a0, RING.a0 + RING.sweep)}" fill="none" stroke-width="22" stroke-linecap="round"></path><path class="chat-effort-glyph-band" data-ring-band fill="none" stroke-width="26" stroke-linecap="round"></path><circle class="chat-effort-glyph-dot" data-ring-rec r="22"></circle>`;
    // The value is a tier word read through the catalog seam (effortText): the overlay leaves it alone.
    return capsuleHead(ring, 'Effort', 'chat-effort-head', ' data-i18n-skip')
        + '<div class="chat-capsule-body">'
        + '<div class="chat-effort-strip" data-effort-strip data-scroll="none"><div class="chat-effort-band" data-effort-band aria-hidden="true"></div>'
        + '<div class="chat-effort-handle chat-effort-rec" data-handle="rec" role="slider" tabindex="-1" aria-label="Primary effort"></div>'
        + segments
        + '<div class="chat-effort-handle chat-effort-cap chat-effort-min" data-handle="min" role="slider" tabindex="-1" aria-label="Minimum helper effort"></div>'
        + '<div class="chat-effort-handle chat-effort-cap chat-effort-max" data-handle="max" role="slider" tabindex="-1" aria-label="Maximum helper effort"></div></div>'
        + '</div>';
}

function contextModeMarkup() {
    const arcs = CONTEXT_MODES.map((mode) => `<path class="chat-context-arc" data-arc="${mode}" d="${ringArc(...CONTEXT_ARCS[mode])}" fill="none" stroke-width="24" stroke-linecap="round"></path>`).join('');
    const modes = CONTEXT_MODES.map((mode) =>
        `<button class="chat-seg" type="button" role="radio" tabindex="-1" aria-checked="false" data-mode="${mode}"><span class="chat-seg-text">${CONTEXT_LABELS[mode]}</span></button>`).join('');
    return capsuleHead(arcs, 'Context', 'chat-context-head')
        + `<div class="chat-capsule-body"><div class="chat-context-strip" role="radiogroup" aria-label="Context size mode" data-scroll="none"><span class="chat-context-thumb" aria-hidden="true"></span>${modes}</div></div>`;
}

/**
 * What both capsules share: the open/pin state machine and the fit. A capsule opens INLINE
 * into its strip, never as a dropdown, and the strip stays on the composer's line: beside the
 * other controls when it fits there, alone (they step aside, its own name too if need be) when
 * it does not, scrolling inside itself only when nothing else fits. The fit is measured, no
 * breakpoint (a translation or another font needs none). One capsule is open at a time: opening
 * one closes the other at once (`peer`), so the fit measures it as it stands closed.
 *
 * Hover is an accelerator only (`(hover: hover) and (pointer: fine)`): a press pins the strip
 * open and unpins it; touch and keyboard use the press. Hover never opens a capsule while the
 * other one is open, since closing it would move this one under a still pointer.
 */
function createCapsule({
    el, row, kind, strip, segs, render, focusTarget, padMax = SEG_PAD.max,
    busy = () => false, onClosed = () => {}, afterOpen = () => {}, afterOverflow = afterOpen,
    onLayout = () => {}, onSettled = () => {}, win, doc,
}) {
    const q = (selector) => el.querySelector(selector);
    const head = q('.chat-capsule-head');
    const glyph = q('.chat-capsule-glyph');
    const name = q('.chat-capsule-name');
    const value = q('.chat-capsule-value');
    const body = q('.chat-capsule-body');
    // `hover` exists only with a mouse; the chat unit-test DOM carries no matchMedia at all.
    const hoverCapable = typeof win?.matchMedia === 'function' && win.matchMedia(HOVER_MEDIA)?.matches === true;
    const state = { open: false, pinned: false, collapsing: false, destroyed: false };
    const api = {
        el, kind, hoverCapable, peer: null, isOpen: () => state.open, isPinned: () => state.pinned,
        isSettled: () => !state.open && !state.collapsing,
    };
    el.dataset.open = 'false';
    head.setAttribute('aria-expanded', 'false');
    let hoverTimer = 0;
    let leaveTimer = 0;
    let lastPointer = null;

    // Focus keeps a hover-opened strip open only when the keyboard put it there: browsers report
    // a handle focused after a mouse press as :focus-visible, so the last input decides instead.
    let keyboardFocus = false;
    const stillHere = () => {
        try { return Boolean(el.matches(':hover') || (keyboardFocus && el.contains(doc.activeElement))); } catch { return false; }
    };
    // The open strip forgives a mouse that drifts a little: inside the zone (the control plus a
    // margin) it stays; outside, it closes after the delay unless the mouse comes back first. A
    // pinned strip, a drag and keyboard focus keep it open regardless.
    function inZone(point) {
        const r = point && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null;
        return Boolean(r) && point.x >= r.left - HOVER_ZONE_PX && point.x <= r.right + HOVER_ZONE_PX
            && point.y >= r.top - HOVER_ZONE_PX && point.y <= r.bottom + HOVER_ZONE_PX;
    }
    function closeSoon() {
        clearTimeout(leaveTimer);
        startTracking();
        leaveTimer = setTimeout(() => {
            leaveTimer = 0;
            if (state.open && !state.pinned && !busy() && !stillHere() && !inZone(lastPointer)) setOpen(false);
        }, HOVER_CLOSE_MS);
    }
    let tracking = false;
    const onDocMove = (event) => {
        if (event.pointerType && event.pointerType !== 'mouse') return;
        lastPointer = { x: event.clientX, y: event.clientY };
        if (!state.open || state.pinned) { stopTracking(); return; }
        if (inZone(lastPointer)) { clearTimeout(leaveTimer); leaveTimer = 0; }
        else if (!leaveTimer) closeSoon();
    };
    const onDocLeave = () => { lastPointer = null; if (state.open && !state.pinned) closeSoon(); };
    function startTracking() {
        if (tracking || !hoverCapable) return;
        tracking = true;
        doc.addEventListener('pointermove', onDocMove);
        doc.documentElement?.addEventListener?.('pointerleave', onDocLeave);
    }
    function stopTracking() {
        if (!tracking) return;
        tracking = false;
        doc.removeEventListener('pointermove', onDocMove);
        doc.documentElement?.removeEventListener?.('pointerleave', onDocLeave);
    }

    /* --- fit: beside the other controls, alone, or scrolling --- */
    const styleOf = (node, pseudo) => { try { return win?.getComputedStyle?.(node, pseudo) || null; } catch { return null; } };
    // Right to left (the rendered direction) the strip starts at its right end: its levels run
    // leftwards to negative offsets and its scrollLeft runs from 0 down.
    const isRtl = () => styleOf(strip)?.direction === 'rtl';
    // What stays on the line beside the open strip: the pills' own children (their wrapper
    // stretches on a phone) and the other capsule, closed, each with its row gap. Alone, they
    // are hidden and measure 0: the width seen before they stepped aside stays.
    let peers = { width: 0, count: 0 };
    function measurePeers(gap) {
        if (row.dataset?.ownerSolo) return peers;
        let width = 0;
        let count = 0;
        for (const node of Array.from(row.children || [])) {
            if (node === el || node.hidden) continue;
            const kids = node.classList?.contains('chat-composer-pills') ? Array.from(node.children || []).filter((kid) => !kid.hidden) : [node];
            const own = kids.reduce((sum, kid) => sum + widthOf(kid), 0) + px(styleOf(node)?.columnGap) * Math.max(0, kids.length - 1);
            if (own > 0) { width += own + gap; count += 1; }
        }
        peers = { width, count };
        return peers;
    }
    // The head as it stands open: the ring, then the name and the divider before the levels
    // unless the strip needs their room (the name is cached: a name that gave way measures 0).
    let nameWidth = 0;
    const measureName = () => { const shown = widthOf(name); if (shown > 0) nameWidth = shown; };
    function headWidths() {
        measureName();
        const headStyle = styleOf(head);
        const nameStyle = styleOf(name);
        const divider = styleOf(body, '::before');
        const bare = px(headStyle?.paddingLeft) + widthOf(glyph) + px(headStyle?.paddingRight);
        return {
            bare,
            label: bare + px(nameStyle?.marginLeft) + nameWidth + px(nameStyle?.marginRight)
                + px(divider?.marginLeft) + px(divider?.width) + px(divider?.marginRight),
        };
    }
    // Every input is independent of the padding it decides (words, head, borders, the row), so
    // a resize that follows a new padding finds the same answer.
    function measure() {
        const rowStyle = styleOf(row);
        const room = widthOf(row) - px(rowStyle?.paddingLeft) - px(rowStyle?.paddingRight) - 1;  // 1 px of slack
        if (!(room > 0)) return null;  // not laid out: a hidden pane, the unit-test DOM
        const words = segs.reduce((sum, seg) => sum + widthOf(seg.firstElementChild), 0);
        const elStyle = styleOf(el);
        const stripStyle = styleOf(strip);
        // Besides the head and the words: the borders and the strip's end gutter (its optical
        // clearance from the rounded end).
        const frame = px(elStyle?.borderLeftWidth) + px(elStyle?.borderRightWidth)
            + px(stripStyle?.marginLeft) + px(stripStyle?.marginRight);
        const heads = headWidths();
        const padFor = (width, headWidth) => Math.floor((width - headWidth - frame - words) / (2 * segs.length));
        const fitted = (mode, pad, label) => ({ mode, pad: Math.min(padMax, pad), label });
        const beside = measurePeers(px(rowStyle?.columnGap));
        let pad = padFor(room - beside.width, heads.label);
        if (pad >= (beside.count ? SEG_PAD.beside : SEG_PAD.alone)) return fitted('inline', pad, true);
        const alone = beside.count ? 'solo' : 'inline';
        pad = padFor(room, heads.label);
        if (pad >= SEG_PAD.alone) return fitted(alone, pad, true);
        pad = padFor(room, heads.bare);
        if (pad >= SEG_PAD.alone) return fitted(alone, pad, false);
        return fitted('overflow', SEG_PAD.alone, false);
    }
    function fit() {
        const fitted = state.open ? measure() : null;
        if (!fitted) return;
        el.style.setProperty('--capsule-seg-pad', `${fitted.pad}px`);
        el.dataset.fit = fitted.mode;
        el.dataset.tight = String(fitted.pad <= SEG_PAD.beside);
        el.dataset.label = String(fitted.label);
        if (fitted.mode !== 'inline') row.dataset.ownerSolo = kind;
        else if (row.dataset.ownerSolo === kind) delete row.dataset.ownerSolo;
        if (held) holdBody();
    }
    // A strip that scrolls fades on the side that hides levels (`start`/`end`: how far along its
    // own direction it is scrolled).
    function onStripScroll() {
        // The levels' own end, not scrollWidth: a bracket's hit area may reach past the last level.
        const last = segs[segs.length - 1];
        const rtl = isRtl();
        const width = Number(strip.clientWidth) || 0;
        const end = rtl ? width - Number(last?.offsetLeft) : Number(last?.offsetLeft) + Number(last?.offsetWidth);
        const scrollable = (Number.isFinite(end) && end > 0 ? end : Number(strip.scrollWidth) || 0) - width;
        const at = (rtl ? -1 : 1) * (Number(strip.scrollLeft) || 0);
        strip.dataset.scroll = scrollable <= 1 ? 'none' : at <= 1 ? 'start' : at >= scrollable - 1 ? 'end' : 'middle';
    }
    // ...and keeps what the owner moved (a handle, a mode; `left..right` in the strip) in view. The
    // view is `scrollLeft..scrollLeft + clientWidth` in offsetLeft's terms either way; it never
    // scrolls back past its start (0).
    function reveal(left, right) {
        if (el.dataset.fit !== 'overflow') return;
        const width = Number(strip.clientWidth) || 0;
        let to = null;
        if (left - REVEAL_MARGIN_PX < strip.scrollLeft) to = left - REVEAL_MARGIN_PX;
        else if (right + REVEAL_MARGIN_PX > strip.scrollLeft + width) to = right + REVEAL_MARGIN_PX - width;
        if (to !== null) strip.scrollLeft = isRtl() ? Math.min(0, to) : Math.max(0, to);
        onStripScroll();
    }
    // The value gives way while the strip grows, on its curve and from both real widths (style.css):
    // the capsule's width moves straight between its closed and open widths, so a line that holds
    // both never wraps in between. The widths are held while that runs, then follow the words again.
    let held = false;
    const heldWidth = (width) => `${Math.ceil(width * 64 + 1) / 64}px`;  // a hair over: layout rounds to 1/64 px
    function valueWidth() {
        try {  // the text keeps its whole width while the value gives way
            const range = doc.createRange();
            range.selectNodeContents(value);
            const width = range.getBoundingClientRect().width;
            if (width > 0) return width;
        } catch { /* no layout */ }
        return Number(value.scrollWidth) || 0;
    }
    function holdBody() {
        const stripStyle = styleOf(strip);
        const divider = el.dataset.label === 'false' ? null : styleOf(body, '::before');
        el.style.setProperty('--capsule-body-width', heldWidth(widthOf(strip) + px(stripStyle?.marginLeft) + px(stripStyle?.marginRight)
            + px(divider?.marginLeft) + px(divider?.width) + px(divider?.marginRight)));
    }
    function holdWidths() {
        if (!(widthOf(row) > 0)) return;  // not laid out: nothing moves
        held = true;
        el.style.setProperty('--capsule-value-width', heldWidth(valueWidth()));
        holdBody();
        void el.offsetWidth;  // the change starts from these widths
    }
    function releaseWidths() {
        if (!held) return;
        held = false;
        el.style.setProperty('--capsule-value-width', '');
        el.style.setProperty('--capsule-body-width', '');
    }
    // Alone, the strip appears and disappears at once (no width animation), so the other
    // controls never come back beside a strip that is still collapsing: the instant close is
    // flushed before the solo flag goes. The padding stays as fitted: beside them the strip is
    // still collapsing, and a wider padding now would push it onto a second line for those
    // frames (the next open fits again).
    function settleClosed() {
        if (el.dataset.fit && el.dataset.fit !== 'inline') void body.offsetWidth;
        delete el.dataset.fit;
        if (row.dataset?.ownerSolo === kind) delete row.dataset.ownerSolo;
    }
    // A layout change (opening, a resize, a new padding) moves the marks with the words at once;
    // only an owner's gesture animates them.
    function settle(update) {
        el.dataset.settling = 'true';
        update();
        void strip.offsetWidth;
        delete el.dataset.settling;
    }
    // An inline strip collapses with a transition; the closed capsule's width settles when it
    // ends (at once alone, scrolling, or snapped).
    let settleTimer = 0;
    function settled() {
        clearTimeout(settleTimer);
        settleTimer = 0;
        state.collapsing = false;
        releaseWidths();
        if (!state.destroyed) onSettled();
    }
    const onBodyTransitionEnd = (event) => {
        if (event.target !== body || event.propertyName !== 'max-width') return;
        if (state.collapsing) settled();
        else if (state.open) releaseWidths();
    };
    function setOpen(open, { focus = false } = {}) {
        if (state.open === open && !focus) return;
        if (open && !state.open) api.peer?.snapClosed();
        const flips = state.open !== open;
        const morph = flips && el.dataset.snap !== 'true' ? beginMorph({ el, row, win }) : null;  // alone, it grows in place
        const collapses = !open && state.open && el.dataset.fit === 'inline' && el.dataset.snap !== 'true';
        state.open = open;
        clearTimeout(settleTimer);
        state.collapsing = collapses;
        if (open) el.dataset.settling = 'true';
        if (open) fit();
        if (flips) holdWidths();
        el.dataset.open = String(open);
        head.setAttribute('aria-expanded', String(open));
        if (!open) {
            state.pinned = false;
            clearTimeout(hoverTimer);
            clearTimeout(leaveTimer);
            leaveTimer = 0;
            stopTracking();
            onClosed();
            settleClosed();
            if (collapses) settleTimer = setTimeout(settled, COLLAPSE_MS);
        }
        render();
        if (open) {
            void strip.offsetWidth;  // the opening frame lands without the marks' transitions
            delete el.dataset.settling;
            afterOpen();
        }
        if (open && focus) focusTarget()?.focus({ preventScroll: true });
        onLayout();
        if (!open && !collapses) settled();
        morph?.play(open);
    }
    // Closed at once, a running collapse included: the other capsule is opening and measures this one.
    function snapClosed() {
        stopMorph(el);  // its own strip, still growing or folding, goes too
        el.dataset.snap = 'true';
        setOpen(false);
        void el.offsetWidth;
        delete el.dataset.snap;
        if (state.collapsing) settled();
    }
    const onHeadClick = (event) => {
        const viaKeyboard = event.detail === 0;
        if (hoverCapable) {
            if (!state.open) { state.pinned = true; setOpen(true, { focus: viaKeyboard }); }
            else if (!state.pinned) state.pinned = true;
            else { state.pinned = false; setOpen(false); }
        } else {
            setOpen(!state.open, { focus: viaKeyboard && !state.open });
        }
    };
    // A press on the other capsule is its own to answer: opening it closes this one at once,
    // before anything moves under the pointer.
    const onDocPointerDown = (event) => {
        keyboardFocus = false;
        if (state.open && !el.contains(event.target) && !api.peer?.el.contains(event.target)) { state.pinned = false; setOpen(false); }
    };
    const onDocKeyDown = (event) => {
        keyboardFocus = true;
        if (event.key !== 'Escape' || !state.open || event.defaultPrevented) return;
        event.preventDefault();  // the capsule took this Escape: the Project room under it stays open
        state.pinned = false;
        setOpen(false);
        head.focus({ preventScroll: true });
    };
    const onEnter = (event) => {
        if (event.pointerType !== 'mouse') return;
        clearTimeout(leaveTimer);
        leaveTimer = 0;
        stopTracking();
        if (!state.open) {
            clearTimeout(hoverTimer);
            hoverTimer = setTimeout(() => {
                if (state.destroyed || api.peer?.isOpen()) return;
                // Hover opens only where the head stays under the mouse. Alone or scrolling, the
                // other controls step aside and the strip moves under a still pointer, so a click
                // meant to pin would land on a level: there the press opens it, as on touch.
                const fitted = measure();
                if (fitted && fitted.mode !== 'inline') return;
                setOpen(true);
            }, HOVER_OPEN_MS);
        }
    };
    const onLeave = (event) => {
        if (event.pointerType !== 'mouse') return;
        clearTimeout(hoverTimer);
        lastPointer = { x: event.clientX, y: event.clientY };
        if (!state.open || state.pinned) return;
        startTracking();
        if (!inZone(lastPointer)) closeSoon();
    };

    // The row and the strip change size without a window resize (a Project pane's divider, a
    // font that loads): measure again then, so the fit and the marks follow. A strip that starts
    // to scroll reveals the active target; one that already scrolls stays where the owner put it.
    const relayout = () => settle(() => {
        const scrolled = el.dataset.fit === 'overflow';
        if (state.open) fit();
        render();
        if (!scrolled && el.dataset.fit === 'overflow') afterOverflow();
        onStripScroll();
    });
    const onResize = () => { if (state.open) relayout(); };
    const stopObserving = observeLayout(win, [row, strip], () => { if (!state.destroyed) relayout(); });

    head.addEventListener('click', onHeadClick);
    body.addEventListener('transitionend', onBodyTransitionEnd);
    strip.addEventListener('scroll', onStripScroll, { passive: true });
    win?.addEventListener?.('resize', onResize);
    doc.addEventListener('pointerdown', onDocPointerDown, true);
    doc.addEventListener('keydown', onDocKeyDown);
    if (hoverCapable) {
        el.addEventListener('pointerenter', onEnter);
        el.addEventListener('pointerleave', onLeave);
    }

    return Object.assign(api, {
        setOpen, snapClosed, closeSoon, onStripScroll, reveal, measureName, isRtl, relayout,
        destroy() {
            state.destroyed = true;
            stopMorph(el);
            clearTimeout(hoverTimer);
            clearTimeout(leaveTimer);
            clearTimeout(settleTimer);
            body.removeEventListener('transitionend', onBodyTransitionEnd);
            doc.removeEventListener('pointerdown', onDocPointerDown, true);
            doc.removeEventListener('keydown', onDocKeyDown);
            head.removeEventListener('click', onHeadClick);
            strip.removeEventListener('scroll', onStripScroll);
            win?.removeEventListener?.('resize', onResize);
            stopObserving();
            stopTracking();
            if (row.dataset?.ownerSolo === kind) delete row.dataset.ownerSolo;
            el.removeEventListener('pointerenter', onEnter);
            el.removeEventListener('pointerleave', onLeave);
        },
    });
}

/** Inline primary/helper effort controls. Gesture-end saves serialize; refusal restores
 * the last server value. A stored `minimal` renders at Low until that handle moves. */
export function createEffortRangeControl({
    row, saveEffortRange, showToast, refreshState = () => {}, onLayout = () => {}, onSettled = () => {},
    win = globalThis.window, doc = globalThis.document,
}) {
    const el = doc.createElement('div');
    el.className = 'chat-capsule chat-effort-range';
    el.setAttribute('data-effort-range', '');
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'Effort range');
    el.dataset.known = 'false';
    el.innerHTML = effortRangeMarkup();
    const q = (selector) => el.querySelector(selector);
    const head = q('.chat-effort-head');
    const value = q('.chat-capsule-value');
    const strip = q('[data-effort-strip]');
    const band = q('[data-effort-band]');
    const ring = { band: q('[data-ring-band]'), rec: q('[data-ring-rec]') };
    const segs = Array.from(el.querySelectorAll('.chat-effort-seg'));
    const handles = { min: q('.chat-effort-min'), rec: q('.chat-effort-rec'), max: q('.chat-effort-max') };
    // After the pills and the context capsule.
    const before = row.querySelector('.chat-context-mode') || row.querySelector('.chat-composer-pills');
    if (before && typeof before.insertAdjacentElement === 'function') before.insertAdjacentElement('afterend', el);
    else row.appendChild(el);

    // `stored` is the server's value; `draft` the tiers the server will hold once every queued
    // save landed (equal to `stored` when nothing is pending); `shown` the owner-tier indices.
    const state = {
        stored: { ...EFFORT_RANGE_DEFAULT }, draft: { ...EFFORT_RANGE_DEFAULT }, shown: indicesOf(EFFORT_RANGE_DEFAULT),
        view: null, drag: null, saving: null, queued: null, lastOk: true, destroyed: false,
    };
    for (const which of HANDLES) { handles[which].setAttribute('role', 'slider'); handles[which].tabIndex = -1; }
    let capsule = null;

    /* --- geometry: segment edges in px along the scale, measured from the DOM (labels differ in
       width). Right to left the levels run leftwards: a position along the scale is the strip's x
       times `dir` (-1), and the same product turns it back. --- */
    const scaleDir = () => (capsule?.isRtl() ? -1 : 1);
    function edges(dir = scaleDir()) {
        // Each level's start, then the last level's end. The levels meet, so right to left a level
        // starts where the one before it ends: one whole-pixel offset per edge, as left to right.
        const left = segs.map((seg) => Number(seg.offsetLeft));
        const e = dir > 0 ? [...left, left[N] + Number(segs[N].offsetWidth)]
            : [-(left[0] + Number(segs[0].offsetWidth)), ...left.map((x) => -x)];
        return e.every(Number.isFinite) && e[N + 1] > e[0] ? e : null;
    }
    function boundaryAt(event) {
        const dir = scaleDir();
        const e = edges(dir);
        if (!e) return null;
        const x = clamp(dir * (event.clientX - strip.getBoundingClientRect().left + (Number(strip.scrollLeft) || 0)), e[0], e[N + 1]);
        let i = 0;
        while (i < N && x >= e[i + 1]) i += 1;
        return i + (x - e[i]) / (e[i + 1] - e[i]);
    }
    function valueAt(event, which) {
        const b = boundaryAt(event);
        if (b === null) return (state.view || state.shown)[which];
        return which === 'min' ? b : which === 'max' ? b - 1 : b - 0.5;
    }
    function pick(event) {
        const b = boundaryAt(event);
        const segment = b === null ? state.shown.rec : Math.min(N, Math.floor(b));
        return { which: 'rec', start: segment };  // helper bounds have their own handles
    }

    /* --- rendering: every mark sits on whole levels, a drag included (the pill always covers
       the word it names independently of the helper brackets) --- */
    function render() {
        const s = snapRange(state.view || state.shown);
        const dir = scaleDir();
        const e = edges(dir);
        if (e) {
            // A span along the scale stands at its left side in the strip.
            const span = (node, from, to) => {
                node.style.left = `${Math.min(dir * from, dir * to)}px`;
                node.style.width = `${to - from}px`;
            };
            span(band, e[s.min], e[s.max + 1]);
            span(handles.rec, e[s.rec], e[s.rec + 1]);
            handles.min.style.left = `${dir * e[s.min]}px`;
            handles.max.style.left = `${dir * e[s.max + 1]}px`;  // each bracket draws toward the range (style.css)
        }
        segs.forEach((seg, i) => { seg.dataset.in = String(i >= s.min && i <= s.max); seg.dataset.rec = String(i === s.rec); });
        el.dataset.dragging = String(Boolean(state.drag?.moved));
        ring.band.setAttribute('d', ringArc(ringAngle(s.min), ringAngle(s.max)));
        const dot = ringPoint(ringAngle(s.rec));
        ring.rec.setAttribute('cx', dot.x.toFixed(2));
        ring.rec.setAttribute('cy', dot.y.toFixed(2));
        // Primary may go anywhere; each helper bracket stops at the other one.
        const bounds = { min: [0, s.max], rec: [0, N], max: [s.min, N] };
        const open = Boolean(capsule?.isOpen());
        for (const which of HANDLES) {
            handles[which].setAttribute('aria-valuemin', String(bounds[which][0]));
            handles[which].setAttribute('aria-valuemax', String(bounds[which][1]));
            handles[which].setAttribute('aria-valuenow', String(s[which]));
            handles[which].setAttribute('aria-valuetext', effortText(OWNER_TIERS[s[which]]));
            handles[which].dataset.dragging = String(state.drag?.which === which);
            handles[which].tabIndex = open ? 0 : -1;
        }
        // Closed, the capsule names only the recommended level; the ring and the name say the range.
        const word = effortText(state.draft.recommended);
        if (value.textContent !== word) value.textContent = word;
        const stored = state.stored;
        head.title = fmt('Primary effort: {level}; helpers {min}–{max}. Applies to new participants in this room.', {
            level: effortText(stored.recommended), min: effortText(stored.min), max: effortText(stored.max),
        });
        head.setAttribute('aria-label', head.title);
    }

    /* --- the model: drag preview, commit on gesture end, serialized saves --- */
    function beginDrag(which) { state.drag = { which }; state.view = { ...state.shown }; render(); }
    // A drag builds on its preview; cancel restores the committed values.
    function dragTo(which, value) { if (!state.drag) return; state.view = applyHandle(state.view || state.shown, which, value); render(); }
    function cancelDrag() { state.drag = null; state.view = null; render(); }
    function endDrag() {
        if (!state.drag) return;
        const { which } = state.drag;
        const next = snapRange(state.view);
        state.drag = null;
        state.view = null;
        commit(next);
        revealHandle(which);
        if (capsule.isOpen() && !capsule.isPinned() && capsule.hoverCapable) capsule.closeSoon();
    }
    // A handle the owner did not move keeps its tier (`minimal` shown at Low stays `minimal`).
    function tripleFor(next) {
        const prev = state.shown;
        const draft = state.draft;
        return {
            min: next.min === prev.min ? draft.min : OWNER_TIERS[next.min],
            recommended: next.rec === prev.rec ? draft.recommended : OWNER_TIERS[next.rec],
            max: next.max === prev.max ? draft.max : OWNER_TIERS[next.max],
        };
    }
    function commit(next) {
        if (sameIndices(next, state.shown)) { render(); return; }
        const triple = tripleFor(next);
        state.shown = next;
        state.draft = triple;
        render();
        enqueue(triple);
    }
    function applyServer(range) {
        state.stored = normalizeEffortRange(range);
        state.draft = { ...state.stored };
        state.shown = indicesOf(state.stored);
        el.dataset.known = 'true';
        render();
    }
    function enqueue(triple) {
        state.queued = triple;
        el.dataset.saving = 'true';
        if (!state.saving) state.saving = drain();
    }
    async function drain() {
        let ok = true;
        while (state.queued) {
            const triple = state.queued;
            state.queued = null;
            try {
                const response = await saveEffortRange(triple);
                if (state.destroyed) return ok;
                const saved = response?.effort_range && typeof response.effort_range === 'object' ? response.effort_range : triple;
                // A newer edit is queued and was built on the draft: the draft stays ahead of this
                // answer, so the next edit never starts from an older range.
                if (state.queued) state.stored = normalizeEffortRange(saved);
                else applyServer(saved);
                ok = true;
            } catch (error) {
                if (state.destroyed) return false;
                ok = false;
                state.queued = null;  // later edits were built on the refused draft
                showToast(error?.message || 'Could not change the effort range.', 'error');
                applyServer(state.stored);  // the server's value, never the unsaved preview
            }
        }
        state.saving = null;
        state.lastOk = ok;
        el.dataset.saving = 'false';
        refreshState(true);
        return ok;
    }

    // A strip that scrolls shows where it continues and keeps the moved handle in view.
    function revealHandle(which = 'rec') {
        if (el.dataset.fit !== 'overflow') return;
        const dir = scaleDir();
        const e = edges(dir);
        if (!e) return;
        const s = snapRange(state.view || state.shown);
        const cap = widthOf(handles.min);
        const [from, to] = which === 'min' ? [e[s.min], e[s.min] + cap]
            : which === 'max' ? [e[s.max + 1] - cap, e[s.max + 1]] : [e[s.rec], e[s.rec + 1]];
        capsule.reveal(Math.min(dir * from, dir * to), Math.max(dir * from, dir * to));
    }

    capsule = createCapsule({
        el, row, kind: 'effort', strip, segs, render, focusTarget: () => handles.rec, onLayout, onSettled, win, doc,
        busy: () => Boolean(state.drag), onClosed: () => { if (state.drag) cancelDrag(); }, afterOpen: () => revealHandle('rec'),
        afterOverflow: () => revealHandle(HANDLES.find((which) => handles[which] === doc.activeElement) || 'rec'),
    });

    /* --- pointer: a press on the strip grabs a handle (or picks one) and keeps the grab offset --- */
    const onDown = (event) => {
        if (event.button) return;
        event.preventDefault?.();
        const handleEl = event.target?.closest?.('[data-handle]');
        let which;
        let start;
        if (handleEl) { which = handleEl.dataset.handle; start = state.shown[which]; }
        else ({ which, start } = pick(event));
        let at = state.shown[which];  // each level the finger reaches ticks once (haptics.js), a press on another too
        const reach = () => { const now = snapRange(state.view)[which]; if (now !== at) { at = now; haptic('tick'); } };
        beginDrag(which);
        if (!handleEl) dragTo(which, start);
        reach();
        const grab = valueAt(event, which) - state.view[which];
        handles[which].focus({ preventScroll: true, focusVisible: false });  // no keyboard ring after a mouse press
        try { strip.setPointerCapture(event.pointerId); } catch { /* a synthetic event */ }
        const move = (ev) => {
            if (ev.pointerId !== event.pointerId || !state.drag) return;
            state.drag.moved = true;  // a press that stays put is a tap: its marks still animate
            dragTo(which, valueAt(ev, which) - grab);
            reach();
        };
        const release = (ev) => {
            if (ev.pointerId !== event.pointerId) return;
            strip.removeEventListener('pointermove', move);
            strip.removeEventListener('pointerup', up);
            strip.removeEventListener('pointercancel', cancel);
        };
        const up = (ev) => { if (ev.pointerId !== event.pointerId) return; release(ev); endDrag(); };
        const cancel = (ev) => { if (ev.pointerId !== event.pointerId) return; release(ev); cancelDrag(); };
        strip.addEventListener('pointermove', move);
        strip.addEventListener('pointerup', up);
        strip.addEventListener('pointercancel', cancel);
    };
    const onHandleKey = (event) => {
        const which = event.currentTarget?.dataset?.handle || event.target?.dataset?.handle;
        if (!which) return;
        const step = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, PageUp: 1, PageDown: -1 }[event.key];
        let target;
        if (step) target = state.shown[which] + step;
        else if (event.key === 'Home') target = 0;
        else if (event.key === 'End') target = N;
        else return;
        event.preventDefault?.();
        commit(snapRange(applyHandle(state.shown, which, clamp(target, 0, N))));
        revealHandle(which);
    };

    strip.addEventListener('pointerdown', onDown);
    for (const which of HANDLES) handles[which].addEventListener('keydown', onHandleKey);
    render();

    return {
        el,
        capsule,
        hoverCapable: capsule.hoverCapable,
        isOpen: () => capsule.isOpen(),
        isPinned: () => capsule.isPinned(),
        shown: () => ({ ...state.shown }),
        stored: () => ({ ...state.stored }),
        /** The containing composer supplies only its room's authoritative value. */
        syncState(data) {
            const range = data?.effort_range;
            if (!range || typeof range !== 'object' || typeof range.recommended !== 'string') return;
            if (state.saving || state.queued || state.drag) return;  // the save's own echo wins over a stale poll
            applyServer(range);
        },
        hasPendingSave: () => Boolean(state.saving),
        /** Resolves once every queued save settled: true when the last one was accepted. */
        pendingSave: () => (state.saving ? state.saving : Promise.resolve(state.lastOk)),
        destroy() {
            state.destroyed = true;
            capsule.destroy();
            strip.removeEventListener('pointerdown', onDown);
            for (const which of HANDLES) handles[which].removeEventListener('keydown', onHandleKey);
            el.remove();
        },
    };
}

/** Context saves apply to future room participants; a refusal retains the saved mode. */
function createContextModeControl({ el, row, saveContextMode, showToast, refreshState, onLayout, onSettled, win, doc }) {
    el.classList.add('chat-capsule');
    el.setAttribute('role', 'group');
    el.setAttribute('aria-label', 'Context size mode');
    el.innerHTML = contextModeMarkup();
    const head = el.querySelector('.chat-context-head');
    head.title = CONTEXT_TITLE;
    const value = el.querySelector('.chat-capsule-value');
    const strip = el.querySelector('.chat-context-strip');
    const thumb = el.querySelector('.chat-context-thumb');
    const segs = Array.from(el.querySelectorAll('.chat-seg'));
    const current = () => contextModeOf(el.dataset.contextMode);
    const chosen = () => segs.find((seg) => seg.dataset.mode === current());
    let capsule = null;
    let named = '';  // the mode the value names (an overlay may have translated the word)

    function render() {
        const mode = current();
        const open = Boolean(capsule?.isOpen());
        if (named !== mode) { named = mode; value.textContent = CONTEXT_LABELS[mode]; }
        for (const seg of segs) {
            seg.setAttribute('aria-checked', String(seg.dataset.mode === mode));
            seg.tabIndex = open && seg.dataset.mode === mode ? 0 : -1;  // one tab stop; arrows move within
        }
        const seg = chosen();
        if (Number(seg?.offsetWidth) > 0) {
            thumb.style.left = `${seg.offsetLeft}px`;
            thumb.style.width = `${seg.offsetWidth}px`;
        }
    }
    let saving = null, lastOk = true, destroyed = false;
    function choose(mode) {
        const next = contextModeOf(mode);
        if (saving || destroyed || next === current()) return;
        el.dataset.disabled = 'true';
        saving = (async () => {
            try {
                const saved = await saveContextMode(next);
                if (destroyed) return false;
                el.dataset.contextMode = contextModeOf(saved.context_mode);
                lastOk = true;
            } catch (e) {
                if (destroyed) return false;
                lastOk = false;
                showToast(e.message || 'Could not change context mode.', 'error');
            } finally {
                saving = null;
                if (!destroyed) {
                    el.dataset.disabled = 'false';
                    render();
                    refreshState(true);
                }
            }
            return lastOk;
        })();
        return saving;
    }
    const onChoose = (event) => {
        const seg = event.target?.closest?.('.chat-seg');
        const started = seg ? choose(seg.dataset.mode) : undefined;
        if (started) haptic('tick');  // another mode, chosen by a press (haptics.js)
        return started;
    };
    // A strip that scrolls (long translated words on a phone) keeps the chosen or focused mode whole.
    const revealMode = (seg) => { if (seg) capsule.reveal(Number(seg.offsetLeft), Number(seg.offsetLeft) + Number(seg.offsetWidth)); };
    const onKey = (event) => {
        const at = segs.indexOf(event.target?.closest?.('.chat-seg'));
        const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
        let to;
        if (at < 0) return;
        if (step) to = clamp(at + step, 0, segs.length - 1);
        else if (event.key === 'Home') to = 0;
        else if (event.key === 'End') to = segs.length - 1;
        else return;
        event.preventDefault?.();
        segs[to].focus({ preventScroll: true });
        revealMode(segs[to]);
        return choose(segs[to].dataset.mode);
    };

    capsule = createCapsule({
        el, row, kind: 'context', strip, segs, render, focusTarget: chosen, padMax: SEG_PAD.max + 3, onLayout, onSettled, win, doc,
        afterOpen: () => revealMode(chosen()),
        afterOverflow: () => revealMode(segs.find((seg) => seg === doc.activeElement) || chosen()),
    });
    el.addEventListener('click', onChoose);
    strip.addEventListener('keydown', onKey);
    render();

    return {
        el,
        capsule,
        /** A save in flight answers for itself; the composer supplies this room's value. */
        syncState(data) {
            if (typeof data?.context_mode !== 'string' || el.dataset.disabled === 'true') return;
            el.dataset.contextMode = contextModeOf(data.context_mode);
            render();
        },
        hasPendingSave: () => Boolean(saving),
        pendingSave: () => saving || Promise.resolve(lastOk),
        destroy() {
            destroyed = true;
            capsule.destroy();
            el.removeEventListener('click', onChoose);
            strip.removeEventListener('keydown', onKey);
        },
    };
}

/**
 * @param {object} deps
 * @param {Element} deps.row  the composer's `.chat-toolbar-row`
 * @param {(suffix: string) => Element|null} deps.byId  the instance-namespaced lookup
 * @param {typeof fetch} deps.apiFetch
 * @param {(body: object) => Promise<object>} deps.saveEffortRange  `apiClient.ownerEffortRange`
 * @param {(message: string, tone?: string) => void} deps.showToast
 * @param {(force?: boolean) => Promise<void>|void} deps.refreshState  a forced `/api/state` re-read
 * @param {() => void} [deps.onLayout]  a strip opened or closed on its own line
 * @param {HTMLElement} [deps.box]  independently sized page the composer docks inside
 * @param {() => boolean} [deps.readingBack]  the owner's own gesture took the room off its newest row (composer_fold.js)
 */
export function createComposerOwnerControls({ row, byId, apiFetch, saveEffortRange, showToast, refreshState, onLayout, projectId = '', box = null, win = globalThis.window, doc = globalThis.document, readingBack }) {
    let destroyed = false;
    const scoped = body => projectId ? { ...body, project_id: projectId } : body;
    const readback = reply => {
        if ((reply?.project_id || '') !== projectId) throw new Error('Settings response belongs to another room.');
        return reply?.cognition || reply;
    };
    const saveContextMode = async mode => {
        const response = await apiFetch('/api/owner/context-mode', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(scoped({ mode })),
        });
        const reply = await response.json();
        if (!response.ok) throw new Error(reply?.error || 'Could not change context mode.');
        return readback(reply);
    };
    const saveRoomEffort = async range => readback(await saveEffortRange(scoped(range)));
    const capsules = [];
    // Measure the settled, closed row synchronously: keep names while it fits, else rings and
    // values. If long translated values still wrap, cap the widest with an ellipsis; accessible
    // names and open strips keep full words. The closed names never flicker between fits. Contributed chips take
    // only the room the capsules leave (composer_chips.js), so their slot is never part of the fit.
    const shownKids = () => Array.from(row.children || [])
        .filter((kid) => widthOf(kid) > 0 && !kid.hasAttribute?.('data-composer-chips'));
    // A phone's ☰ leads the line and never wraps, so it is not what the line is read by: while the row folds it goes
    // down beside the field (composer_fold.js) and the line it left still holds the capsules.
    function wraps() {
        const kids = shownKids().filter((kid) => !kid.classList?.contains?.('composer-menu'));
        const first = kids[0]?.getBoundingClientRect?.();
        return Boolean(first) && kids.some((kid) => kid.getBoundingClientRect().top >= first.bottom);
    }
    function fitNames() {
        if (destroyed || !row?.dataset || capsules.some((capsule) => !capsule.isSettled())) return;
        delete row.dataset.ownerCompact;
        row.style.setProperty('--capsule-value-cap', '');
        capsules.forEach((capsule) => capsule.measureName());
        if (!wraps()) return;
        row.dataset.ownerCompact = 'true';
        const rowStyle = win?.getComputedStyle?.(row);
        const room = widthOf(row) - px(rowStyle?.paddingLeft) - px(rowStyle?.paddingRight);
        if (!(room > 0) || !wraps()) return;  // not laid out (the unit-test DOM), or the line holds
        const kids = shownKids();
        const over = kids.reduce((sum, kid) => sum + widthOf(kid), 0) + px(rowStyle?.columnGap) * (kids.length - 1) - room + 1;  // 1 px of slack
        // The cap that takes `over` off the widest values, each cut to it (a water level).
        const values = capsules.map((capsule) => widthOf(capsule.el.querySelector('.chat-capsule-value'))).sort((a, b) => b - a);
        let cap = 0;
        for (let k = 1, sum = 0; k <= values.length; k += 1) {
            sum += values[k - 1];
            cap = (sum - over) / k;
            if (k === values.length || cap >= values[k]) break;
        }
        row.style.setProperty('--capsule-value-cap', `${Math.max(0, Math.floor(cap))}px`);
        row.dataset.ownerCompact = 'values';
    }
    const shared = { showToast, refreshState, onLayout, onSettled: fitNames, win, doc };
    const contextEl = byId('context-mode');
    const context = contextEl ? createContextModeControl({ el: contextEl, row: row || contextEl.parentElement, saveContextMode, ...shared }) : null;
    const effort = row ? createEffortRangeControl({ row, saveEffortRange: saveRoomEffort, ...shared }) : null;
    for (const control of [context, effort]) if (control) capsules.push(control.capsule);
    // One capsule is open at a time.
    if (context && effort) { context.capsule.peer = effort.capsule; effort.capsule.peer = context.capsule; }
    win?.addEventListener?.('resize', fitNames);
    const stopObserving = observeLayout(win, [row], fitNames);
    const relayout = () => { if (!destroyed) { fitNames(); capsules.forEach((capsule) => capsule.relayout()); } };
    // Descendant fitting cannot resize this docking box, so mount/show/resize settle before paint.
    // Text is observed at the microtask boundary, not our geometry attributes; unchanged words stay.
    const boxObserver = box && typeof win?.ResizeObserver === 'function' ? new win.ResizeObserver(relayout) : null;
    boxObserver?.observe(box);
    // Own values only refit the closed line: realigning an open strip would cancel its motion.
    const ownValue = (record) => {
        const node = record.target.nodeType === 1 ? record.target : record.target.parentNode;
        return Boolean(node?.closest?.('.chat-capsule-value'));
    };
    const onText = (records) => { if (records?.length && records.every(ownValue)) fitNames(); else relayout(); };
    const textObserver = row && typeof win?.MutationObserver === 'function' ? new win.MutationObserver(onText) : null;
    textObserver?.observe(row, { childList: true, characterData: true, subtree: true });
    // On a phone the row folds away while the owner reads back through the room's history (composer_fold.js).
    const fold = box ? createComposerFold({ feed: box.querySelector('#chat-messages, .chat-messages'),
        wrap: row?.closest?.('.chat-input-wrap'), row, win, doc, readingBack }) : null;

    return {
        context,
        effort,
        syncState(data) {
            const values = projectId ? data?.projects?.find(project => project.id === projectId)?.cognition : data;
            if (destroyed || !values) return;
            context?.syncState(values); effort?.syncState(values); fitNames();
        },
        hasPendingSave: () => Boolean(context?.hasPendingSave() || effort?.hasPendingSave()),
        pendingSave: async () => (await Promise.all([context, effort]
            .filter(control => control?.hasPendingSave()).map(control => control.pendingSave()))).every(ok => ok !== false),
        destroy() {
            destroyed = true;
            stopObserving();
            win?.removeEventListener?.('resize', fitNames);
            boxObserver?.disconnect();
            textObserver?.disconnect();
            context?.destroy();
            effort?.destroy();
            fold?.destroy();
            if (row?.dataset) delete row.dataset.ownerCompact;
            row?.style?.setProperty?.('--capsule-value-cap', '');
        },
    };
}
