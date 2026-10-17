"""Find the wake itself after its direct actor handed the same id to the pool."""


class WakeCensusBusy(RuntimeError):
    """A caller holding the direct gate must retry rather than invert Q -> gate."""


def queued_wakes(*, blocking=True) -> list[str]:
    from supervisor import queue as q

    if not q._queue_lock.acquire(blocking=blocking):
        raise WakeCensusBusy("queue census busy")
    try:
        tasks = [*q.PENDING, *(meta.get("task") or {} for meta in q.RUNNING.values())]
        return list(dict.fromkeys(str(task["id"]) for task in tasks if task.get("id")
                                 and (task.get("metadata") or {}).get("usage_category") == "consciousness"))
    finally:
        q._queue_lock.release()


def stop_pooled_wake(task_id: str) -> None:
    from ouroboros.cancel_intents import request_cancel, STOP_POLICY_FINALIZE
    from supervisor import queue as q

    # The ordinary cancellation sweep owns the actual stop, including a row
    # still queued and the transition from inline park to a pooled attempt.
    request_cancel(q.DRIVE_ROOT, task_id, reason="background_consciousness_stopped",
                   source="owner", requested_by="owner", requested_stop_policy=STOP_POLICY_FINALIZE)


def finish_pooled_wake(ctx, task_id: str, result: dict) -> None:
    """The detached attempt's end starts the ordinary next-wake interval."""
    from ouroboros.task_carrier import execution_kind

    clock = getattr(ctx, "consciousness", None)
    if (clock is not None and execution_kind(result) == "pooled"
            and (result.get("metadata") or {}).get("usage_category") == "consciousness"):
        clock._wake_finished(task_id, result.get("status") == "completed")
