import test from 'node:test';
import assert from 'node:assert/strict';

// Executable coverage for the send-time snapshot module (PLAN §9.7.26): the
// Python-side substring pins cannot catch a broken wire SHAPE — this runs the
// real code under a mocked browser environment.

function mockWindow({ pywebviewApi = undefined, narrow = false, coarse = false } = {}) {
    globalThis.window = {
        pywebview: pywebviewApi === undefined ? undefined : { api: pywebviewApi },
        innerWidth: 1234,
        innerHeight: 777,
        matchMedia: (query) => ({
            matches: query.includes('max-width') ? narrow : coarse,
        }),
    };
}

if (typeof navigator === 'undefined') {
    globalThis.navigator = { userAgent: 'node-test/1.0' };
}

test('snapshot measures raw observables at call time', async () => {
    mockWindow({ narrow: true, coarse: false });
    const { clientSurfaceSnapshot } = await import('../modules/client_surface.js');
    const snap = clientSurfaceSnapshot();
    assert.equal(snap.pywebview, false);
    assert.equal(typeof snap.ua, 'string');
    assert.deepEqual(snap.viewport, { w: 1234, h: 777 });
    assert.equal(snap.narrow_layout, true);
    assert.equal(snap.coarse_pointer, false);
    assert.ok(!Number.isNaN(Date.parse(snap.captured_at)), 'captured_at must be a parseable timestamp');
    assert.equal(snap.timezone, Intl.DateTimeFormat().resolvedOptions().timeZone);
    assert.deepEqual(
        Object.keys(snap).sort(),
        ['captured_at', 'coarse_pointer', 'narrow_layout', 'pywebview', 'timezone', 'ua', 'viewport'],
        'the wire shape is a closed set — a renamed/added key breaks the backend contract silently',
    );
});

test('a browser that cannot report its zone sends none', async () => {
    mockWindow({});
    const { clientSurfaceSnapshot } = await import('../modules/client_surface.js');
    const original = Intl.DateTimeFormat;
    Intl.DateTimeFormat = () => { throw new Error('no ICU'); };
    try {
        const snap = clientSurfaceSnapshot();
        assert.ok(snap && !('timezone' in snap), 'absence stays absence, never a guessed zone');
    } finally {
        Intl.DateTimeFormat = original;
    }
});

test('pywebview bridge presence flips the fact', async () => {
    mockWindow({ pywebviewApi: {} });
    const { clientSurfaceSnapshot } = await import('../modules/client_surface.js');
    assert.equal(clientSurfaceSnapshot().pywebview, true);
});

test('field helper wraps the snapshot and stays honest on breakage', async () => {
    mockWindow({});
    const { clientSurfaceField, clientSurfaceSnapshot } = await import('../modules/client_surface.js');
    const field = clientSurfaceField();
    assert.ok(field.client_surface, 'field must wrap a live snapshot');
    assert.deepEqual(Object.keys(field), ['client_surface']);
    // A broken environment yields an honest null / empty spread, never a guess.
    globalThis.window = undefined;
    assert.equal(clientSurfaceSnapshot(), null);
    assert.deepEqual(clientSurfaceField(), {});
});
