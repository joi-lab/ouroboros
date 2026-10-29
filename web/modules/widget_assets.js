/* The parent controls lease lifetime; the frame can read its URL but cannot use these controls. */
import { apiFetch } from './api_client.js';
import { WIDGET_REQUEST_TIMEOUT_MS } from './widget_job.js';

const controls = '/api/widgets/asset-leases';

export async function openWidgetAssetLease(tab, signal) {
    const response = await apiFetch(controls, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal,
        body: JSON.stringify({ skill: tab.skill, tab_id: tab.tab_id, generation: tab.generation }),
    });
    if (!response.ok) throw new Error('widget publication unavailable');
    const lease = await response.json();
    return { ...lease, baseUrl: new URL(lease.base_url, window.location.origin).href };
}

// One declared file, read with the lease alone. Its bytes were captured at
// publication (MAX_WIDGET_BYTES bounds the whole set), so a save may hold them
// as a Blob; the desktop URL bridge admits only its own API prefixes.
export async function readWidgetAsset(url) {
    const response = await apiFetch(url, { credentials: 'omit', cache: 'no-store', redirect: 'error' });
    if (!response.ok) throw new Error(`widget file unavailable: HTTP ${response.status}`);
    return response.blob();
}

export function revokeWidgetAssetLease(lease) {
    if (!lease) return;
    // Keepalive permits navigation cleanup; a crash or a lost request instead
    // leaves at most expires_in seconds since the last accepted renewal.
    return apiFetch(`${controls}/revoke`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ token: lease.token }),
    }).catch(() => {});
}

export function maintainWidgetAssetLease(lease, lost) {
    let stopped = false;
    let renewal = 0;
    let expiry = 0;
    let controller = null;
    const expire = () => { if (!stopped) lost(); };
    const arm = (reply) => {
        clearTimeout(expiry);
        expiry = setTimeout(expire, reply.expires_in * 1000);
        renewal = setTimeout(renew, reply.renew_after * 1000);
    };
    const renew = async () => {
        if (stopped) return;
        controller = new AbortController();
        const timer = setTimeout(() => controller?.abort(), WIDGET_REQUEST_TIMEOUT_MS);
        try {
            const response = await apiFetch(`${controls}/renew`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
                body: JSON.stringify({ token: lease.token }),
            });
            if (!response.ok) throw new Error('widget lease ended');
            const reply = await response.json();
            if (!stopped) arm(reply);
        } catch { expire(); }
        finally { clearTimeout(timer); controller = null; }
    };
    const release = () => {
        if (stopped) return;
        stopped = true;
        clearTimeout(renewal);
        clearTimeout(expiry);
        controller?.abort();
        window.removeEventListener('pagehide', onPageHide);
        revokeWidgetAssetLease(lease);
    };
    const onPageHide = () => { release(); lost(); };
    window.addEventListener('pagehide', onPageHide);
    arm(lease);
    return release;
}
