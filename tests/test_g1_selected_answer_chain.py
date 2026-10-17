"""Synthetic selected-answer chain: real loop, store, gateway GET, CLI and Inspect.

Only model transport, process launch and admission are substituted. This is not
live server startup, provider execution or external benchmark qualification.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tests.test_acceptance_async_loop import full_loop as _full_loop

full_loop = _full_loop
pytestmark = pytest.mark.serial


@pytest.mark.parametrize('content, expected', [
    ('Synthetic computation.\nFINAL ANSWER: éclair π', 'éclair π'),
    ('Withdrawn answer.\nFINAL ANSWER: ', ''),
    ('Narration without a selected answer.', ''),
])
def test_selected_answer_survives_store_gateway_cli_and_inspect(full_loop, tmp_path, monkeypatch, content, expected):
    from ouroboros import cli, loop
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.gateway.tasks import api_task_get
    from ouroboros.outcomes import derive_loop_outcome
    from ouroboros.task_results import write_task_result
    from devtools.benchmarks.gaia.inspect_solver import ouroboros_solver as solver

    f = full_loop
    monkeypatch.setenv('OUROBOROS_TASK_REVIEW_MODE', 'off')
    f.ctx.task_metadata['answer_protocol'] = 'final_answer_line'
    monkeypatch.setattr(loop, 'call_llm_with_retry', lambda *a, **kw: (
        {'content': content}, 0.0))
    answer, usage, trace = f.run()
    outcome = derive_loop_outcome(answer, usage, trace)
    selected = trace['acceptance_material'].get('selected_answer')
    assert outcome['final_answer'] == expected
    assert (selected['text'] if selected else '') == expected
    write_task_result(f.ctx.drive_root, f.ctx.task_id, 'completed', result=answer,
                      final_answer=outcome['final_answer'], loop_outcome=outcome,
                      outcome_axes=outcome['outcome_axes'], cost_final=True)
    app = Starlette(routes=[Route('/api/tasks/{task_id}', api_task_get)])
    app.state.drive_root = f.ctx.drive_root
    app.state.repo_dir = f.ctx.repo_dir
    requests = []
    with TestClient(app) as http:
        class Client:
            timeout = 30
            def request(self, method, path, body=None, **kw):
                requests.append((method, path))
                if method == 'POST':
                    assert body['metadata']['answer_protocol'] == 'final_answer_line'
                    return {'task_id': f.ctx.task_id}  # admission is outside this result-transport check
                response = http.get(path)
                assert response.status_code == 200, response.text
                return response.json()
        monkeypatch.setattr(cli, '_client', lambda *a, **kw: Client())
        def invoke(argv, **kw):
            assert argv[1:3] == ['-m', 'ouroboros.cli']
            status = cli.main([*argv[3:], '--no-stream'])
            return SimpleNamespace(returncode=status, stdout='', stderr='')
        monkeypatch.setattr(solver.subprocess, 'run', invoke)
        monkeypatch.setenv('GAIA_OUROBOROS_RUN_ROOT', str(tmp_path / 'inspect'))
        monkeypatch.setenv('GAIA_BWRAP_ISOLATE', '0')
        result = solver.run_ouroboros('A synthetic Unicode answer.')
    assert result['final_answer'] == expected
    if selected:
        assert result['final_answer'].encode() == read_actor_source_bytes(
            f.ctx.drive_root, f.ctx.task_id, selected['source_ref'])
    assert requests[0][0] == 'POST' and any(method == 'GET' for method, _ in requests)
