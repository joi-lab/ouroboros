"""Caller-owned pre-send accounting waits; no provider retry or task policy below.

Only positive lock contention can keep the same stack alive. Controls come
from the existing model-operation owner. Cleanup and post-response accounting
retain bounded acquisitions. The async bridge joins its mutator on cancellation
so a returned reservation cannot race a successor or lose its local owner.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import pathlib
import threading
import time
import uuid

from ouroboros.usage_ledger import UsageLockUnavailable, USAGE_LOCK_TIMEOUT_SEC

log = logging.getLogger(__name__)
_CANCEL = contextvars.ContextVar("usage_presend_cancel", default=None)
_CLEANUP = contextvars.ContextVar("usage_cleanup", default=False)
# Acquisition slicing controls responsiveness, not lifetime or quota policy.
ACQUISITION_SLICE_SEC = 0.25


def _check() -> None:
    cancel = _CANCEL.get()
    if cancel is not None and cancel.is_set():
        raise asyncio.CancelledError()
    from ouroboros.llm_attempt import require_physical_dispatch_window

    require_physical_dispatch_window()


def _hold(owner, phase: str, started: float, episode_id: str) -> None:
    if owner is None:
        return
    try:
        from ouroboros.loop_messages import _emit_checkpoint_event

        detail = "Waiting for accounting access" if phase == "entered" else "Accounting wait ended"
        _emit_checkpoint_event(owner.event_queue, owner.task_id, pathlib.Path(owner.drive_root) / "logs", {
            "checkpoint_kind": "usage_lock_wait", "owner_visible": True,
            "phase": phase, "episode_id": episode_id, "elapsed_sec": time.monotonic() - started,
            "detail": detail, "content": detail, "text": detail, "role": "system", "system_type": "task_checkpoint",
            **({"chat_id": owner.task["chat_id"]} if "chat_id" in owner.task else {}),
        })
    except Exception:
        log.debug("Could not disclose accounting wait", exc_info=True)


def send_acquisition(check=None):
    """Build one acquisition episode; critical-section exceptions never retry."""
    from ouroboros.model_wait import current_model_wait, dispatch_deadline_remaining_sec
    from ouroboros.config import get_task_idle_timeout_sec

    owner = current_model_wait()
    owned = owner is not None or dispatch_deadline_remaining_sec() is not None
    sliced = owned or _CANCEL.get() is not None
    started = time.monotonic()
    interactive_bound = (float(get_task_idle_timeout_sec()) if owner is not None
                         and owner.task.get("_is_direct_chat") else None)

    @contextlib.contextmanager
    def acquire(root):
        from ouroboros import usage_accounting as ua
        if not sliced or _CLEANUP.get():
            with ua._locked(root) as heartbeat:
                yield heartbeat
            return
        entered = False
        episode_id = uuid.uuid4().hex
        try:
            while True:
                _check()
                if check:
                    check()
                if interactive_bound is not None and time.monotonic() - started >= interactive_bound:
                    from ouroboros.llm_attempt import PhysicalDispatchInterrupted

                    raise PhysicalDispatchInterrupted("accounting_wait_expired")
                stack = contextlib.ExitStack()
                try:
                    heartbeat = stack.enter_context(ua._locked(root, timeout_sec=ACQUISITION_SLICE_SEC))
                except UsageLockUnavailable as exc:
                    if exc.reason != "contention" or (not owned and time.monotonic() - started >= USAGE_LOCK_TIMEOUT_SEC):
                        raise
                    if not entered:
                        entered = True
                        _hold(owner, "entered", started, episode_id)
                    continue
                with stack:
                    _check()
                    # The reserve/dispatch body checks its authoritative scope's
                    # fence once, after preparation and under this same hold.
                    yield heartbeat
                    return
        finally:
            if entered:
                _hold(owner, "ended", started, episode_id)
    return acquire


@contextlib.contextmanager
def bounded_cleanup():
    token = _CLEANUP.set(True)
    try:
        yield
    finally:
        _CLEANUP.reset(token)


def transition_acquisition(state, check=None):
    return cleanup_acquisition if _CLEANUP.get() else send_acquisition(check) if state == "dispatched" else None


def cleanup_acquisition(root):
    from ouroboros import usage_accounting as ua

    return ua._locked(root, timeout_sec=ACQUISITION_SLICE_SEC)


async def presend_off_loop(function, *args, on_cancel=None, **kwargs):
    """Copy ContextVars; cancel cooperatively and JOIN before unwinding custody."""
    from ouroboros import usage_accounting as ua

    def invoke():
        ua.adopt_physical_attempt_capture(None)
        result = function(*args, **kwargs)
        return result, ua.last_physical_attempt_capture()

    cancelled = threading.Event()
    token = _CANCEL.set(cancelled)
    try:
        future = asyncio.create_task(asyncio.to_thread(invoke))
    finally:
        _CANCEL.reset(token)
    try:
        result, capture = await asyncio.shield(future)
        if capture is not None:
            ua.adopt_physical_attempt_capture(capture)
        return result
    except asyncio.CancelledError:
        cancelled.set()
        # A second cancellation cannot abandon a mutator already holding money.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if not future.cancelled() and future.exception() is None and on_cancel:
            # A reservation may have committed just as cancellation arrived.
            # Cleanup itself is bounded and joined under the same rule.
            cleanup = asyncio.create_task(asyncio.to_thread(on_cancel, future.result()[0]))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            failure = cleanup.result()
            capture = getattr(failure, "physical_attempt_capture", None)
            if capture is not None:
                ua.adopt_physical_attempt_capture(capture)
        raise


async def postresponse_off_loop(function, *args, retain_on_cancel):
    """Join accounting, retain the received answer, then propagate cancellation.

    The response already exists: cancellation cannot cancel accounting or
    skip its unresolved fallback. It still forbids caller continuation. The
    existing private response store owns retention, not the cancelled stack.
    """
    from ouroboros import usage_accounting as ua

    def invoke():
        result = function(*args)
        return result, ua.last_physical_attempt_capture()

    future = asyncio.create_task(asyncio.to_thread(invoke))
    cancelled = None
    while True:
        try:
            response, capture = await asyncio.shield(future)
            break
        except asyncio.CancelledError as exc:
            if future.cancelled():
                raise  # Not a caller cancellation; no outcome to invent.
            cancelled = exc
    ua.adopt_physical_attempt_capture(capture)
    if cancelled is None:
        return response
    cancelled.physical_attempt_capture = capture
    cancelled.response = response
    retention = asyncio.create_task(asyncio.to_thread(retain_on_cancel, response, capture))
    while not retention.done():
        try:
            await asyncio.shield(retention)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    try:
        cancelled.response_manifest_ref = retention.result()["manifest_ref"]
    except Exception as exc:
        # Keep the exact object on the exception too; do not turn a failed
        # retention into success or authorize execution after Stop.
        cancelled.response_retention_error = type(exc).__name__
        log.exception("Failed to retain cancelled paid response")
    raise cancelled


def model_send(reservation, send):
    """One physical sender owned until it returns; no wait under Pause's lock."""
    from concurrent.futures import ThreadPoolExecutor
    from ouroboros.owner_pause import submit_model

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="model-send") as executor:
        future = submit_model(reservation, executor.submit, send)
        return future.result()


async def model_send_async(reservation, complete, *, retain_on_cancel):
    """Join cancellation/settlement before the exact consumer may retire."""
    from ouroboros import usage_accounting as ua
    from ouroboros.owner_pause import submit_model

    outcome = {}
    async def invoke():
        outcome["entered"] = True
        try:
            return await complete()
        except BaseException as exc:
            outcome["error"] = exc
            return None  # Keep exception identity across an asyncio Task boundary.
        finally:
            outcome["capture"] = ua.last_physical_attempt_capture()

    try:
        future = submit_model(reservation, lambda run, fn: run(asyncio.create_task, fn()), invoke)
    except BaseException as exc:
        exc.model_sender_not_started = True
        raise
    try:
        response = await asyncio.shield(future)
        if "error" in outcome:
            raise outcome["error"]
        return response
    except asyncio.CancelledError as cancelled:
        future.cancel()
        while not future.done():
            try:
                await asyncio.shield(future)
            except BaseException:
                if future.done():
                    break
        if not outcome.get("entered"):
            cancelled.model_sender_cancelled_before_entry = True
            raise cancelled
        try:
            response = future.result()
            if "error" in outcome:
                raise outcome["error"]
        except BaseException:
            # Task boundaries may synthesize a fresh CancelledError. Keep the
            # caller's cancellation, carrying the exact joined paid outcome.
            error = outcome["error"]
            for field in ("response", "response_manifest_ref", "response_retention_error", "physical_attempt_capture"):
                if hasattr(error, field):
                    setattr(cancelled, field, getattr(error, field))
            raise cancelled from error
        capture = outcome.get("capture")
        # Completion won the cancellation race; its value is still a paid answer.
        cancelled.response, cancelled.physical_attempt_capture = response, capture
        retention = asyncio.create_task(asyncio.to_thread(retain_on_cancel, response, capture))
        while not retention.done():
            try:
                await asyncio.shield(retention)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            cancelled.response_manifest_ref = retention.result()["manifest_ref"]
        except Exception as exc:
            cancelled.response_retention_error = type(exc).__name__
            log.exception("Failed to retain cancelled paid response")
        raise cancelled
    finally:
        capture = outcome.get("capture")
        if capture is not None:
            ua.adopt_physical_attempt_capture(capture)
