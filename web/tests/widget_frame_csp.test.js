import test from 'node:test';
import assert from 'node:assert/strict';

import { moduleFrameCsp, WIDGET_FRAME_ALLOW, WIDGET_FRAME_SANDBOX } from '../modules/widget_module.js';

// The decided capability set, pinned as the exact strings the frames carry
// (widgets lifecycle sprint Q13=A / Q14=B / Q16=A). Anything wider is a
// decision for the owner, not a drift.
test('framed widgets carry exactly the decided sandbox and permissions set', () => {
    assert.equal(WIDGET_FRAME_SANDBOX, 'allow-scripts allow-pointer-lock allow-downloads');
    assert.equal(WIDGET_FRAME_ALLOW, 'autoplay; fullscreen; clipboard-write');
});

test('the module document CSP spells out absolute sources for the owning skill only', () => {
    const csp = moduleFrameCsp('demo_skill', 'http://127.0.0.1:8765');
    assert.equal(csp, [
        "default-src 'none'",
        "script-src 'unsafe-inline' 'wasm-unsafe-eval' blob: http://127.0.0.1:8765/api/extensions/demo_skill/module/",
        'worker-src blob:',
        "style-src 'unsafe-inline'",
        'img-src data: blob: http://127.0.0.1:8765/api/extensions/demo_skill/',
        'media-src data: blob: http://127.0.0.1:8765/api/extensions/demo_skill/',
        'font-src data: blob: http://127.0.0.1:8765/api/extensions/demo_skill/',
    ].join('; '));
    // No scriptable network of its own: connect-src is absent (falls to
    // default-src 'none'); no plain eval; no 'self' (nothing for an opaque origin).
    assert.ok(!csp.includes('connect-src'));
    assert.ok(!csp.includes("'unsafe-eval'"));
    assert.ok(!csp.includes("'self'"));
    // The skill name is URL-encoded into the source expression like the route prefix.
    assert.ok(moduleFrameCsp('a b', 'http://h').includes('http://h/api/extensions/a%20b/module/'));
});

test('ESM resource policy admits only its mount prefix and keeps scriptable network closed', () => {
    const base = 'https://h/api/widget-assets/one/';
    const policy = moduleFrameCsp('demo', 'https://h', base);
    assert.ok(policy.includes(`style-src 'unsafe-inline' ${base}`));
    assert.ok(policy.includes(`script-src 'unsafe-inline' 'wasm-unsafe-eval' blob: ${base}`));
    assert.ok(!policy.includes('/api/extensions/') && !policy.includes('connect-src'));
    assert.ok(!policy.includes('https://h/api/widget-assets/;'));
});
