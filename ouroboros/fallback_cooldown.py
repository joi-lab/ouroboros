"""Process-local 429-aware cooldown for the cross-model fallback chain (F1, v6.39).

A route that just failed transiently (429 / 5xx / overloaded) is parked on a short
cooldown so the fallback chain skips it for a window instead of immediately hammering it
again. Scope is PER-PROCESS: it bounds re-hits WITHIN one worker — a single task's own
fallback walk and its repeated rounds stop re-trying a just-rate-limited binding.
Account pins and Auto are distinct: one account does not cool another. It does
NOT coordinate across sibling worker processes (each worker has its own map), so it is
not a swarm-WIDE rate-limit governor; durable cross-worker coordination is the project
journal / task-tree blackboard's job (Phase 3), not this advisory in-process guard. The
cooldown is ADVISORY (never a durable "model is bad" verdict that could strand a user
after a transient provider event), and heal-back is PASSIVE: the entry simply expires by
timestamp. Default-on, fail-soft (every helper degrades to "not cooling / no-op" on a
bad config value).
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Tuple
from ouroboros.config import runtime_setting

_cooldown: Dict[Tuple[str, bool, str], float] = {}
_lock = threading.Lock()


def cooldown_enabled() -> bool:
    """Default-on; only an explicit falsey value disables it."""
    raw = str(runtime_setting("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", "") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _cooldown_sec() -> float:
    try:
        return max(0.0, float(runtime_setting("OUROBOROS_FALLBACK_COOLDOWN_SEC", "") or 120.0))
    except (TypeError, ValueError):
        return 120.0


def attempts_per_model() -> int:
    """Total attempt ceiling for a SINGLE fallback candidate (1..2) — the chain tries a
    candidate this many times, then moves to the next link. Applied only to fallback
    candidates; the primary model keeps its full per-class retry budgets
    (OUROBOROS_TRANSIENT_RETRY_MAX / max_retries)."""
    try:
        return max(1, min(2, int(runtime_setting("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "") or 1)))
    except (TypeError, ValueError):
        return 1


def mark_cooldown(model: str, use_local: bool = False, account: str = "") -> None:
    """Cool the failed binding (configured pin or Auto, never the observed Auto account)."""
    if not cooldown_enabled():
        return
    key = (str(model or ""), bool(use_local), str(account or ""))
    with _lock:
        _cooldown[key] = time.time() + _cooldown_sec()


def is_cooling_down(model: str, use_local: bool = False, account: str = "") -> bool:
    """True while the binding is inside its cooldown window. Passive heal-back: an expired
    entry is dropped and reads as available again."""
    if not cooldown_enabled():
        return False
    key = (str(model or ""), bool(use_local), str(account or ""))
    now = time.time()
    with _lock:
        until = _cooldown.get(key, 0.0)
        if until and until > now:
            return True
        if until:
            _cooldown.pop(key, None)
        return False


def reset_for_tests() -> None:
    with _lock:
        _cooldown.clear()
