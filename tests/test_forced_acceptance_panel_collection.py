"""A deadline recorder never stamps over a panel it has not first collected.

`_record_forced_acceptance_bypass` writes the terminal truth for a turn a rail
ended. Its bypass reasons all say "the answer was never reviewed", so stamping
one over a panel that DID run is a false record — the defect the async
dispatch barrier introduced, since the panel now commonly outlives the model
round that bought it. The recorder therefore collects the turn's own panel at
$0 first and lets it speak: a clean PASS on the same binding and subject
accepts, a panel still waiting for its reviewers leaves the answer unaccepted
with `review_pending` (an answer that has not arrived is a gap, never a
verdict), and only a turn with no panel at all keeps the rail's bypass reason.
"""

from types import SimpleNamespace

import pytest

from ouroboros import review_dispatch
from ouroboros.loop_acceptance import _record_forced_acceptance_bypass
from ouroboros.loop_delivery import delivery_subject_hash

ANSWER = "The complete report includes the requested budget."
RAIL = "deadline_local"
BYPASS = "acceptance_bypassed_deadline"


def _clean_actor(state=None):
    actor = {
        "slot_id": "acceptance-one", "status": "ok", "signal": "PASS",
        "parsed": {"outcome_tier": "solved", "verdict": "PASS", "criteria_used": [
            {"criterion": "full report", "status": "supported", "evidence_refs": ["packet:criteria"]},
        ]},
    }
    if state:
        actor["operation_state"] = state
    return actor


def _run(subject_hash, *, actors, signal="PASS", degraded=False):
    return {
        "authority": "host_root", "panel_id": "panel-1", "binding_hash": "bind-1",
        "subject_hash": subject_hash, "aggregate_signal": signal, "degraded": degraded,
        "request": {"surface": "task_acceptance", "task_id": "root", "retry_key": "rk-1",
                    "subject": ANSWER},
        "slot_roster": [{"slot_id": "acceptance-one"}],
        "actors": actors,
    }


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """The real recorder over a real root context; only the rail is synthetic."""
    from ouroboros.contracts.task_contract import build_task_contract
    from ouroboros.task_results import STATUS_RUNNING, write_task_result
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "required")
    ctx = ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path, task_id="root")
    ctx.repo_dir.mkdir()
    ctx.task_metadata = {"root_task_id": "root", "delegation_role": "root"}
    ctx.task_contract = build_task_contract({"id": "root", **ctx.task_metadata})
    write_task_result(tmp_path, "root", STATUS_RUNNING, task_contract=ctx.task_contract, **ctx.task_metadata)

    def record(trace):
        _record_forced_acceptance_bypass(SimpleNamespace(
            tools=SimpleNamespace(_ctx=ctx), task_id="root",
            accumulated_usage={"reason_code": RAIL},
        ), trace, RAIL)

    return ctx, record


def _trace(ctx, run=None):
    trace = {"tool_calls": [], "review_runs": [run] if run else []}
    return trace


def test_turn_without_a_panel_keeps_the_rails_bypass_reason(recorder):
    """The quiet direction: nothing ran, so the rail's own reason is the truth."""
    ctx, record = recorder
    trace = _trace(ctx)
    record(trace)
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
    assert trace["acceptance_decision"]["reason"] == BYPASS
    assert trace["review_decision"]["eligibility"] == "eligible"


def test_settled_clean_pass_on_the_same_subject_accepts_without_a_bypass_stamp(recorder):
    ctx, record = recorder
    trace = _trace(ctx)
    subject = delivery_subject_hash(ctx, trace, ANSWER)
    trace["review_runs"] = [_run(subject, actors=[_clean_actor()])]
    record(trace)
    decision = trace["acceptance_decision"]
    assert decision["status"] == "accepted"
    assert decision["reason"] == "clean_pass"
    assert decision["reason"] not in {BYPASS}
    assert decision["reviewer_signal"] == "PASS"
    assert decision["reviewed_panel_id"] == "panel-1"


def test_settled_clean_pass_on_a_different_subject_does_not_accept(recorder):
    ctx, record = recorder
    trace = _trace(ctx)
    trace["review_runs"] = [_run("a-subject-nobody-delivered", actors=[_clean_actor()])]
    record(trace)
    decision = trace["acceptance_decision"]
    assert decision["status"] == "finalized_unaccepted"
    assert decision["reason"] == "review_degraded"
    assert "review_pending" not in decision


@pytest.mark.parametrize("stale", [{"superseded_by_revision": True}, {"owner_source_sha256": "an-older-owner-corpus"}])
def test_a_clean_pass_that_no_longer_speaks_for_this_turn_does_not_accept(recorder, stale):
    """A panel that judged an earlier revision, or older owner premises, is not this answer's
    review: the rail keeps its own "never reviewed" reason. The quiet direction is the
    same-subject test above — an un-superseded PASS on the current owner source accepts."""
    ctx, record = recorder
    trace = _trace(ctx)
    subject = delivery_subject_hash(ctx, trace, ANSWER)
    trace["review_runs"] = [{**_run(subject, actors=[_clean_actor()]), **stale}]
    record(trace)
    decision = trace["acceptance_decision"]
    assert decision["status"] == "finalized_unaccepted"
    assert decision["reason"] == BYPASS


def test_pending_panel_stays_unaccepted_with_review_pending_and_keeps_its_rows(recorder):
    ctx, record = recorder
    trace = _trace(ctx)
    subject = delivery_subject_hash(ctx, trace, ANSWER)
    pending = _run(subject, actors=[_clean_actor("pending_dispatch")], signal="", degraded=False)
    trace["review_runs"] = [pending]
    record(trace)
    decision = trace["acceptance_decision"]
    assert decision["status"] == "finalized_unaccepted"
    assert decision["reason"] == "review_degraded"
    assert decision["review_pending"] is True
    assert decision["reason"] != BYPASS
    # The panel's own rows are kept: the card still shows who had not answered.
    assert trace["review_runs"][0]["actors"][0]["operation_state"] == "pending_dispatch"


def test_panel_that_settles_pass_during_the_free_collection_is_accepted(recorder, monkeypatch):
    """The collection happens BEFORE the stamp: a panel that settles at $0
    decides the turn, instead of being overwritten by the rail."""
    ctx, record = recorder
    trace = _trace(ctx)
    subject = delivery_subject_hash(ctx, trace, ANSWER)
    trace["review_runs"] = [_run(subject, actors=[_clean_actor("pending_dispatch")], signal="")]
    collected = []

    def collect(run, *, drive_root, usage_ctx):
        collected.append(run)
        return SimpleNamespace(actors=[_clean_actor()], aggregate_signal="PASS", degraded=False,
                               parsed_findings=[], degraded_reasons=[])

    monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run", collect)
    record(trace)
    assert len(collected) == 1  # collected once, at $0
    assert trace["acceptance_decision"]["status"] == "accepted"
    assert trace["acceptance_decision"]["reason"] == "clean_pass"


def test_recorded_host_decision_is_never_overwritten(recorder):
    ctx, record = recorder
    trace = _trace(ctx)
    subject = delivery_subject_hash(ctx, trace, ANSWER)
    trace["review_runs"] = [_run(subject, actors=[_clean_actor()])]
    trace["acceptance_decision"] = {"status": "finalized_unaccepted", "reason": "author_stop",
                                    "source": "task_acceptance_review"}
    record(trace)
    assert trace["acceptance_decision"]["reason"] == "author_stop"
    assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
