import test from 'node:test';
import assert from 'node:assert/strict';
import { renderSkillIcon } from '../modules/page_icons.js';

test('named and missing icons keep monochrome house geometry and decorative semantics', () => {
    assert.match(renderSkillIcon('mail'), /currentColor/);
    assert.match(renderSkillIcon({ kind: 'host', name: 'mail' }), /aria-hidden="true"/);
    assert.notEqual(renderSkillIcon('mail'), renderSkillIcon({}));
    assert.equal(renderSkillIcon('missing'), renderSkillIcon({}));
    assert.match(renderSkillIcon({}, { className: '" onload="x', size: 18 }), /class="&quot; onload=&quot;x"/);
    assert.match(renderSkillIcon({ kind: 'svg', svg: '<svg onload="x"/>' }), /<path/);
});

test('projected SVG passes the geometry allowlist at the DOM sink', () => {
    const svg = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M1 2L3 4"/></svg>';
    let options;
    globalThis.DOMPurify = { sanitize(value, config) { options = config; return value; } };
    try {
        assert.match(renderSkillIcon({ kind: 'svg', svg }), /M1 2L3 4/);
        assert.ok(options.ALLOWED_TAGS.includes('path'));
        assert.ok(!options.ALLOWED_TAGS.includes('script'));
        assert.ok(!options.ALLOWED_ATTR.includes('href'));
        assert.equal(options.ALLOW_DATA_ATTR, false);
    } finally { delete globalThis.DOMPurify; }
});
