"""The post-merge workflow's boundaries (owner decision 3A, 2026-10-21).

It runs after pushes to `ouroboros` in the official repository only, coalesces merges instead of
cancelling a running check, holds exactly the permissions its hand-over needs, uses no secret but
the workflow token, pins every third-party action by commit and installs no project into the
environment its probe worktrees share.
"""
from __future__ import annotations

from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / ".github/workflows/post-merge.yml").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(TEXT)
JOB = WORKFLOW["jobs"]["post-merge"]
STEPS = {step.get("id") or step.get("uses") or step["name"]: step for step in JOB["steps"]}


def test_it_runs_after_pushes_to_ouroboros_only_and_in_the_official_repository_only():
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))  # PyYAML's YAML 1.1 spelling of "on"
    assert triggers == {"push": {"branches": ["ouroboros"]}}  # no tags, PRs, dispatch or schedule
    assert list(WORKFLOW["jobs"]) == ["post-merge"]
    assert JOB["if"] == "github.repository == 'razzant/ouroboros'"
    assert JOB["runs-on"] == "ubuntu-latest" and JOB["timeout-minutes"] == 150


def test_merges_coalesce_behind_a_running_check_that_is_never_cancelled():
    assert WORKFLOW["concurrency"] == {"group": "post-merge-${{ github.ref }}", "cancel-in-progress": False}
    assert "concurrency" not in JOB


def test_it_holds_exactly_the_permissions_of_its_hand_over_and_no_secret():
    assert WORKFLOW["permissions"] == {"contents": "read", "actions": "read", "issues": "write",
                                       "pull-requests": "write"}
    assert "permissions" not in JOB
    assert "secrets." not in TEXT and "secrets:" not in TEXT
    tokens = {name: value for step in JOB["steps"] for name, value in (step.get("env") or {}).items()
              if "TOKEN" in name}
    assert tokens == {"GH_TOKEN": "${{ github.token }}"}  # base and route read GitHub, nothing else does
    assert [step["id"] for step in JOB["steps"] if "GH_TOKEN" in (step.get("env") or {})] == ["base", "route"]


def test_every_action_is_pinned_by_commit_or_local():
    uses = [step["uses"] for step in JOB["steps"] if "uses" in step]
    assert uses and all(re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", use) or use.startswith("./.github/actions/")
                        for use in uses), uses
    assert sum(use.startswith("./.github/actions/") for use in uses) == 1


def test_probes_import_their_own_tree_because_no_project_is_installed():
    setup = STEPS["setup_python"]
    assert setup["uses"] == "./.github/actions/setup-python-env"
    assert setup["with"] == {"interpreter": "shipped", "install-project": "false"}
    assert STEPS["actions/checkout@11d5960a326750d5838078e36cf38b85af677262"]["with"] == {"fetch-depth": 0}
    assert STEPS["setup_node"]["with"] == {"node-version": "22"}


def test_the_steps_check_retry_attribute_hand_over_and_report_in_order():
    order = [step.get("id") for step in JOB["steps"] if step.get("id")]
    assert order == ["setup_python", "setup_node", "base", "selected", "classify", "attribute", "route"]
    selected = STEPS["selected"]
    assert selected["continue-on-error"] is True  # the retry, not this step, decides FAILED vs FLAKY
    assert "scripts/run_tests.py --selected --base \"$BASE\" --sequential --keep-going" in selected["run"]
    assert selected["env"]["BASE"] == "${{ steps.base.outputs.base }}"
    for name in ("classify", "attribute", "route"):
        assert f"scripts/post_merge_check.py {name} " in STEPS[name]["run"]
        assert "continue-on-error" not in STEPS[name] and STEPS[name]["if"].startswith("${{ !cancelled() &&")
    # Bisection needs a finished retry; the hand-over also takes what a killed retry or bisection recorded.
    assert "steps.classify.outcome == 'success'" in STEPS["attribute"]["if"]
    assert STEPS["route"]["if"] == "${{ !cancelled() && steps.base.outcome == 'success' }}"
    publish = JOB["steps"][-1]
    assert publish["run"].startswith("python scripts/post_merge_check.py summary ")
    assert publish["if"] == "${{ !cancelled() }}" and "continue-on-error" not in publish
    # Every step, the job timeout included, ends before GitHub would cancel the run.
    assert sum(step.get("timeout-minutes", 0) for step in JOB["steps"]) < JOB["timeout-minutes"]


def test_nothing_reverts_pushes_or_schedules():
    lowered = TEXT.lower()
    for forbidden in ("git push", "git revert", "git reset", "schedule:", "workflow_dispatch", "pull_request"):
        assert forbidden not in lowered, forbidden
