"""Callable in-tree adapter checks; no live providers, GUI server or graders."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("payload,expected", [
    ({"final_answer": "  π\n", "result": "Narration"}, "  π\n"),
    ({"final_answer": "", "result": "Narration"}, ""),
    ({"result": "Narration"}, ""),
])
def test_inspect_reads_only_selected_runtime_bytes(tmp_path, monkeypatch, payload, expected):
    from devtools.benchmarks.gaia.inspect_solver import ouroboros_solver as solver
    monkeypatch.setenv("GAIA_OUROBOROS_RUN_ROOT", str(tmp_path))
    monkeypatch.setenv("GAIA_BWRAP_ISOLATE", "0")
    def cli(argv, **_kwargs):
        assert json.loads(argv[argv.index("--task-metadata-json") + 1])["answer_protocol"] == "final_answer_line"
        target = Path(argv[argv.index("--result-json-out") + 1])
        target.write_text(json.dumps(payload))
        return SimpleNamespace(returncode=0, stderr="", stdout="")
    monkeypatch.setattr(solver.subprocess, "run", cli)
    assert solver.run_ouroboros("Synthetic arithmetic question.")["final_answer"] == expected


def test_bridge_preflight_checks_executing_review_mode(tmp_path, monkeypatch):
    from devtools.benchmarks.common.model_slots import pin_single_model
    from devtools.benchmarks.osworld import run_cu_bridge_agent as bridge
    settings = {"OUROBOROS_TASK_REVIEW_MODE": "required"}
    pin_single_model("fixture/benchmark", target=settings)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(settings))
    target = dict(settings)
    calls = []
    def api(server, method, route, **_kw):
        calls.append((method, route))
        assert server == "http://fixture.invalid"
        return target
    monkeypatch.setattr(bridge, "_api", api)
    assert bridge._cu_actor_preflight(path, "http://fixture.invalid")["ok"]
    target["OUROBOROS_TASK_REVIEW_MODE"] = "auto"
    refused = bridge._cu_actor_preflight(path, "http://fixture.invalid")
    assert not refused["ok"]
    assert "executing server task review mode must be required" in refused["failures"]
    assert calls == [("GET", "/api/settings")] * 2


def test_real_gui_producer_receipt_precedes_result_admission(tmp_path):
    from tests.test_unix_computer_use_remote_backends import _osworld_impl
    from tests.test_acceptance_produced_material import context
    from ouroboros.acceptance_material import retain_acceptance_material
    from ouroboros.extension_surface_names import extension_surface_name
    from ouroboros.loop_acceptance import _task_acceptance_eligible
    calls = []
    _module, impl = _osworld_impl(tmp_path, calls, [{"status": 200,
        "result": {"status": "success", "returncode": 0, "output": ""}}])
    ctx, trace = context(tmp_path), {"tool_calls": []}
    retain_acceptance_material(ctx, trace, '{"action":"click", "x":10, "y":20}')
    assert not calls and not _task_acceptance_eligible("required", trace, False)[0]
    receipt = impl.click(x=10, y=20)
    assert calls and json.loads(receipt)["ok"] is True
    trace["tool_calls"].append({"tool": extension_surface_name("unix_computer_use", "click"),
                                 "result": receipt, "is_error": False, "tool_call_id": "gui-executed"})
    retained = retain_acceptance_material(ctx, trace, "Action finished.")
    assert retained["observed_effects"] and _task_acceptance_eligible("required", trace, False)[0]
    assert "objective" not in retained  # input execution does not certify success


def test_cl_launcher_requests_required_in_settings_and_child_env(tmp_path, monkeypatch):
    from devtools.benchmarks.continual_learning import run_clb
    base, run = tmp_path / "template.json", tmp_path / "run"
    base.write_text(json.dumps({"OUROBOROS_TASK_REVIEW_MODE": "auto"}))
    run.mkdir()
    monkeypatch.setattr(run_clb, "load_secret_env", lambda: {})
    settings = run_clb.render_run_settings(base, run, solve_model="fixture/benchmark",
                                         evolution=False, total_budget=1.0)
    args = SimpleNamespace(ouroboros_clone=str(tmp_path / "source"), effort="low", or_provider="")
    exported = run_clb._sanitized_child_env(run, settings, args)
    assert settings["OUROBOROS_TASK_REVIEW_MODE"] == exported["OUROBOROS_TASK_REVIEW_MODE"] == "required"
    assert json.loads((run / "_run_settings.json").read_text())["OUROBOROS_TASK_REVIEW_MODE"] == "required"
