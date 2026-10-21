"""Collect a Skill Review's reserved operations from their original paid source."""

from __future__ import annotations

from dataclasses import asdict


def logged_error_operations(row):
    """``(slot_id, operation_id)`` whose failure the selected wave row already logged (#1578).

    Only a positive ``error_event_logged`` on that exact operation's terminal record
    counts: a failed append is no receipt, and an awaited seat's event does not cover
    the terminal outcome its operation settles later.
    """
    return frozenset(
        (actor["slot_id"], actor["operation_id"]) for actor in row.get("raw_actor_records") or []
        if isinstance(actor, dict) and actor.get("error_event_logged") is True
        and not actor.get("late_result_pending") and actor.get("operation_state") in {"settled", "late_settled"}
        and isinstance(actor.get("slot_id"), str) and actor["slot_id"]
        and isinstance(actor.get("operation_id"), str) and actor["operation_id"])


def _recorded_assignment(root, wave, chunk, slot_id, operation_id, logged=False):
    from ouroboros.delegate_custody import invocation_record
    from ouroboros.review_dispatch import review_operation_binding
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_operation import recorded_review_assignment

    owner = wave["binding"]
    row = {"slot_id": slot_id, "operation_id": operation_id,
           "operation_state": "in_flight", "late_result_pending": True}
    request, slot = recorded_review_assignment(root, task_id=owner["task_id"], surface="skill_review",
        row=row, retry_key=chunk["retry_key"], root_task_id=owner["root_task_id"])
    expected = review_operation_binding(request, slot, operation_id)
    attribution = request.usage_attribution or {}
    if (request.task_attempt != owner["task_attempt"]
            or attribution.get("review_skill") != owner["skill"]
            or attribution.get("review_wave_id") != wave["wave_id"]
            or expected.get("task_attempt") != owner["task_attempt"]
            or expected.get("epoch") != chunk["retry_key"]):
        raise ValueError("original Skill Review request binding mismatch")
    token = str((chunk.get("invocations") or {}).get(slot_id) or "")
    if slot.route is ReviewRouteKind.AGENT_SESSION and token:
        invocation = invocation_record(root, token)
        if not invocation or any(str(invocation.get(key) or "") != str(expected.get(key) or "")
                for key in ("task_id", "root_task_id", "surface", "slot_id", "operation_id")):
            raise ValueError("original Skill Review invocation binding missing or inconsistent")
    request.reconcile_only = True
    # The selected row's receipt rides this exact operation's frozen row, as a plan wave's row does (#1578).
    return request, slot, {**row, "model": slot.model,
        "recovery_binding": {**expected, **({"pending_invocation_id": token} if token else {})},
        **({"pending_invocation_id": token, "usage": {"pending_invocation_id": token}} if token else {}),
        **({"error_event_logged": True} if logged else {})}


def _collect_original_actor(root, ctx, request, slot, row):
    from ouroboros.review_operation import _collect_pending_row, recover_review_producer
    from ouroboros.review_records import ReviewActorRecord, ReviewRouteKind

    actor = recover_review_producer(root, request, slot, row)
    if actor is not None and actor.operation_state != "in_flight":
        return actor
    if slot.route is not ReviewRouteKind.AGENT_SESSION or not row.get("pending_invocation_id"):
        return _collect_pending_row(root, request, slot, row, ctx, None)[0]
    # A known unfinished session may replay only its original invocation. The
    # existing custody executor owns that same-key join and its remaining window;
    # it must never see today's slots, prompt, rights or a replacement operation.
    from ouroboros.review_substrate import ReviewCoordinator

    names = ("_review_reserved_operations", "_review_frozen_rows")
    prior = {name: getattr(ctx, name, None) for name in names}
    ctx._review_reserved_operations = {"skill_review": {slot.slot_id: row["operation_id"]}}
    ctx._review_frozen_rows = {"skill_review": {slot.slot_id: row}}
    try:
        result = ReviewCoordinator(llm=None, drive_root=root, usage_ctx=ctx).run(request, [slot])
        return ReviewActorRecord(**result.actors[0])
    finally:
        for name, value in prior.items():
            setattr(ctx, name, value)


def _collect_seat(root, ctx, wave, chunk, slot_id, operation_id):
    from ouroboros.tools.review_response import parse_model_response, review_operation_fields

    try:
        request, slot, row = _recorded_assignment(
            root, wave, chunk, slot_id, operation_id,
            logged=(slot_id, operation_id) in getattr(ctx, "_skill_review_logged_errors", ()))
        actor = _collect_original_actor(root, ctx, request, slot, row)
        record = asdict(actor)
        payload = {**record, **review_operation_fields(record)}
        if actor.status in {"ok", "empty"} and actor.operation_state not in {"in_flight", "custody_lost"}:
            payload["choices"] = [{"message": {"content": actor.raw_text}}]
        else:
            payload["error"] = actor.error or "The recorded reviewer has no terminal result yet."
        from ouroboros.reviewer_slot_config import review_cost_facts
        result = parse_model_response(slot.model, payload, None)
        result.update(review_cost_facts(result.get("cost_estimate"), route=slot.route.value, model=slot.model))
        return slot.model, result
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # A missing request cannot borrow today's model or rights. Keep this
        # reserved seat in the quorum denominator and leave its outcome unknown.
        token = str((chunk.get("invocations") or {}).get(slot_id) or "")
        return slot_id, parse_model_response(slot_id, {
            "slot_id": slot_id, "operation_id": operation_id,
            "operation_state": "custody_lost", "late_result_pending": True,
            "failure_code": "review_custody_lost", "usage": {"pending_invocation_id": token},
            "error": f"Original Skill Review source unavailable: {exc}",
        }, None)


def collect_skill_review_wave(ctx, drive_root, file_packs, *, required_items):
    """Read every recorded seat; never build a prompt or dispatch a replacement.

    The first chunk's roster is the wave's; a later chunk may record only its packet
    seats, because a retrieving seat reviewed the whole payload in the first chunk —
    its answer there counts toward every chunk's quorum over the whole roster."""
    from ouroboros.config import adaptive_quorum
    from ouroboros.delegate_custody import custody_root
    from ouroboros.skill_review_passes import _responded_slots, _skill_review_retry_key
    from ouroboros.triad_review import parse_model_review_results

    wave = ctx._skill_review_resume
    advisers = set(wave.get("additional") or [])
    owner, chunks = wave["binding"], wave["chunks"]
    if len(chunks) != len(file_packs):
        return {"error": "Original Skill Review chunk roster is incomplete"}
    root = custody_root(ctx) if getattr(ctx, "drive_root", None) else drive_root
    results, models, pending, roster, later, whole = [], [], False, None, None, set()
    for index, (chunk, pack) in enumerate(zip(chunks, file_packs)):
        retry_key = _skill_review_retry_key(
            skill_name=owner["skill"], wave_id=wave["wave_id"], content_hash=owner["content_hash"],
            contract_fingerprint=owner["review_contract_fingerprint"], rebuttal_sha256=owner["rebuttal_sha256"],
            pack=pack, chunk_index=index, chunk_count=len(chunks))
        operations = chunk.get("operations")
        if (chunk.get("retry_key") != retry_key or not isinstance(operations, dict) or not operations
                or any(not slot or not operation for slot, operation in operations.items())
                or len(set(operations.values())) != len(operations)
                or (roster is not None and not set(operations) <= roster)
                or (later is not None and set(operations) != later)):
            return {"error": "Original Skill Review chunk identity or roster mismatch"}
        if roster is None:
            roster = set(operations)
        elif later is None:
            later = set(operations)
        collected = [_collect_seat(root, ctx, wave, chunk, slot, operation)
                     for slot, operation in operations.items()]
        if index == 0:
            models = [model for model, _row in collected]
        rows = [row for _model, row in collected]
        for row in rows:
            row["additional"] = row.get("slot_id") in advisers
        parsed = parse_model_review_results({"results": rows}, required_items=required_items)
        if index == 0:
            whole = _responded_slots(parsed)
        answered = len(parsed.responsive_models) + (len(whole - set(operations)) if index else 0)
        pending = pending or answered < adaptive_quorum(len(roster - advisers)) or any(
            row.get("late_result_pending") or row.get("operation_state") in {"in_flight", "custody_lost"}
            for row in rows if not row.get("additional"))
        results.extend(rows)
    return {"model_count": len(models), "recorded_models": models, "results": results,
            "additional": list(advisers), "panel": dict(wave.get("panel") or {}),
            "collection_pending": pending}
