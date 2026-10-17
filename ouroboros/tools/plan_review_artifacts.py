"""Read-only reader of earlier-schema (v1/v2) plan-review records.

Finished installs, and tasks in flight at an upgrade, still hold ``plan_review_state``
waves whose exact authority lives in the task artifact store (wave artifacts, spec,
room and author sources). This module resolves those bytes (``read_wave``,
``authority_wave``, ``authority_state``, ``current_author_plan``), renders the whole
dispute for the review-history views (``plan_review_dispute_history``,
``plan_decision_aliases``) and projects a bounded core for a Continue
(``plan_review_authority_core``, ``compact_wave``). Nothing writes either schema any
more: the planning organ's record is ``ouroboros/plan_record.py``.
"""

from __future__ import annotations

import copy
from hashlib import sha256
import json
import pathlib
from typing import Any, Dict, Optional


class PlanReviewSourceUnavailable(ValueError):
    """A recorded plan names full authority that this reader cannot resolve."""


def read_wave(drive_root: Any, task_id: str, ref: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.artifacts import task_artifact_dir_path

    if not isinstance(ref, dict) or ref.get("root") != "artifact_store":
        raise ValueError("invalid plan-review wave artifact ref")
    if ref.get("kind") == "task_source":
        from ouroboros.artifacts import read_actor_source_bytes
        raw = read_actor_source_bytes(drive_root, task_id, ref)
    else:
        name = pathlib.Path(str(ref.get("path") or "")).name
        if not name or name != str(ref.get("path") or ""):
            raise ValueError("invalid plan-review wave artifact path")
        raw = (task_artifact_dir_path(drive_root, task_id, create=False) / name).read_bytes()
        if len(raw) != int(ref.get("bytes") or -1) or sha256(raw).hexdigest() != str(ref.get("sha256") or ""):
            raise ValueError("plan-review wave artifact digest mismatch")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plan-review wave artifact is not an object")
    return value


def authority_wave(drive_root: Any, task_id: str, hot_wave: Optional[dict]) -> Optional[dict]:
    """Materialize exact authority while retaining newer hot lifecycle stamps."""
    if not isinstance(hot_wave, dict):
        return None
    ref = hot_wave.get("wave_artifact") if isinstance(hot_wave.get("wave_artifact"), dict) else {}
    if not ref:
        if hot_wave.get("compact") or hot_wave.get("spec_in_artifact") or hot_wave.get("spec_body_truncated"):
            raise PlanReviewSourceUnavailable("PLAN_REVIEW_SOURCE_UNAVAILABLE: full spec has no artifact reference")
        return hot_wave
    if not drive_root or not task_id:
        raise PlanReviewSourceUnavailable("PLAN_REVIEW_SOURCE_UNAVAILABLE: artifact owner is unknown")
    try:
        exact = read_wave(drive_root, task_id, ref)
    except (OSError, ValueError) as exc:
        raise PlanReviewSourceUnavailable(
            f"PLAN_REVIEW_SOURCE_UNAVAILABLE: task {task_id}, artifact {ref.get('path')}: {exc}"
        ) from exc
    source = hot_wave.get("spec_source_ref") or exact.get("spec_source_ref")
    if source:
        from ouroboros.artifacts import read_actor_source_bytes
        from ouroboros.tools.plan_spec import spec_hash

        try:
            spec = json.loads(read_actor_source_bytes(drive_root, task_id, source))
            if not isinstance(spec, dict) or spec_hash(spec) != hot_wave.get("spec_hash", exact.get("spec_hash")):
                raise ValueError("operative spec hash mismatch")
        except (OSError, ValueError) as exc:
            raise PlanReviewSourceUnavailable(f"PLAN_REVIEW_SOURCE_UNAVAILABLE: {exc}") from exc
    elif hot_wave.get("spec_in_artifact"):
        raise PlanReviewSourceUnavailable("PLAN_REVIEW_SOURCE_UNAVAILABLE: operative spec reference is missing")
    elif not hot_wave.get("spec_body_truncated") and isinstance(hot_wave.get("spec"), dict):
        spec = hot_wave["spec"]  # Legacy inline authority predates the source handle.
    else:
        from ouroboros.tools.plan_spec import spec_hash

        spec = exact.get("spec")
        if (not isinstance(spec, dict) or exact.get("spec_body_truncated")
                or spec_hash(spec) != exact.get("spec_hash")):
            raise PlanReviewSourceUnavailable("PLAN_REVIEW_SOURCE_UNAVAILABLE: artifact has no complete spec")
    dialogue_ref = hot_wave.get("dialogue_source_ref") or exact.get("dialogue_source_ref")
    if dialogue_ref:
        from ouroboros.artifacts import read_actor_source_bytes
        read_actor_source_bytes(drive_root, task_id, dialogue_ref)
    restored = {
        **exact, **hot_wave,
        "spec": copy.deepcopy(spec), "goal": spec.get("goal") or "",
        "findings": list(exact.get("findings") or []),
    }
    if dialogue_ref and isinstance(restored.get("evidence_manifest_full"), dict):
        from ouroboros.artifacts import task_artifact_dir_path
        restored["evidence_manifest_full"] = copy.deepcopy(restored["evidence_manifest_full"])
        own = restored["evidence_manifest_full"].get("own_dialogue")
        if isinstance(own, dict):
            own.update(source_ref=dialogue_ref, file=str(task_artifact_dir_path(drive_root, task_id, create=False) / dialogue_ref["path"]))
    restored.pop("spec_in_artifact", None)
    restored.pop("spec_body_truncated", None)
    return restored


def authority_state(drive_root: Any, task_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the current spec; historical consumers resolve their selected wave."""
    from ouroboros.task_results import current_plan_review_wave

    current = current_plan_review_wave(state)
    if not current or current.get("compact") or not (
        current.get("spec_source_ref") or current.get("spec_in_artifact")
        or (current.get("spec_body_truncated") and current.get("wave_artifact"))
    ):
        return state
    resolved = authority_wave(drive_root, task_id, current)
    return {**state, "waves": [
        resolved if wave.get("request_fingerprint") == current.get("request_fingerprint") else wave
        for wave in state.get("waves") or []
    ]}


def plan_review_source_gap(task_id: Any, exc: BaseException, *, source: Optional[dict] = None) -> dict:
    """The one named gap for recorded plan/review authority a reader could not resolve."""
    return {"status": "authority_source_unavailable", "code": "PLAN_REVIEW_SOURCE_UNAVAILABLE",
            "task_id": str(task_id or ""), "error": type(exc).__name__,
            "rule": "Do not treat this plan/review authority as complete.",
            **({"source": source} if source else {})}


def projected_authority_state(drive_root: Any, task_id: str, state: Dict[str, Any], *,
                              promotion: Any = None) -> tuple[Dict[str, Any], Optional[dict]]:
    """Read projection of a row's plan authority: an unreadable source is a named gap.

    Deciders (acceptance, replay, collection, disposition) keep the strict
    ``authority_state``. A legacy retention gap is re-addressed IN MEMORY from the
    row's own promotion facts first; nothing is written. Any failure (the dialogue
    read raises its reader's error unwrapped) returns the recorded state unresolved
    plus the gap.
    """
    from ouroboros.observability import restore_recorded_task_source_gaps

    try:
        return authority_state(drive_root, task_id, restore_recorded_task_source_gaps(state, promotion, task_id)), None
    except Exception as exc:
        return state, plan_review_source_gap(task_id, exc)


DISPUTE_HISTORY_RULE = (
    "Historical findings, recommendations and author answers belong to their recorded subjects. "
    "An author's rejection or closure is not reviewer agreement. Only the recorded reviewer "
    "answers say what a reviewer accepted or disputed; absence of an answer proves neither. "
    "Preserve the original verdict when the subject changes. A rejected alternative may be "
    "reconsidered with new facts or changed requirements: name the earlier rationale and explain "
    "what changed. Source pointers for duplicate requests and room coverage are not read receipts."
)


def plan_review_dispute_history(drive_root: Any, task_id: str, state: dict) -> dict:
    """Read the full dispute, including evicted rounds and superseded author answers.

    This is a read-only view of the existing wave graph, not a new authority or
    ledger. A compact hot wave is only an index. Walk both predecessor kinds by
    immutable artifact identity, retaining gaps and every available decision.
    The explicit stack avoids a Python recursion limit becoming a history cap.
    """
    from ouroboros.artifacts import read_actor_source_bytes, task_artifact_dir_path
    from ouroboros.task_results import plan_review_wave, current_plan_review_wave
    from ouroboros.tools.plan_author_history import author_selections

    rounds, gaps, decision_rows = [], [], []
    selections, source_gaps = author_selections(drive_root, task_id, state)
    done, visiting = set(), set()
    material_sources, decisions_seen = {}, set()

    def address(ref: dict, field: str = "") -> dict:
        return {"source_ref": copy.deepcopy(ref), "field": field,
                "file": str(task_artifact_dir_path(drive_root, task_id, create=False) / ref["path"]),
                "read": {"tool": "read_file", "arguments": {
                    "root": "artifact_store", "path": ref["path"]}}} if ref.get("path") else {
                    "tool": "get_task_result", "arguments": {"task_id": task_id, "include_authority": True},
                    "field": field}

    def gap(reason: str, ref: dict, wave: dict) -> None:
        gaps.append({"code": "PLAN_REVIEW_SOURCE_UNAVAILABLE", "reason": reason,
                     "source": address(ref), "cycle_index": wave.get("cycle_index"),
                     "request_fingerprint": wave.get("request_fingerprint")})

    def retain_once(view: dict, key: str, kind: str, source: dict) -> None:
        """Inline each exact value once in this view; subsequent copies name it.

        Hash the verified immutable bytes' logical field, not the hot index or
        the mutable wave id. Distinct arguments with the same finding id stay distinct.
        This map lives only for this read and never caches a decision.
        """
        if key not in view or not (source.get("source_ref") or {}).get("sha256"):
            return
        digest = sha256(json.dumps(view[key], ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        identity = (kind, digest)
        if identity in material_sources:
            view.pop(key)
            view[key + "_source"] = {"same_content_as": material_sources[identity], "sha256": digest}
        else:
            material_sources[identity] = source

    def decisions(wave: dict, ref: dict) -> None:
        """An operative index of recorded statements, without inferred agreement."""
        for finding in wave.get("findings") or []:
            answers = [d for d in wave.get("dispositions") or []
                       if d.get("finding_id") == finding.get("finding_id")]
            row = {"decision_kind": "plan_finding", "cycle_index": wave.get("cycle_index"), "request_fingerprint": wave.get("request_fingerprint"),
                   "spec_hash": wave.get("spec_hash"), "finding_id": finding.get("finding_id"),
                   "remark": finding.get("summary"),
                   "status": {"reviewer_class": finding.get("class"), "aggregate": wave.get("aggregate"),
                              "closed": wave.get("closed"), "author_decisions": [d.get("decision") for d in answers]},
                   "reason": {"reviewer_recommendation": finding.get("recommendation"),
                              "author_rationales": [d.get("rationale") for d in answers]}}
            identity = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if identity not in decisions_seen:
                decisions_seen.add(identity)
                decision_rows.append({**row, "source": address(ref, "findings / dispositions / reviewer_outputs")})
        if wave.get("author_disposition"):
            row = {"decision_kind": "plan_author", "cycle_index": wave.get("cycle_index"), "request_fingerprint": wave.get("request_fingerprint"),
                   "remark": "Author disposition", "status": wave["author_disposition"],
                   "reason": wave["author_disposition"].get("rationale")}
            identity = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            if identity not in decisions_seen:
                decisions_seen.add(identity)
                decision_rows.append({**row, "source": address(ref, "author_disposition")})
        if wave.get("closure_notes"):
            decision_rows.append({"decision_kind": "plan_closure", "cycle_index": wave.get("cycle_index"),
                "request_fingerprint": wave.get("request_fingerprint"), "remark": "Recorded closure notes",
                "status": {"aggregate": wave.get("aggregate"), "closed": wave.get("closed")},
                "reason": copy.deepcopy(wave["closure_notes"]), "source": address(ref, "closure_notes")})

    def project(wave: dict, ref: dict) -> dict:
        # These are semantic inputs/decisions, never the recursive copies of
        # previous packets embedded in each reviewer's request transcript.
        keys = ("cycle_index", "request_fingerprint", "previous_fingerprint", "spec_hash",
                "evidence_manifest_hash", "plan_prose_hash", "spec", "plan_prose", "aggregate",
                "closed", "paid", "custody_pending", "findings", "dispositions", "author_disposition",
                "closure_notes", "reviewed_at", "disposition_recorded_at", "addressed", "open_ids")
        view = {key: copy.deepcopy(wave[key]) for key in keys if key in wave}
        view["source"] = address(ref)
        if wave.get("spec_source_ref"):
            view["spec_address"] = address(wave["spec_source_ref"], "$json")
        for key in ("spec", "plan_prose", "findings"):
            retain_once(view, key, key, view.get("spec_address", address(ref, key)) if key == "spec" else address(ref, key))
        manifest = wave.get("evidence_manifest_full") or wave.get("evidence_manifest") or {}
        # Preserve every substantive attachment by default. Only the known room
        # snapshot copy is addressed; no filename or prose heuristic removes evidence.
        view["evidence"] = {key: copy.deepcopy(value) for key, value in manifest.items()
                            if key != "own_dialogue"}
        manifest_field = "evidence_manifest_full" if wave.get("evidence_manifest_full") else "evidence_manifest"
        view["evidence_source"] = address(ref, manifest_field)
        retain_once(view["evidence"], "attached", "attachments", address(ref, manifest_field + ".attached"))
        if manifest.get("own_dialogue"):
            view["room_source"] = address(ref, "evidence_manifest_full.own_dialogue")
        view["reviewer_outputs"] = []
        for index, row in enumerate(wave.get("reviewer_outputs") or []):
            item = {key: copy.deepcopy(value) for key, value in row.items()
                    if key not in {"request_messages", "session_task"}}
            item["request_source"] = address(ref, f"reviewer_outputs[{index}]")
            retain_once(item, "text", "reviewer_text", address(ref, f"reviewer_outputs[{index}].text"))
            view["reviewer_outputs"].append(item)
        view["reviewers"] = [{key: copy.deepcopy(row[key]) for key in (
            "slot_id", "model", "route", "ok", "error", "operation_state", "late_result_pending",
            "carried_findings", "host_file_read_attestation") if key in row}
            for row in wave.get("actors") or []]
        view["coverage_source"] = address(ref, "actors / dialogue_delivery")
        view["historical_feedback"] = []
        for supplement in wave.get("historical_supplements") or []:
            source = supplement.get("source_ref") or {}
            try:
                payload = json.loads(read_actor_source_bytes(drive_root, task_id, source))
                if (payload.get("kind") != "plan_review_historical_supplement"
                        or payload.get("task_id") != task_id
                        or payload.get("request_fingerprint") != wave.get("request_fingerprint")
                        or payload.get("cycle_index") != supplement.get("cycle_index")
                        or not isinstance(payload.get("result"), dict)
                        or payload["result"].get("operation_id") != supplement.get("operation_id")
                        or payload["result"].get("slot_id") != supplement.get("slot_id")):
                    raise ValueError("historical feedback identity mismatch")
                feedback = {
                    "source": address(source), "cycle_index": payload["cycle_index"],
                    "result": copy.deepcopy(payload["result"]), "parsed_findings": payload.get("parsed_findings"),
                    "parse_error": payload.get("parse_error"),
                    "authority": "late feedback; the original verdict is unchanged"}
                retain_once(feedback["result"], "text", "reviewer_text", address(source, "result.text"))
                retain_once(feedback, "parsed_findings", "late_findings", address(source, "parsed_findings"))
                view["historical_feedback"].append(feedback)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                gap(f"historical feedback: {exc}", source, wave)
        if not ref and wave.get("actors") and not wave.get("reviewer_outputs"):
            gap("legacy wave has no exact raw reviewer answers", ref, wave)
        return view

    # Every retained index entry is a root: a closed/disconnected subject is
    # still part of the dispute even when the next dispatch chose another predecessor.
    roots = [wave for wave in state.get("waves") or [] if isinstance(wave, dict)]
    roots.extend({"wave_artifact": selection["review_wave_artifact"]} for selection in selections if selection.get("review_wave_artifact"))
    stack = [(wave, False) for wave in reversed(roots)]
    while stack:
        hot, expanded = stack.pop()
        ref = hot.get("wave_artifact") or {}
        key = ("artifact", ref.get("sha256"), ref.get("path")) if ref else (
            "inline", hot.get("request_fingerprint"), hot.get("cycle_index"))
        if expanded:
            visiting.discard(key)
            done.add(key)
            decisions(hot, ref)
            rounds.append(project(hot, ref))
            continue
        if key in visiting:
            gap("wave artifact chain contains a cycle", ref, hot)
            continue
        if key in done:
            continue
        try:
            if ref:
                exact = read_wave(drive_root, task_id, ref)
                # Materialize the original spec, not a truncated hot copy. Only
                # newer lifecycle/supplement facts may overlay the immutable source.
                try:
                    wave = authority_wave(drive_root, task_id, {**exact, "wave_artifact": ref})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    # A missing secondary spec/room source does not erase the
                    # decisions still present in the verified wave artifact.
                    gap(str(exc), ref, exact)
                    wave = {**exact, "wave_artifact": ref}
                for name in ("paid", "custody_pending", "historical_supplements"):
                    if name in hot:
                        wave[name] = copy.deepcopy(hot[name])
            else:
                wave = authority_wave(drive_root, task_id, hot)
            if wave is None:
                raise ValueError("wave has no authority")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            gap(str(exc), ref, hot)
            done.add(key)
            continue
        visiting.add(key)
        stack.append((wave, True))
        predecessors = []
        previous_ref = wave.get("previous_wave_artifact") or {}
        previous_fp = str(wave.get("previous_fingerprint") or "")
        if previous_ref:
            predecessors.append({"wave_artifact": previous_ref})
        elif previous_fp:
            previous = plan_review_wave(state, previous_fp)
            if previous is None:
                gap(f"predecessor wave {previous_fp} is not in the index", {}, wave)
            else:
                predecessors.append(previous)
        superseded = wave.get("supersedes_wave_artifact") or {}
        if superseded:
            predecessors.append({"wave_artifact": superseded})
        stack.extend((prior, False) for prior in reversed(predecessors))
    indexed = {(w.get("request_fingerprint"), w.get("cycle_index")) for w in state.get("waves") or []}
    recovered = {(w.get("request_fingerprint"), w.get("cycle_index")) for w in rounds} - indexed
    if int(state.get("waves_omitted") or 0) > len(recovered):
        gap("the hot index omitted waves that the recorded artifact chains do not recover", {}, {})
    author_plan = None
    try:
        author_plan = current_author_plan(drive_root, task_id, state)
    except PlanReviewSourceUnavailable as exc:
        gap(str(exc), ((state.get("current_attempt") or {}).get("author_subject") or {}).get("source_ref") or {}, {})
    for missing in source_gaps:
        gap(missing["reason"], missing["source_ref"], {})
    for selection in selections:
        ref = selection.pop("source_ref")
        selection["source"] = address(ref)
        author = selection["author_disposition"]
        decision_rows.append({"decision_kind": "plan_author", "request_fingerprint": selection["fingerprint"],
            "review_fingerprint": selection["review_fingerprint"], "remark": "Selected author plan",
            "review_wave_artifact": selection.get("review_wave_artifact"),
            "status": author, "reason": author["rationale"], "source": address(ref, "author_disposition")})
        for key in ("spec", "plan_prose"):
            retain_once(selection, key, key, address(ref, key))
    if author_plan:
        ref = ((state.get("current_attempt") or {}).get("author_subject") or {}).get("source_ref") or {}
        author_plan["source"] = address(ref)
        for key in ("spec", "plan_prose"):
            retain_once(author_plan, key, key, address(ref, key))
    current = current_plan_review_wave(state) or {}
    current_refs = [current.get("wave_artifact"),
                   ((state.get("current_attempt") or {}).get("author_subject") or {}).get("source_ref")]
    for row in decision_rows:
        ref = row["source"].get("source_ref")
        wave = next((wave for wave in rounds if wave["source"].get("source_ref") == ref), None)
        row["history_role"] = "current" if ref in current_refs else "historical"
        if wave and (not wave.get("closed") or wave.get("custody_pending")
                or row.get("finding_id") in (wave.get("open_ids") or [])
                or any(d.get("decision") == "defer" and d.get("finding_id") == row.get("finding_id")
                       for d in wave.get("dispositions") or [])):
            row["history_role"] = "open"
        if isinstance(row.get("status"), dict) and row["status"].get("disposition") in {"partial", "deferred"}:
            row["history_role"] = "open"
    from ouroboros.review_history_view import canonical_decision_projection
    return canonical_decision_projection({"status": "source_unavailable" if gaps else "complete", "rule": DISPUTE_HISTORY_RULE,
            "rounds": rounds, "decision_rows": decision_rows, "gaps": gaps, "current_author_plan": author_plan,
            "author_selections": selections})


_PLAN_REVIEW_TRANSPORT_KEYS = frozenset({
    "actors", "actors_degraded", "evidence_manifest", "health_epoch", "reasons", "retry_key",
})


def plan_review_authority_core(
    state: Dict[str, Any], *, source_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Project decision authority ahead of compacted request memory and transport."""
    from ouroboros.task_results import _compact_plan_review_wave

    if not isinstance(state, dict) or not state:
        return state
    try:
        schema_version = int(state.get("schema_version") or 0)
    except (TypeError, ValueError):
        return state
    if schema_version == 1:
        return state
    core = copy.deepcopy(state)
    waves = core.get("waves") if isinstance(core.get("waves"), list) else None
    if not waves:
        return core
    last = len(waves) - 1
    core["waves"] = [
        wave if not isinstance(wave, dict)
        else (wave if wave.get("compact") else _compact_plan_review_wave(wave)) if index < last
        else {k: v for k, v in wave.items() if k not in _PLAN_REVIEW_TRANSPORT_KEYS}
        for index, wave in enumerate(waves)
    ]
    if source_ref is not None:
        latest = core["waves"][-1] if isinstance(core["waves"][-1], dict) else {}
        spec = latest.get("spec") if isinstance(latest.get("spec"), dict) else {}

        def recent(value: Any) -> Dict[str, Any]:
            items = value if isinstance(value, list) else []
            return {"items": copy.deepcopy(items[-4:]), "items_omitted": max(0, len(items) - 4), "total": len(items)}

        core["decision_core"] = {
            "identity": {key: copy.deepcopy(latest[key]) for key in ("cycle_index", "request_fingerprint", "previous_fingerprint", "spec_hash", "evidence_manifest_hash", "aggregate", "closed", "paid") if key in latest},
            "goal": spec.get("goal"), "acceptance_claims": recent(spec.get("acceptance_claims")),
            "findings": recent(latest.get("findings")), "dispositions": recent(latest.get("dispositions")),
            "author_disposition": copy.deepcopy(latest.get("author_disposition"))
            if isinstance(latest.get("author_disposition"), dict) else None,
        }
        core["waves"] = [_compact_plan_review_wave(wave) if isinstance(wave, dict) and not wave.get("compact") else wave for wave in core["waves"]]
        core["need_evidence_seen"] = recent(core.get("need_evidence_seen"))
        dropped_keys = sorted({key for wave in waves if isinstance(wave, dict)
                               for key in _PLAN_REVIEW_TRANSPORT_KEYS if key in wave})
        core["projection"] = {
            "projected_from": "plan_review_authority_core", "dropped_keys": dropped_keys,
            "full_chars": len(json.dumps(state, ensure_ascii=False, sort_keys=True, default=str)),
            "source_ref": {**copy.deepcopy(source_ref), "field": "authority.plan_review_state"},
        }
    return core


def compact_wave(wave: Dict[str, Any]) -> Dict[str, Any]:
    """Bounded summary of an older wave (S2): identity, outcome, counts, closure."""
    findings = wave.get("findings") if isinstance(wave.get("findings"), list) else []
    return {
        "compact": True,
        "cycle_index": wave.get("cycle_index"),
        "request_fingerprint": str(wave.get("request_fingerprint") or ""),
        "aggregate": str(wave.get("aggregate") or ""),
        "counts": {
            "findings": int(wave.get("findings_total") or len(findings)),
            "dispositions": len(wave.get("dispositions") or []),
            "blocking": int(wave["counts"].get("blocking") or 0) if isinstance(wave.get("counts"), dict) and "blocking" in wave["counts"] else sum(1 for f in findings if isinstance(f, dict) and f.get("class") == "blocking"),
        },
        "closed": bool(wave.get("closed")),
        "paid": bool(wave.get("paid")),
        "wave_artifact": copy.deepcopy(wave.get("wave_artifact") or {}),
        **{key: copy.deepcopy(wave[key]) for key in ("historical_supplements", "retry_key", "custody_pending", "ordered_weaker", "previous_wave_artifact") if key in wave},
        **({"author_disposition": copy.deepcopy(wave["author_disposition"])}
           if isinstance(wave.get("author_disposition"), dict) else {}),
        **({"spec_source_ref": copy.deepcopy(wave["spec_source_ref"])} if wave.get("spec_source_ref") else {}),
        **{key: copy.deepcopy(wave[key]) for key in ("dialogue_source_ref", "dialogue_chat_id", "author_request_fingerprint") if key in wave},
        **({"reviewed_at": str(wave["reviewed_at"])} if wave.get("reviewed_at") else {}),
    }


def current_author_plan(drive_root: Any, task_id: str, state: dict) -> Optional[dict]:
    """Resolve the selected author source separately from closed reviewer authority."""
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.review_records import validate_author_disposition

    attempt = state.get("current_attempt") or {}
    subject = attempt.get("author_subject") or {}
    author = validate_author_disposition(subject.get("author_disposition"), subject_hash=str(attempt.get("fingerprint") or ""))
    if not author:
        return None
    try:
        value = json.loads(read_actor_source_bytes(drive_root, task_id, subject["source_ref"]))
        if value.get("kind") != "plan_author_subject" or value.get("fingerprint") != author["subject_hash"] or not isinstance(value.get("spec"), dict):
            raise ValueError("author plan source identity mismatch")
        return {**value, "author_disposition": author, "review_fingerprint": subject["review_fingerprint"]}
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise PlanReviewSourceUnavailable(f"PLAN_AUTHOR_SOURCE_UNAVAILABLE: {exc}") from exc


def plan_decision_aliases(history: dict, entry: dict) -> list[tuple[list, Any]]:
    """The plan producer's model-only mirrors of its one canonical decision row."""
    from ouroboros.review_history_view import _decision_ref, _decision_alias
    row, binding = entry["row"], entry["bound_decision"]
    source = _decision_ref(row.get("source"))
    alias, paths, kind = _decision_alias(binding), [], row["decision_kind"]
    for i, wave in enumerate(history.get("rounds") or []):
        if not isinstance(wave, dict):
            continue
        base = ["rounds", i]
        if kind.startswith("plan_") and _decision_ref(wave.get("source")) == source:
            if kind == "plan_finding":
                fid = row.get("finding_id")
                for field in ("findings", "dispositions"):
                    for j, item in enumerate(wave.get(field) or []):
                        if isinstance(item, dict) and item.get("finding_id") == fid:
                            # Keep identity/status keys beside the exact semantic source.
                            shown = {k: copy.deepcopy(v) for k, v in item.items()
                                     if k not in {"summary", "recommendation", "rationale"}}
                            paths.append(([*base, field, j], {**shown, "authored_view": alias}))
                for j, actor in enumerate(wave.get("reviewers") or []):
                    carried = actor.get("carried_findings") if isinstance(actor, dict) else None
                    if isinstance(carried, list):
                        for k, item in enumerate(carried):
                            if isinstance(item, dict) and item.get("finding_id") == fid:
                                paths.append(([*base, "reviewers", j, "carried_findings", k],
                                    {**{name: copy.deepcopy(value) for name, value in item.items()
                                        if name not in {"summary", "recommendation", "rationale"}}, "authored_view": alias}))
            elif kind == "plan_author":
                paths.append(([*base, "author_disposition", "rationale"], alias))
            else:
                paths.append(([*base, "closure_notes"], alias))
    if kind == "plan_author":
        for i, selection in enumerate(history.get("author_selections") or []):
            if _decision_ref(selection.get("source")) == source:
                paths.append((["author_selections", i, "author_disposition", "rationale"], alias))
        author = history.get("current_author_plan")
        if isinstance(author, dict) and _decision_ref(author.get("source") or author.get("source_ref")) == source:
            paths.append((["current_author_plan", "author_disposition", "rationale"], alias))
    return paths
