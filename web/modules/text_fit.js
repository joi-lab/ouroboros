// Fit a text into N lines of its element (docs/DESIGN.md "Replies"; the portal card's latest line). A CSS
// line clamp draws its ellipsis wherever the box ends, so a clamp that lands right after a full stop reads
// `редизайна.…` (four dots). This cuts on a word, drops the punctuation the cut leaves at the end, and only
// then adds the ellipsis. The full text stays on record (`fullText`) for copy, expansion and re-fitting; an
// element with no layout (a hidden page, a test DOM) simply shows the whole text and is fitted once it has
// a width. One ResizeObserver serves every fitted element; a width change re-fits, a height change does not, and an
// element removed from the page leaves the observer (no element is kept alive by it).
const fits = new WeakMap();  // element -> { text, lines, width, onFit }
let observer = null;

const TAIL = /[\s.,;:!?…—–-]+$/u;

/** The words before a cut, without the punctuation or space the cut leaves at their end, plus `…`. */
export function ellipsize(prefix) {
    return `${String(prefix ?? '').replace(TAIL, '')}…`;
}

function lineHeight(el) {
    const style = getComputedStyle(el);
    const value = parseFloat(style.lineHeight);
    return Number.isFinite(value) ? value : parseFloat(style.fontSize) * 1.35;
}

// A one-line (nowrap) element overflows sideways. Its text is measured with sub-pixel precision: a rounded
// scrollWidth can hide a fraction of a pixel that still makes the browser draw its own mid-word ellipsis.
function fitsInline(el, nowrap) {
    if (!nowrap) return true;
    if (typeof document === 'undefined' || typeof document.createRange !== 'function') return el.scrollWidth <= el.clientWidth;
    const range = document.createRange();
    range.selectNodeContents(el);
    return range.getBoundingClientRect().width <= el.getBoundingClientRect().width;
}

function refit(el) {
    const fit = fits.get(el);
    if (!fit) return;
    if (!el.isConnected || !el.clientWidth) {
        if (el.textContent !== fit.text) el.textContent = fit.text;
        fit.onFit?.(false);
        return;
    }
    fit.width = el.clientWidth;
    el.textContent = fit.text;
    const limit = fit.lines * lineHeight(el) + 1;
    // A wrapping element overflows downwards, a one-line (nowrap) element sideways.
    const nowrap = getComputedStyle(el).whiteSpace === 'nowrap';
    const inside = () => el.scrollHeight <= limit && fitsInline(el, nowrap);
    if (inside()) { fit.onFit?.(false); return; }
    // Cut on whitespace only: a word is never split, a line never ends half a word.
    const tokens = fit.text.split(/(\s+)/);
    let low = 0;
    let high = tokens.length;
    while (low < high) {
        const mid = Math.ceil((low + high) / 2);
        el.textContent = ellipsize(tokens.slice(0, mid).join(''));
        if (inside()) low = mid; else high = mid - 1;
    }
    el.textContent = ellipsize(tokens.slice(0, Math.max(low, 1)).join(''));
    fit.onFit?.(true);
}

/**
 * Show `text` in at most `lines` lines of `el` (0 or Infinity = the whole text). `onFit(cut)` hears after every
 * fit whether text is hidden (a `Show all` control follows it). Returns the element.
 */
export function fitText(el, text, lines, onFit = null) {
    if (!el) return el;
    const value = String(text ?? '');
    if (!lines || lines === Infinity) {
        fits.delete(el);
        observer?.unobserve?.(el);
        el.textContent = value;
        return el;
    }
    fits.set(el, { text: value, lines, width: 0, onFit });
    if (!observer && typeof ResizeObserver === 'function') {
        observer = new ResizeObserver((entries) => {
            for (const entry of entries) {
                // A removed element reports its last size change here: it leaves the shared observer with it.
                if (!entry.target.isConnected) { observer.unobserve(entry.target); fits.delete(entry.target); continue; }
                const fit = fits.get(entry.target);
                if (fit && entry.target.clientWidth !== fit.width) refit(entry.target);
            }
        });
    }
    observer?.observe?.(el);
    refit(el);
    return el;
}

/** The text a fitted element stands for (its whole text), or its own text when it is not fitted. */
export function fullText(el) {
    return fits.get(el)?.text ?? el?.textContent ?? '';
}

/** True when the element currently shows less than its whole text. */
export function isCut(el) {
    const fit = fits.get(el);
    return Boolean(fit && el.textContent !== fit.text);
}
