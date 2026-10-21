"""Lock-hold timing of store transactions, in memory, per process.

``store.transaction`` calls ``record`` once per transaction with when its lock was taken
(``BEGIN IMMEDIATE`` returned, or a read's first SELECT took its SHARED lock) and measures until
COMMIT or ROLLBACK returned, a COMMIT's wait on draining readers included. The window keeps, per
store kind (``current``, ``dialog``, ``usage`` or ``other``, from the connection's database file
name; the dialogue and usage stores share ``store.transaction``) and per ``read``/``write``:
``count``, ``total_sec``, ``max_sec`` and ``max_site``, the code site (``module:function:line``)
of the window's longest hold, the first frame outside the store modules. ``drain()`` returns the
window as ``{kind: {"read": {...}, "write": {...}}}`` (only buckets with a count) and resets it: the
per-minute diagnostics row publishes it. Nothing here writes to disk, takes a store lock or raises
into the caller: a failure inside the hook is swallowed and the transaction's outcome stands.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Dict

KINDS = {"current.sqlite": "current", "dialog.sqlite": "dialog", "usage.sqlite": "usage"}
# The frames the site skips: the store transport, not the owner that opened the transaction.
_STORE_FRAMES = frozenset({"store.py", "store_timing.py", "store_hot_facts.py", "store_task_cards.py", "task_store.py",
                           "dialogue_store.py", "usage_store.py", "contextlib.py"})
_LOCK = threading.Lock()
_WINDOW: Dict[str, Dict[str, Dict[str, Any]]] = {}
# The hold clock: high resolution on every OS (``time.monotonic`` ticks at ~15.6 ms on Windows, where a short hold
# would measure 0.0).
clock = time.perf_counter


def kind_of(path: Any) -> str:
    """The store kind a database file name stands for (``other`` for anything else)."""
    return KINDS.get(os.path.basename(os.fspath(path)), "other")


def record(conn: Any, write: bool, started: float) -> None:
    """One transaction held its lock from ``started`` (``clock()``) until now; never raises. A hold as long as the
    window's longest names its site too, so a window whose holds all measure 0.0 still names one."""
    try:
        held = clock() - started
        kind = getattr(conn, "_store_kind", None) or "other"
        with _LOCK:
            bucket = _WINDOW.setdefault(kind, {}).setdefault(
                "write" if write else "read", {"count": 0, "total_sec": 0.0, "max_sec": 0.0, "max_site": ""})
            bucket["count"] += 1
            bucket["total_sec"] += held
            if held >= bucket["max_sec"]:
                bucket["max_sec"], bucket["max_site"] = held, _site()
    except Exception:  # a diagnostic never changes the transaction's outcome
        pass


def _site() -> str:
    frame = sys._getframe(1)
    while frame is not None and os.path.basename(frame.f_code.co_filename) in _STORE_FRAMES:
        frame = frame.f_back
    return "" if frame is None else f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_name}:{frame.f_lineno}"


def drain() -> Dict[str, Dict[str, Dict[str, Any]]]:
    """The window since the last drain (seconds rounded to microseconds), then an empty window."""
    with _LOCK:
        window = {kind: {side: {**bucket, "total_sec": round(bucket["total_sec"], 6), "max_sec": round(bucket["max_sec"], 6)}
                         for side, bucket in sides.items()} for kind, sides in _WINDOW.items()}
        _WINDOW.clear()
    return window
