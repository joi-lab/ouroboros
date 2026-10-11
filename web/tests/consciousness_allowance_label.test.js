import test from 'node:test';
import assert from 'node:assert/strict';
import { allowanceLabel } from '../modules/utils.js';

test('an absent allowance number stays absent, never a $0.00 receipt', () => {
    assert.equal(allowanceLabel(null, 20), '');
    assert.equal(allowanceLabel(undefined, 20), '');
    assert.equal(allowanceLabel(2.5, null), '');
    assert.equal(allowanceLabel('abc', 20), '');
    assert.equal(allowanceLabel(0, 20), '$0.00 / $20.00');  // a real zero is a fact
});

test('unmetered rows print the spend as a floor and a quarantined ledger says so', () => {
    assert.equal(allowanceLabel(5, 20), '$5.00 / $20.00');
    assert.equal(allowanceLabel(5, 20, 2), '≥ $5.00 / $20.00');
    assert.equal(allowanceLabel(5, 20, 0, true), '$5.00 / $20.00 (ledger integrity degraded)');
    assert.equal(allowanceLabel(5, 20, 1, true), '≥ $5.00 / $20.00 (ledger integrity degraded)');
});
