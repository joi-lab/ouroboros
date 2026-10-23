// Main-screen Update affordance (P2): one compact pill. While an update attempt runs it names the
// real stage; afterwards that attempt's last confirmed outcome or its explicit uncertainty, until
// the owner dismisses it (owner 3A, #1818); otherwise it appears when a managed update is available
// (status is populated by the boot-time check-on-restart). The pill is a pointer, not a second apply
// surface: clicking it opens Dashboard -> Updates, where the ONE apply flow (with its verified
// preflight and typed outcomes) lives. The former pill-local staged dialog was removed (owner
// decision 2026-08-31) so two hand-rolled apply controllers cannot drift apart again.

import { apiClient, updateStrategyForPlan } from './api_client.js';

// Fail-soft wrapper around the api_client update helpers (the pill must never throw the app).
async function safe(fn) {
    try {
        return await fn();
    } catch {
        return null;
    }
}

// The capsule says one thing (owner Q6=A): the versions and commit hashes stay in Dashboard → Updates and in
// the capsule's title, where updatePillDetail puts them.
export function updatePillText() {
    return 'Update available';
}

export function updatePillDetail(status = {}) {
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

// The executor's real stages (no percentages or timers): the panel headline, and the pill's shorter word.
export const UPDATE_STAGE_LABELS = {
    draining_writers: 'Finishing current work…',
    stopping_workers: 'Stopping worker processes…',
    stopping_services: 'Stopping services…',
    preparing: 'Preparing the update…',
    applying: 'Applying the update…',
    checking: 'Checking the update and dependencies…',
    rolling_back: 'Restoring the previous version…',
    restart_requested: 'Restart requested…',
};

const PILL_STAGES = {
    draining_writers: 'Finishing work…',
    stopping_workers: 'Stopping work…',
    stopping_services: 'Stopping services…',
    preparing: 'Preparing update…',
    applying: 'Applying update…',
    checking: 'Checking update…',
    rolling_back: 'Restoring version…',
    restart_requested: 'Restart requested…',
};

// Only the server concludes an attempt; a reconnect, a version or a SHA never does.
const OUTCOMES = {
    applied: { tone: 'ok', pill: 'Update completed', headline: 'Update completed.' },
    not_applied: { tone: 'error', pill: 'Update not applied', headline: 'No update was applied.' },
    rolled_back: { tone: 'error', pill: 'Previous version restored',
        headline: 'The update failed; the previous version was restored.' },
    failed: { tone: 'error', pill: 'Update failed', headline: 'The update did not complete.' },
    unknown: { tone: 'neutral', pill: 'Update outcome unconfirmed', headline: 'The last update outcome is unconfirmed.' },
};

const UNKNOWN_REASONS = {
    startup_unconfirmed: 'The restarted server did not confirm its startup checks.',
    running_source_mismatch: 'The running version is not the commit this update landed.',
    record_unreadable: 'The saved record of the last attempt could not be read.',
    state_unreadable: 'The saved state could not be read, so the last attempt is unknown.',
};

const REFUSALS = {
    release_moved: 'The update changed after it was prepared.',
    late_local_changes: 'Local files changed while the update was starting.',
    replace_not_applied: 'Git refused the first checkout step.',
};

function attemptText(progress) {
    const blockers = Array.isArray(progress.blockers) ? progress.blockers.map(String) : [];
    // Only the drain refusal names active writers, and it returns before any handoff or teardown.
    const busy = blockers.length > 0 && blockers.every((item) => item.startsWith('active:'));
    const parts = [];
    if (progress.outcome === 'not_applied') {
        parts.push(busy ? `Work was still finishing: ${blockers.map((item) => item.slice('active:'.length)).join(', ')}.`
            : REFUSALS[progress.reason] || progress.error);
        // Owner 4A: only when that attempt's own executor proved it reached no teardown.
        if (busy && progress.work_stop === 'none' && progress.concluded_by === 'executor') {
            parts.push('This attempt stopped no running work.');
        }
        if (busy) parts.push('Try again when it finishes.');
    } else if (progress.outcome === 'unknown') {
        parts.push(UNKNOWN_REASONS[progress.reason]
            || 'No completion record arrived; the running version alone does not establish the outcome.');
    } else if (progress.outcome === 'applied') {
        parts.push('The restarted server confirmed this update.');
    } else {
        parts.push(progress.error);
    }
    if (progress.work_stop === 'invoked') parts.push('Running work may have been interrupted.');
    if (progress.restart_required && progress.current_generation !== false) {
        parts.push('Ouroboros needs a restart before updates can continue.');
    }
    if (progress.keep_branch) parts.push(`Your local commits are kept on branch ${progress.keep_branch}.`);
    if (progress.stash_note) parts.push(progress.stash_note);
    return parts.filter(Boolean).join(' ');
}

function attemptDetails(progress, outcome) {
    const rows = [];
    const add = (label, value) => {
        if (value) rows.push({ label, value: String(value) });
    };
    add('Base', String(progress.base_sha || '').slice(0, 12));
    add('Target', String(progress.target_sha || '').slice(0, 12));
    add('Stage', progress.stage || (outcome ? 'not started' : ''));
    add('Outcome', outcome);
    add('Reason', progress.reason);
    add('Blocked by', (Array.isArray(progress.blockers) ? progress.blockers : []).join(', '));
    add('Error', progress.error);
    add('Saved local work', progress.keep_branch);
    add('Concluded by', { executor: 'this update', boot: 'the restarted server' }[progress.concluded_by]);
    return rows;
}

/**
 * One reading of the retained attempt for the pill and the panel: the stage while it runs, then the
 * server's conclusion or its named uncertainty. `null` when there is no attempt to show.
 */
export function updateAttemptView(progress) {
    if (!progress || typeof progress !== 'object' || !progress.operation_id) return null;
    // A record without the outcome field (an older shape) that failed is still a failure, never a stage.
    const key = progress.outcome || (!progress.active && progress.result === 'failed' ? 'failed' : '');
    const base = {
        id: String(progress.operation_id),
        when: String(progress.outcome_at || progress.stage_started_at || progress.started_at || ''),
        details: attemptDetails(progress, key),
    };
    const retention = progress.retention_unconfirmed
        ? 'This observation could not be saved and may be unavailable after restart.' : '';
    const outcome = OUTCOMES[key];
    if (outcome && !progress.active) {
        return { ...base, terminal: true, tone: outcome.tone, pill: outcome.pill, headline: outcome.headline,
            text: [attemptText({ ...progress, outcome: key }), retention].filter(Boolean).join(' ') };
    }
    // Not concluded: this server's stage, a restart it needs, a reviewed resolution, or a later server's checks.
    const stage = String(progress.stage || '');
    const here = progress.active || progress.current_generation !== false;
    const [pill, headline] = progress.result === 'assisted_started'
        ? ['Resolving update…', 'A reviewed resolution owns this update.']
        : progress.result === 'restart_required' && here
            ? ['Restart needed', 'The update needs a restart to finish.']
            : here
                ? [PILL_STAGES[stage] || 'Updating…', UPDATE_STAGE_LABELS[stage] || 'Updating…']
                : ['Confirming update…', 'Waiting for the restarted server to confirm the update…'];
    return { ...base, terminal: false, tone: 'neutral', pill, headline, text: retention };
}

export function attemptTime(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime())) return '';
    return new Intl.DateTimeFormat('en-GB', {
        day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false,
    }).format(date);
}

// One bounded per-browser preference: the one terminal attempt dismissed from the pill. It is never
// sent anywhere and changes neither the server's record nor the panel.
const DISMISSED_KEY = 'ouroboros.update-attempt-dismissed';

export function dismissedUpdateAttempt() {
    try {
        return globalThis.localStorage?.getItem(DISMISSED_KEY) || '';
    } catch {
        return '';
    }
}

export function dismissUpdateAttempt(id) {
    try {
        globalThis.localStorage?.setItem(DISMISSED_KEY, String(id || ''));
    } catch {
        // Storage refused: the pill keeps showing the result, which is the safe side.
    }
    globalThis.window?.dispatchEvent?.(new CustomEvent('ouro:update-attempt-dismissed'));
}

export function initUpdateStatus({ showPage, openDashboardTab, ws, readStatus = () => apiClient.updateStatus() } = {}) {
    let latest = null;

    let ownedPill = null;
    function ensurePill() {
        let pill = document.getElementById('update-pill');
        if (!pill) {
            pill = document.createElement('button');
            ownedPill = pill;
            pill.id = 'update-pill';
            pill.type = 'button';
            pill.className = 'btn btn-default btn-sm update-pill';
            pill.hidden = true;
            pill.addEventListener('click', () => {
                if (openDashboardTab) openDashboardTab('updates');
                else showPage?.('dashboard');
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
        const attempt = updateAttemptView(status?.update_progress);
        const shown = attempt && !(attempt.terminal && attempt.id === dismissedUpdateAttempt());
        // Content or uncommitted work of the checkout's own is a caveat about an AVAILABLE update, not a
        // count of commits ahead; while an attempt shows, its stage or outcome is the pill's one fact.
        pill.classList.toggle('has-local', Boolean(!shown && status?.available
            && (status.dirty || status.local_changes !== 'absent')));
        if (shown) {
            pill.textContent = attempt.pill;
            pill.title = [attempt.headline, attempt.text].filter(Boolean).join(' ');
            pill.dataset.tone = attempt.tone;
            pill.hidden = false;
            return;
        }
        delete pill.dataset.tone;
        if (!status || !status.available) {
            pill.hidden = true;
            return;
        }
        pill.textContent = updatePillText(status);
        pill.title = updatePillDetail(status);
        pill.hidden = false;
    }

    // The panel's coalescing (updates.js loadStatus): one read in flight; a newer notice owns the next read.
    let reading = null;
    let readAgain = false;
    let disposed = false;
    function refresh() {
        readAgain = true;
        if (reading) return reading;
        reading = (async () => {
            while (readAgain && !disposed) {
                readAgain = false;
                const status = await safe(readStatus);
                if (readAgain || disposed) continue; // superseded: never render an older answer
                if (status) latest = status; // a failed read hides nothing already known
                renderPill(latest);
            }
        })().finally(() => { reading = null; });
        return reading;
    }

    const disposers = [];
    const listen = (event, handler) => {
        const dispose = ws?.on?.(event, handler);
        if (typeof dispose === 'function') disposers.push(dispose);
    };
    listen('update_status_ready', refresh);
    listen('update_progress_changed', refresh);
    listen('open', (event = {}) => {
        if (event.previouslyConnected === true) refresh();
    });
    const onDismissed = () => renderPill(latest);
    const onPageShown = (event) => {
        if (event?.detail?.page === 'chat') refresh();
    };
    window.addEventListener('ouro:update-attempt-dismissed', onDismissed);
    window.addEventListener('ouro:page-shown', onPageShown);
    refresh();

    return {
        refresh,
        dispose() {
            disposed = true;
            disposers.splice(0).forEach((dispose) => dispose());
            window.removeEventListener('ouro:update-attempt-dismissed', onDismissed);
            window.removeEventListener('ouro:page-shown', onPageShown);
            ownedPill?.remove();
        },
    };
}
