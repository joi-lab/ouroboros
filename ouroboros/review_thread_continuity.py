"""Thin Claudexor v3 thread operations for delegated plan reviewers."""

from __future__ import annotations

import json
from typing import Any, Dict


def ensure_review_thread(
    gateway: Any, custody: Any, thread_id: str, *, route: Any, root: str,
    surface: str, slot_id: str, task_id: str,
) -> str:
    """Create the initial sticky read-only thread; otherwise retain its id."""
    if thread_id:
        return thread_id
    key = custody.idempotency_key(
        "review_thread", surface, slot_id, task_id, route.route_id,
        str(getattr(route, "profile_id", "") or ""), root,
    )
    request: Dict[str, Any] = {
        "title": f"Ouroboros {surface} · {slot_id}",
        "scope": {"kind": "project", "root": root},
        "mode": "ask", "workspace": "in_place", "authPreference": "subscription",
        "primaryHarness": route.route_id, "eligibleHarnesses": [route.route_id],
        "access": "readonly",
    }
    profile_id = str(getattr(route, "profile_id", "") or "")
    if profile_id:
        request["credentialProfileId"] = profile_id
    return str(gateway.create_thread(request, idempotency_key=key).get("id") or "")


def start_review_thread_turn(
    gateway: Any, thread_id: str, run_request: Dict[str, Any], *, idempotency_key: str,
) -> Dict[str, Any]:
    """Strip run-only fields and append through the public thread pipeline."""
    request = {
        key: value for key, value in run_request.items()
        if key not in {"_use_thread", "_thread_id", "scope", "execution"}
    }
    return gateway.start_thread_turn(thread_id, request, idempotency_key=idempotency_key)


def profile_rotation_receipts(gateway: Any, run_id: str) -> list[dict]:
    """Read the engine's typed profile-rotation events for one settled run."""
    try:
        raw = gateway.get_run_artifact(run_id, "events.jsonl").decode("utf-8")
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return []
    receipts = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict) or row.get("type") != "route.profile.rotated":
            continue
        payload = (
            row.get("payload") if isinstance(row.get("payload"), dict)
            else row.get("data") if isinstance(row.get("data"), dict)
            else row
        )
        receipts.append({
            "seq": row.get("seq"),
            "type": "route.profile.rotated",
            "from_profile_id": payload.get("from_profile_id"),
            "to_profile_id": payload.get("to_profile_id"),
            "reason": str(payload.get("reason") or ""),
            "attempt_id": str(payload.get("attempt_id") or ""),
            "resets_at": str(payload.get("resets_at") or ""),
        })
    return receipts


def profile_continuity_receipt(
    expected_profile: str, applied_profile: str, rotations: list[dict],
) -> dict:
    """Bind profiles through one complete ordered engine-owned rotation chain."""
    expected, applied = str(expected_profile or ""), str(applied_profile or "")
    chain = [dict(row) for row in rotations if isinstance(row, dict)]

    def result(status: str, reason: str, receipt: dict | None = None) -> dict:
        return {
            "expected_profile": expected,
            "applied_profile": applied,
            "status": status,
            "verification_reason": reason,
            "rotation_receipt": dict(receipt or {}),
            "rotation_receipts": chain,
        }

    if not rotations:
        if not expected:
            # An expectation that was never recorded is not drift: unpinned slots
            # legitimately let the engine choose the account. The receipt stays
            # telemetry either way — it never gates parsing, counting, or PASS.
            return result("matched", "no_expectation_recorded")
        if expected == applied:
            return result("matched", "profile_matched")
        return result("cannot_verify", "unexplained_profile_drift")
    if len(chain) != len(rotations):
        return result("cannot_verify", "rotation_event_malformed")
    # No pinned expectation: a typed engine rotation on an unpinned slot walks
    # from the chain's own first hop instead of comparing against "".
    current, previous_seq = expected or str(chain[0].get("from_profile_id") or ""), None
    for row in chain:
        seq = row.get("seq")
        source = str(row.get("from_profile_id") or "")
        target = str(row.get("to_profile_id") or "")
        if (row.get("type") != "route.profile.rotated"
                or not isinstance(seq, int) or isinstance(seq, bool)
                or not target or target == source or not str(row.get("reason") or "")):
            return result("cannot_verify", "rotation_event_malformed")
        if previous_seq is not None and seq <= previous_seq:
            return result("cannot_verify", "rotation_event_order_invalid")
        if source != current:
            return result("cannot_verify", "rotation_chain_gap")
        current, previous_seq = target, seq
    if current != applied:
        return result("cannot_verify", "rotation_terminal_mismatch")
    return result("typed_rotation", "typed_rotation_chain", chain[-1])


def review_thread_receipt(
    gateway: Any, thread_id: str, run_id: str, turn_id: str, *,
    expected_profile: str = "", applied_profile: str = "",
) -> dict:
    """Require the completed run's exact turn/session/continuity binding."""
    detail = gateway.get_thread(thread_id)
    turns = [row for row in (detail.get("turns") or []) if isinstance(row, dict)]
    turn = next(
        (row for row in reversed(turns)
         if (
             str(row.get("runId") or "") == run_id
             and (not turn_id or str(row.get("id") or "") == turn_id)
         )),
        None,
    )
    if turn is None:
        from ouroboros.review_execution import ReviewRouteUnavailable

        raise ReviewRouteUnavailable(
            f"Claudexor thread {thread_id} does not bind completed run {run_id}",
            code="review_thread_receipt_missing",
        )
    turn_id = str(turn.get("id") or turn_id)
    return {
        "thread_id": thread_id, "turn_id": turn_id, "run_id": run_id,
        "head_run_id": str((detail.get("thread") or {}).get("headRunId") or ""),
        "continuity": turn.get("continuity") or {},
        "profile_continuity": profile_continuity_receipt(
            expected_profile, applied_profile, profile_rotation_receipts(gateway, run_id),
        ),
        "sessions": [
            {
                "id": str(row.get("id") or ""),
                "harness_id": str(row.get("harnessId") or ""),
                "profile_id": str(row.get("profileId") or ""),
                "state": str(row.get("state") or ""),
            }
            for row in (detail.get("sessions") or []) if isinstance(row, dict)
        ],
    }
