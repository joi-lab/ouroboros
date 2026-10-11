import test from 'node:test';
import assert from 'node:assert/strict';
import {renderHubCard} from '../modules/utils.js';

test('failed hub operation preserves outcome and retry without claiming live computation', () => {
    const item = {slug: 'sample'};
    const options = {pending: {failed: true}, lifecycle: {label: 'Failed', hint: 'Network unavailable'}, primaryHtml: '<button>Retry</button>'};
    const failed = renderHubCard(item, options);
    assert.doesNotMatch(failed, /marketplace-working-spinner|is-working/);
    assert.match(failed, /Failed/);
    assert.match(failed, /Network unavailable/);
    assert.match(failed, /<button>Retry<\/button>/);
    assert.match(renderHubCard(item, {...options, pending: {}}), /marketplace-working-spinner/);
    assert.doesNotMatch(renderHubCard(item), /marketplace-working-spinner|is-working/);
});
