"""Dynamic homeostatic setpoint updates (EWMA + drift clamps)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Tuple


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def update_setpoints(
    *,
    current: Dict[str, float],
    observed: Dict[str, float],
    ewma_alpha: float,
    max_daily_drift: float,
    now_iso: str,
    previous_updated_at: str | None = None,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Update setpoints with EWMA and per-day drift cap.

    Returns:
        (new_setpoints, absolute_drift_map)
    """
    alpha = _clamp01(ewma_alpha)
    daily_cap = max(0.0, float(max_daily_drift))
    now_dt = _parse_iso(now_iso) or datetime.now(timezone.utc)
    prev_dt = _parse_iso(previous_updated_at) or now_dt
    elapsed_days = max(1.0, min(7.0, (now_dt - prev_dt).total_seconds() / 86400.0))
    cap = daily_cap * elapsed_days

    new_values: Dict[str, float] = {}
    drift_map: Dict[str, float] = {}
    keys = sorted(set((current or {}).keys()) | set((observed or {}).keys()))
    for key in keys:
        cur = _clamp01(float((current or {}).get(key, 0.5)))
        target = _clamp01(float((observed or {}).get(key, cur)))
        proposed_delta = alpha * (target - cur)
        bounded_delta = max(-cap, min(cap, proposed_delta))
        updated = _clamp01(cur + bounded_delta)
        new_values[key] = round(updated, 6)
        drift_map[key] = round(abs(updated - cur), 6)

    return new_values, drift_map

