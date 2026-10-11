"""Sanitised observation parsing for the CyberGym sidecar contracts.

This module owns the seam that reads facts out of sanitised runner
observations: Docker ``inspect``-shaped payload readers and the
connectivity-probe evaluation, together with the attestation schema
identifier and the sha256-digest pattern shared with the argv/spec
validators in ``cybergym_sidecar``.  It stays pure (stdlib only) so the
rules remain testable on CI hosts without Docker, and it must never
import from ``cybergym_sidecar`` (that module imports from here).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "ouroboros.benchmark.cybergym.sidecar_attestation.v1"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


_CONNECTIVITY_EXPECTATIONS = {
    "agent_to_server": True,
    "verifier_to_private": True,
    "agent_to_public": True,
    "agent_to_verifier": False,
    "agent_socket_visible": False,
}


def _probe_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        for key in ("reachable", "visible", "ok", "success", "passed"):
            if isinstance(value.get(key), bool):
                return value[key]
    return None


_PROTECTED_ROUTE_KEY = "agent_to_server_protected"
# A wrong HTTP method (405) reaches Starlette routing before FastAPI's private
# API-key dependency, so it is not evidence that the route is protected.  The
# production probe uses POST with an intentionally invalid JSON body and must
# observe an auth denial.  Keep this set narrow rather than treating every
# client error (notably 405/422) as proof of authorization enforcement.
_PROTECTED_DENIAL_STATUS_RANGE = {401, 403, 404}


def _mapping_value(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    """Read a small set of Docker/runner spellings without accepting aliases blindly."""

    for name in names:
        if name in mapping:
            return mapping[name]
    lowered = {name.lower() for name in names}
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() in lowered:
            return value
    return None


def _bool_mapping_value(mapping: Mapping[str, Any], names: Sequence[str]) -> bool | None:
    value = _mapping_value(mapping, names)
    return value if isinstance(value, bool) else None


def _http_status(mapping: Mapping[str, Any]) -> int | None:
    value = _mapping_value(mapping, ("status_code", "http_status", "response_status", "status"))
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def _protected_route_record(value: Any) -> dict[str, Any] | None:
    """Normalize one non-mutating protected-route probe result.

    A transport response is useful evidence only when the runner also records
    that the unauthenticated request was denied and did not mutate state.  A
    bare boolean is deliberately rejected, because it cannot distinguish a
    denied 401/405 from a successful mutating request.
    """

    if not isinstance(value, Mapping):
        return None
    status = _http_status(value)
    reachable = _bool_mapping_value(
        value,
        ("reachable", "transport_reachable", "transport_ok", "transport"),
    )
    if reachable is None and status is not None:
        reachable = True
    authorized = _bool_mapping_value(value, ("authorized", "allowed", "accepted"))
    denied = _bool_mapping_value(
        value,
        ("denied", "unauthorized", "protected", "expected_denial", "denied_by_auth"),
    )
    if denied is None and authorized is not None:
        denied = not authorized
    if denied is None and status is not None:
        denied = status in _PROTECTED_DENIAL_STATUS_RANGE
    mutating = _bool_mapping_value(
        value,
        ("mutating", "mutation", "mutation_succeeded", "mutation_success", "write_succeeded", "mutated"),
    )
    # An auth denial is an explicit non-mutating outcome for the malformed POST
    # probe.  A 2xx/5xx response still requires an explicit mutating=false fact.
    if mutating is None and denied is True and status in _PROTECTED_DENIAL_STATUS_RANGE:
        mutating = False
    return {
        "reachable": reachable,
        "denied": denied,
        "mutating": mutating,
        "status_code": status,
    }


def _protected_route_summary(value: Any) -> tuple[dict[str, Any], bool]:
    """Evaluate aggregate or per-target protected-route observations."""

    per_target = False
    raw_records: list[Any]
    aggregate_defaults: dict[str, Any] = {}
    if isinstance(value, Mapping):
        aggregate_defaults = {
            key: item
            for key, item in value.items()
            if key in {
                "reachable",
                "transport_reachable",
                "transport_ok",
                "transport",
                "denied",
                "unauthorized",
                "protected",
                "expected_denial",
                "denied_by_auth",
                "authorized",
                "allowed",
                "accepted",
                "mutating",
                "mutation",
                "mutation_succeeded",
                "mutation_success",
                "write_succeeded",
                "mutated",
                "status_code",
                "http_status",
                "response_status",
                "status",
            }
        }
        nested = _mapping_value(value, ("targets", "target_results", "routes"))
        if isinstance(nested, Mapping):
            raw_records = list(nested.values())
            per_target = True
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            if all(isinstance(item, Mapping) for item in nested):
                raw_records = list(nested)
            else:
                # Some runners report only the target names and put the
                # aggregate facts beside them.  Preserve the requirement that
                # both configured routes were probed while reusing those facts.
                raw_records = [dict(aggregate_defaults) for _ in nested]
            per_target = True
        elif value and all(isinstance(item, Mapping) for item in value.values()):
            # A URL-keyed mapping is a convenient runner representation.
            raw_records = list(value.values())
            per_target = True
        else:
            raw_records = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        raw_records = list(value)
        per_target = True
    else:
        raw_records = []
    records = [_protected_route_record(item) for item in raw_records]
    if per_target and aggregate_defaults:
        records = [
            _protected_route_record({**aggregate_defaults, **item}) if isinstance(item, Mapping) else record
            for item, record in zip(raw_records, records)
        ]
    declared_count = value.get("target_count") if isinstance(value, Mapping) else None
    declared_count_ok = declared_count is None or (
        isinstance(declared_count, int) and not isinstance(declared_count, bool) and declared_count == len(records)
    )
    target_count_ok = bool(records) and declared_count_ok and (not per_target or len(records) >= 2)
    pass_value = bool(
        target_count_ok
        and all(
            record is not None
            and record["reachable"] is True
            and record["denied"] is True
            and record["mutating"] is False
            and record["status_code"] in _PROTECTED_DENIAL_STATUS_RANGE
            for record in records
        )
    )
    summary = {
        "reachable": all(record is not None and record["reachable"] is True for record in records) if records else None,
        "denied": all(record is not None and record["denied"] is True for record in records) if records else None,
        "mutating": any(record is not None and record["mutating"] is True for record in records) if records else None,
        "target_count": len(records),
        "per_target": per_target,
        "pass": pass_value,
    }
    return summary, pass_value


def evaluate_connectivity_checks(
    observed: Mapping[str, Any],
    *,
    require_protected_route_evidence: bool = False,
) -> dict[str, Any]:
    """Evaluate route facts; optional production probes fail closed when present."""

    checks: dict[str, Any] = {}
    failed: list[str] = []
    if not isinstance(observed, Mapping):
        observed = {}
    for name, expected in _CONNECTIVITY_EXPECTATIONS.items():
        value = _probe_value(observed.get(name))
        passed = value is expected
        checks[name] = {"observed": value, "expected": expected, "pass": passed}
        if not passed:
            failed.append(name)
    if _PROTECTED_ROUTE_KEY in observed or require_protected_route_evidence:
        summary, passed = _protected_route_summary(observed.get(_PROTECTED_ROUTE_KEY))
        checks[_PROTECTED_ROUTE_KEY] = {
            "observed": summary,
            "expected": {"reachable": True, "denied": True, "mutating": False},
            "pass": passed,
        }
        if not passed:
            failed.append(_PROTECTED_ROUTE_KEY)
    return {"schema": f"{SCHEMA_VERSION}.connectivity", "ok": not failed, "checks": checks, "failed": failed}


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _name(observation: Mapping[str, Any]) -> str | None:
    value = observation.get("Name") or observation.get("name")
    return value.lstrip("/") if isinstance(value, str) and value.lstrip("/") else None


def _id(observation: Mapping[str, Any]) -> str | None:
    value = observation.get("Id") or observation.get("ID") or observation.get("id")
    return value if isinstance(value, str) and value else None


def _pid(observation: Mapping[str, Any]) -> int | None:
    value = _nested(observation, "State", "Pid")
    if value is None:
        value = observation.get("Pid") or observation.get("pid")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _running(observation: Mapping[str, Any]) -> bool:
    return _nested(observation, "State", "Running") is True or observation.get("running") is True


def _labels_from(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _nested(observation, "Config", "Labels")
    if not isinstance(value, Mapping):
        value = observation.get("Labels")
    return value if isinstance(value, Mapping) else {}


def _network_mode(observation: Mapping[str, Any]) -> str | None:
    value = _nested(observation, "HostConfig", "NetworkMode")
    if value is None:
        value = observation.get("NetworkMode") or observation.get("network_mode")
    return value if isinstance(value, str) else None


def _network(observation: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    values = _nested(observation, "NetworkSettings", "Networks")
    if not isinstance(values, Mapping):
        values = observation.get("Networks")
    value = values.get(name) if isinstance(values, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _mounts(observation: Mapping[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    values = observation.get("Mounts")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for item in values:
            if isinstance(item, Mapping):
                source = item.get("Source") or item.get("source")
                destination = item.get("Destination") or item.get("destination") or item.get("Target")
                if isinstance(source, str) and isinstance(destination, str):
                    result.append((source, destination))
    values = _nested(observation, "HostConfig", "Binds")
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for item in values:
            if isinstance(item, str) and ":" in item:
                source, destination = item.split(":", 1)[:2]
                result.append((source, destination))
    return result


def _bindings(observation: Mapping[str, Any], port: int) -> list[Mapping[str, Any]]:
    values = _nested(observation, "NetworkSettings", "Ports")
    if not isinstance(values, Mapping):
        values = observation.get("Ports")
    value = values.get(f"{port}/tcp") if isinstance(values, Mapping) else None
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []


def _digests(observation: Mapping[str, Any]) -> set[str]:
    values = _nested(observation, "Config", "RepoDigests")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        values = observation.get("RepoDigests")
    result: set[str] = set()
    for item in values or ():
        if isinstance(item, str) and "@" in item:
            result.add(item.split("@", 1)[1])
    image = observation.get("Image")
    if isinstance(image, str) and _DIGEST.fullmatch(image):
        result.add(image)
    config_image = _nested(observation, "Config", "Image")
    if isinstance(config_image, str) and _DIGEST.fullmatch(config_image):
        result.add(config_image)
    return result
