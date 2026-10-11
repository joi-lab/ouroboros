"""Presence keeps its real admission writer and terminal consumers."""

from types import SimpleNamespace

import pytest

from ouroboros.loop_delivery import _swarm_handoff_attempt
from ouroboros.presence_runner import build_presence_result_event
from ouroboros.tools.control_routing import _finish_swarm_handoff


@pytest.mark.parametrize("status", ["scheduled", "unconfirmed", "rejected"])
def test_presence_handoff_writer_feeds_terminal_consumer_and_preserves_first_receipt(status):
    ctx = SimpleNamespace(
        task_metadata={"presence": {"binding_id": "presence-binding"}},
        _presence_completion={"outcome": "deferred"},
        _presence_completion_accepted=True,
    )
    event = {"task_id": "managed-presence-work", "routing_token": "first-token"}
    response = "The actual admission response."
    assert _finish_swarm_handoff(
        ctx, event, response, status=status, reason="exact-admission-reason",
    ) == response
    first = ctx._swarm_handoff_attempt
    assert first == {
        **event, "status": status, "reason": "exact-admission-reason", "response": response,
    }
    assert _swarm_handoff_attempt(ctx) == first
    # The co-owner guard keeps the existing receipt object and every field.
    assert _finish_swarm_handoff(
        ctx, {"task_id": "other-work", "routing_token": "other-token"},
        "Later response", status="scheduled",
    ) == "Later response"
    assert ctx._swarm_handoff_attempt is first
    assert first["task_id"] == "managed-presence-work" and first["status"] == status
    task = {"id": "presence-turn", "metadata": dict(ctx.task_metadata)}
    terminal = build_presence_result_event(task, "Work continues.", ctx, terminal_origin="model_final")
    assert terminal["work_ref"] == (event["task_id"] if status == "scheduled" else "")
    assert terminal["outcome"] == ("deferred" if status == "scheduled" else "message")


@pytest.mark.parametrize("metadata", [{}, {"force_plan": True, "force_plan_source": "swarm"}])
def test_ordinary_and_managed_swarm_work_never_acquire_a_presence_latch(metadata):
    ctx = SimpleNamespace(task_metadata=metadata)
    for task_id in ("first-work", "second-work"):
        assert _finish_swarm_handoff(
            ctx, {"task_id": task_id}, task_id, status="scheduled",
        ) == task_id
        assert not hasattr(ctx, "_swarm_handoff_attempt")
