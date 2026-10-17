"""The selected CLI runs explicit modules through both real pytest lanes."""
import importlib.util
import json
from pathlib import Path

import pytest

from ouroboros import test_selection
from tests.test_preflight_runner import _make_repo


@pytest.mark.serial
def test_selected_cli_skips_unrelated_collection_and_records_both_lanes(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("selected_cli", Path(__file__).resolve().parents[1] / "scripts/run_tests.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    repo = _make_repo(tmp_path, {
        ".gitignore": "__pycache__/\n.pytest_cache/\n",
        "tests/conftest.py": "def pytest_addoption(parser): parser.addoption('--ci-evidence-dir')\n",
        "tests/test_floor.py": "def test_floor(): assert True\n",
        "leaf.py": "value = 1\n",
        "tests/test_leaf.py": "from leaf import value\ndef test_leaf(): assert value == 2\n",
        "tests/test_unrelated.py": "raise AssertionError('must not collect')\n",
    })
    monkeypatch.setattr(cli, "REPO", repo)
    monkeypatch.setattr(test_selection, "FLOOR_TESTS", {"tests/test_floor.py": "minimal witness"})
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TEST_WORKERS", "2")
    monkeypatch.setenv("OURO_TEST_WORKER_BUDGET", "2")  # nested slots: the selected passes run two real workers
    monkeypatch.delenv("OUROBOROS_PREFLIGHT_SERIAL", raising=False)
    node = []
    monkeypatch.setattr(cli, "_node_lane", lambda: node.append(True) or 0)
    (repo / "leaf.py").write_text("value = 2\n", encoding="utf-8")
    evidence = tmp_path / "evidence"
    github_output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    args = ["--selected", "--base", "HEAD", "--evidence-dir", str(evidence)]
    assert cli.main(args) == 0
    row = json.loads((evidence / "selection.json").read_text(encoding="utf-8"))
    assert row["tier"] == "selected" and row["expected_files"] == ["tests/test_floor.py", "tests/test_leaf.py"]
    assert row["omitted_files"] == 1 and set(row["inventory"]) == {"parallel", "serial"}
    assert sorted(row["inventory"]["parallel"]["executed"]) == sorted(row["inventory"]["parallel"]["expected"])
    assert "selected: 2/3 Python modules" in capsys.readouterr().out
    assert node == [True]
    assert row["outcomes"] == {"parallel": "success", "serial": "success"}
    assert github_output.read_text(encoding="utf-8").splitlines() == ["parallel_outcome=success", "serial_outcome=success"]
    # The same entry point escalates a verifier change and exposes the hidden failure.
    with (repo / "pytest.ini").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed verifier\n")
    assert cli.main(args) != 0
    assert json.loads((evidence / "selection.json").read_text(encoding="utf-8"))["tier"] == "full"
    assert github_output.read_text(encoding="utf-8").splitlines()[-2:] == ["parallel_outcome=failure", "serial_outcome=skipped"]
    assert node == [True, True]


def test_pr_runs_selected_and_landed_runs_remain_full():
    from tests.test_platform_ci_events import WORKFLOW, _value

    job = WORKFLOW["jobs"]["quick-test"]
    steps = {step.get("id"): step for step in job["steps"] if step.get("id")}
    selected = steps["tests_selected"]
    assert '--selected --base "$PR_BASE"' in selected["run"]
    assert selected["env"]["PR_BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert selected["timeout-minutes"] > 0 and "continue-on-error" not in selected
    for event in ("pull_request", "push", "workflow_dispatch"):
        # Setup is already proven by each producer's own guard; exercise the event branch.
        for label, admitted in (("selected", event == "pull_request"), ("parallel", event != "pull_request"),
                                ("serial", event != "pull_request")):
            expression = steps[f"tests_{label}"]["if"].replace("steps.setup_python.outcome", "'success'").replace(
                "steps.setup_node.outcome", "'success'")
            assert bool(_value(expression, event=event, ref="refs/heads/ouroboros", base="ouroboros")) is admitted


@pytest.mark.parametrize("tier", ["selected", "full"])
def test_ci_summary_discloses_the_executed_tier(tmp_path, tier):
    from tests.ci_evidence import render_summary

    (tmp_path / "selection.json").write_text(json.dumps({"tier": tier}), encoding="utf-8")
    summary = render_summary(tmp_path / "parallel", producer_outcome="success", artifact_outcome="success")
    assert f"Selection: **{tier}**" in summary
