"""Plan-review facts for the learning surfaces (``ouroboros.plan_review_facts``).

The reflection asks the mind to account for every review finding; these tests pin
that the host now puts the plan review's facts in front of it — bounded, with a
source pointer, never a score — and that a panel settling after the task ended
leaves one bounded row in the reflection log.
"""
from __future__ import annotations

import json

import pytest

from ouroboros import plan_review_facts as facts_mod
from ouroboros.plan_review_facts import (
    LATE_SETTLEMENT_TASK_TYPE,
    PLAN_REVIEW_REFLECTION_CHARS,
    facts_from_state,
    late_settlement_reflection_entry,
    learn_from_late_settlement,
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


def test_the_evidence_formatter_leads_with_the_slice_and_is_byte_identical_without_it():
    from ouroboros.review_evidence import format_review_evidence_for_prompt

    evidence = {"task_id": "t-1", "has_evidence": True, "lens_marker": "lens-survives"}
    plain = format_review_evidence_for_prompt(evidence, max_chars=8000, acceptance_panels=None)
    assert format_review_evidence_for_prompt(evidence, max_chars=8000, acceptance_panels=None, plan_review=None) == plain

    with_facts = format_review_evidence_for_prompt(evidence, max_chars=8000, acceptance_panels=None,
                                                   plan_review={"claims_source": "author_plan", "elements": []})
    assert with_facts.startswith("TASK PLAN REVIEW (host-recorded facts; which advice mattered is yours to judge):")
    assert with_facts.endswith(plain) and "author_plan" in with_facts


@pytest.mark.parametrize("recorded", [True, False])
def test_the_reflection_prompt_carries_the_slice_exactly_when_the_task_recorded_waves(monkeypatch, recorded):
    from ouroboros.reflection import generate_reflection

    captured = {}

    class _Llm:
        def chat(self, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            return {"content": "Reflection completed."}, {}

    slice_ = {"claims_source": "author_plan", "elements": [{"id": "claim_7", "text": "MARKER_CLAIM_SEVEN"}]}
    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice",
                        lambda root, tid, task=None: slice_ if recorded else None)

    generate_reflection(task={"id": "reflection-task", "text": "reflect"},
                        llm_trace={"tool_calls": [], "review_runs": []}, trace_summary="completed",
                        llm_client=_Llm(), usage_dict={"rounds": 2, "cost": 0.1},
                        review_evidence={"has_evidence": True, "lens_marker": "lens-survives"})

    assert ("TASK PLAN REVIEW" in captured["prompt"]) is recorded
    assert ("MARKER_CLAIM_SEVEN" in captured["prompt"]) is recorded
    assert "lens-survives" in captured["prompt"]


def _late_result(tmp_path, task_id="late-1", retry_key="rk-1", **fields):
    from ouroboros.task_results import write_task_result

    write_task_result(tmp_path, task_id, "completed", text="Ship the deck", result="done", **fields, review_projection={
        "panels": [{"panel_id": "panel_1", "late_settlement": {
            "note": "Reviewers later passed it.\nSecond line of the host's sentence.",
            "reviewed_subject": {"retry_key": retry_key, "panel_id": "panel_1"},
            "reviewer_outputs": [{"slot_id": "a", "verdict": "PASS", "model": "model/a"}]}}]})
    return task_id


def test_the_late_settlement_row_is_bounded_sourced_and_closed_to_the_pattern_register(tmp_path, monkeypatch):
    from ouroboros.reflection import _admits_pattern_register

    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice", lambda root, tid, task=None: None)
    task_id = _late_result(tmp_path)

    entry = late_settlement_reflection_entry(tmp_path, task_id, "rk-1")

    assert entry["type"] == entry["task_type"] == LATE_SETTLEMENT_TASK_TYPE
    assert entry["supplement_id"] == "acceptance-late:rk-1" and entry["task_id"] == task_id
    assert entry["reflection"].startswith("Reviewers later passed it.")
    assert entry["reflection"].splitlines()[1] == f"Source: get_task_result(task_id={task_id}), review_projection panel panel_1."
    assert "- a (model/a): PASS" in entry["reflection"]
    assert entry["source_ref"]["panel_id"] == "panel_1" and entry["goal"] == "Ship the deck"
    assert not _admits_pattern_register(entry)
    assert late_settlement_reflection_entry(tmp_path, task_id, "unknown-key") is None


def test_the_late_row_passes_the_result_so_the_claims_source_sees_the_task_contract(tmp_path, monkeypatch):
    seen = {}

    def claims(ctx, contract, root, tid):
        seen["contract"] = contract
        return [], "ingress_contract", {}

    _patch_sources(monkeypatch, state={"waves": [_wave()]}, author=None)
    monkeypatch.setattr("ouroboros.review_evidence_sections._accept_effective_claims", claims)
    task_id = _late_result(tmp_path, task_contract={"acceptance_claims": [{"id": "claim_1", "claim": "The deck has ten slides"}]})

    entry = late_settlement_reflection_entry(tmp_path, task_id, "rk-1")

    assert seen["contract"]["acceptance_claims"][0]["id"] == "claim_1"
    assert "Plan review (ingress_contract claims; 3 of 3 elements)" in entry["reflection"]


def test_a_failing_plan_fact_read_still_leaves_the_late_row_with_its_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice",
                        lambda root, tid, task=None: (_ for _ in ()).throw(ValueError("dialogue source torn")))
    task_id = _late_result(tmp_path)

    entry = late_settlement_reflection_entry(tmp_path, task_id, "rk-1")

    assert entry["reflection"].startswith("Reviewers later passed it.")
    assert "Plan-review facts unavailable: dialogue source torn" in entry["reflection"]


def test_learning_from_a_late_settlement_appends_one_routed_row_and_never_raises(tmp_path, monkeypatch):
    from ouroboros.task_results import load_task_result

    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice", lambda root, tid, task=None: None)
    task_id = _late_result(tmp_path)
    result = load_task_result(tmp_path, task_id)

    assert learn_from_late_settlement(tmp_path, result, "rk-1") is True
    assert learn_from_late_settlement(tmp_path, result, "unknown-key") is False
    rows = [json.loads(line) for line in (tmp_path / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["supplement_id"] for row in rows] == ["acceptance-late:rk-1"]

    monkeypatch.setattr("ouroboros.reflection.append_reflection_routed",
                        lambda env, task, entry: (_ for _ in ()).throw(OSError("disk gone")))
    assert learn_from_late_settlement(tmp_path, result, "rk-1") is False


def test_a_project_bound_late_row_lands_on_the_project_drive_with_a_canonical_pointer(tmp_path, monkeypatch):
    import ouroboros.project_facts as pf
    from ouroboros.task_results import load_task_result

    monkeypatch.setattr(pf, "_project_store_root", lambda pid: tmp_path / "projects" / pid)
    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice", lambda root, tid, task=None: None)
    canonical = tmp_path / "data"
    task_id = _late_result(canonical, task_id="late-proj", project_id="slime", budget_drive_root=str(canonical))
    result = load_task_result(canonical, task_id)

    assert learn_from_late_settlement(canonical, result, "rk-1") is True

    project_log = tmp_path / "projects" / "slime" / "logs" / "task_reflections.jsonl"
    [row] = [json.loads(line) for line in project_log.read_text(encoding="utf-8").splitlines()]
    assert row["type"] == LATE_SETTLEMENT_TASK_TYPE and row["task_id"] == "late-proj"
    [pointer] = [json.loads(line) for line in (canonical / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert pointer["type"] == "project_reflection_pointer" and pointer["project_id"] == "slime"


def test_every_path_that_announces_a_settlement_writes_the_row_and_a_replay_does_not(tmp_path, monkeypatch):
    """Maintenance and recovery announce through enqueue_late_acceptance_settlement directly,
    so the learning hook lives there: a NEW announcement writes one row, an already delivered
    notice writes none."""
    from types import SimpleNamespace

    from ouroboros import acceptance_settlement as settlement
    from ouroboros.task_results import load_task_result
    from supervisor import terminal_delivery

    monkeypatch.setattr(facts_mod, "plan_review_reflection_slice", lambda root, tid, task=None: None)
    monkeypatch.setattr(terminal_delivery, "pending_deliveries", lambda root: [])
    outcomes = iter([terminal_delivery.ENQUEUE_QUEUED, terminal_delivery.ENQUEUE_ALREADY_DELIVERED])
    monkeypatch.setattr(terminal_delivery, "enqueue_terminal_delivery_outcome", lambda root, row, event_queue=None: next(outcomes))
    task_id = _late_result(tmp_path, task_id="late-m")
    result = load_task_result(tmp_path, task_id)
    panel = result["review_projection"]["panels"][0]
    ctx = SimpleNamespace(drive_root=tmp_path, budget_drive_root=str(tmp_path))

    assert settlement.enqueue_late_acceptance_settlement(ctx, task_id, "rk-1", result, panel) == "announced"
    assert settlement.enqueue_late_acceptance_settlement(ctx, task_id, "rk-1", result, panel) == "published"

    rows = [json.loads(line) for line in (tmp_path / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["supplement_id"] for row in rows] == ["acceptance-late:rk-1"]
