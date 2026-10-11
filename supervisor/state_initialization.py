"""Is this data root new? The one durable answer ``init_state`` consults (#1307).

Two missing ``state.json`` copies used to mean "fresh install", so a lost or
unreadable state minted a new owner slot, session and cleared Stop. First
initialization is now positive: ``state/state.initialized.json`` records ONE
initialization identity (``pending`` before the first state is written,
``complete`` after), written by supervisor boot (or an isolated benchmark's
explicit seed before boot) before admission, and by an owner Reset (``pending``,
``origin=owner_reset``). It holds identity and phase only — no permissions,
no history.

With both copies absent the decision is:

- a ``complete`` witness: initialized before, state LOST -> refuse (unavailable);
- a ``pending`` witness: an interrupted initialization -> finish the SAME id,
  unless supervisor-run evidence appeared since (then refuse);
- no witness: create only when a fixed, non-recursive set of evidence that a
  supervisor already ran here is positively absent (a legacy root whose state
  was lost is recovery, not first boot);
- an unreadable witness or evidence probe: refuse; unknown never mints identity.

Absence is proven, not inferred from an errno: Windows reports a file where
``state/`` belongs as ERROR_PATH_NOT_FOUND, the same ``FileNotFoundError`` as a
missing directory, so ``confirm_absent`` checks the entry and its ancestors.

Bootstrap scaffolding that may precede init — empty ``state``/``logs``/
``memory``/``task_results`` directories, ``settings.json`` from onboarding,
the usage ledger import, a benchmark sentinel — is deliberately not evidence.
A root whose every trace was deleted is indistinguishable from a new one: that
is a disclosed residual, not proof of historylessness.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import pathlib
import stat
import uuid
from typing import Any, Dict, Tuple

from ouroboros.utils import utc_now_iso, write_bytes_atomic

log = logging.getLogger(__name__)

WITNESS_REL = pathlib.Path("state") / "state.initialized.json"
# Each is written only by a running supervisor or its tasks, never by bootstrap.
_EVIDENCE_FILES = (
    pathlib.Path("state") / "queue_snapshot.json",        # every supervisor start, after init
    pathlib.Path("state") / "evolution_campaign.json",    # owner/agent evolution control
    pathlib.Path("state") / "project_task_bindings.json",  # task -> project binding
)
_EVIDENCE_NONEMPTY_FILES = (pathlib.Path("logs") / "chat.jsonl",)
_EVIDENCE_DIRS = (pathlib.Path("task_results"),)


def witness_path(drive_root: Any) -> pathlib.Path:
    return pathlib.Path(drive_root) / WITNESS_REL


def confirm_absent(path: Any) -> None:
    """After ``FileNotFoundError`` on ``path``, return only when it is really absent.

    POSIX types a non-directory where a directory belongs as ENOTDIR; Windows reports
    ERROR_PATH_NOT_FOUND, indistinguishable from a missing parent. A present entry
    after the failed operation is unknown. Only missing entries below a directory
    prove absence; a dangling link or an unexaminable ancestor cannot prove it."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError(errno.EEXIST, "path present after failed read", str(path))
    for ancestor in pathlib.Path(path).parents:
        try:
            mode = os.stat(ancestor).st_mode
        except FileNotFoundError:
            try:
                os.lstat(ancestor)  # stat alone also reports a dangling link as ENOENT
            except FileNotFoundError:
                continue
            raise
        if not stat.S_ISDIR(mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(ancestor))
        return
    raise FileNotFoundError(errno.ENOENT, "no directory ancestor confirmed", str(path))


def read_witness(drive_root: Any) -> Tuple[str, Dict[str, Any]]:
    """``("missing"|"ok"|"invalid"|"unreadable", witness)``; only proven absence is missing."""
    try:
        try:
            raw = witness_path(drive_root).read_bytes()
        except FileNotFoundError:
            confirm_absent(witness_path(drive_root))
            return "missing", {}
    except OSError:  # NotADirectoryError included: a file where ``state/`` belongs is not absence
        return "unreadable", {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "invalid", {}
    if (not isinstance(data, dict) or data.get("phase") not in {"pending", "complete"}
            or not str(data.get("initialization_id") or "")):
        return "invalid", {}
    return "ok", data


def _write(drive_root: Any, witness: Dict[str, Any]) -> None:
    path = witness_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(path, (json.dumps({"schema_version": 1, **witness}, ensure_ascii=False, indent=2)
                              + "\n").encode("utf-8"), fsync=True)


def mark_pending(drive_root: Any, *, origin: str) -> str:
    """Record an explicit fresh start (owner Reset) before the restart that runs init."""
    initialization_id = uuid.uuid4().hex
    _write(drive_root, {"initialization_id": initialization_id, "phase": "pending",
                        "origin": str(origin), "created_at": utc_now_iso()})
    return initialization_id


def supervisor_evidence(drive_root: Any) -> Tuple[str, str]:
    """``("none"|"present"|"unknown", path)``: did a supervisor already run here?"""
    root = pathlib.Path(drive_root)
    try:  # only proven absence is absence; a file where a directory belongs is unknown
        for rel in _EVIDENCE_FILES:
            try:
                os.lstat(root / rel)
                return "present", str(rel)
            except FileNotFoundError:
                confirm_absent(root / rel)
        for rel in _EVIDENCE_NONEMPTY_FILES:
            try:
                if os.lstat(root / rel).st_size > 0:
                    return "present", str(rel)
            except FileNotFoundError:
                confirm_absent(root / rel)
        for rel in _EVIDENCE_DIRS:
            try:
                with os.scandir(root / rel) as entries:
                    if next(entries, None) is not None:
                        return "present", str(rel)
            except FileNotFoundError:
                confirm_absent(root / rel)
    except OSError as exc:
        return "unknown", f"{type(exc).__name__} errno={exc.errno}"
    return "none", ""


def initialization_decision(drive_root: Any, *, origin: str = "first_boot") -> Dict[str, Any]:
    """Both state copies are positively absent: may a FIRST state be created?

    On yes, the witness is ``pending`` with the returned id before the caller
    writes the state; the caller completes it (``complete``) after."""
    status, witness = read_witness(drive_root)
    if status in {"invalid", "unreadable"}:
        return {"create": False, "reason": "initialization_witness_unreadable", "detail": status}
    if status == "ok" and witness.get("phase") == "complete":
        return {"create": False, "reason": "initialized_state_lost",
                "detail": f"initialization {witness.get('initialization_id')} completed at "
                          f"{witness.get('completed_at') or '?'}"}
    explicit_reset = status == "ok" and witness.get("origin") == "owner_reset"
    evidence, where = supervisor_evidence(drive_root)
    if evidence == "unknown" and not explicit_reset:
        return {"create": False, "reason": "initialization_evidence_unknown", "detail": where}
    if evidence == "present" and not explicit_reset:
        return {"create": False, "reason": "prior_history_without_state", "detail": where}
    if status == "ok":
        return {"create": True, "initialization_id": str(witness["initialization_id"])}
    initialization_id = uuid.uuid4().hex
    _write(drive_root, {"initialization_id": initialization_id, "phase": "pending",
                        "origin": str(origin or "first_boot"), "created_at": utc_now_iso()})
    return {"create": True, "initialization_id": initialization_id}


def authority_reason(drive_root: Any, initialization_id: str) -> str:
    """Read-only admission proof: a durable completion of this exact identity."""
    status, witness = read_witness(drive_root)
    if status != "ok":
        return f"initialization_witness_{status}"
    if not initialization_id or witness["initialization_id"] != initialization_id:
        return "initialization_identity_mismatch"
    return "" if witness["phase"] == "complete" else "initialization_incomplete"


def prepare_adoption(drive_root: Any, state: Dict[str, Any]) -> str:
    """Bind readable legacy data or resume a pending identity before writing it.

    Caller holds STATE_LOCK. Readable legacy session/creation facts, rather than
    absence of files, are the adoption evidence. An existing witness never changes
    identity here; only the explicit Reset owner may replace it.
    """
    status, witness = read_witness(drive_root)
    identity = str(state.get("initialization_id") or "")
    source = pathlib.Path(drive_root) / "state" / "state.json"
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if status == "ok":
        if (not identity and witness.get("phase") == "pending"
                and witness.get("origin") == "legacy_adopted"
                and witness.get("legacy_source_sha256") == source_digest):
            return str(witness["initialization_id"])
        if identity != witness["initialization_id"]:
            raise ValueError("initialization_identity_mismatch")
        return identity
    if status != "missing":
        raise ValueError(f"initialization_witness_{status}")
    if not identity and not (state.get("session_id") and state.get("created_at")):
        raise ValueError("legacy_initialization_evidence_missing")
    identity = identity or uuid.uuid4().hex
    _write(drive_root, {"initialization_id": identity, "phase": "pending",
                        "origin": "legacy_adopted", "created_at": utc_now_iso(),
                        "legacy_source_sha256": source_digest})
    return identity


def complete(drive_root: Any, initialization_id: str, *, adopted: bool) -> bool:
    """Complete only the exact pending identity after both state writes succeeded."""
    try:
        status, witness = read_witness(drive_root)
        if (status != "ok" or not initialization_id
                or witness.get("initialization_id") != initialization_id):
            return False
        if witness["phase"] != "complete":
            _write(drive_root, {**witness, "phase": "complete", "completed_at": utc_now_iso()})
        return not authority_reason(drive_root, initialization_id)
    except Exception:
        log.warning("state initialization witness could not be completed", exc_info=True)
        return False
