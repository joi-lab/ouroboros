// The blocking overlay frames the wizard in a SANDBOXED iframe, and the Agents
// step's primary action is the agent's own sign-in link opened in a new tab.
// Those two facts have to agree: without `allow-popups` the browser blocks that
// click SILENTLY — the owner presses "Open sign-in link" and nothing happens.
//
// This is asserted BEHAVIOURALLY, not as a string match on the attribute: a
// miniature browser applies the real sandbox rules to a window.open() coming
// from that frame, over the overlay's OWN exported policy constant, and the URL
// opened is the one the real login card rendered for a real device-code
// disclosure.
// If the card stops opening a new context the requirement relaxes on its own;
// if the overlay drops the token, the click stops working here exactly as it
// stopped working for the owner.

import assert from 'node:assert/strict';
import test from 'node:test';

import { loginCardHtml } from '../modules/harness_login_cards.js';
import { FRAME_SANDBOX } from '../modules/onboarding_overlay.js';

// --- the sandbox rule, as a browser applies it -----------------------------

const SANDBOX_TOKEN_OPENS_POPUPS = 'allow-popups';

function framedWindowOpen(sandboxTokens, url) {
    // A sandboxed browsing context may only open a new one when its sandbox
    // grants `allow-popups`. A browser reports nothing when it refuses — which
    // is exactly why this defect was invisible.
    if (!sandboxTokens.includes(SANDBOX_TOKEN_OPENS_POPUPS)) return null;
    const inheritsSandbox = !sandboxTokens.includes('allow-popups-to-escape-sandbox');
    return { url, sandboxed: inheritsSandbox };
}

function overlaySandboxTokens() {
    // The policy is a NAMED constant the overlay applies to the frame, not a
    // token buried in markup, so this reads the exported value rather than
    // regexing the module text: a source scrape would pass over a constant that
    // the module then forgot to apply, and fail over markup that merely moved.
    // `showSetupFrame` applying it is pinned by onboarding_overlay.test.js.
    assert.ok(FRAME_SANDBOX, 'the overlay must frame the wizard in a sandboxed iframe');
    return String(FRAME_SANDBOX).split(/\s+/).filter(Boolean);
}

// --- the actual proof ------------------------------------------------------

test('the framed wizard can actually open the sign-in link the login card renders', () => {
    const tokens = overlaySandboxTokens();

    // The REAL card, for a real device-code disclosure, is what supplies the URL
    // and the fact that it opens a new browsing context.
    const signInUrl = 'https://claude.ai/oauth/authorize?code=abc123';
    const card = loginCardHtml({
        harness: 'claude',
        envelope: { job: { state: 'running', phase: 'awaiting_user' },
            deviceCode: { flow: 'oauth_url', verificationUrl: signInUrl, userCode: '' } },
    }, Date.now());

    const anchor = card.match(/<a[^>]*data-open-signin[^>]*>/);
    assert.ok(anchor, 'the card must render the sign-in link as its primary action');
    assert.match(anchor[0], /target="_blank"/, 'the link opens a NEW browsing context');
    assert.ok(card.includes(signInUrl), 'the disclosed URL reaches the anchor');

    // Now perform that click from inside the framed wizard.
    const opened = framedWindowOpen(tokens, signInUrl);
    assert.ok(opened, 'the sandboxed wizard could not open the sign-in link — '
        + 'the owner would click "Open sign-in link" and see nothing happen');
    assert.equal(opened.url, signInUrl);
    // And the vendor page must not inherit the wizard's sandbox: an OAuth page
    // with neither same-origin nor scripts cannot complete a sign-in.
    assert.equal(opened.sandboxed, false,
        'the sign-in window inherited the wizard sandbox; no OAuth flow survives that');

    // The frame still keeps the capabilities the wizard itself needs, and the
    // overlay still mounts one.
    for (const needed of ['allow-same-origin', 'allow-scripts', 'allow-forms']) {
        assert.ok(tokens.includes(needed), needed);
    }
    // That the overlay actually APPLIES this policy to the frame it mounts is
    // pinned by onboarding_overlay.test.js, which drives the real
    // initOnboardingOverlay() and reads the mounted iframe's attribute. Scraping
    // the module text for a literal here would only re-test the markup shape.
});

test('a sandbox without allow-popups reproduces the silent block', () => {
    // The negative control: the same click, under the sandbox as it was, opens
    // nothing at all — and throws no error, which is why nobody saw it.
    const before = ['allow-same-origin', 'allow-scripts', 'allow-forms'];
    assert.equal(framedWindowOpen(before, 'https://claude.ai/oauth/authorize'), null);
});
