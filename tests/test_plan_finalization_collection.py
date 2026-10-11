"""The production finalization gate collects real custody without another send."""
from __future__ import annotations

import json

import pytest

from ouroboros.artifacts import read_actor_source_bytes
from ouroboros.context_health import build_health_invariants
from ouroboros.owner_hurry import force_plan_decision, plan_review_disclosure, plan_review_reminder
from ouroboros.tools.plan_review_artifacts import read_wave
from ouroboros.utils import append_jsonl
from tests.test_health_invariants_ownership import _env
from tests.test_plan_review_engine import CLEAN, _call, _finding, _state
from tests.test_plan_review_engine import harness as _engine_harness
from tests.test_plan_review_event_route import _HeldExecutor, _mailbox_entries, _wait_until

harness = _engine_harness


class _AnswerExecutor(_HeldExecutor):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def execute(self):
        from ouroboros.review_execution import ReviewAttemptResult

        attempt = super().execute()
        return ReviewAttemptResult(message={"content": self.answer}, usage=attempt.usage,
                                   raw_text=self.answer)


@pytest.fixture
def panel(monkeypatch):
    executors = {f"s{i}": _AnswerExecutor(CLEAN) for i in range(1, 4)}
    monkeypatch.setattr("ouroboros.review_substrate._review_route_executor",
                        lambda assignment, **_kw: executors[assignment.slot.slot_id])
    yield executors
    for executor in executors.values():
        executor.release.set()
    # Custody callbacks, not just the fake transport, must have finished.
    from ouroboros import review_custody

    assert _wait_until(lambda: not review_custody._RELEASED_WAVES)


def _sent(panel):
    return sum(executor.execute_calls for executor in panel.values())


def _settle(panel, ctx):
    for executor in panel.values():
        executor.release.set()
    assert _wait_until(lambda: len(_mailbox_entries(ctx.drive_root, ctx.task_id)) == 1)


@pytest.mark.parametrize("blocking_finding", [False, True])
@pytest.mark.parametrize("separate_execution_root", [False, True])
def test_gate_collects_the_original_packet_after_owner_clarification(
    harness, monkeypatch, panel, blocking_finding, separate_execution_root,
):
    if blocking_finding:
        for executor in panel.values():
            executor.answer = json.dumps([_finding("b1", "blocking", breaks="claim_1")])
    ctx = harness.make_ctx()
    ctx.current_chat_id = 1
    ctx.budget_drive_root = harness.drive
    if separate_execution_root:
        ctx.drive_root = harness.drive / "execution"
        ctx.drive_root.mkdir()
    chat = harness.drive / "logs" / "chat.jsonl"
    append_jsonl(chat, {"direction": "in", "chat_id": 1, "text": "Use the agreed outline."})
    live = ["Read the original room."]
    monkeypatch.setattr("ouroboros.tools.plan_review_runtime.root_exploration_log", lambda _ctx: live[0])
    _call(ctx)
    assert _wait_until(lambda: _sent(panel) == 3)
    first = _state(harness)["waves"][-1]
    source = read_actor_source_bytes(harness.drive, ctx.task_id, first["dialogue_source_ref"])
    sent = read_wave(harness.drive, ctx.task_id, first["wave_artifact"])
    health = build_health_invariants(_env(harness.drive), task_id=ctx.task_id)
    line = next(line for line in health.splitlines() if "PLAN REVIEW WAVE OPEN" in line)
    assert first["request_fingerprint"][:8] in line and first["reviewed_at"] in line
    assert "recorded pending" in line and "plan_task" not in line and "since" not in line

    append_jsonl(chat, {"direction": "in", "chat_id": 1, "text": "Keep the alternative too."})
    live[0] += " New owner clarification arrived."
    _settle(panel, ctx)
    # A stale health snapshot remains a recorded fact until the real collector runs.
    assert "PLAN REVIEW WAVE OPEN" in build_health_invariants(_env(harness.drive), task_id=ctx.task_id)
    decision = force_plan_decision(ctx, {}, enforcement="blocking")
    assert decision["allow"] is (not blocking_finding)
    assert decision["outcome"] == ("REVISE_PLAN" if blocking_finding else "GREEN")
    assert not decision.get("custody_pending")
    assert _sent(panel) == 3 and _state(harness)["cycles_paid"] == 1
    settled = _state(harness)["waves"][-1]
    exact = read_wave(harness.drive, ctx.task_id, settled["wave_artifact"])
    assert settled["dialogue_source_ref"] == first["dialogue_source_ref"]
    assert read_actor_source_bytes(harness.drive, ctx.task_id, settled["dialogue_source_ref"]) == source
    assert b"Keep the alternative too" not in source
    assert "Keep the alternative too" in chat.read_text()
    for key in ("request_policy", "slot_prompt_chars", "retry_key", "cycle_index"):
        assert exact[key] == sent[key]
    assert [row["request_messages"] for row in exact["reviewer_outputs"]] == [
        row["request_messages"] for row in sent["reviewer_outputs"]]
    assert "PLAN REVIEW WAVE OPEN" not in build_health_invariants(_env(harness.drive), task_id=ctx.task_id)
    assert force_plan_decision(ctx, {}, enforcement="blocking")["allow"] is (not blocking_finding)
    assert _sent(panel) == 3 and _state(harness)["cycles_paid"] == 1


@pytest.mark.parametrize("mode", ["blocking", "advisory", "hurry"])
def test_two_clean_siblings_do_not_close_a_still_running_panel(harness, panel, mode):
    harness.state["enforcement"] = "advisory" if mode == "advisory" else "blocking"
    ctx = harness.make_ctx()
    _call(ctx)
    assert _wait_until(lambda: _sent(panel) == 3)
    panel["s1"].release.set()
    panel["s2"].release.set()
    assert _wait_until(lambda: sum(" answered — " in line for line in harness.progress) == 2)
    if mode == "hurry":
        ctx._owner_hurry_latch = {"reason": "owner_hurry"}
    enforcement = "advisory" if mode == "advisory" else "blocking"
    decision = force_plan_decision(ctx, {}, enforcement=enforcement)
    assert decision["allow"] is (mode != "blocking") and decision["custody_pending"] is True
    assert not panel["s3"].release.is_set() and _sent(panel) == 3
    assert "running or awaiting collection" in plan_review_reminder(decision)
    assert "no parseable reviewer quorum" not in plan_review_reminder(decision)
    railed = force_plan_decision(ctx, {}, enforcement=enforcement, hard_rail="round_limit")
    assert railed["allow"] is True and railed["status"] == "rail_degraded"
    disclosure = plan_review_disclosure(railed, "round_limit")
    assert "running or awaiting collection" in disclosure
    assert "no parseable reviewer quorum" not in disclosure
    _settle(panel, ctx)
    assert force_plan_decision(ctx, {}, enforcement=enforcement)["status"] == "closed"
    assert _sent(panel) == 3 and _state(harness)["cycles_paid"] == 1


@pytest.mark.parametrize("hurry", [False, True])
@pytest.mark.parametrize("blocking_finding", [False, True])
def test_advisory_and_hurry_collect_the_paid_wave_at_the_gate(harness, panel, hurry, blocking_finding):
    """Owner 10=A accepts local collection latency, never more paid review work."""
    harness.state["enforcement"] = "blocking" if hurry else "advisory"
    if blocking_finding:
        for executor in panel.values():
            executor.answer = json.dumps([_finding("b1", "blocking", breaks="claim_1")])
    ctx = harness.make_ctx()
    _call(ctx)
    assert _wait_until(lambda: _sent(panel) == 3)
    _settle(panel, ctx)
    if hurry:
        ctx._owner_hurry_latch = {"reason": "owner_hurry"}
    decision = force_plan_decision(ctx, {}, enforcement="blocking" if hurry else "advisory")
    assert decision["allow"] is True
    assert decision["outcome"] == ("REVISE_PLAN" if blocking_finding else "GREEN")
    assert not decision.get("custody_pending") and not decision.get("review_late_result_pending")
    assert decision.get("owner_hurry_local_advisory", False) is hurry
    assert not _state(harness)["waves"][-1].get("custody_pending")
    disclosure = plan_review_disclosure(decision)
    if blocking_finding:
        assert "REVISE_PLAN" in disclosure
        assert "running or awaiting collection" not in disclosure
        assert "a late result is still owed" not in disclosure
    else:
        assert decision["status"] == "closed" and not disclosure
    assert force_plan_decision(ctx, {}, enforcement="blocking" if hurry else "advisory") == decision
    assert _sent(panel) == 3 and _state(harness)["cycles_paid"] == 1


def test_ordinary_task_and_unattributed_health_do_not_open_a_panel(harness, panel):
    ctx = harness.make_ctx()
    assert force_plan_decision(ctx, {}, enforcement="blocking")["required"] is False
    assert "PLAN REVIEW WAVE OPEN" not in build_health_invariants(_env(harness.drive))
    assert _sent(panel) == 0
