"""Off-loop workspace preparation for acting ``schedule_subagent`` children.

An acting child's write surface (an isolated current-tree copy, a genesis
project, or an external workspace's Git binding) costs lock waits, Git, copy
and index work. The supervisor event loop only claims the producer's REQUESTED
row for one preparation token (the existing admission reservation map) and
commits the durable ``workspace_preparation`` marker on that row; one FIFO
thread per supervisor generation then runs the existing schedule owner with the
claim (``events_schedule_task._handle_schedule_task(..., preparation=claim)``).
Command and event draining never wait on a copy, and physical preparations stay
serialized in acceptance order. A host that never opened
a generation (a direct caller of the handler) keeps the historical inline path.
Positive entry contention keeps the unaccepted event on its original transport;
after marker acceptance, acquisition waits retain the same FIFO stack off Q.

The marker carries the token, the owning process (pid and birth) and
generation, the phase and, once the copy returned, the owned resource handle.
Phases are positive facts written before their effect: ``queued`` (claimed,
nothing started), ``copying`` (physical work may have begun), ``ready`` (the copy
returned), ``admitting`` (committed before the final enqueue+receipt interlock
is entered); the scheduled receipt (``admitted``) or the never-admitted refusal
(``refused``) closes it. Stop, Pause, a closed generation or lost ownership is
read before the copy starts and again after it returns; the final enqueue
revalidates Project binding, root fences and the token under Q.

``recover_interrupted_preparations`` (startup after queue restore, and the
periodic reconcile pass) settles ``queued``/``copying``/``ready`` as never admitted
only once the recorded owner positively ended; ``admitting`` or unreadable
authority stays unresolved on the existing ``unknowns`` carrier, never inferred
unsent from an absent queue row. Disposal is exact: only the recorded handle of
this token, only after its refusal receipt and the copy's return; anything else
stays registered residue for the existing worktree retention. Recovery never
re-copies or dispatches.
"""

from __future__ import annotations

import collections
import copy
import functools
import logging
import os
import pathlib
import subprocess
import threading
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from ouroboros.task_results import STATUS_REQUESTED, load_task_result, write_task_result
from ouroboros.store import StoreBusy
from ouroboros.tool_capabilities import ACTING_SUBAGENT_MODE
from ouroboros.utils import append_jsonl, utc_now_iso
from supervisor.task_admission import WORKSPACE_PREPARATION_FIELD as MARKER

log = logging.getLogger(__name__)

QUEUED, COPYING, READY, ADMITTING, ADMITTED, REFUSED = (
    "queued", "copying", "ready", "admitting", "admitted", "refused")
UNSENT_PHASES = frozenset({QUEUED, COPYING, READY})
MARKER_UNWRITABLE = "preparation_marker_unwritable"
INTERRUPTED_BY_RESTART = "preparation_interrupted_by_restart"
INTERRUPTED_BY_GENERATION_EXIT = "preparation_interrupted_by_generation_exit"
EXECUTOR_UNAVAILABLE = "preparation_executor_unavailable"
ADMISSION_UNCERTAIN = "workspace_preparation_admission_uncertain"
_RESOURCE_KEYS = ("surface", "path", "branch", "created_at", "base_sha")
_RETRY_TOKEN = "_workspace_preparation_token"
_DETAILS = {
    MARKER_UNWRITABLE: ("Subagent not scheduled: its workspace preparation could not record its progress "
                        "durably; it was never admitted and is not retried automatically."),
    INTERRUPTED_BY_RESTART: ("Subagent not scheduled: the server stopped while its workspace was being "
                             "prepared; it was never admitted and is not retried automatically."),
    INTERRUPTED_BY_GENERATION_EXIT: ("Subagent not scheduled: the supervisor generation that owned its "
                                     "workspace preparation ended first; it was never admitted and is not "
                                     "retried automatically."),
    EXECUTOR_UNAVAILABLE: ("Subagent not scheduled: the workspace preparation worker could not start; "
                           "it was never admitted."),
}
_AUTHORITY_UNKNOWN = ("Subagent not scheduled: its durable result is unreadable, so its preparation was not "
                      "started and the existing authority was preserved.")

_STATE_LOCK = threading.Lock()
_CURRENT: list = [None]  # the open generation's owner; None keeps the historical inline path
_LIVE: Dict[str, "PreparationClaim"] = {}  # token -> claim, from its claim until its job ends
_OWNER: Dict[str, Any] = {}


def store_contention(error: BaseException) -> bool:
    """A temporary acquisition gap, not unreadable authority or an unknown commit."""
    return (isinstance(error, StoreBusy) and error.reason == "contention"
            and getattr(error, "rollback_confirmed", None) is not False)


class PreparationStopped(Exception):
    def __init__(self, refusal: Dict[str, Any]):
        self.refusal = refusal
        super().__init__(refusal["detail"])


def defer_schedule_event(ctx: Any, event: Dict[str, Any]) -> bool:
    """Retain an unaccepted schedule on its existing transport, before any effect."""
    try:
        from supervisor.workers import get_event_q
        endpoint = getattr(ctx, "event_queue", None)
        if endpoint is None:
            endpoint = get_event_q(create=False)
        endpoint.put(event)
        return True
    except Exception:
        log.warning("Schedule request %s could not retain its event transport", event.get("task_id"), exc_info=True)
        from supervisor.events_schedule_task import _reject_schedule_task
        _reject_schedule_task(
            ctx, tid=str(event.get("task_id") or ""), chat_id=event.get("chat_id"),
            delegation_role=str(event.get("delegation_role") or "subagent"), parent_id=event.get("parent_task_id"),
            root_task_id=str(event.get("root_task_id") or event.get("task_id") or ""),
            role=str(event.get("role") or ""), result_fields={}, persist_result=False,
            reason_code="scheduled_result_authority_unknown",
            detail="Subagent not scheduled: its event transport is unavailable; the request remains unconfirmed.")
        return False


def _process_owner() -> Dict[str, Any]:
    """This process's recorded identity, read once per process (``ps`` on macOS)."""
    if _OWNER.get("pid") != os.getpid():
        from ouroboros.platform_layer import process_start_time

        _OWNER.clear()
        _OWNER.update(pid=os.getpid(), birth=process_start_time(os.getpid()))
    return dict(_OWNER)


class PreparationGeneration:
    """One supervisor generation's FIFO preparation owner: one daemon thread at a time.

    ``submit`` never waits: it appends and starts the thread when none runs; the
    thread drains jobs in order and exits when the queue is empty. Closing never
    joins: a running copy keeps its custody until it returns.
    """

    def __init__(self, stop: Optional[Callable[[], bool]] = None):
        self.token = uuid.uuid4().hex
        self.owner = _process_owner()
        self._stop = stop
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._jobs: collections.deque = collections.deque()
        self._thread: Optional[threading.Thread] = None

    def closed(self) -> bool:
        """Closed explicitly, or its server generation / process stop or restart was requested."""
        return self._closed.is_set() or bool(self._stop is not None and self._stop())

    def close(self) -> None:
        self._closed.set()

    def submit(self, job: Callable[[], None]) -> bool:
        """Queue ``job`` behind earlier ones; False when no thread could start for it."""
        with self._lock:
            self._jobs.append(job)
            if self._thread is not None:
                return True
            thread = threading.Thread(target=self._drain, name="workspace-preparation", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                log.warning("Workspace preparation thread could not start", exc_info=True)
                self._thread = None
                self._jobs.clear()  # no thread ran, so the queue held only this job
                return False
            return True

    def on_executor(self) -> bool:
        return self._thread is threading.current_thread()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until nothing is queued or running (tests, diagnostics; never the event loop)."""
        with self._idle:
            return self._idle.wait_for(lambda: self._thread is None and not self._jobs, timeout)

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._jobs:
                    self._thread = None
                    self._idle.notify_all()
                    return
                job = self._jobs.popleft()
            try:
                job()
            except Exception:
                log.warning("Workspace preparation job raised", exc_info=True)


def start_generation(*, stop: Optional[Callable[[], bool]] = None) -> PreparationGeneration:
    """Open the preparation owner of a starting supervisor generation (an older one closes).

    ``stop`` answers True once that generation must not start or admit anything
    more: its own stop token, an explicit server stop, or a requested restart.
    """
    generation = PreparationGeneration(stop)
    with _STATE_LOCK:
        previous, _CURRENT[0] = _CURRENT[0], generation
    if previous is not None:
        previous.close()
    return generation


def close_generation(generation: Optional[PreparationGeneration] = None) -> None:
    """Close without waiting: queued claims never start; a running copy keeps its custody."""
    target = generation if generation is not None else _CURRENT[0]
    if target is not None:
        target.close()


def _requests_preparation(evt: Dict[str, Any]) -> bool:
    requested = evt.get("task_constraint")
    return isinstance(requested, dict) and str(requested.get("mode") or "") == ACTING_SUBAGENT_MODE


def _no_store_wait():
    """The short event-entry claim/marker/refusal never waits under Q."""
    from ouroboros.store import acquisition_budget
    return acquisition_budget(0)


def accept(evt: Dict[str, Any], ctx: Any, route: Dict[str, Any]) -> bool:
    """Claim an acting child's preparation for this generation's FIFO; False keeps the inline path.

    Event-loop work only, all without a storage wait: the short reservation
    under Q, the durable ``queued`` marker before FIFO entry, a nonblocking
    submit. Positive contention retains this same request on the existing event
    transport; it has not entered the preparation FIFO. A duplicate coalesces
    with its live claim. Hard unreadability retains the existing typed refusal.
    """
    generation = _CURRENT[0]
    if (generation is None or str(route.get("delegation_role") or "") != "subagent"
            or not str(evt.get("task_id") or "").strip() or not _requests_preparation(evt)):
        return False
    with _STATE_LOCK:
        retry = str(evt.get(_RETRY_TOKEN) or "")
        claim = _LIVE.get(retry) if retry else None
        if retry and (claim is None or claim.task_id != str(evt.get("task_id") or "")
                      or {key: value for key, value in evt.items() if key != _RETRY_TOKEN} != claim.event):
            return True  # an ended generation's frame cannot mint a new owner
        if claim is None:
            if any(value.task_id == str(evt["task_id"]) for value in _LIVE.values()):
                return True
            claim = PreparationClaim.live(generation, evt, ctx, route)
            _LIVE[claim.token] = claim
        elif claim.submitted:
            return True
    from supervisor.task_admission import claim_requested_child

    submitted = deferred = False
    with _no_store_wait():
        try:
            if not claim_requested_child(claim.ctx, claim.task_id, claim.token):
                log.info("Ignoring replayed schedule event for task %s", claim.task_id)
                return True
            if claim.generation.closed():
                claim.refuse(_DETAILS[INTERRUPTED_BY_GENERATION_EXIT], INTERRUPTED_BY_GENERATION_EXIT)
                return True
            landed = _transition(claim.ctx.DRIVE_ROOT, claim.task_id, claim.token, claim.queued_marker, expected=None)
            if landed is not True:
                claim.unrecorded(landed)
                return True
            submitted = claim.submitted = claim.generation.submit(functools.partial(_run, claim))
            if not submitted:
                claim.refuse(_DETAILS[EXECUTOR_UNAVAILABLE], EXECUTOR_UNAVAILABLE)
            return True
        except Exception as exc:
            if store_contention(exc):
                deferred = defer_schedule_event(claim.ctx, {**claim.event, _RETRY_TOKEN: claim.token})
            else:
                log.warning("Preparation claim of %s found unreadable authority", claim.task_id, exc_info=True)
                claim.disclose_unknown()
            return True
        finally:
            if not submitted and not deferred:
                claim.release()
                _finish(claim)


def _run(claim: "PreparationClaim") -> None:
    """The FIFO job: the existing schedule owner, on this thread, under the claim."""
    try:
        from supervisor.events_schedule_task import _handle_schedule_task

        _handle_schedule_task(claim.event, claim.ctx, preparation=claim)
    except PreparationStopped as exc:
        # Raised only by a proven-unsent store gate, never around enqueue or copy.
        try:
            claim.refuse(**exc.refusal)
        except Exception:
            log.warning("Stopped preparation %s retains its recovery marker", claim.task_id, exc_info=True)
    except Exception as exc:
        log.warning("Workspace preparation of %s failed", claim.task_id, exc_info=True)
        if not claim.crossed:  # past the boundary the admission path owns the outcome
            try:
                claim.refuse(f"Subagent not scheduled: its workspace preparation failed: "
                             f"{type(exc).__name__}: {exc}", "admission_preparation_failed")
            except Exception:
                log.warning("Preparation refusal of %s failed", claim.task_id, exc_info=True)
    finally:
        claim.release()
        _finish(claim)


def _finish(claim: "PreparationClaim") -> None:
    with _STATE_LOCK:
        if _LIVE.get(claim.token) is claim:
            _LIVE.pop(claim.token, None)


class PreparationClaim:
    """One requested child's preparation, owned by one token."""

    def __init__(self, *, task_id: str, ctx: Any, route: Dict[str, Any], token: str,
                 generation: Optional[PreparationGeneration], generation_token: str,
                 owner: Dict[str, Any], event: Optional[Dict[str, Any]] = None, surface: str = "",
                 resource: Optional[Dict[str, Any]] = None, claimed_at: str = "",
                 unidentified_residue: bool = False):
        self.task_id, self.ctx, self.route, self.token = task_id, ctx, route, token
        self.generation, self.generation_token, self.owner = generation, generation_token, dict(owner)
        self.event, self.surface = event or {}, surface
        self.resource = dict(resource or {})
        self.claimed_at = claimed_at or utc_now_iso()
        self.unidentified_residue = unidentified_residue
        self.crossed = False
        self.submitted = False
        self.queued_marker = self.marker(QUEUED)
        self.refusal_receipt = uuid.uuid4().hex

    def store_gate(self, operation: Callable[[], Any], *, controls: bool = True,
                   retryable: Optional[Callable[[], bool]] = None) -> Any:
        """Keep this FIFO stack across a known-unsent acquisition, outside every Q scope.

        Callers wrap one read/CAS/interlock, never a copy or arbitrary handler.
        ``retryable`` closes the gate before any possible queue handoff.
        """
        from ouroboros._usage_wait import ACQUISITION_SLICE_SEC
        from supervisor import queue

        while True:
            try:
                return operation()
            except StoreBusy as exc:
                if (not store_contention(exc) or self.generation is None or not self.generation.on_executor()
                        or retryable is not None and not retryable()):
                    raise
                if queue._queue_lock._is_owned():
                    raise RuntimeError("Preparation acquisition wait still owns Q") from exc
                if controls:
                    try:
                        refusal = self._control_refusal()
                    except StoreBusy as control_gap:
                        if not store_contention(control_gap):
                            raise
                    else:
                        if refusal is not None:
                            raise PreparationStopped(refusal) from exc
                    self.generation._closed.wait(ACQUISITION_SLICE_SEC)
                else:
                    # Closing forbids new execution, not finishing this same
                    # refusal receipt. The closed event would spin here.
                    threading.Event().wait(ACQUISITION_SLICE_SEC)

    @classmethod
    def live(cls, generation: PreparationGeneration, evt: Dict[str, Any], ctx: Any,
             route: Dict[str, Any]) -> "PreparationClaim":
        requested = evt.get("task_constraint") if isinstance(evt.get("task_constraint"), dict) else {}
        return cls(task_id=str(evt.get("task_id") or "").strip(), ctx=ctx, route=dict(route),
                   token=uuid.uuid4().hex, generation=generation, generation_token=generation.token,
                   owner=generation.owner, event=copy.deepcopy(evt),
                   surface=str(requested.get("surface") or ""))

    @classmethod
    def recovered(cls, drive_root: Any, row: Dict[str, Any], marker: Dict[str, Any]) -> "PreparationClaim":
        """The custody an ended owner left: its identity and only the handle it recorded."""
        from supervisor.message_bus import send_with_budget

        tid = str(row.get("task_id") or "")
        phase = str(marker.get("phase") or "")
        resource = marker.get("resource") if phase == READY and isinstance(marker.get("resource"), dict) else {}
        route = {"chat_id": row.get("chat_id"), "delegation_role": "subagent",
                 "parent_id": row.get("parent_task_id"), "role": str(row.get("role") or ""),
                 "root_task_id": str(row.get("root_task_id") or row.get("parent_task_id") or tid)}
        ctx = SimpleNamespace(DRIVE_ROOT=pathlib.Path(drive_root), send_with_budget=send_with_budget)
        return cls(task_id=tid, ctx=ctx, route=route, token=str(marker.get("token") or ""), generation=None,
                   generation_token=str(marker.get("generation") or ""),
                   owner=marker.get("owner") if isinstance(marker.get("owner"), dict) else {},
                   surface=str(marker.get("surface") or ""), resource=resource,
                   claimed_at=str(marker.get("claimed_at") or ""), unidentified_residue=phase == COPYING)

    def marker(self, phase: str, **facts: Any) -> Dict[str, Any]:
        return {"token": self.token, "phase": phase, "generation": self.generation_token,
                "owner": dict(self.owner), "surface": self.surface, "claimed_at": self.claimed_at,
                "updated_at": utc_now_iso(), **({"resource": dict(self.resource)} if self.resource else {}),
                **facts}

    def refusal_fields(self) -> Dict[str, Any]:
        disposition = ("after_refusal_receipt" if self.resource
                       else "unidentified_residue_retained" if self.unidentified_residue else "none")
        return {MARKER: self.marker(REFUSED, resource_disposition=disposition),
                "_admission_refusal_token": self.refusal_receipt}

    def refuse(self, detail: str, reason_code: str, *, extra_fields: Optional[Dict[str, Any]] = None,
               persist_result: bool = True) -> bool:
        """The typed never-admitted refusal through the schedule refusal and notice owner."""
        from supervisor.events_schedule_task import _reject_schedule_task

        route = self.route
        root_task_id = str(route.get("root_task_id") or self.task_id)
        return bool(self.store_gate(functools.partial(_reject_schedule_task,
            self.ctx, tid=self.task_id, chat_id=route.get("chat_id"),
            delegation_role=str(route.get("delegation_role") or "subagent"), parent_id=route.get("parent_id"),
            root_task_id=root_task_id, role=str(route.get("role") or ""),
            result_fields={"parent_task_id": route.get("parent_id"), "root_task_id": root_task_id,
                           "delegation_role": "subagent", "role": str(route.get("role") or "")},
            detail=detail, reason_code=reason_code, extra_fields=extra_fields,
            persist_result=persist_result, preparation=self), controls=False))

    def disclose_unknown(self) -> None:
        """Unreadable authority before any claim: the existing notice only, nothing reserved or written."""
        from supervisor.events_schedule_task import _reject_schedule_task

        route = self.route
        _reject_schedule_task(
            self.ctx, tid=self.task_id, chat_id=route.get("chat_id"),
            delegation_role=str(route.get("delegation_role") or "subagent"), parent_id=route.get("parent_id"),
            root_task_id=str(route.get("root_task_id") or self.task_id), role=str(route.get("role") or ""),
            result_fields={}, detail=_AUTHORITY_UNKNOWN, reason_code="scheduled_result_authority_unknown",
            persist_result=False)

    def after_refusal(self, remove_drive: Callable[[], None]) -> None:
        """After the receipt: this token's own copy, then the child drive, off the event loop.

        Recovery may clean here; live claims enqueue even from the FIFO thread
        so a caller's admission interlock is released first. Without an executor,
        retain the recorded copy and child drive for their existing retention
        owners; a confirmed refusal never lends the event thread to cleanup.
        """
        def cleanup() -> None:
            self.dispose()
            remove_drive()

        generation = self.generation
        if generation is None:
            cleanup()
        elif (generation.on_executor() or not generation.closed()) and generation.submit(cleanup):
            return
        else:
            log.info("Refused preparation %s (%s) retains its resources for ordinary retention: "
                     "no cleanup executor", self.task_id, self.token)

    def unrecorded(self, landed: Optional[bool]) -> None:
        """A phase did not land: lost ownership is abandoned, an unwritable store refused."""
        if landed is False:
            self.abandon()
        else:
            self.refuse(_DETAILS[MARKER_UNWRITABLE], MARKER_UNWRITABLE)

    def prepare(self, resolve: Callable[..., tuple]) -> Optional[tuple]:
        """Run ``resolve(handle_out)`` (the copy) under this claim, off the event loop.

        Returns the resolver's tuple, or None once a control, a closed
        generation or lost ownership ended the claim (its refusal or
        abandonment already done). Controls are read before the copy starts
        and again after it returns; a copy is never interrupted mid-way.
        """
        if not self._may_proceed():
            return None
        landed = self.store_gate(functools.partial(
            _transition, self.ctx.DRIVE_ROOT, self.task_id, self.token, self.marker(COPYING), expected=(QUEUED,)))
        if landed is not True:
            self.unrecorded(landed)
            return None
        handle: Dict[str, Any] = {}
        try:
            resolved = resolve(handle)
        finally:
            self.resource = {key: handle[key] for key in _RESOURCE_KEYS if key in handle}
        landed = self.store_gate(functools.partial(
            _transition, self.ctx.DRIVE_ROOT, self.task_id, self.token, self.marker(READY), expected=(COPYING,)))
        if landed is not True:
            self.unrecorded(landed)
            return None
        return resolved if self._may_proceed() else None

    def enter_admission(self) -> Optional[bool]:
        """Commit ``admitting`` before the admission path: True crossed, False lost, None unknown."""
        landed = self.store_gate(functools.partial(
            _transition, self.ctx.DRIVE_ROOT, self.task_id, self.token, self.marker(ADMITTING), expected=(READY,)))
        self.crossed = landed is not False
        return landed

    def release(self) -> None:
        from supervisor import queue

        queue.release_task_admission(self.task_id, self.token)

    def abandon(self) -> bool:
        """Leave the row unwritten; free this token's copy only once another owner holds the row.

        Returns whether another owner provably holds it. A row still this token's
        (an unwritten refusal) or unreadable keeps the copy for recovery.
        """
        from supervisor import queue
        from supervisor.task_admission import own_requested_child

        with queue._queue_lock:
            reserved = queue.ADMISSION_RESERVATIONS.get(self.task_id)
        self.release()
        try:
            row = load_task_result(self.ctx.DRIVE_ROOT, self.task_id, strict=True) or {}
        except Exception:
            return False
        if own_requested_child(row, self.token, reserved):
            return False
        if self.resource and not _row_references(row, self.resource):
            self.dispose()
        return True

    def dispose(self) -> str:
        """Remove this token's recorded copy (its refusal is confirmed and the copy returned)."""
        if not self.resource:
            return "none"
        resource, self.resource = self.resource, {}
        outcome = _dispose_resource(self.task_id, resource)
        try:
            append_jsonl(pathlib.Path(self.ctx.DRIVE_ROOT) / "logs" / "supervisor.jsonl", {
                "ts": utc_now_iso(), "type": "workspace_preparation_resource", "task_id": self.task_id,
                "preparation_token": self.token, "surface": str(resource.get("surface") or ""),
                "path": str(resource.get("path") or ""), "outcome": outcome})
        except Exception:
            log.debug("Preparation disposal row for %s was not written", self.task_id, exc_info=True)
        return outcome

    def _may_proceed(self) -> bool:
        owned = self.store_gate(self._owned)
        if owned is None:
            self.refuse(_AUTHORITY_UNKNOWN, "scheduled_result_authority_unknown", persist_result=False)
            return False
        if not owned:
            self.abandon()
            return False
        refusal = self.store_gate(self._control_refusal)
        if refusal is not None:
            self.refuse(refusal["detail"], refusal["reason_code"], extra_fields=refusal.get("extra_fields"))
            return False
        return True

    def _owned(self) -> Optional[bool]:
        from supervisor import queue
        from supervisor.task_admission import own_requested_child

        with queue._queue_lock:
            reserved = queue.ADMISSION_RESERVATIONS.get(self.task_id)
        if reserved and reserved != self.token:
            return False
        try:
            current = load_task_result(self.ctx.DRIVE_ROOT, self.task_id, strict=True) or {}
        except Exception as exc:
            if store_contention(exc):
                raise
            return None
        return own_requested_child(current, self.token, reserved)

    def _control_refusal(self) -> Optional[Dict[str, Any]]:
        """The refusal a current control requires: a closed generation, then the root latches
        ``enqueue_task`` reads (Stop/cancel, budget or owner Pause, acceptance fence)."""
        if self.generation is None or self.generation.closed():
            return {"detail": _DETAILS[INTERRUPTED_BY_GENERATION_EXIT], "reason_code": INTERRUPTED_BY_GENERATION_EXIT}
        from supervisor import queue
        from supervisor.task_admission import scheduled_admission_rejection

        root = str(self.route.get("root_task_id") or self.task_id)
        probe = {"id": self.task_id, "root_task_id": root, "parent_task_id": str(self.route.get("parent_id") or "")}
        with _no_store_wait(), queue._queue_lock:
            closed = queue.apply_budget_root_admission_fence(probe, root)
            fence = queue.ACCEPTANCE_FENCES.get(root)
            if not closed and isinstance(fence, dict) and str(fence.get("status") or "") in {"active", "sealed"}:
                probe.update(_admission_blocked="task_acceptance_fence",
                             _acceptance_fence_token=str(fence.get("token") or ""),
                             _acceptance_fence_status=str(fence.get("status") or "active"))
                closed = True
        if not closed:
            return None
        return scheduled_admission_rejection(probe, project_id=str(self.event.get("project_id") or ""),
                                             root_task_id=root)


def _transition(drive_root: Any, task_id: str, token: str, marker: Dict[str, Any], *,
                expected: Optional[tuple]) -> Optional[bool]:
    """Write ``marker`` only onto this token's still-requested row (``expected`` phases).

    ``expected=None`` is the first claim: the row may not carry any marker yet.
    True: the marker landed. False: the row left this token (progressed, or
    another owner). None: it did not land and the row could not prove otherwise.
    """
    def project(current: Dict[str, Any], incoming: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        held = current.get(MARKER)
        if current and str(current.get("status") or "") != STATUS_REQUESTED:
            return None
        if expected is None:
            if held is not None:
                return None
        elif not (isinstance(held, dict) and held.get("token") == token and held.get("phase") in expected):
            return None
        return {**incoming, MARKER: marker, "status": STATUS_REQUESTED}

    write_error = None
    try:
        write_task_result(drive_root, task_id, STATUS_REQUESTED, _field_projector=project,
                          strict_existing_dict=True)
    except Exception as exc:
        write_error = exc
        if not store_contention(exc):
            log.warning("Preparation marker write for %s raised; reading it back", task_id, exc_info=True)
    try:
        stored = load_task_result(drive_root, task_id, strict=True) or {}
    except Exception as exc:
        if store_contention(exc):
            raise
        log.warning("Preparation marker for %s is unreadable", task_id, exc_info=True)
        return None
    held = stored.get(MARKER) if isinstance(stored.get(MARKER), dict) else None
    if str(stored.get("status") or "") == STATUS_REQUESTED and held == marker:
        return True
    if stored and (str(stored.get("status") or "") != STATUS_REQUESTED
                   or held is not None and held.get("token") != token
                   or expected is None and held is not None):
        return False
    if store_contention(write_error):
        raise write_error
    return None


def _row_references(row: Dict[str, Any], resource: Dict[str, Any]) -> bool:
    """Whether another owner's row names the copy as its workspace (then it is theirs to keep)."""
    path = str(resource.get("path") or "")
    constraint = row.get("task_constraint") if isinstance(row.get("task_constraint"), dict) else {}
    copy_binding = row.get("workspace_copy") if isinstance(row.get("workspace_copy"), dict) else {}
    return bool(path) and path in {str(row.get("workspace_root") or ""), str(constraint.get("write_root") or ""),
                                   str(copy_binding.get("execution_root") or "")}


def _dispose_resource(task_id: str, resource: Dict[str, Any]) -> str:
    """Remove exactly the copy ``resource`` names; a different or unknown holder is kept."""
    from ouroboros import subagent_worktrees

    surface, path = str(resource.get("surface") or ""), str(resource.get("path") or "")
    try:
        if surface == "self_worktree" and path:
            row = next((entry for entry in subagent_worktrees.list_worktrees() if entry.get("path") == path), None)
            if (row is None or row.get("kind") or str(row.get("task_id") or "") != task_id
                    or row.get("created_at") != resource.get("created_at")
                    or str(row.get("branch") or "") != str(resource.get("branch") or "")):
                return "retained_not_owned"
            return "removed" if subagent_worktrees.remove_worktree(path=path) else "retained_remove_failed"
        if surface == "genesis" and path:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True,
                                  check=False) if pathlib.Path(path).is_dir() else None
            if head is None or head.returncode != 0 or head.stdout.strip() != str(resource.get("base_sha") or ""):
                return "retained_not_owned"
            return "removed" if subagent_worktrees.remove_genesis_project(path) else "retained_remove_failed"
    except Exception:
        log.warning("Prepared copy %s of %s was kept: disposal failed", path, task_id, exc_info=True)
        return "retained_remove_failed"
    return "retained_unidentified"


def _owner_live(marker: Dict[str, Any]) -> bool:
    """A recorded owner still able to act: a job registered here, or another live process."""
    with _STATE_LOCK:
        if str(marker.get("token") or "") in _LIVE:
            return True
    owner = marker.get("owner") if isinstance(marker.get("owner"), dict) else {}
    try:
        pid = int(owner.get("pid") or 0)
    except (TypeError, ValueError):
        return True
    if pid == os.getpid():
        return False  # this process holds the pid: no registered job here means the owner ended
    from ouroboros.local_custody_repair import owner_ended

    return not owner_ended(pid, owner.get("birth"))


def _interruption_reason(marker: Dict[str, Any]) -> str:
    owner = marker.get("owner") if isinstance(marker.get("owner"), dict) else {}
    me = _process_owner()
    if owner.get("pid") != me["pid"] or owner.get("birth") != me["birth"]:
        return INTERRUPTED_BY_RESTART
    current = _CURRENT[0]
    if current is not None and marker.get("generation") == current.token and not current.closed():
        return MARKER_UNWRITABLE  # this generation's job ended without recording its outcome
    return INTERRUPTED_BY_GENERATION_EXIT


def recover_interrupted_preparations(drive_root: Any, *, stop: Optional[Callable[[], bool]] = None) -> Dict[str, list]:
    """Settle preparations whose recorded owner positively ended (startup and periodic reconcile).

    Selects only REQUESTED rows carrying a marker (the status-indexed read). A
    ``queued``/``copying``/``ready`` marker becomes the typed never-admitted
    refusal through the schedule refusal owner, then only its recorded copy is
    disposed; the queue's own admission of the token retires the marker; an
    ``admitting`` marker stays unresolved on ``unknowns``. A live owner, a
    foreign reservation or unreadable authority is left alone.
    """
    from ouroboros import obligations
    from ouroboros.task_store import list_documents

    root = pathlib.Path(drive_root)
    report: Dict[str, list] = {"settled": [], "retained": [], "admitted": [], "uncertain": [], "live": [],
                               "errors": []}
    try:
        _retire_resolved_gaps(root, obligations)
        rows = list_documents(root, statuses=[STATUS_REQUESTED])
    except Exception as exc:
        log.warning("Preparation recovery could not read requested rows", exc_info=True)
        report["errors"].append(f"requested_rows_unreadable:{type(exc).__name__}")
        return report
    for row in rows:
        marker = row.get(MARKER)
        if not isinstance(marker, dict) or not str(marker.get("token") or ""):
            continue
        if stop is not None and stop():
            break
        tid = str(row.get("task_id") or "")
        try:
            report[_recover_one(root, row, marker, obligations)].append(tid)
        except Exception:
            log.warning("Preparation recovery of %s failed; it stays for the next pass", tid, exc_info=True)
            report["errors"].append(tid)
    return report


def _recover_one(root: pathlib.Path, row: Dict[str, Any], marker: Dict[str, Any], obligations: Any) -> str:
    from supervisor import queue

    tid, token = str(row.get("task_id") or ""), str(marker.get("token") or "")
    if _owner_live(marker):
        return "live"
    with queue._queue_lock:
        reserved = queue.ADMISSION_RESERVATIONS.get(tid)
        queued = next((item for item in queue.PENDING if isinstance(item, dict) and str(item.get("id") or "") == tid),
                      None)
        meta = queue.RUNNING.get(tid)
        if queued is None and isinstance(meta, dict):
            queued = meta.get("task") if isinstance(meta.get("task"), dict) else {"id": tid}
    if reserved and reserved != token:
        return "live"
    phase = str(marker.get("phase") or "")
    if queued is not None:
        if phase == ADMITTING and str(queued.get("_admission_owner_token") or "") == token:
            _transition(root, tid, token, {**marker, "phase": ADMITTED, "updated_at": utc_now_iso(),
                                           "retired_by": "recovery"}, expected=(ADMITTING,))
        return "admitted"  # the queue owns admitted work
    if phase in UNSENT_PHASES:
        reason = _interruption_reason(marker)
        confirmed = PreparationClaim.recovered(root, row, marker).refuse(_DETAILS[reason], reason)
        return "settled" if confirmed else "retained"
    obligations.add(root, "unknowns", f"task:{tid}", {
        "task_id": tid, "reason": ADMISSION_UNCERTAIN, "phase": phase, "preparation_token": token})
    return "uncertain"


def _retire_resolved_gaps(root: pathlib.Path, obligations: Any) -> None:
    """Drop this owner's ``unknowns`` rows once their row left that claim (a lifecycle owner moved it)."""
    for identity, facts in obligations.members(root, "unknowns").items():
        if facts.get("reason") != ADMISSION_UNCERTAIN:
            continue
        current = load_task_result(root, str(facts.get("task_id") or ""), strict=True) or {}
        held = current.get(MARKER) if isinstance(current.get(MARKER), dict) else {}
        if (str(current.get("status") or "") != STATUS_REQUESTED
                or held.get("token") != facts.get("preparation_token")):
            obligations.remove(root, "unknowns", identity)


def _reset_for_tests() -> None:
    """Forget this process's generation and live claims (test isolation only)."""
    with _STATE_LOCK:
        _CURRENT[0] = None
        _LIVE.clear()
