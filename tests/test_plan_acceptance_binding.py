"""Task acceptance over the planning organ: ONE binder. Ingress claims win (and the adopted
promises stay visible), otherwise the LATEST revision's done_when binds — open objections or
not — with a history exhibit carrying every revision's actual text; a legacy record binds
what the old organ selected; an unreadable record reaches the infrastructure-failure path."""
from __future__ import annotations

import types as _t

import pytest

from ouroboros import plan_record
from ouroboros.review_evidence import build_task_acceptance_evidence
from ouroboros.task_results import STATUS_RUNNING, write_task_result


def _plan(root, task_id, *revisions, objection=None):
    write_task_result(root, task_id, STATUS_RUNNING, result="running")
    latest = None
    for index, claims in enumerate(revisions, start=1):
        fields = plan_record.compose_revision(latest, goal="ship", plan=f"plan v{index}", done_when=claims, affected_paths=[])
        _record, latest, _created = plan_record.append_revision(root, task_id, fields, body=False)
    if objection:
        plan_record.append_ask(root, task_id, {"ask_id": "a1", "ts": "t", "revision": 1, "message": "look", "participants": [
            {"seat_id": "s1", "model": "m", "route": "api_chat", "owner_marked": True, "dispatched": True, "status": "answered",
             "reply": {"prose": "", "objections": [{"id": "o1", "text": objection}], "withdrawn": []}}]})


def _ctx(root, task_id, **contract):
    return _t.SimpleNamespace(task_contract={"requirements": "do X", **contract}, task_metadata={}, drive_root=str(root),
                              task_id=task_id, repo_dir=str(root))


def test_the_latest_revision_binds_always_with_every_revisions_text_in_the_exhibit(tmp_path):
    _plan(tmp_path, "acc", [{"id": "c1", "claim": "five slides", "support": "count"}],
          [{"id": "c1", "claim": "a five-slide story", "support": "read it"}, {"id": "c2", "claim": "speaker notes", "support": "open them"}],
          objection="the bar is low")
    ev = build_task_acceptance_evidence(_ctx(tmp_path, "acc"), llm_trace={"tool_calls": []}, drive_root=tmp_path, task_id="acc")
    assert ev["acceptance_claims_source"] == "plan_review"
    assert [c["claim"] for c in ev["task_contract"]["acceptance_claims"]] == ["a five-slide story", "speaker notes"]
    assert ev["task_contract"]["acceptance_claims"][0]["id"] == "c1" and ev["__provenance__"]["plan_claims_exhibit"] == "host_attested"
    exhibit = ev["plan_claims_exhibit"]
    assert exhibit["binding"] == "latest_revision:2"
    assert [[c["claim"] for c in r["done_when"]] for r in exhibit["revisions"]] == [["five slides"], ["a five-slide story", "speaker notes"]]
    assert exhibit["revisions"][0]["seen_by_asks"] == ["a1"] and exhibit["revisions"][1]["seen_by_asks"] == []
    assert exhibit["open_objections"] == [{"seat_id": "s1", "id": "o1", "text": "the bar is low", "holds": True,
                                          "not_adjudicated": False, "ask_id": "a1", "revision": 1}]


def test_ingress_claims_win_and_the_adopted_promises_stay_visible(tmp_path):
    _plan(tmp_path, "acc", [{"id": "c1", "claim": "plan promise", "support": "x"}])
    ev = build_task_acceptance_evidence(_ctx(tmp_path, "acc", acceptance_claims=[{"id": "adapter_1", "claim": "adapter claim"}]),
                                        llm_trace={"tool_calls": []}, drive_root=tmp_path, task_id="acc")
    assert ev["acceptance_claims_source"] == "ingress_contract"
    assert [c["claim"] for c in ev["task_contract"]["acceptance_claims"]] == ["adapter claim"]
    assert ev["plan_claims_exhibit"]["binding"] == "ingress_contract"
    assert ev["plan_claims_exhibit"]["adopted_promises"] == [{"id": "c1", "claim": "plan promise", "support": "x"}]


def test_a_task_without_a_plan_carries_no_exhibit(tmp_path):
    write_task_result(tmp_path, "acc", STATUS_RUNNING, result="running")
    ev = build_task_acceptance_evidence(_ctx(tmp_path, "acc"), llm_trace={"tool_calls": []}, drive_root=tmp_path, task_id="acc")
    assert "acceptance_claims_source" not in ev and "plan_claims_exhibit" not in ev


def test_an_unreadable_plan_record_reaches_the_infrastructure_failure_path(tmp_path, monkeypatch):
    from ouroboros.review_evidence_sections import _accept_effective_claims
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable

    _plan(tmp_path, "acc", [{"id": "c1", "claim": "x", "support": ""}])
    monkeypatch.setattr("ouroboros.plan_record.load_plan_record",
                        lambda *_a, **_k: (_ for _ in ()).throw(plan_record.PlanRecordInvalid("PLAN_RECORD_INVALID: torn")))
    with pytest.raises(PlanReviewSourceUnavailable):
        _accept_effective_claims(_ctx(tmp_path, "acc"), {"requirements": "do X"}, tmp_path, "acc")
    # Generic lookup failures stay fail-soft.
    monkeypatch.setattr("ouroboros.plan_record.load_plan_record", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert _accept_effective_claims(_ctx(tmp_path, "acc"), {"requirements": "do X"}, tmp_path, "acc") == ([], "", {})


def test_an_unreadable_earlier_state_never_drops_the_latest_revisions_bar(tmp_path):
    """Scope R1, Fable 4 (+ the one legacy loader): the earlier-schema state's own ValueError used to
    fall into the binder's fail-soft branch, dropping the readable record's latest done_when so
    acceptance judged without the bar. Now the latest revision binds and the unread state is a
    typed fact on the exhibit; with no revision to bind, the typed infrastructure failure speaks."""
    from ouroboros.review_evidence_sections import _accept_effective_claims
    from ouroboros.task_result_schema import stamp_task_result_schema
    from ouroboros.task_store import patch_document
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable

    _plan(tmp_path, "acc", [{"id": "c1", "claim": "five slides", "support": "count"}])
    patch_document(tmp_path, "acc", lambda doc: stamp_task_result_schema({**doc, "plan_review_state": {"schema_version": 99}}))
    claims, source, exhibit = _accept_effective_claims(_ctx(tmp_path, "acc"), {"requirements": "do X"}, tmp_path, "acc")
    assert source == "plan_review" and [c["claim"] for c in claims] == ["five slides"] and exhibit["binding"] == "latest_revision:1"
    assert exhibit["legacy_unreadable"]["source"] == "plan_review_state" and "PLAN_REVIEW_STATE_INVALID" in exhibit["legacy_unreadable"]["error"]
    write_task_result(tmp_path, "bare", STATUS_RUNNING, result="running")
    patch_document(tmp_path, "bare", lambda doc: stamp_task_result_schema({**doc, "plan_review_state": {"schema_version": 99}}))
    with pytest.raises(PlanReviewSourceUnavailable, match="PLAN_REVIEW_STATE_UNAVAILABLE: plan_review_state"):
        _accept_effective_claims(_ctx(tmp_path, "bare"), {"requirements": "do X"}, tmp_path, "bare")
    # The other direction: a readable earlier state carries no such fact.
    _plan(tmp_path, "clean", [{"id": "c1", "claim": "x", "support": ""}])
    assert "legacy_unreadable" not in _accept_effective_claims(_ctx(tmp_path, "clean"), {"requirements": "do X"}, tmp_path, "clean")[2]
