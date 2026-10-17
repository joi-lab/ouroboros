"""Exit/capacity facts cross the real status and context seams without a wake."""
import json
from types import SimpleNamespace

import pytest

from ouroboros import claudexor_daemon as daemon, claudexor_exit_facts as facts, config
from ouroboros.gateways import claudexor as transport
from ouroboros.utils import append_jsonl


def exit_row(root, *, serving=True, cause='heap_exhausted'):
    append_jsonl(root / 'logs/supervisor.jsonl', {
        'type': 'claudexor_daemon_start_failed', 'descriptor_written': serving,
        'classification': cause, 'exit_signal': 6, 'exit_code': None,
        'pin_version': '3.22.0', 'at': '2026-10-08T01:02:03Z',
    })


def rows(root):
    return [json.loads(line) for line in (root / 'logs/supervisor.jsonl').read_text(
        encoding='utf-8').splitlines()]


def memory():
    return {'heapUsedBytes': 3 * 2**30, 'heapLimitBytes': 16 * 2**30,
            'rssBytes': 4 * 2**30, 'externalBytes': 100,
            'nodeHeapArgs': ['--max-old-space-size=16384'],
            'atAdmission': {'heapUsedBytes': 2 * 2**30, 'rssBytes': 3 * 2**30,
                            'at': '2026-10-08T02:00:00Z'},
            'sampledAt': '2026-10-08T02:01:00Z'}


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(facts, '_CAPACITY_SEEN', {})
    descriptor = tmp_path / 'descriptor.json'
    descriptor.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(daemon, 'owned_descriptor_path', lambda: descriptor)
    monkeypatch.setattr(daemon, 'verify_owned_home', lambda: '')
    import ouroboros.claudexor_runtime as runtime
    monkeypatch.setattr(runtime, 'get_runtime_manager', lambda: SimpleNamespace(status=lambda **kw: {}))
    manager = daemon.OwnedClaudexorDaemon()
    manager._engine_version, manager._engine_build_sha = '3.22.1', 'fixture-build'
    endpoint = SimpleNamespace(host='127.0.0.1', port=12345)
    monkeypatch.setattr(manager, '_classify_liveness', lambda: (endpoint, 'running', ''))
    calls = []

    class Gateway:
        response = {'memory': memory()}
        error = None

        def __init__(self, selected):
            assert selected is endpoint

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def daemon_status(self, **kwargs):
            calls.append(kwargs)
            if self.error:
                raise self.error
            return self.response

    monkeypatch.setattr(transport, 'ClaudexorGateway', Gateway)
    return manager, Gateway, descriptor, calls


def test_status_exposes_exit_and_records_capacity_once_per_generation(tmp_path, status_env):
    manager, gateway, descriptor, calls = status_env
    exit_row(tmp_path)
    for _ in range(3):
        status = manager.status_dict()
        assert status['last_exit']['phase'] == 'serving'
        assert status['last_exit']['observed_at'] == '2026-10-08T01:02:03Z'
        assert status['memory'] == memory()
    capacity = [row for row in rows(tmp_path) if row['type'] == 'claudexor_engine_capacity']
    assert len(capacity) == 1 and len(calls) == 3
    assert capacity[0]['heap_limit_bytes'] == 16 * 2**30
    assert capacity[0]['admission_heap_used_bytes'] == 2 * 2**30
    assert capacity[0]['engine_build_sha'] == 'fixture-build'
    assert capacity[0]['node_heap_args'] == ['--max-old-space-size=16384']
    facts._CAPACITY_SEEN.clear()  # A second reader rejoins the existing log observation.
    manager.status_dict()
    assert len([r for r in rows(tmp_path) if r['type'] == 'claudexor_engine_capacity']) == 1
    descriptor.write_text('{"new_generation":true}', encoding='utf-8')
    manager.status_dict()
    assert len([r for r in rows(tmp_path) if r['type'] == 'claudexor_engine_capacity']) == 2


def test_old_engine_status_absence_is_unknown_and_offline_never_calls(tmp_path, status_env, monkeypatch):
    manager, gateway, _, calls = status_env
    gateway.error = transport.ClaudexorUnavailable('not_found', 'old engine', status_code=404)
    assert manager.status_dict()['memory'] is None
    row, = rows(tmp_path)
    assert row['heap_limit_bytes'] is None and row['admission_heap_used_bytes'] is None
    assert row['node_heap_args'] is None
    before = len(calls)
    monkeypatch.setattr(manager, '_classify_liveness', lambda: (None, 'stale', ''))
    assert manager.status_dict()['memory'] is None and len(calls) == before


def test_context_has_one_saved_observation_line_without_daemon_access(tmp_path, monkeypatch):
    from ouroboros.context_health import build_health_invariants

    def forbidden(*args, **kwargs):
        raise AssertionError('context must not contact or wake the daemon')

    monkeypatch.setattr(daemon, 'ensure_owned_gateway', forbidden)
    monkeypatch.setattr(daemon, 'read_owned_gateway', forbidden)
    monkeypatch.setattr(daemon, 'get_owned_daemon', forbidden)
    monkeypatch.setattr(transport, 'ClaudexorGateway', forbidden)
    env = SimpleNamespace(drive_path=lambda p: tmp_path / p, repo_path=lambda p: tmp_path / p)
    assert 'ENGINE EXITED' not in build_health_invariants(env)
    exit_row(tmp_path, serving=False, cause='writer_lease_contended')
    exit_row(tmp_path)
    facts._record_capacity(tmp_path, 'generation', '3.22.1', 'fixture-build', memory())
    rendered = build_health_invariants(env)
    assert rendered.count('WARNING: ENGINE EXITED') == 1
    assert 'exited 2 time(s)' in rendered and 'heap_exhausted, signal 6, while serving' in rendered
    assert 'observed at 2026-10-08T01:02:03Z, not the death time' in rendered
    assert 'heap limit 16.00 GiB' in rendered and 'admission heap 2.00 GiB' in rendered


def test_saved_window_is_bounded_and_malformed_rows_are_fail_soft(tmp_path):
    path = tmp_path / 'logs/supervisor.jsonl'
    exit_row(tmp_path)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'padding': '.' * facts._RECENT_BYTES}) + '\n')
        stream.write('broken\n')
    assert facts.health_line(tmp_path) == ''
    exit_row(tmp_path, serving=False)
    assert 'while startup' in facts.health_line(tmp_path)
    assert facts.memory_facts({}) is None
    partial = facts.memory_facts({'memory': {'heapUsedBytes': 0, 'heapLimitBytes': True}})
    assert partial['heapUsedBytes'] == 0 and partial['heapLimitBytes'] is None
    assert partial['atAdmission'] is None and partial['nodeHeapArgs'] is None


def runtime_status():
    return {
        'memory': memory(), 'servingMode': 'normal',
        'loop': {'windowMs': 10004, 'windowEndedAt': '2026-10-11T01:02:03Z', 'utilization': 0.25,
                 'delay': {'resolutionMs': 1, 'samples': 92, 'p50Ms': 1.2, 'p99Ms': 3.4, 'maxMs': 9},
                 'gc': {'count': 2, 'totalMs': 3.2, 'maxMs': 2}},
        'store': {'flush_lag_ms': 7, 'wal_bytes': 2**20, 'interval_ms': 200, 'busy_waits': 0,
                  'obligations_open': 0, 'last_barrier_at': '2026-10-11T01:02:02Z', 'integrity': 'ok',
                  'flusher': {'state': 'up', 'interval_ms': 200, 'flush_lag_ms': 7, 'generation': 4,
                              'acknowledged_generation': 3, 'pending_registrations': 1, 'pending_waiters': 0,
                              'last_barrier_at': '2026-10-11T01:02:02Z'}, 'migration': None},
        'capacity': {'maxConcurrent': 'unlimited', 'maxConcurrentModelOperations': 8,
                     'maxConcurrentNonModelJobs': 'unlimited',
                     'sources': {'max_concurrent': 'default', 'max_concurrent_model_operations': 'config',
                                 'max_concurrent_non_model_jobs': 'environment'}},
        'admission': {'active': {'model': 1, 'non_model': 24}, 'queued': {'model': 0, 'non_model': 3}},
    }


def test_status_and_capacity_record_share_observed_runtime_facts(tmp_path, status_env):
    manager, gateway, _, calls = status_env
    gateway.response = runtime_status()
    result = manager.status_dict()
    assert result['servingMode'] == 'normal'
    for field in ('loop', 'store', 'capacity', 'admission'):
        assert result[field] == gateway.response[field]
    assert len(calls) == 1  # The same read supplies memory, SQL, loop and admission.
    row, = rows(tmp_path)
    assert row['type'] == 'claudexor_engine_capacity' and row['generation'] and row['ts']
    assert row['loop']['delay']['samples'] == 92
    assert row['store']['obligations_open'] == 0
    assert row['capacity']['maxConcurrent'] == 'unlimited'
    assert row['admission']['active']['non_model'] == 24


def test_recovery_progress_is_live_while_generation_record_stays_historical(tmp_path, status_env):
    manager, gateway, _, calls = status_env
    gateway.response = runtime_status()
    gateway.response.update(servingMode='recovery_only', loop=None)
    gateway.response['store'] = {key: None for key in gateway.response['store']}
    progress = {'phase': 'verifying', 'completedPartitions': 2, 'totalPartitions': 3,
                'currentPartition': 'project-fixture', 'processedBytes': 1024, 'totalBytes': 2048}
    gateway.response['store']['migration'] = progress
    recovering = manager.status_dict()
    assert recovering['state'] == 'running' and recovering['servingMode'] == 'recovery_only'
    assert recovering['loop'] is None
    assert recovering['store']['migration'] == progress
    assert recovering['store']['obligations_open'] is None and recovering['store']['integrity'] is None
    gateway.response = runtime_status()
    normal = manager.status_dict()
    assert normal['servingMode'] == 'normal' and normal['store']['migration'] is None
    assert normal['store']['integrity'] == 'ok'
    row, = rows(tmp_path)
    assert row['servingMode'] == 'recovery_only' and row['store']['migration'] == progress
    assert len(calls) == 2  # No poller or repeated durable metrics writer.


def test_unknown_metrics_and_caps_do_not_become_zero_or_unlimited(status_env):
    manager, gateway, _, _ = status_env
    gateway.response = {
        'servingMode': 'new_unknown_mode',
        'loop': {'windowMs': -1, 'utilization': 2, 'delay': {'samples': True, 'p99Ms': float('nan')}},
        'store': {'integrity': 'unverified', 'obligations_open': False,
                  'flush_lag_ms': float('inf'), 'flusher': {'state': 'missing'}},
        'capacity': {'maxConcurrent': 24},
        'admission': {'active': {'model': 0, 'non_model': False}},
    }
    result = manager.status_dict()
    assert result['servingMode'] is None
    assert result['loop']['windowMs'] is None and result['loop']['utilization'] is None
    assert result['loop']['delay']['samples'] is None and result['loop']['delay']['p99Ms'] is None
    assert result['store']['obligations_open'] is None and result['store']['flush_lag_ms'] is None
    assert result['store']['integrity'] is None and result['store']['flusher']['state'] is None
    assert result['capacity'] == {'maxConcurrent': 24, 'maxConcurrentNonModelJobs': None,
                                  'maxConcurrentModelOperations': None, 'sources': None}
    assert result['admission'] == {'active': {'model': 0, 'non_model': None}, 'queued': None}
    for value in (None, 0, -1, 1.5, True, '24', 2**53):
        gateway.response['capacity']['maxConcurrent'] = value
        assert manager.status_dict()['capacity']['maxConcurrent'] is None


def test_failed_or_changed_generation_read_never_reuses_old_runtime_facts(tmp_path, status_env):
    manager, gateway, descriptor, calls = status_env
    gateway.response = runtime_status()
    assert manager.status_dict()['store']['integrity'] == 'ok'
    gateway.error = transport.ClaudexorUnavailable('daemon_busy', 'not read', status_code=503)
    failed = manager.status_dict()
    for field in ('loop', 'store', 'capacity', 'admission', 'servingMode'):
        assert failed[field] is None
    assert len(calls) == 2 and len(rows(tmp_path)) == 1
    gateway.error = None
    original = gateway.daemon_status

    def changing(self, **kwargs):
        result = original(self, **kwargs)
        descriptor.write_text('{"replaced":true}', encoding='utf-8')
        return result

    gateway.daemon_status = changing
    changed = manager.status_dict()
    assert changed['memory'] is None and changed['store'] is None and changed['capacity'] is None
    assert len(rows(tmp_path)) == 1


def test_old_engine_and_offline_runtime_facts_stay_unknown(tmp_path, status_env, monkeypatch):
    manager, gateway, _, calls = status_env
    gateway.response = {'memory': memory()}
    old = manager.status_dict()
    assert old['memory'] == memory()
    assert all(old[field] is None for field in ('loop', 'store', 'capacity', 'admission', 'servingMode'))
    monkeypatch.setattr(manager, '_classify_liveness', lambda: (None, 'stale', ''))
    assert manager.status_dict()['store'] is None and len(calls) == 1
