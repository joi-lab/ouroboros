"""Finite delegated-leaf continuation after a CONFIRMED wall-clock expiry (#1196).

A delegated run started with ``maxSeconds`` is cancelled by the engine when that
cap expires and settles ``cancelled`` with the engine's typed reason
``wall_clock_exceeded`` (recorded on the SETTLED custody row as
``outcome_reason`` and replayed as ``RunCustody.terminal_reason``). Such a run
did real work that the nanny may want finished. This module is the ONE gate a
``delegate_start(continue_from=<run_id>)`` passes before a NEW run is started
for the remaining work, and the ONE author of the host block that binds that
run to its predecessor.

It is deliberately NOT crash recovery and NOT a resume: ``delegate_recovery``
keeps its ``NO_RESUME_CAUSES`` untouched, nothing is replayed under an old key,
no engine session state is transferred, and the host never decides what the
remaining work is — the model writes the continuation prompt with the prior
result and its explicit patch disposition in front of it. The gate admits only:

* the caller's OWN settled run (custody says OWNED and SETTLED);
* whose terminal is ``cancelled`` with reason ``wall_clock_exceeded`` — an owner
  deadline, Stop or Panic (``host_cancelled``/``owner_task_gone``), a user cancel,
  a failure, an absent/unknown engine outcome or a pre-field settlement are
  refused typed, each with the fact that refused it — AND whose cap was a
  finite leaf cap the nanny ASKED for (``max_seconds_basis`` on the STARTED
  row: ``requested``, at most narrowed by the engine's schema bound): the
  engine's typed reason alone is not enough, because a cap the nanny's own
  deadline or lifetime derived or narrowed expiring IS that deadline, and a
  row with no recorded basis is unknown, never assumed;
* whose result is RETAINED (its terminal detail staged in full) AND READ to EOF
  (a run nobody waited on, a partial staging or an unread staging refuses:
  continuing work nobody has read is a blind resend);
* whose captured patch has an EXPLICIT apply/reject disposition (an undisposed
  or apply-ambiguous patch refuses: the continuation must know what the tree
  already contains, and nothing may be applied twice), and whose partial
  work order, if any, has fully verified source coverage;
* under POSITIVELY the same executor and authority: the recorded actor, route,
  configuration fingerprint, task authority fingerprint, access, mode and
  isolation must each be recorded AND equal to this start's (an unrecorded
  side never "does not contradict" — it refuses), a mutating run's authority
  target likewise, and a configured session's canonical work order must be
  the one the prior run was bound to; the continuation's own ``max_seconds``
  is narrowed by the nanny's remaining bounds exactly like any start.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from ouroboros import delegate_custody as custody

log = logging.getLogger(__name__)

# The engine's typed cancel reason for a maxSeconds expiry (Claudexor
# ``RunOutcomeReason``: a cancelled lifecycle with reason ``wall_clock_exceeded``).
CONTINUATION_CAUSE = "wall_clock_exceeded"
CONTINUATION_TERMINAL_STATE = "cancelled"

REFUSAL_SOURCE_UNKNOWN = "continuation_source_unknown"
REFUSAL_SOURCE_NOT_OWNED = "continuation_source_not_owned"
REFUSAL_SOURCE_NOT_TERMINAL = "continuation_source_not_terminal"
REFUSAL_CAUSE_UNRECORDED = "continuation_cause_unrecorded"
REFUSAL_CAUSE_NOT_WALL_CLOCK = "continuation_cause_not_wall_clock"
REFUSAL_RESULT_UNREAD = "continuation_result_unread"
REFUSAL_PATCH_UNDISPOSED = "continuation_patch_undisposed"
REFUSAL_APPLY_AMBIGUOUS = "continuation_apply_ambiguous"
REFUSAL_EXECUTOR_MISMATCH = "continuation_executor_mismatch"
REFUSAL_AUTHORITY_MISMATCH = "continuation_authority_mismatch"
REFUSAL_TARGET_MISMATCH = "continuation_target_mismatch"
REFUSAL_CAP_BASIS_UNKNOWN = "continuation_cap_basis_unknown"
REFUSAL_CAP_NOT_FINITE_LEAF = "continuation_cap_not_finite_leaf"
REFUSAL_RESULT_UNRETAINED = "continuation_result_unretained"
REFUSAL_RESULT_INCOMPLETE = "continuation_result_incomplete"
REFUSAL_SOURCE_UNVERIFIED = "continuation_source_unverified"
REFUSAL_CONFIG_MISMATCH = "continuation_config_mismatch"
REFUSAL_TASK_AUTHORITY_MISMATCH = "continuation_task_authority_mismatch"
REFUSAL_WORK_ORDER_UNBOUND = "continuation_work_order_unbound"
REFUSAL_WORK_ORDER_MISMATCH = "continuation_work_order_mismatch"


def bind_continuation(ctx: Any, drive: Any, run_id: str, *, actor: Dict[str, Any], route: Any,
                      authority: Any, target_root: str,
                      canonical_work_order_fingerprint: str = "") -> Tuple[Dict[str, Any], str, str]:
    """``(facts, refusal_code, detail)``: the typed gate, from durable custody only.

    ``facts`` is complete only when ``refusal_code`` is empty. Nothing here
    reads the engine: the SETTLED row, the STARTED row's cap basis, the
    retained output facts and the disposition rows are the whole authority, so
    a daemon that is gone cannot turn an unknown cause into an admitted
    continuation. Every binding fact is checked POSITIVELY: recorded on the
    prior run, present on this start, and equal.
    """
    from ouroboros.configured_subagents import SESSION_ACCESS_PROFILES
    from ouroboros.delegate_registration_policy import FINITE_LEAF_CAP_BASES

    rid = str(run_id or "").strip()
    task_id = str(getattr(ctx, "task_id", "") or "")
    status, entry = custody.lookup(drive, task_id, rid)
    if status == custody.UNKNOWN or entry is None:
        return {}, REFUSAL_SOURCE_UNKNOWN, (
            f"No durable custody record names run {rid!r} on this drive; a continuation "
            "binds to a run this task can prove it owns.")
    if status == custody.FOREIGN:
        return {}, REFUSAL_SOURCE_NOT_OWNED, (
            f"Run {rid} belongs to task {entry.task_id or 'unknown'}, not to this task; "
            "only its owner may continue it.")
    if not entry.settled or not entry.terminal_state:
        return {}, REFUSAL_SOURCE_NOT_TERMINAL, (
            f"Run {rid} has no settled terminal on its custody rows (state {entry.terminal_state or 'unsettled'!r}): "
            "it may still be live. Wait on it with delegate_wait or cancel it and verify the receipt; "
            "never start a second writer over a run that may still be running.")
    if not entry.terminal_reason:
        return {}, REFUSAL_CAUSE_UNRECORDED, (
            f"Run {rid} settled {entry.terminal_state!r} without a recorded engine reason, so a wall-clock "
            "expiry cannot be CONFIRMED; a continuation is admitted only over a confirmed maxSeconds expiry. "
            "Start a plain new run if the work is still needed, stating what the prior run left.")
    if entry.terminal_state != CONTINUATION_TERMINAL_STATE or entry.terminal_reason != CONTINUATION_CAUSE:
        return {}, REFUSAL_CAUSE_NOT_WALL_CLOCK, (
            f"Run {rid} ended {entry.terminal_state!r} with reason {entry.terminal_reason!r}, which is not a "
            f"maxSeconds expiry ({CONTINUATION_CAUSE}). An owner deadline, Stop or Panic, a user cancel or a "
            "failure is not continued through this seam.")
    started_ts, prior_max_seconds = custody.run_timing(drive, rid)
    # How the cap was decided (``delegate_registration_policy.CAP_BASIS_*``), from
    # the same durable STARTED row; "" when the row predates the field — an
    # absent basis stays absent (#1196).
    cap_basis = next((str(row.get("max_seconds_basis") or "") for row in custody.custody_rows(drive)
                      if str(row.get("run_id") or "") == rid and str(row.get("type") or "") == custody.STARTED
                      and row.get("max_seconds_basis")), "")
    if not cap_basis:
        return {}, REFUSAL_CAP_BASIS_UNKNOWN, (
            f"Run {rid}'s STARTED row records no basis for its maxSeconds cap, so its expiry cannot be told "
            "apart from this task's own deadline or lifetime. The engine's typed reason alone does not admit "
            "a continuation; start a plain new run for the remaining work.")
    if cap_basis not in FINITE_LEAF_CAP_BASES or int(prior_max_seconds or 0) <= 0:
        return {}, REFUSAL_CAP_NOT_FINITE_LEAF, (
            f"Run {rid}'s cap was {cap_basis!r} ({int(prior_max_seconds or 0)}s): derived from or narrowed by "
            "this task's own deadline or lifetime, not a finite leaf cap the nanny asked for. Its expiry IS "
            "that bound; it is not continued through this seam.")
    output = custody.output_disposition(entry)
    if not output:
        return {}, REFUSAL_RESULT_UNRETAINED, (
            f"Run {rid} settled without its terminal detail being staged: nothing of its result is retained "
            "on this drive. Wait on it with delegate_wait (which stages the full detail), read that to EOF, "
            "then continue — continuing work nobody has retained is a blind resend.")
    if not output.get("staged_output_complete"):
        return {}, REFUSAL_RESULT_INCOMPLETE, (
            f"Run {rid}'s staged output at {entry.output_artifact} is not verified FULL content; a preview "
            "or a cut staging is not the result. Re-wait so the complete detail is staged, read it, then continue.")
    if custody.settled_output_unread(entry) or not output.get("staged_output_consumed"):
        return {}, REFUSAL_RESULT_UNREAD, (
            f"Run {rid}'s full output is staged at {entry.output_artifact} and was never read to EOF; "
            "read it with read_file(root='task_drive') first — continuing work nobody has read is a blind resend.")
    if entry.patch_apply_pending:
        return {}, REFUSAL_APPLY_AMBIGUOUS, (
            f"Run {rid} has a pending apply intent with no disposition: the tree MAY already carry its patch. "
            "Resolve it through integrate_delegated_patch(acknowledge_ambiguous=true) before continuing.")
    # The prior run's work reaches the tree only through an explicit disposition
    # when it was captured: an execution snapshot, or a copied directory workspace.
    ref = entry.resource_ref if isinstance(entry.resource_ref, dict) else {}
    needs_disposition = bool(entry.snapshot_id or (
        ref.get("workspace_kind") == "directory" and ref.get("strategy") == "copy"))
    if needs_disposition and not entry.patch_disposed:
        return {}, REFUSAL_PATCH_UNDISPOSED, (
            f"Run {rid}'s captured changes have no explicit disposition yet. Apply or reject them with "
            "integrate_delegated_patch(run_id=...) first, so the continuation knows what the tree contains "
            "and nothing is applied twice.")
    verification = custody.work_order_source_verification(entry)
    if str(verification.get("status") or "") == "cannot_verify":
        return {}, REFUSAL_SOURCE_UNVERIFIED, (
            f"Run {rid}'s external work order was only partially delivered and its canonical source ranges "
            "are not fully verified; a continuation cannot bind to a brief nobody has proven complete.")
    prior_actor = str(entry.selected_subagent_id or "")
    this_actor = str((actor or {}).get("selected_subagent_id") or "")
    route_id = str(getattr(route, "route_id", "") or "")
    if not prior_actor or not this_actor or prior_actor != this_actor \
            or not entry.route_id or not route_id or entry.route_id != route_id:
        return {}, REFUSAL_EXECUTOR_MISMATCH, (
            f"Run {rid} ran on actor {prior_actor or 'unrecorded'!r} via route {entry.route_id or 'unrecorded'!r}; "
            f"this start resolves to actor {this_actor or 'unrecorded'!r} via route {route_id or 'unrecorded'!r}. "
            "A continuation keeps the SAME recorded executor on both sides; select that actor or start a plain new run.")
    this_config = str((actor or {}).get("config_fingerprint") or "")
    if not entry.config_fingerprint or not this_config or entry.config_fingerprint != this_config:
        return {}, REFUSAL_CONFIG_MISMATCH, (
            f"Run {rid} was started from actor configuration {entry.config_fingerprint or 'unrecorded'!r}; this "
            f"start carries {this_config or 'unrecorded'!r}. A continuation runs the SAME recorded configuration.")
    this_authority = str((actor or {}).get("authority_fingerprint") or "")
    if not entry.authority_fingerprint or not this_authority or entry.authority_fingerprint != this_authority:
        return {}, REFUSAL_TASK_AUTHORITY_MISMATCH, (
            f"Run {rid} was started under task authority {entry.authority_fingerprint or 'unrecorded'!r}; this "
            f"start derives {this_authority or 'unrecorded'!r}. A continuation runs under the SAME recorded "
            "task authority (contract, constraint, workspace); it cannot be re-derived.")
    if not entry.work_order_fingerprint:
        return {}, REFUSAL_WORK_ORDER_UNBOUND, (
            f"Run {rid}'s STARTED row binds no work order; a continuation follows a run whose assignment is "
            "recorded. Start a plain new run stating the remaining work.")
    canonical = str(canonical_work_order_fingerprint or "")
    if canonical and canonical != entry.work_order_fingerprint:
        return {}, REFUSAL_WORK_ORDER_MISMATCH, (
            f"This configured session's canonical work order ({canonical[:12]}…) is not the one run {rid} was "
            f"bound to ({entry.work_order_fingerprint[:12]}…). A continuation stays inside the SAME assignment.")
    shape = {key: str(getattr(authority, key, "") or "") for key in ("access", "mode", "isolation")}
    prior_shape = {"access": entry.access, "mode": entry.mode, "isolation": entry.isolation}
    if (not prior_shape["access"] or not prior_shape["mode"] or not shape["access"] or not shape["mode"]
            or any(prior_shape[key] != shape[key] for key in shape)):
        return {}, REFUSAL_AUTHORITY_MISMATCH, (
            f"Run {rid}'s authority shape was {prior_shape}; this start derives {shape}. A continuation runs "
            "under the SAME recorded workspace authority; it cannot widen, reshape or leave it unrecorded.")
    if entry.access in SESSION_ACCESS_PROFILES and (
            not entry.target_root or not target_root or entry.target_root != target_root):
        return {}, REFUSAL_TARGET_MISMATCH, (
            f"Run {rid} held authority over {entry.target_root or 'an unrecorded target'}; this task's present "
            f"target is {target_root or 'unrecorded'}. A continuation writes for the target the prior run held, "
            "never a tree it has since moved to.")
    facts: Dict[str, Any] = {
        "continuation_of": rid,
        "cause": entry.terminal_reason,
        "prior_terminal_state": entry.terminal_state,
        "prior_started_at": started_ts,
        "prior_max_seconds": int(prior_max_seconds or 0) or None,
        "prior_cap_basis": cap_basis,
        "prior_patch_disposition": entry.patch_disposed or ("not_applicable" if not needs_disposition else ""),
        "prior_target_root": entry.target_root,
        "prior_access": entry.access,
        "prior_baseline_sha": entry.baseline_sha,
        "prior_output": output,
        "prior_invocation_id": entry.invocation_id,
        "prior_actor": prior_actor,
        "prior_route": entry.route_id,
        "prior_config_fingerprint": entry.config_fingerprint,
        "prior_authority_fingerprint": entry.authority_fingerprint,
        "prior_work_order_fingerprint": entry.work_order_fingerprint,
        "canonical_work_order_fingerprint": canonical,
        "state_transfer": "none",
    }
    return facts, "", ""


def continuation_instruction(facts: Dict[str, Any]) -> str:
    """The host block appended to the run's instructions: what is bound, what is not."""
    disposition = str(facts.get("prior_patch_disposition") or "")
    if disposition == "applied":
        tree_line = ("Its captured changes were explicitly APPLIED to the authority target: the tree you start "
                     "from already contains them. Do not redo or re-apply that work.")
    elif disposition == "rejected":
        tree_line = ("Its captured changes were explicitly REJECTED: the tree you start from does NOT contain "
                     "them, and only the assignment in the prompt says what is still wanted.")
    elif str(facts.get("prior_access") or "readonly") == "readonly":
        tree_line = "It captured no changes to dispose of (a read-only run)."
    else:
        # A mutating run with nothing to dispose wrote IN PLACE (a direct
        # directory strategy, a session without an execution snapshot): its
        # effects are already on the target, not absent.
        tree_line = ("It wrote DIRECTLY into the authority target with no captured patch: the tree you "
                     "start from already contains whatever it changed. Do not redo or re-apply that work.")
    cap = facts.get("prior_max_seconds")
    cap_line = f" after its {int(cap)}s wall-clock cap" if cap else " at its wall-clock cap"
    return (
        f"\n\nCONTINUATION OF RUN {facts.get('continuation_of')}: that run was cancelled by the engine{cap_line} "
        f"(reason {facts.get('cause')}); it is settled and is NOT running. {tree_line} "
        "NOTHING of its session state is transferred to you: no transcript, no memory, no assumptions. "
        "The prompt states the remaining work as the host decided it; verify what is already done from the "
        "workspace as it is NOW before repeating any step, never assume a result you cannot see in the tree, "
        "and never re-apply what was applied. This run has its own wall-clock cap; finish the remaining work "
        "or report exactly what remains."
    )


def start_binding(ctx: Any, drive: Any, token: str, *, actor: Dict[str, Any], route: Any,
                  authority: Any, target_root: str,
                  canonical_work_order_fingerprint: str = "") -> Tuple[Dict[str, Any], str, Optional[Any]]:
    """``(facts, instruction, refusal)`` for ONE start: the gate plus its host block.

    Decided from durable custody before any snapshot or registration exists; a
    refusal is a definite no-run recorded as a start-blocked evidence row.
    """
    from ouroboros.delegate_evidence import record_start_blocked
    from ouroboros.delegate_shared import _fail

    facts, code, detail = bind_continuation(
        ctx, drive, token, actor=actor, route=route, authority=authority, target_root=target_root,
        canonical_work_order_fingerprint=canonical_work_order_fingerprint)
    if code:
        record_start_blocked(ctx, str(getattr(ctx, "task_id", "") or ""), code)
        return {}, "", _fail("delegate_start", code, detail, continue_from=token, definitely_unrun=True)
    return facts, continuation_instruction(facts), None


__all__ = [
    "CONTINUATION_CAUSE",
    "CONTINUATION_TERMINAL_STATE",
    "bind_continuation",
    "continuation_instruction",
    "start_binding",
]
