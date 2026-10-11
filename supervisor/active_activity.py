"""In-memory, process-local registry for active direct chat activities.

Tracks in-flight direct conversational turns so
the Gateway (/api/state) and WebSocket activity pipeline have authoritative,
thread-safe visibility into in-progress work without creating spurious queue records.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger(__name__)


@dataclass
class DirectActivityEntry:
    activity_id: str
    chat_id: int
    project_id: str = ""
    client_message_id: str = ""
    kind: str = "direct_chat"
    phase: str = "thinking"
    started_at: float = field(default_factory=time.time)
    origin_message_ref: Dict[str, Any] = field(default_factory=dict)

    actor: Any = field(default=None, repr=False, compare=False)

    def _live_model_wait_projection(self, *, availability: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Read the direct turn's existing live ``TaskModelWait`` owner.

        Direct turns do not have a queue ``RUNNING`` row to carry the wait
        projection.  The actor's task-scoped ``ToolContext`` already points at
        the live owner, so use that authority while it is open instead of
        replaying a durable result or introducing a second poller/store.
        """
        try:
            actor = self.actor
            tools = getattr(actor, "tools", None)
            context = getattr(tools, "_ctx", None)
            owner = getattr(context, "model_wait_context", None)
        except Exception:
            if availability is not None:
                availability["complete"] = False
            log.debug("Direct activity owner lookup unavailable: %s", self.activity_id,
                      exc_info=True)
            return {}
        if owner is None or str(getattr(owner, "task_id", "") or "") != self.activity_id:
            return {}
        if bool(getattr(owner, "closed", False)):
            return {}
        snapshot = getattr(owner, "snapshot", None)
        if not callable(snapshot):
            if availability is not None:
                availability["complete"] = False
            return {}
        try:
            projection = snapshot()
        except Exception:
            if availability is not None:
                availability["complete"] = False
            log.debug("Direct activity model-wait snapshot unavailable: %s", self.activity_id,
                      exc_info=True)
            return {}
        if not isinstance(projection, dict):
            if availability is not None:
                availability["complete"] = False
            return {}
        result: Dict[str, Any] = {}
        waits = projection.get("model_waits")
        if isinstance(waits, dict) and waits:
            result["model_waits"] = waits
        try:
            attempt = int(getattr(owner, "attempt", 0) or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt >= 1:
            result["task_attempt"] = attempt
        return result

    def to_dict(self, *, availability: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        row = {
            "activity_id": self.activity_id,
            "chat_id": self.chat_id,
            "project_id": self.project_id,
            "client_message_id": self.client_message_id,
            "kind": self.kind,
            "phase": self.phase,
            "started_at": self.started_at,
        }
        # Keep the historical static shape for an actor that has not entered
        # its task context yet. Once the direct turn owns a live model wait,
        # publish the same optional fields as managed/post-task activities.
        wait_availability: Dict[str, Any] = {}
        row.update(self._live_model_wait_projection(availability=wait_availability))
        if wait_availability.get("complete") is False:
            row["phase"] = "unknown"
            if availability is not None:
                availability["complete"] = False
        return row


class DirectActivityRegistry:
    """Thread-safe registry for active direct-chat turns."""

    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._activities: Dict[str, DirectActivityEntry] = {}

    def register(
        self,
        activity_id: str,
        chat_id: int,
        *,
        client_message_id: str = "",
        project_id: str = "",
        kind: str = "direct_chat",
        phase: str = "thinking",
        origin_message_ref: Optional[Dict[str, Any]] = None,
        actor: Any = None,
    ) -> DirectActivityEntry:
        aid = str(activity_id or "").strip()
        if not aid:
            raise ValueError("activity_id cannot be empty")
        entry = DirectActivityEntry(
            activity_id=aid,
            chat_id=int(chat_id or 0),
            project_id=str(project_id or ""),
            client_message_id=str(client_message_id or ""),
            kind=str(kind or "direct_chat"),
            phase=str(phase or "thinking"),
            started_at=time.time(),
            origin_message_ref=dict(origin_message_ref or {}),
            actor=actor,
        )
        with self._lock:
            self._activities[aid] = entry
        log.debug("Registered direct activity: %s (chat_id=%s, kind=%s)", aid, chat_id, kind)
        return entry

    def unregister(self, activity_id: str) -> Optional[DirectActivityEntry]:
        aid = str(activity_id or "").strip()
        with self._lock:
            entry = self._activities.pop(aid, None)
            self._lock.notify_all()
        if entry:
            log.debug("Unregistered direct activity: %s (chat_id=%s)", aid, entry.chat_id)
        return entry

    def snapshot(
        self, chat_id: Optional[int] = None, *, availability: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            entries = list(self._activities.values())
        return [e.to_dict(availability=availability) for e in entries
                if chat_id is None or e.chat_id == int(chat_id)]

    def get(self, activity_id: str) -> Optional[DirectActivityEntry]:
        aid = str(activity_id or "").strip()
        with self._lock:
            return self._activities.get(aid)

    def actors(self) -> List[DirectActivityEntry]:
        """Private actor handles; never include execution objects in UI snapshots."""
        with self._lock:
            return list(self._activities.values())

    def wait_until_empty(self, timeout: float) -> List[str]:
        """Wait for whole executions, including preparation and post-task work."""
        with self._lock:
            self._lock.wait_for(lambda: not self._activities, timeout=max(0.0, timeout))
            return list(self._activities)

    def clear(self) -> None:
        """Clear registry — primarily for tests and process resets."""
        with self._lock:
            self._activities.clear()
            self._lock.notify_all()


# Global process-local singleton
_DIRECT_ACTIVITY_REGISTRY = DirectActivityRegistry()


def get_direct_activity_registry() -> DirectActivityRegistry:
    return _DIRECT_ACTIVITY_REGISTRY


@contextmanager
def track_direct_activity(
    activity_id: str,
    chat_id: int,
    *,
    client_message_id: str = "",
    project_id: str = "",
    kind: str = "direct_chat",
    phase: str = "thinking",
    origin_message_ref: Optional[Dict[str, Any]] = None,
) -> Iterator[DirectActivityEntry]:
    registry = get_direct_activity_registry()
    entry = registry.register(
        activity_id,
        chat_id,
        client_message_id=client_message_id,
        project_id=project_id,
        kind=kind,
        phase=phase,
        origin_message_ref=origin_message_ref,
    )
    try:
        yield entry
    finally:
        registry.unregister(activity_id)
