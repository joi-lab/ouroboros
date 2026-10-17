import assert from 'node:assert/strict';
import test from 'node:test';
import { daemonStatusLine, serviceBannerLine } from '../modules/harness_accounts.js';
import { statusUnavailableNote } from '../modules/claudexor_status_store.js';

function status() {
    return {
        daemon: { state: 'running', engine_version: '4.0.0', servingMode: 'normal',
            loop: { windowMs: 10004, delay: { p99Ms: 3.4, maxMs: 9 }, utilization: 0.25 },
            store: { integrity: 'ok', flusher: { state: 'up' }, flush_lag_ms: 7,
                wal_bytes: 2 ** 20, obligations_open: 0, migration: null },
            capacity: { maxConcurrent: 'unlimited', maxConcurrentModelOperations: 8,
                maxConcurrentNonModelJobs: 'unlimited', sources: {
                    max_concurrent: 'default', max_concurrent_model_operations: 'config',
                    max_concurrent_non_model_jobs: 'environment',
                } },
            admission: { active: { model: 1, non_model: 24 }, queued: { model: 0, non_model: 3 } },
        },
        reads: { catalog: 'ok', accounts: 'ok', quota: 'ok' },
    };
}

test('the existing daemon banner carries measured loop, store and effective admission', () => {
    const payload = status();
    const original = structuredClone(payload);
    const line = daemonStatusLine(payload);
    assert.equal(line.tone, 'ok');
    assert.match(line.text, /Loop: p99 3\.4 ms, max 9 ms, busy 25% \(10004 ms window\)/);
    assert.match(line.text, /Store: integrity ok, flusher up, flush lag 7 ms, WAL 1\.0 MiB, obligations 0/);
    assert.match(line.text, /Jobs: model 1 active \/ 0 queued; other 24 active \/ 3 queued/);
    assert.match(line.text, /Effective caps: total unlimited \(default\), model 8 \(config\), other unlimited \(environment\)/);
    assert.doesNotMatch(line.text, /Import|Recovery only|restartRequired/);
    assert.deepEqual(payload, original); // A display never changes caps or admission.
});

test('recovery is running with import progress and unknown SQL measurements, never ready', () => {
    const payload = status();
    payload.daemon.servingMode = 'recovery_only';
    payload.daemon.store = { integrity: null, flusher: null, flush_lag_ms: null,
        wal_bytes: null, obligations_open: null, migration: {
            phase: 'verifying', completedPartitions: 2, totalPartitions: 3,
            processedBytes: 2 ** 20, totalBytes: 2 ** 21, currentPartition: 'project-fixture',
        } };
    payload.daemon.loop = null;
    const line = daemonStatusLine(payload);
    assert.equal(line.tone, 'warn');
    assert.match(line.text, /Claudexor running/);
    assert.match(line.text, /Recovery only; product work is not admitted/);
    assert.match(line.text, /integrity unknown, flusher unknown, flush lag unknown, WAL unknown, obligations unknown/);
    assert.match(line.text, /Import verifying: 2\/3 partitions, 1\.0\/2\.0 MiB \(project-fixture\)/);
    assert.doesNotMatch(line.text, /Claudexor ready|Loop:|obligations 0|flush lag 0/);
});

test('a stale account facet cannot hide engine recovery or erase its retained accounts', () => {
    const snapshot = status();
    snapshot.daemon.servingMode = 'recovery_only';
    snapshot.daemon.store.migration = { phase: 'importing', completedPartitions: 0,
        totalPartitions: 3, processedBytes: 0, totalBytes: 2 ** 21, currentPartition: null };
    snapshot.reads.accounts = 'failed';
    snapshot.facets = { accounts: { stale: true, observed_at: '2026-10-11T01:02:03Z', error: 'daemon_recovery_only' } };
    snapshot.profiles = { profiles: [{ profile: { profile_id: 'kept-account', harness_id: 'fixture', enabled: true } }] };
    const retained = structuredClone(snapshot);
    const store = { snapshot, reads: snapshot.reads, loading: false, everSettled: true,
        facet: (name) => snapshot.reads[name],
        unavailableNote: (facet, { subject = '' } = {}) => statusUnavailableNote(snapshot.reads[facet], {
            error: 'daemon_recovery_only', facet, subject,
        }),
    };
    const line = serviceBannerLine(store);
    assert.match(line.text, /accounts.*could not be read/i);
    assert.match(line.text, /Recovery only/);
    assert.match(line.text, /Import importing: 0\/3 partitions, 0\.0\/2\.0 MiB/);
    assert.equal(line.text.split('Import importing:').length, 2);
    assert.deepEqual(snapshot, retained);
});

test('legacy finite totals keep class caps unknown and missing engine facts stay absent', () => {
    const payload = { daemon: { state: 'running', engine_version: '3.25.1', capacity: { maxConcurrent: 24 } } };
    const text = daemonStatusLine(payload).text;
    assert.match(text, /total 24 \(source unknown\), model unknown \(source unknown\), other unknown \(source unknown\)/);
    assert.doesNotMatch(text, /unlimited|Loop:|Store:|Jobs:/);
    delete payload.daemon.capacity;
    assert.equal(daemonStatusLine(payload).text, 'Claudexor ready (engine 3.25.1) · home ');
});

test('invalid partial values do not imply healthy zero or an unlimited default', () => {
    const payload = status();
    payload.daemon.loop = { windowMs: null, delay: null, utilization: 2 };
    payload.daemon.store = { integrity: 'failed', flusher: { state: 'down' },
        flush_lag_ms: null, wal_bytes: null, obligations_open: null };
    payload.daemon.admission = { active: { model: 0, non_model: null }, queued: null };
    for (const invalid of [null, 0, -1, true, 1.5, '24', Number.MAX_SAFE_INTEGER + 1]) {
        payload.daemon.capacity = { maxConcurrent: invalid };
        const text = daemonStatusLine(payload).text;
        assert.match(text, /Loop: p99 unknown, max unknown, busy unknown/);
        assert.match(text, /integrity failed, flusher down/);
        assert.match(text, /model 0 active \/ unknown queued; other unknown active \/ unknown queued/);
        assert.match(text, /Effective caps: total unknown/);
        assert.doesNotMatch(text, /unlimited|WAL 0|obligations 0|p99 0/);
    }
});
