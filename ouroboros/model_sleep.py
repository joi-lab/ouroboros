"""The model's own sleep: warm or cold, woken by what it chose, or at a time it chose.

``await_messages`` keeps its bounded in-slot default; warm/cold sleep is the
model's choice, never a duration threshold. It selects senders, readable task
terminals, own run terminals, own service exits (warm only) and/or a wake time.
Without selectors, any addressed mail wakes it. Owner words/controls and typed
descendant escalations bypass sender filters; ordinary unselected mail waits.
New questions from any ordinary run THIS author owns also wake it, never a
foreign run or reviewer custody. Exact question sources reach its transcript;
exposure is remembered per run/interaction only after delivery, never an answer.
Legacy continuations without exposure memory may show a pending question once
again; they do not reconstruct it from the latest run's supervision ACK.

Readiness is read from the CANONICAL records every time — the mailbox file,
the task results, the custody rows, the live service registry (execution facts
of the start that was selected) — never from a cursor or a sender-side
latch, so an event that lands between the first check and the park is seen by
the first check after it: the sequence is check → install the wait → recheck.
A failed source read cannot hide another ready source or the chosen time.
If nothing is ready, an explicit source's original error reaches the caller
(pollers keep sleeping; arming raises). Pollers and implicit question reads
(open custody rows only) never wait on store contention; arming still does.
Unavailable implicit observations are logged, not new prerequisites.
A terminal counts once as the stable fact that the task settled, never as a
later enrichment of its result. Readiness while the task cannot
run (no capacity, a lease, money, an owner Pause or a Restart hold) stays
recorded; readiness is never permission to bypass those.

Warm sleep parks the SAME stack through the existing owner wait: a pooled
worker lends its capacity and keeps its project lane; a direct turn keeps its
actor. The sleep interval is not execution: it is folded into the one
paused-interval carrier every finite-lifetime reader subtracts (live while
parked), so overlapping exclusions count once and prior elapsed time is kept.
An explicit calendar ``deadline_at`` never moves.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import pathlib
import time
import uuid
from typing import Any, Dict, Iterator, List

log = logging.getLogger(__name__)

MODE_IN_SLOT = "in_slot"
MODE_WARM = "warm"
MODE_COLD = "cold"
MODES = (MODE_IN_SLOT, MODE_WARM, MODE_COLD)
_MAX_SELECTED = 32


def _ids(value: Any, name: str) -> List[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must be a list of ids")
    ids = list(dict.fromkeys(item.strip() for item in value))
    if len(ids) > _MAX_SELECTED:
        raise ValueError(f"{name} names more than {_MAX_SELECTED} ids")
    return ids


def _wake_at(wake_at: Any, wake_after_sec: Any) -> str:
    """An absolute UTC ISO stamp, or ``""`` for no time wake."""
    from ouroboros.deadline_utils import parse_deadline_ts

    if wake_at not in (None, "") and wake_after_sec not in (None, ""):
        raise ValueError("give wake_at or wake_after_sec, not both")
    if wake_after_sec not in (None, ""):
        seconds = int(wake_after_sec)
        if seconds <= 0:
            raise ValueError("wake_after_sec must be positive")
        stamp = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
        return stamp.isoformat()
    if wake_at in (None, ""):
        return ""
    parsed = parse_deadline_ts(str(wake_at))
    if parsed is None:
        raise ValueError("wake_at must be an ISO-8601 time with a timezone")
    return parsed.isoformat()


def _canonical_root(ctx: Any) -> pathlib.Path:
    return pathlib.Path(getattr(ctx, "budget_drive_root", None) or ctx.drive_root)


def selectors(ctx: Any, *, senders: Any = None, tasks: Any = None, runs: Any = None, services: Any = None,
              wake_at: Any = None, wake_after_sec: Any = None) -> Dict[str, Any]:
    """Validate the selected sources; ``ValueError`` names the first bad one.

    Senders and tasks must be tasks this installation knows (a readable result);
    runs must be delegated runs THIS task owns (its custody rows); services must be
    this task's own, each pinned to the start it has NOW (``_service_pins``).
    """
    from ouroboros.task_results import load_task_result, validate_task_id

    chosen = {"senders": _ids(senders, "senders"), "tasks": _ids(tasks, "tasks"), "runs": _ids(runs, "runs"),
              "wake_at": _wake_at(wake_at, wake_after_sec)}
    root = _canonical_root(ctx)
    for key in ("senders", "tasks"):
        for task_id in chosen[key]:
            validate_task_id(task_id)
            if not load_task_result(root, task_id, strict=True):
                raise ValueError(f"{key}: task {task_id} is unknown")
    if chosen["runs"]:
        owned = _owned_runs(ctx)
        missing = [run_id for run_id in chosen["runs"] if run_id not in owned]
        if missing:
            raise ValueError(f"runs: {', '.join(missing)} are not delegated runs of this task")
    pins = _service_pins(ctx, _ids(services, "services"))
    if pins:  # absent otherwise: a sleep without services keeps its existing shape
        chosen["services"] = pins
    chosen["any_mail"] = not (chosen["senders"] or chosen["tasks"] or chosen["runs"] or pins)
    return chosen


# What pins ONE start of a service: its ``task_id:name`` id is the lookup key a
# stop/start reuses, so the start time and process identity (pid/pgid on the host,
# the backend pid on an executor) tell a replacement apart.
_SERVICE_IDENTITY = ("service_id", "started_at", "pid", "pgid", "backend_pid")


def _service_pins(ctx: Any, names: List[str]) -> List[Dict[str, Any]]:
    from ouroboros.tools import services as registry

    pins = []
    for name in names:
        facts = registry.service_execution_facts(registry._service_key(ctx, name))
        if facts is None:
            raise ValueError(f"services: {name} is not a service of this task")
        pins.append({"name": name, **{key: facts[key] for key in _SERVICE_IDENTITY if key in facts}})
    return pins


def _service_wake(pin: Dict[str, Any]) -> str:
    """``""`` while the pinned start runs; its exit, else ``unknown`` — never success by absence.

    Execution facts only (no readiness work). A record this process no longer holds
    (stopped, replaced by a new start, a registry a Restart did not carry) or cannot
    read is ``unknown``, as is a present record whose backend probe was inconclusive:
    the woken model judges it; nothing counts or times a retry.
    """
    name = str(pin.get("name") or "")
    try:
        from ouroboros.tools.services import service_execution_facts

        facts = service_execution_facts(str(pin.get("service_id") or ""))
    except Exception:
        log.warning("Service %s execution facts unreadable; waking as unknown", name, exc_info=True)
        facts = None
    if not facts or any(facts.get(key) != pin[key] for key in _SERVICE_IDENTITY if key in pin):
        return f"service:{name}:unknown"
    if facts.get("state") == "running":
        return ""
    if facts.get("state") == "exited":
        code = facts.get("returncode")
        return f"service:{name}:exited:{'unknown' if code is None else code}"
    return f"service:{name}:unknown"


def _owned_runs(ctx: Any, held_only: bool = False) -> dict:
    """This task's custody rows: every one for a selected terminal (it may already be
    settled), only the indexed open (``held``) ones for implicit question attention."""
    from ouroboros import delegate_custody as custody
    from ouroboros.delegate_custody_current import HELD, runs

    task_id = str(ctx.task_id)
    where, params = ("task_id=? AND state=?", (task_id, HELD)) if held_only else ("task_id=?", (task_id,))
    return {run.run_id: run for run in runs(pathlib.Path(custody.custody_root(ctx)), where, params).values()
            if run.task_id == task_id}


def _pending_question(run: Any) -> bool:
    from ouroboros.delegate_custody import TERMINAL_STATES

    return bool(run is not None and not run.review_owned and not run.settled and run.terminal_state not in
                TERMINAL_STATES and run.pending_interaction_ids and run.interaction_source)


def _question_runs(ctx: Any) -> dict:
    """Implicit attention is opportunistic, never a new arm-time prerequisite."""
    from ouroboros.store import acquisition_budget

    try:
        with acquisition_budget(0):
            return _owned_runs(ctx, held_only=True)
    except Exception:
        log.debug("Sleep question observations unavailable for %s", ctx.task_id, exc_info=True)
        return {}


def question_sources(ctx: Any, runs: Any = None) -> list:
    """Unseen own questions accompany a return even when another source woke it."""
    from ouroboros.sleep_custody import seen_interactions

    seen = seen_interactions(ctx)
    return [{"run_id": rid, "pending_interaction_ids": list(run.pending_interaction_ids),
             "interaction_source": dict(run.interaction_source)}
            for rid, run in (_question_runs(ctx) if runs is None else runs).items()
            if _pending_question(run) and set(run.pending_interaction_ids) - set(seen.get(rid) or ())]


def append_question_notice(ctx: Any, reason: str, messages: list, shown: Any = ()) -> None:
    """Expose the retained source on every return path, then remember notification only.

    ``shown`` rows already reached this transcript (the cold custody census): their refs
    count as delivered, so only questions beyond them are repeated, and the question
    that woke the author could not be re-read when neither they nor a fresh read
    show it pending. A failed fresh read marks nothing it did not show.
    """
    import json
    from ouroboros.sleep_custody import record_question_notice

    record_question_notice(ctx, shown)
    if reason.startswith("control:"):
        return
    runs = _question_runs(ctx)
    questions = question_sources(ctx, runs)
    waking = reason[13:] if reason.startswith("run_question:") else ""
    delivered = {str(row.get("run_id") or "") for row in shown if isinstance(row, dict)
                 and row.get("interaction_source") and row.get("pending_interaction_ids")}
    if questions:
        messages.append({"role": "user", "content": "[SYSTEM NOTICE]\nDelegated question observation: "
                         + json.dumps(questions, ensure_ascii=False)
                         + "\nRead the retained source or use delegate_wait; nothing answered the question."})
        record_question_notice(ctx, questions)
    elif waking and waking not in delivered and not _pending_question(runs.get(waking)):
        messages.append({"role": "user", "content": "[SYSTEM NOTICE]\nThe question that woke you could not "
                         "be re-read now. Nothing was acknowledged; inspect " + waking + "."})


def acknowledge_question_delivery(ctx: Any, text: str) -> None:
    """A complete ready-result view reached the transcript, not merely its producer."""
    import json
    from ouroboros.sleep_custody import record_question_notice

    try:
        result, _end = json.JSONDecoder().raw_decode(text)
    except (ValueError, TypeError):
        return
    if isinstance(result, dict) and isinstance(result.get("questions"), list):
        record_question_notice(ctx, result["questions"])


def wake_reason(ctx: Any, chosen: Dict[str, Any]) -> str:
    """Why the sleep is ready now (``""`` = not yet). Owner input is never filtered."""
    from ouroboros.deadline_utils import parse_deadline_ts, utc_now
    from ouroboros.owner_mailbox import KIND_CARD_INPUT, KIND_OWNER_TEXT, KIND_QUIZ_ANSWER, KIND_QUIZ_CLOSED, KIND_TASK_MESSAGE
    from ouroboros.owner_wait import _wait_entries
    from ouroboros.task_status import SETTLED_STATUSES

    first_error = None

    def read_source(read, *args, **kwargs):
        # Sources are alternatives, not prerequisites for each other. Retain
        # the original exception for the existing caller if none is ready.
        nonlocal first_error
        try:
            return read(*args, **kwargs)
        except Exception as exc:
            if first_error is None:
                first_error = exc
                log.debug("Sleep readiness source unreadable for %s", ctx.task_id, exc_info=True)
            return None

    for entry in read_source(_wait_entries, ctx) or ():
        kind = str(entry.get("kind") or KIND_OWNER_TEXT)
        if kind in {KIND_OWNER_TEXT, KIND_QUIZ_ANSWER, KIND_QUIZ_CLOSED, KIND_CARD_INPUT}:
            return "owner_text"
        if kind != KIND_TASK_MESSAGE:
            return f"control:{kind}"  # finalize_now, hurry, owner_pause, ... always wake
        source = str(entry.get("source_task_id") or "")
        if (entry.get("provenance") == "descendant_task" or chosen.get("any_mail")
                or source in chosen.get("senders", [])):
            return f"mail:{source or 'unknown'}"
    if chosen.get("tasks"):
        from ouroboros.task_store import task_versions

        versions = read_source(task_versions, _canonical_root(ctx), chosen["tasks"]) or {}
        for task_id in chosen["tasks"]:
            status = versions.get(task_id, (0, "", False))[1]
            if status in SETTLED_STATUSES:
                return f"task:{task_id}:{status}"
    from ouroboros.delegate_custody import TERMINAL_STATES
    from ouroboros.sleep_custody import seen_interactions

    # A selected terminal waits on contention like any arm-time read; the pollers
    # bound their whole readiness pass to a probe instead.
    owned = (read_source(_owned_runs, ctx) or {}) if chosen.get("runs") else {}
    for run_id in chosen.get("runs") or ():
        run = owned.get(run_id)
        if run is not None and (run.terminal_state in TERMINAL_STATES or run.settled):
            return f"run:{run_id}"
    # Questions need their responsible author, even when it selected another
    # branch. Reviewer custody belongs to its panel, never to this sleeper.
    seen = seen_interactions(ctx)
    for run_id, run in _question_runs(ctx).items():
        shown = set(seen.get(run_id) or ()) | set((chosen.get("interaction_seen") or {}).get(run_id) or ())
        if _pending_question(run) and set(run.pending_interaction_ids) - shown:
            return f"run_question:{run_id}"
    for pin in chosen.get("services") or ():
        observed = _service_wake(pin)  # already returns an unknown wake on a read failure
        if observed:
            return observed
    wake_at = parse_deadline_ts(chosen.get("wake_at") or "")
    if wake_at is not None and utc_now() >= wake_at:
        return "timeout"
    if first_error is not None:
        raise first_error
    return ""


def cold_blockers(ctx: Any, *, chosen: Dict[str, Any] | None = None,
                  allow_transfer: bool = False, transferred: Any = ()) -> List[Dict[str, str]]:
    """What this task still runs that a COLD sleep would leave unwatched.

    Only exact bound ordinary external runs can be handed to the body. Local
    producers, services, other actors and unknown starts still keep the actor.
    Arming may propose a transfer; release needs its exact accepted pairs.
    """
    from ouroboros.budget_pause import observe_task_runs
    from ouroboros import delegate_custody as custody

    from ouroboros.model_detach import is_detach

    chosen = chosen if chosen is not None else (getattr(ctx, "_model_sleep", None) or {})
    detach = is_detach(chosen)
    allow_transfer = allow_transfer and not detach
    if detach:
        transferred = ()
    blockers: List[Dict[str, str]] = []
    try:
        observed = observe_task_runs(custody.custody_root(ctx), str(ctx.task_id), reason="cold_sleep_check",
                                     request_stop=False)
    except Exception as exc:
        observed = {"custody_read": "failed", "error": str(exc), "runs": []}
    if observed.get("custody_read") != "ok":
        blockers.append({"kind": "custody_unreadable", "detail": str(observed.get("error") or "")})
    allowed = {tuple(pair) for pair in transferred}
    if allow_transfer:
        try:
            from ouroboros.sleep_custody import transferable_pairs
            pairs, _writer = transferable_pairs(custody.custody_root(ctx), str(ctx.task_id))
            allowed.update(tuple(pair) for pair in pairs)
        except Exception:
            pass  # the authoritative census below keeps unknown/untransferable work
    blockers.extend({"kind": "delegated_run", "run_id": str(run.get("run_id") or ""),
                     "invocation_id": str(run.get("invocation_id") or "")}
                    for run in observed.get("runs") or [] if isinstance(run, dict)
                    and (str(run.get("run_id") or ""), str(run.get("invocation_id") or "")) not in allowed)
    from ouroboros.tools import services

    with services._LOCK:
        records = [record for record in services._SERVICES.values() if record.task_id == str(ctx.task_id)]
    blockers.extend({"kind": "service", "name": record.name} for record in records
                    if record.proc.poll() is None)
    # Only this actor's accepted external obligations transfer. Every member's
    # other retained custody matters, including paused and terminal children.
    from ouroboros.owner_pause import tree_member_results, current_tool_operation, _member_coordinates
    from ouroboros.delegate_custody_current import tree_task_ids
    from ouroboros import process_custody as pc
    from ouroboros.platform_layer import pid_is_alive

    root = _canonical_root(ctx)
    tree_id = _member_coordinates(ctx)[1]
    dependencies = set(chosen.get("tasks", []) + chosen.get("senders", []))
    members = {str(ctx.task_id)}
    try:
        members.update(tree_task_ids(root, tree_id))  # the store answers or raises: unreadable is a blocker
        for task_id, row in tree_member_results(root, tree_id).items():
            members.add(task_id)
            own = task_id == str(ctx.task_id)
            if not own and row.get("status") == "running":
                blockers.append({"kind": "running_member", "detail": task_id})
            if not own and task_id in dependencies and row.get("status") in {"requested", "scheduled"}:
                # The root sleep fence would prevent this member from starting,
                # including a child whose terminal/mail was selected as our wake.
                blockers.append({"kind": "queued_member", "detail": task_id})
            own_sleep = current_tool_operation(ctx, "continue_in_background" if detach else "await_messages") if own else ""
            from ouroboros.tool_custody import retained_tool_custody
            if retained_tool_custody(root, task_id, row, excluding=own_sleep):
                blockers.append({"kind": "member_custody", "detail": task_id})
        for member in members - {str(ctx.task_id)}:
            observed = observe_task_runs(root, member, request_stop=False)
            if observed.get("custody_read") != "ok" or observed.get("runs"):
                blockers.append({"kind": "member_custody", "detail": member})
        from ouroboros.tool_custody import task_process_blockers
        blockers.extend(task_process_blockers(root, members))
        complete, processes = pc._read_ledger_strict(root)
        if not complete:
            raise OSError("process_custody_unreadable")
        for record in processes:
            if str(record.get("owner_task") or "") in members and (
                    pid_is_alive(int(record.get("pid") or 0)) or pc._service_group_survives_leader(record)):
                blockers.append({"kind": "owned_process", "detail": str(record.get("pid"))})
    except Exception as exc:
        blockers.append({"kind": "tree_custody_unreadable", "detail": str(exc)[:200]})
    return blockers


class ColdSleepBlocked(ValueError):
    def __init__(self, blockers):
        self.blockers = blockers
        super().__init__("a cold sleep would leave this task's own work unwatched: "
                         + "; ".join(f"{b['kind']} {b.get('run_id') or b.get('name') or b.get('detail') or ''}"
                                     for b in blockers)
                         + " — sleep warm, wait for them, or stop them first")


def request_sleep(ctx: Any, chosen: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Check, then arm the sleep: warm parks after this tool batch
    (``wait_after_tools``), cold at the next round boundary (``enter_cold_sleep``).

    Already ready (selected mail unread, a selected terminal reached, the time
    passed, owner input waiting): answered at once, nothing parks. A cold
    request proposes transfer of exact bound external work; other live writers
    and services refuse it because their process-local owner would end.
    """
    if mode == MODE_COLD and chosen.get("services"):
        raise ValueError("services wake only a warm sleep: a cold sleep ends the process that holds them")
    from ouroboros.model_detach import is_detach

    ready = wake_reason(ctx, chosen)
    if ready and (not is_detach(chosen) or ready.startswith("control:")):
        questions = [] if ready.startswith("control:") else question_sources(ctx)
        return {"reason": "ready", "woke_by": ready, "slept": False, "mode": mode,
                **({"questions": questions} if questions else {})}
    from ouroboros.sleep_custody import transferable_pairs
    retains_lane = mode == MODE_WARM
    if mode == MODE_COLD:
        try:
            _pairs, retains_lane = transferable_pairs(_canonical_root(ctx), str(ctx.task_id))
        except Exception:
            retains_lane = False  # cold_blockers reports the actual unavailable custody
    if retains_lane:
        from ouroboros.task_results import load_task_result

        project_id = str(getattr(ctx, "project_id", "") or (getattr(ctx, "task_metadata", None) or {}).get("project_id") or "")
        from ouroboros.owner_pause import _member_coordinates
        tree_id = _member_coordinates(ctx)[1]
        for selected in set(chosen.get("tasks", []) + chosen.get("senders", [])):
            row = load_task_result(_canonical_root(ctx), selected, strict=True) or {}
            if (project_id and row.get("project_id") == project_id
                    and str(row.get("root_task_id") or selected) != tree_id
                    and row.get("status") in {"requested", "scheduled"}):
                raise ValueError("this sleep retains the project lane needed by the selected queued root; "
                                 "choose another source or finish the external writer before releasing its lane")
    if mode == MODE_COLD:
        blockers = cold_blockers(ctx, chosen=chosen, allow_transfer=True)
        if blockers:
            raise ColdSleepBlocked(blockers)
    sleep_id = uuid.uuid4().hex
    ctx._owner_wait_requested = f"sleep:{sleep_id}"
    ctx._model_sleep = {"sleep_id": sleep_id, "mode": mode, **chosen}
    ctx._owner_wait_deadline_at = chosen.get("wake_at") or ""
    return {"reason": "sleep_armed", "sleep_id": sleep_id, "mode": mode, "slept": False,
            "note": ("The sleep begins after this tool batch completes; you wake at the next round with the "
                     "reason. Selected mail wakes you" if not chosen.get("any_mail")
                     else "The sleep begins after this tool batch completes; any addressed mail wakes you")
                    + "; owner input and a new question from your own executors always do."}


def begin(ctx: Any, *, started_at: float | None = None) -> None:
    """Open a sleep, or restore its known wall start onto this process's monotonic clock."""
    now = time.time()
    started = now if started_at is None else started_at
    waiter = getattr(ctx, "model_wait_context", None)
    if waiter is not None:
        waiter.sleep_started_monotonic = time.monotonic() - max(0.0, now - started)
    ctx._model_sleep_started = started


def end(ctx: Any) -> float:
    """The task runs again: fold the slept wall time into the ONE paused carrier."""
    started = float(getattr(ctx, "_model_sleep_started", 0.0) or 0.0)
    slept = max(0.0, time.time() - started) if started else 0.0
    ctx._model_sleep_started = 0.0
    ctx._budget_paused_sec = float(getattr(ctx, "_budget_paused_sec", 0.0) or 0.0) + slept
    waiter = getattr(ctx, "model_wait_context", None)
    if waiter is not None:
        waiter.sleep_started_monotonic = None
        waiter.budget_paused_sec = float(waiter.budget_paused_sec or 0.0) + slept
    return slept


IN_SLOT_WAIT_KIND = "in_slot_wait"


def announce_resumed(ctx: Any, outcome: Any = "") -> None:
    """The task's own stack runs again after a wait: re-announce it as its start does.

    A warm resume continues the same attempt, so no new start tells a chat
    transport that preparation goes on (DESIGN §9, Telegram typing). A stopping
    control resumes nothing; the owner's Resume of a warm Pause does. Best effort:
    a lost frame costs one missed cue, never the wait.
    """
    from ouroboros.utils import utc_now_iso

    reason, queue = str(outcome or ""), getattr(ctx, "event_queue", None)
    task_id = str(getattr(ctx, "task_id", "") or "")
    if queue is None or not task_id or (reason.startswith("control:") and reason != "control:owner_resume"):
        return
    try:
        queue.put({"type": "typing_start", "task_id": task_id, "chat_id": getattr(ctx, "current_chat_id", None),
                   "phase": "thinking", "ts": utc_now_iso()})
    except Exception:
        log.debug("resume announce failed for %s", task_id, exc_info=True)


def declare_in_slot_wait(ctx: Any, key: str, until_ts: float) -> None:
    """A tool holds this task's slot until ``until_ts`` waiting on what ``key`` names:
    ``mail:``, ``tasks:``, a delegated run (``delegate:<run_id>``) or a review panel
    (``review:<owner_id>:``). Re-declaring the same key renews it.

    Holding the slot is not preparing a reply; only what the key names may be
    (``gateway/reply_preparation.py``). The mark lives on the live context (a direct
    turn's only owner) and rides a typed ``in_slot_wait`` operation lease to the
    RUNNING row (a pooled task); both expire with the window, so a lost end outlives nothing.
    """
    from ouroboros.utils import emit_cognitive_operation_event

    waits = getattr(ctx, "_in_slot_waits", None)
    if not isinstance(waits, dict):
        waits = {}
        try:
            ctx._in_slot_waits = waits
        except Exception:  # a context that cannot carry the mark still waits; only the cue misses it
            log.debug("in-slot wait mark unavailable for %s", getattr(ctx, "task_id", ""), exc_info=True)
    waits[key] = float(until_ts)
    emit_cognitive_operation_event(
        getattr(ctx, "event_queue", None), task_id=str(getattr(ctx, "task_id", "") or ""),
        operation_id=f"{IN_SLOT_WAIT_KIND}:{key}", phase="started", kind=IN_SLOT_WAIT_KIND,
        task_attempt=getattr(ctx, "task_attempt", None), lease_until=float(until_ts))


def end_in_slot_wait(ctx: Any, key: str) -> None:
    """The slot wait ``key`` returned: retract its mark and lease, and re-announce the task."""
    from ouroboros.utils import emit_cognitive_operation_event

    waits = getattr(ctx, "_in_slot_waits", None)
    if isinstance(waits, dict):
        waits.pop(key, None)
    emit_cognitive_operation_event(
        getattr(ctx, "event_queue", None), task_id=str(getattr(ctx, "task_id", "") or ""),
        operation_id=f"{IN_SLOT_WAIT_KIND}:{key}", phase="finished", kind=IN_SLOT_WAIT_KIND,
        task_attempt=getattr(ctx, "task_attempt", None))
    announce_resumed(ctx)


@contextlib.contextmanager
def in_slot_wait(ctx: Any, window_sec: float, target: str = "mail") -> Iterator[None]:
    """``declare_in_slot_wait`` for one bounded window (plus a short teardown margin)."""
    key = f"{target}:{uuid.uuid4().hex}"
    declare_in_slot_wait(ctx, key, time.time() + max(0.0, float(window_sec)) + 5.0)
    try:
        yield
    finally:
        end_in_slot_wait(ctx, key)


def wake_notice(chosen: Dict[str, Any], outcome: str, slept: float | None) -> Dict[str, Any]:
    """Host facts for the woken model: what woke it — a fact, never a verdict.

    ``slept`` is ``None`` when no sleep start was recorded: no duration is claimed."""
    what = {
        "timeout": "the wake time you chose",
    }.get(outcome, "")
    if not what:
        if outcome.startswith("task:"):
            _prefix, task_id, status = (outcome.split(":", 2) + ["", ""])[:3]
            what = f"task {task_id} reaching {status} — read its result; settled is not success"
        elif outcome.startswith("run:"):
            what = f"delegated run {outcome[4:]} reaching an engine terminal state — inspect its outcome; collection may remain owed"
        elif outcome.startswith("run_question:"):
            what = f"delegated run {outcome[13:]} requesting input — inspect its retained question and decide how to answer"
        elif outcome.startswith("service:"):
            _prefix, name, state, code = (outcome.split(":", 3) + ["", "", ""])[:4]
            code = f"return code {code}" if code != "unknown" else "a return code this backend does not observe"
            what = (f"service {name} exiting with {code} — read its logs; an exit is a fact, not a verdict"
                    if state == "exited" else
                    f"the selected start of service {name} becoming unobservable (stopped, replaced by a new start, "
                    "lost to a restart, or an inconclusive backend probe) — its outcome is unknown, not success")
        elif outcome.startswith("mail:"):
            what = f"mail from {outcome[5:]}"
        elif outcome.startswith("control:") or outcome == "owner_text":
            what = "the owner's message or control"
        else:
            what = outcome or "an unconfirmed input"
    return {"role": "user", "content": (
        "[SYSTEM NOTICE]\nYou slept" + ("" if slept is None else f" {slept:.0f}s")
        + f" ({chosen.get('mode') or 'warm'}) and were woken by {what}. "
        "Mail you did not select stayed unread and reaches you now with everything else pending. "
        "The sleep did not count as execution time; an explicit deadline did not move."
        + (" A cold sleep ended your previous process: its browser and task-local services are gone; "
           "re-read files before building on them." if chosen.get("mode") == "cold" else ""))}


def enter_cold_sleep(limit_ctx: Any) -> None:
    """The round boundary after a COLD sleep was armed (``model_sleep``).

    The loop's own continuation is saved and the process parks under the same
    exact-pause carrier; nothing wakes it but its selected sources through the
    supervisor's sleep-wake policy. Bound external work needs the body's exact
    acceptance before release. Other writers and local services refuse the park:
    the model is told the blockers and stays awake — never a killed helper.
    """
    from ouroboros.budget_pause import request_pause, RAIL_MODEL_SLEEP
    from ouroboros.model_detach import is_detach, pool_available

    ctx = getattr(getattr(limit_ctx, "tools", None), "_ctx", None)
    sleep = getattr(ctx, "_model_sleep", None) if ctx is not None else None
    if not isinstance(sleep, dict) or sleep.get("mode") != "cold":
        return
    blockers = cold_blockers(ctx, allow_transfer=True)
    if is_detach(sleep) and not pool_available().get("available"):
        blockers.append({"kind": "worker_pool_unavailable"})
    if blockers:
        if getattr(ctx, "_owner_wait_requested", "") == "sleep:" + str(sleep.get("sleep_id") or ""):
            ctx._owner_wait_requested, ctx._owner_wait_deadline_at = "", ""
        ctx._model_sleep = None
        limit_ctx.messages.append({"role": "user", "content": (
            "[SYSTEM NOTICE]\nYour cold sleep was not taken: this task still runs "
            + "; ".join(f"{b['kind']} {b.get('run_id') or b.get('name') or b.get('detail') or ''}" for b in blockers)
            + ". Sleep warm, wait for it, or stop it first.")})
        return
    request_pause(limit_ctx, rail=RAIL_MODEL_SLEEP, scope="task", reason_text="The model chose a cold sleep.")
    if getattr(ctx, "_owner_wait_requested", "") == "sleep:" + str(sleep.get("sleep_id") or ""):
        ctx._owner_wait_requested, ctx._owner_wait_deadline_at = "", ""
    ctx._model_sleep = None  # no continuation owner: the ordinary loop continues awake


