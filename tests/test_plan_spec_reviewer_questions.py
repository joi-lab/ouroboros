"""``need_evidence`` as a question to the AUTHOR (owner batch 2, Q4=A): valid with a locator
the host attaches OR with a spec id in ``breaks``; Ouroboros answers, escalates or defers it
openly through the $0 disposition. New ``plan_spec`` cases live here because
``tests/test_plan_spec.py`` sits near its size band."""

from __future__ import annotations

from ouroboros.tools import plan_spec
from ouroboros.tools.plan_spec import MAX_ITEM_CHARS

IDS = frozenset({"goal", "claim_1", "invariant_1"})


def _validate(raw, seen=()):
    return plan_spec.validate_findings(raw, spec_ids=IDS, seen_locators=set(seen), slot="1")


def test_need_evidence_with_a_spec_id_is_a_question_to_the_author():
    normalized, disclosures, seen = _validate([
        {"id": "q1", "class": "need_evidence", "breaks": "claim_1", "summary": "why sqlite over postgres?"},
        {"id": "q2", "class": "need_evidence", "breaks": "claim_9", "summary": "invalid id, no locator"},
        {"id": "q3", "class": "need_evidence", "breaks": "goal", "locator": "x" * (MAX_ITEM_CHARS + 1), "summary": "long"},
        {"id": "q4", "class": "need_evidence", "locator": "y" * (MAX_ITEM_CHARS + 1), "summary": "long, no id"},
        {"id": "q5", "class": "need_evidence", "breaks": "claim_1", "locator": "docs/a.md", "summary": "both"},
    ])
    by_id = {f["id"]: f for f in normalized}
    assert by_id["q1"]["class"] == "need_evidence" and by_id["q1"]["locator"] == "" and by_id["q1"]["breaks"] == "claim_1"
    assert by_id["q2"]["class"] == "note"  # neither a locator nor a valid spec id: advice, disclosed as before
    assert by_id["q3"]["class"] == "need_evidence" and by_id["q3"]["locator"] == ""  # the question stands, only the locator goes
    assert by_id["q4"]["class"] == "note"
    assert by_id["q5"]["class"] == "need_evidence" and by_id["q5"]["locator"] == "docs/a.md"
    assert disclosures == ["need_evidence_without_locator:q2", "need_evidence_locator_too_long:q3",
                           "need_evidence_locator_too_long:q4"]
    assert seen == frozenset({"docs/a.md"})  # a question adds nothing to the host's locator memory


def test_a_question_holds_the_wave_until_a_free_disposition_under_both_enforcements():
    question = {"finding_id": "1:q1", "class": "need_evidence", "breaks": "claim_1", "locator": "", "summary": "?"}
    for enforcement in ("blocking", "advisory"):
        opened = plan_spec.closure_after_disposition("REVIEW_REQUIRED", [question], [], enforcement)
        assert opened["closed"] is False and opened["open_ids"] == ["1:q1"]
        for decision in ("accept", "reject", "defer"):  # answered / declined / deferred openly
            closed = plan_spec.closure_after_disposition(
                "REVIEW_REQUIRED", [question],
                [{"finding_id": "1:q1", "decision": decision, "rationale": "the author's word"}], enforcement)
            assert closed["closed"] is True and closed["open_ids"] == []


def test_a_question_only_wave_is_review_required():
    rows = [{"slot": "s1", "model": "m", "ok": True, "findings": [
                {"id": "q1", "class": "need_evidence", "breaks": "claim_1", "locator": "", "summary": "?"}]},
            {"slot": "s2", "model": "m", "ok": True, "findings": []},
            {"slot": "s3", "model": "m", "ok": True, "findings": []}]
    agg = plan_spec.aggregate(rows)
    assert agg["aggregate"] == "REVIEW_REQUIRED" and agg["counts"]["need_evidence"] == 1


def test_the_output_contract_states_both_need_evidence_forms():
    """Owner-forwarded audit, finding 3: the rubric invites a question to the author with
    the spec id in ``breaks`` and no locator, and the validator accepts exactly that, while
    the Output contract inserted into the same prompt still demanded a locator for every
    ``need_evidence``. The contract must state the two forms instead of making the reviewer
    guess between its own halves or invent a file."""
    from ouroboros.tools.plan_packet import build_plan_review_system_prompt

    prompt = build_plan_review_system_prompt(
        checklist_section="", constitutional=False, bible_text=None,
        cycle_index=1, enforcement="blocking")
    assert "no locator needed" in prompt  # the rubric's question-to-the-author form
    assert "REQUIRED for need_evidence" not in prompt
    assert "for a need_evidence DOCUMENT request" in plan_spec.PLAN_FINDINGS_ARRAY_CONTRACT
    assert ("a need_evidence that asks the AUTHOR a question names the spec id in `breaks` "
            "and needs no locator") in plan_spec.PLAN_FINDINGS_ARRAY_CONTRACT
    assert plan_spec.PLAN_FINDINGS_ARRAY_CONTRACT in prompt
    # The validator already accepts the question form; the contract now says so.
    normalized, disclosures, _seen = _validate(
        [{"id": "q1", "class": "need_evidence", "breaks": "goal", "summary": "Which goal matters more?"}])
    assert normalized[0]["class"] == "need_evidence" and not disclosures
