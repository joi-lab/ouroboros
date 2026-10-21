/** Edge decoration follows actual hidden content, never masks a settled edge. */
export function bindScrollFade(scroller, { axis = 'y' } = {}) {
    const win = scroller.ownerDocument?.defaultView;
    // Decoration only: a host without the observers (a unit-test DOM) keeps the strip undecorated.
    if (!win?.ResizeObserver || !win.MutationObserver || !win.requestAnimationFrame) return () => {};
    let frame = null;
    let disposed = false;
    const update = () => {
        frame = null;
        if (disposed) return;
        const horizontal = axis === 'x';
        const position = horizontal ? Math.abs(scroller.scrollLeft) : scroller.scrollTop;
        const extent = horizontal ? scroller.scrollWidth - scroller.clientWidth
            : scroller.scrollHeight - scroller.clientHeight;
        scroller.toggleAttribute(horizontal ? 'data-scroll-start' : 'data-scroll-above', position > 1);
        scroller.toggleAttribute(horizontal ? 'data-scroll-end' : 'data-scroll-below', extent - position > 1);
    };
    const schedule = () => {
        if (!disposed && frame === null) frame = win.requestAnimationFrame(update);
    };
    const resize = new win.ResizeObserver(schedule);
    const observeContent = () => {
        resize.disconnect();
        resize.observe(scroller);
        for (const child of scroller.children) resize.observe(child);
        schedule();
    };
    // Direct child replacement changes the observed boxes; text/late media
    // within them changes their size and is covered by ResizeObserver.
    const mutations = new win.MutationObserver(observeContent);
    mutations.observe(scroller, { childList: true });
    scroller.addEventListener('scroll', schedule, { passive: true });
    observeContent();
    return () => {
        disposed = true;
        if (frame !== null) win.cancelAnimationFrame(frame);
        resize.disconnect();
        mutations.disconnect();
        scroller.removeEventListener('scroll', schedule);
    };
}
