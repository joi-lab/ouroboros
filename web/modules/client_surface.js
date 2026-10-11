/** Owner Surface Fact: raw sending-surface observables, measured AT SEND TIME
 * (the pywebview JS bridge appears asynchronously after page load, so a
 * load-time snapshot could misclassify the desktop shell as a browser tab).
 * Deliberately no device taxonomy — the model classifies from raw facts. */
/** Spread-ready chat-frame field: `{}` when the snapshot failed. */
export function clientSurfaceField() {
    const snap = clientSurfaceSnapshot();
    return snap ? { client_surface: snap } : {};
}

export function clientSurfaceSnapshot() {
    try {
        const snapshot = {
            pywebview: Boolean(window.pywebview?.api),
            ua: String(navigator.userAgent || ''),
            viewport: { w: Number(window.innerWidth) || 0, h: Number(window.innerHeight) || 0 },
            narrow_layout: window.matchMedia('(max-width: 980px)').matches,
            coarse_pointer: window.matchMedia('(pointer: coarse)').matches,
            captured_at: new Date().toISOString(),
        };
        // The sender's IANA zone: the host names it beside UTC in Main's clock line
        // (owner 7A). A browser that cannot report one sends none — never a guess.
        const timezone = sendingTimeZone();
        if (timezone) snapshot.timezone = timezone;
        return snapshot;
    } catch {
        return null;  // absence is an honest gap; never a guessed default
    }
}

function sendingTimeZone() {
    try {
        const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
        return typeof zone === 'string' ? zone : '';
    } catch {
        return '';
    }
}
