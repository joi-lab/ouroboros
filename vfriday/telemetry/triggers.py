"""Trigger batching rules for ingest events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from vfriday.schemas import TriggerType


IDLE_THRESHOLD_SECONDS = 40.0


def normalize_trigger(
    *,
    requested: TriggerType,
    idle_seconds: float | None = None,
    user_message: str | None = None,
) -> TriggerType:
    """Normalize incoming trigger using policy defaults."""
    msg = (user_message or "").strip().lower()
    if msg.startswith("/help"):
        return TriggerType.HELP_REQUEST
    if requested == TriggerType.PAUSE and (idle_seconds or 0.0) < IDLE_THRESHOLD_SECONDS:
        return TriggerType.CONTEXT_SWITCH
    return requested


def should_emit_pause_trigger(idle_seconds: float | None, threshold_seconds: float = IDLE_THRESHOLD_SECONDS) -> bool:
    """Return True when pause trigger should fire."""
    return float(idle_seconds or 0.0) >= float(threshold_seconds)


@dataclass
class PauseBatcher:
    """Simple stateful helper for polling-based integrations."""

    threshold_seconds: float = IDLE_THRESHOLD_SECONDS
    last_activity_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def mark_activity(self, now: datetime | None = None) -> None:
        self.last_activity_at = now or datetime.now(timezone.utc)

    def idle_seconds(self, now: datetime | None = None) -> float:
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - self.last_activity_at).total_seconds())

    def should_fire(self, now: datetime | None = None) -> bool:
        return self.idle_seconds(now) >= self.threshold_seconds
