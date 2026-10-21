"""Route capability admission and engine-owned scoped quota observations.

The same reader serves dispatch and direct starts. Quota unknowns never refuse
admission; only the engine's start verdict proves that an attempt can run.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ouroboros.subagents import DelegatedRunShape


def route_health(
    gateway: Any, route_id: str, shape: DelegatedRunShape, *, route_model: str = "",
    pinned_profile: str = "",
) -> tuple[str, str]:
    """Check the requested access/marker shape, not aggregate doctor health.

    Doctor status concerns the default store, not the credential pool. An
    unpinned harness's explicit owner-disabled toggle still refuses; a strict
    profile pin keeps its historical exception. Execution-delegated marker
    support is an engine-version wire floor, not proof of OS containment.
    Engine quota blocks supply dated resets to existing caller guards; unknowns
    stay fail-open. A positive observation never guarantees a later start.
    """
    from ouroboros.config import CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION
    from ouroboros.gateways.claudexor import engine_at_least

    catalog = gateway.agent_capabilities()
    entry = next((row for row in catalog.get("harnesses") or []
                  if isinstance(row, dict) and str(row.get("id") or "") == route_id), None)
    if entry is None:
        return "route_not_in_capability_catalog", ""
    if not pinned_profile and entry.get("enabled") is False:
        return "route_disabled", ""
    supported = [str(v) for v in entry.get("accessProfilesSupported") or []]
    # Old engines translate delegated workspace_write to their confined full
    # adapter profile. Full itself must be advertised literally.
    if shape.access not in supported and not (
        shape.access == "workspace_write" and shape.delegated and "external_sandbox_full" in supported
    ):
        return f"access_profile_unsupported:{shape.access}", ""
    if shape.delegated and not engine_at_least(
        str(getattr(gateway, "engine_version", "") or ""), CLAUDEXOR_DELEGATED_MARKER_MIN_VERSION,
    ):
        return "engine_rejects_delegated_marker", ""
    _exhausted, reset_at = _exhausted_window(gateway, route_id, route_model, pinned_profile)
    return "", reset_at


def _exhausted_window(gateway: Any, route_id: str, route_model: str = "",
                      pinned_profile: str = "") -> tuple[bool, str]:
    """Read exact engine scope; no host aliases, prefix table or monetary admission.

    A legacy/unhonored selector supplies observations, not exact availability.
    A stale/missing subject stays unknown and fail-open; a healthy sibling cannot
    rescue a strict pin. Unsupported embedded gateways likewise prove nothing.
    """
    from ouroboros.claudexor_observations import default_subject_aliases, quota_availability, read_quota
    from ouroboros.gateways.claudexor import ClaudexorUnavailable

    if not callable(getattr(gateway, "quota_state", None)):
        return False, ""
    try:
        value = read_quota(gateway, model=route_model or None)
        aliases = ()
        if pinned_profile == f"{route_id}-default" and callable(getattr(gateway, "credential_profiles", None)):
            aliases = default_subject_aliases(gateway.credential_profiles(), route_id, pinned_profile)
    except ClaudexorUnavailable:
        return False, ""
    verdict = quota_availability(value, route_id, model=route_model or None, profile=pinned_profile, aliases=aliases)
    return verdict["state"] in ("exhausted", "cooldown"), verdict.get("resets_at") or ""
