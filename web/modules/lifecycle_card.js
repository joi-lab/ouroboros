import { apiFetch } from './api_client.js';
const pendingBySlug = new Map();
const listeners = new Set();
let lifecyclePollTimer = null;

function notify() {
    for (const listener of Array.from(listeners)) {
        try {
            listener(pendingBySlug);
        } catch (err) {
            console.warn('lifecycle card listener failed', err);
        }
    }
}

async function tickLifecyclePoll() {
    if (pendingBySlug.size === 0) {
        lifecyclePollTimer = null;
        return;
    }
    try {
        const data = await apiFetch('/api/skills/lifecycle-queue', { cache: 'no-store' })
            .then((r) => (r.ok ? r.json() : { active: null, events: [] }))
            .catch(() => ({ active: null, events: [] }));
        const active = data?.active;
        const events = Array.isArray(data?.events) ? data.events : [];
        let hasQueuedOrRunningJob = false;
        let changed = false;
        for (const slug of Array.from(pendingBySlug.keys())) {
            const prev = pendingBySlug.get(slug) || {};
            const targets = new Set([slug, prev.target, ...(Array.isArray(prev.targets) ? prev.targets : [])].filter(Boolean));
            let job = events.find((e) => targets.has(e?.target) && (e?.status === 'running' || e?.status === 'queued'));
            if (!job && targets.has(active?.target)) job = active;
            if (!job) continue;
            if (job.status === 'running' || job.status === 'queued') {
                hasQueuedOrRunningJob = true;
            }
            const nextMsg = String(job.message || prev.message || '').trim();
            if (nextMsg && nextMsg !== prev.message) {
                pendingBySlug.set(slug, { ...prev, message: nextMsg });
                changed = true;
            }
        }
        if (changed) notify();
        if (!hasQueuedOrRunningJob) {
            lifecyclePollTimer = null;
            return;
        }
    } catch (_) {
        /* polling is best-effort */
    }
    lifecyclePollTimer = pendingBySlug.size > 0 ? setTimeout(tickLifecyclePoll, 1000) : null;
}

function ensureLifecyclePoll() {
    if (lifecyclePollTimer || pendingBySlug.size === 0) return;
    lifecyclePollTimer = setTimeout(tickLifecyclePoll, 1000);
}

export function startLifecyclePoller(onTick) {
    if (onTick) listeners.add(onTick);
    ensureLifecyclePoll();
    return () => {
        if (onTick) listeners.delete(onTick);
    };
}

export function setPending(slug, pending) {
    if (pending) pendingBySlug.set(slug, pending);
    else pendingBySlug.delete(slug);
    ensureLifecyclePoll();
    notify();
}

export function clearPending(slug) {
    setPending(slug, null);
}

export function getPending(slug) {
    return pendingBySlug.get(slug);
}

export function getPendingBySlug() {
    return pendingBySlug;
}

export function lifecycleCardClassFor(pending) {
    return pending && pending.failed !== true ? 'marketplace-card is-working' : 'marketplace-card';
}

export function lifecycleSpinnerFor(pending) {
    return pending && pending.failed !== true ? '<span class="marketplace-working-spinner" aria-hidden="true"></span>' : '';
}
