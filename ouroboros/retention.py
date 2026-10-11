"""Single source of truth for age-based runtime cleanup (GC) retention.

Every disposable-runtime-artifact prune -- subagent worktrees, headless/direct
task drives, leftover service logs -- shares ONE owner-facing knob,
``OUROBOROS_GC_RETENTION_DAYS``. Before this module the ``now - days * 86400``
cutoff and the ``max(1, min(days, 365))`` clamp were hand-rolled in four places
with three separate, differently-defaulted keys.

Per-subsystem ``retention_days=`` overrides are still honored by the prune
functions (tests and special cases pass them explicitly); this module only
governs the shared default resolution and the cutoff/clamp math.

The deprecated per-subsystem keys are accepted only as backward-compatible
fallbacks so a previously-stored value is not orphaned (see ``load_settings``
migration in ``config.py``).
"""

from __future__ import annotations

import time
from typing import Any, Optional

GC_RETENTION_HARD_MAX = 365
GC_RETENTION_FALLBACK_DEFAULT = 7

# Deprecated per-subsystem retention keys mapped to their FORMER per-subsystem
# defaults. Kept only so a stored value still resolves / migrates into the unified
# key. The former defaults let the seed picker prefer a CUSTOMIZED value (one that
# differs from its old default) so a user's customization is never dropped.
LEGACY_RETENTION_DEFAULTS = {
    "OUROBOROS_SUBAGENT_WORKTREE_RETENTION_DAYS": 7,
    "OUROBOROS_SERVICE_LOG_RETENTION_DAYS": 14,
    "OUROBOROS_HEADLESS_TASK_RETENTION_DAYS": 7,
}
LEGACY_RETENTION_KEYS = tuple(LEGACY_RETENTION_DEFAULTS)


def pick_legacy_retention_seed(get) -> Optional[Any]:
    """Choose a legacy retention value to seed the unified key.

    ``get`` is a ``key -> value-or-None`` lookup (e.g. ``dict.get`` or
    ``os.environ.get``). Prefer a value that was CUSTOMIZED (differs from its
    former per-subsystem default) so a user's customization is preserved across
    the rename; otherwise fall back to the first present value. Returns ``None``
    when no legacy value is set (the caller then uses the unified default)."""
    fallback = None
    for key, former_default in LEGACY_RETENTION_DEFAULTS.items():
        val = get(key)
        if val is None or str(val).strip() == "":
            continue
        if fallback is None:
            fallback = val
        try:
            if int(val) != int(former_default):
                return val
        except (TypeError, ValueError):
            continue
    return fallback


def _default_gc_days() -> int:
    try:
        from ouroboros.config import SETTINGS_DEFAULTS

        return int(SETTINGS_DEFAULTS.get("OUROBOROS_GC_RETENTION_DAYS", GC_RETENTION_FALLBACK_DEFAULT))
    except Exception:
        return GC_RETENTION_FALLBACK_DEFAULT


def clamp_retention_days(value, *, default: Optional[int] = None, hard_max: int = GC_RETENTION_HARD_MAX) -> int:
    """Clamp a retention-days value into ``[1, hard_max]``; fall back to ``default``
    (or the configured GC default) on missing/invalid/<1 input."""
    base = _default_gc_days() if default is None else int(default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = base
    if parsed < 1:
        parsed = base
    return max(1, min(parsed, hard_max))


def age_cutoff(days, now: Optional[float] = None) -> float:
    """Return the epoch-seconds cutoff: artifacts older than this are GC-eligible.

    Uses ``max(0, days)`` and is intentionally NOT clamped to ``>= 1`` so an
    explicit ``0`` prunes everything created before ``now`` (callers pass 0 to
    force a full prune; the >=1 clamp lives only in ``clamp_retention_days`` for
    default resolution)."""
    base = time.time() if now is None else float(now)
    try:
        whole = max(0, int(days))
    except (TypeError, ValueError):
        whole = 0
    return base - whole * 86400


def get_gc_retention_days() -> int:
    """Resolve the unified GC retention in days.

    Precedence: ``OUROBOROS_GC_RETENTION_DAYS`` -> first set legacy key
    (backward-compat) -> configured default. Always clamped to ``[1, 365]``."""
    from ouroboros.config import runtime_setting

    raw = runtime_setting("OUROBOROS_GC_RETENTION_DAYS", "")
    if str(raw or "").strip():
        return clamp_retention_days(raw)
    seed = pick_legacy_retention_seed(lambda key: runtime_setting(key, ""))
    if seed is not None and str(seed).strip():
        return clamp_retention_days(seed)
    return clamp_retention_days(_default_gc_days())
