"""The plan-review disclosure states the wave's CURRENT state (owner item spam D).

The suffix the owner reads at finalization said a review "remained open", which
is a verb about something that ended. Nothing had ended: the wave is open at
that exact moment, and a paid reviewer slot may still be in flight, so the one
fact the owner needs (a result is still owed) was missing while the sentence
implied the panel was over. An absent rail reason was worse still - it rendered
the internal token ``task_rail`` in backticks as if that were a cause.

New module rather than tests/test_owner_hurry_s3.py: that file is 935 lines and
these cases would carry it past the 1000-line target.
"""

from __future__ import annotations

import pathlib
import tempfile
import types

from ouroboros.owner_hurry import force_plan_decision, plan_review_disclosure
from ouroboros.task_results import write_task_result

_FINGERPRINTS = {"REVISE_PLAN": "a" * 64, "REVIEW_REQUIRED": "b" * 64, "DEGRADED": "c" * 64}


def _wave(aggregate: str, **fields) -> dict:
    return {
        "request_fingerprint": _FINGERPRINTS[aggregate], "aggregate": aggregate,
        "closed": False, "spec": {}, "findings": [], **fields,
    }


def _ctx_over(waves: list, current: str) -> types.SimpleNamespace:
    """A context whose durable authority is exactly the given wave series."""
    root = pathlib.Path(tempfile.mkdtemp())
    write_task_result(
        root, "hurry-custody", "running",
        plan_review_state={
            "schema_version": 2, "series_id": "series-1", "cycles_paid": len(waves),
            "need_evidence_seen": [],
            "current_attempt": {
                "fingerprint": _FINGERPRINTS[current], "status": "open", "reason": "",
            },
            "waves": waves, "waves_omitted": 0,
        },
    )
    return types.SimpleNamespace(
        task_id="hurry-custody", budget_drive_root=str(root), drive_root=str(root),
        repo_dir=root, system_repo_dir=root,
        task_metadata={"force_plan": True},
    )


def test_a_three_wave_series_discloses_the_open_state_never_a_past_tense() -> None:
    """REVISE_PLAN then REVIEW_REQUIRED then DEGRADED: the last wave is the one
    the projection carries, and it is OPEN when the rail fires."""
    ctx = _ctx_over(
        [_wave("REVISE_PLAN"), _wave("REVIEW_REQUIRED"), _wave("DEGRADED")], "DEGRADED",
    )
    decision = force_plan_decision(ctx, {}, hard_rail="round_limit", enforcement="blocking")

    disclosure = plan_review_disclosure(decision, "round_limit")

    assert "Blocking plan review is open (DEGRADED" in disclosure
    assert "remained" not in disclosure
    assert "`round_limit`" in disclosure


def test_a_late_result_still_owed_reaches_the_owner_line() -> None:
    """plan_review_runtime keeps a wave open and DEGRADED precisely because a
    paid slot can still settle; that typed fact is what the owner is missing."""
    ctx = _ctx_over(
        [_wave("DEGRADED", custody_pending=True, reasons=["review_late_result_pending"])],
        "DEGRADED",
    )
    decision = force_plan_decision(ctx, {}, hard_rail="round_limit", enforcement="blocking")

    assert decision["review_late_result_pending"] is True
    assert "a late result is still owed" in plan_review_disclosure(decision, "round_limit")

    quiet = force_plan_decision(
        _ctx_over([_wave("DEGRADED")], "DEGRADED"), {},
        hard_rail="round_limit", enforcement="blocking",
    )
    assert "review_late_result_pending" not in quiet
    assert "late result" not in plan_review_disclosure(quiet, "round_limit")


def test_advisory_enforcement_also_states_the_wave_is_open() -> None:
    ctx = _ctx_over([_wave("DEGRADED")], "DEGRADED")
    decision = force_plan_decision(ctx, {}, enforcement="advisory")

    disclosure = plan_review_disclosure(decision)

    assert decision["allow"] is True
    assert "Plan review is still open (DEGRADED" in disclosure
    assert "remained" not in disclosure
    assert "advisory enforcement" in disclosure


def test_a_rail_with_no_recorded_reason_renders_absence_not_an_internal_token() -> None:
    """The owner-visible suffix quoted ``task_rail`` in backticks when the rail
    had no recorded reason, which reads as a named cause and is not one."""
    railed = {
        "required": True, "enforcement": "blocking", "status": "rail_degraded",
        "outcome": "DEGRADED", "reason": "",
    }

    without_reason = plan_review_disclosure(railed)
    assert "task_rail" not in without_reason
    assert "a task-wide rail required best-effort finalization." in without_reason

    with_reason = plan_review_disclosure({**railed, "reason": "budget_exhausted"})
    assert "the task-wide rail `budget_exhausted` required best-effort" in with_reason

    forced = plan_review_disclosure(railed, "round_limit")
    assert "the task-wide rail `round_limit` required best-effort" in forced
