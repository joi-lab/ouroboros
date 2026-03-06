"""Shared stress metrics for Viktor and AI co-adaptation."""

from __future__ import annotations

from typing import Dict, Tuple


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def weighted_stress(factors: Dict[str, float], weights: Dict[str, float]) -> float:
    """Compute weighted stress in [0..1]. Missing factors default to 0."""
    w_total = 0.0
    score = 0.0
    for key, weight in (weights or {}).items():
        w = max(0.0, float(weight))
        v = _clamp01(float((factors or {}).get(key, 0.0)))
        score += v * w
        w_total += w
    if w_total <= 0:
        return 0.0
    return _clamp01(score / w_total)


def make_ai_factors(
    *,
    verifier_disagreement_rate: float,
    repeated_confusion_after_hints: float,
    direct_answer_pressure_incidents: float,
    latency_ms: int,
    sla_ms: int = 8000,
    non_transfer_recurrence: float,
) -> Dict[str, float]:
    """Build normalized AI stress factors."""
    latency_over_sla = max(0.0, (float(latency_ms) - float(sla_ms)) / max(1.0, float(sla_ms)))
    return {
        "verifier_disagreement_rate": _clamp01(verifier_disagreement_rate),
        "repeated_confusion_after_hints": _clamp01(repeated_confusion_after_hints),
        "direct_answer_pressure_incidents": _clamp01(direct_answer_pressure_incidents),
        "latency_over_sla": _clamp01(latency_over_sla),
        "non_transfer_recurrence": _clamp01(non_transfer_recurrence),
    }


def make_viktor_factors(
    *,
    idle_seconds: float,
    idle_threshold_seconds: float = 40.0,
    hint_to_progress_lag: float = 0.0,
    repeated_error_signature: float = 0.0,
) -> Dict[str, float]:
    """Build normalized Viktor stress proxies."""
    idle_blocks = max(0.0, (float(idle_seconds) - float(idle_threshold_seconds)) / max(1.0, float(idle_threshold_seconds)))
    return {
        "idle_blocks_over_threshold": _clamp01(idle_blocks),
        "hint_to_progress_lag": _clamp01(hint_to_progress_lag),
        "repeated_error_signature": _clamp01(repeated_error_signature),
    }


def compute_shared_stress(
    *,
    ai_factors: Dict[str, float],
    viktor_factors: Dict[str, float],
    stress_weights_ai: Dict[str, float],
    stress_weights_viktor: Dict[str, float],
) -> Tuple[float, float]:
    """Compute both AI and Viktor stress values."""
    return (
        weighted_stress(ai_factors, stress_weights_ai),
        weighted_stress(viktor_factors, stress_weights_viktor),
    )

