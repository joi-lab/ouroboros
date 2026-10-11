import assert from 'node:assert/strict';
import test from 'node:test';

import { storedOrFallback } from '../modules/settings.js';

// Settings GET serves the server's typed read, so a saved allowance of 0 arrives
// as the number 0. Absence is undefined, null or '' — never a stored 0.
test('a stored zero keeps its value instead of the fallback', () => {
    assert.equal(storedOrFallback(0, '20'), 0);
    assert.equal(storedOrFallback('0', '20'), '0');
    assert.equal(storedOrFallback(5, '20'), 5);
});

test('only an absent value takes a non-empty fallback', () => {
    for (const absent of [undefined, null, '']) assert.equal(storedOrFallback(absent, '20'), '20');
});

test('a row without a fallback shows exactly what is stored', () => {
    for (const stored of [undefined, null, '', 0, false, 'x']) assert.equal(storedOrFallback(stored, ''), stored);
});
