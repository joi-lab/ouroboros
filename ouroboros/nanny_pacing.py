"""Metered pacing state for configured-session host actors."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


DELEGATE_ACTIVITY_TOOLS = frozenset({
    "delegate_start", "delegate_wait", "delegate_cancel", "delegate_answer",
    "delegate_message",
})

# Only genuine ACTS of delegation reset the burn baseline: starting a physical
# run or spawning an explicit child (sprint plan D6, owner-approved 2026-08-28;
# the wait-style treatment of answer/cancel below is the operator's disclosed
# reading of that plan — "only start/schedule reset" — not a separate owner
# decision). Supervision verbs advance the round baseline while dollars keep
# accumulating; coordination verbs are untracked — never a meter reset, and
# the poltergeist pattern was tens of metered rounds each "paid for" by a
# cheap tree_read/verify_and_record baseline reset.
BASELINE_RESET_TOOLS = frozenset({"delegate_start", "schedule_subagent"})

def note_nanny_delegate_activity(
    ctx: Any,
    round_idx: int,
    accumulated_usage: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
) -> None:
    """Advance metered progress and the latest physical/host coordination baseline."""

    if not getattr(ctx, "_nanny_route_dispatched", False):
        return
    try:
        cost = float(accumulated_usage.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    mark = {"round": int(round_idx), "cost": cost}
    ctx._nanny_metered_progress = mark
    verbs = set()
    for call in tool_calls or []:
        fn = call.get("function") if isinstance(call, dict) else None
        name = str((fn or {}).get("name") or "").strip() if isinstance(fn, dict) else ""
        if name in DELEGATE_ACTIVITY_TOOLS or name in BASELINE_RESET_TOOLS:
            verbs.add(name)
    # Coordination verbs are deliberately NOT tracked: they neither reset the
    # meter (charter) nor feed any reader — the unified reminder wording
    # already counts supervision/coordination rounds toward the burn.
    if not verbs:
        return
    if verbs & BASELINE_RESET_TOOLS:
        ctx._nanny_delegate_baseline = dict(mark)
        # Only a real act of delegation re-arms the reminder from scratch.
        ctx._nanny_reminder_mark = None
    else:
        # Supervision (wait/answer/cancel) advances the round baseline but
        # preserves cumulative dollar burn — and deliberately KEEPS the
        # reminder cursor: with dollars accumulating across supervision,
        # wiping the cursor on every wait/answer would re-fire the reminder
        # each following round once lifetime burn crosses the threshold
        # (the adversarial-wave "reminder storm"), bypassing the
        # threshold-width re-arm and punishing honest waiting harder than
        # co-building.
        prior = getattr(ctx, "_nanny_delegate_baseline", None)
        prior_cost = float(prior.get("cost") or 0.0) if isinstance(prior, dict) else 0.0
        ctx._nanny_delegate_baseline = {"round": mark["round"], "cost": prior_cost}


def nanny_metered_since_delegate_activity(ctx: Any) -> Tuple[int, float]:
    """Return own metered rounds/dollars since the latest coordination baseline."""

    progress = getattr(ctx, "_nanny_metered_progress", None)
    progress = progress if isinstance(progress, dict) else {}
    baseline = getattr(ctx, "_nanny_delegate_baseline", None)
    baseline = baseline if isinstance(baseline, dict) else {}
    try:
        rounds = max(
            0, int(progress.get("round") or 0) - int(baseline.get("round") or 0),
        )
    except (TypeError, ValueError):
        rounds = 0
    try:
        cost = max(
            0.0, float(progress.get("cost") or 0.0) - float(baseline.get("cost") or 0.0),
        )
    except (TypeError, ValueError):
        cost = 0.0
    return rounds, cost


def nanny_reminder_due(ctx: Any, round_idx: int) -> Tuple[int, float, bool]:
    """Return measured burn and whether either proportional reminder axis is due."""

    from ouroboros.task_pacing import (
        NANNY_FIRST_REMINDER_ROUNDS,
        NANNY_REMINDER_ROUNDS,
        NANNY_REMINDER_USD,
    )

    rounds, cost = nanny_metered_since_delegate_activity(ctx)
    round_threshold = NANNY_REMINDER_ROUNDS
    if (
        not isinstance(getattr(ctx, "_nanny_delegate_baseline", None), dict)
        and not isinstance(getattr(ctx, "_nanny_reminder_mark", None), dict)
    ):
        round_threshold = NANNY_FIRST_REMINDER_ROUNDS
    if rounds < round_threshold and cost < NANNY_REMINDER_USD:
        return rounds, cost, False
    mark = getattr(ctx, "_nanny_reminder_mark", None)
    if not isinstance(mark, dict):
        return rounds, cost, True
    progress = getattr(ctx, "_nanny_metered_progress", None)
    progress = progress if isinstance(progress, dict) else {}
    try:
        rounds_since_fire = int(progress.get("round") or 0) - int(mark.get("round") or 0)
    except (TypeError, ValueError):
        rounds_since_fire = 0
    try:
        cost_since_fire = float(progress.get("cost") or 0.0) - float(mark.get("cost") or 0.0)
    except (TypeError, ValueError):
        cost_since_fire = 0.0
    due = (
        rounds_since_fire >= NANNY_REMINDER_ROUNDS
        or cost_since_fire >= NANNY_REMINDER_USD
    )
    return rounds, cost, due


def nanny_burn_phrase(rounds: int, cost: float) -> str:
    if rounds <= 0 and cost > 0:
        # Supervision advances the round baseline while dollars accumulate, so
        # "0 rounds (~$2.45)" would be an absurd self-contradiction.
        return f"~${cost:.2f} of your own metered spend"
    if cost > 0:
        return f"{rounds} of your own metered LLM rounds (~${cost:.2f})"
    return f"{rounds} of your own LLM rounds (no provider price reported: cash cost unknown, not zero)"


# Compatibility spellings retained on ``ouroboros.loop`` through imports.
_DELEGATE_ACTIVITY_TOOLS = DELEGATE_ACTIVITY_TOOLS
_note_nanny_delegate_activity = note_nanny_delegate_activity
_nanny_metered_since_delegate_activity = nanny_metered_since_delegate_activity
_nanny_reminder_due = nanny_reminder_due
_nanny_burn_phrase = nanny_burn_phrase
