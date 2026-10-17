"""Call-scoped attribution for the existing Pause, Resume and Continue owners.

No authority is stored here. The supervisor binds the requesting mind for one
control call; the existing fence/grant/CAS records retain who requested it.
"""

from contextlib import contextmanager
from contextvars import ContextVar

_ACTOR = ContextVar("lifecycle_control_actor", default=None)


def control_actor() -> dict:
    return dict(_ACTOR.get() or {})


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
