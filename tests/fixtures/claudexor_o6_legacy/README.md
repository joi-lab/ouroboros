# Legacy native-run lookup fixture

`fixture.json` records the exact binary hash, expected public identity and frozen
producer provenance. The journal was written once by Claudexor's sealed baseline
`820e849cd15cca47758b700603e156ef29129fbf`, through the existing `DurableJournal` and
`CommandStore`, then independently reopened with that baseline's fold/reader.
No daemon, vendor or user data was involved. The smoke copies bytes; it has no codec.

This fixture certifies native `runId` lookup after import and restart. `jobId` is
different. It deliberately has no `runDir` or per-run artifacts: the public detail
contract supports old records without those optional files. It makes no claim
about old output content, telemetry or vendor sessions. The existing lifecycle
smoke still tests newly executed fake runs and production custody separately.

To reproduce in a Claudexor source checkout containing the sealed snapshot, use
its existing TypeScript toolchain and imports under
`packages/daemon/src/store/test-support/fixtures/legacy/`. Verify
`verifyFixtureManifest()` before and after. With `meta` from `fixture.json`, in a
fresh temporary directory (`rootDir` is its `journal` child), perform the following
through those existing classes:

```js
const now = () => new Date(meta.fixture_time);
const journal = new DurableJournal({ rootDir, partition: 'global', now,
    epochFactory: () => meta.epoch, deferCompaction: true });
const commands = new CommandStore(journal, now);
const { jobId, runId, taskId } = meta.expected;
commands.accept({ id: jobId, clientId: 'o6-fixture',
    idempotencyKey: '33333333-3333-4333-8333-333333333333',
    params: { mode: 'ask', scope: { kind: 'none' }, access: 'readonly',
        prompt: 'O6 retained run fixture.' } });
commands.update(jobId, { state: 'running', runId, taskId, startedAt: meta.fixture_time });
journal.append('run.event', { seq: 1, ts: meta.fixture_time, run_id: runId, task_id: taskId,
    type: 'run.completed', payload: { lifecycle: 'succeeded' } });
commands.update(jobId, { state: 'succeeded', finishedAt: meta.fixture_time,
    result: { lifecycle: 'succeeded' } });
journal.close();
```

Reopen with `DurableJournal.prepare({rootDir, partition: 'global', now,
fold: journalFoldPolicy})` and `new CommandStore(reader, now)`. Require one
retained record with the expected IDs/state, no `runDir`, and no record for an
unknown native ID; reading must leave the binary hash unchanged. Retain the
producer's partition directory name. Always copy/read this `.bin` as bytes,
including on Windows. Published-archive and native three-OS execution are separate
qualification receipts, not supplied by this static fixture.
