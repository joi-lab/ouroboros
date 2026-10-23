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
    phase: str = "preparing"
    started_at: float = field(default_factory=time.time)
    origin_message_ref: Dict[str, Any] = field(default_factory=dict)
    # Reply-preparation facts (DESIGN §9, Telegram typing), never census fields:
    # the owner's door stamped this turn, so its answer is expected where it is
    # routed; and that answer has entered the host's delivery path.
    reply_expected: bool = False
    reply_handed_off: bool = False

    actor: Any = field(default=None, repr=False, compare=False)
    active_llm_call: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)
    parked_wait: Optional[Dict[str, Any]] = field(default=None, repr=False, compare=False)
    parked_root: Any = field(default=None, repr=False, compare=False)

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
        active = self.active_llm_call
        if active and active.get("task_attempt") == row.get("task_attempt") and active.get("model_activity"):
            row["model_activity"] = dict(active["model_activity"])
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
        phase: str = "preparing",
        origin_message_ref: Optional[Dict[str, Any]] = None,
        actor: Any = None,
        reply_expected: bool = False,
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
            phase=str(phase or "preparing"),
            started_at=time.time(),
            origin_message_ref=dict(origin_message_ref or {}),
            actor=actor,
            reply_expected=bool(reply_expected),
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

    def mark_parked(
        self,
        activity_id: str,
        wait: Optional[Dict[str, Any]],
        *,
        drive_root: Any = None,
    ) -> bool:
        aid = str(activity_id or "").strip()
        if not aid:
            return False
        with self._lock:
            entry = self._activities.get(aid)
            if entry is None:
                return False
            entry.parked_wait = dict(wait) if isinstance(wait, dict) else None
            entry.parked_root = drive_root
            self._lock.notify_all()
            return True

    def clear_parked(self, activity_id: str) -> bool:
        aid = str(activity_id or "").strip()
        if not aid:
            return False
        with self._lock:
            entry = self._activities.get(aid)
            if entry is None:
                return False
            entry.parked_wait = None
            entry.parked_root = None
            self._lock.notify_all()
            return True

    def wait_until_empty(self, timeout: float) -> List[str]:
        """Wait for unparked executions, including preparation and post-task work."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                entries = list(self._activities.items())
            # The parked predicate reads the task store and run custody; it runs
            # outside the registry lock so registration and census stay responsive.
            remaining_ids = [aid for aid, entry in entries if not is_parked_direct_entry(entry)]
            if not remaining_ids:
                return []
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return remaining_ids
            with self._lock:
                self._lock.wait(timeout=min(remaining, 0.05))

    def clear(self) -> None:
        """Clear registry — primarily for tests and process resets."""
        with self._lock:
            self._activities.clear()
            self._lock.notify_all()


# Global process-local singleton
_DIRECT_ACTIVITY_REGISTRY = DirectActivityRegistry()


def get_direct_activity_registry() -> DirectActivityRegistry:
    return _DIRECT_ACTIVITY_REGISTRY


def is_parked_owner_wait(task_id: str, wait: Any, *, drive_root: Any = None) -> bool:
    """True when ``task_id`` is durably parked in ``owner_wait`` with no live detached or open runs."""
    tid = str(task_id or "").strip()
    if not tid or not isinstance(wait, dict) or wait.get("state") != "waiting":
        return False
    effective_wait = wait
    if drive_root is not None:
        try:
            from ouroboros.task_results import load_task_result

            row = (load_task_result(drive_root, tid, strict=True) or {}).get("owner_wait")
        except Exception:
            return False
        if not isinstance(row, dict) or row.get("state") != "waiting":
            return False
        effective_wait = row
    try:
        from ouroboros.review_pause import live_detached_operations

        if live_detached_operations(tid):
            return False
    except Exception:
        return False
    if effective_wait.get("reason") == "owner_pause":
        try:
            from ouroboros.budget_pause import observe_task_runs, task_owned_runs_open

            owner_pause = effective_wait.get("owner_pause")
            external = owner_pause.get("external_runs") if isinstance(owner_pause, dict) else None
            if task_owned_runs_open(external):
                if drive_root is None:
                    return False
                external = observe_task_runs(
                    drive_root, tid, reason="owner_pause_retained_runs", request_stop=False,
                )
                if task_owned_runs_open(external):
                    return False
        except Exception:
            return False
    return True


def is_parked_direct_entry(entry: DirectActivityEntry) -> bool:
    if entry.parked_wait is None:
        return False
    return is_parked_owner_wait(entry.activity_id, entry.parked_wait, drive_root=entry.parked_root)


def reply_expected(record: Any) -> bool:
    """Whether the owner awaits this root's answer: the owner's door stamped it
    (``run_origin``'s ``owner_ingress``, which promoted, continued and retried roots
    carry by value) and it is no Presence correspondent's turn. Consciousness,
    schedule and API/CLI roots never carry that stamp; a child counts through its root."""
    from ouroboros.dialogue_provenance import is_presence_task, run_origin

    source = record if isinstance(record, dict) else {}
    return bool(run_origin(source)["owner_ingress"]) and not is_presence_task(source)


def mark_reply_handed_off(task_id: str, running: Any = None) -> None:
    """The task's terminal answer entered the delivery path: its reply is no longer prepared.

    Process-local like both owners it marks, the direct turn's entry or the RUNNING
    row's meta (never the persisted task record), so post-answer work that keeps
    either alive cannot read as preparation (``gateway/reply_preparation.py``).
    """
    tid = str(task_id or "").strip()
    meta = running.get(tid) if isinstance(running, dict) and tid else None
    if isinstance(meta, dict):
        meta["reply_handed_off"] = True
    entry = get_direct_activity_registry().get(tid) if tid else None
    if entry is not None:
        entry.reply_handed_off = True


@contextmanager
def track_direct_activity(
    activity_id: str,
    chat_id: int,
    *,
    client_message_id: str = "",
    project_id: str = "",
    kind: str = "direct_chat",
    phase: str = "preparing",
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
