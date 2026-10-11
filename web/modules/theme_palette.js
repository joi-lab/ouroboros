/* Canvas-side half of the appearance tokens.

   Chart.js paints into a canvas, so it cannot inherit a CSS variable the way the
   DOM and Mermaid (via themeVariables) do: the chrome colours have to be copied
   into each live instance. theme.js owns the choice and announces a real repaint
   with `ouro:theme-changed`; this module is the single place that translates the
   current tokens into Chart options, so a mounted chart follows the palette
   without being rebuilt and without its data or author config being re-read. */

const CHART_FALLBACK = { text: '#a8b0bd', grid: 'rgba(255, 255, 255, 0.10)' };

/** Current chart chrome from the CSS tokens on <html>. */
export function chartChrome() {
    if (typeof getComputedStyle !== 'function' || typeof document === 'undefined') return { ...CHART_FALLBACK };
    const style = getComputedStyle(document.documentElement);
    return {
        text: style.getPropertyValue('--chart-text').trim() || CHART_FALLBACK.text,
        grid: style.getPropertyValue('--chart-grid').trim() || CHART_FALLBACK.grid,
    };
}

const branch = (parent, key) => {
    const existing = parent[key];
    if (existing && typeof existing === 'object') return existing;
    parent[key] = {};
    return parent[key];
};

/**
 * Repaint one mounted Chart instance in the current theme, keeping its data.
 *
 * `authored` is the caller's own untouched option object. Chart.js resolves
 * every scale option against its defaults, so "did the author pick this colour?"
 * cannot be read back off the instance; the caller, which still holds what it
 * was given, decides instead. Declaring `scales` or `plugins` claims that whole
 * group — a coarse rule, but one that never silently discards author colours.
 */
export function applyChartTheme(chart, authored = {}) {
    const options = chart?.options;
    if (!options) return;
    const { text, grid } = chartChrome();
    if (authored.color === undefined) options.color = text;
    if (authored.borderColor === undefined) options.borderColor = grid;
    if (authored.plugins === undefined) {
        const plugins = branch(options, 'plugins');
        branch(branch(plugins, 'legend'), 'labels').color = text;
        branch(plugins, 'title').color = text;
    }
    if (authored.scales === undefined) {
        // chart.scales holds only the scales this chart really built, so a
        // doughnut or any scale-less type is skipped instead of being handed
        // x/y axes it never asked for.
        for (const scale of Object.values(chart.scales || {})) {
            const scaleOptions = scale?.options;
            if (!scaleOptions) continue;
            branch(scaleOptions, 'ticks').color = text;
            const gridOptions = branch(scaleOptions, 'grid');
            gridOptions.color = grid;
            gridOptions.tickColor = grid;
            branch(scaleOptions, 'title').color = text;
        }
    }
    // 'none': re-tint without replaying entry animations on an already-shown chart.
    try { chart.update('none'); } catch { /* a chart destroyed mid-switch is not an error */ }
}

/** Subscribe to real theme repaints. Returns the unsubscribe the caller must own. */
export function onThemeChange(handler) {
    if (typeof window === 'undefined') return () => {};
    window.addEventListener('ouro:theme-changed', handler);
    return () => window.removeEventListener('ouro:theme-changed', handler);
}
