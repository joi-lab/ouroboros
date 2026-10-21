"""Offline diagnostics for the live smoke's failed and successful review paths."""

import json
from types import SimpleNamespace

import pytest

from ouroboros.skill_review import _skill_quorum_failure_outcome
from ouroboros.skill_review_prompt import _SKILL_REVIEW_ITEMS
from ouroboros.triad_review import parse_model_review_results
from tests import test_skill_smoke_official as smoke
from tests._skill_review_shared import _make_actor


@pytest.mark.parametrize("prior_review", [False, True])
@pytest.mark.parametrize("provider_error", [False, True])
def test_smoke_diagnostics_show_current_quorum_failure(tmp_path, monkeypatch,
                                                     prior_review, provider_error):
    monkeypatch.setattr(smoke, "DATA_DIR", tmp_path)
    name = "smoke-diagnostic"
    state = smoke._skill_state_dir(name)
    if prior_review:
        state.mkdir(parents=True)
        (state / "review.json").write_text(json.dumps({
            "status": "clean", "content_hash": "old-hash", "raw_actor_records": [],
        }), encoding="utf-8")
    # Both transport failures and malformed answers miss the same quorum. Keep
    # enough text to expose accidental diagnostic truncation, including its end.
    raw = ("403 Inference is blocked on this account" if provider_error
           else "I have not completed the review") + " evidence" * 2000 + " END-WITNESS"
    actor = _make_actor(smoke._REVIEW_MODEL, raw)
    if provider_error:
        actor["verdict"] = "ERROR"
    result = {"results": [actor]}
    parsed = parse_model_review_results(result, required_items=_SKILL_REVIEW_ITEMS)
    assert not parsed.responsive_models
    outcome = _skill_quorum_failure_outcome(
        SimpleNamespace(name=name), findings=parsed.findings,
        models=[smoke._REVIEW_MODEL], content_hash="current-hash", required_quorum=1,
        result_json_text=json.dumps(result), parsed_review=parsed, advisory_evidence={},
        single_reviewer_no_diversity=True, drive_root=tmp_path, persist=True,
    )
    assert outcome.status == "pending"
    assert (state / "review.json").exists() is prior_review
    diagnostic = smoke._review_diag(name, outcome.status, outcome.error,
                                   {"review_findings": outcome.findings})
    assert json.dumps(raw) in diagnostic, diagnostic
    assert "current-hash" in diagnostic, diagnostic


def test_smoke_diagnostics_keep_successful_review_actors(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "DATA_DIR", tmp_path)
    state = smoke._skill_state_dir("clean")
    state.mkdir(parents=True)
    actors = [{"raw_text": "complete successful review"}]
    (state / "review.json").write_text(json.dumps({
        "reviewer_models": [smoke._REVIEW_MODEL], "raw_actor_records": actors,
    }), encoding="utf-8")
    diagnostic = smoke._review_diag("clean", "clean", "", {"review_findings": []})
    assert smoke._REVIEW_MODEL in diagnostic
    assert json.dumps(actors) in diagnostic


@pytest.mark.parametrize("case,calls", [
    ("in_flight", 1), ("custody_lost", 1), ("pending_dispatch", 1),
    ("late_result_pending", 1), ("missing_history", 1), ("wrong_hash", 1),
    ("empty_records", 1), ("unknown_record", 1), ("nested_pending", 1),
    ("blockers", 1), ("settled_error", 2), ("late_settled_error", 2),
    ("not_dispatched", 2), ("deps_failed", 2), ("clean", 1),
])
def test_smoke_retry_preserves_original_wave_and_requires_settlement(tmp_path, monkeypatch, case, calls):
    from ouroboros.skill_review_history import append_history_once, review_history_path

    name = "bounded-smoke"
    monkeypatch.setattr(smoke, "DATA_DIR", tmp_path)
    monkeypatch.setattr(smoke, "find_skill", lambda *_: SimpleNamespace(content_hash="current-hash"))
    status = "clean" if case in {"clean", "deps_failed"} else "blockers" if case == "blockers" else "pending"
    state = case if case in {"in_flight", "custody_lost", "pending_dispatch", "not_dispatched"} else "settled"
    if case == "late_settled_error":
        state = "late_settled"
    records = [{"operation_state": state, "late_result_pending": case == "late_result_pending"}]
    if case == "empty_records":
        records = []
    elif case == "unknown_record":
        records = [{}]
    elif case == "nested_pending":
        records = [{"raw_results": [{"operation_state": "in_flight"}]}]
    path = review_history_path(tmp_path, name)
    before, observed = None, []

    def flow(slug, skill):
        nonlocal before
        observed.append((slug, skill))
        if len(observed) == 1:  # the first attempt records the original wave, as review_skill does
            if case != "missing_history":
                assert append_history_once(tmp_path, name, {"job_id": "original-wave", "status": status,
                    "content_hash": "old-hash" if case == "wrong_hash" else "current-hash",
                    "raw_actor_records": records, "paid": True})
            before = path.read_bytes() if path.exists() else None
            return status, "" if status in {"clean", "blockers"} else "first failure", (
                "failed" if case == "deps_failed" else "installed"), {}
        assert (path.read_bytes() if path.exists() else None) == before  # the retry keeps the original wave
        return "clean", "", "installed", {}

    monkeypatch.setattr(smoke, "_run_review_flow", flow)
    result = smoke._review_with_one_settled_retry("a2a", name)
    assert len(observed) == calls
    assert (path.read_bytes() if path.exists() else None) == before
    if calls == 1 and case != "clean":
        assert result[0] == status and "not retried:" in result[-1]
    else:
        assert result[:3] == ("clean", "", "installed")
