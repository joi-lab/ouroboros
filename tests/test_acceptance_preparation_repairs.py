"""Availability and explicit retry contracts at their host authority boundary."""

import copy
import json

import pytest

from tests._acceptance_preparation_helpers import _ctx, _expose, _fail, _receipt_retry

pytestmark = pytest.mark.serial


def test_known_unknown_same_known_preserves_failure_and_spent_retry(tmp_path, monkeypatch):
    from ouroboros import acceptance_preparation as prep

    ctx = _ctx(tmp_path)
    observed = {"identity": "known-material", "known": True, "unknown_parts": [],
                "owner_source_sha256": "source"}
    monkeypatch.setattr(prep, "preparation_source_identity", lambda *_: dict(observed))
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    record["retry_keys"] = ["spent-source"]
    record["retry"] = {"key": "spent-source", "consumed": True}
    before = copy.deepcopy(record)
    for known in (False, True, False, True):
        observed.update(identity="known-material" if known else "unknown", known=known,
                        unknown_parts=[] if known else ["repository_source"])
        assert prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx) is record
        assert record["source_known"] is known
        assert prep.preparation_blocked(record)
        for key in ("source_identity", "incident_id", "attempts", "exposed_attempt", "retry", "retry_keys", "history"):
            assert record[key] == before[key]
    observed.update(identity="changed-material")
    changed = prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)
    assert changed is not record and changed["attempts"] == 0
    assert changed["history"][-1]["attempts"] == 1


def test_informed_choice_survives_transient_unknown_but_not_other_material(tmp_path, monkeypatch):
    """A transient read failure is missing evidence, not a material change: the
    author's informed finish/stop over the SAME known incident still holds. Proven
    other material still voids it, exactly as begin_preparation reopens."""
    from ouroboros import acceptance_preparation as prep, loop
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance
    from ouroboros.loop_delivery import DeliveryCandidate
    from ouroboros.loop_messages import owner_source_sha256

    ctx = _ctx(tmp_path)
    tool_ctx = ctx.tools._ctx
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setattr(loop, "get_review_enforcement", lambda: "advisory")
    tool_ctx._delivery_candidate = DeliveryCandidate("saved answer", "saved-hash", 1, 1, "prior-fp", {})
    tool_ctx._delivery_candidate.owner_source_sha256 = owner_source_sha256(tool_ctx)
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, tool_ctx)))
    assert record["source_known"] is True
    merge_agent_acceptance_stance(ctx.llm_trace, {"explicit_finish": True, "author_action": "finish",
        "disposition": "partial", "rationale": "caveat"}, tool_ctx)
    assert prep.preparation_delivery_choice(tool_ctx, ctx.llm_trace)
    original = prep.preparation_source_identity
    observed = {"known": True}

    def flaky(*args, **kwargs):
        source = original(*args, **kwargs)
        if observed["known"]:
            return source
        return {**source, "identity": prep.UNKNOWN_SOURCE_IDENTITY, "known": False,
                "unknown_parts": ["repository_source"]}

    monkeypatch.setattr(prep, "preparation_source_identity", flaky)
    observed["known"] = False
    assert prep.preparation_delivery_choice(tool_ctx, ctx.llm_trace)
    observed["known"] = True
    assert prep.preparation_delivery_choice(tool_ctx, ctx.llm_trace)
    tool_ctx._delivery_effective_criteria = "changed requirements"  # proven other material
    assert not prep.preparation_delivery_choice(tool_ctx, ctx.llm_trace)


@pytest.mark.parametrize("source", ["owner", "receipt"])
def test_retry_source_cannot_be_rebought_by_rationale_basis_or_locator(tmp_path, source):
    from ouroboros import acceptance_preparation as prep
    from ouroboros.outcome_receipt_store import append_verification_receipt

    ctx = _ctx(tmp_path)
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    intent = (_receipt_retry(ctx, record) if source == "receipt" else {
        "incident_id": record["incident_id"], "basis": "owner_retry", "rationale": "the owner asked",
        "owner_source_sha256": record["owner_source_sha256"],
    })
    grant = prep.record_retry_intent(ctx.llm_trace, intent, ctx.tools._ctx)
    assert grant and prep.consume_retry(record)
    _expose(_fail(record))
    for basis in (intent["basis"], "material_change"):
        replay = prep.record_retry_intent(ctx.llm_trace, {**intent, "basis": basis,
                                         "rationale": "a completely different explanation"}, ctx.tools._ctx)
        assert not replay or replay["consumed"]
        assert not prep.consume_retry(record)
    if source == "receipt":
        # Move the original dated receipt to another index by inserting an
        # earlier row. The locator moves, but its exact source identity does not.
        assert append_verification_receipt(ctx.tools._ctx.drive_root, ctx.task_id, {
            "check": "earlier check", "status": "pass", "ts": "2026-01-01T00:00:00Z"})
        replay = prep.record_retry_intent(ctx.llm_trace, {**intent, "verification_receipt_index": 1}, ctx.tools._ctx)
        assert replay["key"] == grant["key"] and replay["consumed"]
        assert not prep.consume_retry(record)
    assert len(record["retry_keys"]) == 1


@pytest.mark.parametrize("source", [{}, {"owner_source_sha256": "invented"},
    {"verification_receipt_index": 999}, {"verification_receipt_index": True},
    {"verification_receipt_index": -1}, {"source": {"kind": "owner_source", "sha256": "invented"}}])
def test_retry_needs_a_host_resolvable_source(tmp_path, monkeypatch, source):
    from ouroboros import acceptance_preparation as prep
    from ouroboros.tools.review import _handle_task_acceptance_review

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    ctx = _ctx(tmp_path)
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    intent = {"incident_id": record["incident_id"], "basis": "material_change", "rationale": "changed", **source}
    assert prep.record_retry_intent(ctx.llm_trace, intent, ctx.tools._ctx) == {}
    response = _handle_task_acceptance_review(ctx.tools._ctx, claim="answer", goal="goal", acceptance_retry=intent)
    assert "TOOL_ARG_ERROR" in response and "retry source" in response
    assert "retry" not in record


@pytest.mark.parametrize("stance", [{"author_action": "stop"}, {"author_action": "finish"},
                                   {"agent_disposition": "partial"}])
def test_retry_plus_terminal_stance_is_explicitly_refused_before_building(tmp_path, monkeypatch, stance):
    from ouroboros import review_evidence
    from ouroboros.tools.review import _handle_task_acceptance_review

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    monkeypatch.setattr(review_evidence, "build_task_acceptance_evidence",
                        lambda *_a, **_k: pytest.fail("a conflicting choice reached the builder"))
    ctx = _ctx(tmp_path)
    response = _handle_task_acceptance_review(ctx.tools._ctx, claim="answer", goal="goal",
        rationale="unfinished", acceptance_retry={"incident_id": "incident", "basis": "owner_retry",
                                                 "rationale": "the owner asked"}, **stance)
    assert "TOOL_ARG_ERROR" in response and "conflicts" in response
    assert not ctx.llm_trace.get("acceptance_preparation")


def test_root_retry_nomination_keeps_source_and_does_not_build(tmp_path, monkeypatch):
    from ouroboros import review_evidence, acceptance_preparation as prep
    from ouroboros.tools.review import _handle_task_acceptance_review

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    monkeypatch.setattr(review_evidence, "build_task_acceptance_evidence",
                        lambda *_a, **_k: pytest.fail("a root retry nomination reached the builder"))
    ctx = _ctx(tmp_path)
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    payload = json.loads(_handle_task_acceptance_review(ctx.tools._ctx, claim="answer", goal="goal",
        acceptance_retry={"incident_id": record["incident_id"], "basis": "owner_retry",
                          "rationale": "the owner asked", "owner_source_sha256": record["owner_source_sha256"]}))
    assert payload["status"] == "deferred_to_host_acceptance"
    assert payload["acceptance_retry"]["owner_source_sha256"] == record["owner_source_sha256"]
    assert payload["acceptance_retry"]["source"] == {
        "kind": "owner_source", "sha256": record["owner_source_sha256"]}
    assert "agent_decision" not in payload


def test_receipt_locator_cannot_rebind_between_tool_return_and_host_consumption(tmp_path):
    from ouroboros import acceptance_preparation as prep
    from ouroboros.outcome_receipt_store import append_verification_receipt

    ctx = _ctx(tmp_path)
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    intent = _receipt_retry(ctx, record)
    intent["source"] = prep.resolve_retry_source(ctx.tools._ctx, intent)
    assert append_verification_receipt(ctx.tools._ctx.drive_root, ctx.task_id, {
        "check": "another check", "status": "pass", "ts": "2026-01-01T00:00:00Z"})
    assert prep.record_retry_intent(ctx.llm_trace, intent, ctx.tools._ctx) == {}
    assert "retry" not in record


def test_declared_receipt_is_not_repair_evidence(tmp_path):
    from ouroboros import acceptance_preparation as prep
    from ouroboros.outcome_receipt_store import append_verification_receipt

    ctx = _ctx(tmp_path)
    record = _expose(_fail(prep.begin_preparation(ctx.llm_trace, ctx.tools._ctx)))
    assert append_verification_receipt(ctx.tools._ctx.drive_root, ctx.task_id, {
        "check": "I repaired it", "status": "declared", "contract_kind": "declared"})
    assert prep.record_retry_intent(ctx.llm_trace, {"incident_id": record["incident_id"],
        "basis": "repair_evidence", "rationale": "new wording", "verification_receipt_index": 0}, ctx.tools._ctx) == {}
