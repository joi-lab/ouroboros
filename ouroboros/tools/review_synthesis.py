"""Shared pure synthesis and normalization for reviewer outputs.

Commit-review claims use the optional LLM deduplicator; the plan-review engine
(``tools/plan_review.py`` + ``plan_spec``/``plan_packet``) keeps only the shared
control-line prefix, the mixed-window input-cap helpers, the cache-friendly
message pair and the usage-emission shim here.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ouroboros.triad_review import REVIEW_JSON_MATRIX_CONTRACT, extract_json_array
from ouroboros.tools.review_helpers import (
    REPO_ANTI_PATTERN_LOCK_GUARD,
    REVIEW_PREAMBLE,
    emit_review_usage,
)

log = logging.getLogger(__name__)

# Bound cost and avoid mixed canonical/raw output on oversized finding sets.
_MAX_CLAIMS_FOR_SYNTHESIS = 30

_MIN_CLAIMS_FOR_SYNTHESIS = 2

_SYNTHESIS_PROMPT_TEMPLATE = (
    "You are a code-review claim synthesizer. You receive a list of raw findings\n"
    "from multiple independent reviewers (triad diff-reviewers + the\n"
    "whole-repository scope reviewer). Your job is to produce a deduplicated\n"
    "canonical list.\n"
    "\n"
    "## Rules\n"
    "\n"
    "1. Merge claims that share the same **root cause** in the same file/symbol\n"
    "   into ONE canonical entry. Use the most specific/concrete reason text.\n"
    "2. **Do NOT merge** findings about genuinely different bugs, even if they are\n"
    "   in the same file. One root cause = one canonical issue.\n"
    "3. If an incoming claim already carries an `obligation_id` that matches an\n"
    "   open obligation from a previous round (provided below), PRESERVE that\n"
    "   `obligation_id` on the canonical entry. This allows durable obligations\n"
    "   to survive across retries without ID rotation.\n"
    "4. If no existing obligation matches, leave `obligation_id` as \"\" — a new\n"
    "   obligation will be assigned downstream.\n"
    "5. Do NOT invent new findings. Only deduplicate what you have been given.\n"
    "6. For each canonical entry, list `evidence_from_reviewers`: which reviewer(s)\n"
    "   independently flagged this issue (use the `tag` or `model` field if present).\n"
    "7. Output ONLY valid JSON — a JSON array of canonical findings, no markdown fences,\n"
    "   no prose outside the array.\n"
    "\n"
    "## Output format (each element)\n"
    "\n"
    '{"item": "<checklist item name>", "severity": "critical|advisory",\n'
    ' "reason": "<most concrete reason>", "obligation_id": "<existing id or empty>",\n'
    ' "evidence_from_reviewers": ["<tag/model1>", "<tag/model2>"]}\n'
    "\n"
    "## Open obligations from previous rounds (match by item + reason similarity)\n"
    "\n"
    "OPEN_OBLIGATIONS_PLACEHOLDER\n"
    "\n"
    "## Raw reviewer claims to deduplicate\n"
    "\n"
    "CLAIMS_PLACEHOLDER\n"
    "\n"
    "Respond with ONLY the JSON array. No explanation.\n"
)


def _redact(text: str) -> str:
    """Redact secret-like values from a string before including it in an LLM prompt."""
    try:
        from ouroboros.tools.review_helpers import redact_prompt_secrets
        redacted, _ = redact_prompt_secrets(str(text or ""))
        return redacted
    except Exception:
        return ""


def _format_obligations(open_obligations: List[Any]) -> str:
    """Render open obligations as compact secret-redacted JSON."""
    if not open_obligations:
        return "[]"
    from ouroboros.utils import truncate_review_artifact
    items = []
    for o in open_obligations:
        raw_reason = str(getattr(o, "reason", "") or "")
        redacted_reason = _redact(raw_reason)
        items.append({
            "obligation_id": str(getattr(o, "obligation_id", "") or ""),
            "item": str(getattr(o, "item", "") or ""),
            "reason_excerpt": truncate_review_artifact(redacted_reason, limit=500),
        })
    try:
        return json.dumps(items, ensure_ascii=False, indent=2)
    except Exception:
        return "[]"


def _format_claims(findings: List[Dict[str, Any]]) -> str:
    """Render raw findings as compact JSON with secret-redacted reasons."""
    try:
        safe = []
        for f in findings:
            entry = dict(f)
            if "reason" in entry:
                entry["reason"] = _redact(str(entry["reason"] or ""))
            safe.append(entry)
        return json.dumps(safe, ensure_ascii=False, indent=2)
    except Exception:
        return "[]"


def _normalize_evidence(value: Any) -> List[str]:
    """Normalize evidence_from_reviewers without splitting bare strings into chars."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, str)]
    return []


def _parse_synthesis_output(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the synthesizer's JSON array response. Returns None on failure."""
    if not raw:
        return None
    parsed = extract_json_array(raw)
    if not isinstance(parsed, list):
        return None
    result = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if not entry.get("item"):
            continue
        canonical = {
            "item": str(entry.get("item", "") or ""),
            # The synthesizer's INPUT is exclusively critical findings, so a
            # missing severity must stay critical — an "advisory" default
            # silently downgraded blocking findings out of the gate.
            "severity": str(entry.get("severity", "critical") or "critical"),
            "reason": str(entry.get("reason", "") or ""),
            "obligation_id": str(entry.get("obligation_id", "") or ""),
            "evidence_from_reviewers": _normalize_evidence(entry.get("evidence_from_reviewers")),
            # FAIL default ensures synthesized findings create obligations downstream.
            "verdict": str(entry.get("verdict", "") or "FAIL"),
        }
        for key in ("tag", "model"):
            if key in entry:
                canonical[key] = entry[key]
        result.append(canonical)
    return result if result else None


def synthesize_to_canonical_issues(
    critical_findings: List[Dict[str, Any]],
    *,
    open_obligations: Optional[List[Any]] = None,
    ctx: Any = None,
) -> List[Dict[str, Any]]:
    """Return deduplicated findings, or original findings on any synthesis failure."""
    if not critical_findings:
        return critical_findings

    if len(critical_findings) < _MIN_CLAIMS_FOR_SYNTHESIS:
        return critical_findings

    # Oversized sets pass through unchanged; no hybrid canonical/raw tail.
    if len(critical_findings) > _MAX_CLAIMS_FOR_SYNTHESIS:
        log.debug(
            "review_synthesis: %d claims exceeds limit %d — skipping synthesis, "
            "returning original findings unchanged",
            len(critical_findings),
            _MAX_CLAIMS_FOR_SYNTHESIS,
        )
        return critical_findings

    obligations = list(open_obligations or [])

    try:
        prompt = (
            _SYNTHESIS_PROMPT_TEMPLATE
            .replace("OPEN_OBLIGATIONS_PLACEHOLDER", _format_obligations(obligations))
            .replace("CLAIMS_PLACEHOLDER", _format_claims(critical_findings))
        )
    except Exception as exc:
        log.warning("review_synthesis: failed to build prompt: %s", exc)
        return critical_findings

    try:
        raw_response = _call_synthesis_llm(prompt, ctx=ctx)
    except Exception as exc:
        from ouroboros.llm_claudexor import propagate_model_error
        propagate_model_error(exc)
        log.warning("review_synthesis: LLM call raised exception: %s — using original findings", exc)
        return critical_findings

    if raw_response is None:
        log.warning("review_synthesis: LLM call returned None — using original findings")
        return critical_findings

    canonical = _parse_synthesis_output(raw_response)
    if canonical is None:
        log.warning("review_synthesis: failed to parse LLM output — using original findings")
        return critical_findings

    log.debug(
        "review_synthesis: %d raw → %d canonical",
        len(critical_findings),
        len(canonical),
    )
    return canonical


def _call_synthesis_llm(prompt: str, *, ctx: Any = None) -> Optional[str]:
    """Call the light LLM and emit usage so synthesis spend is accounted."""
    try:
        from ouroboros.config import get_light_model
        from ouroboros.llm import LLMClient

        model = get_light_model()

        client = LLMClient()

        # no_proxy avoids macOS fork-safety crashes in worker processes.
        msg, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            model_role="light",
            max_tokens=16384,
            reasoning_effort="low",
            no_proxy=True,
        )

        if _has_billable_usage(usage):
            resolved_model = str((usage or {}).get("resolved_model") or "") or model
            provider = str((usage or {}).get("provider") or "") if isinstance(usage, dict) else ""
            emit_review_usage(
                ctx,
                model=resolved_model,
                usage=usage,
                source="review_synthesis",
                provider=provider,
            )

        if not msg:
            return None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ]
            return "\n".join(t for t in texts if t) or None
        return str(content) if content else None

    except Exception as exc:
        from ouroboros.llm_claudexor import propagate_model_error
        propagate_model_error(exc)
        log.warning("review_synthesis: LLM call failed: %s", exc)
        return None


def _has_billable_usage(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(
        usage.get(key)
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "cost", "total_cost")
    )


PLAN_REVIEW_CONTROL_PREFIX = "PLAN_REVIEW_CONTROL_JSON: "


def quorum_input_token_limit(models: Any, slot_limits: Any) -> int:
    """Assembly budget for ONE shared prompt fanned across mixed-window slots: the largest cap that
    still leaves a review QUORUM callable, i.e. the quorum-th largest per-slot cap.

    The global minimum let the SMALLEST window dictate the Atlas for everyone — with caps
    [545K, 745K, 745K] and quorum 2 it discarded ~200K of context the two large reviewers could
    have read, and refused an irreducible 600K prompt that those two would have accepted. Here the
    small slot drops OUT of the quorum instead of shrinking it: the caller records it as a typed
    ``preflight_oversize`` result, so a slot that cannot fit is REPORTED as not participating,
    never silently ignored. Quorum is counted over the CONFIGURED slots, so an unavailable or
    uncalibrated slot reads 0 and simply cannot be part of the quorum that justifies a bigger prompt."""
    from ouroboros.config import adaptive_quorum

    limits = dict(slot_limits or {})
    caps = sorted((int(limits.get(str(m), 0) or 0) for m in (models or [])), reverse=True)
    if not caps:
        return 0
    return caps[min(adaptive_quorum(len(caps)), len(caps)) - 1]


def per_slot_input_token_limits(
    models: Any,
    *,
    context_window: Optional[int] = None,
    output_reserve: int,
    tokenizer_margin: int,
    slots: Any = None,
) -> Dict[str, int]:
    """Per-slot calibrated input caps for a prompt fanned across mixed families.

    ``context_window=None`` (the default) resolves each slot's REAL window from
    Capability Evidence and scales the reserves to it, so a sub-1M slot gets a
    fit-sized pack instead of a prompt sized for a window it does not have.
    An explicit window stays honoured for callers that pin one deliberately.
    Frozen ``slots`` return caps keyed by stable slot ID; legacy model-only
    callers retain model keys. Equal models may have different account limits."""
    from ouroboros.reviewer_window import reviewer_context_window, reviewer_window_binding, window_scaled_reserves
    from ouroboros.tools.review_helpers import calibrated_input_token_limit

    limits: Dict[str, int] = {}
    models = list(models or [])
    rows = list(slots) if slots is not None else None
    if rows is not None and len(rows) != len(models):
        raise ValueError("Reviewer capacity rows must align with their frozen models")
    for index, model in enumerate(models):
        row = rows[index] if rows is not None else None
        binding = reviewer_window_binding(row) if row is not None else {}
        key = binding.get("model_role", "").removeprefix("reviewer:") if row is not None else str(model)
        if not key or (key in limits and row is not None):
            raise ValueError("Reviewer capacity requires unique stable slot IDs")
        window = (
            int(context_window)
            if context_window is not None
            else reviewer_context_window(str(model), **binding)
        )
        slot_output_reserve, slot_margin = window_scaled_reserves(
            window, output_reserve=output_reserve, tokenizer_margin=tokenizer_margin,
        )
        limits[key] = max(0, calibrated_input_token_limit(
            str(model),
            context_window=window,
            output_reserve=slot_output_reserve,
            tokenizer_margin=slot_margin,
        ))
    return limits


def build_scope_review_prompt(
    current_files_section: str,
    *,
    scope_checklist: str,
    canonical_docs: str,
    intent_context: str,
    history_block: str,
    diff_text: str,
    repo_pack_placeholder: str,
    critical_calibration: str,
    task_evidence_section: str = "",
) -> tuple:
    # STABLE-FIRST for provider prompt caching: instructions, checklist and the
    # always-inline governance tier are byte-stable across commits and form the
    # cache-marked prefix; goal/scope/history/diff/index are the per-commit
    # tail. The returned boundary is the length of that prefix.
    stable = f"""\
{REVIEW_PREAMBLE}

## Your role

You are the whole-repository reviewer, and you REACH the repository with your own
read-only tools. Diff reviewers cover line-level mistakes;
you cover cross-module contracts, forgotten touchpoints, hidden regressions,
prompt/doc sync, architecture fit, and end-to-end intent completeness.

## Your task

For each finding, you MUST name the exact file, symbol, test, prompt, doc,
config, or sibling flow that proves the issue. Vague concerns without a
concrete artifact reference must be marked advisory, not critical.

## Output format

Output ONLY a valid JSON array.

You MUST cover every checklist item from the Intent / Scope Review
Checklist below. Skipping an item is not allowed — a missing entry
indicates the item was not actually reviewed.

The eight checklist item identifiers you MUST return (exactly these strings
in the "item" field; no substitutions):

1. intent_alignment
2. forgotten_touchpoints
3. cross_surface_consistency
4. regression_surface
5. prompt_doc_sync
6. architecture_fit
7. cross_module_bugs
8. implicit_contracts

Each element must follow the shared review JSON contract:
{REVIEW_JSON_MATRIX_CONTRACT}

Additional scope-review requirements:
- "item" must be one of the eight identifiers above — verbatim, case-sensitive.
- optional "obligation_id" when resolving or re-checking a previously surfaced obligation.
- "reason":
  - For FAIL: concrete artifact (file/symbol/line/contract) + what is wrong + how to fix.
  - For PASS: 1–2 sentences stating WHY this item passes, naming a concrete
artifact or code path that you checked. A bare "PASS" or single-word
reason without justification indicates the item was not actually
reviewed and will be treated as a reviewer failure.

If one checklist item has multiple distinct concrete problems, return one
FAIL entry per distinct root cause. Do not compress unrelated bugs into a
single summary. If an item has no problems, return one PASS entry. Do not
return duplicate PASS entries, and do not return PASS for an item that also
has a FAIL — the concrete FAIL is authoritative.

Severity rules: critical requires a concrete current artifact and a required
change to this diff; otherwise use advisory. Scope affects only unchanged
legacy code outside the diff. Apply the `Critical surface whitelist` in
`docs/CHECKLISTS.md` for prose-vs-code mismatches.

If an open obligation record in the review history section below already names
an `obligation_id` for this root cause, reuse that exact `obligation_id`.
Do NOT invent a new id for the same root cause.

## Anti pattern-lock guard

{REPO_ANTI_PATTERN_LOCK_GUARD}

{critical_calibration}

{scope_checklist}

## Canonical Documentation Context

These rules are inlined for every review of every change. Every other governance
document is named in the navigation below and is one read away; none of them is
omitted.

{canonical_docs}
"""
    dynamic = f"""\
{intent_context}

{history_block}

{task_evidence_section}

## Touched files

The manifest below names every path this change touches, with what happened to
it and how large it is in the candidate tree. No file body is inlined: the
staged diff carries every added and removed line, and you read any file itself
with your own tools.

{current_files_section}

## Staged diff

{diff_text}

## Wider repository context

{repo_pack_placeholder}
"""
    return stable + "\n" + dynamic, len(stable) + 1


def build_plan_review_messages(
    system_prompt: str,
    user_content: str,
    user_stable_len: int = 0,
) -> List[Dict[str, Any]]:
    """Cache-friendly plan-review message pair.

    The whole system prompt (governance docs + reviewer contract) is byte-stable
    across plan reviews and carries the cache marker; the user content marks its
    evidence/plan boundary so stable repository evidence caches while the
    revised plan does not."""
    from ouroboros.tools.review_helpers import cached_prompt_blocks

    return [
        {"role": "system", "content": cached_prompt_blocks(system_prompt)},
        {
            "role": "user",
            "content": (
                cached_prompt_blocks(
                    user_content[:user_stable_len], user_content[user_stable_len:]
                )
                if 0 < user_stable_len <= len(user_content)
                else user_content
            ),
        },
    ]
