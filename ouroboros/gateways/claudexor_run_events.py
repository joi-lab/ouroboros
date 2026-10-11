"""Bounded reads of one Claudexor run's own event journal (``GET /v2/runs/:id/events``).

Transport only, beside ``gateways/claudexor.py`` (docs/DEVELOPMENT.md "Gateway Boundary
Pattern"). The engine serves every persisted run event with its durable ``seq`` as the SSE
``id`` — the same cursor its run detail reports as ``lastSeq`` — and its secret-redacted JSON
line as ``data``; it resumes after ``Last-Event-ID`` and ends the stream at a terminal run
event. The run detail's ``timeline`` is a projection of the same journal WITHOUT that
identity, which is why a caller that must say exactly which events it showed reads here.

One call is one bounded read inside an existing wait: it opens the stream at a cursor, reads
to a caller-given ``seq`` fence, the stream's own end, a byte bound or a wall-clock bound,
and closes it. It never subscribes, retries or polls. Only whole frames count as read, so
``through_seq`` is always a cursor the next read can resume from exactly. Presence of the
route is negotiated from the engine's own catalog, like every other optional route.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ouroboros.gateways.claudexor import ClaudexorUnavailable

RUN_EVENTS_OPERATION = ("GET", "/v2/runs/:id/events")
# One wait poll's worth of journal is far below this; a read that reaches it stops at the
# last whole frame and says so, and the next read resumes from that cursor.
MAX_READ_BYTES = 8 * 1024 * 1024
# Catalog answers per exact engine identity (version, build sha): a route cannot appear or
# vanish without the engine changing, so one catalog read per identity per process.
_SUPPORT: Dict[Tuple[str, str], bool] = {}
_SUPPORT_LOCK = threading.Lock()


@dataclass
class RunEvent:
    """One journal event exactly as served: its cursor, its type, its redacted line."""

    seq: int
    type: str
    data: str
    value: Optional[Dict[str, Any]] = None


@dataclass
class RunEventsRead:
    """What one bounded read obtained. ``through_seq`` is the last WHOLE event read."""

    events: List[RunEvent] = field(default_factory=list)
    through_seq: int = 0
    ended: bool = False
    stop: str = "time"  # fence | end | bytes | time
    bytes_read: int = 0


def run_events_supported(gateway: Any, *, timeout_sec: float) -> bool:
    """Does the handshaken engine behind ``gateway`` list the run event stream?

    An unhandshaken transport, or one without the stream seam (an older client or a test
    double), is answered ``False`` without a request: the caller keeps its window view.
    """
    version = str(getattr(gateway, "engine_version", "") or "")
    if not version or not callable(getattr(gateway, "open_run_events", None)):
        return False
    key = (version, str(getattr(gateway, "engine_build_sha", "") or ""))
    with _SUPPORT_LOCK:
        if key in _SUPPORT:
            return _SUPPORT[key]
    method, path = RUN_EVENTS_OPERATION
    supported = any(row.get("method") == method and row.get("path") == path
                    for row in gateway.operations(timeout_sec=timeout_sec) if isinstance(row, dict))
    with _SUPPORT_LOCK:
        _SUPPORT[key] = supported
    return supported


def _refusal(response: Any) -> ClaudexorUnavailable:
    try:
        body = json.loads(response.read() or b"{}")
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    code = str(body.get("code") or f"http_{response.status_code}")
    message = str(body.get("message") or body.get("error") or response.status_code)[:500]
    return ClaudexorUnavailable(code, f"run event stream refused: {message}",
                                status_code=response.status_code)


def read_run_events(gateway: Any, run_id: str, *, after_seq: int, through_seq: Optional[int],
                    timeout_sec: float, max_bytes: int = MAX_READ_BYTES) -> RunEventsRead:
    """Read the run's journal after ``after_seq``, up to ``through_seq`` when given.

    A refusal or a transport failure before the stream opened raises the gateway's typed
    ``ClaudexorUnavailable``. Once the stream is open, running out of time or bytes is not
    a failure: the read returns what it holds with ``stop`` naming the bound.
    Zero allowance opens nothing; a positive one sets HTTPX phase timeouts and a deadline
    checked between lines. A recv already in flight can finish past that deadline.
    """
    result = RunEventsRead(through_seq=int(after_seq))
    bound = float(timeout_sec)
    if bound <= 0:
        return result
    deadline = time.monotonic() + bound
    opened = False
    try:
        with gateway.open_run_events(run_id, int(after_seq), bound) as response:
            if response.status_code >= 400:
                raise _refusal(response)
            opened = True
            frame: Dict[str, str] = {}
            for line in response.iter_lines():
                result.bytes_read += len(line) + 1
                if line:
                    if not line.startswith(":"):
                        name, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        frame[name] = frame[name] + "\n" + value if name == "data" and name in frame else value
                elif frame:
                    if _dispatch(frame, result, through_seq):
                        return result
                    frame = {}
                if result.bytes_read > max_bytes:
                    result.stop = "bytes"
                    return result
                if time.monotonic() >= deadline:
                    return result
    except httpx.HTTPError as exc:
        if opened:
            return result  # an open stream that went quiet or broke: what was read stands
        raise ClaudexorUnavailable(
            "daemon_unreachable", f"Claudexor run event stream unreachable: {type(exc).__name__}: {exc}",
        ) from exc
    return result


def _dispatch(frame: Dict[str, str], result: RunEventsRead, through_seq: Optional[int]) -> bool:
    """Take one whole SSE frame; answer whether the read is complete."""
    if frame.get("event") == "end":
        result.ended, result.stop = True, "end"
        return True
    raw_id = frame.get("id", "")
    if not raw_id.isdigit() or "data" not in frame:
        return False
    seq = int(raw_id)
    if seq <= result.through_seq:
        return False
    try:
        value = json.loads(frame["data"])
    except ValueError:
        value = None
    result.events.append(RunEvent(seq=seq, type=frame.get("event", ""), data=frame["data"],
                                  value=value if isinstance(value, dict) else None))
    result.through_seq = seq
    if through_seq is not None and seq >= int(through_seq):
        result.stop = "fence"
        return True
    return False
