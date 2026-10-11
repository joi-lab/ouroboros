"""A sourced transport limit changes delivery, never the review's evidence horizon."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.gateways.claudexor import ClaudexorGateway, DaemonEndpoint, run_failure_error
from ouroboros.review_execution import ReviewRouteKind
from ouroboros.review_session_preparation import render_review_session_prompt
from ouroboros.review_substrate import ReviewSlot
from ouroboros.tools import plan_dialogue, plan_review
from ouroboros.tools.plan_spec import PLAN_FINDINGS_ARRAY_CONTRACT
from ouroboros.utils import append_jsonl
from tests.test_plan_review_engine import CLEAN, _call, _state, harness as _harness

harness = _harness


def _native_limit(limit=12_000, overhead=315):
    return {"scope": "turn_text", "unit": "unicode_scalars", "limit": limit,
            "source": "native.turn/start", "verified_against": "native-cli 1.0",
            "askPromptBudget": {"shape": "ordinary_initial_attempt", "engineOverheadMax": overhead}}


def _gateway(monkeypatch, rows):
    gateway = ClaudexorGateway(DaemonEndpoint(host="127.0.0.1", port=1, token="fixture-token"))
    gateway._engine_version = "test-engine"
    gateway._engine_build_sha = "a" * 40
    monkeypatch.setattr(gateway, "agent_capabilities", lambda: {"harnesses": rows})
    return gateway


def _slot(sid="session", target="cursor=fixture-model"):
    return ReviewSlot(slot_id=sid, model="fixture-model", effort="high", role_hint="plan reviewer",
                      route=ReviewRouteKind.AGENT_SESSION, session_target=target)


def _prompt(slot, task):
    request = SimpleNamespace(surface="plan_review", policy={"output_contract": PLAN_FINDINGS_ARRAY_CONTRACT})
    return render_review_session_prompt(request, slot, task.strip())


def _delivery(harness, text, *, bound=None, asserted=0, monkeypatch):
    from ouroboros import model_slots

    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    chat = harness.drive / "logs" / "chat.jsonl"
    for direction, body in [("in", "Preserve original files."), ("out", text), ("in", "LATEST: choose A.")]:
        append_jsonl(chat, {"direction": direction, "chat_id": 1, "text": body})
    manifest = plan_dialogue.attach_own_dialogue(ctx, harness.drive, {}, "a" * 64, persist=True)
    packet = "OPERATIVE PLAN MUST STAY\n" + plan_dialogue.render_dialogue(manifest)
    monkeypatch.setattr(model_slots, "model_role_option", lambda *_a: asserted)
    slot = _slot()
    delivery = plan_dialogue.dialogue_slot_inputs([slot], system_prompt="g", user_content=packet,
        session_task=packet, manifest=manifest, slot_messages={}, native_mandatory_chars=len(packet),
        session_root=str(harness.workspace), task_id=ctx.task_id,
        session_limits={slot.slot_id: bound} if bound else {})
    return slot, manifest, packet, delivery


def test_catalog_budget_has_unit_scope_framing_and_provenance(monkeypatch):
    declaration = _native_limit()
    rows = [{"id": "cursor", "inputLimits": [declaration]}, {"id": "claude"}]
    original = deepcopy(rows)
    gateway = _gateway(monkeypatch, rows)
    try:
        got = gateway.ask_input_limits()
        assert set(got) == {"cursor"}  # No harness-name or model-name inference.
        assert got["cursor"] == {**declaration, "prompt_budget": 11_685,
                                 "engine_version": "test-engine", "engine_build_sha": "a" * 40}
        assert rows == original
        got["cursor"]["askPromptBudget"]["engineOverheadMax"] = 1
        assert rows == original
    finally:
        gateway.close()


@pytest.mark.parametrize("changed", [
    {"unit": "tokens"}, {"scope": "context_window"}, {"limit": True}, {"limit": 0},
    {"source": ""}, {"verified_against": None}, {"askPromptBudget": None},
    {"askPromptBudget": {"shape": "deep_scan", "engineOverheadMax": 0}},
    {"askPromptBudget": {"shape": "ordinary_initial_attempt", "engineOverheadMax": -1}},
])
def test_unknown_or_malformed_constraint_never_becomes_a_budget(monkeypatch, changed):
    gateway = _gateway(monkeypatch, [{"id": "codex", "inputLimits": [{**_native_limit(), **changed}]}])
    try:
        assert gateway.ask_input_limits() == {}
    finally:
        gateway.close()


def test_slot_probe_reads_once_and_uses_the_same_route_as_dispatch(monkeypatch):
    from ouroboros import claudexor_daemon

    calls = []
    bound = {**_native_limit(), "prompt_budget": 11_685}
    gateway = SimpleNamespace(ask_input_limits=lambda: calls.append("catalog") or {"cursor": bound},
                              close=lambda: calls.append("close"))
    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", lambda: gateway)
    result = plan_dialogue.session_input_limits([_slot("one"), _slot("two"),
        ReviewSlot(slot_id="api", model="any-api-model")])
    assert result == {"one": bound, "two": bound} and calls == ["catalog", "close"]
    monkeypatch.setattr(gateway, "ask_input_limits", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert plan_dialogue.session_input_limits([_slot()]) == {}
    assert calls[-1] == "close"


def test_large_room_fits_actual_session_wrapper_and_keeps_exact_source(harness, monkeypatch):
    bound = {**_native_limit(), "prompt_budget": 11_685, "engine_version": "test-engine"}
    slot, manifest, packet, delivery = _delivery(harness, "Older explanation 🐍 Кириллица\n" * 60_000,
                                               bound=bound, monkeypatch=monkeypatch)
    task = delivery["slot_session_tasks"][slot.slot_id]
    actual_prompt = _prompt(slot, task)
    assert len(actual_prompt) + bound["askPromptBudget"]["engineOverheadMax"] <= bound["limit"]
    assert "LATEST: choose A." in task and "Older explanation" not in task
    assert "OPERATIVE PLAN MUST STAY" in task and "exact omitted prefix" in task
    source = read_actor_source_bytes(harness.drive, "task-1", manifest["own_dialogue"]["source_ref"])
    assert source.decode("utf-8") == manifest["own_dialogue"]["text"]
    assert "Preserve original files." in source.decode("utf-8")
    assert manifest["own_dialogue"]["file"] in task
    facts = delivery["dialogue_delivery"][slot.slot_id]
    assert facts["window"] == "unasserted" and facts["input_limit"]["fits"] is True
    assert facts["input_limit"]["prompt_chars"] == delivery["slot_prompt_chars"][slot.slot_id] == len(actual_prompt)
    assert len(packet) > 1_000_000


def test_small_unicode_room_is_not_fitted_by_bytes_or_utf16(harness, monkeypatch):
    slot, manifest, packet, whole = _delivery(harness, "😀я" * 120, monkeypatch=monkeypatch)
    original_task = whole["slot_session_tasks"][slot.slot_id]
    budget = len(_prompt(slot, original_task))
    assert len(_prompt(slot, original_task).encode("utf-8")) > budget
    bound = {**_native_limit(budget + 315), "prompt_budget": budget}
    fitted = plan_dialogue.dialogue_slot_inputs([slot], system_prompt="g", user_content=packet,
        session_task=packet, manifest=manifest, slot_messages={}, native_mandatory_chars=len(packet),
        session_limits={slot.slot_id: bound})
    assert fitted["slot_session_tasks"][slot.slot_id] == original_task
    assert fitted["dialogue_delivery"][slot.slot_id]["conversation_inline_rows"] == 3
    # Tighten exactly one scalar: a row moves behind the pointer, never a chopped substring.
    bound["prompt_budget"] -= 1
    fitted = plan_dialogue.dialogue_slot_inputs([slot], system_prompt="g", user_content=packet,
        session_task=packet, manifest=manifest, slot_messages={}, native_mandatory_chars=len(packet),
        session_limits={slot.slot_id: bound})
    assert len(_prompt(slot, fitted["slot_session_tasks"][slot.slot_id])) <= budget - 1
    assert fitted["dialogue_delivery"][slot.slot_id]["conversation_inline_rows"] < 3


def test_immutable_core_overflow_is_disclosed_without_truncation(harness, monkeypatch):
    bound = {**_native_limit(100), "prompt_budget": 0}
    slot, _manifest, _packet, delivery = _delivery(harness, "An explanation", bound=bound, monkeypatch=monkeypatch)
    task = delivery["slot_session_tasks"][slot.slot_id]
    assert "OPERATIVE PLAN MUST STAY" in task and "snapshot" in task
    assert delivery["dialogue_delivery"][slot.slot_id]["input_limit"]["fits"] is False
    assert delivery["dialogue_delivery"][slot.slot_id]["conversation_inline_rows"] == 0


@pytest.mark.parametrize("asserted,transport_bound,expected_rows", [(40_000, 500_000, 1), (0, 12_000, 1)])
def test_asserted_and_transport_bounds_are_independent(harness, monkeypatch, asserted, transport_bound, expected_rows):
    bound = {**_native_limit(transport_bound), "prompt_budget": transport_bound - 315}
    slot, _manifest, _packet, delivery = _delivery(harness, "OLD " * 80_000, bound=bound,
        asserted=asserted, monkeypatch=monkeypatch)
    facts = delivery["dialogue_delivery"][slot.slot_id]
    assert facts["conversation_inline_rows"] == expected_rows
    assert facts["window"] == ("asserted" if asserted else "unasserted")
    assert facts["input_limit"]["limit"] == transport_bound


def test_free_replay_keeps_original_fitted_task_and_constraint(harness, monkeypatch):
    import dataclasses
    from ouroboros.tools.plan_review_artifacts import authority_wave

    slot = dataclasses.replace(harness.state["slots"][0], route=ReviewRouteKind.AGENT_SESSION,
                               session_target="cursor=fixture-model")
    harness.state["slots"][0] = slot
    probes = []
    bound = {**_native_limit(), "prompt_budget": 11_685, "engine_version": "first"}
    monkeypatch.setattr(plan_review, "session_input_limits", lambda _slots: probes.append(1) or {slot.slot_id: bound})
    substrate = harness.install({"s1": CLEAN, "s2": CLEAN, "s3": CLEAN})
    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    chat = harness.drive / "logs" / "chat.jsonl"
    append_jsonl(chat, {"direction": "out", "chat_id": 1, "text": "OLD " * 80_000})
    append_jsonl(chat, {"direction": "in", "chat_id": 1, "text": "LATEST: A"})
    _call(ctx)
    first = authority_wave(harness.drive, ctx.task_id, _state(harness)["waves"][-1])
    append_jsonl(chat, {"direction": "in", "chat_id": 1, "text": "Later message"})
    monkeypatch.setattr(plan_review, "session_input_limits", lambda _slots: pytest.fail("replay queried new limits"))
    _call(ctx)
    replay = authority_wave(harness.drive, ctx.task_id, _state(harness)["waves"][-1])
    assert len(substrate.calls) == len(probes) == 1
    assert replay["slot_prompt_chars"] == first["slot_prompt_chars"]
    assert replay["dialogue_delivery"] == first["dialogue_delivery"]
    assert replay["reviewer_outputs"][0]["session_task"] == first["reviewer_outputs"][0]["session_task"]


def test_input_refusal_has_no_borrowed_quota_reset():
    error = run_failure_error("r", "failed", {"code": "input_too_large", "category": "validation",
        "safeMessage": "Input exceeds the native limit", "resetsAt": "2099-01-01T00:00:00Z"})
    assert error.code == "input_too_large" and not getattr(error, "reset_at", "")
    quota = run_failure_error("r", "failed", {"code": "credential_pool_exhausted",
        "safeMessage": "Pool exhausted", "resetsAt": "2099-01-01T00:00:00Z"})
    assert quota.reset_at == "2099-01-01T00:00:00Z"


def test_unresolvable_slot_keeps_its_dispatch_refusal_without_hiding_sibling_health(monkeypatch):
    from tests.test_plan_review_epoch import _patch_snapshot_health, _profile_slots, _SPENT
    from ouroboros.review_execution import ReviewRouteUnavailable, session_route_for_review_slot
    from ouroboros.tools.plan_review_runtime import plan_panel_health_snapshot

    asked = _patch_snapshot_health(monkeypatch, lambda rid, model, pin: _SPENT)
    slots = _profile_slots(("bad", "=gpt-6-astra", ""),
                           ("good", "codex=gpt-6-astra:low", "selected-account"))
    with pytest.raises(ReviewRouteUnavailable, match="unparsable session target"):
        session_route_for_review_slot(slots[0])
    assert plan_panel_health_snapshot(slots) == {
        "good": {"failure_code": "subscription_window_exhausted", "reset_at": _SPENT[1]}}
    assert asked == [("codex", "gpt-6-astra", "selected-account")]
    assert session_route_for_review_slot(slots[1]).effort == "high"
