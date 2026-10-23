// Fit a text into N lines of its element (docs/DESIGN.md "Replies"; the portal card's latest line). A CSS
// line clamp draws its ellipsis wherever the box ends, so a clamp that lands right after a full stop reads
// `редизайна.…` (four dots). This cuts on a word, drops the punctuation the cut leaves at the end, and only
// then adds the ellipsis. The full text stays on record (`fullText`) for copy, expansion and re-fitting; an
// element with no layout (a hidden page, a test DOM) simply shows the whole text and is fitted once it has
// a width. One ResizeObserver serves every fitted element; a width change re-fits on the next frame (a fit changes the
// element's own size, so a fit inside the observer's callback could loop in one frame: WebKit reports that as an
// error), a height change does not, and an element removed from the page leaves the observer (no element is kept
// alive by it). That frame's re-fit is heard (`onRefit`): a room places its reader again once its lines changed size
// (Main shown again had laid out its newest message before its lines were fitted at their width). Elements fitted together are fitted in lockstep: every element's probe of a round is written before
// any is read, so a batch costs a few layouts of the page however many lines it holds (fitted one by one, every probe
// laid out the whole page: a long Main whose hundred work lines changed behind a page froze for seconds on return).
const fits = new WeakMap();  // element -> { text, lines, width, onFit }
let observer = null;
const widened = new Set();  // elements whose width changed, fitted on the next frame
let frame = 0;
const refitted = new Set();  // who hears a frame's re-fit

/** `listener(elements)` hears the elements each frame re-fitted after their width changed; returns its disposer. */
export function onRefit(listener) {
    refitted.add(listener);
    return () => refitted.delete(listener);
}

const TAIL = /[\s.,;:!?…—–-]+$/u;

/** The words before a cut, without the punctuation or space the cut leaves at their end, plus `…`. */
export function ellipsize(prefix) {
    return `${String(prefix ?? '').replace(TAIL, '')}…`;
}

function lineHeight(style) {
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

// A wrapping element overflows downwards, a one-line (nowrap) element sideways.
const inside = (job) => job.el.scrollHeight <= job.limit && fitsInline(job.el, job.nowrap);

function refit(els) {
    const jobs = [];
    const heard = [];  // onFit is called after the last read: a caller's own write would lay the page out again
    for (const el of els) {
        const fit = fits.get(el);
        if (!fit) continue;
        if (!el.isConnected || !el.clientWidth) {
            if (el.textContent !== fit.text) el.textContent = fit.text;
            heard.push([fit, false]);
            continue;
        }
        const style = getComputedStyle(el);
        jobs.push({ el, fit, width: el.clientWidth, limit: fit.lines * lineHeight(style) + 1, nowrap: style.whiteSpace === 'nowrap' });
    }
    for (const job of jobs) { job.fit.width = job.width; job.el.textContent = job.fit.text; }
    let open = [];
    for (const job of jobs) {
        if (inside(job)) { heard.push([job.fit, false]); continue; }
        // Cut on whitespace only: a word is never split, a line never ends half a word.
        job.tokens = job.fit.text.split(/(\s+)/);
        job.low = 0;
        job.high = job.tokens.length;
        open.push(job);
    }
    const cut = open.slice();
    while (open.length) {
        for (const job of open) {
            job.mid = Math.ceil((job.low + job.high) / 2);
            job.el.textContent = ellipsize(job.tokens.slice(0, job.mid).join(''));
        }
        for (const job of open) {
            if (inside(job)) job.low = job.mid; else job.high = job.mid - 1;
        }
        open = open.filter((job) => job.low < job.high);
    }
    for (const job of cut) {
        job.el.textContent = ellipsize(job.tokens.slice(0, Math.max(job.low, 1)).join(''));
        heard.push([job.fit, true]);
    }
    for (const [fit, wasCut] of heard) fit.onFit?.(wasCut);
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
                if (fit && entry.target.clientWidth !== fit.width) widened.add(entry.target);
            }
            if (!widened.size || frame) return;
            const later = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (fn) => setTimeout(fn, 0);
            frame = later(() => {
                frame = 0;
                const due = [...widened];
                widened.clear();
                refit(due);
                for (const listener of refitted) listener(due);
            });
        });
    }
    observer?.observe?.(el);
    refit([el]);
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
