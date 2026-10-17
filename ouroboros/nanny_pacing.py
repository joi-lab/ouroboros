"""Metered pacing state for configured-session host actors.

The marks feed per-round and forced-finalization facts: a
harness-dispatched nanny reads its own burn as observed numbers with their
anchors named, and decides for itself (owner 11 Oct: facts, no advice; no cap
returns, owner 12 Aug "2=B").
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


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
# cheap tree_read/verify_and_record baseline reset. Both anchors move on the
# CALL (a request): its success is not implied.
BASELINE_RESET_TOOLS = frozenset({"delegate_start", "schedule_subagent"})


def note_nanny_delegate_activity(
    ctx: Any,
    round_idx: int,
    accumulated_usage: Dict[str, Any],
    tool_calls: List[Dict[str, Any]],
) -> None:
    """Advance metered progress and the latest physical/host coordination baseline.

    ``cost`` is the task's recorded cost (``llm_pricing.add_usage``: provider prices and
    estimates summed, unpriced calls absent) or ``None`` while no cost has been recorded
    at all — never a stand-in zero; the usage carrier's ``cost_final`` False says an
    estimated or unpriced call exists."""

    if not getattr(ctx, "_nanny_route_dispatched", False):
        return
    mark = {"round": int(round_idx), "cost": _recorded_cost(accumulated_usage.get("cost"))}
    ctx._nanny_metered_progress = mark
    verbs = set()
    for call in tool_calls or []:
        fn = call.get("function") if isinstance(call, dict) else None
        name = str((fn or {}).get("name") or "").strip() if isinstance(fn, dict) else ""
        if name in DELEGATE_ACTIVITY_TOOLS or name in BASELINE_RESET_TOOLS:
            verbs.add(name)
    if not verbs:
        return
    if verbs & BASELINE_RESET_TOOLS:
        ctx._nanny_delegate_baseline = {"round": mark["round"], "cost": mark["cost"], "cost_round": mark["round"]}
    else:
        # Supervision (wait/answer/cancel/message) advances the round anchor and
        # keeps the dollar anchor at the last start/schedule call (task start: None).
        prior = getattr(ctx, "_nanny_delegate_baseline", None)
        prior = prior if isinstance(prior, dict) else {"cost": None}
        ctx._nanny_delegate_baseline = {
            "round": mark["round"],
            **({"cost": _recorded_cost(prior.get("cost")),
                "cost_round": prior.get("cost_round", prior.get("round"))}
               if "cost" in prior else {}),
        }


def _recorded_cost(raw: Any) -> Optional[float]:
    try:
        return None if raw is None or isinstance(raw, bool) else float(raw)
    except (TypeError, ValueError):
        return None


def nanny_metered_since_delegate_activity(ctx: Any) -> Tuple[Optional[int], Optional[float]]:
    """Own rounds since the round anchor, and the recorded cost since the dollar anchor.

    The cost is read from the bound loop usage, or the saved mark when unbound: missing money
    stays unknown. Before any costed call the recorded sum at an anchor is zero by
    construction (it only sums recorded costs), so an anchor without one subtracts
    nothing; that is a subtotal, never a claim about unpriced calls. Legacy cold
    marks lack round/cost keys: each interval stays unknown until a new request
    supplies that anchor, without reconstructing history."""

    progress = getattr(ctx, "_nanny_metered_progress", None)
    progress = progress if isinstance(progress, dict) else {}
    baseline = getattr(ctx, "_nanny_delegate_baseline", None)
    baseline = baseline if isinstance(baseline, dict) else {"round": 0, "cost": 0.0}
    try:
        rounds = max(
            0, int(progress.get("round") or 0) - int(baseline.get("round") or 0),
        )
    except (TypeError, ValueError):
        rounds = 0
    if "round" not in baseline:
        rounds = None  # older cold checkpoints have no saved activity marks
    if "cost" not in baseline:
        return rounds, None
    # Compaction can add usage between responses; the mark owns rounds, not current money.
    usage = getattr(ctx, "_accumulated_usage", None)
    current = usage if isinstance(usage, dict) else progress
    now, anchor = _recorded_cost(current.get("cost")), _recorded_cost(baseline.get("cost"))
    return rounds, None if now is None else max(0.0, now - (anchor or 0.0))


def _anchor(round_value: Any) -> str:
    try:
        number = int(round_value)
    except (TypeError, ValueError):
        return "round unknown"
    # A round-0 anchor is the host's startup delegated run (``agent_dispatch`` seed).
    return "host-started run before round 1" if number <= 0 else f"round {number}"


def nanny_burn_fact(ctx: Any) -> str:
    """The facts-line segment of a harness-dispatched nanny, ``""`` for any other task.

    Money is known (``$X``, a confirmed zero included), a subtotal that is not final
    when an estimated or unpriced call exists (``add_usage`` sums estimates and has
    nothing to add for an unpriced call), or unknown when no cost was recorded at all.
    The host seed (``agent_dispatch``, round 0) anchors rounds at the host-started run
    but records no cost there, so its dollars count from task start."""
    if not getattr(ctx, "_nanny_route_dispatched", False):
        return ""
    usage = getattr(ctx, "_accumulated_usage", None)
    partial = isinstance(usage, dict) and usage.get("cost_final") is False  # sticky per task (llm_pricing.add_usage)
    rounds, cost = nanny_metered_since_delegate_activity(ctx)
    rounds = rounds if rounds is not None else "unknown"
    baseline = getattr(ctx, "_nanny_delegate_baseline", None)
    if cost is None:
        money = "unknown (no provider price recorded)"
    elif partial:
        money = f"${cost:.2f} subtotal, not final (estimates included; unpriced calls not counted)"
    else:
        money = f"${cost:.2f}"
    if not isinstance(baseline, dict):
        return (f"nanny: no delegate_start/schedule_subagent call yet; since task start {rounds} own rounds, "
                f"own recorded cost {money}")
    cost_anchor = baseline.get("cost_round", baseline.get("round"))
    if "cost" not in baseline:
        money = "unknown"
        since_cost = "since the last delegation request (historical cost anchor unavailable)"
    elif cost_anchor is None:
        since_cost = "since task start (no delegate_start/schedule_subagent call yet)"
    elif int(cost_anchor) <= 0:
        since_cost = "since task start (the host-started run records no cost anchor)"
    else:
        since_cost = f"since the last delegate_start/schedule_subagent call ({_anchor(cost_anchor)})"
    return (f"nanny: {rounds} own rounds since the last delegate verb call ({_anchor(baseline.get('round'))}); "
            f"own recorded cost {money} {since_cost}; these anchors are requests, not confirmed runs")


# Compatibility spellings retained on ``ouroboros.loop`` through imports.
_DELEGATE_ACTIVITY_TOOLS = DELEGATE_ACTIVITY_TOOLS
_note_nanny_delegate_activity = note_nanny_delegate_activity
_nanny_metered_since_delegate_activity = nanny_metered_since_delegate_activity
