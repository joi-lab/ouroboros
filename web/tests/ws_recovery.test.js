import assert from 'node:assert/strict';
import test from 'node:test';

import { WS, decide, RECOVERY_HEALTHY_PROBE_LIMIT } from '../modules/ws.js';

// Behavior coverage for the served-SHA reconciliation contract (netres Lane B):
// one SSOT decision drives both the post-open refresh and the socket-down
// recovery probe. These tests run the real WS code under a mocked browser
// environment (same pattern as client_surface.test.js).

class FakeSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    static CLOSING = 2;
    static CLOSED = 3;
    static instances = [];

    constructor(url) {
        this.url = url;
        this.readyState = FakeSocket.CONNECTING;
        this.sent = [];
        FakeSocket.instances.push(this);
    }

    send(data) { this.sent.push(data); }

    close() {}
}

function installEnv(responses = []) {
    const replaced = [];
    let calls = 0;
    globalThis.WebSocket = FakeSocket;
    FakeSocket.instances = [];
    globalThis.window = {
        location: {
            href: 'http://127.0.0.1:8000/',
            replace: (url) => replaced.push(String(url)),
        },
    };
    globalThis.document = { getElementById: () => null };
    globalThis.fetch = async () => {
        calls += 1;
        // The last scripted response repeats for every later probe.
        const script = responses.length > 1 ? responses.shift() : responses[0];
        if (!script || script.reject) throw new Error('network down');
        return {
            ok: script.ok !== false,
            json: async () => {
                if (script.badJson) throw new Error('malformed body');
                return script.body ?? {};
            },
        };
    };
    return {
        replaced,
        fetchCalls: () => calls,
        setFetch: (fn) => { globalThis.fetch = async (...args) => { calls += 1; return fn(...args); }; },
    };
}

function settle(turns = 4) {
    let p = Promise.resolve();
    for (let i = 0; i < turns; i += 1) {
        p = p.then(() => new Promise((resolve) => setTimeout(resolve, 0)));
    }
    return p;
}

async function waitFor(cond, what, maxTurns = 400) {
    for (let i = 0; i < maxTurns; i += 1) {
        if (cond()) return;
        await settle(1);
    }
    throw new Error(`condition not reached: ${what}`);
}

// Stops any chained delay-0 recovery timers so node:test can exit: an OPEN
// socket makes an in-flight probe bail without re-arming, then clear timers.
async function teardown(ws) {
    ws.ws = { readyState: FakeSocket.OPEN };
    await settle(6);
    ws._clearUiRecoveryTimer();
    ws._clearReconnectTimer();
    ws._clearWatchdogTimer();
    ws._clearPostOpenRetryTimer();
    ws.ws = null;
}

// ---------------------------------------------------------------------------
// decide(): the pure SSOT contract.
// ---------------------------------------------------------------------------

test('decide keeps the page on the first-ever connection regardless of SHA state', () => {
    assert.equal(decide(null, 'abc', false), 'keep');
    assert.equal(decide(null, undefined, false), 'keep');
    assert.equal(decide('abc', 'def', false), 'keep');
});

test('decide keeps the page when the served SHA is unchanged', () => {
    assert.equal(decide('abc', 'abc', true), 'keep');
    assert.equal(decide(' abc ', 'abc', true), 'keep');
});

test('decide reloads as changed when the served SHA differs', () => {
    assert.equal(decide('abc', 'def', true), 'reload_changed');
});

test('decide reloads as unknown when a previously-known SHA disappears or garbles', () => {
    assert.equal(decide('abc', undefined, true), 'reload_unknown');
    assert.equal(decide('abc', null, true), 'reload_unknown');
    assert.equal(decide('abc', '', true), 'reload_unknown');
    assert.equal(decide('abc', '   ', true), 'reload_unknown');
    assert.equal(decide('abc', 12345, true), 'reload_unknown');
    assert.equal(decide('abc', { sha: 'abc' }, true), 'reload_unknown');
    // An unproveable PREVIOUS SHA equally forbids the keep claim once the
    // server does serve one.
    assert.equal(decide(null, 'abc', true), 'reload_unknown');
    assert.equal(decide('', 'abc', true), 'reload_unknown');
});

test('decide keeps ONLY on an exact empty-string served SHA with nothing remembered (Q19)', () => {
    // Owner-selected default under uncertainty (netres Q19), narrowed: the
    // server explicitly serves sha="" (unversioned /api/state while
    // current_sha is unset) and no non-empty SHA was ever remembered.
    assert.equal(decide(null, '', true), 'keep');
    assert.equal(decide('', '', true), 'keep');
    assert.equal(decide(undefined, '', true), 'keep');
});

test('decide reloads as unknown for absent/malformed served values even with nothing remembered', () => {
    // Only the exact empty string is the unversioned-install fact; a missing
    // field, parse failure, non-string or whitespace-only value after a
    // previous connection cannot prove the page current.
    assert.equal(decide(null, undefined, true), 'reload_unknown');
    assert.equal(decide(null, null, true), 'reload_unknown');
    assert.equal(decide('', '   ', true), 'reload_unknown');
    assert.equal(decide(undefined, null, true), 'reload_unknown');
    assert.equal(decide(null, 12345, true), 'reload_unknown');
});

// ---------------------------------------------------------------------------
// _refreshStateAfterOpen: post-open reconciliation through the same SSOT.
// ---------------------------------------------------------------------------

test('first open remembers the served SHA without reloading; later opens compare against it', async () => {
    const env = installEnv([{ body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._refreshStateAfterOpen(false);
        await settle();
        assert.equal(env.replaced.length, 0);

        // Same SHA after a reconnect: keep.
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 0);

        // Changed SHA after a reconnect: reload (proves the first open stored 'abc').
        env.setFetch(async () => ({ ok: true, json: async () => ({ sha: 'def' }) }));
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-change/);
    } finally {
        await teardown(ws);
    }
});

test('an OK state response without a SHA after a reconnect reloads (the closed keep-hole)', async () => {
    const env = installEnv([{ body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a malformed state body after a reconnect reloads as unknown', async () => {
    const env = installEnv([{ badJson: true }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a non-OK state response after open never reloads', async () => {
    const env = installEnv([{ ok: false, body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 0);
    } finally {
        await teardown(ws);
    }
});

// A non-answer is no "keep": the comparison a reconnect owes is repeated until the
// server answers or the socket is gone. Delay 0 chains the retries for the test.
test('a non-answer after open is retried until the server answers; a changed SHA then reloads once', async () => {
    const env = installEnv([{ ok: false, body: {} }, { reject: true }, { body: { sha: 'def' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true, 0);
        await waitFor(() => env.replaced.length === 1, 'reload after the third read');
        assert.match(env.replaced[0], /_ouro_reason=sha-change/);
        assert.equal(env.fetchCalls(), 3, 'a 500, then a failed request, then the answer');
        await settle(6);
        assert.equal(env.fetchCalls(), 3, 'the answer ends the retries');
        assert.equal(env.replaced.length, 1);
    } finally {
        await teardown(ws);
    }
});

test('a retried post-open read that finds the same SHA keeps the page and stops', async () => {
    const env = installEnv([{ ok: false, body: {} }, { body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws._refreshStateAfterOpen(true, 0);
        await waitFor(() => env.fetchCalls() === 2, 'second read');
        await settle(6);
        assert.equal(env.fetchCalls(), 2);
        assert.equal(env.replaced.length, 0);
        assert.equal(ws._postOpenRetryTimer, null);
    } finally {
        await teardown(ws);
    }
});

test('a first-open non-answer is retried and stores the baseline without reloading', async () => {
    const env = installEnv([{ ok: false, body: {} }, { body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._refreshStateAfterOpen(false, 0);
        await waitFor(() => ws._lastSha === 'abc', 'baseline stored by the retry');
        assert.equal(env.replaced.length, 0);
    } finally {
        await teardown(ws);
    }
});

test('post-open retries belong to their socket: a cycled connection ends them', async () => {
    const env = installEnv([{ ok: false, body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._lastSha = 'abc';
        ws.ws = { readyState: FakeSocket.OPEN };
        ws._refreshStateAfterOpen(true, 0);
        await waitFor(() => env.fetchCalls() >= 3, 'retries chain while the socket lives');
        ws.ws = { readyState: FakeSocket.OPEN };
        await settle(6);
        const calls = env.fetchCalls();
        await settle(6);
        assert.equal(env.fetchCalls(), calls, 'the old socket no longer reads');
        assert.equal(env.replaced.length, 0);
    } finally {
        await teardown(ws);
    }
});

test('a disconnect clears the pending post-open retry', async () => {
    const env = installEnv([{ ok: false, body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws.connect();
        const socket = FakeSocket.instances[0];
        socket.readyState = FakeSocket.OPEN;
        socket.onopen();
        await waitFor(() => ws._postOpenRetryTimer !== null, 'retry armed after the 500');
        socket.onclose();
        assert.equal(ws._postOpenRetryTimer, null);
        assert.equal(env.replaced.length, 0);
    } finally {
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// _scheduleUiRecovery: probe decisions, fuse, race bail, queue survival.
// ---------------------------------------------------------------------------

test('healthy same-SHA probes reconnect in place; the fuse reloads exactly on the Nth probe', async () => {
    const env = installEnv([{ body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'fuse reload');
        // The fuse fired on the Nth probe — earlier healthy probes kept the page.
        assert.equal(env.fetchCalls(), RECOVERY_HEALTHY_PROBE_LIMIT);
        assert.match(env.replaced[0], /_ouro_reason=socket-disconnect/);
    } finally {
        await teardown(ws);
    }
});

test('a failed probe resets the consecutive healthy count', async () => {
    const env = installEnv([
        { body: { sha: 'abc' } },
        { body: { sha: 'abc' } },
        { ok: false },
        { body: { sha: 'abc' } },
    ]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'fuse reload after reset');
        // Two healthy, one failed (reset), then a fresh full run of healthy probes.
        assert.equal(env.fetchCalls(), 3 + RECOVERY_HEALTHY_PROBE_LIMIT);
    } finally {
        await teardown(ws);
    }
});

test('a recovery probe seeing a changed SHA reloads immediately', async () => {
    const env = installEnv([{ body: { sha: 'def' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'sha-change reload');
        assert.equal(env.fetchCalls(), 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-change/);
    } finally {
        await teardown(ws);
    }
});

test('a recovery probe with a missing SHA after a connection reloads as unknown', async () => {
    const env = installEnv([{ body: {} }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'sha-unknown reload');
        assert.equal(env.fetchCalls(), 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a probe resolving after the socket reopened bails without reloading or re-arming', async () => {
    const env = installEnv([]);
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    env.setFetch(async () => {
        await gate;
        // A changed SHA that would reload were the bail missing.
        return { ok: true, json: async () => ({ sha: 'zzz' }) };
    });
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() === 1, 'probe dispatched');
        ws.ws = { readyState: FakeSocket.OPEN };
        release();
        await settle();
        assert.equal(env.replaced.length, 0);
        await settle();
        assert.equal(env.fetchCalls(), 1, 'a bailed probe must not re-arm recovery');
    } finally {
        await teardown(ws);
    }
});

test('the fuse fires at most once per disconnect episode and resets on reconnect', async () => {
    const env = installEnv([{ body: { sha: 'abc' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';

        // Episode 1: fuse fires once.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'first fuse');

        // Still down, recovery re-armed by the reconnect cycle: healthy probes
        // continue but the episode fuse stays latched.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        const seen = env.fetchCalls();
        await waitFor(
            () => env.fetchCalls() >= seen + RECOVERY_HEALTHY_PROBE_LIMIT + 2,
            'post-fuse probes',
        );
        assert.equal(env.replaced.length, 1, 'the fuse must not fire twice in one episode');

        // Successful reconnect ends the episode.
        ws.ws = { readyState: FakeSocket.OPEN };
        await settle(6);
        ws.ws = null;
        ws.connect();
        const sock = FakeSocket.instances.at(-1);
        sock.readyState = FakeSocket.OPEN;
        sock.onopen();
        await settle();
        assert.equal(env.replaced.length, 1, 'a same-SHA reconnect must not reload');

        // Episode 2 after a fresh disconnect: the fuse is armed again AND the
        // healthy counter restarted from zero — the episode must consume a
        // full run of healthy probes before its own fuse (pins the onopen
        // counter reset).
        const episode2Base = env.fetchCalls();
        ws.ws = null;
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 2, 'second-episode fuse');
        assert.ok(
            env.fetchCalls() - episode2Base >= RECOVERY_HEALTHY_PROBE_LIMIT,
            'episode 2 fired its fuse without a full run of healthy probes',
        );
    } finally {
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// In-flight probe gating: at most one recovery probe chain per episode.
// ---------------------------------------------------------------------------

test('arm attempts while a probe hangs in flight dispatch no extra probe', async () => {
    const env = installEnv([]);
    const releases = [];
    env.setFetch(() => new Promise((resolve) => {
        releases.push(() => resolve({ ok: true, json: async () => ({ sha: 'abc' }) }));
    }));
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() === 1, 'first probe dispatched');
        // send()/_scheduleReconnect would re-arm during the hung probe: gated.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await settle(8);
        assert.equal(env.fetchCalls(), 1, 'no second probe while one is in flight');
        releases.shift()();
        // The single chain resumes with exactly one follow-up probe.
        await waitFor(() => env.fetchCalls() === 2, 'chain resumed after resolution');
        await settle(4);
        assert.equal(env.fetchCalls(), 2, 'still one chain after the resolution');
        assert.equal(env.replaced.length, 0);
    } finally {
        while (releases.length) releases.shift()();
        await teardown(ws);
    }
});

test('hung probes cannot multi-count the healthy fuse', async () => {
    const env = installEnv([]);
    const releases = [];
    env.setFetch(() => new Promise((resolve) => {
        releases.push(() => resolve({ ok: true, json: async () => ({ sha: 'abc' }) }));
    }));
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        for (let i = 0; i < RECOVERY_HEALTHY_PROBE_LIMIT; i += 1) {
            await waitFor(() => env.fetchCalls() === i + 1, `probe ${i + 1} dispatched`);
            // Spam arm attempts while the probe hangs — none may stack.
            ws._scheduleUiRecovery('socket-disconnect', 0);
            ws._scheduleUiRecovery('socket-disconnect', 0);
            assert.equal(env.fetchCalls(), i + 1);
            releases.shift()();
            await settle(4);
        }
        // The fuse needed LIMIT distinct sequential probes — one healthy count
        // per physical probe — and fired exactly once.
        assert.equal(env.replaced.length, 1, 'fuse fired exactly once');
        assert.equal(env.fetchCalls(), RECOVERY_HEALTHY_PROBE_LIMIT);
        assert.match(env.replaced[0], /_ouro_reason=socket-disconnect/);
    } finally {
        while (releases.length) releases.shift()();
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// Run-from-source installs (served sha "") and probe-side SHA adoption.
// ---------------------------------------------------------------------------

test('a source install (served sha "") keeps the page on reconnect, post-open path', async () => {
    const env = installEnv([{ body: { sha: '' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._refreshStateAfterOpen(true);
        await settle();
        assert.equal(env.replaced.length, 0, 'no SHA was ever provable: keep');
        assert.equal(ws._lastSha, null, 'an empty served SHA is never stored');
    } finally {
        await teardown(ws);
    }
});

test('a source install keeps on recovery probes and the fuse still heals a frozen runtime', async () => {
    // Interplay B2+B3: never-any-sha probes keep (no reload_unknown storm) and
    // store nothing; the healthy-probe fuse remains the one reload that heals
    // a frozen WebSocket runtime.
    const env = installEnv([{ body: { sha: '' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'fuse reload');
        assert.equal(env.fetchCalls(), RECOVERY_HEALTHY_PROBE_LIMIT,
            'every pre-fuse probe kept the page');
        assert.match(env.replaced[0], /_ouro_reason=socket-disconnect/,
            'the reload is the runtime fuse, not a SHA decision');
        assert.equal(ws._lastSha, null);
    } finally {
        await teardown(ws);
    }
});

test('known-SHA-then-empty still reloads as unknown on the recovery probe', async () => {
    const env = installEnv([{ body: { sha: '' } }]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'sha-unknown reload');
        assert.equal(env.fetchCalls(), 1);
        assert.match(env.replaced[0], /_ouro_reason=sha-unknown/);
    } finally {
        await teardown(ws);
    }
});

test('a recovery probe never stores the served SHA; only the post-open refresh does', async () => {
    const env = installEnv([]);
    const releases = [];
    env.setFetch(() => new Promise((resolve) => {
        releases.push(() => resolve({ ok: true, json: async () => ({ sha: 'X' }) }));
    }));
    const ws = new WS('ws://unused');
    try {
        // Never-connected client probing a restarted server: keep, store nothing.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() === 1, 'pre-connect probe dispatched');
        releases.shift()();
        await settle();
        assert.equal(env.replaced.length, 0);
        assert.equal(ws._lastSha, null, 'the probe must not adopt the served SHA');

        // Park the probe chain, then let the first open remember the SHA.
        ws.ws = { readyState: FakeSocket.OPEN };
        await settle(6);
        ws._clearUiRecoveryTimer();
        while (releases.length) releases.shift()();
        env.setFetch(async () => ({ ok: true, json: async () => ({ sha: 'X' }) }));
        ws._refreshStateAfterOpen(false);
        await settle();
        assert.equal(env.replaced.length, 0);
        assert.equal(ws._lastSha, 'X', 'the post-open refresh stores the served SHA');

        // A later probe seeing different assets heals the page (as base did).
        ws._wasConnected = true;
        ws.ws = null;
        env.setFetch(async () => ({ ok: true, json: async () => ({ sha: 'Y' }) }));
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'changed-SHA reload');
        assert.match(env.replaced[0], /_ouro_reason=sha-change/);
    } finally {
        while (releases.length) releases.shift()();
        await teardown(ws);
    }
});

test('keep-recovery preserves the outbound queue and the reconnect flush delivers it', async () => {
    const env = installEnv([
        { body: { sha: 'abc' } },
        { ok: false },
    ]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        const outbound = [];
        ws.on('outbound_queued', (e) => outbound.push(['queued', e.clientMessageId]));
        ws.on('outbound_sent', (e) => outbound.push(['sent', e.clientMessageId, e.queued]));

        const result = ws.send({ type: 'chat', text: 'hello' });
        assert.equal(result.status, 'queued');
        // send() armed the slow default timers; drive recovery deterministically.
        ws._clearUiRecoveryTimer();
        ws._clearReconnectTimer();
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() >= 2, 'keep probe plus follow-up');
        assert.equal(env.replaced.length, 0, 'a healthy same-SHA probe must not reload');

        const sock = ws.ws;
        sock.readyState = FakeSocket.OPEN;
        sock.onopen();
        await settle();
        assert.equal(env.replaced.length, 0);
        assert.equal(sock.sent.length, 1, 'the queued message must flush on reconnect');
        const frame = JSON.parse(sock.sent[0]);
        assert.equal(frame.type, 'chat');
        assert.equal(frame.text, 'hello');
        assert.deepEqual(outbound[0], ['queued', result.clientMessageId]);
        assert.deepEqual(outbound[1], ['sent', result.clientMessageId, true]);
    } finally {
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// Disconnect-episode generations: a probe belongs to the episode it was armed
// in; a reconnect ends the episode and stale resolutions are discarded.
// ---------------------------------------------------------------------------

test('a probe hung across reconnect + second disconnect neither disarms the new episode nor acts stale', async () => {
    const env = installEnv([]);
    const releases = [];
    env.setFetch(() => new Promise((resolve) => { releases.push(resolve); }));
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';

        // Episode 1: probe A dispatches and hangs.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() === 1, 'probe A dispatched');

        // Reconnect while A is still in flight (its post-open refresh fetch
        // also hangs — call 2).
        ws.connect();
        const sock = FakeSocket.instances.at(-1);
        sock.readyState = FakeSocket.OPEN;
        sock.onopen();
        await settle();

        // Second disconnect: the NEW episode must be able to arm its own
        // probe (with a global in-flight flag it would stay disarmed forever
        // while A hangs).
        ws.ws = null;
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() === 3, 'new episode armed its own probe');

        // Late resolution of stale probe A: a changed SHA that would reload
        // were the generation guard missing, with the healthy counter one
        // short of the fuse so a stale healthy count would fire it.
        ws._recoveryHealthyProbes = RECOVERY_HEALTHY_PROBE_LIMIT - 1;
        releases[0]({ ok: true, json: async () => ({ sha: 'zzz' }) });
        await settle(6);
        assert.equal(env.replaced.length, 0, 'a stale probe must not reload or fire the fuse');
        assert.equal(ws._recoveryHealthyProbes, RECOVERY_HEALTHY_PROBE_LIMIT - 1,
            'a stale probe must not touch the new episode\'s counters');
        assert.equal(ws._uiRecoveryProbeInFlight, true,
            'a stale probe must not clear the newer episode\'s in-flight flag');

        // The stale resolution must not have re-armed a second chain either.
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await settle(4);
        assert.equal(env.fetchCalls(), 3, 'no extra probe after the stale resolution');

        // The live probe B still owns the episode: releasing it healthy
        // same-SHA reaches the fuse threshold set above.
        releases[2]({ ok: true, json: async () => ({ sha: 'abc' }) });
        await waitFor(() => env.replaced.length === 1, 'live probe reaches the fuse');
        assert.match(env.replaced[0], /_ouro_reason=socket-disconnect/);
    } finally {
        releases.forEach((release) => release({ ok: false, json: async () => ({}) }));
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// Captive-portal probe responses: 200 without a parseable object body is a
// FAILED probe, never health.
// ---------------------------------------------------------------------------

test('a 200 non-JSON probe body counts as a failed probe: reset and re-arm, no reload', async () => {
    // A captive portal / interposed proxy answers 200 HTML for a remote
    // browser: with a remembered SHA the old code reloaded straight into the
    // portal, destroying queued outbound messages.
    const env = installEnv([
        { body: { sha: 'abc' } },
        { body: { sha: 'abc' } },
        { badJson: true },
        { body: { sha: 'abc' } },
    ]);
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.replaced.length === 1, 'fuse reload after the portal reset');
        // Two healthy, the portal response (failed → counter reset), then a
        // fresh full run of healthy probes before the fuse. Had the portal
        // response counted as healthy, the reload would have come earlier and
        // with a sha-unknown reason.
        assert.equal(env.fetchCalls(), 3 + RECOVERY_HEALTHY_PROBE_LIMIT);
        assert.match(env.replaced[0], /_ouro_reason=socket-disconnect/,
            'the only reload is the runtime fuse, never a portal-driven SHA decision');
    } finally {
        await teardown(ws);
    }
});

test('a 200 probe whose JSON body is not an object is equally a failed probe', async () => {
    const env = installEnv([]);
    env.setFetch(async () => ({ ok: true, json: async () => 'signin required' }));
    const ws = new WS('ws://unused');
    try {
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        ws._scheduleUiRecovery('socket-disconnect', 0);
        await waitFor(() => env.fetchCalls() >= RECOVERY_HEALTHY_PROBE_LIMIT + 2,
            'probe chain keeps re-arming');
        assert.equal(env.replaced.length, 0, 'non-object bodies must never reload');
        assert.equal(ws._recoveryHealthyProbes, 0, 'failed probes keep the counter at zero');
    } finally {
        await teardown(ws);
    }
});

// ---------------------------------------------------------------------------
// Post-open refresh socket scoping: a stale fetch must not overwrite _lastSha
// or reload after the connection cycled (mirror of the probe's OPEN bail).
// ---------------------------------------------------------------------------

test('a post-open refresh resolving after the connection cycled neither stores nor reloads', async () => {
    const env = installEnv([]);
    const releases = [];
    env.setFetch(() => new Promise((resolve) => { releases.push(resolve); }));
    const ws = new WS('ws://unused');
    try {
        // First-open refresh dispatches against socket 1 and hangs.
        const socket1 = { readyState: FakeSocket.OPEN };
        ws.ws = socket1;
        ws._refreshStateAfterOpen(false);
        await waitFor(() => env.fetchCalls() === 1, 'refresh dispatched');

        // The connection cycles: a newer socket owns reconciliation now.
        ws.ws = { readyState: FakeSocket.OPEN };
        releases[0]({ ok: true, json: async () => ({ sha: 'stale' }) });
        await settle();
        assert.equal(ws._lastSha, null, 'a stale refresh must not adopt the served SHA');
        assert.equal(env.replaced.length, 0);

        // Same for a reconnect refresh that would otherwise reload.
        ws._wasConnected = true;
        ws._lastSha = 'abc';
        const socket3 = ws.ws;
        ws._refreshStateAfterOpen(true);
        await waitFor(() => env.fetchCalls() === 2, 'reconnect refresh dispatched');
        assert.equal(ws.ws, socket3);
        ws.ws = { readyState: FakeSocket.OPEN };
        releases[1]({ ok: true, json: async () => ({ sha: 'zzz' }) });
        await settle();
        assert.equal(env.replaced.length, 0, 'a stale refresh must not reload the page');
        assert.equal(ws._lastSha, 'abc');
    } finally {
        releases.forEach((release) => release({ ok: false, json: async () => ({}) }));
        await teardown(ws);
    }
});
