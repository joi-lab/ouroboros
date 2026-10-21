"""Plan-review facts of one task (``ouroboros.plan_review_facts``).

What the plan's reviewers objected to and how the author answered, projected from the
task's plan record (else its earlier-schema plan-review state) — bounded, with a source
pointer, never a score. The reflection row a late task-acceptance settlement appended is
retired with task acceptance.
"""
from __future__ import annotations

import json

import pytest

from ouroboros.plan_review_facts import (
    PLAN_REVIEW_REFLECTION_CHARS,
    facts_from_state,
    plan_review_reflection_slice,
    render_plan_review_section,
)

FP = "a" * 64


def _wave(**overrides):
    wave = {
        "request_fingerprint": FP, "cycle_index": 1, "aggregate": "REVIEW_REQUIRED", "closed": False, "paid": True,
        "counts": {"blocking": 1, "need_evidence": 1, "note": 1},
        "spec": {"goal": "Ship the deck",
                 "acceptance_claims": [{"id": "claim_1", "claim": "The deck has ten slides"},
                                       {"id": "claim_2", "claim": "Every slide has speaker notes"}],
                 "decisions": [{"id": "decision_1", "choice": "Use the house template"}], "deferred": []},
        "findings": [
            {"finding_id": "s1:f1", "id": "f1", "slot": "s1", "model": "grok-4.7", "class": "blocking",
             "breaks": "claim_1", "locator": "", "summary": "Ten slides is not enough for the agenda"},
            {"finding_id": "s2:f2", "id": "f2", "slot": "s2", "model": "astra", "class": "need_evidence",
             "breaks": "claim_2", "locator": "", "summary": "Who writes the notes?"},
            {"finding_id": "s2:f3", "id": "f3", "slot": "s2", "model": "astra", "class": "note",
             "breaks": "decision_1", "locator": "", "summary": "The template is dated"},
        ],
        "dispositions": [{"finding_id": "s1:f1", "decision": "accept", "rationale": "Twelve slides then"}],
        "wave_artifact": {"path": "artifacts/plan/wave-1.json", "sha256": "b" * 64},
        "actors": [{"slot_id": "s1", "ok": True, "operation_state": "settled"},
                   {"slot_id": "s2", "ok": False, "operation_state": "in_flight", "late_result_pending": True}],
    }
    wave.update(overrides)
    return wave


def _author_plan():
    return {"kind": "plan_author_subject", "fingerprint": "c" * 64, "review_fingerprint": FP,
            "spec": {"goal": "Ship the deck",
                     "acceptance_claims": [{"id": "claim_1", "claim": "The deck has twelve slides"}],
                     "decisions": [{"id": "decision_1", "choice": "Use the house template"}], "deferred": []},
            "author_disposition": {"action": "finish", "enforcement": "advisory",
                                   "rationale": "Dropped speaker notes; the owner agreed"}}


def test_facts_name_each_element_its_findings_answers_and_fate_in_the_selected_plan():
    from ouroboros.tools.plan_review_runtime import plan_wave_slot_census

    wave = _wave()
    facts = facts_from_state({"waves": [wave]}, critic=wave, author_plan=_author_plan(), claims_source="author_plan",
                             census=plan_wave_slot_census(wave), source_ref={"kind": "task_result", "task_id": "t"})

    by_id = {row["id"]: row for row in facts["elements"]}
    assert set(by_id) == {"claim_1", "claim_2", "decision_1"}
    assert by_id["claim_1"]["changed_in_selected_plan"] == "changed"
    assert by_id["claim_1"]["findings"][0]["disposition"] == "accept: Twelve slides then"
    assert by_id["claim_1"]["findings"][0]["model"] == "grok-4.7"
    assert by_id["claim_2"]["changed_in_selected_plan"] == "removed"
    assert by_id["claim_2"]["findings"][0]["disposition"] == "unanswered"
    assert by_id["decision_1"]["changed_in_selected_plan"] == "same"
    assert facts["questions"] == [{"finding_id": "s2:f2", "breaks": "claim_2",
                                   "question": "Who writes the notes?", "answer": "unanswered"}]
    assert facts["reviewers_without_a_merged_answer"] == {"awaiting": 0, "unresolved": 1, "uncollected": 0, "slots": ["s2"]}
    assert facts["claims_source"] == "author_plan"
    assert facts["wave_ref"] == wave["wave_artifact"]
    assert facts["selected_plan"]["delta"]["removed"] == ["claim_2"] and facts["selected_plan"]["delta"]["changed"] == ["claim_1"]
    assert "enforcement" not in facts["selected_plan"] and "paid" not in facts["waves"][0]
    assert facts["reviewed_plan"] == {"aggregate": "REVIEW_REQUIRED", "closed": False, "cycle": 1,
                                      "closure_notes": [], "closure_notes_total": 0}
    assert facts["unchanged_elements_without_findings"] == 1  # the goal
    assert "omitted" not in facts
    # Facts, not judgement: no key anywhere in the slice scores a reviewer or ranks a finding.
    assert not {"score", "rank", "weight"} & set(_keys(facts))


def _keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value)


def test_element_fates_follow_the_texts_not_the_positional_ids():
    """Ids are positional: dropping the first claim moves the second into its slot. The
    reviewed first claim is REMOVED (its text is gone), the second is RENUMBERED, an invariant
    edited in place is CHANGED, a changed goal is CHANGED, and an element the selected plan
    introduces is ADDED."""
    critic = {"goal": "Ship the deck", "invariants": ["Keep the house voice", "No new fonts"],
              "acceptance_claims": [{"id": "claim_1", "claim": "A"}, {"id": "claim_2", "claim": "B"}],
              "decisions": [], "deferred": []}
    selected = {"goal": "Ship the deck by Friday", "invariants": ["Use a new voice", "No new fonts"],
                "acceptance_claims": [{"id": "claim_1", "claim": "B"}, {"id": "claim_2", "claim": "C"}],
                "decisions": [], "deferred": []}
    wave = _wave(spec=critic, findings=[{"finding_id": "s1:f1", "id": "f1", "slot": "s1", "model": "m",
                                        "class": "blocking", "breaks": "invariant_1", "locator": "", "summary": "voice"}],
                 dispositions=[{"finding_id": "s1:f1", "decision": "accept", "rationale": "rewritten"}])
    author = {**_author_plan(), "spec": selected}

    facts = facts_from_state({"waves": [wave]}, critic=wave, author_plan=author)

    fates = {row["id"]: row["changed_in_selected_plan"] for row in facts["elements"]}
    assert fates == {"claim_1": "removed", "claim_2": "renumbered→claim_1", "invariant_1": "changed",
                     "goal": "changed", "selected:claim_2": "added"}
    [added] = [row for row in facts["elements"] if row["id"] == "selected:claim_2"]
    assert added["text"] == "C"
    assert facts["unchanged_elements_without_findings"] == 1  # invariant_2 kept its text
    assert facts["selected_plan"]["delta"]["goal_changed"] is True
    assert facts["selected_plan"]["delta"]["renumbered"] == [{"from": "claim_2", "to": "claim_1"}]


def test_duplicate_texts_and_reused_ids_claim_each_selected_occurrence_once():
    from ouroboros.plan_review_facts import _element_fates

    # [A, A] -> [A]: one survives, the other is removed (never "both renumbered into one").
    assert _element_fates({"claim_1": "A", "claim_2": "A"}, {"claim_1": "A"}) == {"claim_1": "same", "claim_2": "removed"}
    # [A, B] -> [B, C]: A removed, B renumbered, C added under its own key although claim_2 is reused.
    assert _element_fates({"claim_1": "A", "claim_2": "B"}, {"claim_1": "B", "claim_2": "C"}) == {
        "claim_1": "removed", "claim_2": "renumbered→claim_1", "selected:claim_2": "added"}
    # An empty reviewed text never borrows its replacement's text.
    assert _element_fates({"claim_1": ""}, {"claim_1": "X"}) == {"claim_1": "changed"}


def test_a_finding_on_an_id_the_reviewed_plan_lacks_is_named_as_such():
    wave = _wave(findings=[{"finding_id": "s1:f9", "id": "f9", "slot": "s1", "model": "m", "class": "note",
                            "breaks": "claim_99", "locator": "", "summary": "points nowhere"}], dispositions=[])

    facts = facts_from_state({"waves": [wave]}, critic=wave, author_plan=_author_plan())

    [row] = [r for r in facts["elements"] if r["id"] == "claim_99"]
    assert row["changed_in_selected_plan"] == "not an element of the reviewed plan" and row["text"] == ""


def test_the_bound_holds_when_only_the_selected_plan_delta_is_left_to_shrink():
    """201 changed decisions: once every element is gone, the delta's id lists are the last
    variable-length field; they collapse to counts and the section (metadata included) fits."""
    decisions = [{"id": f"decision_{i}", "choice": f"Choice {i} " + "y" * 40} for i in range(1, 202)]
    changed = [{"id": f"decision_{i}", "choice": f"Choice {i} changed " + "z" * 40} for i in range(1, 202)]
    wave = _wave(spec={"goal": "g", "acceptance_claims": [], "decisions": decisions, "deferred": []}, findings=[], dispositions=[])
    author = {**_author_plan(), "spec": {"goal": "g", "acceptance_claims": [], "decisions": changed, "deferred": []}}

    facts = facts_from_state({"waves": [wave]}, critic=wave, author_plan=author)

    assert len(render_plan_review_section(facts)) <= PLAN_REVIEW_REFLECTION_CHARS
    assert facts["omitted"]["elements"] == 201
    assert facts["omitted"]["delta_id_lists_collapsed_to_counts"] is True
    assert facts["selected_plan"]["delta"]["changed"] == 201


def test_two_answers_to_one_finding_in_one_call_are_reported_as_the_open_contradiction_they_are():
    wave = _wave(dispositions=[{"finding_id": "s1:f1", "decision": "accept", "rationale": "yes"},
                               {"finding_id": "s1:f1", "decision": "reject", "rationale": "no"}])

    facts = facts_from_state({"waves": [wave]}, critic=wave)

    [finding] = [f for e in facts["elements"] for f in e["findings"] if f["finding_id"] == "s1:f1"]
    assert finding["disposition"].startswith("contradictory answers in one call (2: accept, reject)")
    assert finding["disposition"].endswith("the finding stays open")


def test_an_unreadable_author_plan_is_named_not_rendered_as_no_author_plan():
    facts = facts_from_state({"waves": [_wave()]}, critic=_wave(), author_plan_unavailable="PLAN_AUTHOR_SOURCE_UNAVAILABLE: gone")
    assert facts["selected_plan"] == {"unavailable": "PLAN_AUTHOR_SOURCE_UNAVAILABLE: gone"}
    assert {row["changed_in_selected_plan"] for row in facts["elements"]} == {"n/a"}


def test_no_recorded_wave_means_no_slice():
    assert facts_from_state({"waves": []}, critic=None) is None


@pytest.mark.parametrize("klass", ["note", "need_evidence"])
def test_the_slice_is_bounded_with_every_cut_named(klass):
    """Note summaries, elements AND author questions all bend to the one bound (the
    heading included); each cut is counted."""
    claims = [{"id": f"claim_{i}", "claim": f"Claim number {i} " + "x" * 120} for i in range(1, 41)]
    findings = [{"finding_id": f"s{j}:f{i}", "id": f"f{i}", "slot": f"s{j}", "model": "m", "class": klass,
                 "breaks": f"claim_{i}", "locator": "", "summary": "question or note text " * 8}
                for i in range(1, 41) for j in range(1, 5)]
    wave = _wave(spec={"goal": "g", "acceptance_claims": claims, "decisions": [], "deferred": []},
                 findings=findings, dispositions=[])

    facts = facts_from_state({"waves": [wave]}, critic=wave)

    assert len(render_plan_review_section(facts)) <= PLAN_REVIEW_REFLECTION_CHARS
    assert sum(facts["omitted"][key] for key in ("note_findings_summaries", "elements", "questions")) > 0
    if klass == "need_evidence":
        assert facts["omitted"]["questions"] > 0
    assert facts["omitted"]["note"].startswith("whole rows omitted")
    assert facts["source_ref"] == {}


def _patch_sources(monkeypatch, *, state, author, authority=None):
    from ouroboros.tools import plan_review_artifacts as artifacts

    monkeypatch.setattr("ouroboros.task_results.load_plan_review_state", lambda root, tid: state)
    monkeypatch.setattr(artifacts, "current_author_plan", lambda root, tid, st: author)
    monkeypatch.setattr(artifacts, "authority_wave", authority or (lambda root, tid, hot: hot))
    monkeypatch.setattr("ouroboros.review_evidence_sections._accept_effective_claims",
                        lambda ctx, contract, root, tid: ([], "author_plan", {}))


def test_the_loader_reads_the_reviewed_wave_the_author_answered(tmp_path, monkeypatch):
    wave = _wave()
    _patch_sources(monkeypatch, state={"waves": [wave], "current_attempt": {"fingerprint": FP}}, author=_author_plan())

    facts = plan_review_reflection_slice(tmp_path, "task-1", task={"task_contract": {}})

    assert facts["source_ref"] == {"kind": "task_result", "reader": "get_task_result", "task_id": "task-1",
                                   "field": "plan_review_state"}
    assert {row["id"] for row in facts["elements"]} == {"claim_1", "claim_2", "decision_1"}
    assert facts["claims_source"] == "author_plan"


def test_an_unreadable_recorded_source_is_disclosed_never_silent(tmp_path, monkeypatch):
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable

    def gone(root, tid, hot):
        raise PlanReviewSourceUnavailable("PLAN_REVIEW_SOURCE_UNAVAILABLE: artifact gone")

    _patch_sources(monkeypatch, state={"waves": [_wave()]}, author=None, authority=gone)

    facts = plan_review_reflection_slice(tmp_path, "task-2")

    assert facts["unavailable"].endswith("artifact gone") and facts["source_ref"]["task_id"] == "task-2"


def test_an_unreadable_author_plan_reaches_the_slice_as_a_named_fact(tmp_path, monkeypatch):
    from ouroboros.tools import plan_review_artifacts as artifacts
    from ouroboros.tools.plan_review_artifacts import PlanReviewSourceUnavailable

    _patch_sources(monkeypatch, state={"waves": [_wave()]}, author=None)
    monkeypatch.setattr(artifacts, "current_author_plan",
                        lambda root, tid, st: (_ for _ in ()).throw(PlanReviewSourceUnavailable("PLAN_AUTHOR_SOURCE_UNAVAILABLE: gone")))

    facts = plan_review_reflection_slice(tmp_path, "task-2b")

    assert facts["selected_plan"] == {"unavailable": "PLAN_AUTHOR_SOURCE_UNAVAILABLE: gone"}
    assert facts["elements"], "the reviewed wave's facts still come through"


def test_the_wave_the_author_answered_is_never_substituted_when_it_is_missing(tmp_path, monkeypatch):
    other = _wave(request_fingerprint="d" * 64, cycle_index=2,
                  findings=[{"finding_id": "s:only2", "id": "only2", "slot": "s", "model": "m", "class": "note",
                             "breaks": "claim_1", "locator": "", "summary": "ONLY_WAVE_2"}], dispositions=[])
    author = {**_author_plan(), "review_fingerprint": "e" * 64}
    _patch_sources(monkeypatch, state={"waves": [_wave(), other], "current_attempt": {"fingerprint": "c" * 64}}, author=author)

    facts = plan_review_reflection_slice(tmp_path, "task-2c")

    assert "unavailable" in facts and "not in the index" in facts["unavailable"]
    assert "ONLY_WAVE_2" not in json.dumps(facts)


def test_a_task_without_waves_or_without_an_id_has_no_slice(tmp_path, monkeypatch):
    _patch_sources(monkeypatch, state={"waves": []}, author=None)
    assert plan_review_reflection_slice(tmp_path, "task-3") is None
    assert plan_review_reflection_slice(tmp_path, "") is None


def test_the_measured_section_leads_with_its_heading_and_carries_the_facts():
    rendered = render_plan_review_section({"claims_source": "author_plan", "elements": []})
    assert rendered.startswith("TASK PLAN REVIEW (host-recorded facts; which advice mattered is yours to judge):")
    assert "author_plan" in rendered


def _task_result(tmp_path, task_id, **fields):
    from ouroboros.task_results import write_task_result

    write_task_result(tmp_path, task_id, "completed", text="Ship the deck", result="done", **fields)
    return task_id


def test_the_slice_passes_the_task_contract_to_the_claims_source(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result

    seen = {}

    def claims(ctx, contract, root, tid):
        seen["contract"] = contract
        return [], "ingress_contract", {}

    _patch_sources(monkeypatch, state={"waves": [_wave()]}, author=None)
    monkeypatch.setattr("ouroboros.review_evidence_sections._accept_effective_claims", claims)
    task_id = _task_result(tmp_path, "t-claims", task_contract={"acceptance_claims": [{"id": "claim_1", "claim": "The deck has ten slides"}]})

    facts = plan_review_reflection_slice(tmp_path, task_id, task=load_task_result(tmp_path, task_id))

    assert seen["contract"]["acceptance_claims"][0]["id"] == "claim_1"
    assert facts["claims_source"] == "ingress_contract"
    assert {row["id"] for row in facts["elements"]} == {"claim_1", "claim_2", "decision_1"}


def _organ_participant(seat, *, reply, owner_marked=True):
    return {"seat_id": seat, "model": "m/x", "route": "api_chat", "owner_marked": owner_marked, "dispatched": True,
            "status": "answered", "reply": {"prose": "", "objections": [], "withdrawn": [], **reply}}


def test_an_organ_task_slice_names_each_objection_with_its_standing(tmp_path):
    """An organ task has no legacy elements: its slice names every objection of the plan
    record and whether its raiser withdrew it; a plan nobody objected to says so."""
    from ouroboros import plan_record as pr

    task_id = _task_result(tmp_path, "organ-1")
    pr.append_revision(tmp_path, task_id, pr.compose_revision(
        None, goal="ship", plan="outline, then draft", done_when=["five slides"], affected_paths=[]), body=False)
    pr.append_ask(tmp_path, task_id, {"ask_id": "a1", "ts": "t1", "revision": 1, "message": "look", "participants": [
        _organ_participant("triad_a", reply={"objections": [{"id": "o1", "text": "no export"}, {"id": "o2", "text": "too long"}]}),
        _organ_participant("critic_x", owner_marked=False, reply={"objections": [{"id": "o3", "text": "dated"}]})]})
    pr.append_ask(tmp_path, task_id, {"ask_id": "a2", "ts": "t2", "revision": 1, "message": "export added", "participants": [
        _organ_participant("triad_a", reply={"withdrawn": [{"id": "o1", "why": "added"}]})]})

    # Scope R1, D1: the slice carries exactly the counts and the fold — and no unrendered projection.
    facts = plan_review_reflection_slice(tmp_path, task_id, task={"task_contract": {}})
    assert set(facts) == {"organ", "source_ref", "claims_source", "revisions", "asks", "objections"}
    assert facts["revisions"] == 1 and facts["asks"] == 2 and [o["id"] for o in facts["objections"]] == ["o1", "o2", "o3"]
    assert facts["source_ref"]["field"] == "plan_record"
    standing = {(o["seat_id"], o["id"]): o for o in facts["objections"]}
    assert standing[("triad_a", "o1")]["open"] is False  # withdrawn by its raiser
    assert standing[("triad_a", "o2")]["open"] is True and standing[("triad_a", "o2")]["holds"] is True
    assert standing[("triad_a", "o2")]["not_adjudicated"] is True  # open, owner-marked, not adjudicated
    assert standing[("critic_x", "o3")]["open"] is True  # an added critic's objection is open...
    assert standing[("critic_x", "o3")]["holds"] is False  # ...and never owner-marked
    assert standing[("critic_x", "o3")]["not_adjudicated"] is False

    quiet = _task_result(tmp_path, "organ-2")
    pr.append_revision(tmp_path, quiet, pr.compose_revision(
        None, goal="ship", plan="outline", done_when=["five slides"], affected_paths=[]), body=False)
    pr.append_ask(tmp_path, quiet, {"ask_id": "a1", "ts": "t1", "revision": 1, "message": "look",
                                    "participants": [_organ_participant("triad_a", reply={"prose": "fine"})]})
    calm = plan_review_reflection_slice(tmp_path, quiet, task={"task_contract": {}})
    assert calm["asks"] == 1 and calm["objections"] == []  # none raised
