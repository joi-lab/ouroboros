"""Process-local observations of the existing managed-update executor.

This is presentation state, never update/recovery authority. Only the synchronous
executor that acquired the update lock can advance its observation; cancelling
the HTTP await does not finish work still running in ``asyncio.to_thread``.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import uuid

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)
_lock = threading.Lock()
_current: dict = {}


def snapshot() -> dict:
    """A short copy independent of the worker queue and monetary locks."""
    with _lock:
        return {key: value for key, value in _current.items() if key != "owner_thread"}


def _notify() -> None:
    # A progress notice is NOT update_status_ready: that event proves the boot
    # finalizer returned, and the UI uses it to settle a reconnect episode.
    from ouroboros.gateway.ws import broadcast_ws_sync

    broadcast_ws_sync({"type": "update_progress_changed"})


def begin() -> None:
    """Called only after the executor acquired the existing update lock."""
    global _current
    try:
        from ouroboros.process_custody import current_custody_session_id

        now = utc_now_iso()
        with _lock:
            _current = {
                "operation_id": uuid.uuid4().hex,
                "generation": current_custody_session_id() or f"pid:{os.getpid()}",
                "stage": "preparing", "started_at": now, "stage_started_at": now,
                "active": True, "result": "", "error": "", "restart_required": False,
                "owner_thread": threading.get_ident(),
            }
        _notify()
    except Exception:
        log.debug("Could not publish update progress", exc_info=True)


def advance(stage: str) -> None:
    try:
        with _lock:
            if (not _current.get("active")
                    or _current.get("owner_thread") != threading.get_ident()
                    or _current.get("stage") == stage):
                return
            _current.update(stage=stage, stage_started_at=utc_now_iso())
        _notify()
    except Exception:
        log.debug("Could not publish update stage", exc_info=True)


def _finish(response, error: Exception | None) -> None:
    try:
        body = json.loads(response.body) if response is not None else {}
        failure = str(body.get("error") or error or "")
        result = ("failed" if error or getattr(response, "status_code", 500) >= 400
                  else "restart_requested" if body.get("restarting")
                  else str(body.get("status") or "completed"))
        with _lock:
            if not _current.get("active") or _current.get("owner_thread") != threading.get_ident():
                return
            _current.update(
                active=False, result=result, error=failure,
                restart_required=bool(body.get("restart_required") or result == "restart_required"),
            )
        _notify()
    except Exception:
        log.debug("Could not finish update progress", exc_info=True)


def acknowledge_failure() -> None:
    """An explicit successful recheck dismisses a retryable old observation."""
    try:
        with _lock:
            if (_current.get("active") or _current.get("restart_required")
                    or _current.get("result") != "failed"):
                return
            _current.clear()
        _notify()
    except Exception:
        log.debug("Could not acknowledge update failure", exc_info=True)


def observe_result(execute):
    """Observe the actual executor's return, including a disconnected client."""
    @functools.wraps(execute)
    def observed(*args, **kwargs):
        response, error = None, None
        try:
            response = execute(*args, **kwargs)
            return response
        except Exception as exc:
            error = exc
            raise
        finally:
            _finish(response, error)

    return observed
