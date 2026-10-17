"""Early admission failure and paid-collection ordering at the real root consumer."""
from __future__ import annotations

from tests.test_acceptance_preparation_loop import _root_pass
from tests.test_forced_acceptance_panel_collection import recorder as recorder


def test_reviewed_early_return_still_collects_paid_operation(tmp_path, monkeypatch):
    fx = _root_pass(tmp_path, monkeypatch)
    fx.ctx._task_acceptance_reviewed = True
    fx.trace['review_runs'] = [{'authority': 'host_root', 'binding_hash': 'paid', 'actors': []}]
    collected = []
    monkeypatch.setattr('ouroboros.review_dispatch.reconcile_pending_acceptance_runs',
                        lambda trace, **kw: collected.append(trace['review_runs'][0]['binding_hash']))
    assert fx.run() is False
    assert collected == ['paid']


def test_early_collection_failure_stays_reconcile_failure(tmp_path, monkeypatch):
    fx = _root_pass(tmp_path, monkeypatch)
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'cyber_pro')
    paid = {'authority': 'host_root', 'binding_hash': 'paid', 'actors': []}
    fx.trace['review_runs'] = [paid]
    def fail(*a, **kw):
        raise OSError('receipt unavailable')
    monkeypatch.setattr('ouroboros.review_dispatch.reconcile_pending_acceptance_runs', fail)
    assert fx.run() is False
    assert fx.trace['review_runs'] == [paid]
    assert fx.trace['review_decision']['host_failure']['stage'] == 'reconcile'
    assert fx.trace['acceptance_decision']['status'] == 'finalized_unaccepted'
    assert 'acceptance_preparation' not in fx.trace


def test_early_material_failure_is_local_preparation_not_a_panel(tmp_path, monkeypatch):
    fx = _root_pass(tmp_path, monkeypatch)
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'cyber_pro')
    def fail(*a, **kw):
        raise OSError('material reader unavailable')
    monkeypatch.setattr('ouroboros.acceptance_material.retain_acceptance_material', fail)
    assert fx.run() is False
    assert fx.trace.get('review_runs', []) == []
    assert fx.trace['acceptance_preparation']['status'] == 'failed'
    assert fx.trace['acceptance_decision']['status'] == 'finalized_unaccepted'


def test_forced_capture_failure_collects_paid_facts_and_clears_previous_source(recorder, monkeypatch):
    _ctx, record = recorder
    trace = {'tool_calls': [], 'acceptance_material': {'produced': True, 'selected_answer': {'text': 'old'}}}
    collected = []
    monkeypatch.setattr('ouroboros.review_dispatch.reconcile_pending_acceptance_runs',
                        lambda *a, **kw: collected.append(True))
    def fail(*a, **kw):
        assert collected == [True], 'paid collection precedes fresh capture'
        raise OSError('capture unavailable')
    monkeypatch.setattr('ouroboros.acceptance_material.retain_acceptance_material', fail)
    record(trace)
    assert trace['acceptance_material']['produced'] is False
    assert 'selected_answer' not in trace['acceptance_material']
    assert trace['acceptance_material']['issues'][0]['status'] == 'source_unavailable'
