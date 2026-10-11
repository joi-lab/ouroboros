"""Rolling-24h spend of consciousness: its wake-ups plus the tasks they started.

Owner decision В11/В18 (PLAN 5.5, 5.13 п.11, 5.14 п.1): the daily allowance
gates NEW starts (a wake, a promoted/scheduled root) on the money the whole
consciousness tree accounted in the last 24 hours. There is no root index —
the roots are read off the ledger itself: every FINAL row younger than the
fold horizon (``USAGE_LEDGER_FOLD_MIN_AGE_SEC``, 48 h — younger attempts are
never folded, so their ``ts`` is the true spend time) whose category is
``consciousness`` (a wake's own rows) or ``consciousness_task`` (a started
root's rows) names a consciousness root; the window is then every money row
(attempt / subscription_session / external_unmetered — never a folded
``usage_baseline_group`` or a ``legacy_*`` row) with ``ts`` inside the last
24 h under one of those roots, reduced by the ONE money reducer
``_usage_rows._summary``. ``accounted_usd`` (settled + reserved + unresolved)
is the number: a hanging reservation can read as exhausted for its lifetime,
disclosed rather than hidden; ``unknown_unmetered`` makes it "at least".

The row selection is memoized by ledger fingerprint through the same
``_render_cached`` seam as ``usage_projection``; the TIME filter runs on
every call, so an exhausted window frees itself as rows age out without a
new ledger write. A ledger that cannot be read yields the typed
``allowance_unknown`` outcome — the caller refuses the start honestly.
"""

from __future__ import annotations

import datetime as _dt
import time
from typing import Any, Dict, Optional

from ouroboros._usage_rows import _summary, row_ts_epoch
from ouroboros._usage_rows_memo import _render_cached
from ouroboros.runtime_limits import USAGE_LEDGER_FOLD_MIN_AGE_SEC, get_consciousness_daily_usd
from ouroboros.usage_ledger import _drive_root

CONSCIOUSNESS_CATEGORIES = frozenset({"consciousness", "consciousness_task"})
MONEY_ROW_KINDS = frozenset({"attempt", "subscription_session", "external_unmetered"})
WINDOW_SEC = 24 * 3600
ROOT_HORIZON_SEC = USAGE_LEDGER_FOLD_MIN_AGE_SEC
STATUS_AVAILABLE, STATUS_EXHAUSTED, STATUS_UNKNOWN = "available", "exhausted", "allowance_unknown"


def _candidate_rows(final: list, integrity_degraded: bool) -> Dict[str, Any]:
    """The fingerprint-memoized selection: money rows under any consciousness root, any age."""
    roots = {
        str(row.get("root_task_id") or "")
        for row in final
        if str(row.get("category") or "") in CONSCIOUSNESS_CATEGORIES and row.get("root_task_id")
    }
    rows = [
        row for row in final
        if str(row.get("kind") or "attempt") in MONEY_ROW_KINDS
        and str(row.get("root_task_id") or "") in roots
    ]
    return {"rows": rows, "integrity_degraded": bool(integrity_degraded)}


def allowance_window(
    drive_root: Any = None, *, now: Optional[float] = None, allow_stale: bool = False,
) -> Dict[str, Any]:
    """The allowance verdict for the 24 h ending at ``now`` (epoch seconds; default: the clock).

    ``allow_stale`` is the STATUS view's read (it admits nothing): under a contended ledger
    lock it rides the last validated snapshot. Every admission of a wake reads exactly."""
    now_ts = time.time() if now is None else float(now)
    limit = float(get_consciousness_daily_usd())
    try:
        selected = _render_cached(
            _drive_root(drive_root), ("consciousness_allowance_rows",), _candidate_rows, allow_stale=allow_stale)
    except Exception as exc:  # noqa: BLE001 — every read failure is the one typed outcome
        return {"status": STATUS_UNKNOWN, "error": f"{type(exc).__name__}: {exc}",
                "limit_usd": limit, "accounted_usd": None, "remaining_usd": None, "resets_at": ""}
    stamped = [(row_ts_epoch(row), row) for row in selected["rows"]]
    roots = {
        str(row.get("root_task_id") or "") for ts, row in stamped
        if ts is not None and ts >= now_ts - ROOT_HORIZON_SEC
        and str(row.get("category") or "") in CONSCIOUSNESS_CATEGORIES
    }
    window = [
        (ts, row) for ts, row in stamped
        if ts is not None and ts >= now_ts - WINDOW_SEC and str(row.get("root_task_id") or "") in roots
    ]
    summary = _summary([row for _ts, row in window])
    accounted = float(summary["accounted_usd"])
    oldest = min((ts for ts, _row in window), default=None)
    resets_at = (
        _dt.datetime.fromtimestamp(oldest + WINDOW_SEC, tz=_dt.timezone.utc).isoformat()
        if oldest is not None else ""
    )
    return {
        "status": STATUS_EXHAUSTED if accounted >= limit else STATUS_AVAILABLE,
        "limit_usd": limit,
        "accounted_usd": accounted,
        "remaining_usd": round(max(0.0, limit - accounted), 6),
        "unknown_unmetered": int(summary["unknown_unmetered"]),
        "non_final_rows": int(summary["non_final_rows"]),
        "roots": sorted(roots),
        "window_rows": len(window),
        "resets_at": resets_at,
        "integrity_degraded": bool(selected["integrity_degraded"]),
    }


def remaining_allowance_usd(drive_root: Any = None, *, now: Optional[float] = None) -> Optional[float]:
    """Dollars left in the window; ``None`` when the ledger could not be read (allowance_unknown)."""
    window = allowance_window(drive_root, now=now)
    return None if window["status"] == STATUS_UNKNOWN else float(window["remaining_usd"])
