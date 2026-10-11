"""A filled optional argument that asks for nothing is not a refusal.

Models may fill every key of a tool schema. These tests pin, through the REAL registry,
that such a value takes the omitted path with one disclosure line, and that a
genuine argument mistake is refused once, typed, naming the value it received.
"""
import queue

import pytest

from ouroboros.owner_wait import direct_owner_wait
from ouroboros.task_results import load_task_result
from ouroboros.tools.arg_feedback import argument_refusal, ignored_argument_note
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _registry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path, task_id="root-task",
                      is_direct_chat=True, current_chat_id=1, event_queue=queue.Queue())
    ctx.owner_wait_callback = direct_owner_wait
    registry = ToolRegistry(repo_dir=repo, drive_root=tmp_path)
    registry.set_context(ctx)
    return registry, ctx


def test_helpers_name_the_field_the_value_and_the_repair(tmp_path):
    assert ignored_argument_note("timezone", "UTC", "run_at carries its own offset") == (
        "timezone='UTC' ignored: run_at carries its own offset")
    _registry_unused, ctx = _registry(tmp_path)
    text = argument_refusal(ctx, "DEMO_INVALID", ["a=1 must be 2.", "b is missing"],
                            effect="Nothing was recorded.")
    assert text == "⚠️ DEMO_INVALID: a=1 must be 2; b is missing. Nothing was recorded."


@pytest.mark.parametrize("reflex", [0, 1])
def test_escalate_ignores_a_wait_bound_on_a_quiz_that_does_not_wait(tmp_path, reflex):
    """The live loop: `max_wait_minutes` 0 or 1 beside `wait_for_answer=false` and a real
    assumption was refused eight times in one day. The quiz is sent; the receipt says
    the bound was ignored; the ignored bound is never persisted with the quiz."""
    registry, ctx = _registry(tmp_path)
    result = registry.execute_result("escalate", {
        "question": "Which theme?", "options": ["Light", "Dark"], "stake": "",
        "assumption": "Light meanwhile", "wait_for_answer": False, "max_wait_minutes": reflex,
    })
    assert result.status == "ok" and result.text.startswith("OK: quiz ")
    assert "max_wait_minutes ignored: it applies only to wait_for_answer=true" in result.text
    quiz_id = ctx.event_queue.get_nowait()["quiz_id"]
    block = load_task_result(tmp_path, "root-task")["owner_quiz"][quiz_id]
    assert "max_wait_minutes" not in block


def test_escalate_refuses_a_bound_that_cannot_hold_and_names_the_repair(tmp_path, monkeypatch):
    """A REQUIRED wait keeps the refusal: `0` and a bound past the task's own ceiling both
    ask for something the wait cannot serve, so each is one typed refusal that names the
    repair. The tolerant path is the OPTIONAL question above, not a silent reinterpretation."""
    monkeypatch.setenv("OUROBOROS_TASK_ABS_CEILING_SEC", "21600")  # 360 minutes
    registry, ctx = _registry(tmp_path)
    for asks_for_nothing in (0, 100000):
        refused = registry.execute_result("escalate", {
            "question": "Continue?", "options": ["Yes", "No"],
            "wait_for_answer": True, "max_wait_minutes": asks_for_nothing})
        assert refused.status == "error" and refused.code == "TOOL_ARG_ERROR"
        assert refused.text.startswith("⚠️ QUIZ_WAIT_BOUND_INVALID")
        assert "omit it for an unbounded wait" in refused.text  # the repair, not just the rule
        assert refused.text.endswith("The quiz was not sent.")
    assert not getattr(ctx, "_owner_wait_max_minutes", None) and ctx.event_queue.empty()


def test_escalate_genuine_argument_mistake_is_one_typed_refusal(tmp_path):
    registry, ctx = _registry(tmp_path)
    result = registry.execute_result("escalate", {
        "question": "Continue?", "options": ["Yes", "No"], "wait_for_answer": True, "max_wait_minutes": -3})
    assert result.status == "error" and result.code == "TOOL_ARG_ERROR"
    assert result.text.startswith("⚠️ QUIZ_WAIT_BOUND_INVALID: max_wait_minutes must be ")
    assert result.text.endswith("The quiz was not sent.") and ctx.event_queue.empty()


def test_a_blocked_link_stays_a_policy_denial_and_a_malformed_one_an_argument_fault(tmp_path):
    registry, _ctx = _registry(tmp_path)
    blocked = registry.execute_result("send_links", {"links": [{"label": "x", "url": "javascript:alert(1)"}]})
    assert blocked.status == "blocked" and blocked.text.startswith("⚠️ SEND_LINKS_URL_BLOCKED")
    assert blocked.text.endswith("No links were sent.")
    malformed = registry.execute_result("send_links", {"links": "nope"})
    assert malformed.status == "error" and malformed.text.startswith("⚠️ SEND_LINKS_")

