"""The real continuation serializer preserves nanny observations, not fresh seeds."""
import json
from types import SimpleNamespace

import pytest

from ouroboros.agent_dispatch import reset_nanny_economics_marks
from ouroboros.nanny_pacing import (
    nanny_burn_fact,
    nanny_metered_since_delegate_activity,
    note_nanny_delegate_activity,
)
from ouroboros.owner_wait import continuation_state, restore_continuation_state


def context():
    ctx = SimpleNamespace(task_id="nanny", task_attempt=1)
    reset_nanny_economics_marks(ctx, route_dispatched=True, delegate_activity_seed=True)
    return ctx


def observe(ctx, round_idx, cost, verb=None):
    usage = {"cost": cost, "cost_final": True}
    ctx._accumulated_usage = usage
    note_nanny_delegate_activity(ctx, round_idx, usage,
                                [{"function": {"name": verb}}] if verb else [])


def snapshot(ctx, round_idx=8):
    return json.loads(json.dumps(continuation_state(
        ctx, [], {}, getattr(ctx, "_accumulated_usage", {}), round_idx, [], set())))


def restore(saved):
    restored = context()  # agent dispatch seeds before the cold loop restores
    usage = {}
    restore_continuation_state(SimpleNamespace(_ctx=restored), saved, [], {}, usage, set())
    restored._accumulated_usage = usage
    return restored


@pytest.mark.parametrize("verb", ["delegate_start", "schedule_subagent"])
def test_cold_restore_keeps_round_and_cost_anchors_separate(verb):
    ctx = context()
    observe(ctx, 3, 1.0, verb)
    observe(ctx, 5, 2.0, "delegate_wait")
    observe(ctx, 8, 5.0)
    before = nanny_burn_fact(ctx)
    saved = snapshot(ctx)
    restored = restore(saved)
    assert nanny_burn_fact(restored) == before
    assert nanny_metered_since_delegate_activity(restored) == (3, 4.0)
    observe(restored, 9, 6.0)
    assert nanny_metered_since_delegate_activity(restored) == (4, 5.0)
    assert "round 5" in nanny_burn_fact(restored) and "round 3" in nanny_burn_fact(restored)


def test_legacy_checkpoint_exposes_missing_anchors_until_new_requests():
    ctx = context()
    observe(ctx, 8, 5.0)
    saved = snapshot(ctx)
    saved.pop("nanny_activity", None)  # an older writer never stored these observations
    restored = restore(saved)
    for actor in (restored, restore(snapshot(restored))):
        assert nanny_metered_since_delegate_activity(actor) == (None, None)
        fact = nanny_burn_fact(actor)
        assert "unknown own rounds" in fact and "historical cost anchor unavailable" in fact
        assert "no provider price recorded" not in fact
        assert "host-started" not in fact and "no delegate_start" not in fact
    observe(restored, 9, 6.0, "delegate_wait")
    observe(restored, 10, 7.0)
    assert nanny_metered_since_delegate_activity(restored) == (1, None)
    assert "round 9" in nanny_burn_fact(restored)
    assert "historical cost anchor unavailable" in nanny_burn_fact(restored)
    observe(restored, 11, 8.0, "delegate_start")
    observe(restored, 12, 9.0)
    assert nanny_metered_since_delegate_activity(restored) == (1, 1.0)
    assert "unavailable" not in nanny_burn_fact(restored)


@pytest.mark.parametrize("seed", [False, True])
def test_new_empty_checkpoint_keeps_known_fresh_state(seed):
    ctx = context()
    reset_nanny_economics_marks(ctx, route_dispatched=True, delegate_activity_seed=seed)
    assert nanny_burn_fact(restore(snapshot(ctx, 0))) == nanny_burn_fact(ctx)
