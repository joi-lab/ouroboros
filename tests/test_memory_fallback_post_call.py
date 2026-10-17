"""Receipt failure preserves the paid answer's actual publication and stage evidence."""
import json
from types import SimpleNamespace

import pytest

from ouroboros import memory_fallback as mf
from ouroboros import post_task_synthesis as pts
from tests.test_memory_fallback import _Light, _part_fixture
from tests.test_memory_fallback import light as _light

light = _light


@pytest.mark.parametrize('failure', ['retain', 'receipt', 'clear'])
def test_post_call_receipt_failure_is_not_setup_failure(tmp_path, monkeypatch, light, failure):
    from supervisor import state

    store, ids, trace = _part_fixture(tmp_path, monkeypatch)
    originals = [store.get(ident) for ident in ids]
    def unavailable(*args, **kwargs):
        raise TimeoutError('Synthetic receipt storage unavailable')
    if failure == 'retain':
        monkeypatch.setattr(mf.chat_chain, 'retain_memory_source', unavailable)
    else:
        monkeypatch.setattr(mf, '_set_refusal', unavailable)
    answer = {'text': 'The owner limited both projects to research.', 'quotes': []} if failure == 'clear' else {
        'keep_original': True, 'reason': 'Retain the distinct obligations.'}
    llm, refreshed = _Light(json.dumps(answer)), []
    monkeypatch.setattr(state, 'update_budget_from_usage', refreshed.append)
    env = SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path)
    task = {'id': 'root-task', 'budget_drive_root': str(tmp_path), '_is_direct_chat': True}
    reason = pts._run_memory_fallback_draft(env, task, llm, tmp_path/'logs', trace)
    expected = 'receipt_clear_failed' if failure == 'clear' else 'receipt_failed'
    assert reason == expected
    assert len(llm.prompts) == len(refreshed) == 1
    assert refreshed[0]['cost'] == 0.0125  # projection refresh, not a canonical billing oracle
    events = [json.loads(line) for line in (tmp_path/'logs/events.jsonl').read_text().splitlines()]
    event = next(row for row in events if row.get('type') == 'memory_fallback_draft')
    assert event['kind'] == expected
    assert [store.get(ident) for ident in ids] == originals
    if failure == 'clear':
        assert event['outcome'] == 'published'
        assert store.get(event['record_id'])['text'] == answer['text']
    else:
        assert event['outcome'] == 'failed' and event['record_id'] is None
