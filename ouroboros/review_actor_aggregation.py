"""Contract aggregation for completed review actor rows."""

from __future__ import annotations

from typing import Any, Callable, Dict, List

from ouroboros.review_projection import AWAITING_PROJECTION, awaiting_panel_reason
from ouroboros.triad_review import parse_review_findings


def aggregate_review_actors(
    *,
    request: Any,
    slots: List[Any],
    actors: List[Any],
    slots_by_id: Dict[str, Any],
    actor_projection: Callable[[Any, str], Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply participation, quorum, and enforcement semantics."""
    all_findings: List[Dict[str, Any]] = []
    # Split participation faults (a slot errored / timed out / returned empty)
    # from parse-degraded (a slot produced a DEGRADED verdict or unparseable
    # text). Only a participation fault fail-closes: a single Markdown/non-JSON
    # slot must NOT poison a clean quorum PASS.
    actor_errors: List[str] = []
    parse_degraded: List[str] = []
    awaiting: List[str] = []
    fail_count = 0
    pass_count = 0
    for actor in actors:
        parsed, findings, signal = parse_review_findings(actor.raw_text)
        actor.parsed = parsed
        actor.signal = signal
        slot = slots_by_id.get(actor.slot_id)
        actor.actor_role = (
            str(getattr(slot, "role_hint", "") or "").strip()
            or f"{request.surface} reviewer"
        )
        truth = actor_projection(actor, request.surface)
        for key in (
            "model", "transport_status", "parse_status", "semantic_verdict", "provider",
            "coverage", "reason",
        ):
            setattr(actor, key, truth[key])
        # A slot released at the dispatch barrier is a gap: it holds a participation fault's
        # fail-closed place without being reported as one. The projection decides it, from the
        # row as a mapping (the typed predicate never matches a dataclass).
        held = actor.status in {"error", "not_dispatched"} and truth["transport_status"] == AWAITING_PROJECTION
        if held:
            awaiting.append(actor.slot_id)
        elif actor.status in {"error", "not_dispatched"}:
            actor_errors.append(f"{actor.slot_id}:{actor.error}")
        elif actor.status != "ok":
            actor_errors.append(f"{actor.slot_id}:{actor.status}")
        all_findings.extend(
            {**item, "slot_id": actor.slot_id, "model": actor.model}
            for item in findings
        )
        if signal == "FAIL":
            fail_count += 1
        elif signal == "PASS":
            pass_count += 1
        elif signal == "DEGRADED" and not held:
            parse_degraded.append(f"{actor.slot_id}:degraded")

    min_successful = max(
        1, int((request.policy or {}).get("min_successful_slots") or 1)
    )
    fail_closed_on_errors = bool((request.policy or {}).get("fail_closed_on_errors"))
    degraded_reasons = actor_errors + parse_degraded
    if fail_count >= 1:
        aggregate = "FAIL"
    elif pass_count >= min_successful and not (
        fail_closed_on_errors and (actor_errors or awaiting)
    ):
        aggregate = "PASS"
    else:
        aggregate = "DEGRADED"
        if not degraded_reasons and not awaiting:
            degraded_reasons.append(
                f"quorum_not_met: pass_count={pass_count} < min_successful={min_successful}"
            )

    if awaiting:
        degraded_reasons.insert(0, awaiting_panel_reason(awaiting, len(actors), aggregate))
    participating_ids = {
        actor.slot_id
        for actor in actors
        if str(actor.signal or "").upper() in {"PASS", "FAIL"}
    }
    for actor in actors:
        actor.quorum_contribution = actor.slot_id in participating_ids
        if not actor.quorum_contribution:
            actor.enforcement_impact = "abstains"
        elif str(actor.signal or "").upper() == "FAIL":
            actor.enforcement_impact = "veto"
        else:
            actor.enforcement_impact = "supports_pass"
    return {
        "parsed_findings": all_findings,
        "aggregate_signal": aggregate,
        "degraded": aggregate == "DEGRADED",
        "degraded_reasons": degraded_reasons,
        "single_reviewer_no_diversity": len(slots) == 1,
    }
