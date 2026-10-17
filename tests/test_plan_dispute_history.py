"""Earlier-schema (v1/v2) plan-review disputes survive cold reads and exact-source loss.

The retired plan-review engine wrote these records; finished tasks (and tasks in flight at
the upgrade) still hold them, and ``plan_review_artifacts.plan_review_dispute_history`` and
the task authority projection read them. The records here are the engine's own, recorded
from its real flows while it existed (``tests/fixtures/legacy_plan_dispute``, planted by
``tests._legacy_plan_shared``), or direct v2 records built in their stored shape.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from ouroboros.artifacts import task_artifact_dir_path
from ouroboros.context import _task_authority_projection
from ouroboros.task_results import load_plan_review_state
from ouroboros.tools import plan_review_artifacts as artifacts
from tests import _legacy_plan_shared as legacy
from tests.test_plan_task_tool import harness as _organ_harness

harness = _organ_harness
RATIONALE = "Reject tables: the owner chose charts for the visual briefing; no new storage service is authorized."
OWNER_CHOICES = "A: charts only; B: tables plus a new service. Owner: A. Authorization is limited to the deck."
RAW_REASON = "The recommendation also avoids duplicate interpretation. " * 120 + "RAW_REASON_END"
# The two author selections recorded over round 2 (``two_rounds_author_selected`` and
# ``two_rounds_two_author_selections``), in order.
STANCES = ["Keep charts, with exact rationale. " * 110 + "FIRST_STANCE_END",
           "Owner now asks for both formats; accept the new requirement. SECOND_STANCE_END"]
# The complete late answer recorded for slot s0 (``closed_wave_late_supplement``).
LATE_FEEDBACK = json.dumps([{"id": "late", "class": "note", "summary": "Complete old feedback " * 3000,
                             "breaks": "", "locator": "", "recommendation": "keep this source"}])
# The deck spec as the retired engine's normalization stored it (goal "Ship"): what the
# direct v2 waves below carried when the engine still built them.
SHIP_SPEC = {
    "goal": "Ship", "in_scope": ["a 5-slide deck on the Q3 roadmap"], "non_goals": ["speaker notes"],
    "acceptance_claims": [
        {"id": "claim_1", "claim": "exactly 5 slides", "surface": "", "support": "", "priority": "must"},
        {"id": "claim_2", "claim": "every slide has a title and one chart", "surface": "", "support": "",
         "priority": "must"},
    ],
    "invariants": ["deliver by Friday", "no confidential numbers"],
    "decisions": [{"id": "decision_1", "choice": "one chart per slide", "rejected": ["tables"], "why": "audience"}],
    "deferred": [{"id": "deferred_1", "what": "color palette", "why_safe_to_defer": "cosmetic"}],
    "affected_paths": [], "affected_resources": ["the Q3 board deck"], "evidence": [], "normalization_omissions": [],
}


def _state(h, task_id="task-1"):
    return load_plan_review_state(h.drive, task_id)


def _history(h, ctx):
    return _task_authority_projection(SimpleNamespace(drive_root=h.drive), {"id": ctx.task_id})["plan_review_authority"]


def _resident_reason(history, alias):
    assert alias["representation"] == "resident_review_decision"
    return next(row["reason"] for row in history["decision_rows"] if row.get("decision_ref") == alias["decision_ref"])


def _first_two_rounds(h):
    """The retired engine's first two closed rounds, as recorded (``two_rounds``): round 1 over
    the owner's choices (``notes.md``), whose s1 note 'tables' carries a long recommendation and
    RAW_REASON and was rejected by the author with RATIONALE; round 2 with s1 note 'captions'.
    Returns the context and the recorded facts: ``first_wave`` (round 1 before the rejection),
    ``raw`` (s1's round-1 answer) and ``recommendation``."""
    meta = legacy.plant_recorded(h.drive, "two_rounds", workspace=h.workspace)
    return h.make_ctx(), meta


def test_three_round_dispute_reaches_cold_author_projection(harness):
    ctx, meta = _first_two_rounds(harness)
    author = _history(harness, ctx)
    dispute = author["dispute_history"]
    assert dispute["status"] == "complete"
    assert any(RATIONALE in _resident_reason(dispute, d["authored_view"])["author_rationales"]
               for r in dispute["rounds"] for d in r.get("dispositions", []))
    assert any(r.get("text") == meta["raw"] for wave in dispute["rounds"] for r in wave["reviewer_outputs"])
    assert "session_task" not in json.dumps(dispute)
    assert "request_messages" not in json.dumps(dispute)
    assert "reviewer_outputs" not in author["current_wave"]
    # The complete dispute a cold author or a new reviewer is given: every recorded
    # argument, whole, beside the decisions it led to.
    for text in (RATIONALE, RAW_REASON, meta["recommendation"], "Check caption spelling"):
        assert text in json.dumps(dispute)
    assert meta["first_wave"]["aggregate"] == "GREEN"
    # A third recorded round lands; a cold projection keeps the first round's own verdict.
    legacy.advance_recorded(harness.drive, "three_rounds", workspace=harness.workspace)
    after = _history(harness, harness.make_ctx())
    assert any(r["request_fingerprint"] == meta["first_wave"]["request_fingerprint"] and r["aggregate"] == "GREEN"
               for r in after["dispute_history"]["rounds"])


def _wave(root, cycle, previous=None, *, fingerprint=None, spec=None, **changes):
    """One full v2 wave over SHIP_SPEC, retained as its exact artifact (task-1)."""
    if previous:
        changes.update(previous_fingerprint=previous["request_fingerprint"], previous_wave_artifact=previous["wave_artifact"])
    return legacy.with_artifact(root, "task-1", legacy.wave(cycle, fingerprint=fingerprint, spec=spec or SHIP_SPEC, **changes))


def test_evicted_rounds_same_subject_and_superseded_answers_keep_original_verdict(tmp_path):
    first = _wave(tmp_path, 1, fingerprint="a" * 64,
                  findings=[{"finding_id": "s1:f", "class": "blocking", "summary": "original objection"}],
                  aggregate="REVISE_PLAN")
    answered = _wave(tmp_path, 1, fingerprint="a" * 64, supersedes_wave_artifact=first["wave_artifact"],
                     aggregate="GREEN", closed=True,
                     dispositions=[{"finding_id": "s1:f", "decision": "reject", "rationale": RATIONALE}])
    revised = _wave(tmp_path, 1, fingerprint="a" * 64, supersedes_wave_artifact=answered["wave_artifact"],
                    dispositions=[{"finding_id": "s1:f", "decision": "accept", "rationale": "New owner requirement"}])
    last = revised
    for cycle in range(2, 14):
        last = _wave(tmp_path, cycle, last)
    state = {"waves": [artifacts.compact_wave(last)], "waves_omitted": 12}
    original = copy.deepcopy(state)
    history = artifacts.plan_review_dispute_history(tmp_path, "task-1", state)
    assert history["status"] == "complete" and state == original
    assert len(history["rounds"]) == 15
    assert history["rounds"][0]["aggregate"] == "REVISE_PLAN"
    assert history["rounds"][1]["dispositions"][0]["rationale"] == RATIONALE
    assert history["rounds"][2]["dispositions"][0]["rationale"] == "New owner requirement"
    assert all(r["source"]["source_ref"]["sha256"] for r in history["rounds"])


def test_disposition_revisions_inline_each_distinct_material_once(tmp_path):
    from ouroboros.artifacts import read_actor_source_bytes

    spec = {**SHIP_SPEC, "invariants": ["SPEC_MATERIAL " * 800]}
    original = _wave(tmp_path, 1, spec=spec,
                     findings=[{"finding_id": "s1:f", "summary": "One recommendation", "class": "note"}],
                     reviewer_outputs=[{"slot_id": "s1", "text": RAW_REASON,
                                        "request_messages": [{"content": "RECURSIVE_PACKET " * 1000}],
                                        "session_task": "RECURSIVE_SESSION " * 1000}])
    latest = original
    for turn in range(15):
        exact = artifacts.read_wave(tmp_path, "task-1", latest["wave_artifact"])
        exact.update(supersedes_wave_artifact=latest["wave_artifact"],
                     dispositions=[{"finding_id": "s1:f", "decision": "reject", "rationale": f"Distinct reason {turn}"}])
        latest = {**exact, "wave_artifact": legacy.persist_wave(tmp_path, "task-1", exact)}
    history = artifacts.plan_review_dispute_history(tmp_path, "task-1", {"waves": [artifacts.compact_wave(latest)]})
    text = json.dumps(history)
    assert history["status"] == "complete"
    assert text.count("RAW_REASON_END") == 1
    assert text.count("SPEC_MATERIAL") == 800
    assert "RECURSIVE_PACKET" not in text and "RECURSIVE_SESSION" not in text
    assert len(history["decision_rows"]) == 16
    for turn in range(15):
        assert f"Distinct reason {turn}" in text
    # The aliases name verified existing bytes, rather than another hidden copy.
    alias = history["rounds"][-1]["reviewer_outputs"][0]["text_source"]["same_content_as"]
    assert json.loads(read_actor_source_bytes(tmp_path, "task-1", alias["source_ref"]))["reviewer_outputs"][0]["text"] == RAW_REASON
    assert alias["field"] == "reviewer_outputs[0].text"


def test_selected_author_subject_keeps_its_source_and_no_invented_verdict(harness):
    # two_rounds, then the author's recorded selection over round 2 (its critic).
    meta = legacy.plant_recorded(harness.drive, "two_rounds_author_selected", workspace=harness.workspace)
    sub = harness.install({})
    critic = meta["critic"]
    author = _history(harness, harness.make_ctx())
    selected = author["dispute_history"]["current_author_plan"]
    assert author["current_wave"] is None
    assert selected["review_fingerprint"] == critic["request_fingerprint"]
    assert selected["fingerprint"] != critic["request_fingerprint"]
    assert "aggregate" not in selected and "verdict" not in selected
    assert _resident_reason(author["dispute_history"], selected["author_disposition"]["rationale"]) == STANCES[0]
    assert sub.calls == []


def test_successive_author_selections_survive_cold_new_attempt(harness):
    from ouroboros.artifacts import read_actor_source_bytes

    # two_rounds, then the author's two recorded selections over round 2 (STANCES, in order).
    meta = legacy.plant_recorded(harness.drive, "two_rounds_two_author_selections", workspace=harness.workspace)
    critic = meta["critic"]
    before = _state(harness)
    ref = before["author_history_head"]
    exact = json.loads(read_actor_source_bytes(harness.drive, "task-1", ref))
    assert exact["author_disposition"]["rationale"] == STANCES[1]
    assert exact["previous_author_subject"]["sha256"]
    assert exact["review_wave_artifact"] == critic["wave_artifact"]
    # The author graph itself names its critic; recovery does not require a hot wave.
    evicted = artifacts.plan_review_dispute_history(harness.drive, "task-1", {**before, "waves": [], "waves_omitted": 2})
    assert evicted["status"] == "complete", evicted["gaps"]
    assert critic["request_fingerprint"] in {wave["request_fingerprint"] for wave in evicted["rounds"]}
    # The recorded next attempt opens; a cold reader still finds both selections.
    legacy.advance_recorded(harness.drive, "two_rounds_author_selections_next_attempt", workspace=harness.workspace)
    history = _history(harness, harness.make_ctx())["dispute_history"]
    assert history["status"] == "complete", history["gaps"]
    assert [_resident_reason(history, p["author_disposition"]["rationale"]) for p in history["author_selections"]] == STANCES
    assert all("verdict" not in p and "aggregate" not in p for p in history["author_selections"])


def test_disposition_rationale_is_retained_exact_before_hot_projection(harness):
    # two_rounds, the author's rejection of s1:captions with RATIONALE below, then a new attempt.
    meta = legacy.plant_recorded(harness.drive, "two_rounds_captions_rejected", workspace=harness.workspace)
    rationale = "  Exact whitespace\n\n" + "Original argument, alternatives and evidence. " * 350 + "RATIONALE_TAIL\n"
    history = _history(harness, harness.make_ctx())["dispute_history"]
    assert any(rationale in _resident_reason(history, d["authored_view"])["author_rationales"]
               for wave in history["rounds"] for d in wave.get("dispositions", []))
    assert any(rationale in row["reason"].get("author_rationales", [])
               for row in history["decision_rows"] if isinstance(row["reason"], dict))
    wave = next(w for w in _state(harness)["waves"] if w["request_fingerprint"] == meta["rejected_wave"]["request_fingerprint"])
    exact = artifacts.read_wave(harness.drive, "task-1", wave["wave_artifact"])
    assert exact["dispositions"][0]["rationale"] == rationale


@pytest.mark.parametrize("missing_source", [False, True])
def test_reachable_legacy_author_stance_reads_before_and_after_the_next_attempt(tmp_path, missing_source):
    # An earlier-schema author selection held only in the current attempt (no author history).
    ref = legacy.plant_recorded(tmp_path, "legacy_author_stance")["ref"]
    if missing_source:
        (task_artifact_dir_path(tmp_path, "task-1", create=False) / ref["path"]).unlink()
        before = artifacts.plan_review_dispute_history(tmp_path, "task-1", load_plan_review_state(tmp_path, "task-1"))
        assert _resident_reason(before, before["author_selections"][0]["author_disposition"]["rationale"]) == "Genuine older stance"
    # The recorded next attempt retained that selection in author history.
    legacy.advance_recorded(tmp_path, "legacy_author_stance_retained" + ("_source_lost" if missing_source else ""))
    history = artifacts.plan_review_dispute_history(tmp_path, "task-1", load_plan_review_state(tmp_path, "task-1"))
    [selection] = history["author_selections"]
    assert selection["selected_source_ref"] == ref
    assert _resident_reason(history, selection["author_disposition"]["rationale"]) == "Genuine older stance"
    assert history["current_author_plan"] is None
    assert history["status"] == "source_unavailable"
    assert any("legacy author selection" in gap["reason"] for gap in history["gaps"])


def test_lost_selected_source_keeps_known_stance_and_exact_critic(tmp_path):
    # One open critic wave and an author selection against it, as recorded with the returned state.
    meta = legacy.plant_recorded(tmp_path, "author_selection_recorded")
    selected, author, critic = meta["state"], meta["author"], meta["critic"]
    lost = selected["author_history_head"]
    (task_artifact_dir_path(tmp_path, "task-1", create=False) / lost["path"]).unlink()
    # The recorded next attempt, opened after that loss.
    second = legacy.advance_recorded(tmp_path, "author_selection_head_lost_new_attempt")["state"]
    for state in (selected, second):
        history = artifacts.plan_review_dispute_history(tmp_path, "task-1", state)
        assert history["status"] == "source_unavailable"
        selected_author = history["author_selections"][0]["author_disposition"]
        assert {**selected_author, "rationale": _resident_reason(history, selected_author["rationale"])} == author
        assert history["rounds"][0]["request_fingerprint"] == critic["request_fingerprint"]
        assert "spec" not in history["author_selections"][0]  # no invented replacement plan


def test_late_feedback_stays_historical_and_missing_source_is_a_gap(tmp_path):
    # A closed wave whose slot s0 answered late, attached as a historical supplement.
    meta = legacy.plant_recorded(tmp_path, "closed_wave_late_supplement")
    task = "history-task"
    stored = load_plan_review_state(tmp_path, task)
    history = artifacts.plan_review_dispute_history(tmp_path, task, stored)
    feedback = history["rounds"][-1]["historical_feedback"][0]
    assert feedback["result"]["text"] == LATE_FEEDBACK
    assert feedback["result"]["operation_id"] == meta["operation_id"]
    assert history["rounds"][-1]["aggregate"] == meta["original"]["aggregate"]
    source = feedback["source"]["source_ref"]
    (task_artifact_dir_path(tmp_path, task) / source["path"]).unlink()
    gap = artifacts.plan_review_dispute_history(tmp_path, task, stored)
    assert gap["status"] == "source_unavailable"
    assert gap["rounds"][-1]["aggregate"] == meta["original"]["aggregate"]
    assert gap["gaps"][0]["code"] == "PLAN_REVIEW_SOURCE_UNAVAILABLE"
    assert load_plan_review_state(tmp_path, task) == stored


def test_missing_secondary_source_does_not_erase_the_verified_wave(tmp_path):
    from ouroboros.artifacts import store_actor_source_bytes

    ref = store_actor_source_bytes(tmp_path, "task-1", category="context_checkpoints", source_id="room",
                                   data=b"room source", extension="jsonl")
    wave = _wave(tmp_path, 1, dialogue_source_ref=ref,
                 dispositions=[{"finding_id": "s1:f", "decision": "reject", "rationale": RATIONALE}])
    (task_artifact_dir_path(tmp_path, "task-1") / ref["path"]).unlink()
    history = artifacts.plan_review_dispute_history(tmp_path, "task-1", {"waves": [wave]})
    assert history["status"] == "source_unavailable"
    assert history["rounds"][0]["dispositions"][0]["rationale"] == RATIONALE


def test_missing_predecessor_is_a_typed_gap_beside_available_decisions(harness):
    ctx, meta = _first_two_rounds(harness)
    # Damage only a fixture's superseded original; the answered wave still has
    # its rejection and the second wave remains readable.
    path = task_artifact_dir_path(harness.drive, ctx.task_id) / meta["first_wave"]["wave_artifact"]["path"]
    path.unlink()
    sub = harness.install({})
    before = _state(harness)
    history = _history(harness, ctx)["dispute_history"]
    assert history["status"] == "source_unavailable"
    assert any(g["code"] == "PLAN_REVIEW_SOURCE_UNAVAILABLE" for g in history["gaps"])
    assert RATIONALE in json.dumps(history)
    assert _state(harness) == before and sub.calls == []


def test_pending_projection_does_not_collect_or_dispatch(harness):
    # One wave recorded custody-pending: its s1 review was still in flight.
    legacy.plant_recorded(harness.drive, "pending_wave", workspace=harness.workspace)
    sub = harness.install({})
    ctx = harness.make_ctx()
    before = _state(harness)
    snapshots = [_history(harness, ctx) for _ in range(2)]
    assert snapshots[0] == snapshots[1]
    assert snapshots[0]["current_wave"]["custody_pending"]
    assert any(not row.get("ok") for wave in snapshots[0]["dispute_history"]["rounds"] for row in wave["reviewers"])
    assert _state(harness) == before and sub.calls == []


def test_unresolvable_index_and_lineage_loop_are_gaps(tmp_path):
    compact = artifacts.compact_wave({"cycle_index": 1, "request_fingerprint": "a" * 64})
    gap = artifacts.plan_review_dispute_history(tmp_path, "task-1", {"waves": [compact]})
    assert gap["status"] == "source_unavailable" and gap["gaps"]
    hot = {"cycle_index": 1, "request_fingerprint": "b" * 64, "previous_fingerprint": "b" * 64, "spec": copy.deepcopy(SHIP_SPEC)}
    loop = artifacts.plan_review_dispute_history(tmp_path, "task-1", {"waves": [hot]})
    assert loop["status"] == "source_unavailable" and len(loop["rounds"]) == 1
    assert "cycle" in loop["gaps"][0]["reason"]
