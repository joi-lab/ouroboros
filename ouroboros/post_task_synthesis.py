"""Post-task synthesis workers for the task pipeline (v7 L-C2 split).

The best-effort work the post-task orchestrator
(``agent_task_pipeline._run_post_task_processing_async``) dispatches after a
task ends: the tool-trace summary, the free host facts row, the fallback
memory draft (only while consciousness is off) and scratchpad compression,
plus the shared pre-synthesis usage snapshot and the compact review
projection. No stage writes lessons, the Pattern Register or the backlog:
the acting mind authors those (``knowledge_write``). Extracted from
agent_task_pipeline.py; the pipeline re-exports every name, so historical
imports and monkeypatch targets keep working."""

from __future__ import annotations

import json
import logging
import pathlib

from dataclasses import replace
from typing import Any, Callable, Dict
from ouroboros.dialogue_provenance import presence_provenance_fields
from ouroboros.llm_claudexor import propagate_model_error
from ouroboros.observability import without_finalization_timing
from ouroboros.outcomes import normalize_outcome_axes
from ouroboros.subagent_messages import initiator_meta
from ouroboros.synthesis_cost_text import _summary_row_cost_fields
from ouroboros.tool_capabilities import routing_action_for_tool
from ouroboros.utils import append_jsonl, truncate_review_artifact as _truncate_with_notice, utc_now_iso


log = logging.getLogger("ouroboros.agent_task_pipeline")


def _atp():
    """The parent module, read at call time.

    The parent owns the rebindable module state and the members tests
    monkeypatch there; reading them through the module at each call keeps
    one binding, where a from-import would freeze the value this leaf saw
    at import time (the owner-approved D18/D33 mechanical exception).
    """
    from ouroboros import agent_task_pipeline

    return agent_task_pipeline


def task_tool_metrics(llm_trace: dict) -> dict:
    """Project recorded calls once; unknown names never become an empty census."""
    unavailable = bool(llm_trace.get("loop_evidence_unavailable"))
    calls = llm_trace.get("tool_calls") or []
    metrics = {
        "tool_calls": None if unavailable else len(calls),
        "tool_errors": None if unavailable else sum(
            1 for call in calls if isinstance(call, dict) and call.get("is_error")),
        # The addressing calls among them (tool_capabilities owns the family), so
        # a replayed block can tell a receipt-only turn from real work without
        # a client list of tool names.
        "routing_tool_calls": None if unavailable else sum(
            1 for call in calls if isinstance(call, dict) and routing_action_for_tool(call.get("tool"))),
        "completion_tool_calls": None if unavailable else sum(
            1 for call in calls if isinstance(call, dict) and call.get("completion_control") is True and not call.get("is_error")),
        "tool_call_counts": None,
    }
    if unavailable or llm_trace.get("recovered_post_task_synthesis") or not isinstance(llm_trace.get("tool_calls"), list):
        return metrics
    counts: dict[str, int] = {}
    for call in calls:
        name = call.get("tool") if isinstance(call, dict) else None
        if not isinstance(name, str) or not name.strip():
            return metrics
        name = name.strip()
        counts[name] = counts.get(name, 0) + 1
    metrics["tool_call_counts"] = counts
    return metrics


def capture_task_inputs(ctx: Any, task: dict, drive_root: Any, receipts: list) -> dict:
    """Freeze the existing owner corpus and check receipts before actor cleanup."""
    from ouroboros._outcome_receipts import verification_receipt_ledger_row
    from ouroboros.observability import redact_projection
    from ouroboros.review_evidence_sections import (
        _accept_owner_directives, _accept_verification_summary,
    )

    task_id = str(task.get("id") or task.get("task_id") or "")
    result: dict = {
        "version": 1, "task_id": task_id,
        "source_ref": {"kind": "task_result", "task_id": task_id, "reader": "get_task_result"},
        "unavailable_sections": [],
    }
    try:
        # The run's provenance comes first: a reader learns who started the run
        # and whether the owner door stamped it before it reads the first text.
        from ouroboros.dialogue_provenance import run_origin

        metadata = getattr(ctx, "task_metadata", None)
        result["run_origin"] = run_origin({
            **task, "metadata": metadata if isinstance(metadata, dict) else task.get("metadata"),
        })
    except Exception:
        result["unavailable_sections"].append("run_origin")
        log.warning("Task run origin unavailable for synthesis: %s", task_id, exc_info=True)
    try:
        result["owner_requirements_and_decisions"] = _accept_owner_directives(ctx, drive_root, task_id)
    except Exception:
        result["unavailable_sections"].append("owner_requirements_and_decisions")
        log.warning("Task owner input unavailable for synthesis: %s", task_id, exc_info=True)
    try:
        result["verification_summary"] = _accept_verification_summary(receipts)
        result["verification_receipts"] = [
            {"ts": row.get("ts"), **verification_receipt_ledger_row(row)}
            for row in receipts if isinstance(row, dict)
        ]
    except Exception:
        result["unavailable_sections"].append("verification_receipts")
        log.warning("Task verification input unavailable for synthesis: %s", task_id, exc_info=True)
    # Copy once into the normal durable completion package. Prompt workers must
    # neither retain mutable actor objects nor re-read a later task's messages.
    return json.loads(json.dumps(redact_projection(result).value, ensure_ascii=False, default=str))



def build_trace_summary(llm_trace: dict) -> str:
    """Return the bounded, human-readable preview of tool calls and agent notes.

    It is what is stored and shown (task card, parents, children): two arguments per
    call, a positional window past thirty calls, a total cut. Values stay width-bounded,
    with a disclosed omission; the trace itself keeps every call.
    """
    if llm_trace.get("loop_evidence_unavailable"):
        return "## Tool trace (call count unknown)\nThe failed loop supplied no verified execution trace."
    tool_calls = llm_trace.get("tool_calls", []) or []
    notes = llm_trace.get("reasoning_notes", []) or []

    n = len(tool_calls)
    # Honest breakdown so a task that finished with a deliverable is not mislabeled
    # "43 errors": separate GENUINE unresolved errors from POLICY denials, cosmetic
    # non-zero exits, recovered errors, and ignored read-only blocks.
    from ouroboros.outcomes import _classify_tool_errors

    _buckets = _classify_tool_errors(llm_trace)
    _unresolved = len(_buckets.get("unresolved") or [])
    _policy = len(_buckets.get("policy_denials") or [])
    _cosmetic = len(_buckets.get("cosmetic") or [])
    _recovered = len(_buckets.get("recovered") or [])
    _ignored = len(_buckets.get("ignored") or [])
    _breakdown_bits = [f"{_unresolved} errors"]
    if _policy:
        _breakdown_bits.append(f"{_policy} policy-denied")
    if _recovered:
        _breakdown_bits.append(f"{_recovered} recovered")
    if _cosmetic:
        _breakdown_bits.append(f"{_cosmetic} cosmetic")
    if _ignored:
        _breakdown_bits.append(f"{_ignored} ignored")

    lines: list[str] = [f"## Tool trace ({n} calls, {', '.join(_breakdown_bits)})"]

    if not tool_calls:
        lines.append("No tool calls.")
    else:
        def _fmt_call(index: int, tc: dict) -> str:
            name = tc.get("tool", "unknown")
            args = tc.get("args", {})
            if isinstance(args, dict):
                parts = []
                arg_items = list(args.items())
                for k, v in arg_items[:2]:
                    v_str = str(v)
                    if len(v_str) > 200:
                        v_str = _truncate_with_notice(v_str, 200).replace("\n", " ")
                    parts.append(f"{k}={v_str!r}")
                if len(arg_items) > 2:
                    parts.append(f"⚠️ OMISSION NOTE: {len(arg_items) - 2} more args omitted")
                args_str = ", ".join(parts)
            else:
                args_str = repr(args)
                if len(args_str) > 200:
                    args_str = _truncate_with_notice(args_str, 200).replace("\n", " ")
            facts = []
            status = str(tc.get("status") or "").strip()
            if status:
                facts.append(f"status={status}")
            if tc.get("exit_code") is not None:
                facts.append(f"exit_code={tc.get('exit_code')}")
            if tc.get("signal"):
                facts.append(f"signal={tc.get('signal')}")
            fact_suffix = f" [{', '.join(facts)}]" if facts else ""
            suffix = " → ERROR" if tc.get("is_error") else ""
            return f"{index + 1}. {name}({args_str}){fact_suffix}{suffix}"

        if n > 30:
            shown = (
                [_fmt_call(i, tool_calls[i]) for i in range(15)]
                + [f"⚠️ OMISSION NOTE: {n - 30} middle tool calls omitted from trace summary."]
                + [_fmt_call(n - 15 + i, tool_calls[n - 15 + i]) for i in range(15)]
            )
        else:
            shown = [_fmt_call(i, tool_calls[i]) for i in range(n)]
        lines.extend(shown)

    if notes:
        lines.append("\n## Agent notes (supplementary, not source of truth)")
        lines.extend(f"- {note}" for note in notes)

    summary = "\n".join(lines)
    if len(summary) > 4000:
        summary = _truncate_with_notice(summary, 4000)
    return summary


def _pre_synthesis_usage_snapshot(
    env: Any,
    task: Dict[str, Any],
    usage: Dict[str, Any],
) -> Dict[str, Any]:
    """Freeze one honest, non-final root/subtree cost view for the post-task phase.

    The facts row reads this loop-local dictionary. The existing terminal
    checkpoint remains the sole final authority after later model calls settle.
    """
    snapshot = json.loads(json.dumps(without_finalization_timing(usage), ensure_ascii=False, default=str))
    if not _atp()._is_root_post_task(task):
        return snapshot

    task_id = str(task.get("id") or task.get("task_id") or "")
    budget_root = pathlib.Path(
        task.get("budget_drive_root") or getattr(env, "drive_root", ".")
    )
    snapshot.update({
        "cost_snapshot_at": utc_now_iso(),
        "cost_final": False,
        "cost_with_children_partial": True,
    })
    try:
        from ouroboros.usage_accounting import usage_breakdown
        from ouroboros.cost_projection import COST_SCOPE_ROOT_TREE, build_cost_presentation

        logical_root_id = str(task.get("root_task_id") or task_id)
        subtree = usage_breakdown(budget_root, root_task_id=logical_root_id)
        snapshot.update({
            "accounted_upper_bound_usd_with_children": round(float(subtree["accounted_usd"]), 6),
            "cost_presentation": build_cost_presentation(subtree, scope=COST_SCOPE_ROOT_TREE),
            "reserved_usd": round(float(subtree["reserved_usd"]), 6),
            "unresolved_upper_bound_usd": round(
                float(subtree["unresolved_upper_bound_usd"]), 6
            ),
            "unknown_unmetered": int(subtree["unknown_unmetered"]),
            "ledger_integrity": (
                "degraded" if bool(subtree.get("integrity_degraded")) else "ok"
            ),
            "cost_accounting_status": "available",
        })
    except Exception:
        log.warning(
            "Pre-synthesis subtree cost is unavailable for %s",
            task_id or "unknown",
            exc_info=True,
        )
        snapshot.update({
            "accounted_upper_bound_usd_with_children": None,
            "reserved_usd": None,
            "unresolved_upper_bound_usd": None,
            "unknown_unmetered": None,
            "ledger_integrity": "unavailable",
            "cost_accounting_status": "unavailable",
            "cost_presentation": None,
        })
    return snapshot


def _compact_review_projection(llm_trace: Dict[str, Any], task: Dict[str, Any] | None = None,
                               drive_root: Any = None) -> Dict[str, Any]:
    """Build the public review projection without copying raw actor output: the trace's
    acceptance runs, then this task's own review-ledger records (``task_ledger_records``)."""
    try:
        from ouroboros.review_projection import task_ledger_records
        from ouroboros.review_substrate import compact_review_projection

        records, omitted = task_ledger_records(task or {}, drive_root)
        projection = compact_review_projection(llm_trace.get("review_runs") or [], records=records)
        if records:
            projection["review_records_omitted"] = omitted
        return projection
    except Exception:
        log.debug("Failed to build compact review projection", exc_info=True)
        return {"panels": []}


def _record_task_facts(env: Any, task: Dict[str, Any], usage: Dict[str, Any],
                       llm_trace: Dict[str, Any], drive_logs: pathlib.Path) -> None:
    """Append the task's free host facts row to chat.jsonl: no model call, no prose.

    The owner removed the paid task narrative (TZ-2 decision 2=A). This row keeps
    the facts its readers take from a ``task_summary`` row when the stored result is
    gone: the direct-turn fact, origin label, typed routing action, tool metrics,
    rounds, cost and review projection. Its kind is ``host_task_facts``, never
    ``authored_root_summary``, and it persists no continuation narrative, so a
    Main continuation sees the typed ``continuation_narrative_unavailable`` gap.
    Text stays empty: existing Project and result references own navigation.
    """
    task_id = str(task.get("id") or "unknown")
    try:
        from ouroboros.project_dialogue import append_canonical_task_summary, completion_status_label, outcome_phase
        from ouroboros.task_finalization import artifact_store_roots, rescued_files_fact

        canonical_root = pathlib.Path(task.get("budget_drive_root") or drive_logs.parent)
        result_root = pathlib.Path(getattr(env, "drive_root", canonical_root))
        stored_result = _atp().load_task_result(result_root, task_id) or {}
        review_projection = _compact_review_projection(llm_trace, task, canonical_root)
        # TZ-2 C2: how many files the task rescued into its store(s) — positive, zero or
        # unknown — by stat alone; the fact discloses that no hash was computed. A split
        # non-Project root synthesizes on the canonical drive (parent env and task): its
        # actor store is then the row's recorded ``child_drive_root``, else this drive.
        canonical = result_root.resolve(strict=False) == canonical_root.resolve(strict=False)
        files_rescued = rescued_files_fact(task_id, artifact_store_roots(
            canonical_root, task_id, task=task, child_root=None if canonical else result_root))
        append_canonical_task_summary(canonical_root, {
            "ts": utc_now_iso(), "direction": "system", "type": "task_summary",
            "summary_kind": "host_task_facts", "summary_id": f"task-facts:{task_id}",
            "task_id": task_id, "parent_task_id": str(task.get("parent_task_id") or ""), "root_task_id": str(task.get("root_task_id") or task_id),
            "project_id": str(task.get("project_id") or ""), "chat_id": int(task.get("chat_id") or 0), "delegation_role": str(task.get("delegation_role") or ""), "role": str(task.get("role") or ""),
            "status": str(stored_result.get("status") or "completed"), "outcome": completion_status_label(stored_result, usage), "outcome_phase": outcome_phase(stored_result, usage),
            "outcome_final": False, "outcome_authority": "pre_finalization_host_facts",
            # The chat block reads its chrome, the addressing fact and the
            # origin label from this row when the task result has been pruned.
            "_is_direct_chat": bool(task.get("_is_direct_chat")), **initiator_meta(task),
            **({"typed_routing_action": str(usage["typed_routing_action"])} if usage.get("typed_routing_action") else {}),
            "text": "", **task_tool_metrics(llm_trace),
            "rounds": None if usage.get("loop_evidence_unavailable") else int(usage.get("rounds") or 0),
            "outcome_axes": normalize_outcome_axes(usage), "reason_code": str(usage.get("reason_code") or ""),
            "result_ref": {"kind": "task_result", "task_id": task_id, "reader": "get_task_result"},
            "files_rescued": files_rescued,
            **_summary_row_cost_fields(usage), **presence_provenance_fields(task),
            **({"review_projection": review_projection} if review_projection.get("panels") else {}),
        })
    except Exception:
        log.warning("Task facts row was not recorded for %s (non-critical)", task_id, exc_info=True)


POST_TASK_INTERRUPT_KINDS = frozenset({"budget_exhausted", "provider_outcome_unknown"})


def propagate_paid_interruption(error: BaseException) -> None:
    """Re-raise what must stop later paid post-work; return for an ordinary failure.

    ``propagate_model_error`` carries the control and typed unknown-provider facts;
    the wallet's ``BudgetExceeded`` and an unresolved attempt on ANY provider's
    exception chain — the consolidator's own classifier, never a broadened global
    one — are the others (TZ-2 C3). A stage adapter that only logged them let the
    coordinator run the next paid stage and write ``completed``. Anything else
    returns, so the caller isolates the failure to its own stage.
    """
    propagate_model_error(error)
    from ouroboros.transport_custody import outcome_unknown_on_chain
    from ouroboros.usage_accounting import BudgetExceeded

    if isinstance(error, BudgetExceeded) or outcome_unknown_on_chain(error):
        raise error


def post_task_interruption(control: BaseException) -> str:
    """The closed stop-reason word for a control that ended paid post-work.

    A model wait carries its ``control_reason``; a typed provider fact keeps its
    code; an unknown outcome (a dispatched attempt without a terminal provider
    fact) is always ``provider_outcome_unknown`` — the transport's own
    ``model_outcome_unknown`` spelling never reaches the checkpoint.
    """
    reason = str(getattr(control, "control_reason", "") or "")
    if reason:
        return reason
    code = str(getattr(control, "code", "") or "")
    from ouroboros.transport_custody import outcome_unknown_on_chain

    if not code or code == "model_outcome_unknown" or outcome_unknown_on_chain(control):
        return "provider_outcome_unknown"
    return code


def _post_task_paid_interruption(errors: Any) -> str:
    """The stage's typed outcome from returned error facts: '' when clean.

    Memory consolidation returns errors to keep completed chunks. Only this
    stage adapter interprets those existing facts: a budget or unknown-provider
    kind wins and stops later paid post-work; any other kind names the last
    UNRESOLVED ordinary failure, so a stage that lost a chunk reads ``degraded``
    like a stage that raised (TZ-2 C3: unfinished stages are never ``completed``).
    The history keeps every attempt; a refusal its producer answered (a split whose
    halves carry their own rows, ``resolution``) is not an unfinished stage.
    """
    rows = [row for row in (errors if isinstance(errors, list) else []) if isinstance(row, dict)]
    for row in rows:
        if row.get("kind") in POST_TASK_INTERRUPT_KINDS:
            return str(row["kind"])
    unresolved = [row for row in rows if not row.get("resolution")]
    return str((unresolved[-1].get("kind") or "stage_error")) if unresolved else ""


def _run_memory_fallback_draft(env: Any, task: Dict[str, Any], llm: Any, drive_logs: pathlib.Path,
                               llm_trace: Dict[str, Any]) -> str:
    """Run the fallback memory draft (``memory_fallback``) inside the root post-task worker.

    With consciousness on it reads one state and makes no call. Otherwise at most one
    Light call, scoped and billed as consolidation; a refusal with a receipt and a
    returned failure read ``degraded``, a budget or unknown-provider kind interrupts.
    """
    try:
        from ouroboros import memory_fallback
        from ouroboros.usage_accounting import UsageScope, current_usage_scope, usage_scope

        base_scope = current_usage_scope()
        scope = (replace(base_scope, category="consolidation", source="memory_fallback") if base_scope is not None
                 else UsageScope(drive_root=env.drive_root, category="consolidation", source="memory_fallback"))
        with usage_scope(scope):
            run = memory_fallback.run_fallback_draft(env, task, llm, drive_logs, llm_trace)
        usage = run.usage
        if usage and (usage.get("cost") or usage.get("prompt_tokens")):
            from supervisor.state import update_budget_from_usage
            update_budget_from_usage(usage)
        if run.outcome in ("consciousness_on", "not_activated", "nothing"):
            return ""
        from ouroboros.knowledge import observed_route_stamp

        reason = (run.kind if run.outcome == "refused"
                  else _post_task_paid_interruption(run.errors))
        try:
            append_jsonl(pathlib.Path(drive_logs) / "events.jsonl", {
                "ts": utc_now_iso(), "type": "memory_fallback_draft", "task_id": str(task.get("id") or ""),
                "unit": run.unit.describe() if run.unit else None, "outcome": run.outcome, "kind": run.kind or None,
                "record_id": run.record_id or None, "input_tokens": run.input_tokens,
                "accounted_upper_bound_usd": round(float(usage["cost"]), 6) if usage.get("cost") is not None else None,
                "route": observed_route_stamp(usage) if usage else None})
        except Exception:
            log.debug("memory_fallback_draft event was not recorded", exc_info=True)
        return reason
    except Exception as error:
        propagate_paid_interruption(error)
        log.debug("Fallback memory draft setup failed", exc_info=True)
        return "stage_setup_failed"


def _run_scratchpad_consolidation(env: Any, memory: Any, llm: Any) -> None:
    """Run scratchpad consolidation inside the root post-task worker."""
    try:
        from ouroboros import consolidator as _c

        should_consolidate = _c.should_consolidate_scratchpad
        consolidate = _c.consolidate_scratchpad
        if should_consolidate(memory):
            _identity = memory.load_identity()
            from ouroboros.usage_accounting import UsageScope, current_usage_scope, usage_scope

            base_scope = current_usage_scope()
            scratch_scope = (
                replace(base_scope, category="consolidation", source="scratchpad_consolidation")
                if base_scope is not None
                else UsageScope(
                    drive_root=env.drive_root,
                    category="consolidation",
                    source="scratchpad_consolidation",
                )
            )

            with usage_scope(scratch_scope):
                u = consolidate(memory, llm, _identity)
            if u and (u.get("cost") or u.get("prompt_tokens")):
                from supervisor.state import update_budget_from_usage
                update_budget_from_usage(u)
            return _post_task_paid_interruption(u.get("_consolidation_errors") if isinstance(u, dict) else [])
    except Exception as error:
        propagate_paid_interruption(error)
        log.debug("Scratchpad consolidation setup failed", exc_info=True)
        return "stage_setup_failed"


# --- owner D10: the root late phase as a saved Pause (post_task_checkpoint.py) ---

def late_phase_step(name: str, run_step: Callable[[], Any]) -> Any:
    """Run one completed-once step of a stage; a resumed Pause skips a finished one."""
    from ouroboros.post_task_checkpoint import current_late_phase_run

    late = current_late_phase_run()
    if late is not None and late.marked(name):
        return None
    outcome = run_step()
    if late is not None:
        late.mark(name)
    return outcome


def reflection_callback_spec(callback: Any) -> Dict[str, Any] | None:
    """The one durable shape of the split Project root's canonical chooser entry (an earlier
    version saved the same shape for its reflection callback; resumed, it receives the sealed final)."""
    import functools

    if (isinstance(callback, functools.partial) and callback.func is _atp()._run_global_backlog_promotion_only
            and len(callback.args) == 2):
        parent_env, parent_task = callback.args
        return {"drive_root": str(parent_env.drive_root), "repo_dir": str(parent_env.repo_dir),
                "task": json.loads(json.dumps(parent_task, ensure_ascii=False, default=str))}
    return None


def _late_env(drive_root: Any, repo_dir: Any):
    from types import SimpleNamespace

    root = pathlib.Path(drive_root)
    return SimpleNamespace(repo_dir=pathlib.Path(repo_dir), drive_root=root, drive_path=lambda rel, _r=root: _r / rel)


def park_late_phase(env: Any, task: Dict[str, Any], stage: str, remaining: list, completed: list,
                    *, inputs: tuple, state: Dict[str, Any], late: Any) -> bool:
    """Save the interrupted remainder of a root late phase as the owner's Pause.

    ``inputs`` are the coordinator's frozen inputs (usage, usage snapshot, trace,
    sealed final, drive logs, the split Project root's canonical chooser entry); ``late``
    is what the phase already holds (completed steps); the original money scope
    rides along. One actor-source payload carries them; the checkpoint stays open
    ``paused``. A payload an earlier version saved may carry more keys (review
    evidence, a reflection entry and its nominations): resume reads none of them.
    ``review_evidence`` is still written, empty: an earlier version's resume reads
    it only after consuming the grant, so its absence would lose the Pause there.
    Durable copies omit boot-relative timing; live delivery keeps its clocks.
    False means nothing was saved: the caller keeps its honest ``degraded``.
    """
    import dataclasses
    import uuid

    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.owner_pause import read_fence
    from ouroboros.post_task_checkpoint import (
        post_task_synthesis_is_paused,
        root_checkpoint_roots,
        set_root_post_task_checkpoint,
    )
    from ouroboros.usage_accounting import current_usage_scope

    roots = root_checkpoint_roots(env, task)
    task_id = str(task.get("id") or task.get("task_id") or "")
    if late is None or not roots or not task_id or not stage:
        return False
    usage, usage_snapshot, trace, sealed_final, drive_logs, callback = inputs
    scope = current_usage_scope()
    try:
        pause_id = uuid.uuid4().hex
        payload = {"schema": "ouroboros.late_phase_pause.v1", "task_id": task_id, "stage": stage,
                   "completed_stages": list(completed), "remaining_stages": list(remaining),
                   "marks": sorted(late.marks), "drafts": dict(late.drafts), "task": task,
                   "env": {"drive_root": str(env.drive_root), "repo_dir": str(getattr(env, "repo_dir", "") or "")},
                   "money_scope": dataclasses.asdict(scope) if scope is not None else None,
                   "usage": without_finalization_timing(usage),
                   "usage_snapshot": without_finalization_timing(usage_snapshot), "trace": trace,
                   "review_evidence": {}, "sealed_final": sealed_final, "drive_logs": str(drive_logs),
                   "reflection_callback": reflection_callback_spec(callback), **state}
        ref = store_actor_source_bytes(roots[0], task_id, category="context_checkpoints",
                                       source_id="late-phase-pause-" + pause_id, extension="json",
                                       data=json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        record = {"pause_id": pause_id, "fence_id": str(read_fence(roots[0], task_id).get("fence_id") or ""),
                  "paused_at": utc_now_iso(), "stage": stage, "completed_stages": list(completed),
                  "remaining_stages": list(remaining), "payload_ref": ref,
                  **({"retired_stages": list(state["retired_stages"])} if state.get("retired_stages") else {})}
        stored = set_root_post_task_checkpoint(env, task, "paused", pause=record)
    except Exception:
        log.warning("Late-phase pause of %s was not saved", task_id, exc_info=True)
        return False
    saved = post_task_synthesis_is_paused(((stored or {}).get("root_phase_checkpoint") or {}).get("post_task_synthesis"))
    if saved:
        append_jsonl(roots[0] / "logs" / "events.jsonl", {
            "ts": utc_now_iso(), "type": "late_phase_paused", "task_id": task_id, "stage": stage,
            "remaining_stages": list(remaining), "kept_drafts": len(late.drafts)})
    return saved


def revoke_late_phase_grant(drive_root: Any, task_id: str, *, reason: str) -> Dict[str, Any] | None:
    """Return an unconsumed Resume grant to its pause (Restart, a newer Pause, a failed start)."""
    from ouroboros.post_task_checkpoint import update_late_phase_pause

    def revoke(record: Dict[str, Any]) -> Dict[str, Any] | None:
        grant = record.get("grant") if isinstance(record.get("grant"), dict) else {}
        if not grant or grant.get("consumed_at") or grant.get("revoked_at"):
            return None
        return {**record, "grant": {**grant, "revoked_at": utc_now_iso(), "revoke_reason": str(reason)}}

    return update_late_phase_pause(drive_root, task_id, revoke)


def resume_paused_late_phase(drive_root: Any, repo_dir: Any, task_id: str) -> bool:
    """Consume one granted Resume and continue the saved remainder (detached).

    The exact fence the Pause saved is reopened first, as for a loop Resume (a
    failure consumes nothing and the caller revokes the grant); the grant is
    then consumed exactly once under the result lock, and a newer owner Pause
    or a Stop that landed first wins with nothing run. The remainder runs under
    the saved original money scope and re-reads its CURRENT memory inputs;
    completed stages, steps and effects do not repeat, and a saved stage this
    version no longer has is retired and disclosed, never run.
    """
    from contextlib import nullcontext

    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.owner_pause import reopen_for_resume
    from ouroboros.post_task_checkpoint import late_phase_pause_record, update_late_phase_pause
    from ouroboros.task_results import load_task_result
    from ouroboros.usage_accounting import UsageScope, usage_scope

    root = pathlib.Path(drive_root).resolve(strict=False)
    record = late_phase_pause_record(load_task_result(root, task_id, strict=True) or {})
    grant = record.get("grant") if isinstance(record.get("grant"), dict) else {}
    if not grant.get("grant_id") or grant.get("consumed_at") or grant.get("revoked_at"):
        return False
    try:
        payload = json.loads(read_actor_source_bytes(root, task_id, record["payload_ref"]))
    except Exception:
        log.warning("Saved late phase of %s is unreadable; its pause is kept", task_id, exc_info=True)
        revoke_late_phase_grant(root, task_id, reason="pause_source_unreadable")
        return False
    now = utc_now_iso()

    def consume(current: Dict[str, Any]) -> Dict[str, Any] | None:
        live = current.get("grant") if isinstance(current.get("grant"), dict) else {}
        if (current.get("pause_id") != record.get("pause_id") or live.get("grant_id") != grant["grant_id"]
                or live.get("consumed_at") or live.get("revoked_at") or current.get("stopped_at")):
            return None
        return {**current, "grant": {**live, "consumed_at": now}, "resumed_at": now}

    try:
        if grant.get("fence_id"):
            # The loop Resume's rule: a newer Pause minted over this grant keeps
            # its fence, and the remainder parks again at its first paid send.
            reopen_for_resume(root, task_id, task_id, fence_id=str(grant["fence_id"]), grant_id=grant["grant_id"])
    except Exception:
        log.warning("Late-phase Resume of %s lost its fence; the pause is kept", task_id, exc_info=True)
        return False
    if update_late_phase_pause(root, task_id, consume) is None:
        return False
    env = _late_env(payload["env"]["drive_root"], payload["env"]["repo_dir"] or repo_dir)
    spec = payload.get("reflection_callback")
    callback = None
    if isinstance(spec, dict):
        import functools

        callback = functools.partial(_atp()._run_global_backlog_promotion_only,
                                     _late_env(spec["drive_root"], spec["repo_dir"]), spec["task"])
    money = payload.get("money_scope")
    scope = UsageScope(**{key: value for key, value in money.items()
                          if key in UsageScope.__dataclass_fields__}) if isinstance(money, dict) else None
    append_jsonl(root / "logs" / "events.jsonl", {"ts": now, "type": "late_phase_resumed", "task_id": task_id,
                                                  "stage": record.get("stage"), "grant_id": grant["grant_id"]})
    with usage_scope(scope) if scope is not None else nullcontext():
        _atp()._run_post_task_processing_async(
            env, payload["task"], payload["usage"], payload["trace"],
            pathlib.Path(payload["drive_logs"]), blocking=False, on_reflection=callback,
            sealed_final=payload.get("sealed_final"), resume=payload)
    return True


def stop_paused_late_phase(drive_root: Any, task_id: str) -> bool:
    """The owner's Stop of a saved remainder: degraded ``owner_stopped``, answer kept.

    Applies only while the phase is still paused and no Resume consumed its
    grant; otherwise the live phase's own stage gate owns the Stop.
    """
    from ouroboros.post_task_checkpoint import set_root_post_task_checkpoint, update_late_phase_pause
    from ouroboros.task_results import load_task_result

    root = pathlib.Path(drive_root).resolve(strict=False)
    now = utc_now_iso()

    def stop(record: Dict[str, Any]) -> Dict[str, Any] | None:
        grant = record.get("grant") if isinstance(record.get("grant"), dict) else {}
        if grant.get("consumed_at"):
            return None
        revoked = {**grant, "revoked_at": now, "revoke_reason": "owner_stopped"} if grant and not grant.get("revoked_at") else grant
        return {**record, "stopped_at": now, **({"grant": revoked} if grant else {})}

    stopped = update_late_phase_pause(root, task_id, stop)
    if stopped is None:
        return False
    row = load_task_result(root, task_id, strict=True) or {}
    skipped = ",".join(stopped.get("remaining_stages") or [stopped.get("stage") or ""])
    retired = ",".join(stopped.get("retired_stages") or ())
    set_root_post_task_checkpoint(_late_env(root, root.parent), {**row, "id": task_id, "budget_drive_root": str(root)},
                                  "degraded", stop_reason=f"owner_stopped:skipped={skipped}"
                                  + (f";stage_retired:skipped={retired}" if retired else ""))
    return True
