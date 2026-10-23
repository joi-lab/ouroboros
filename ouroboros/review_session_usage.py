"""UsageScope-to-custody attribution for delegated review sessions."""

from __future__ import annotations

import copy
from typing import Any, Dict, Tuple

from ouroboros._usage_rows import REVIEW_ATTRIBUTION_KEYS
from ouroboros._usage_response import _normalized_input_token_usage
from ouroboros.event_usage import event_token_counts


def session_usage_facts(run_id: str, *, requested_route: str = "",
                        observed_attempt: Any = None, summary: Any = None) -> Dict[str, Any]:
    """A reviewer session's own usage identity for its ``llm_usage`` row (#1578).

    The ``claudexor`` receipt keeps the run, the requested route, the final
    attempt's delegated route and that observed attempt separately: a request
    echo or an earlier attempt is never an observation. Token counters are the
    run summary's disclosed native counts (the ledger's ``subscription_session``
    sources), null when the harness disclosed none; the light verdict-extraction
    call reports its own usage, never these.
    """
    observed = copy.deepcopy(observed_attempt) if isinstance(observed_attempt, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    delegated_route = str(observed.get("harness_id") or "")
    receipt = {"run_id": str(run_id or ""), "requested_route": str(requested_route or ""),
               "delegated_route": delegated_route, "observed_attempt": copy.deepcopy(observed)}
    split = _normalized_input_token_usage(summary.get("inputTokenUsage"))
    if split is not None:
        receipt["usage_scope"] = "run"  # totals span attempts, not just observed_attempt
    return {
        "provider": "claudexor", "resolved_model": str(observed.get("model") or ""),
        "delegated_run_started": bool(run_id), "delegated_run_id": str(run_id or ""),
        "requested_route": receipt["requested_route"], "delegated_route": delegated_route,
        "observed_attempt": observed, "claudexor": receipt,
        **({"input_token_usage": split} if split is not None else {}),
        **event_token_counts({"prompt_tokens": summary.get("inputTokens"),
                              "completion_tokens": summary.get("outputTokens"),
                              "cached_tokens": summary.get("cachedInputTokens")}),
    }


def session_invocation_fields(invocation: Any) -> Tuple[Any, ...]:
    """Unpack the session policy fields without reinterpreting them."""
    return (
        invocation.task_id,
        invocation.surface,
        invocation.slot_id,
        invocation.timeout_sec,
        invocation.logical_key_extra,
        invocation.output_schema,
        invocation.session_route,
        invocation.instructions,
        invocation.retry_state,
    )


def session_custody_attribution(scope: Any) -> Tuple[str, str, Dict[str, str]]:
    """Project task lineage and every landed review-attribution key verbatim."""
    root_task_id = str(getattr(scope, "root_task_id", "") or "")
    parent_task_id = str(getattr(scope, "parent_task_id", "") or "")
    usage_custody = {
        "category": str(getattr(scope, "category", "") or "subagent"),
        "source": str(getattr(scope, "source", "") or "delegated_subagent"),
        **{
            key: str(getattr(scope, key, "") or "")
            for key in REVIEW_ATTRIBUTION_KEYS
        },
    }
    return root_task_id, parent_task_id, usage_custody


def session_failure_identity(usage: Dict[str, Any]) -> Dict[str, Any]:
    """The started-run identity a failed reviewer's custody keeps (route, attempt, receipt)."""
    return {key: usage[key] for key in ("provider", "requested_route", "delegated_route", "observed_attempt",
                                        "claudexor", "review_source_delivery") if key in usage}


def review_event_identity(request: Any) -> Dict[str, str]:
    """Known task/root/parent/wave identity of a review event (#1578 B1).

    The request's own task and resolved wave, the bound usage scope's lineage;
    nothing is looked up from history, and an unknown id stays absent.
    """
    from ouroboros.usage_accounting import current_usage_scope

    scope = current_usage_scope()
    identity = {"task_id": str(getattr(request, "task_id", "") or getattr(scope, "task_id", "") or ""),
                **{key: str(getattr(scope, key, "") or "") for key in ("root_task_id", "parent_task_id")},
                "review_wave_id": str(getattr(request, "resolved_wave_id", "")
                                      or getattr(scope, "review_wave_id", "") or "")}
    return {key: value for key, value in identity.items() if value}
