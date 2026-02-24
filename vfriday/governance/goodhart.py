"""Anti-Goodhart hidden evaluation utilities."""

from __future__ import annotations

from typing import Dict, Iterable, List

from vfriday.schemas import GoodhartScore


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def non_transfer_recurrence(current_error_type: str | None, recent_error_types: Iterable[str]) -> float:
    """Estimate recurrence ratio of same error signature in recent history."""
    curr = str(current_error_type or "").strip()
    if not curr:
        return 0.0
    recent = [str(x).strip() for x in recent_error_types if str(x).strip()]
    if not recent:
        return 0.0
    hits = sum(1 for x in recent if x == curr)
    return _clamp01(hits / max(1, len(recent)))


def evaluate_hidden_score(
    *,
    leakage_penalty: float,
    verifier_disagreement_rate: float,
    repeated_confusion_after_hints: float,
    post_hint_progress: bool,
    requires_attempt: bool,
) -> GoodhartScore:
    """
    Compute hidden evaluator score for anti-Goodhart control.

    The score is intentionally separate from tutor-visible metrics.
    """
    flags: List[str] = []
    penalty = _clamp01(leakage_penalty)
    disagreement = _clamp01(verifier_disagreement_rate)
    confusion = _clamp01(repeated_confusion_after_hints)

    competency_credit = 1.0 if post_hint_progress else (0.5 if requires_attempt else 0.3)
    raw = 0.65 + 0.20 * competency_credit - 0.45 * penalty - 0.25 * disagreement - 0.15 * confusion
    score = _clamp01(raw)

    if penalty > 0.0:
        flags.append("leakage_penalty_applied")
    if disagreement >= 0.5:
        flags.append("verifier_disagreement_high")
    if confusion >= 0.5:
        flags.append("repeated_confusion_high")
    if score < 0.45:
        flags.append("hidden_score_low")

    return GoodhartScore(
        hidden_score=score,
        leakage_penalty=penalty,
        flags=flags,
        competency_credit=_clamp01(competency_credit),
    )


def model_observed_setpoint_targets(
    *,
    goodhart: GoodhartScore,
    verifier_disagreement_rate: float,
    non_transfer_rate: float,
) -> Dict[str, float]:
    """Map hidden evaluator outputs to observed setpoint targets [0..1]."""
    return {
        "competency": _clamp01(goodhart.competency_credit * (1.0 - goodhart.leakage_penalty)),
        "transfer": _clamp01(1.0 - non_transfer_rate),
        "horizon": _clamp01(1.0 - verifier_disagreement_rate),
        "error_signature": _clamp01(1.0 - non_transfer_rate * 0.7),
        "safety_agency": _clamp01(goodhart.hidden_score),
    }

