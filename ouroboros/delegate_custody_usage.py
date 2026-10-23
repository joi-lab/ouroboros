"""Pure usage and terminal-state projections for delegated-run custody."""

from __future__ import annotations

import hashlib
import logging
import pathlib
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)


@contextmanager
def project_retirement_lock(drive_root: Any, project_id: str):
    """Cross-process lock for one project's settlement/retirement decision."""
    from ouroboros.platform_layer import (
        acquire_exclusive_file_lock, release_exclusive_file_lock,
    )

    digest = hashlib.sha256(str(project_id or "").encode("utf-8")).hexdigest()[:24]
    lock_path = pathlib.Path(drive_root) / "state" / "delegate_project_retirements" / f"{digest}.lock"
    fd = acquire_exclusive_file_lock(
        lock_path, timeout_sec=20.0, stale_sec=120.0, owner_aware_stale=True,
    )
    if fd is None:
        raise TimeoutError("project retirement lock is unavailable")
    try:
        yield
    finally:
        release_exclusive_file_lock(lock_path, fd)


def summary_of(detail: Dict[str, Any]) -> Dict[str, Any]:
    return detail.get("summary") if isinstance(detail.get("summary"), dict) else {}


def run_names(detail: Dict[str, Any]) -> frozenset[str]:
    """Both names the engine gives this detail's run: its run id and the job id backing it.

    A start answered before its run bound (HTTP 202) hands the host only the job id, and the
    engine addresses a run by either name (control-api ``findRun``), so a held run id is one
    of these exactly. Empty when the detail names no run.
    """
    summary = summary_of(detail)
    run = summary.get("runId") or summary.get("id") or detail.get("runId")
    return frozenset(str(name) for name in (run, summary.get("jobId")) if name)


def names_another_run(detail: Dict[str, Any], run_id: Any) -> bool:
    """The detail names a run, and ``run_id`` is neither of its names."""
    names = run_names(detail)
    return bool(names) and str(run_id or "") not in names


def held_run_names(detail: Dict[str, Any], run_id: Any) -> frozenset[str]:
    """The names the held run may carry in evidence beside this detail (telemetry, turns,
    continuity rows): ``run_id`` and, only when the detail is of that run, its two names."""
    held = str(run_id or "")
    return frozenset({held}) if names_another_run(detail, held) else frozenset({held, *run_names(detail)})


def disclosed_spend(summary: Dict[str, Any], *, attempt_execution: Any = None) -> Tuple[Optional[float], bool]:
    """The cash the harness reported AND whether it is settled — never one without both.

    ``spendUsd`` is only half the disclosure: the engine populates the sibling
    ``spendEstimated``. Reading the amount alone makes an estimate
    indistinguishable from settled cash, so callers receive the pair atomically.
    """
    if isinstance(attempt_execution, list) and attempt_execution:
        from ouroboros._usage_response import provider_cost_value

        cash, complete, exact = 0.0, True, True
        for attempt in attempt_execution:
            cost = attempt.get("usageCost") if isinstance(attempt, dict) else None
            cost = cost if isinstance(cost, dict) else {}
            amount = provider_cost_value(cost.get("cashUsd"))
            knowledge = cost.get("cashKnowledge")
            if amount is None or knowledge not in {"exact", "estimated"}:
                complete = exact = False
            else:
                cash += amount
                exact = exact and knowledge == "exact"
        # Keep a proven positive partial debit in the budget, with its missing
        # remainder disclosed by the original attempt evidence. Unknown zero is not free.
        return (cash, not exact) if complete or cash > 0 else (None, False)
    raw = summary.get("spendUsd")
    if raw is None:
        return None, False
    try:
        return float(raw), summary.get("spendEstimated") is True
    except (TypeError, ValueError):
        return None, False


def disclosed_tokens(raw: Any) -> Optional[int]:
    """A reported token count, or ``None`` when the harness reported nothing.

    The control schema keeps token counts null until a harness reports them;
    converting absence to zero would erase that distinction.
    """
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def is_terminal(detail: Dict[str, Any], terminal_states: frozenset[str]) -> bool:
    return str(summary_of(detail).get("state") or "") in terminal_states


# ---- reviewer usage observation (sessions and physical extraction sends stay separate) ----

_EMITTED_SESSION_USAGE: Dict[str, float] = {}  # delegated run id -> first llm_usage emission


def session_usage_once(run_id: str) -> bool:
    """One llm_usage row per delegated run, across executors (process-local)."""
    key = str(run_id or "")
    if key in _EMITTED_SESSION_USAGE:
        return False
    if key:
        _EMITTED_SESSION_USAGE.clear() if len(_EMITTED_SESSION_USAGE) >= 256 else None
        _EMITTED_SESSION_USAGE[key] = time.monotonic()
    return True


def observe_session_usage(observer: Any, usage: Dict[str, Any]) -> None:
    """A continued reviewer emits each run, never its chain total as the last run."""
    for part in usage.get("session_usage_events") or [usage]:
        if session_usage_once(part.get("delegated_run_id", "")):
            observe_review_usage(observer, part)


def observe_review_usage(observer: Any, usage: Optional[Dict[str, Any]]) -> None:
    """Hand one row per ledger attempt to the reviewer usage observer."""
    if observer is None:
        return
    row = dict(usage or {})
    attempt_ids = [str(value) for value in (row.get("ledger_attempt_ids") or []) if value]
    for attempt_id in attempt_ids[:-1]:
        observer({
            "resolved_model": row.get("resolved_model"), "provider": row.get("provider"),
            "ledger_attempt_ids": [attempt_id],
        })
    if attempt_ids:
        row["ledger_attempt_ids"] = attempt_ids[-1:]
    observer(row)


def observe_failed_review_send(observer: Any, exc: BaseException) -> None:
    """Rows for every physically dispatched attempt behind a failed reviewer send."""
    from ouroboros import usage_store
    from ouroboros.usage_accounting import POSITIVE_PHYSICAL_ATTEMPT_STATES, _drive_root, current_usage_scope

    capture = getattr(exc, "physical_attempt_capture", None)
    attempt_ids = [str(value) for value in (getattr(exc, "ledger_attempt_ids", None) or []) if value]
    capture_id = str(getattr(capture, "attempt_id", "") or "")
    if capture_id and capture_id not in attempt_ids:
        attempt_ids.append(capture_id)
    rows: Dict[str, Dict[str, Any]] = {}
    try:
        scope = current_usage_scope()
        root = _drive_root(getattr(scope, "drive_root", None))
        with usage_store.read(root) as txn:  # one PK lookup per attempt id
            found = {attempt_id: txn.attempt(attempt_id) for attempt_id in attempt_ids}
        rows = {attempt_id: row for attempt_id, row in found.items() if row is not None}
    except Exception:
        log.debug("failed to resolve review attempt states", exc_info=True)
    capture_state = str(getattr(capture, "state", "") or "")
    for attempt_id in attempt_ids:
        row = rows.get(attempt_id, {})
        state = str(row.get("state") or (capture_state if attempt_id == capture_id else ""))
        if state not in POSITIVE_PHYSICAL_ATTEMPT_STATES and not (
            not rows and attempt_id != capture_id
        ):
            continue
        observe_review_usage(observer, {
            "resolved_model": str(row.get("model") or getattr(capture, "model", "") or ""),
            "provider": str(row.get("provider") or getattr(capture, "provider", "") or ""),
            "ledger_attempt_ids": [attempt_id],
        })
