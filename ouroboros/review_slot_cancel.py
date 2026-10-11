"""The hosted-review slot poller's cancel-honesty helpers.

Extracted from ``ouroboros/review_execution.py`` at the module-size gate — the
same split as ``delegate_interactions`` out of ``tools/delegate.py``, and one
coherent concern: when the slot poller must stop a delegated review run (parked
question or slot timeout), what did the cancel actually PROVE, and how is that
worded honestly? ``review_execution`` imports these names and remains the only
caller; the poll loop itself stays there.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class ReviewSessionSucceededResultUnavailable(RuntimeError):
    """The run SUCCEEDED and is settled, but its result detail is unreadable NOW.

    The cancel's verify read discovered the run's natural success terminal and
    ``cancel_and_verify`` settled it (BR2-1) — that state is KNOWN, so a re-read
    blip must never be reported as "the run may still be live", and the finished
    result must never be discarded. This typed failure says exactly that: the
    success is durable (custody holds the settled row with the terminal receipt)
    and the detail is retrievable later via ``delegate_wait`` / the run's own
    capture surfaces — only this moment's fetch failed.
    """

    def __init__(self, message: str, *, run_id: str):
        super().__init__(message)
        self.delegated_run_started = True
        self.delegated_run_id = str(run_id or "")


def _interaction_outlives_slot(timeout_at: Any, deadline_monotonic: float) -> bool:
    """Does the parked question have NO engine expiry inside this slot's budget?

    True (terminate the slot early, F18) when the row's ``timeout_at`` is
    absent, unparseable, or lands at/after the slot deadline — waiting would be
    the pre-F18 slot-long silent burn. False when the engine's own
    benign-decline timer PROVABLY fires first: the poller then keeps polling on
    the slot's OWN clock and the resumed session is judged normally (R2-2 — the
    recoverable case F18 regressed: slot budgets of 900s/560s routinely exceed
    typical shorter interaction timeouts). This is the slot's own clock, never
    an owner host-wait: the slot deadline stays the hard cap either way."""
    raw = str(timeout_at or "").strip()
    if not raw:
        return True
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    seconds_left = deadline_monotonic - time.monotonic()
    return expiry.timestamp() >= time.time() + seconds_left


def _slot_cancel_outcome(gateway: Any, custody: Any, custody_drive: Any, entry: Any,
                         run_id: str, reason: str) -> tuple:
    """Cancel a slot's run through the verified path; report what it PROVED.

    Returns ``(outcome, state, terminal_detail)`` in delegate_custody's typed
    cancel vocabulary; a raising cancel/verify — or a shapeless result — is an
    UNVERIFIED attempt reported as its own marker, never swallowed into a
    "host-cancelled" claim. The state rides along so the caller can consume a
    natural terminal the verify read discovered (completion wins, BR1-1), and
    ``terminal_detail`` is that read's own detail when the cancel carried it
    (BR2-1: the discovered success must not depend on a second fetch)."""
    try:
        result = custody.cancel_and_verify(custody_drive, gateway, entry, reason)
    except Exception:
        log.warning("cancel/verify raised for review session %s (%s)",
                    run_id, reason, exc_info=True)
        return "cancel_attempt_exception", "", None
    if not isinstance(result, dict):
        return "cancel_attempt_exception", "", None
    detail = result.get("terminal_detail")
    return (str(result.get("outcome") or ""), str(result.get("state") or ""),
            detail if isinstance(detail, dict) else None)


def _cancel_honesty_clause(outcome: str, state: str = "") -> str:
    """"host-cancelled" ONLY on the verified ``confirmed`` receipt; every other
    outcome (requested/failed/containment fault/exception) says the cancel was
    requested but unverified and the run MAY STILL BE LIVE. A ``confirmed``
    whose verified state is the run's OWN non-success terminal (failed /
    interrupted) is attributed to the run, not to the host's cancel (BR2-2);
    a confirmed with state ''/'settled'/'absent' keeps the verified-receipt
    wording — there the receipt is the cancel itself, no natural terminal is
    claimed."""
    from ouroboros.delegate_custody import (
        CANCEL_CONFIRMED,
        SUCCEEDED_STATES,
        TERMINAL_STATES,
    )

    if outcome == CANCEL_CONFIRMED:
        if state in TERMINAL_STATES - SUCCEEDED_STATES - {"cancelled"}:
            return (f"the run had already reached its own terminal state "
                    f"{state!r} (settled with a verified receipt), not an "
                    "effect of the host's cancel")
        return "host-cancelled with a verified terminal receipt, not an engine decline"
    return (f"cancel requested but UNVERIFIED — outcome "
            f"{outcome or 'unknown'!r}; the run may still be live")


def _natural_success_terminal(gateway: Any, custody: Any, run_id: str, state: str,
                              carried: Optional[Dict[str, Any]] = None,
                              ) -> Optional[Dict[str, Any]]:
    """COMPLETION WINS: the cancel/verify read can discover the run already
    reached its natural SUCCESS terminal (settled ``state=succeeded``) — that
    terminal is the slot's ordinary result, never raised over, and never LOST
    to a transport blip (BR2-1): the detail the cancel already read is used
    as-is when carried; otherwise the re-read gets one bounded retry, and a
    fetch that still fails raises the typed settled-succeeded failure instead
    of falling through to the "may still be live" clause — the state is
    known."""
    from ouroboros.delegate_custody import SUCCEEDED_STATES

    if state not in SUCCEEDED_STATES:
        return None
    if carried is not None and custody.is_terminal(carried):
        return carried
    detail: Any = None
    for attempt in (1, 2):
        try:
            detail = gateway.get_run(run_id)
            break
        except Exception:
            log.warning("re-read %d of settled-succeeded review run %s failed",
                        attempt, run_id, exc_info=True)
    if isinstance(detail, dict) and custody.is_terminal(detail):
        return detail
    raise ReviewSessionSucceededResultUnavailable(
        f"delegated review session {run_id} SUCCEEDED — the cancel's verify read "
        "found its natural success terminal and the run is settled — but the "
        "settled result detail could not be retrieved at this moment. The result "
        "is NOT lost: custody holds the settled row with the terminal receipt, "
        "and the run's output stays readable via delegate_wait / the run's own "
        "capture surfaces.",
        run_id=run_id,
    )
