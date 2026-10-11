"""Validate Claudexor's additive effort evidence without resolving any route."""
from __future__ import annotations

import copy
from typing import Any


def model_effort_usage(result: dict, request_facts: Any) -> dict:
    """Decode only this result's reports, retaining the exact attempt's intent.

    Live calls and late receipts share this decoder. An older observation on a
    retained candidate is never evidence about the result being settled now.
    """
    effort = copy.deepcopy(request_facts) if isinstance(request_facts, dict) else {}
    effort.update(reported=None, report_source=None)
    usage = {"effort": effort}
    resolution = validated_effort_resolution(result.get("effortResolution"))
    if "effortResolution" in result:
        usage["effort_resolution"] = resolution
    applied = result.get("appliedOptions")
    reported = applied.get("reasoningEffort") if isinstance(applied, dict) else None
    if isinstance(reported, str) and reported.strip():
        effort.update(reported=reported, report_source="provider_applied_options")
    if resolution and resolution["observed"] is not None:
        effort.update(reported=resolution["observed"], report_source=resolution["observedSource"])
    return usage


def validated_effort_resolution(value: Any) -> dict | None:
    """Prepared native options are not dispatch proof or provider observations.

    Unknown/malformed reports stay unknown. Claudexor owns native vocabulary
    and ordering; validate the evidence shape without re-resolving its claim.
    """
    if not isinstance(value, dict):
        return None
    nullable = ("requested", "submitted", "parameter", "observed", "observedSource")
    if any(key not in value or (value[key] is not None and
           (not isinstance(value[key], str) or not value[key].strip())) for key in nullable):
        return None
    resolution = value.get("resolution")
    if not isinstance(resolution, str) or resolution not in {"exact", "downward", "floor", "omitted", "unverifiable", "rejected"}:
        return None
    source = value.get("source")
    if not isinstance(source, str) or source not in {"account_catalog", "live_probe", "versioned_snapshot", "adapter"}:
        return None
    if "reason" in value and not isinstance(value["reason"], str):
        return None
    requested, submitted = value["requested"], value["submitted"]
    # An omitted option may still name its known native parameter.
    if submitted is not None and value["parameter"] is None:
        return None
    if (value["observed"] is None) != (value["observedSource"] is None):
        return None
    if resolution == "exact":
        if requested is None or submitted != requested:
            return None
    elif resolution in {"downward", "floor"}:
        if requested is None or submitted is None:
            return None
    elif submitted is not None:
        return None
    if resolution == "rejected" and value["observed"] is not None:
        return None
    return copy.deepcopy(value)
