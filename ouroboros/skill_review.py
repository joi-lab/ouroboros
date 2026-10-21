"""Tri-model review for one external skill package.

Uses the Skill Review Checklist and persists verdicts in skill state, siloed
from repo commit obligations. Tool registration lives in ``tools/skill_exec.py``.
"""

from __future__ import annotations

import json
import logging
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.config import adaptive_quorum, get_auto_grant_enabled
from ouroboros.reviewer_slot_config import commit_triad_delivery, reviewer_slot_config_error
from ouroboros.skill_loader import (
    SkillReviewState,
    auto_grant_if_enabled,
    compute_content_hash,
    save_review_state,
)
from ouroboros.tool_access import (
    ResolvedResourceBinding,
    build_resolved_resource_binding,
    load_bound_skill,
)
from ouroboros.skill_review_history import (
    append_history as _append_skill_review_history,
    count_attempts as _count_attempts_for_content,  # noqa: F401 — split facade re-export
    finding_signature as _finding_signature,  # noqa: F401 — split facade re-export
    load_history as _load_skill_review_history,
    review_history_path,  # noqa: F401 — split facade re-export
)
from ouroboros.skill_review_status import (
    CRITICAL_ITEMS,  # noqa: F401 — split facade re-export
    STATUS_BLOCKERS,  # noqa: F401 — split facade re-export
    STATUS_CLEAN,  # noqa: F401 — split facade re-export
    STATUS_PENDING,
    STATUS_WARNINGS,  # noqa: F401 — split facade re-export
    WARNINGS_CONVERGENCE_ROUNDS,  # noqa: F401 — split facade re-export
    aggregate_skill_review_status,  # noqa: F401 — split facade re-export
    count_trailing_warnings_rounds,  # noqa: F401 — split facade re-export
)
from ouroboros.tools.review_helpers import (
    REVIEW_PROMPT_TOKEN_BUDGET,  # noqa: F401 — split facade re-export
    build_anti_thrashing_rules_section,  # noqa: F401 — split facade re-export
    build_rebuttal_section,  # noqa: F401 — split facade re-export
    build_self_verification_template,  # noqa: F401 — split facade re-export
    build_skill_host_context,  # noqa: F401 — split facade re-export
    format_obligation_excerpt,  # noqa: F401 — split facade re-export
    format_prompt_code_block,  # noqa: F401 — split facade re-export
    load_checklist_section,  # noqa: F401 — split facade re-export
)
from ouroboros.triad_review import emit_review_model_error_events, extract_json_array, parse_model_review_results  # noqa: F401 — split facade re-export
from ouroboros.utils import (
    append_jsonl,  # noqa: F401 — split facade re-export
    estimate_tokens,  # noqa: F401 — split facade re-export
    sanitize_tool_result_for_log,
    utc_now_iso,
)

log = logging.getLogger(__name__)


from ouroboros.skill_review_packs import (  # noqa: F401 — split facade re-exports
    _LOADABLE_BINARY_EXTENSIONS,
    _SKILL_PACK_TOKEN_HEADROOM,
    _SkillBinaryPayload,
    _SkillFileOverBudget,
    _SkillFileUnreadable,
    _build_skill_file_packs,
    _read_skill_file,
    _skill_pack_token_budget,
)
from ouroboros.skill_review_rebuttals import (  # noqa: F401 — split facade re-exports
    _build_skill_review_history_section,
    _convergence_hint,
    _render_accepted_rebuttals_section,
    _review_history_path,
)
from ouroboros.skill_review_prompt import (  # noqa: F401 — split facade re-exports
    _CRITICAL_ITEMS,
    _REPO_ROOT,
    _SKILL_CHECKLIST_SECTION,
    _SKILL_REVIEW_ITEMS,
    _build_review_prompt,
    _build_review_prompt_for_attempt,
    _load_governance_artifact,
)
from ouroboros.skill_review_output import (  # noqa: F401 — split facade re-exports
    _aggregate_status,
    _extract_actor_findings,
    _parse_json_array,
    render_skill_review_block,
)


def _truncate_raw_result(text: str) -> str:
    """Return full raw review text; actor records are the structured SSOT."""
    return str(text or "")


@dataclass
class SkillReviewOutcome:
    """Return payload from ``review_skill``."""

    skill_name: str
    status: str  # "clean" | "warnings" | "blockers" | "pending"
    findings: List[Dict[str, Any]] = field(default_factory=list)
    reviewer_models: List[str] = field(default_factory=list)
    content_hash: str = ""
    prompt_chars: int = 0
    cost_usd: float = 0.0
    raw_result: str = ""
    raw_actor_records: List[Dict[str, Any]] = field(default_factory=list)
    advisory_result: Dict[str, Any] = field(default_factory=dict)
    convergence_hint: str = ""
    error: str = ""
    auto_flow: bool = False
    # Bible P3: a single configured reviewer is honored on this executable TRUST
    # gate but the lost cross-model diversity is recorded loudly + DURABLY (in the
    # outcome and review history), so a one-slot skill review can never later look
    # like an ordinary clean/warnings multi-reviewer review.
    single_reviewer_no_diversity: bool = False
    requested_keys: List[str] = field(default_factory=list)
    auto_granted_keys: List[str] = field(default_factory=list)
    requested_permissions: List[str] = field(default_factory=list)
    auto_granted_permissions: List[str] = field(default_factory=list)
    review_profile: str = ""
    # Max-Review-Cycles facts (Q17/Q23): ``paid`` = a reviewer panel was
    # physically dispatched for this outcome (recorded WRITE-AHEAD at dispatch
    # via the durable dispatch marker; one chunked wave = ONE cycle) with
    # ``wave_id`` naming that dispatch; the contract fingerprint the panel ran
    # under; the content hash of the rebuttal supplied; and, for a $0 free
    # replay, the ts of the recorded verdict it quotes. All land on the
    # terminal history row.
    paid: bool = False
    wave_id: str = ""
    review_contract_fingerprint: str = ""
    rebuttal_sha256: str = ""
    replayed_from_ts: str = ""
    # Foreground call outcome, not the skill's pending status or recorded history.
    dispatch_refusal_code: str = ""
    panel: Dict[str, Any] = field(default_factory=dict)


def _apply_auto_grant_outcome(outcome: SkillReviewOutcome, skill: Any, auto_grant: Any) -> None:
    outcome.requested_keys = list(getattr(auto_grant, "requested_keys", []) or [])
    outcome.auto_granted_keys = list(getattr(auto_grant, "granted_keys", []) or [])
    outcome.requested_permissions = list(getattr(auto_grant, "requested_permissions", []) or [])
    outcome.auto_granted_permissions = list(getattr(auto_grant, "granted_permissions", []) or [])
    # Refresh the policy even on free replay; explicit task-authorized toggles
    # follow their own lifecycle path rather than this automatic first enable.
    outcome.auto_flow = bool(getattr(skill, "is_self_authored", False) and get_auto_grant_enabled())


# The accepted-rebuttal ledger and the Max-Review-Cycles machinery moved whole
# to ouroboros/skill_review_cycles.py (module-size gate; same split shape as
# phase 0's update_candidate.py). Historical names stay importable from here.
from ouroboros.skill_review_cycles import (  # noqa: E402
    accepted_rebuttals_path as _accepted_rebuttals_path,  # noqa: F401 — re-export
    load_accepted_rebuttals as _load_accepted_rebuttals,  # noqa: F401 — split facade re-export
    fail_items_from_history_entry as _fail_items_from_history_entry,  # noqa: F401 — re-export
    install_skill_dispatch_stamp as _install_skill_dispatch_stamp,
    persist_rebuttal_flips as _persist_rebuttal_flips,
    plugin_api_admission_refusal_outcome as _admission_refusal_outcome,
    record_accepted_rebuttal as _record_accepted_rebuttal,  # noqa: F401 — re-export
    review_wave_budget_block as _review_wave_budget_block,
    skill_wave_purchased as _wave_purchased,
)


def _is_module_widget_skill(skill: Any) -> bool:
    return (
        skill.manifest.is_extension()
        and isinstance(skill.manifest.ui_tab, dict)
        and str(((skill.manifest.ui_tab or {}).get("render") or {}).get("kind") or "") == "module"
    )


def _run_deterministic_preflight(
    ctx: Any,
    drive_root: pathlib.Path,
    skill: Any,
    content_hash: str,
    *,
    persist: bool,
    binding: ResolvedResourceBinding | None = None,
) -> Optional[SkillReviewOutcome]:
    """Run deterministic checks before spending panel review tokens.

    FAIL-CLOSED (ABI-1): an exception or unparseable output from the
    preflight machinery is a structural failure of the gate itself, never a
    silent pass — in the owner-attestation path this preflight is the ENTIRE
    replacement for the LLM review, so failing open there would mint trust
    from a broken gate.
    """
    preflight_raw = ""
    try:
        from ouroboros.tools.skill_preflight import _handle_skill_preflight
        preflight_raw = _handle_skill_preflight(
            ctx, skill=skill.name, _resolved_binding=binding,
        )
        preflight = json.loads(preflight_raw)
    except Exception as exc:
        # Infrastructure failure of the gate itself: fail closed WITHOUT
        # persisting — a transient breakage must not clobber live review state
        # the way a genuine payload gate failure (below) deliberately does.
        return SkillReviewOutcome(
            skill_name=skill.name,
            status=STATUS_PENDING,
            content_hash=content_hash,
            error=(
                "deterministic preflight infrastructure failure "
                f"(fail-closed, nothing persisted): {type(exc).__name__}: {exc}"
            ),
        )
    if not isinstance(preflight, dict):
        preflight = {"ok": False, "error": "deterministic preflight returned a non-object result (fail-closed)"}
    if preflight.get("ok", True):
        return None
    findings = [{
        "item": "skill_preflight",
        "verdict": "FAIL",
        "severity": "critical",
        "reason": _truncate_raw_result(json.dumps(preflight, ensure_ascii=False)),
        "model": "deterministic_preflight",
    }]
    # A deterministic preflight failure is a structural fact, not an LLM verdict,
    # so it is persisted as PENDING (non-executable outside Cyber Pro, in every
    # readiness/execution caller) rather than BLOCKERS (which advisory
    # enforcement would let an operator override). The skill_preflight FAIL finding
    # still records exactly why, just like the other PENDING review-time failures.
    outcome = SkillReviewOutcome(
        skill_name=skill.name,
        status=STATUS_PENDING,
        findings=findings,
        reviewer_models=["deterministic_preflight"],
        content_hash=content_hash,
        error="deterministic skill_preflight failed before LLM review; the review stays PENDING",
        raw_result=preflight_raw,
    )
    if persist:
        review_state = SkillReviewState(
            status=outcome.status,
            content_hash=content_hash,
            findings=findings,
            reviewer_models=outcome.reviewer_models,
            timestamp=utc_now_iso(),
            prompt_chars=0,
            cost_usd=0.0,
            raw_result=outcome.raw_result,
            raw_actor_records=[],
        )
        save_review_state(
            drive_root,
            skill.name,
            review_state,
        )
        if not getattr(ctx, "_skill_review_lifecycle_guard", False):
            _append_skill_review_history(
                drive_root,
                skill.name,
                status=outcome.status,
                content_hash=content_hash,
                findings=findings,
            )
        skill.review = review_state
        # Record what the skill requests (transparency in the review block) but
        # NEVER auto-grant a skill that FAILED the deterministic preflight gate
        # (invalid manifest, sensitive-shaped file, binary payload, path escape).
        # The PENDING status already makes it non-executable outside Cyber Pro; recording
        # the requested keys/permissions just keeps the review block honest about
        # what the skill wanted.
        from ouroboros.skill_loader import (
            requested_core_setting_keys,
            requested_skill_permissions,
        )
        manifest = getattr(skill, "manifest", None)
        outcome.requested_keys = requested_core_setting_keys(
            list(getattr(manifest, "env_from_settings", []) or [])
        )
        outcome.requested_permissions = requested_skill_permissions(
            list(getattr(manifest, "permissions", []) or []),
            list(getattr(manifest, "subscribe_events", []) or []),
        )
    return outcome


def run_acceptance_preflight(ctx: Any, drive_root: pathlib.Path, skill_name: str) -> Dict[str, Any]:
    """The deterministic floor for bytes a source's trust accepts without a review.

    Installers call it after landing a trusted release, inside their lifecycle job.
    A payload failure persists the ordinary PENDING preflight record for these bytes,
    which the gate honours before the install basis; an infrastructure failure persists
    nothing and returns ``ok: False`` so the installer keeps the release off.
    """
    from ouroboros.skill_loader import find_skill

    skill = find_skill(pathlib.Path(drive_root), skill_name)
    if skill is None or skill.load_error or not skill.content_hash:
        return {"ok": False, "error": getattr(skill, "load_error", "") or f"skill {skill_name!r} not found"}
    outcome = _run_deterministic_preflight(ctx, pathlib.Path(drive_root), skill, skill.content_hash, persist=True)
    if outcome is not None:
        return {"ok": False, "error": outcome.error, "content_hash": skill.content_hash}
    return {"ok": True, "content_hash": skill.content_hash}


def _admission_gate(
    ctx: Any, skill: Any, drive_root: pathlib.Path, content_hash: str, persist: bool,
) -> Optional[SkillReviewOutcome]:
    """ABI-1 admission at NEW-PASS issuance, checked BEFORE dispatching a paid
    panel that could never mint a PASS for these bytes ($0 typed refusal; the
    byte-identical free replay in the cycles gate still serves a grandfathered
    PASS first)."""
    from ouroboros.contracts.plugin_api import extension_new_pass_admission_error

    admission_error = extension_new_pass_admission_error(skill.manifest)
    if not admission_error:
        return None
    return _admission_refusal_outcome(
        ctx, skill, drive_root,
        content_hash=content_hash,
        admission_error=admission_error,
        persist=persist,
    )


# Public entry point


def _skill_cycles_gate(
    ctx: Any,
    skill: Any,
    drive_root: pathlib.Path,
    models: List[str],
    delivery: Dict[str, Any],
    review_rebuttal: str,
    content_hash: str,
    *,
    persist: bool = True,
    file_packs: Optional[List[str]] = None,
    resume_only: bool = False,
    previous: Optional[Dict[str, Any]] = None,
) -> tuple[Optional["SkillReviewOutcome"], str, str, str]:
    """Select exact late reconciliation or free substantive replay before paid
    admission. The shared cap counts per root task, or per manual content hash.
    Return (early outcome, contract fingerprint, rebuttal hash, shared profile).
    New reviews carry no profile: a source's trust is an install basis read by
    the gate, never a softer review (persisted ``official_hub`` verdicts keep
    their recorded aggregation)."""
    from ouroboros.skill_review_cycles import (
        free_replay_outcome,
        skill_review_contract_fingerprint,
        skill_review_cycles_refusal,
        skill_review_wave_binding, select_skill_review_resume,
    )
    from ouroboros.tools.commit_gate import compute_rebuttal_sha256

    review_profile = ""
    contract_fp = skill_review_contract_fingerprint(
        models, required_items=_SKILL_REVIEW_ITEMS, review_profile=review_profile,
        delivery=delivery,
    )
    rebuttal_sha = compute_rebuttal_sha256(review_rebuttal)
    group_id = str(getattr(ctx, "_skill_review_group_id", "") or "") or f"manual:{skill.name}"
    if file_packs is not None and select_skill_review_resume(ctx, skill, drive_root,
            skill_review_wave_binding(ctx, skill, drive_root, content_hash, contract_fp,
                                      rebuttal_sha, file_packs), previous):
        return None, ctx._skill_review_resume["binding"]["review_contract_fingerprint"], rebuttal_sha, review_profile
    if resume_only:  # failed current preflight or settings may still collect the paid wave
        return None, contract_fp, rebuttal_sha, review_profile
    replayed = free_replay_outcome(
        skill, drive_root=drive_root, group_id=group_id, content_hash=content_hash,
        contract_fingerprint=contract_fp, rebuttal_sha256=rebuttal_sha,
    )
    if replayed is not None:
        allowed = persist
        if allowed and getattr(ctx, "_skill_review_lifecycle_guard", False):
            from ouroboros.skill_review_runner import _can_persist_review_outcome

            allowed = _can_persist_review_outcome(
                drive_root, skill.name, content_hash,
                expected_job_id=str(getattr(ctx, "_skill_review_lifecycle_job_id", "") or ""),
            )
        if allowed:
            # Reuse the existing verdict while reconciling current grant policy
            # and authored-work eligibility; no new panel or review-state write.
            _apply_auto_grant_outcome(replayed, skill, auto_grant_if_enabled(drive_root, skill))
        return replayed, contract_fp, rebuttal_sha, review_profile
    refusal = skill_review_cycles_refusal(
        ctx, skill.name, drive_root=drive_root, group_id=group_id, models=models,
        content_hash=content_hash, contract_fingerprint=contract_fp,
    )
    return refusal, contract_fp, rebuttal_sha, review_profile


def _stamp_paid_facts(
    outcome: "SkillReviewOutcome",
    contract_fp: str,
    rebuttal_sha: str,
    stamp: Any = None,
) -> "SkillReviewOutcome":
    """Max-Review-Cycles facts for a post-panel outcome: the contract and
    rebuttal and wave identities always ride it; ``paid`` only when the wave
    PHYSICALLY dispatched — ``stamp.fired`` mirrors the durable
    write-ahead dispatch marker recorded before the first transport call
    (plan-review precedent), so a crash cannot launder the spend and an
    assembly-refused $0 wave never counts."""
    outcome.paid = _wave_purchased(stamp)
    outcome.review_contract_fingerprint = contract_fp
    outcome.rebuttal_sha256 = rebuttal_sha
    if stamp is not None:
        outcome.wave_id = str(getattr(stamp, "wave_id", "") or "")
    return outcome


def _skill_quorum_failure_outcome(
    skill: Any,
    *,
    findings: List[Dict[str, Any]],
    models: List[str],
    content_hash: str,
    required_quorum: int,
    result_json_text: str,
    parsed_review: Any,
    advisory_evidence: Dict[str, Any],
    single_reviewer_no_diversity: bool,
    drive_root: pathlib.Path,
    persist: bool,
    lifecycle_owns_history: bool = False,
    paid: bool = False,
    review_contract_fingerprint: str = "",
    rebuttal_sha256: str = "",
    wave_id: str = "",
    panel: Optional[Dict[str, Any]] = None,
) -> SkillReviewOutcome:
    """PENDING outcome for a skill review that missed the adaptive reviewer quorum,
    preserving the single-reviewer degraded marker on the outcome AND the durable
    history (extracted to keep ``review_skill`` under the function-size gate).
    The caller passes the Max-Review-Cycles paid facts (F3): the internal
    history append must carry them — a dispatched quorum failure spent
    reviewer money, and an unpaid row here would let the direct
    ``review_skill(persist=True)`` path (marketplace install) launder it."""
    outcome = SkillReviewOutcome(
        skill_name=skill.name,
        status=STATUS_PENDING,
        findings=findings,
        reviewer_models=models,
        content_hash=content_hash,
        single_reviewer_no_diversity=single_reviewer_no_diversity,
        error=(
            f"Skill review quorum failure: fewer than {required_quorum} reviewers "
            "returned parseable findings. Raw result preserved."
        ),
        raw_result=_truncate_raw_result(result_json_text),
        raw_actor_records=[record.to_dict() for record in parsed_review.actor_records],
        advisory_result=advisory_evidence,
    )
    from ouroboros.review_ledger import panel_not_reached
    outcome.panel = {**dict(panel or {}), "not_reached": panel_not_reached(outcome.raw_actor_records)}
    if persist and not lifecycle_owns_history:
        _append_skill_review_history(
            drive_root,
            skill.name,
            status=outcome.status,
            content_hash=content_hash,
            findings=findings,
            raw_actor_records=[record.to_dict() for record in parsed_review.actor_records],
            single_reviewer_no_diversity=single_reviewer_no_diversity,
            paid=paid,
            review_contract_fingerprint=review_contract_fingerprint,
            rebuttal_sha256=rebuttal_sha256,
            wave_id=wave_id, panel=outcome.panel,
        )
    return outcome


def _skill_payload_packs(skill: Any, binding: ResolvedResourceBinding, content_hash: str, *, split: bool,
                         packet_seats: List[str]) -> tuple[List[str], Any, Optional[SkillReviewOutcome]]:
    """``(packs, skill, refusal)``: the hashed payload's review packs in one layout (``split``: packet-budget
    chunks) with the skill re-bound to the same manifest and bytes, or the typed pending refusal."""
    from ouroboros.skill_loader import SkillPayloadUnreadable
    code = ""  # only the live over-budget refusal is a typed no-dispatch refusal (SkillReviewOutcome)
    try:
        packs = _build_skill_file_packs(skill.skill_dir, manifest_entry=skill.manifest.entry,
                                        manifest_scripts=skill.manifest.scripts,
                                        expected_content_hash=content_hash, split=split)
        rebound = load_bound_skill(binding)
        if rebound is None or rebound.load_error:
            raise _SkillFileUnreadable("(payload snapshot)", RuntimeError("skill manifest changed after hashing"))
        if compute_content_hash(rebound.skill_dir, manifest_entry=rebound.manifest.entry,
                                manifest_scripts=rebound.manifest.scripts) != content_hash:
            raise _SkillFileUnreadable("(payload snapshot)", RuntimeError("skill payload changed after hashing"))
        return packs, rebound, None
    except _SkillFileOverBudget as exc:
        code = "LEGACY_BLOCKED"
        error = (f"Skill file {exc.relpath!r} alone is ~{exc.tokens} tokens, over the {exc.budget}-token "
                 f"packet budget: packet seat(s) {', '.join(packet_seats) or '(unresolved panel)'} cannot "
                 "receive it untruncated in any chunk, so no reviewer was dispatched and review stays "
                 "pending (it never truncates the executable surface). Shrink or split that one file.")
    except _SkillBinaryPayload as exc:
        error = (f"Skill file {exc.relpath!r} ({exc.size_bytes} bytes) is a loadable executable "
                 f"({exc.kind or 'native magic bytes'}); review hard-blocks native code the subprocess could load "
                 "via ctypes/import without reviewer inspection. Remove it or store it outside the hashed surface.")
    except (_SkillFileUnreadable, SkillPayloadUnreadable) as exc:
        error = (f"Skill file {exc.relpath!r} is unreadable ({type(exc.err).__name__}: {exc.err}). Review refuses "
                 "to fail open — fix the file permissions or remove the file before re-running skill_review.")
    return [], skill, SkillReviewOutcome(skill_name=skill.name, status=STATUS_PENDING, content_hash=content_hash,
                                         error=error, dispatch_refusal_code=code)


def review_skill(ctx: Any, skill_name: str, *, persist: bool = True, review_rebuttal: str = "",
                 _resolved_binding: ResolvedResourceBinding | None = None,
                 reviewers: Optional[List[str]] = None, reason: str = "") -> SkillReviewOutcome:
    """Admit one skill panel before preflight or review spend; preserve the job's snapshot."""
    from ouroboros.reviewer_slot_config import PanelCompositionError, compose_review_panel, composed_review_pool

    prior = getattr(ctx, "_skill_review_panel", None)
    try:
        panel = None if prior is not None else compose_review_panel(reviewers or (), reason, surface="skill")
    except PanelCompositionError as exc:
        return SkillReviewOutcome(skill_name=skill_name, status=STATUS_PENDING, error=str(exc),
                                  dispatch_refusal_code="TOOL_ARG_ERROR")
    except ValueError:
        panel = None  # an invalid pool reaches the review's own typed configuration refusal
    import contextlib
    with composed_review_pool(panel.seats) if panel is not None else contextlib.nullcontext():
        if panel is not None:
            ctx._skill_review_panel = panel.facts
        try:
            outcome = _review_skill(ctx, skill_name, persist=persist, review_rebuttal=review_rebuttal,
                                    _resolved_binding=_resolved_binding)
            if not outcome.panel:
                outcome.panel = dict(getattr(ctx, "_skill_review_panel", {}) or {})
            return outcome
        finally:
            if panel is not None:
                del ctx._skill_review_panel


def _review_skill(
    ctx: Any,
    skill_name: str,
    *,
    persist: bool = True,
    review_rebuttal: str = "",
    _resolved_binding: ResolvedResourceBinding | None = None,
) -> SkillReviewOutcome:
    """Run the admitted panel on one skill, optionally persisting its verdict."""
    from ouroboros.tools.review import _handle_multi_model_review
    try:
        binding = _resolved_binding or build_resolved_resource_binding(
            ctx, root="skill_payload", operation="review", path=".", skill_name=skill_name,
        )
    except Exception as exc:
        return SkillReviewOutcome(skill_name=skill_name, status=STATUS_PENDING, error=str(exc))
    drive_root = binding.state_drive_root
    skill = load_bound_skill(binding)
    if skill is None:
        return SkillReviewOutcome(skill_name=skill_name, status=STATUS_PENDING, error=f"Skill {skill_name!r} not found")
    if skill.load_error:
        return SkillReviewOutcome(skill_name=skill_name, status=STATUS_PENDING, error=f"Skill manifest could not be parsed: {skill.load_error}")
    from ouroboros.skill_loader import SkillPayloadUnreadable
    try:
        content_hash = compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        return SkillReviewOutcome(
            skill_name=skill.name,
            status=STATUS_PENDING,
            error=(
                f"Skill payload {exc.relpath!r} is unreadable "
                f"({type(exc.err).__name__}: {exc.err}). Review refuses "
                "to emit a PASS over a partial hash — fix file "
                "permissions or remove the unreadable file and re-run."
            ),
        )
    slot_err = reviewer_slot_config_error()
    try:
        delivery = {} if slot_err else commit_triad_delivery()
    except ValueError as exc:
        slot_err, delivery = str(exc), {}
    models = list(delivery.get("models") or [])
    from ouroboros.skill_review_cycles import preceding_review_row
    from ouroboros.skill_review_passes import panel_delivery_classes
    packet_seats, packet_api_models, native_models = panel_delivery_classes(delivery)
    split = bool(packet_seats) or not models
    # A paid wave of these bytes is collected in the layout it was bought in, whatever today's pool says.
    previous = preceding_review_row(ctx, drive_root, skill.name)
    recorded = ((previous or {}).get("review_wave") or {}).get("binding") or {}
    layout = len(recorded.get("chunks") or ()) > 1 if recorded.get("content_hash") == content_hash else split
    file_packs, skill, refused = _skill_payload_packs(skill, binding, content_hash, split=layout,
                                                      packet_seats=packet_seats)
    if refused is not None:
        return refused
    manifest_dump = json.dumps({
        "name": skill.manifest.name, "description": skill.manifest.description,
        "version": skill.manifest.version, "type": skill.manifest.type,
        "runtime": skill.manifest.runtime, "timeout_sec": skill.manifest.timeout_sec,
        "permissions": list(skill.manifest.permissions), "conflicts": list(skill.manifest.conflicts),
        "env_from_settings": list(skill.manifest.env_from_settings),
        "requires": list(skill.manifest.requires), "scripts": list(skill.manifest.scripts),
        "scheduled_tasks": list(getattr(skill.manifest, "scheduled_tasks", []) or []), "entry": skill.manifest.entry,
    }, ensure_ascii=False, indent=2)
    history = _load_skill_review_history(drive_root, skill.name, group_id=str(
        getattr(ctx, "_skill_review_group_id", "") or ""))
    composition_error = str((getattr(ctx, "_skill_review_panel", None) or {}).get("composition_error") or "")
    preflight_outcome = _run_deterministic_preflight(
        ctx, drive_root, skill, content_hash, persist=persist and not composition_error, binding=binding)
    try:
        current_hash = compute_content_hash(
            skill.skill_dir, manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts)
    except SkillPayloadUnreadable as exc:
        return SkillReviewOutcome(skill_name=skill.name, status=STATUS_PENDING,
                                  content_hash=content_hash, error=str(exc))
    if current_hash != content_hash:
        return SkillReviewOutcome(skill_name=skill.name, status=STATUS_PENDING, content_hash=content_hash,
            error="Skill payload changed during deterministic preflight; review did not dispatch.")
    early_outcome, contract_fp, rebuttal_sha, review_profile = _skill_cycles_gate(
        ctx, skill, drive_root, models, delivery, review_rebuttal, content_hash, persist=persist,
        file_packs=file_packs, resume_only=preflight_outcome is not None or bool(slot_err or composition_error), previous=previous)
    if composition_error and not getattr(ctx, "_skill_review_resume", None):
        return SkillReviewOutcome(skill_name=skill.name, status=STATUS_PENDING, content_hash=content_hash,
                                  error=composition_error, dispatch_refusal_code="TOOL_ARG_ERROR")
    if early_outcome is not None or (preflight_outcome is not None and not getattr(ctx, "_skill_review_resume", None)):
        return early_outcome or preflight_outcome
    if slot_err and not getattr(ctx, "_skill_review_resume", None):
        return SkillReviewOutcome(skill_name=skill.name, status=STATUS_PENDING, content_hash=content_hash,
            error=f"invalid review pool configuration blocks skill review: {slot_err}")
    if layout != split and not getattr(ctx, "_skill_review_resume", None):
        # That wave did not resume: a new one is laid out for today's pool and bound to its own packs.
        file_packs, skill, refused = _skill_payload_packs(skill, binding, content_hash, split=split,
                                                          packet_seats=packet_seats)
        if refused is not None:
            return refused
        from ouroboros.skill_review_cycles import skill_review_wave_binding
        ctx._skill_review_wave_binding = skill_review_wave_binding(
            ctx, skill, drive_root, content_hash, contract_fp, rebuttal_sha, file_packs)
    admission_outcome = _admission_gate(ctx, skill, drive_root, content_hash, persist)
    if admission_outcome is not None:
        return admission_outcome

    if len(file_packs) > 1:
        log.warning(
            "Skill %s exceeds the reviewer token budget; reviewing in %d chunked passes.",
            skill.name,
            len(file_packs),
        )
    # Budget admission for the WHOLE review wave (v6.69.0): a wave that cannot
    # fit the remaining root budget is declined up front (typed event, $0 spent,
    # skill honestly pending) instead of dying mid-wave. Fail-open on unknowns.
    budget_block = (
        # A native seat reads the whole payload once; a packet seat is priced per pack.
        _review_wave_budget_block(ctx, skill.name, file_packs, packet_api_models,
                                  **({"whole_models": native_models} if native_models else {}))
        if (packet_api_models or native_models) and not getattr(ctx, "_skill_review_resume", None) else None
    )
    if budget_block is not None:
        return SkillReviewOutcome(skill_name=skill.name, status=STATUS_PENDING,
                                  reviewer_models=models, content_hash=content_hash,
                                  error=budget_block)
    from ouroboros.skill_review_passes import retained_beside_preflight, run_skill_review_passes

    # F3 write-ahead seam: the durable dispatch marker lands immediately
    # before the panel's first transport call (assembly failures inside the
    # pass runner stay $0); every post-panel outcome below derives its paid
    # fact from whether the stamp actually fired.
    stamp, _prior_stamp = _install_skill_dispatch_stamp(
        ctx, drive_root, skill.name,
        group_id=str(getattr(ctx, "_skill_review_group_id", "") or "") or f"manual:{skill.name}",
        content_hash=content_hash, contract_fp=contract_fp, rebuttal_sha=rebuttal_sha,
    )

    def _paid_facts(outcome: SkillReviewOutcome) -> SkillReviewOutcome:
        from ouroboros.review_ledger import panel_not_reached
        resume = getattr(ctx, "_skill_review_resume", None) or {}
        outcome.panel = {**dict(resume.get("panel") or getattr(ctx, "_skill_review_panel", {}) or {}),
                         "not_reached": panel_not_reached(outcome.raw_actor_records)}
        return _stamp_paid_facts(outcome, contract_fp, rebuttal_sha, stamp)

    from ouroboros.skill_lifecycle_queue import lend_lifecycle_lane

    try:
        # The panel reads the frozen candidate bound to content_hash and mutates no
        # skill state, so other lifecycle work runs meanwhile; the lane is back
        # before anything below persists (Astra A01).
        with lend_lifecycle_lane():
            prompt, advisory_evidence, result_json_text, infra_error = run_skill_review_passes(
                ctx,
                drive_root,
                skill,
                evidence={
                    "manifest_dump": manifest_dump,
                    "content_hash": content_hash,
                    "history": history,
                    "review_rebuttal": review_rebuttal,
                    "required_items": _SKILL_REVIEW_ITEMS,
                },
                file_packs=file_packs,
                models=models,
                row_plan=delivery,
                session_root=str(_REPO_ROOT),
                usage_attribution={"review_skill": skill.name, "review_wave_id": stamp.wave_id}, review_contract_fingerprint=contract_fp, rebuttal_sha256=rebuttal_sha,
                build_prompt=_build_review_prompt_for_attempt,
                run_review=_handle_multi_model_review,
            )
    finally:
        ctx._review_paid_stamp = _prior_stamp
    if infra_error:
        log.warning("Skill review infrastructure failure for %s: %s", skill.name, infra_error)
        return _paid_facts(SkillReviewOutcome(
            skill_name=skill.name,
            status=STATUS_PENDING,
            reviewer_models=models,
            content_hash=content_hash,
            error=f"infrastructure failure: {sanitize_tool_result_for_log(infra_error)}",
        ))

    try:
        result_json = json.loads(result_json_text)
    except json.JSONDecodeError:
        return _paid_facts(SkillReviewOutcome(
            skill_name=skill.name,
            status=STATUS_PENDING,
            reviewer_models=models,
            content_hash=content_hash,
            error="review returned non-JSON top-level response",
            raw_result=_truncate_raw_result(result_json_text),
        ))

    if "error" in result_json:
        return _paid_facts(SkillReviewOutcome(
            skill_name=skill.name,
            status=STATUS_PENDING,
            reviewer_models=models,
            content_hash=content_hash,
            error=f"review service error: {result_json['error']}",
        ))

    models = list(result_json["recorded_models"]) if getattr(ctx, "_skill_review_resume", None) else models
    advisers = list(result_json.get("additional") or []) if getattr(ctx, "_skill_review_resume", None) else [
        sid for sid, extra in zip(delivery.get("slot_ids") or [], delivery.get("additional") or []) if extra]
    parsed_review = parse_model_review_results(result_json, required_items=_SKILL_REVIEW_ITEMS, additional=advisers)
    emit_review_model_error_events(ctx, parsed_review, source="skill_review", skill_name=skill.name)
    findings, responded_models = parsed_review.findings, parsed_review.responsive_models
    required_quorum = adaptive_quorum(len(models) - len(advisers))
    single_reviewer_no_diversity = len(models) - len(advisers) < 2
    if single_reviewer_no_diversity:
        # Skill review is an executable TRUST gate; a single configured reviewer
        # is honored but the lost diversity is recorded loudly AND durably (Bible
        # P3) — in the outcome + review history below, not just this log line.
        log.warning("Skill review (trust gate) ran with a single reviewer (single_reviewer_no_diversity).")
    if len(responded_models) < required_quorum or result_json.get("collection_pending"):
        return _paid_facts(_skill_quorum_failure_outcome(
            skill,
            findings=findings,
            models=models,
            content_hash=content_hash,
            required_quorum=required_quorum,
            result_json_text=result_json_text,
            parsed_review=parsed_review,
            advisory_evidence=advisory_evidence,
            single_reviewer_no_diversity=single_reviewer_no_diversity,
            drive_root=drive_root,
            persist=persist,
            lifecycle_owns_history=bool(getattr(ctx, "_skill_review_lifecycle_guard", False)),
            # F3(a): the internal history append must carry the paid facts —
            # a dispatched quorum failure spent reviewer money.
            paid=_wave_purchased(stamp),
            review_contract_fingerprint=contract_fp,
            rebuttal_sha256=rebuttal_sha,
            wave_id=str(getattr(stamp, "wave_id", "") or ""),
            panel=(getattr(ctx, "_skill_review_resume", None) or {}).get("panel") or getattr(ctx, "_skill_review_panel", {}),
        ))

    # review_profile was resolved ONCE in _skill_cycles_gate (it is part of
    # the panel-contract fingerprint) and rides down to aggregation here.
    status = _aggregate_status(
        findings,
        skill_type=skill.manifest.type,
        is_module_widget=_is_module_widget_skill(skill),
        review_profile=review_profile,
    )
    outcome = _paid_facts(SkillReviewOutcome(
        skill_name=skill.name,
        status=status,
        findings=findings,
        reviewer_models=responded_models,
        content_hash=content_hash,
        prompt_chars=len(prompt),
        single_reviewer_no_diversity=single_reviewer_no_diversity,
        raw_result=_truncate_raw_result(result_json_text),
        raw_actor_records=[record.to_dict() for record in parsed_review.actor_records],
        advisory_result=advisory_evidence,
        convergence_hint=_convergence_hint(history, findings, current_status=status),
        review_profile=review_profile,
    ))
    retained_beside_preflight(outcome, preflight_outcome)

    if persist:
        return _persist_reviewed_outcome(
            ctx, skill, drive_root, outcome,
            history=history, review_rebuttal=review_rebuttal,
            contract_fp=contract_fp, rebuttal_sha=rebuttal_sha,
        )
    return outcome


def _persist_reviewed_outcome(
    ctx: Any,
    skill: Any,
    drive_root: pathlib.Path,
    outcome: SkillReviewOutcome,
    *,
    history: List[Dict[str, Any]],
    review_rebuttal: str,
    contract_fp: str,
    rebuttal_sha: str,
) -> SkillReviewOutcome:
    """Persist one reviewed verdict (state, history, rebuttal flips, auto-grant)
    — extracted whole from ``review_skill`` at the function-size gate.

    The panel ran with the lifecycle lane lent out, so the payload may have moved
    on (an update, an edit, an uninstall): the verdict lands in review state only
    while the installed bytes are still the reviewed bytes."""
    content_hash = outcome.content_hash
    from ouroboros.skill_loader import SkillPayloadUnreadable

    try:
        current_hash = compute_content_hash(skill.skill_dir, manifest_entry=skill.manifest.entry,
                                            manifest_scripts=skill.manifest.scripts)
    except (OSError, SkillPayloadUnreadable):
        current_hash = ""
    if current_hash != content_hash:
        outcome.status = STATUS_PENDING
        outcome.error = ("review outcome was not persisted because the skill payload changed "
                         "while the review ran; review the current bytes")
        return outcome
    if getattr(ctx, "_skill_review_lifecycle_guard", False):
        from ouroboros.skill_review_runner import _can_persist_review_outcome

        if not _can_persist_review_outcome(
            drive_root,
            skill.name,
            content_hash,
            expected_job_id=str(getattr(ctx, "_skill_review_lifecycle_job_id", "") or ""),
        ):
            outcome.status = STATUS_PENDING
            outcome.error = (
                "review outcome was not persisted because the lifecycle job "
                "is already terminal or no longer matches this content hash"
            )
            return outcome
    persisted_state = SkillReviewState(
        status=outcome.status,
        content_hash=content_hash,
        findings=outcome.findings,
        reviewer_models=outcome.reviewer_models,
        timestamp=utc_now_iso(),
        prompt_chars=outcome.prompt_chars,
        cost_usd=outcome.cost_usd,
        raw_result=outcome.raw_result,
        raw_actor_records=list(outcome.raw_actor_records or []),
        advisory_result=dict(outcome.advisory_result or {}),
        review_profile=outcome.review_profile, panel=dict(outcome.panel),
    )
    save_review_state(drive_root, skill.name, persisted_state)
    if not getattr(ctx, "_skill_review_lifecycle_guard", False):
        _append_skill_review_history(
            drive_root, skill.name,
            status=outcome.status, content_hash=content_hash, findings=outcome.findings,
            raw_actor_records=list(outcome.raw_actor_records or []),
            single_reviewer_no_diversity=outcome.single_reviewer_no_diversity,
            paid=outcome.paid, review_contract_fingerprint=contract_fp,
            rebuttal_sha256=rebuttal_sha, wave_id=outcome.wave_id, panel=outcome.panel,
        )
    _persist_rebuttal_flips(
        drive_root, skill.name,
        history=history, findings=outcome.findings,
        review_rebuttal=review_rebuttal, content_hash=content_hash,
        responded_models=list(outcome.reviewer_models),
    )
    skill.review = persisted_state
    auto_grant = auto_grant_if_enabled(drive_root, skill)
    _apply_auto_grant_outcome(outcome, skill, auto_grant)
    return outcome


__all__ = [
    "SkillReviewOutcome",
    "render_skill_review_block",
    "review_skill",
    "run_acceptance_preflight",
]
