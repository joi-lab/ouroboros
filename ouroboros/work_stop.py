"""Current stops and cause-free Q9 selections: pure record construction.

The queue owns publication; exact grants keep their existing single-use CAS.
A selection survives consumption, but carries no reason from the released stop.
Technical refusals and writer waits are classified separately from decisions.
"""

SELECTION_KEY = "_resume_selection"
SELECTION_BINDING = (
    "root_task_id", "root_resume_basis", "root_grant_id", "root_resume_generation",
    "fence_id", "root_fence_id", "grant_id", "grant_generation",
)

# The only translation from transport names to lifecycle policy.
STOP_REASONS = {
    "saved_work_hold": {"cause": "app_stop", "release": "resume", "ui_code": "saved_work_hold"},
    "owner_restart_hold": {"cause": "app_stop", "release": "resume", "ui_code": "owner_restart_hold", "door": "owner_restart"},
    "panic_hold": {"cause": "panic", "release": "owner", "ui_code": "panic_hold"},
    "saved_sleep_recovery": {"cause": "app_stop", "release": "resume", "ui_code": "saved_sleep_recovery",
                             "door_replaceable": True, "replaces_door": False},
    "unsaved_pause_intent": {"cause": "pause", "release": "resume", "ui_code": "owner_paused", "needs_source": True},
    "root_fence_lifted_pending_selection": {
        "cause": "awaiting_selection", "release": "resume", "ui_code": "root_fence_lifted_pending_selection"},
    "root_fence_member_pending_selection": {
        "cause": "awaiting_selection", "release": "resume", "ui_code": "root_fence_member_pending_selection"},
    "continuation_writer_unsettled": {"cause": "wait", "release": "none", "ui_code": "continuation_writer_unsettled",
                                      "wait_for": "continuation_writers", "door_replaceable": True},
    "owner": {"cause": "pause", "release": "resume", "ui_code": "owner_paused"},
    "budget": {"cause": "money", "release": "resume", "ui_code": "budget_paused"},
    "authority": {"cause": "authority", "release": "resume", "ui_code": "budget_paused"},
    "sleep": {"cause": "wait", "release": "none", "ui_code": "sleeping"},
}
# Product words shared by the owner's work projection and the mind's roster.
STOP_TEXT = {
    "paused_by_owner": "You paused it", "paused_by_mind": "Ouroboros paused it",
    "paused_unattributed": "", "saved_on_stop": "Saved when the app stopped",
    "after_restart": "Held after a restart", "after_panic": "Stopped by Panic",
    "spending_limit": "Spending limit reached", "technical_pause": "After a technical problem",
    "awaiting_selection": "Waiting for Ouroboros or you to resume it",
    "restore_failed": "Its saved state could not be restored", "held": "Held",
    "sleeping": "Sleeping until it is woken",
    "earlier_work_unconfirmed": "Continue starts when the earlier attempt's work is confirmed finished",
}
_CAUSE_CODES = {"pause": "paused_unattributed", "app_stop": "saved_on_stop", "panic": "after_panic",
                "money": "spending_limit", "authority": "technical_pause",
                "awaiting_selection": "awaiting_selection", "refusal": "restore_failed"}
_PAUSE_CAUSES = {"pause": "owner", "app_stop": "restart", "money": "budget", "authority": "authority"}


REFUSAL_FACTS = {
    "stale_queue_row_grant_consumed": {"grant_state": "consumed"},
    "stale_resume_grant_superseded": {"grant_state": "superseded"},
    "dispatch_outcome_unknown": {"dispatch_unknown": True},
}


def reason_policy(reason: str) -> dict:
    """Classify the existing vocabulary; an unknown name stays an unknown refusal."""
    if reason in STOP_REASONS:
        return dict(STOP_REASONS[reason])
    return {"cause": "refusal", "release": "resume", "ui_code": "restore_refused",
            **REFUSAL_FACTS.get(reason, {})}


def record_policy(record: dict | None) -> dict:
    record = record or {}
    policy = reason_policy(str(record.get("reason") or ""))
    if policy["cause"] != "refusal":
        policy["release"] = record.get("release", policy["release"])
    return policy


def hold_fact(task: dict | None) -> dict | None:
    """Current explicit stop or owned writer wait; never a technical refusal."""
    hold = task.get("_budget_pause_hold") if isinstance(task, dict) else None
    return hold if isinstance(hold, dict) and record_policy(hold)["cause"] != "refusal" else None


def pause_marker(task: dict) -> dict:
    """A point's refusal alone is not evidence that its execution parked."""
    point = task.get("_budget_pause")
    return {key: value for key, value in point.items() if key != "refusal"} if isinstance(point, dict) else {}


def current_stop(task: dict | None, fences: dict | None = None) -> dict:
    """Pure projection: installation, tree, member. Waiting is not a stop.

    ``fences`` contains an observed ``owner`` fence and the ``budget`` root map.
    No reads, clock decisions or copied causes. A tree overlay retains the
    member's independent release rule; releasing the tree cannot waive it.
    """
    task, fences = task if isinstance(task, dict) else {}, fences or {}
    hold = hold_fact(task)
    member = {**hold, **record_policy(hold), "scope": "member"} if hold else {}
    if member.get("cause") == "wait":
        member = {}
    if not member:
        pause = pause_marker(task)
        policy = reason_policy(str(pause.get("reason") or (
            "owner" if pause.get("cause") == "owner_pause" else
            "budget" if pause.get("exact_continuation") or pause.get("status") == "paused_before_dispatch" else "")))
        if policy["cause"] not in {"wait", "refusal"}:
            if policy["cause"] == "pause":
                policy = reason_policy("root_fence_member_pending_selection")
            member = {**pause, **policy, "scope": "member"}
    owner = fences.get("owner") or {}
    root_id = str(task.get("root_task_id") or task.get("id") or "")
    budget = (fences.get("budget") or {}).get(root_id) or {}
    tree = {}
    if owner.get("awaiting_selection"):
        member = member or {**reason_policy("root_fence_member_pending_selection"), "scope": "member"}
    elif owner.get("state") in {"requested", "paused"}:
        tree = {**owner, **reason_policy("owner"), "scope": "tree"}
    elif budget.get("status") in {"active", "paused"}:
        from ouroboros.budget_pause import budget_fence_selected

        if not budget_fence_selected(task, budget):
            tree = {**budget, **reason_policy("owner" if budget.get("cause") == "owner_pause" else "budget"),
                    "scope": "tree"}
    if tree.get("cause") == "pause" and not hold and member.get("cause") == "awaiting_selection":
        member = {}  # its owner marker is this tree stop, not a second decision
    stop = {**tree, **({"member_stop": member} if member else {})} if tree else member
    installation = fences.get("installation")
    if installation:
        return {**installation, **({"member_stop": stop} if stop else {}), "scope": "installation"}
    return stop


def _display_task(task: dict) -> dict:
    """Join the display snapshot's locator with its current durable point."""
    source = dict(task)
    pause = source.get("budget_pause") or {}
    if pause.get("state") in {"pausing", "paused", "resume_granted"}:
        source["_budget_pause"] = {"reason": "budget", **pause, **(source.get("_budget_pause") or {})}
    return source


def stop_presentation(task: dict, fences: dict | None = None) -> dict:
    """The same stop, reason, author and release rule for every surface. No storage probes."""
    from ouroboros.control_actor import pause_author

    task = _display_task(task)
    stop = task.get("stop") if isinstance(task.get("stop"), dict) else current_stop(task, fences)
    refusal = task.get("refusal") or continuation_refusal(task)
    point = pause_marker(task)
    if not stop and not refusal and point.get("reason") and record_policy(point)["cause"] == "refusal":
        refusal = {"reason": point["reason"]}
    cause = stop.get("cause")
    code = _CAUSE_CODES.get(cause, "held")
    author = pause_author(stop)
    if cause == "pause":
        code = {"owner": "paused_by_owner", "ouroboros": "paused_by_mind"}.get(author["actor"], code)
    elif cause == "app_stop" and stop.get("door") == "owner_restart":
        code = "after_restart"
    wait = record_policy(hold_fact(task)) if not stop else {}
    sleeping = not stop and record_policy(pause_marker(task) or task.get("budget_pause")).get("cause") == "wait"
    if not stop:
        code = ("earlier_work_unconfirmed" if wait.get("wait_for") else "restore_failed" if refusal
                else "sleeping" if sleeping else "held")
    # An interrupted, consumed handoff can still need addressed Resume even
    # though its stop was already released; the census names that existing door.
    needs_resume = refusal or task.get("can_resume")
    release = stop.get("release") or ("resume" if needs_resume else "none")
    if release_refusal(stop, {"actor": "ouroboros"}):
        release = "owner" if release_refusal(stop, {"actor": "owner"}) is None else "none"
    entry = {"code": code, "text": STOP_TEXT[code]}
    since = (author.get("at") or stop.get("held_at") or stop.get("requested_at") or stop.get("paused_at")
             or (hold_fact(task) or {}).get("held_at"))
    if since:
        entry["since"] = since
    if author.get("actor"):
        entry["by"] = author
    return {"stop": stop, "refusal": refusal, "reason": entry,
            "pause_cause": _PAUSE_CAUSES.get(cause, "sleep" if sleeping else "unknown"),
            "release": release, "resumable": release in {"resume", "owner"},
            "waiting": bool(not stop and not needs_resume and (wait.get("wait_for") or sleeping))}


def activity_stop_view(row: dict, raw: dict | None = None) -> dict:
    """Census adapter, including the older phase/cause shape; translations live only here."""
    raw = raw or {}
    task = {**row, **raw, "id": row.get("activity_id") or row.get("id")}
    if "hold" in raw:
        task["_budget_pause_hold"] = raw["hold"]
    fences = dict(raw.get("fences") or {})
    # Older census consumers supply only pause_cause. A declared owner author
    # describes a tree fence, not an orphaned member marker (R3).
    if "stop" not in task and not task.get("_budget_pause"):
        cause = row.get("pause_cause")
        if cause == "owner":
            fences.setdefault("owner", {"state": "requested", "by": raw.get("pause_by") or {},
                                        "requested_at": (raw.get("pause_by") or {}).get("at")})
        elif cause in {"budget", "authority", "sleep"}:
            task["_budget_pause"] = {"reason": cause}
        elif cause == "restart" and not task.get("_budget_pause_hold"):
            task["_budget_pause_hold"] = {"reason": "owner_restart_hold"}
    return stop_presentation(task, fences)


def release_refusal(stop: dict | None, actor: dict | None) -> dict | None:
    """Release permission, not readiness; empty actor means owner, mind doors must use ``mind_control``."""
    stop, actor = stop or {}, actor or {}
    if stop.get("release") == "owner" and actor.get("actor", "owner") != "owner":
        return {"ok": False, "error": "panic_requires_owner_resume"}
    if stop.get("release") == "none":
        return {"ok": False, "error": "stop_not_resumable"}
    return release_refusal(stop["member_stop"], actor) if stop.get("member_stop") else None


def door_can_replace(task: dict, reason: str) -> bool:
    """The existing door matrix: only sleep recovery / writer waits are weak.

    Sleep recovery itself replaces nothing. Exact money/owner pauses keep their
    own rule; a sleep marker is a wait and can acquire the door's stop.
    """
    prior = hold_fact(task)
    if prior:
        return bool(record_policy(prior).get("door_replaceable")
                    and reason_policy(reason).get("replaces_door", True))
    return not current_stop(task)


def writer_wait(task: dict | None) -> bool:
    return record_policy(hold_fact(task)).get("wait_for") == "continuation_writers"


_POINTS = ("_budget_pause", "_owner_wait_resume", "_working_recovery", "_budget_pause_resume", SELECTION_KEY)


def continuation_refusal(task: dict | None) -> dict:
    """Read a point's refusal, including the old shape until boot normalization."""
    task = task if isinstance(task, dict) else {}
    for key in _POINTS:
        point = task.get(key)
        if isinstance(point, dict) and isinstance(point.get("refusal"), dict):
            return point["refusal"]
    hold = task.get("_budget_pause_hold")
    if isinstance(hold, dict) and hold and record_policy(hold)["cause"] == "refusal":
        return {**hold, **record_policy(hold)}
    if task.get("_continuation_prepared"):
        return {"reason": "continuation_publication_unconfirmed"}
    if task.get("_owner_hold"):
        return {"reason": "_owner_hold"}
    return {}


def clear_refusal(task: dict) -> None:
    for key in _POINTS:
        point = task.get(key)
        if isinstance(point, dict) and "refusal" in point:
            task[key] = {k: v for k, v in point.items() if k != "refusal"}
            if not task[key]:
                task.pop(key)
    if isinstance(task.get("_budget_pause_hold"), dict) and hold_fact(task) is None:
        task.pop("_budget_pause_hold")


def selection_record(task: dict) -> dict:
    """Selection can release a hold, repair a point, or select an orphaned owner marker."""
    record = hold_fact(task) or continuation_refusal(task)
    if record:
        return record
    stop = current_stop(task)
    return stop if stop.get("cause") == "awaiting_selection" else {}


def refuse_continuation(task: dict, record: dict) -> dict:
    """Retain the refusal on existing custody, preserving any current decision.

    With no surviving source, the pause locator carries only the refusal's
    identities. That is not an exact pause and cannot re-arm a spent grant.
    """
    refusal = {**record, **REFUSAL_FACTS.get(record["reason"], {})}
    refusal.pop("release", None)
    for key in _POINTS[:3]:
        if isinstance(task.get(key), dict):
            task[key] = {**task[key], "refusal": refusal}
            break
    else:
        key = SELECTION_KEY if isinstance(task.get(SELECTION_KEY), dict) else "_budget_pause"
        task[key] = {**(task.get(key) or {}), "refusal": refusal}
    task.pop("_budget_pause_resume", None)  # the refused grant is not executable
    return refusal


def declare_stop(task: dict, record: dict) -> dict:
    """Write a decision or a point refusal; only the door matrix replaces a stop.

    Explicit Pause releases its authorized prior decision under the queue lock.
    Other producers cannot silently erase it while publishing custody facts.
    """
    policy = record_policy(record)
    if policy["cause"] == "refusal":
        return refuse_continuation(task, record)
    prior = hold_fact(task)
    door = policy["cause"] in {"app_stop", "panic"}
    if door and not door_can_replace(task, record["reason"]) or prior and not door:
        return prior or current_stop(task)
    task.pop("_budget_pause_resume", None)
    task.pop(SELECTION_KEY, None)
    task["_budget_pause_hold"] = record
    return record


def stop_record(reason: str, *, held_at: str, detail: str = "", by: dict | None = None,
                binding: dict | None = None) -> dict:
    """Build the current hold without conflating its name and the actor's text."""
    return {**(binding or {}), "reason": reason, "release": reason_policy(reason)["release"],
            "by": dict(by) if by else {"actor": "host", "task_id": "", "text": "", "at": held_at},
            "held_at": held_at, "detail": str(detail or "")[:300]}


def resume_selection(binding: dict, *, selected_by: str, selected_at: str, by: dict) -> dict:
    """Copy identities only. ``selected_by`` is the Q9 door, ``by`` its attribution."""
    return {**{key: binding[key] for key in SELECTION_BINDING if key in binding},
            "selected_by": selected_by, "selected_at": selected_at, "by": dict(by)}


def retain_consumed_selection(task: dict, pause_row: dict) -> bool:
    """Retire a matching spent handoff, keeping the root's selection, never its stop.

    Local lineage alone decides whether this row can lend: reciprocal retry
    authority is still read by dispatch. A member's spent handoff creates no
    replacement dispatch carrier. A current hold is never replaced.
    """
    from ouroboros.task_results import resolve_task_lineage

    handoff = task.get("_budget_pause_resume") if isinstance(task.get("_budget_pause_resume"), dict) else {}
    grant = pause_row.get("grant") if isinstance(pause_row.get("grant"), dict) else {}
    if not (handoff.get("authority") == "explicit_resume" and grant.get("authority") == "explicit_resume"
            and pause_row.get("state") == "resumed" and grant.get("consumed_at")
            and not grant.get("revoked_at") and grant.get("grant_id")
            and handoff.get("grant_id") == grant["grant_id"]
            and handoff.get("pause_id") == pause_row.get("pause_id")
            and handoff.get("source_ref") == pause_row.get("source_ref")
            and handoff.get("grant_generation") == grant.get("generation")
            and not isinstance(task.get("_budget_pause_hold"), dict)):
        return False
    lineage = resolve_task_lineage(task.get("id"), **{key: task.get(key) for key in (
        "metadata", "root_task_id", "parent_task_id", "delegation_role", "original_task_id", "timeout_retry_from")})
    if lineage["parent_task_id"] and lineage["root_task_id"] != lineage["task_id"]:
        task.pop(SELECTION_KEY, None)
        return False
    task[SELECTION_KEY] = resume_selection(
        {**grant, "root_task_id": str(task.get("root_task_id") or task.get("id") or ""),
         "grant_generation": int(grant["generation"]), "fence_id": str(grant.get("root_fence_id") or "")},
        selected_by=str(grant.get("selected_by") or "owner"),
        selected_at=str(grant.get("granted_at") or ""), by=grant.get("by") or {})
    return True


def resume_outcome(outcome: dict) -> dict:
    """Ordinary words beside typed refusals, shared by HTTP and both mind doors."""
    if outcome.get("ok"):
        return outcome
    if outcome.get("runs") and not outcome.get("blockers"):
        outcome = {**outcome, "blockers": [{"kind": "delegated_run", **run} for run in outcome["runs"]]}
    error = str(outcome.get("error") or "")
    phrases = {"panic_requires_owner_resume": "Only the owner can release a Panic hold.",
               "update_in_progress": "An update is in progress; press Resume after the restart.",
               "pause_source_unreadable": STOP_TEXT["restore_failed"],
               "pause_record_missing": STOP_TEXT["restore_failed"],
               "root_still_paused": STOP_TEXT["awaiting_selection"],
               "root_resume_grant_missing": STOP_TEXT["awaiting_selection"]}
    words = []
    for block in outcome.get("blockers") or ():
        kind, tid = block.get("kind"), block.get("task_id", "")
        if kind == "delegated_run":
            run = block.get("run_id") or block.get("invocation_id") or ""
            words.append(f"delegated run {run} is running" if block.get("state") == "running" else
                         "a delegated start has an unknown outcome (no run was recorded)"
                         if block.get("stop_outcome") == "pending_invocation_unbound" else
                         f"delegated run {run}: its stop is requested, not confirmed"
                         if block.get("state") == "stop_requested" else
                         f"delegated run {run}: its outcome is unknown")
            continue
        words.append({"model_handoff": "a model request has no recorded result",
                      "tool_handoff": f"a tool call of task {tid} has not returned",
                      "running_member": f"task {tid} is still running",
                      "dispatchable_member": f"task {tid} is queued to run",
                      "owned_process": f"a process of task {tid} is still alive",
                      "merge_operation": f"a merge of task {tid} is not settled",
                      "workspace_executor": f"a workspace process of task {tid} is not settled"}.get(
                          kind, str(kind or "unknown").replace("_", " ")))
    return {**outcome, "message": "Resume refused: " + (
        "; ".join(dict.fromkeys(words)) if words else phrases.get(error, error.replace("_", " ")))}
