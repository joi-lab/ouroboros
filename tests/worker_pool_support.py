"""Explicit fake process readiness for assignment consumers (no product fallbacks)."""

from types import SimpleNamespace


def affirm_ready(worker):
    if not hasattr(worker, "proc"):
        # Above every platform's PID ceiling: a stray tree kill can never reach a real process.
        worker.proc = SimpleNamespace(pid=2147483200 + worker.wid, is_alive=lambda: True)
    worker.process_birth = "test-birth"
    worker.ready_identity = (worker.proc.pid, worker.process_birth)
    worker.reaping = getattr(worker, "reaping", False)
    worker.active_capacity = getattr(worker, "active_capacity", True)
    worker.readiness_exhausted = getattr(worker, "readiness_exhausted", False)
    worker.booting = False
    worker.retiring = False
    worker.quiescence_token = ""
    worker.quiescence_confirmed = False
    worker.resume_hold = ""
    return worker
