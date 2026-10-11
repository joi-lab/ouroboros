import assert from 'node:assert/strict';
import test from 'node:test';

import { topReviewFinding } from '../modules/utils.js';

test('the compact review hint discloses how many further findings it hides', () => {
    const one = topReviewFinding({ review_findings: [
        { verdict: 'FAIL', item: 'writes outside payload', reason: 'escapes the bucket' },
    ] });
    assert.equal(one, 'FAIL writes outside payload: escapes the bucket');

    const three = topReviewFinding({ review_findings: [
        { verdict: 'FAIL', item: 'writes outside payload', reason: 'escapes the bucket' },
        { verdict: 'WARN', item: 'b', reason: 'r' },
        { verdict: 'WARN', item: 'c', reason: 'r' },
    ] });
    assert.equal(three, 'FAIL writes outside payload: escapes the bucket (+2 more)');

    assert.equal(topReviewFinding({ review_findings: [] }), '');
});
