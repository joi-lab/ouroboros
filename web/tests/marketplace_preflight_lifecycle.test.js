import test from 'node:test';
import assert from 'node:assert/strict';

import { lifecycleFor, staleRepairSecondaryHtml } from '../modules/marketplace.js';

// #335: a preflight-failed install must offer Repair in the marketplace
// lifecycle, not a Re-review that deterministically fails the same way.

function installedWithPreflightFail() {
    return {
        name: 'minecraft-widget',
        review_status: 'pending',
        review_stale: false,
        review_gate: { executable_review: false, preflight_failed: true },
        executable_review: false,
        review_findings: [{
            item: 'skill_preflight', verdict: 'FAIL', severity: 'critical',
            reason: JSON.stringify({
                manifest: [{ item: 'manifest_entry_exists', ok: false, detail: 'missing or escaping entry: plugin.py' }],
                files: [], ok: false,
            }),
        }],
        grants: { all_granted: true },
    };
}

test('preflight-failed install offers Repair with the human diagnosis', () => {
    const lifecycle = lifecycleFor({}, installedWithPreflightFail(), null);
    assert.equal(lifecycle.action, 'fix');
    assert.equal(lifecycle.button, 'Repair and run');
    assert.equal(lifecycle.tone, 'danger');
    assert.equal(lifecycle.label, 'Preflight failed');
    assert.match(lifecycle.hint, /missing or escaping entry: plugin\.py/);
});

test('plain pending install keeps the Review action', () => {
    const installed = installedWithPreflightFail();
    installed.review_gate = { executable_review: false };
    installed.review_findings = [];
    const lifecycle = lifecycleFor({}, installed, null);
    assert.equal(lifecycle.action, 'review');
    assert.equal(lifecycle.button, 'Review');
});

test('a stale recorded FAIL keeps Re-review primary with the recorded diagnosis (D11)', () => {
    const installed = installedWithPreflightFail();
    installed.review_stale = true;
    installed.review_gate = {
        executable_review: false, preflight_failed: false, preflight_failed_stale: true,
    };
    const lifecycle = lifecycleFor({}, installed, null);
    assert.equal(lifecycle.action, 'review');
    assert.equal(lifecycle.button, 'Re-review');
    assert.equal(lifecycle.label, 'Review stale');
    assert.match(lifecycle.hint, /Last recorded preflight/);
    assert.match(lifecycle.hint, /missing or escaping entry: plugin\.py/);
});

test('stale-Repair secondary renders only while no lifecycle work is pending', () => {
    const installed = installedWithPreflightFail();
    installed.review_stale = true;
    installed.review_gate = {
        executable_review: false, preflight_failed: false, preflight_failed_stale: true,
    };
    // No pending lifecycle job -> the secondary Repair button renders.
    const html = staleRepairSecondaryHtml('minecraft-widget', installed, null);
    assert.match(html, /data-mp-action="fix"/);
    assert.match(html, /data-slug="minecraft-widget"/);
    assert.match(html, />Repair and run</);
    assert.match(html, /based on the last recorded preflight/);
    // A queued/running lifecycle job suppresses it (the primary's pending
    // discipline): no concurrent repair while other work runs.
    assert.equal(staleRepairSecondaryHtml('minecraft-widget', installed, { label: 'Working' }), '');
    // Fresh FAIL or no recorded fact -> the secondary never renders.
    const fresh = installedWithPreflightFail();
    assert.equal(staleRepairSecondaryHtml('minecraft-widget', fresh, null), '');
    assert.equal(staleRepairSecondaryHtml('minecraft-widget', null, null), '');
});

test('a stale review without a recorded FAIL keeps the generic stale branch', () => {
    const installed = installedWithPreflightFail();
    installed.review_stale = true;
    installed.review_gate = {
        executable_review: false, preflight_failed: false, preflight_failed_stale: false,
    };
    installed.review_findings = [];
    const lifecycle = lifecycleFor({}, installed, null);
    assert.equal(lifecycle.action, 'review');
    assert.equal(lifecycle.button, 'Re-review');
    assert.doesNotMatch(String(lifecycle.hint || ''), /Last recorded preflight/);
});
