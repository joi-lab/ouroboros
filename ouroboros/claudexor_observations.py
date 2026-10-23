"""One passive quota read contract for Accounts, actor inspection, admission and the primary observation.

The engine owns applicability; a selector sent is not a selector honored. Old
engines' observations remain readable without authorizing exact availability.
"""
from __future__ import annotations

import time
from typing import Any

QUOTA_VIEW = "resources_freshness"
# Never project arbitrary engine error codes or exception prose: either can
# contain paths, credential material or an unbounded upstream response body.
_SAFE_ERROR_CODES = frozenset({
    "daemon_not_discovered", "daemon_descriptor_unreadable",
    "daemon_descriptor_incomplete", "daemon_token_unreadable",
    "daemon_endpoint_not_loopback", "daemon_unreachable",
    "malformed_response", "protocol_incompatible", "daemon_recovery_only",
})


def passive_read_error(exc: Exception) -> dict[str, Any]:
    """A failed passive read as a safe code and optional HTTP 400-599 status, never prose."""
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    if not isinstance(exc, ClaudexorUnavailable):
        return {"code": "read_failed"}
    code = exc.code if exc.code in _SAFE_ERROR_CODES else "read_refused"
    if exc.observation_reason == "observation_read_timeout":
        code = "observation_read_timeout"
    result: dict[str, Any] = {"code": code}
    if 400 <= exc.status_code <= 599:
        result["status_code"] = exc.status_code
    return result


def quota_envelope_valid(value: Any) -> bool:
    """Reject partial lists/freshness, preserving legacy envelopes and new fields."""
    if not isinstance(value, dict) or not isinstance(value.get("snapshots"), list):
        return False
    for key in ("snapshots", "absences", "resources"):
        if key in value and (not isinstance(value[key], list)
                             or not all(isinstance(row, dict) for row in value[key])):
            return False
    groups = [snapshot.get("constraints", []) for snapshot in value["snapshots"]]
    constraints = [row for group in groups if isinstance(group, list) for row in group]
    if any(isinstance(row, dict) and "freshness" in row for row in constraints):
        if not all(isinstance(group, list) for group in groups) or not all(
            isinstance(row, dict) and row.get("freshness") in ("fresh", "stale", "unknown")
            for row in constraints
        ):
            return False
    return True


def read_quota(gateway: Any, *, model: str | None = None, request_kind: str = "text",
               require_resources: bool = False, timeout_sec: float | None = None) -> dict[str, Any]:
    """No discovery/refresh. Accounts may require its already-advertised resources.

    A definite 400 permits a legacy view: Accounts requires resources, passive
    readers try view-only constraint freshness, then plain after a second 400.
    Legacy windows remain readable without implying honored model/kind filters.
    Accounts' existing catalog also permits resources after an ignored selector.
    Return that whole epoch, never combine reads or retry an unknown failure.
    ``timeout_sec`` is one budget for every view asked; a spent one raises TimeoutError.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    if request_kind not in ("text", "image"):
        raise ValueError("request_kind must be text or image")
    started = time.monotonic()

    def quota_state(**selectors: Any) -> Any:
        if timeout_sec is None:
            return gateway.quota_state(**selectors)
        remaining = timeout_sec - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Quota read deadline elapsed")
        return gateway.quota_state(**selectors, timeout_sec=remaining)

    try:
        value = quota_state(view=QUOTA_VIEW, model=model, request_kind=request_kind)
    except ClaudexorUnavailable as exc:
        if exc.status_code != 400:
            raise
        if require_resources:
            value = quota_state(view="resources")
        else:
            try:
                value = quota_state(view="constraint_freshness")
            except ClaudexorUnavailable as legacy_exc:
                if legacy_exc.status_code != 400:
                    raise
                value = quota_state()
    else:
        if (require_resources and quota_envelope_valid(value)
                and "resources" not in value and "availability_scope" not in value):
            value = quota_state(view="resources")
    if not quota_envelope_valid(value) or (require_resources and "resources" not in value):
        raise ClaudexorUnavailable("malformed_response", "Quota envelope is malformed")
    return value


def scope_honored(value: dict, model: str | None, request_kind: str = "text") -> bool:
    scope = value.get("availability_scope")
    return (isinstance(scope, dict) and "model" in scope and scope["model"] == model
            and scope.get("requestKind") == request_kind and scope.get("basis") == "quota_block")


def default_subject_aliases(roster: Any, harness: str, profile: str) -> tuple:
    """Frozen unified default id may inherit null only after a positive roster read."""
    if (not isinstance(roster, dict) or not isinstance(roster.get("accountPools"), list)
            or not isinstance(roster.get("profiles"), list)
            or profile != f"{harness}-default"):
        return ()
    return (None, "") if any(isinstance(row, dict) and isinstance(row.get("profile"), dict)
        and row["profile"].get("harness_id") == harness and row["profile"].get("profile_id") == profile
        for row in roster.get("profiles", [])) else ()


def subject_rows(rows: Any, harness: str, profile: str = "", *, aliases: tuple = ()) -> list[dict]:
    """A pin matches only its literal engine subject, never a healthy sibling."""
    relevant = [row for row in rows if isinstance(row, dict)
            and isinstance(row.get("subject"), dict)
            and row["subject"].get("harness") == harness] if isinstance(rows, list) else []
    exact = [row for row in relevant if not profile or row["subject"].get("subject_id") == profile]
    return exact or [row for row in relevant if row["subject"].get("subject_id") in aliases]


def resource_rows(rows: Any, harness: str, profile: str = "") -> list[dict]:
    """AccountResourceSnapshot uses AccountTarget, not a nullable quota subject.

    Resource targets always name a profile: quota native-null aliases cannot
    widen this independently typed identity or borrow another account's money.
    """
    return [row for row in rows if isinstance(row, dict)
            and isinstance(row.get("target"), dict)
            and row["target"].get("harness") == harness
            and isinstance(row["target"].get("profile_id"), str) and row["target"]["profile_id"]
            and (not profile or row["target"]["profile_id"] == profile)] if isinstance(rows, list) else []


def quota_availability(value: dict, harness: str, *, model: str | None = None,
                       profile: str = "", request_kind: str = "text", aliases: tuple = ()) -> dict:
    """Project engine verdicts, never infer applicability from model spellings.

    Unknowns fail open at admission, not as a claim of execution readiness.
    A stale sibling or explicit absence cannot prove an exhausted whole pool.
    Monetary resources are deliberately not consulted.
    """
    unknown = {"state": "unknown", "resets_at": None}
    if not scope_honored(value, model, request_kind):
        return {**unknown, "reason": "availability_scope_unhonored"}
    snapshots = subject_rows(value.get("snapshots"), harness, profile, aliases=aliases)
    if not snapshots or subject_rows(value.get("absences"), harness, profile, aliases=aliases):
        return {**unknown, "reason": "quota_evidence_missing"}
    verdicts = []
    for row in snapshots:
        availability = row.get("availability")
        # A read-time engine availability uses per-constraint freshness. The
        # aggregate can be stale because an unrelated sibling window expired.
        constraints = row.get("constraints", [])
        fresh = (isinstance(constraints, list) and any(
            isinstance(c, dict) and c.get("freshness") == "fresh" for c in constraints
        )) or row.get("freshness") == "fresh"
        if not fresh or not isinstance(availability, dict) or availability.get("state") not in (
            "available", "exhausted", "cooldown",
        ):
            return {**unknown, "reason": "quota_evidence_stale_or_incomplete"}
        verdicts.append(availability)
    if any(v["state"] == "available" for v in verdicts):
        return {"state": "available", "resets_at": None, "execution_guaranteed": False}
    resets = [v.get("resets_at") for v in verdicts if isinstance(v.get("resets_at"), str)]
    return {"state": "exhausted" if any(v["state"] == "exhausted" for v in verdicts) else "cooldown",
            "resets_at": min(resets) if resets else None, "execution_guaranteed": False}


def _dated_row(row: dict, *, snapshot: bool) -> dict:
    """One engine row's subject, source and dates; no labels, ratios, money or detail prose."""
    fact = {"subject_id": row["subject"].get("subject_id"), "observed_at": row.get("observed_at")}
    if not snapshot:
        return {**fact, "reason": row.get("reason")}
    availability = row.get("availability") if isinstance(row.get("availability"), dict) else {}
    constraints = row.get("constraints")
    windows = [c for c in constraints if isinstance(c, dict)] if isinstance(constraints, list) else []
    return {**fact, "source": row.get("source"), "freshness": row.get("freshness"),
            "fresh_windows": sum(c.get("freshness") == "fresh" for c in windows), "windows": len(windows),
            "state": availability.get("state"), "resets_at": availability.get("resets_at")}


def quota_observation(harness: str, model: str | None, profile: str = "", *, timeout_sec: float,
                      request_kind: str = "text") -> dict[str, Any]:
    """Quota-only facts for one exact binding: the shared verdict and every scoped row, compact and dated.

    One deadline covers the metadata connection, a default pin's roster proof and every
    legacy view. A failed read, an unread envelope and an empty one stay distinct; an
    unreadable roster only withholds the alias. Nothing refreshes, starts, selects or reads
    money, and roster contents never leave here. Auto sees every subject of the harness.
    """
    from ouroboros.claudexor_daemon import read_owned_gateway
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    started = time.monotonic()

    def left() -> float:
        remaining = timeout_sec - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Quota observation deadline elapsed")
        return remaining

    fact: dict[str, Any] = {"requested_scope": {"model": model, "requestKind": request_kind,
                                                "account": profile or "Auto"}}
    aliases: tuple = ()
    try:
        gateway = read_owned_gateway(timeout_sec=left())
        try:
            if profile == f"{harness}-default":
                try:
                    aliases = default_subject_aliases(gateway.credential_profiles(timeout_sec=left()), harness, profile)
                except ClaudexorUnavailable as exc:
                    fact["roster_error"] = passive_read_error(exc)
            value = read_quota(gateway, model=model, request_kind=request_kind, timeout_sec=left())
        finally:
            gateway.close()
    except TimeoutError:
        return {**fact, "read": "not_read", "reason": "observation_deadline_elapsed"}
    except Exception as exc:
        return {**fact, "read": "failed", "error": passive_read_error(exc)}
    snapshots = subject_rows(value.get("snapshots"), harness, profile, aliases=aliases)
    absences = subject_rows(value.get("absences"), harness, profile, aliases=aliases)
    verdict = quota_availability(value, harness, model=model, profile=profile,
                                 request_kind=request_kind, aliases=aliases)
    fact.update(read="ok", scope_honored=scope_honored(value, model, request_kind),
                subjects=("every engine subject of the harness, enabled or not" if not profile
                          else "the pin and its proven default alias" if aliases else "the exact pin"),
                availability={**verdict, "scope": "quota_only"},
                snapshots=[_dated_row(row, snapshot=True) for row in snapshots],
                absences=[_dated_row(row, snapshot=False) for row in absences])
    return fact
