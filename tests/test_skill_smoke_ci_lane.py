"""The `skill-smoke` CI job: its secret boundary and its paid review legs' budget.

The live skill review stays in the release gate under the product's own review
deadlines (owner, 2026-10-19: a deterministic simulation would not notice a
behavioural regression). Two properties of the job keep that gate able to pass
when the product works, so they are pinned against the workflow itself:

- the secret boundary: one step names OPENROUTER_API_KEY, it runs only in a
  review leg, and a review leg runs nothing that imports downloaded plugin code;
- the budget: one ubuntu review leg per review skill, each bounded above the
  test's own timeout (``_REVIEW_TEST_TIMEOUT_SEC``), which itself covers the
  product's review window, so a slow but working review is never cut by CI.
"""

from __future__ import annotations

import pathlib
import re

import yaml

import tests.test_skill_smoke_official as smoke
from tests.test_system_e2e_ci_lane import _job_text

CI_PATH = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _job() -> dict:
    return yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))["jobs"]["skill-smoke"]


def _leg_minutes(expression: str) -> tuple:
    """``(review, install)`` minutes of the job's one ``timeout-minutes`` expression."""
    match = re.fullmatch(r"\$\{\{ matrix\.lane == 'review' && (\d+) \|\| (\d+) \}\}", str(expression))
    assert match, expression
    return int(match[1]), int(match[2])


def test_one_review_leg_per_review_skill_bounded_above_the_tests_own_timeout():
    job = _job()
    legs = job["strategy"]["matrix"]["include"]
    review = [leg for leg in legs if leg["lane"] == "review"]
    assert sorted(leg["skill"] for leg in review) == sorted(smoke.REVIEW_SKILLS)
    assert {leg["os"] for leg in review} == {"ubuntu-latest"}  # an LLM verdict is OS-independent
    assert sorted(leg["os"] for leg in legs if leg["lane"] == "install") == [
        "macos-latest", "ubuntu-latest", "windows-latest"]
    review_minutes, install_minutes = _leg_minutes(job["timeout-minutes"])
    # Setup on top of the test's own bound, so the test's named timeout fires before the runner's.
    assert review_minutes * 60 >= smoke._REVIEW_TEST_TIMEOUT_SEC + 10 * 60, review_minutes
    assert install_minutes == 45


def test_the_secret_lives_only_in_the_review_step_of_a_review_leg():
    job = _job()
    # Never job-wide (the job above, integration-optional, carries its key at job level): every
    # install leg runs downloaded plugin code. The whole block names exactly one secret.
    assert "secrets." not in str(job.get("env") or {}), job.get("env")
    assert _job_text("skill-smoke").count("secrets.") == 1
    steps = job["steps"]
    secret = [step for step in steps if "OPENROUTER_API_KEY" in str(step.get("env") or {})]
    assert len(secret) == 1 and secret[0]["if"] == "matrix.lane == 'review'", secret
    run = secret[0]["run"]
    assert "tests/test_skill_smoke_official.py::test_review_grants_and_enable[${{ matrix.skill }}]" in run
    assert "-m skill_smoke" in run and "--timeout=" not in run  # the test's own marker is the bound
    others = [step for step in steps if "run" in step and step is not secret[0]]
    assert others and all("matrix.lane == 'install'" in str(step.get("if")) for step in others), others
