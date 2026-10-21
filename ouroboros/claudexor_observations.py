"""One passive quota read contract for Accounts, actor inspection and admission.

The engine owns applicability; a selector sent is not a selector honored. Old
engines' observations remain readable without authorizing exact availability.
"""
from __future__ import annotations

from typing import Any

QUOTA_VIEW = "resources_freshness"


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


def read_quota(gateway: Any, *, model: str | None = None,
               request_kind: str = "text", require_resources: bool = False) -> dict[str, Any]:
    """No discovery/refresh. Accounts may require its already-advertised resources.

    A definite 400 permits a legacy view: Accounts requires resources, passive
    readers try view-only constraint freshness, then plain after a second 400.
    Legacy windows remain readable without implying honored model/kind filters.
    Accounts' existing catalog also permits resources after an ignored selector.
    Return that whole epoch, never combine reads or retry an unknown failure.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    if request_kind not in ("text", "image"):
        raise ValueError("request_kind must be text or image")
    try:
        value = gateway.quota_state(view=QUOTA_VIEW, model=model, request_kind=request_kind)
    except ClaudexorUnavailable as exc:
        if exc.status_code != 400:
            raise
        if require_resources:
            value = gateway.quota_state(view="resources")
        else:
            try:
                value = gateway.quota_state(view="constraint_freshness")
            except ClaudexorUnavailable as legacy_exc:
                if legacy_exc.status_code != 400:
                    raise
                value = gateway.quota_state()
    else:
        if (require_resources and quota_envelope_valid(value)
                and "resources" not in value and "availability_scope" not in value):
            value = gateway.quota_state(view="resources")
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
