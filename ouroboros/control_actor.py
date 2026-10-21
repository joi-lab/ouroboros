"""Call-scoped attribution for the existing Pause, Resume and Continue owners.

No authority is stored here. The supervisor binds the requesting mind for one
control call; the existing fence/grant/CAS records retain who requested it.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_ACTOR = ContextVar("lifecycle_control_actor", default=None)


def control_actor() -> dict:
    return dict(_ACTOR.get() or {})


PAUSE_AUTHORS = ("owner", "ouroboros")


def pause_author(fence: dict | None) -> dict:
    """Who declared a Pause and why, as its fence recorded it: ``{actor, text, at}``.

    ``actor`` is ``owner`` when the owner's client declared the press, ``ouroboros`` for a mind's ``pause_task``
    (``text`` is its own reason), else ``""``: a press that declared nothing, or a missing or unreadable fence.
    Attribution is never inferred from the door a press came through. ``at`` is the press time, never the
    ``paused_at`` a settling fence restamps. Fences written before ``by`` existed keep their mind record: the
    top-level ``actor``, else the mind's first press on that same fence.
    """
    fence = fence if isinstance(fence, dict) else {}
    at = str(fence.get("requested_at") or "")
    by = fence.get("by") if isinstance(fence.get("by"), dict) else None
    if by is not None and str(by.get("actor") or "") in PAUSE_AUTHORS:
        return {"actor": str(by["actor"]), "text": str(by.get("text") or "").strip(), "at": str(by.get("at") or at)}
    legacy = [fence] + [entry for entry in (fence.get("requests") or {}).values()
                        if isinstance(entry, dict) and entry.get("fence_id") == fence.get("fence_id")]
    for entry in legacy:
        if entry.get("actor") == "mind":
            return {"actor": "ouroboros", "text": str(entry.get("reason") or "").strip(), "at": at}
    return {"actor": "", "text": "", "at": at}


@contextmanager
def mind_control(actor_task_id: str, reason: str):
    token = _ACTOR.set({"actor": "mind", "actor_task_id": actor_task_id, "reason": reason})
    try:
        yield
    finally:
        _ACTOR.reset(token)


def mind_resume_refusal(root, task=None) -> dict | None:
    """A model may not release a Panic hold, even after the global flag cleared."""
    from pathlib import Path
    from ouroboros.budget_pause import budget_hold_fact

    if not control_actor():
        return None
    hold = budget_hold_fact(task or {}) or {}
    if hold.get("reason") == "panic_hold" or (Path(root) / "state" / "panic_stop.flag").exists():
        return {"ok": False, "error": "panic_requires_owner_resume"}
    return None
