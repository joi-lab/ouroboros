"""The size_ratchet CI steps are the ONLY blocking surface for the size gates
(local runs merely warn). The marker-guards canary proves the lane is
non-empty — it cannot see the RUN steps being deleted from ci.yml — so their
presence (and the explicit event-base env) is pinned here, per job."""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("job", ["quick-test", "full-test"])
def test_each_ci_job_runs_the_blocking_size_ratchet_lane(job):
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    block = re.search(
        rf"^  {re.escape(job)}:\n(.*?)(?=^  [A-Za-z0-9_-]+:$|\Z)", ci, re.MULTILINE | re.DOTALL
    )
    assert block, f"ci.yml has no `{job}:` job"
    body = block.group(1)
    assert "run: python -m pytest tests/ -m size_ratchet" in body, (
        f"{job} no longer runs the blocking size_ratchet lane"
    )
    assert "OURO_SIZE_RATCHET_BASE_REF" in body, (
        f"{job}'s size_ratchet step lost its explicit event-base env"
    )
