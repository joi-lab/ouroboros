"""Gateway admission, waiting, cancellation and terminal custody.

``_CustodyMixin`` keeps one gateway attempt registered from admission through
terminal observation and transfer to outer-write custody. Normal and cancelled
waits retain their distinct bounds; both feed the same terminal transfer.
The methods are mixed into ``CyberGymExecutor`` without another scheduler.
Everything here is gateway HTTP, checkpoint persistence and in-memory ownership;
it never operates on containers or child processes.
"""
from __future__ import annotations

import hashlib
import pathlib
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from typing import Any

from devtools.benchmarks.cybergym.cybergym_adapter import _TERMINAL_GATEWAY_STATUSES
from devtools.benchmarks.cybergym.cybergym_docker import _GATEWAY_TASK_ID, _write_json
from devtools.benchmarks.cybergym.cybergym_wire import (
    FINALIZATION_GRACE_SEC,
    GATEWAY_TRANSPORT_RETRY_BUDGET_SEC,
    ExecutorFailure,
    GatewayAdmissionRejected,
    GatewayTransportError,
    HttpStatusError,
    _cost_is_pending,
    _definitive_admission_rejection,
    _CostGraceTracker,
    _gateway_finalizing,
    _gateway_path,
    _response_status,
    _unwrap_http_json,
    _valid_cost_grace,
)


# Gateway statuses under which the task has been admitted but has not started
# executing: no worker lane, no provider spend, no wall clock the agent can
# pace against.  The launcher's task deadline starts when the task leaves this
# set (full1507 postmortem: a submit-anchored deadline cancelled healthy tasks
# after ~1 h of runtime because they had queued ~1 h behind a finalization
# backlog).  The isolate's own ``OUROBOROS_TASK_ABS_CEILING_SEC`` bounds the
# RUNNING phase from the same moment; ``TASK_DEADLINE_GRACE_SEC`` keeps the
# launcher's cancel a backstop behind that server-side settle, not a race
# against it.
_QUEUED_GATEWAY_STATUSES = frozenset({"", "scheduled", "queued", "pending"})
TASK_DEADLINE_GRACE_SEC = 300.0

class _CustodyMixin:
    """Own gateway attempt custody across normal and cancelled execution."""

    def _terminalize_gateway_attempt(self, gateway_task_id: str) -> None:
        """Atomically transfer a settled gateway attempt to outer-write custody."""
        with self._registry_condition:
            entry = self._gateway_attempts.get(gateway_task_id)
            if isinstance(entry, Mapping):
                workspace_name = str(entry.get("workspace_name") or "")
                if workspace_name:
                    self._terminal_uncommitted_workspaces[workspace_name] = {
                        "task_id": str(entry.get("task_id") or ""),
                        "attempt_id": str(entry.get("attempt_id") or ""),
                    }
            self._gateway_attempts.pop(gateway_task_id, None)

    def probe_gateway_alive(self) -> bool:
        """Liveness probe for the dispatch breaker: did the gateway answer?

        Any answer (even a non-2xx status) proves the transport is back; only
        a transport-level failure keeps the campaign paused.
        """

        try:
            self.config.http_runner(
                "GET",
                _gateway_path(self.config.ouroboros_url, "/api/health"),
                timeout=15,
            )
        except GatewayTransportError:
            return False
        except HttpStatusError:
            return True
        except Exception:  # noqa: BLE001 - malformed body still means "answered"
            return True
        return True

    def _gateway_wait(
        self,
        body: Mapping[str, Any],
        checkpoint: pathlib.Path,
        *,
        workspace_name: str = "",
        task_id: str = "",
        attempt_id: str = "",
    ) -> Mapping[str, Any]:
        requested_task_id = str(body.get("task_id") or "").strip()
        owner_task_id = str(task_id)
        owner_attempt_id = str(attempt_id)
        # The gateway currently echoes the opaque caller task id.  Register it
        # before POST so a dropped response can still be treated as an
        # admitted-or-unknown attempt and retained for manual reattachment.
        pending_id = requested_task_id or ("pending-" + uuid.uuid4().hex)
        idempotency_key = "cybergym-" + hashlib.sha256(
            (pending_id + "\0" + str(body.get("actor_id") or "cybergym")).encode()
        ).hexdigest()
        self._gateway_attempts[pending_id] = {
            "gateway_task_id": requested_task_id,
            "status": "admission_pending",
            "checkpoint": str(checkpoint),
            "idempotency_key": idempotency_key,
            "workspace_name": str(workspace_name),
            "task_id": owner_task_id,
            "attempt_id": owner_attempt_id,
        }
        try:
            created = _unwrap_http_json(
                self.config.http_runner(
                    "POST",
                    _gateway_path(self.config.ouroboros_url, "/api/tasks"),
                    body=body,
                    headers={"Idempotency-Key": idempotency_key},
                    timeout=60,
                ),
                operation="Ouroboros task admission",
            )
        except BaseException as exc:
            rejected = _definitive_admission_rejection(exc)
            status = "admission_rejected" if rejected else "admission_unknown"
            entry = self._gateway_attempts.get(pending_id)
            if entry is not None:
                entry.update({"status": status, "error": type(exc).__name__})
            if rejected:
                # A typed 4xx response is evidence that the gateway refused the
                # request before scheduling it.  Do not retain a phantom
                # custody claim, but keep the redacted checkpoint for audit.
                self._gateway_attempts.pop(pending_id, None)
            _write_json(
                checkpoint,
                {
                    "gateway_task_id": requested_task_id or pending_id,
                    "status": status,
                    "custody_required": not rejected,
                    "idempotency_key": idempotency_key,
                    "error": type(exc).__name__,
                },
            )
            if rejected:
                raise GatewayAdmissionRejected(str(exc)) from exc
            raise
        task_id = str(created.get("task_id") or "").strip()
        if not task_id or not _GATEWAY_TASK_ID.fullmatch(task_id):
            self._gateway_attempts[pending_id]["status"] = "admission_unknown_response"
            _write_json(
                checkpoint,
                {
                    "gateway_task_id": requested_task_id or pending_id,
                    "status": "admission_unknown_response",
                    "custody_required": True,
                    "idempotency_key": idempotency_key,
                },
            )
            raise ExecutorFailure("Ouroboros gateway returned no task id")
        if requested_task_id and task_id != requested_task_id:
            self._gateway_attempts[pending_id].update(
                {"gateway_task_id": task_id, "status": "admission_id_mismatch"}
            )
            _write_json(
                checkpoint,
                {
                    "gateway_task_id": task_id,
                    "submitted_task_id": requested_task_id,
                    "status": "admission_id_mismatch",
                    "custody_required": True,
                    "idempotency_key": idempotency_key,
                },
            )
            raise ExecutorFailure("Ouroboros gateway changed the submitted task id")
        if pending_id != task_id:
            self._gateway_attempts.pop(pending_id, None)
        self._gateway_attempts[task_id] = {
            "gateway_task_id": task_id,
            "status": "submitted",
            "checkpoint": str(checkpoint),
            "idempotency_key": idempotency_key,
            "workspace_name": str(workspace_name),
            "task_id": owner_task_id,
            "attempt_id": owner_attempt_id,
        }
        _write_json(
            checkpoint,
            {
                "gateway_task_id": task_id,
                "status": "submitted",
                "idempotency_key": idempotency_key,
                "body": {k: v for k, v in body.items() if k != "description"},
            },
        )
        # Two bounds, one active at a time: the queue-wait cap while the
        # gateway still reports the task as not started, then the task
        # deadline anchored at the first observed non-queued status.
        queue_started = time.monotonic()
        queue_wait_cap = queue_started + float(self.config.task_timeout_sec)
        run_deadline: float | None = None
        observed_start_at: str | None = None
        latest: Mapping[str, Any] = created
        cost_grace = _CostGraceTracker()
        transport_deadline: float | None = None
        finalization_grace_until: float | None = None
        while True:
            bound = run_deadline if run_deadline is not None else queue_wait_cap
            if time.monotonic() >= bound:
                # The worker is done and the server is finalizing artifacts:
                # a finished, paid result is minutes away — wait for it (once,
                # bounded) instead of cancelling it.
                if finalization_grace_until is None and _gateway_finalizing(latest):
                    finalization_grace_until = time.monotonic() + FINALIZATION_GRACE_SEC
                    run_deadline = finalization_grace_until
                    _write_json(checkpoint, {
                        "gateway_task_id": task_id,
                        "status": _response_status(latest),
                        "result": dict(latest),
                        "deadline_basis": "finalization_grace",
                        "finalization_grace_sec": FINALIZATION_GRACE_SEC,
                    })
                    continue
                break
            try:
                latest = _unwrap_http_json(
                    self.config.http_runner(
                        "GET",
                        _gateway_path(self.config.ouroboros_url, "/api/tasks/" + urllib.parse.quote(task_id, safe="")),
                        timeout=60,
                    ),
                    operation="Ouroboros task status",
                )
            except GatewayTransportError:
                # A transient transport failure (an isolate event-loop stall
                # starves the HTTP answer) must not kill a healthy paid task
                # on the first error: ride it out within a bounded budget.
                # Exhaustion re-raises so a dead gateway still produces the
                # circuit-breaker row.
                now = time.monotonic()
                if now >= bound:
                    # The task's own deadline passed while the gateway was
                    # unreachable: stop polling and cancel it like a normal
                    # deadline exit instead of writing a transport row.
                    break
                if transport_deadline is None:
                    transport_deadline = min(
                        bound, now + GATEWAY_TRANSPORT_RETRY_BUDGET_SEC
                    )
                if now >= transport_deadline:
                    raise
                self.config.sleep(max(0.5, float(self.config.poll_interval_sec)))
                continue
            transport_deadline = None
            returned_id = str(latest.get("task_id") or "").strip()
            if returned_id and returned_id != task_id:
                raise ExecutorFailure("Ouroboros status response belongs to a different task")
            status = _response_status(latest)
            if run_deadline is None and status not in _QUEUED_GATEWAY_STATUSES:
                run_deadline = (
                    time.monotonic()
                    + float(self.config.task_timeout_sec)
                    + TASK_DEADLINE_GRACE_SEC
                )
                observed_start_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
            frame = {
                "gateway_task_id": task_id,
                "status": status,
                "result": dict(latest),
                "deadline_basis": (
                    "finalization_grace" if finalization_grace_until is not None
                    else "observed_start" if run_deadline is not None else "queue_wait_cap"
                ),
            }
            if observed_start_at is not None:
                frame["observed_start_at"] = observed_start_at
            _write_json(checkpoint, frame)
            if status in _TERMINAL_GATEWAY_STATUSES:
                # Root post-task accounting can publish ``completed`` before
                # its durable cost roll-up is final; only the bounded
                # abandoned-residue grace (cybergym_wire) releases such a
                # frame early, with the residue disclosed on it.
                if status == "completed" and _cost_is_pending(latest):
                    accepted = cost_grace.accept(
                        latest,
                        now=time.monotonic(),
                        wall_now=time.time(),
                    )
                    if accepted is None:
                        self.config.sleep(max(0.5, float(self.config.poll_interval_sec)))
                        continue
                    latest = accepted
                    _write_json(checkpoint, {"gateway_task_id": task_id, "status": status, "result": dict(latest)})
                self._terminalize_gateway_attempt(task_id)
                return latest
            self.config.sleep(max(0.5, float(self.config.poll_interval_sec)))
        # The task may still be running after the local wait expires.  Ask the
        # gateway to stop it and retain the original attempt until a terminal
        # custody response is observed; never return a reusable task id here.
        return self._cancel_gateway_task(task_id, checkpoint)

    def _poll_gateway_custody(
        self,
        task_id: str,
        checkpoint: pathlib.Path,
        *,
        cancel_response: Mapping[str, Any] | None,
        cancel_status_code: int | None = None,
        custody_seconds: float,
    ) -> Mapping[str, Any]:
        """Poll an already admitted task until a terminal custody frame.

        This helper is shared by the normal cancellation response and the
        gateway's 503/404 cancellation races.  A ``completed`` frame with
        pending cost accounting is not terminal for this adapter unless the
        bounded abandoned-residue grace releases it: the outer campaign
        ledger must receive a final/upper-bound frame, never an intermediate
        cost snapshot.
        """

        deadline = time.monotonic() + custody_seconds
        # A frame that shows the worker finished and artifacts finalizing is a
        # paid result in flight, not a stuck cancellation: custody stays open
        # for it up to the finalization grace (once).
        finalization_deadline = time.monotonic() + max(custody_seconds, FINALIZATION_GRACE_SEC)
        finalization_extended = False
        transport_deadline: float | None = None
        cost_grace = _CostGraceTracker()
        cancel_frame = dict(cancel_response) if isinstance(cancel_response, Mapping) else None
        latest: Mapping[str, Any] = cancel_response or {}
        status_url = _gateway_path(
            self.config.ouroboros_url,
            "/api/tasks/" + urllib.parse.quote(task_id, safe=""),
        )
        while time.monotonic() < deadline:
            try:
                latest = _unwrap_http_json(
                    self.config.http_runner(
                        "GET", status_url, timeout=30
                    ),
                    operation="Ouroboros cancellation custody status",
                )
                returned_id = str(latest.get("task_id") or "").strip()
                if returned_id and returned_id != task_id:
                    raise ExecutorFailure("cancellation status belongs to a different task")
                status = _response_status(latest)
                if status == "completed" and _cost_is_pending(latest):
                    latest = (
                        cost_grace.accept(latest, now=time.monotonic(), wall_now=time.time())
                        or latest
                    )
                frame: dict[str, Any] = {
                    "gateway_task_id": task_id,
                    "status": status or "cancel_pending",
                    "result": dict(latest),
                }
                if cancel_status_code is not None:
                    frame["cancel_status_code"] = cancel_status_code
                if cancel_frame is not None:
                    frame["cancel_response"] = cancel_frame
                _write_json(checkpoint, frame)
                if status in _TERMINAL_GATEWAY_STATUSES and not (
                    status == "completed"
                    and _cost_is_pending(latest)
                    and _valid_cost_grace(latest) is None
                ):
                    self._terminalize_gateway_attempt(task_id)
                    return latest
                if not finalization_extended and _gateway_finalizing(latest):
                    finalization_extended = True
                    deadline = max(deadline, finalization_deadline)
                    _write_json(checkpoint, {**frame, "custody_basis": "finalization_grace"})
                transport_deadline = None
            except (GatewayTransportError, HttpStatusError) as exc:
                if isinstance(exc, HttpStatusError) and exc.status_code != 503:
                    raise
                # Cancellation waves can starve both the cancel POST and the
                # follow-up GET on the same event loop.  Keep custody through
                # that transient outage, bounded by both the custody window
                # and the shared transport retry budget.
                now = time.monotonic()
                if transport_deadline is None:
                    transport_deadline = min(
                        deadline, now + GATEWAY_TRANSPORT_RETRY_BUDGET_SEC
                    )
                frame = {
                    "gateway_task_id": task_id,
                    "status": "cancel_poll_error",
                    "cancel_error": type(exc).__name__,
                }
                if isinstance(exc, HttpStatusError):
                    frame["cancel_poll_status_code"] = exc.status_code
                if cancel_status_code is not None:
                    frame["cancel_status_code"] = cancel_status_code
                if cancel_frame is not None:
                    frame["cancel_response"] = cancel_frame
                _write_json(checkpoint, frame)
                if now >= transport_deadline:
                    raise
            except ExecutorFailure:
                # HTTP/auth/transport failures remain typed failures and keep
                # the attempt registered for manual custody recovery.
                raise
            except Exception as exc:
                frame = {
                    "gateway_task_id": task_id,
                    "status": "cancel_poll_error",
                    "cancel_error": type(exc).__name__,
                }
                if cancel_status_code is not None:
                    frame["cancel_status_code"] = cancel_status_code
                if cancel_frame is not None:
                    frame["cancel_response"] = cancel_frame
                _write_json(checkpoint, frame)
            self.config.sleep(max(0.5, float(self.config.poll_interval_sec)))
        raise ExecutorFailure("Ouroboros task cancellation custody did not settle")

    def _cancel_gateway_task(
        self, task_id: str, checkpoint: pathlib.Path
    ) -> Mapping[str, Any]:
        """Request cancellation and retain custody until a terminal status.

        A caller-side polling deadline is not proof that the worker stopped.
        The cancel response and the subsequent short custody poll are written
        to the same checkpoint, so an operator can later inspect/reattach
        without making a duplicate paid attempt.  A 503 (durable cancel
        intent, asynchronous teardown) or a 404 (the task already left the
        active set — a long-terminal task still answers GET from its durable
        result) allows a GET-only recovery of the existing terminal task
        result.  Other HTTP statuses and transport failures are not converted
        into apparent task results.
        """
        cancel_url = _gateway_path(
            self.config.ouroboros_url,
            "/api/tasks/" + urllib.parse.quote(task_id, safe="") + "/cancel",
        )
        # The post-cancel custody poll must outlast a deadline-wave settle:
        # when a full 64-lane wave hits its 2 h deadline together, the isolate
        # takes minutes to settle each cancellation, and the previous ~34 s
        # bound wrote off 18 paid tasks in one wave as "custody did not
        # settle" even though every cancel had already landed.
        custody_seconds = min(
            600.0, max(300.0, float(self.config.poll_interval_sec) * 8.0 + 10.0)
        )
        cancel_response: Mapping[str, Any] | None = None
        transport_deadline: float | None = None
        while cancel_response is None:
            try:
                cancel_response = _unwrap_http_json(
                    self.config.http_runner(
                        "POST", cancel_url, body={}, timeout=30
                    ),
                    operation="Ouroboros task cancellation",
                    accepted_statuses=(200, 202, 204),
                )
            except HttpStatusError as exc:
                _write_json(
                    checkpoint,
                    {
                        "gateway_task_id": task_id,
                        "status": "cancel_request_failed",
                        "cancel_error": type(exc).__name__,
                        "cancel_status_code": exc.status_code,
                    },
                )
                if exc.status_code in (503, 404):
                    # Only a later terminal GET can turn a cancellation race into
                    # an adapter outcome; absent that frame we retain the
                    # original custody block.
                    return self._poll_gateway_custody(
                        task_id,
                        checkpoint,
                        cancel_response=None,
                        cancel_status_code=exc.status_code,
                        custody_seconds=custody_seconds,
                    )
                raise ExecutorFailure("Ouroboros task cancellation request failed") from exc
            except GatewayTransportError as exc:
                # Transient: the cancel intent usually lands server-side and
                # only the response is starved (an isolate event-loop stall),
                # and a duplicate cancel POST is idempotent.  Ride out the
                # stall within a bounded budget before writing the attempt
                # off; exhaustion keeps the original fail-closed behaviour.
                now = time.monotonic()
                if transport_deadline is None:
                    transport_deadline = now + GATEWAY_TRANSPORT_RETRY_BUDGET_SEC
                if now >= transport_deadline:
                    _write_json(
                        checkpoint,
                        {
                            "gateway_task_id": task_id,
                            "status": "cancel_request_failed",
                            "cancel_error": type(exc).__name__,
                        },
                    )
                    raise
                self.config.sleep(max(0.5, float(self.config.poll_interval_sec)))
            except Exception as exc:
                _write_json(
                    checkpoint,
                    {
                        "gateway_task_id": task_id,
                        "status": "cancel_request_failed",
                        "cancel_error": type(exc).__name__,
                    },
                )
                raise ExecutorFailure("Ouroboros task cancellation request failed") from exc
        _write_json(
            checkpoint,
            {
                "gateway_task_id": task_id,
                "status": _response_status(cancel_response) or "cancel_requested",
                "cancel_response": dict(cancel_response),
            },
        )
        return self._poll_gateway_custody(
            task_id,
            checkpoint,
            cancel_response=cancel_response,
            custody_seconds=custody_seconds,
        )
