"""Retrieving scope reviewer for the commit pipeline.

Runs beside triad review and REACHES the whole body: every scope row delivers by
retrieval — an ``api_chat`` row as a bounded native inspection episode on its own
route, an ``agent_session`` row as a delegated read-only session. What the
reviewer is owed in full arrives as the change-relative required-source manifest
(``scope_required_sources``); observed coverage remains diagnostic evidence
beside the findings and never changes quorum eligibility. Reviewer window size
is not a condition of authority: it sizes the output reserve of the request
and nothing else. Critical findings follow the
selected enforcement outside Cyber; Cyber findings are advisory to action.
Failed rows retain their original status and typed origin. The commit aggregate
applies permission to technical failures independently of candidate, custody and
owner admission. Scope review applies in every context mode.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional

from ouroboros.config import runtime_setting
from ouroboros.llm import LLMClient
# review_repo_dirs_for stays importable and MONKEYPATCHABLE on this module:
# review_admission.prepare_scope_review resolves it through this namespace.
from ouroboros.review_substrate import review_repo_dirs_for, scope_reviewer_slots  # noqa: F401
from ouroboros.reviewer_window import window_scaled_reserves as _shared_window_scaled_reserves
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.scope_review_contract import (
    SCOPE_REQUIRED_ITEMS,
    build_scope_block_message as _build_block_message,
    classify_scope_findings as _classify_scope_findings,
    normalize_scope_items as _normalize_scope_items,
)
from ouroboros.tools.review_helpers import (
    CANONICAL_GOVERNANCE_DOCS,
    review_drive_root,
    review_enforcement_blocks,
    _ANTI_THRASHING_RULE_VERDICT,
    _CONVERGENCE_RULE_TEXT,
    _HISTORY_VERIFICATION_ONLY_RULE,
    build_review_history_section as _shared_review_history_section,
    format_review_history_entry,
)
from ouroboros.tools.scope_window import (
    SCOPE_MODEL_DEFAULT as _SCOPE_MODEL_DEFAULT,
    SCOPE_SIZING_FALLBACK_WINDOW as _SCOPE_SIZING_FALLBACK,
    scope_window as _scope_window,
)
from ouroboros.triad_review import REVIEW_JSON_MATRIX_CONTRACT, extract_json_array
from ouroboros.utils import (
    utc_now_iso,
    append_jsonl,
    estimate_tokens,
)

log = logging.getLogger(__name__)
_SCOPE_REQUIRED_ITEMS = SCOPE_REQUIRED_ITEMS  # compatibility export used by tests/review tooling

# The canonical corpus has ONE owner (``review_helpers``); the governance tiers
# a brief delivers are decided by ``governance_context``, and this alias keeps
# the historical spelling the external review tooling's inventory reads.
_CANONICAL_CONTEXT_DOCS = CANONICAL_GOVERNANCE_DOCS

# The brief's forensic coverage manifest for the row being assembled. A
# ContextVar because assembly and dispatch can run on different threads: the
# assembling half captures it, the dispatching half re-seeds it.
_SCOPE_CONTEXT_MANIFEST = contextvars.ContextVar("scope_context_manifest", default={})

_SCOPE_MAX_TOKENS = 100_000  # 100K output tokens

_SCOPE_REVIEW_SLOT_TIMEOUT_SEC = None

_SCOPE_OUTPUT_MARGIN_TOKENS = 155_000


def _current_scope_context_manifest() -> dict:
    return dict(_SCOPE_CONTEXT_MANIFEST.get({}) or {})


def _window_scaled_reserves(window: int) -> tuple:
    """(output_reserve, tokenizer_margin) scaled to the reviewer window.

    The absolute 1M-calibrated reserves (100K output + 155K margin) would
    swallow a small window whole (gigachat 131K => a request whose max_tokens
    alone exceeds the route — Provider Independence). Sub-floor windows scale
    the reserves to the window instead: a quarter for output (floored at 8K so
    the reviewer can still produce the full checklist JSON) and an eighth for
    tokenizer margin. >=1M windows keep the absolute reserves unchanged.
    """
    return _shared_window_scaled_reserves(
        window,
        output_reserve=_SCOPE_MAX_TOKENS,
        tokenizer_margin=_SCOPE_OUTPUT_MARGIN_TOKENS,
    )


def _get_scope_model() -> str:
    """Return the configured scope review model (env → settings default)."""
    try:
        from ouroboros.config import get_scope_review_models

        models = get_scope_review_models()
        if models:
            return models[0]
    except Exception:
        pass
    return runtime_setting("OUROBOROS_SCOPE_REVIEW_MODEL", "").strip() or _SCOPE_MODEL_DEFAULT


@dataclass
class ScopeReviewResult:
    """Structured outcome from ``run_scope_review``."""
    blocked: bool = False
    block_message: str = ""
    parsed_items: List[dict] = field(default_factory=list)
    critical_findings: List[dict] = field(default_factory=list)
    advisory_findings: List[dict] = field(default_factory=list)
    # Canonical per-actor evidence.
    raw_text: str = ""
    model_id: str = ""
    # responded|error|parse_failure|empty_response — a returned, conforming
    # verdict counts toward quorum independently of observed read coverage.
    status: str = "responded"
    # complete|declared_empty|unobserved|incomplete — how much of the required
    # -source manifest the host or harness recorded this row reading. These
    # observations never replace the reviewer's judgment or create a retry.
    coverage: str = "unobserved"
    coverage_manifest_ref: dict = field(default_factory=dict)
    failure_phase: str = ""
    failure_code: str = ""
    # The measured size of the brief this row sent (len of the first send's
    # work order); the actor record labels it `measured` for that reason.
    prompt_chars: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    context_manifest: dict = field(default_factory=dict)
    prompt_ref: dict = field(default_factory=dict)
    response_ref: dict = field(default_factory=dict)
    operation_id: str = ""
    operation_state: str = "settled"
    late_result_pending: bool = False
    pending_invocation_id: str = ""
    delegated_run_id: str = ""


def _build_review_history_section(history: list, open_obligations: list = None) -> str:
    """Format previous triad rounds for scope-review context."""
    return _shared_review_history_section(
        history,
        open_obligations,
        title="## Previous triad review rounds",
        include_commit_message=False,
        compact_labels=True,
    )


def _build_scope_history_section(
    scope_review_history: Optional[list], history_section: str = "",
) -> str:
    """Format prior scope review rounds into a brief section.

    A scope-only retry chain leaves the TRIAD history empty, so the shared
    anti-thrashing block never reaches its convergence rule; from the third
    scope round on it is appended here instead. ``history_section`` is the
    already-rendered triad history: when it carries the rule, this section does
    not repeat it.
    """
    if not scope_review_history:
        return ""
    rounds = []
    for i, entry in enumerate(scope_review_history, 1):
        status = str(entry.get("status") or "responded").strip()
        label = (
            "BLOCKED" if entry.get("blocked")
            else status.upper() if status and status != "responded"
            else "PASSED"
        )
        parts = [f"Round {i}: {label}"]
        critical_findings = list(entry.get("critical_findings") or [])
        advisory_findings = list(entry.get("advisory_findings") or [])
        if critical_findings:
            parts.append("Critical findings:")
            for finding in critical_findings:
                parts.append(f"- {format_review_history_entry(finding, default_severity='critical')}")
        if advisory_findings:
            parts.append("Advisory findings:")
            for finding in advisory_findings:
                parts.append(f"- {format_review_history_entry(finding)}")
        if not critical_findings and not advisory_findings:
            parts.append(str(entry.get("summary") or "(no summary)"))
        rounds.append("\n".join(parts))
    section = (
        "\n## Prior scope review rounds (your previous findings for this commit)\n\n"
        + "\n\n---\n".join(rounds)
        + "\n\nAddress any previously raised issues. If the same issue persists, "
        "mark it FAIL again with a reference to the prior round.\n"
        f"\nIMPORTANT: {_HISTORY_VERIFICATION_ONLY_RULE}\n"
        f"\nIMPORTANT: {_ANTI_THRASHING_RULE_VERDICT}\n"
    )
    if len(scope_review_history) >= 2 and _CONVERGENCE_RULE_TEXT not in str(history_section or ""):
        section = section.rstrip() + f"\n\n**IMPORTANT: {_CONVERGENCE_RULE_TEXT}**\n"
    return section


def _log_scope_result(
    ctx: ToolContext,
    critical_count: int,
    advisory_count: int,
    prompt_chars: int = 0,
    prompt_tokens: int = 0,
    model_id: str = "",
    coverage: str = "unobserved",
) -> None:
    """Append a scope_review_complete event to events.jsonl."""
    prompt_tokens = int(prompt_tokens or 0)
    if prompt_tokens <= 0 and prompt_chars:
        prompt_tokens = max(0, int(prompt_chars) // 4)
    try:
        append_jsonl(ctx.drive_logs() / "events.jsonl", {
            "ts": utc_now_iso(), "type": "scope_review_complete",
            "task_id": getattr(ctx, "task_id", "") or "",
            "model": model_id or _get_scope_model(),
            "critical_count": critical_count,
            "advisory_count": advisory_count,
            "prompt_tokens": prompt_tokens,
            "read_coverage": coverage,
        })
    except Exception:
        pass


# The one user turn every scope row's episode opens with; the commit gate's wave
# admission measures the same first send the substrate dispatches.
SCOPE_USER_TURN = "Review the staged change and context above. Output ONLY a JSON array."
# The output contract a RETRIEVING scope row (session or native episode) is
# handed: the extraction fallback canonicalizes to the SCOPE contract —
# required-matrix shape, eight verbatim item ids (D19 — never a looser contract).
SCOPE_RETRIEVING_OUTPUT_CONTRACT = (
    REVIEW_JSON_MATRIX_CONTRACT
    + "\nRequired item ids (verbatim, one entry each): "
    + ", ".join(sorted(SCOPE_REQUIRED_ITEMS))
)


def _call_scope_llm(
    prompt: str = "",
    scope_model: str | None = None,
    ctx: ToolContext | None = None,
    slot_id: str = "",
    route: Any = None,
    session_task: str = "",
    session_root: str = "",
    slot_effort: str = "",
    session_target: str = "",
    session_profile: str = "", retry_key: str = "", subagent_id: str = "", use_local: bool | None = None, task_evidence: dict = None,
    required_sources: Optional[list] = None,
    required_sources_ref: Optional[dict] = None,
    native_data_root: str = "",
) -> tuple:
    """Execute the scope review call synchronously — native episode or session.

    Returns (raw_text, usage, error_msg) — error_msg is non-empty on failure.
    ``usage`` may contain a private ``_review_refs`` entry with durable prompt
    and response refs from the shared review substrate.

    ``slot_id`` is the identity of the configured row this call belongs to,
    supplied by whoever fanned the rows out. ``route`` is the row's configured
    transport: on ``agent_session`` the substrate's session executor delivers the
    brief in ``session_root``, on ``api_chat`` the native episode executor runs
    it as a bounded inspection episode on the row's own route. Parsing,
    classification and blocking above this call are identical for both (5.3).
    The brief rides as ``session_task``; ``prompt`` is the same text under its
    historical spelling for callers that pass it positionally.

    ``required_sources`` rides the request policy, so a native episode folds its
    observed reads over the manifest and a delegated session's harness-observed
    reads fold over the same rows. ``native_data_root`` is the data root a
    native episode reads its own sources under — the recorded task evidence
    view, or the root the brief paged its exact staged diff into."""
    from ouroboros.config import resolve_effort as _resolve_effort
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools.review import _owner_deadline_at

    scope_model = scope_model or _get_scope_model()
    # 6.1/6.3: the row's own effort wins; the global key stays the default.
    scope_effort = slot_effort or _resolve_effort("scope_review")
    delegated = str(getattr(route, "value", route) or "") == "agent_session"
    brief = session_task or prompt
    # Output budget scales with the reviewer window: requesting the absolute
    # 100K reserve on a small-window model would 400 on input+max_tokens.
    _scope_output_tokens, _ = _window_scaled_reserves(
        _scope_window(scope_model, **({"model_role": f"reviewer:{slot_id}",
                      "credential_profile_id": session_profile, "use_local": use_local} if slot_id else {})).sizing_window(_SCOPE_SIZING_FALLBACK)
    )
    try:
        from ouroboros.review_substrate import ReviewRequest, run_review_request

        from ouroboros.review_evidence import commit_review_evidence_refs
        evidence = task_evidence or {}
        policy = {"output_contract": SCOPE_RETRIEVING_OUTPUT_CONTRACT}
        if required_sources is not None:
            # BOTH retrieving deliveries carry the manifest: the native
            # episode folds its own read receipts over it, and a delegated
            # session's harness-observed reads fold over the same rows.
            policy["native_required_sources"] = required_sources
            policy["native_required_sources_ref"] = dict(required_sources_ref or {})
        if not delegated:
            # The episode's own reader root: the recorded task evidence view, or
            # the root the brief paged its exact staged-diff source under.
            root = str(native_data_root or (evidence.get("data_root") if evidence else "") or "")
            if root:
                policy["native_data_root"] = root
        request = ReviewRequest(
            surface="scope_review",
            evidence={"task_execution": evidence} if evidence else {},
            evidence_refs=commit_review_evidence_refs(evidence),
            goal=SCOPE_USER_TURN,
            messages=[],
            task_id=str(getattr(ctx, "task_id", "") or "scope_review") if ctx is not None else "scope_review", retry_key=str(retry_key or ""),
            call_type="scope_review",
            max_tokens=_scope_output_tokens,
            default_temperature=0.2,
            no_proxy=True,
            session_task=brief,
            session_root=session_root,
            reconcile_only=bool(getattr(ctx, "_review_reconcile_only", False)),
            deadline_at=_owner_deadline_at(ctx),
            policy=policy,
        )
        row = scope_reviewer_slots([scope_model], effort=scope_effort)[0]
        slot = replace(
            row,
            slot_id=slot_id or row.slot_id,
            timeout_sec=_SCOPE_REVIEW_SLOT_TIMEOUT_SEC,
            max_tokens=_scope_output_tokens,
            default_temperature=0.2,
            # The caller's fanned-out route is authoritative; never re-derive it.
            route=ReviewRouteKind.AGENT_SESSION if delegated else ReviewRouteKind.API_CHAT,
            # Empty keeps the shared session-route fallback.
            session_target=session_target if delegated else "",
            session_profile=session_profile, subagent_id=str(subagent_id or ""),
            use_local=row.use_local if use_local is None else use_local,
            # Every scope row retrieves: an api row binds the native episode
            # executor whether or not an actor id binds it.
            native_retrieval_override=not delegated,
        )
        result = run_review_request(
            request,
            slots=[slot],
            drive_root=review_drive_root(ctx),
            llm=LLMClient(),
            usage_ctx=ctx,
        )
        actor = (result.actors or [{}])[0]
        usage = dict(actor.get("usage") or {})
        usage["_review_refs"] = {
            "prompt_ref": actor.get("prompt_ref") or {},
            "response_ref": actor.get("response_ref") or {},
        }
        usage.update({
            "operation_id": str(actor.get("operation_id") or ""),
            "operation_state": str(actor.get("operation_state") or "settled"),
            "late_result_pending": bool(actor.get("late_result_pending")),
            "recovery_binding": dict(actor.get("recovery_binding") or {}),
            "pending_invocation_id": str(actor.get("pending_invocation_id") or usage.get("pending_invocation_id") or ""),
            "delegated_run_id": str(actor.get("delegated_run_id") or usage.get("delegated_run_id") or ""),
            "failure_code": str(actor.get("failure_code") or ""),
        })
        if actor.get("status") not in {"ok", "empty"}:
            error_msg = (
                f"⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer ({scope_model}) failed — commit blocked.\n"
                f"Error: {actor.get('error') or actor.get('status') or 'scope reviewer failed'}\n"
                "Retry the commit, or check API key and network connectivity."
            )
            return str(actor.get("raw_text") or ""), usage, error_msg
        return str(actor.get("raw_text") or ""), usage, ""
    except Exception as e:
        from ouroboros.llm_claudexor import propagate_model_error
        propagate_model_error(e)
        error_msg = (
            f"⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer ({scope_model}) failed — commit blocked.\n"
            f"Error: {type(e).__name__}: {e}\n"
            "Retry the commit, or check API key and network connectivity."
        )
        return "", None, error_msg


def run_scope_review(
    ctx: ToolContext,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
    review_history: Optional[list] = None,
    scope_review_history: Optional[list] = None,  # prior scope rounds for this commit
    scope_model: Optional[str] = None,
    slot_id: str = "",  # identity of the configured row this call runs (see scope_reviewer_slots)
    route: Any = None,  # the row's configured transport (ReviewRouteKind); None/api_chat = native episode
    slot_effort: str = "",  # the row's own effort (6.1); "" = global scope_review effort
    session_target: str = "",  # the row's own harness[=model] target; "" = shared route
    session_profile: str = "",  # credential pin (Q2-в); "" = rotation
    subagent_id: str = "",  prepared: Optional[dict] = None, retry_key: str = "",  # assembled brief + immutable cycle identity
) -> ScopeReviewResult:
    """Run blocking scope review from a prepared brief or a direct call."""
    if prepared is None:
        from ouroboros.tools.review_admission import prepare_scope_review

        prepared, final = prepare_scope_review(
            ctx, commit_message, goal=goal, scope=scope,
            review_rebuttal=review_rebuttal, review_history=review_history,
            scope_review_history=scope_review_history, scope_model=scope_model,
            slot_id=slot_id, route=route, slot_effort=slot_effort,
            session_target=session_target, session_profile=session_profile,
            subagent_id=subagent_id,
        )
        if final is not None:
            return final
    _SCOPE_CONTEXT_MANIFEST.set(dict(prepared["context_manifest"] or {}))
    session_task = prepared["session_task"]
    repo_dir, scope_model_id = prepared["repo_dir"], prepared["scope_model_id"]
    slot_id, route = prepared["slot_id"], prepared["route"]
    slot_effort, session_target = prepared["slot_effort"], prepared["session_target"]
    session_profile = prepared["session_profile"]
    subagent_id = str(prepared.get("subagent_id") or "")
    _manifest_ref = dict(prepared.get("required_sources_ref") or {})

    _prompt_chars = len(session_task)
    _prompt_tokens_est = estimate_tokens(session_task)
    raw_text, usage, llm_error = _call_scope_llm(
        scope_model=scope_model_id, ctx=ctx, slot_id=slot_id,
        route=route, session_task=session_task, session_root=str(repo_dir),
        slot_effort=slot_effort, session_target=session_target,
        session_profile=session_profile, retry_key=retry_key, subagent_id=subagent_id,
        use_local=prepared.get("use_local"), task_evidence=prepared.get("task_evidence"),
        required_sources=prepared.get("required_sources"),
        required_sources_ref=_manifest_ref,
        native_data_root=str(prepared.get("native_data_root") or ""),
    )
    _usage = dict(usage or {})
    host_route = _usage.get("model_role_route") or {}
    actual_model = str(host_route.get("model") or scope_model_id)
    _review_refs = dict(_usage.pop("_review_refs", {}) or {})
    _prompt_ref = dict(_review_refs.get("prompt_ref") or {})
    _response_ref = dict(_review_refs.get("response_ref") or {})
    _tokens_in = int(_usage.get("prompt_tokens", 0) or 0)
    _tokens_out = int(_usage.get("completion_tokens", 0) or 0)
    _cost_usd = float(_usage.get("cost", 0.0) or 0.0)
    _operation = {
        "operation_id": str(_usage.get("operation_id") or ""),
        "operation_state": str(_usage.get("operation_state") or "settled"),
        "late_result_pending": bool(_usage.get("late_result_pending")),
        "pending_invocation_id": str(_usage.get("pending_invocation_id") or ""),
        "delegated_run_id": str(_usage.get("delegated_run_id") or ""),
    }
    failure = {"failure_phase": str(_usage.get("review_failure_phase") or ""),
               "failure_code": str(_usage.get("failure_code") or "")}
    if llm_error:
        return ScopeReviewResult(
            blocked=True,
            block_message=llm_error,
            model_id=scope_model_id,
            status="error", raw_text=raw_text, **failure,
            prompt_chars=_prompt_chars,
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
            **_operation,
        )
    # Usage emission happens once inside the shared review substrate.
    if not raw_text.strip():
        # Empty model response is distinct from transport/API error.
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer returned empty response — commit blocked.\n"
                "Retry the commit."
            ),
            model_id=scope_model_id,
            status="empty_response", failure_phase="format", failure_code="empty_response",
            prompt_chars=_prompt_chars,
            tokens_in=_tokens_in,
            tokens_out=_tokens_out,
            cost_usd=_cost_usd,
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
            **_operation,
        )
    items = extract_json_array(raw_text, normalize=True)
    if items is None:
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Could not parse scope reviewer output as JSON — commit blocked.\n"
                "Full raw response preserved in scope_raw_result (status='parse_failure')."
            ),
            model_id=scope_model_id,
            status="parse_failure", failure_phase="format", failure_code="parse_failure",
            raw_text=raw_text,
            prompt_chars=_prompt_chars,
            tokens_in=_tokens_in,
            tokens_out=_tokens_out,
            cost_usd=_cost_usd,
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
            **_operation,
        )
    parsed_items, contract_error = _normalize_scope_items(items)
    if contract_error:
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer output violated the "
                "Intent / Scope Review Checklist coverage contract — commit blocked.\n"
                f"{contract_error}\n"
                "Retry the commit so scope review covers all required checklist items."
            ),
            model_id=scope_model_id,
            status="parse_failure", failure_phase="format", failure_code="checklist_contract",
            raw_text=raw_text,
            parsed_items=parsed_items,
            critical_findings=_classify_scope_findings(parsed_items)[0],
            advisory_findings=_classify_scope_findings(parsed_items)[1],
            prompt_chars=_prompt_chars,
            tokens_in=_tokens_in,
            tokens_out=_tokens_out,
            cost_usd=_cost_usd,
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
            **_operation,
        )

    critical_findings, advisory_findings = _classify_scope_findings(parsed_items)
    # How much of the required-source manifest this row was OBSERVED to read.
    # One reader for every retrieving delivery: a native episode reports
    # host-observed receipts, a delegated session harness-observed ones. A
    # delivery that reports no coverage fact at all stays `unobserved` — the P3
    # provenance limit, not a finding that the review was incomplete.
    from ouroboros.tools.scope_required_sources import coverage_state

    _coverage_fact = _usage.get("native_read_coverage")
    _manifest = _current_scope_context_manifest()
    if isinstance(_coverage_fact, dict):
        # A native episode attests its reads as host-observed; a delegated
        # session states its provenance on the folded fact directly.
        _manifest = {**_manifest, "native_read_coverage": _coverage_fact,
                     "read_provenance": str(_usage.get("read_provenance")
                                            or _usage.get("host_file_read_attestation") or "")}
    result_kwargs = {
        "parsed_items": parsed_items,
        "model_id": scope_model_id,
        "raw_text": raw_text,
        "prompt_chars": _prompt_chars,
        "tokens_in": _tokens_in,
        "tokens_out": _tokens_out,
        "cost_usd": _cost_usd,
        "context_manifest": _manifest,
        "coverage": coverage_state(_coverage_fact),
        "coverage_manifest_ref": _manifest_ref,
        "prompt_ref": _prompt_ref,
        "response_ref": _response_ref,
        **_operation,
    }
    _log_scope_result(
        ctx,
        len(critical_findings),
        len(advisory_findings),
        prompt_chars=_prompt_chars,
        prompt_tokens=_prompt_tokens_est,
        model_id=actual_model,
        coverage=result_kwargs["coverage"],
    )

    if critical_findings:
        from ouroboros import config as _cfg
        if review_enforcement_blocks(_cfg.get_review_enforcement()):
            return ScopeReviewResult(
                blocked=True,
                block_message=_build_block_message(critical_findings, advisory_findings),
                critical_findings=critical_findings,
                advisory_findings=advisory_findings,
                status="responded",
                **result_kwargs,
            )
        # Parallel review aggregates advisory findings on the main thread.

    return ScopeReviewResult(
        blocked=False,
        critical_findings=critical_findings,
        advisory_findings=advisory_findings,
        status="responded",
        **result_kwargs,
    )
