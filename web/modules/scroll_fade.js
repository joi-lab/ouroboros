/** Edge decoration follows actual hidden content, never masks a settled edge. */
export function bindScrollFade(scroller) {
    const win = scroller.ownerDocument.defaultView;
    let frame = null;
    let disposed = false;
    const update = () => {
        frame = null;
        if (disposed) return;
        scroller.toggleAttribute('data-scroll-above', scroller.scrollTop > 1);
        scroller.toggleAttribute('data-scroll-below',
            scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop > 1);
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
