// Main-screen Update affordance (P2): a compact pill that appears when a managed
// update is available (status is populated by the boot-time check-on-restart).
// The pill is a pointer, not a second apply surface: clicking it opens
// Dashboard -> Updates, where the ONE apply flow (with its verified preflight
// and typed outcomes) lives. The former pill-local staged dialog was removed
// (owner decision 2026-08-31) so two hand-rolled apply controllers cannot
// drift apart again.

import { apiClient, updateStrategyForPlan } from './api_client.js';

// Fail-soft wrapper around the api_client update helpers (the pill must never throw the app).
async function safe(fn) {
    try {
        return await fn();
    } catch {
        return null;
    }
}

export function updatePillText(status = {}) {
    const currentVersion = String(status.current_version || '');
    const latestVersion = String(status.latest_version || '');
    const currentSha = String(status.current_short_sha || status.current_sha || '').slice(0, 8);
    const latestSha = String(status.latest_short_sha || status.latest_sha || '').slice(0, 8);
    if (currentVersion && latestVersion && currentVersion === latestVersion) {
        return currentSha && latestSha
            ? `Update ${currentSha} → ${latestSha}`
            : `Update available${latestSha ? ` · ${latestSha}` : ''}`;
    }
    const current = currentVersion || currentSha;
    const latest = latestVersion || latestSha;
    return current && latest ? `Update ${current} → ${latest}` : 'Update available';
}

// Shared preflight verification for the ONE apply surface (consumed by
// web/modules/updates.js): never lets an unverified plan reach updateApply.
export function verifiedUpdatePlan(preflight) {
    const plan = preflight?.merge_plan;
    if (!plan || typeof plan !== 'object') return null;
    const strategy = updateStrategyForPlan(plan);
    if (
        !strategy
        || !plan.base_sha
        || !plan.target_sha
        || !Number.isInteger(plan.local_dirty_count)
        || plan.local_dirty_count < 0
    ) return null;
    return { plan, strategy };
}

export function initUpdateStatus({ showPage, openDashboardTab, ws } = {}) {
    function ensurePill() {
        let pill = document.getElementById('update-pill');
        if (!pill) {
            pill = document.createElement('button');
            pill.id = 'update-pill';
            pill.type = 'button';
            pill.className = 'update-pill';
            pill.hidden = true;
            pill.addEventListener('click', () => {
                showPage?.('dashboard');
                openDashboardTab?.('updates');
            });
            const anchor = document.getElementById('nav-version');
            if (anchor && anchor.parentNode) {
                anchor.parentNode.insertBefore(pill, anchor.nextSibling);
            } else {
                document.body.appendChild(pill);
            }
        }
        return pill;
    }

    function renderPill(status) {
        const pill = ensurePill();
        if (!status || !status.available) {
            pill.hidden = true;
            return;
        }
        pill.textContent = updatePillText(status);
        pill.classList.toggle('has-local', Boolean(status.dirty || status.ahead));
        pill.hidden = false;
    }

    async function refresh() {
        renderPill(await safe(() => apiClient.updateStatus()));
    }

    refresh();
    ws?.on?.('update_status_ready', refresh);
    window.addEventListener('ouro:page-shown', (event) => {
        if (event?.detail?.page === 'chat') refresh();
    });

    return { refresh };
}
