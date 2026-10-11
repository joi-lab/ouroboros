"""Per-model concurrency cap (#4, cyber-racing post-mortem): prevent a self-inflicted DoS.

When a task's main loop, its in-process subagent threads, and owner status pings fire at the
SAME rate-limited model at once, the provider answers a storm of 429s and otherwise-good work
dies ``provider_unavailable`` with work already done. A process-local
``threading.BoundedSemaphore`` per resolved route (``model``, ``use_local``) serializes
concurrent provider calls to a small cap; excess threads WAIT (bounded by the task deadline)
instead of all firing and getting rate-limited.

SCOPE — PER-PROCESS only (exactly like ``fallback_cooldown``): heavy worker tasks run in
SEPARATE processes, so this caps the calls WITHIN one process (the main loop + its in-process
subagent threads + status pings), NOT across the multi-worker swarm. A cross-worker governor
(supervisor-mediated admission / a shared lease) is future work; the docs/README state this
limit honestly rather than overclaiming a swarm-wide cap.

Design constraints (codex review):
- Wrap ONLY the actual provider call (``llm.chat``) per attempt — NOT the retry/fallback chain,
  and NEVER a backoff sleep.
- Sync primitive (the calls run on threads, not an asyncio loop), so a ``threading`` semaphore.
- Default-on, FAIL-SOFT: if disabled, mis-resolved, or the slot can't be acquired before the
  task deadline, proceed WITHOUT throttling — never block a task past its deadline, never raise.
- Semaphore wait time is NOT a provider failure (the caller's cooldown classifier never sees it).
- Keyed like ``fallback_cooldown`` on (model, use_local); the model id already carries the
  provider prefix (``z-ai/glm-5.2``, ``cloudru::…``), so this separates distinct routes.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Optional
from ouroboros.config import runtime_setting

_LOCK = threading.Lock()
_SEMAPHORES: dict = {}

def _max_slot_wait_sec() -> float:
    """Hard ceiling (seconds) a single call WAITS for a slot when the task has no deadline,
    so a wedged provider can never park a worker forever. SSOT: config SETTINGS_DEFAULTS."""
    from ouroboros.config import SETTINGS_DEFAULTS

    default = SETTINGS_DEFAULTS["OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC"]
    try:
        return float(runtime_setting("OUROBOROS_MODEL_SLOT_MAX_WAIT_SEC", default))
    except (TypeError, ValueError):
        return float(default)


def _cap() -> int:
    """Max concurrent provider calls allowed per (model, use_local) route. <=0 disables.
    The default comes from the config SSOT (SETTINGS_DEFAULTS), not a hardcoded literal."""
    from ouroboros.config import SETTINGS_DEFAULTS

    default = SETTINGS_DEFAULTS.get("OUROBOROS_MODEL_MAX_CONCURRENCY", 3)
    try:
        return int(runtime_setting("OUROBOROS_MODEL_MAX_CONCURRENCY", default))
    except (TypeError, ValueError):
        try:
            return int(default)
        except (TypeError, ValueError):
            return 3


def enabled() -> bool:
    # Single SSOT knob: OUROBOROS_MODEL_MAX_CONCURRENCY (<=0 disables the guard). No
    # separate enable flag — one config surface only (P7 minimalism / DEVELOPMENT SSOT).
    return _cap() > 0


def _semaphore_for(model: str, use_local: bool) -> threading.BoundedSemaphore:
    # The cap is part of the key so changing OUROBOROS_MODEL_MAX_CONCURRENCY at runtime
    # (settings hot-reload) takes effect on the next call — a route's existing semaphore
    # is never silently stuck at the old cap.
    key = (str(model or ""), bool(use_local), max(1, _cap()))
    with _LOCK:
        sem = _SEMAPHORES.get(key)
        if sem is None:
            sem = threading.BoundedSemaphore(key[2])
            _SEMAPHORES[key] = sem
        return sem


@contextlib.contextmanager
def model_call_slot(model: str, use_local: bool = False, deadline_ts: Optional[float] = None):
    """Hold a per-route concurrency slot around ONE provider call.

    Fail-soft: if disabled, or the slot can't be acquired before the task deadline (or the
    no-deadline wait ceiling), proceed WITHOUT a slot (no throttle) rather than blocking
    the task past its deadline. Never raises out of the context setup.
    """
    if not enabled():
        yield
        return
    try:
        sem = _semaphore_for(model, use_local)
    except Exception:
        yield
        return
    # Bound the wait by the remaining deadline (epoch seconds) and the hard ceiling.
    timeout = _max_slot_wait_sec()
    if deadline_ts:
        timeout = max(0.0, min(timeout, float(deadline_ts) - time.time()))
    acquired = False
    try:
        acquired = sem.acquire(timeout=timeout) if timeout > 0 else False
    except Exception:
        acquired = False
    try:
        yield
    finally:
        if acquired:
            try:
                sem.release()
            except (ValueError, RuntimeError):
                pass


def reset_for_tests() -> None:
    with _LOCK:
        _SEMAPHORES.clear()
