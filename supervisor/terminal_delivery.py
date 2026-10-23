"""Terminal delivery for completion, cancellation and reap, without model rounds.

The bounded terminal_deliveries.json outbox owns exact-id debt across restart.
Send precedes its receipt: a crash in between remains at-least-once externally.
Completed answers remain review-aware through task_finalization; cancellation and
non-retry reap use lineage-routed unreviewed salvage (banner, bounded preview with
omitted count, full-copy path/size/hash, optional subtree digest). Retryable reap
sends nothing; callers retain outcome routing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
from typing import Any, Dict, List, Optional

from ouroboros.observability import without_finalization_timing
from ouroboros.store import bounded_wait
from ouroboros.utils import update_json_locked, utc_now_iso
from ouroboros.task_store import patch_document
from ouroboros.task_finalization import (
    HOST_AUTHORED_TERMINAL_ORIGINS, TERMINAL_ORIGIN_HOST_SALVAGE, TERMINAL_ORIGIN_MODEL_FINAL,
    artifact_store_roots, rescued_files_fact, rescued_files_sentence,
)

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 3  # v3: ``exhausted`` — a delivery the host gave up on, kept with its payload
_REGISTRY_CAP = 512
# Bounded chat preview of the salvaged answer; the full copy is referenced by a
# durable receipt, and the omitted tail is DISCLOSED (BIBLE P1), never silent.
SALVAGE_PREVIEW_CHARS = 3500
# Durable pending OUTBOX bound. A terminal answer is registered here BEFORE it is
# enqueued and removed once the send succeeded, so a crash in that window replays
# instead of losing the owner's answer forever (the incident class). Bounded the
# same way the delivered registry is: newest kept.
_PENDING_CAP = 64
# A row younger than this is presumed still in flight through the event queue,
# not lost; and each row is replayed at most this many times before the outbox
# gives up loudly (an unreachable chat must not become a tick-rate retry storm).
_REPLAY_MIN_AGE_SEC = 60.0
_PENDING_MAX_REPLAYS = 5

# GR3-7: marker prefix for "the teardown audit itself failed" rows riding the
# delegated_runs_unreconciled surface — run state is UNKNOWN, never clean.
RUN_STATE_UNKNOWN_PREFIX = "delegated_run_state_unknown"

_HOST_SALVAGE_RECEIPT = (
    "⚠️ A model-provider outage stopped this task before Ouroboros produced "
    "a complete answer. The full intermediate output and technical details are preserved in the "
    "task details."
)


def cleanup_settled_owner_mailbox(
    drive_root: Any, task_id: str, task: Optional[Dict[str, Any]] = None, *, carry_inputs: bool = True, stop: Any = None,
) -> None:
    """Release the execution mailbox once its canonical obligations settle and the canonical row holds its unread rows with their input closure; ``carry_inputs=False`` (the loop thread) leaves inputs to an off-loop owner, whose generation ``stop()`` fences the cleanup."""
    from ouroboros.owner_mailbox import cleanup_task_mailbox, settled_mailbox_cleanup_allowed
    from ouroboros.task_results import load_task_result
    from supervisor.queue import _task_drive_for_task

    durable = load_task_result(pathlib.Path(drive_root), str(task_id)) or {}
    if settled_mailbox_cleanup_allowed(durable):
        cleanup_task_mailbox(_task_drive_for_task(task or durable, str(task_id)), str(task_id), canonical_root=drive_root, carry_inputs=carry_inputs, stop=stop)


def _registry_path(drive_root: Any) -> pathlib.Path:
    root = pathlib.Path(drive_root) / "state"
    root.mkdir(parents=True, exist_ok=True)
    return root / "terminal_deliveries.json"


# A terminal ANSWER's identity; custody-notice and other suffixed rows are not answers.
# Exact answer-byte evidence (receipt capture, acceptance) stays this narrow.
_TERMINAL_ANSWER_ID = re.compile(r"final:(?P<task>[^:]+):[0-9a-f]{16}")
# The three families a task's terminal MESSAGE takes: the model final, the Stop/reap
# salvage receipt (``cancel:<tid>:<request_id>``, CF-04) and a cascade root's digest
# (``cascade:<root>:<request_id>``). Exactly three parts, so a ``:custody_notice`` /
# ``:terminal_incident`` suffix, the Main mirror (``project-completion:``) and every
# other row are never the answer. The Done gate reads debt through THIS family set.
TERMINAL_MESSAGE_ID = re.compile(r"(?:final|cancel|cascade):(?P<task>[^:]+):(?P<episode>[^:]+)")


def _mapping_rows(current: Dict[str, Any], key: str, *, strict: bool = False) -> Dict[str, Any]:
    """Missing/None mappings mean empty; strict mutations preserve malformed forensic bytes."""
    value = current.get(key)
    if isinstance(value, dict):
        return dict(value)
    if strict and value is not None:
        raise ValueError(f"terminal-delivery registry '{key}' is malformed (not an object)")
    return {}


def _registry_document(current: Dict[str, Any], rows: List[str], pending: Dict[str, Any],
                       receipts: Optional[Dict[str, Any]] = None,
                       exhausted: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Keep untouched receipts/exhausted payloads (replay/capacity limit). Their Done-card disposition
    retires only on a real receipt of that id."""
    receipts = _mapping_rows(current, "receipts", strict=True) if receipts is None else receipts
    exhausted = _mapping_rows(current, "exhausted", strict=True) if exhausted is None else exhausted
    document = {"schema_version": _SCHEMA_VERSION, "delivered": rows[-_REGISTRY_CAP:], "pending": pending}
    if receipts:
        document["receipts"] = dict(list(receipts.items())[-_REGISTRY_CAP:])
    if exhausted:
        document["exhausted"] = dict(list(exhausted.items())[-_REGISTRY_CAP:])
    return document


def _capture_emitted_answer(drive_root: Any, delivery_id: str, emitted: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Retain the terminal text, routed chat and identity after the send returned.

    The immutable source binds exact text; failed capture dedupes only, proving no bytes."""
    match = _TERMINAL_ANSWER_ID.fullmatch(delivery_id)
    task_id = str(emitted.get("task_id") or "")
    text = emitted.get("text")
    if match is None or match.group("task") != task_id or not isinstance(text, str):
        return None
    try:
        from ouroboros.artifacts import store_actor_source_bytes

        record = {"delivery_id": delivery_id, "task_id": task_id, "chat_id": int(emitted["chat_id"]),
                  "text": text, "sent_at": emitted.get("sent_at") or utc_now_iso(), "basis": "send_handler_returned",
                  **{key: str(emitted.get(key) or "") for key in ("format", "role", "system_type", "terminal_origin")}}
        if isinstance(emitted.get("routing"), dict):
            record["routing"] = dict(emitted["routing"])
        ref = store_actor_source_bytes(
            pathlib.Path(drive_root), task_id, category="context_checkpoints", source_id="terminal-delivery",
            data=json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"), extension="json")
    except Exception:
        log.warning("terminal answer %s: emitted bytes could not be retained; no receipt", delivery_id, exc_info=True)
        return None
    return {"task_id": task_id, "chat_id": record["chat_id"], "sent_at": record["sent_at"],
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text_chars": len(text),
            "basis": record["basis"], "source_ref": ref}


def _read_registry(drive_root: Any) -> Dict[str, Any]:
    from ouroboros.utils import read_json_dict
    path = pathlib.Path(drive_root) / "state" / "terminal_deliveries.json"
    data = read_json_dict(path) if path.is_file() else {}
    if data is None:
        raise ValueError("registry file is malformed or is not an object")
    return data


def terminal_answer_receipts(drive_root: Any, task_id: str) -> Dict[str, Any]:
    """What the host can PROVE it emitted as one task's terminal answer, and what it still owes.

    ``delivered`` lists exact text digests, routed chats and retained sources
    captured after sends (``final:`` ids only). Owed rows and ids without
    receipts prove no bytes. Membership covers every terminal-message family
    (``final:``, the Stop/reap ``cancel:`` receipt, the ``cascade:`` digest), so a
    pending salvage answer is debt too. ``state`` is ``owed`` while any such
    message is still owed — debt outranks every older or technical receipt —
    then ``exhausted`` when the host gave up delivering one (its payload stays
    in the registry), then ``delivered`` (at least one receipt), else
    ``unknown``; an unreadable registry answers ``unknown`` with
    ``registry: "unreadable"``. Never inferred from the task result or an id digest.
    """
    fact: Dict[str, Any] = {"source": "terminal_delivery_registry", "state": "unknown", "delivered": [],
                            "unverified_delivery_ids": [], "owed_delivery_ids": [], "owed": [], "exhausted": []}
    try:
        data = _read_registry(drive_root)
        receipts, pending = _mapping_rows(data, "receipts", strict=True), _mapping_rows(data, "pending", strict=True)
        exhausted = _mapping_rows(data, "exhausted", strict=True)
        delivered = _delivered_rows(data, strict=True)
    except Exception:
        log.debug("terminal-delivery registry unreadable for %s", task_id, exc_info=True)
        fact["registry"] = "unreadable"
        return fact

    def answer(did: str) -> bool:
        match = TERMINAL_MESSAGE_ID.fullmatch(did)
        return match is not None and match.group("task") == str(task_id)

    for did in (item for item in delivered if answer(item)):
        receipt = receipts.get(did)
        if isinstance(receipt, dict) and receipt.get("text_sha256") and isinstance(receipt.get("source_ref"), dict):
            fact["delivered"].append({"delivery_id": did, **receipt})
        else:
            fact["unverified_delivery_ids"].append(did)
    fact["owed_delivery_ids"] = sorted(did for did in pending if answer(str(did)) and did not in delivered)
    # The owed rows themselves (exact text, routed chat): recovery finalizes from these bytes.
    fact["owed"] = [{**pending[did], "delivery_id": did} for did in fact["owed_delivery_ids"]
                    if isinstance(pending.get(did), dict)]
    fact["exhausted"] = [{**exhausted[did], "delivery_id": did} for did in sorted(exhausted)
                         if answer(str(did)) and did not in delivered and isinstance(exhausted.get(did), dict)]
    fact["state"] = ("owed" if fact["owed_delivery_ids"] else "exhausted" if fact["exhausted"]
                     else "delivered" if fact["delivered"] else "unknown")
    return fact


def delivery_id_for(task_id: str, text: str) -> str:
    """Stable terminal identity: ``final:<tid>:<sha256[:16]>``.

    Callers hash task/status framing and core answer, excluding mutable custody
    disclosures. Replay can refresh those notes without owing a second answer."""
    digest = hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]
    return f"final:{task_id}:{digest}"


def already_delivered(drive_root: Any, delivery_id: str, *, task_id: str = "", publication_token: Any = None) -> bool:
    """Read-only durable dedupe check (registration happens AFTER a real send). A canonical Main
    publication answers from its own receipt, by the event's typed task and publication token."""
    did = str(delivery_id or "").strip()
    if not did:
        return False
    try:
        from ouroboros.terminal_projection import main_delivery_recorded

        if publication_token:  # the bounded id cache cannot attest a different attempt
            return main_delivery_recorded(drive_root, did, task_id=task_id, publication_token=publication_token)
        data = _read_registry(drive_root)
        delivered = data.get("delivered")
        return isinstance(delivered, list) and did in delivered
    except Exception:
        # Fail open toward delivery: a broken registry read must never cost the
        # only owner-visible answer ("never lost" outranks "never doubled").
        log.debug("terminal-delivery registry read failed for %s", did, exc_info=True)
        return False


def _delivered_rows(current: Dict[str, Any], *, strict: bool = False) -> List[str]:
    """Read delivered ids; strict reads reject malformed containers or entries.

    Mutators refuse corruption without overwriting the file. Fail-soft readers
    keep only a valid list; corruption disclosure belongs to their caller."""
    delivered = current.get("delivered")
    if isinstance(delivered, list):
        if strict and any(not isinstance(item, str) for item in delivered):
            raise ValueError(
                "terminal-delivery registry 'delivered' holds non-string entries"
            )
        return [str(item) for item in delivered]
    if strict and delivered is not None:
        raise ValueError(
            "terminal-delivery registry 'delivered' is malformed (not a list)"
        )
    return []


def register_delivery(drive_root: Any, delivery_id: str, *, emitted: Optional[Dict[str, Any]] = None,
                      strict: bool = False) -> bool:
    """After send, retain exact bytes/ack and atomically retire the owed row.

    Return whether the bounded delivered-id registry gained an id. Strict callers
    retain debt on any receipt failure; legacy callers still count the live send.
    """
    did = str(delivery_id or "").strip()
    if not did:
        return True
    from ouroboros.terminal_projection import acknowledge_main_delivery

    acknowledge_main_delivery(drive_root, did, task_id=(emitted or {}).get("task_id"),
                              chat_id=(emitted or {}).get("chat_id"),
                              publication_token=(emitted or {}).get("terminal_projection_token"))
    fresh = {"value": False}
    receipt = _capture_emitted_answer(drive_root, did, emitted) if isinstance(emitted, dict) else None
    if strict and emitted and _TERMINAL_ANSWER_ID.fullmatch(did) and receipt is None:
        raise RuntimeError("terminal answer receipt capture deferred")

    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        rows: List[str] = _delivered_rows(current, strict=True)
        pending = _mapping_rows(current, "pending", strict=True)
        receipts = _mapping_rows(current, "receipts", strict=True)
        exhausted = _mapping_rows(current, "exhausted", strict=True)
        target = pending.get(did)
        if target is not None and not isinstance(target, dict):
            # GR6-3: never clear a corrupt owed row and destroy its forensic bytes.
            raise ValueError(f"terminal-delivery registry pending row for {did} is malformed")
        had_pending = target is not None and (not target.get("terminal_projection_token")
            or target["terminal_projection_token"] == (emitted or {}).get("terminal_projection_token"))
        if had_pending:
            pending.pop(did)
        # A real receipt retires only ITS exhausted mark: the answer reached the chat after all.
        was_exhausted = exhausted.pop(did, None) is not None
        new_receipt = receipt is not None and did not in receipts
        if new_receipt:
            receipts[did] = receipt
        if did not in rows:
            rows.append(did)
            fresh["value"] = True
        elif not (had_pending or new_receipt or was_exhausted):
            return None
        return _registry_document(current, rows, pending, receipts, exhausted)

    try:
        update_json_locked(_registry_path(drive_root), _mutate, strict_existing_dict=True,
                           timeout_sec=bounded_wait(4.0))
    except ValueError:
        _log_registry_corrupt(drive_root, did, op="register_delivery")
        if strict:
            raise
        return True
    except Exception:
        if strict:
            raise
        log.warning("terminal-delivery registry write failed for %s", did, exc_info=True)
        return True
    return fresh["value"]


def _log_registry_corrupt(drive_root: Any, delivery_id: str, *, op: str) -> None:
    """Disclose a refused corrupt-registry mutation as a durable typed event.

    The original malformed file stays untouched for diagnosis."""
    log.error(
        "terminal-delivery registry is corrupt; %s refused for %s (no overwrite)",
        op, delivery_id,
    )
    try:
        from ouroboros.utils import append_jsonl

        append_jsonl(
            pathlib.Path(drive_root) / "logs" / "events.jsonl",
            {"ts": utc_now_iso(), "type": "terminal_delivery_registry_corrupt",
             "op": op, "delivery_id": str(delivery_id or "")},
        )
    except Exception:
        log.debug("registry-corrupt event append failed", exc_info=True)


def register_pending_delivery(drive_root: Any, event: Dict[str, Any]) -> bool:
    """Owe exact bytes before enqueue; retire only after confirmed send/receipt.

    Newest _PENDING_CAP rows survive; eviction retains and discloses the payload.
    Deferred handlers retain timestamps/sent facts. False is missing id or disclosed
    failure (cancel stays open); True includes owed/delivered. Completion still tries live send."""
    did = str(event.get("delivery_id") or "").strip()
    if not did:
        return False
    token = event.get("terminal_projection_token")
    if token and already_delivered(drive_root, did, task_id=str(event.get("task_id") or ""), publication_token=token):
        return True
    event = {**event, "ts": event.get("ts") or utc_now_iso()}
    evicted: List[Dict[str, Any]] = []

    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        evicted.clear()
        rows: List[str] = _delivered_rows(current, strict=True)
        if did in rows and not token:
            return None  # already delivered: nothing is owed
        pending = _mapping_rows(current, "pending", strict=True)
        exhausted = _mapping_rows(current, "exhausted", strict=True)
        if did in pending and not isinstance(pending[did], dict):
            raise ValueError(f"terminal-delivery registry pending row for {did} is malformed")
        if did in pending and pending[did].get("terminal_projection_token") == token:
            if "delivery_deferred" not in event:
                return None
            pending[did].update({key: event[key] for key in ("ts", "delivery_deferred", "emitted", "transport_failed") if key in event})
            return _registry_document(current, rows, pending, exhausted=exhausted)
        pending[did] = {**without_finalization_timing(event), "registered_at": utc_now_iso()}
        pending[did].pop("type", None)
        if len(pending) > _PENDING_CAP:
            for stale in list(pending)[: len(pending) - _PENDING_CAP]:
                row = pending.pop(stale, None)
                # An eviction is a TRANSITION of the same debt, payload kept: the
                # exhausted row is what the Done card reads; the disclosure seam
                # below preserves a copy and emits the typed event.
                if isinstance(row, dict):
                    exhausted[str(stale)] = _exhausted_row(row, reason="outbox_capacity")
                    evicted.append({**exhausted[str(stale)], "delivery_id": str(stale)})
        return _registry_document(current, rows, pending, exhausted=exhausted)

    try:
        if token:
            from ouroboros.terminal_projection import main_delivery_current
            if not main_delivery_current(drive_root, str(event.get("task_id") or ""), token):
                return True  # an older send cannot replace the current publication's debt
        update_json_locked(_registry_path(drive_root), _mutate, strict_existing_dict=True,
                           timeout_sec=bounded_wait(4.0))
    except ValueError:
        _log_registry_corrupt(drive_root, did, op="register_pending_delivery")
        _log_unregistered_delivery(drive_root, event, did)
        return False
    except Exception:
        # Failed crash insurance is disclosed; the caller still tries the live send (GR3-4).
        log.error("terminal-delivery pending registration failed for %s", did, exc_info=True)
        _log_unregistered_delivery(drive_root, event, did)
        return False
    for row in evicted:
        _disclose_exhausted_delivery(drive_root, row, reason="outbox_capacity")
    return True


def withdraw_stop_episode(drive_root: Any, task_id: str) -> bool:
    """Completion won: the Stop's provisional ``cancel:`` salvage is no longer owed.

    Running custody owes its salvage before the terminal write; when the monotonic
    guard keeps the worker's own completed row instead, that undelivered row (and
    its undelivered receipt block) would hold the Done card and later replay a
    false "was stopped" message. A delivered id is history and stays.
    """
    tid = str(task_id or "")
    did = _stop_episode_delivery_id(pathlib.Path(drive_root), tid)
    if not did.startswith(f"cancel:{tid}:"):
        return True

    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pending = _mapping_rows(current, "pending", strict=True)
        if did not in pending or did in _delivered_rows(current, strict=True):
            return None
        pending.pop(did)
        return _registry_document(current, _delivered_rows(current, strict=True), pending,
                                  exhausted=_mapping_rows(current, "exhausted", strict=True))

    def _drop_receipt(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        receipt = row.get("cancel_receipt") if isinstance(row, dict) else None
        if not isinstance(receipt, dict) or receipt.get("delivery_id") != did or "delivered_chat_id" in receipt:
            return None
        return {key: value for key, value in row.items() if key != "cancel_receipt"}

    try:
        update_json_locked(_registry_path(drive_root), _mutate, strict_existing_dict=True)
        patch_document(pathlib.Path(drive_root), tid, _drop_receipt)
        return True
    except Exception:
        log.warning("stop-episode withdrawal failed for %s", did, exc_info=True)
        return False


def _log_unregistered_delivery(drive_root: Any, event: Dict[str, Any], did: str) -> None:
    """Typed durable disclosure of a terminal send whose owed registration failed."""
    try:
        from ouroboros.utils import append_jsonl

        append_jsonl(
            pathlib.Path(drive_root) / "logs" / "events.jsonl",
            {"ts": utc_now_iso(), "type": "terminal_delivery_unregistered",
             "task_id": str((event or {}).get("task_id") or ""),
             "delivery_id": str(did or ""),
             "detail": ("the owed-outbox registration failed; the live send (if any) "
                        "proceeds without crash insurance")},
        )
    except Exception:
        log.debug("unregistered-delivery event append failed for %s", did, exc_info=True)


def pending_deliveries(
    drive_root: Any, *, disclose_corruption: bool = False, strict: bool = False,
) -> List[Dict[str, Any]]:
    """Read owed sends, excluding delivered ids, with disclosed corruption.

    An absent registry is empty. Unreadable/malformed data logs an error and
    returns []; malformed individual rows are skipped with their bytes intact.
    ``disclose_corruption`` emits the corruption event; ``strict`` refuses an
    unreadable registry (including receipts) for evidence settlement."""
    try:
        data = _read_registry(drive_root)
        pending = _mapping_rows(data, "pending", strict=True)
        if strict:
            _mapping_rows(data, "receipts", strict=True)
            _delivered_rows(data, strict=True)
    except Exception as exc:
        if strict:
            raise
        log.error(
            "terminal-delivery registry is unreadable/malformed (%s); the owed "
            "outbox reads as EMPTY until the file is repaired", exc,
        )
        if disclose_corruption:
            _log_registry_corrupt(drive_root, "", op="pending_deliveries_read")
        return []
    delivered = data.get("delivered")
    delivered_ids = {str(item) for item in delivered} if isinstance(delivered, list) else set()
    rows: List[Dict[str, Any]] = []
    malformed: List[str] = []
    for did, row in pending.items():
        if not isinstance(row, dict):
            # Quarantine and disclose; strict mutations preserve these bytes on disk.
            malformed.append(str(did))
            continue
        if str(did) in delivered_ids and not row.get("terminal_projection_token"):
            continue
        rows.append({**row, "delivery_id": str(did)})
    if malformed:
        log.error(
            "terminal-delivery outbox holds %d malformed owed row(s) (%s); "
            "quarantined — skipped by replay, bytes kept on disk",
            len(malformed), ", ".join(malformed[:5]),
        )
        if disclose_corruption:
            _log_registry_corrupt(
                drive_root, ",".join(malformed[:5]), op="pending_row_malformed",
            )
    return rows


def _exhausted_row(row: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
    """The registry's exhausted disposition of one owed row: its payload plus how it ended."""
    try:
        attempts = int(row.get("replay_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {**row, "exhausted_at": utc_now_iso(), "reason": reason, "attempts": attempts, "preserved_path": ""}


def _bump_replay_attempts(drive_root: Any, ids: List[str]) -> None:
    """Count admitted copies/failed physical retries on the existing backoff.
    Queued/in-flight/unknown membership or busy pre-send storage spends nothing.
    Exhaustion is a separate transition before retry.
    """
    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        pending = _mapping_rows(current, "pending", strict=True)
        changed = False
        for did in ids:
            row = pending.get(did)
            if not isinstance(row, dict):
                continue
            changed = True
            pending[did] = {**row, "replay_attempts": int(row.get("replay_attempts") or 0) + 1,
                            "last_replay_at": utc_now_iso()}
        if not changed:
            return None
        rows = _delivered_rows(current, strict=True)
        return _registry_document(current, rows, pending)

    try:
        update_json_locked(_registry_path(drive_root), _mutate, strict_existing_dict=True, timeout_sec=bounded_wait(4.0))
    except ValueError:
        _log_registry_corrupt(drive_root, ",".join(ids), op="bump_replay_attempts")
    except Exception:
        log.warning("terminal-delivery replay bookkeeping failed", exc_info=True)


def _retire_exhausted(drive_root: Any, rows: List[Dict[str, Any]], *, reason: str = "replay_exhausted") -> None:
    """Move owed rows the host gives up on into the registry's ``exhausted`` mapping, payload kept.

    The transition re-checks each row under the registry lock (a receipt that
    landed meanwhile wins: nothing is retired), so the only active disposition is
    never lost between a pop and its disclosure. Each retired row is then
    disclosed through ``_disclose_exhausted_delivery``."""
    ids = [str(row.get("delivery_id") or "") for row in rows]
    retired: List[Dict[str, Any]] = []

    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        retired.clear()
        rows = _delivered_rows(current, strict=True)
        pending = _mapping_rows(current, "pending", strict=True)
        exhausted = _mapping_rows(current, "exhausted", strict=True)
        for did in ids:
            row = pending.get(did)
            if not isinstance(row, dict) or did in rows:
                continue
            exhausted[did] = _exhausted_row(pending.pop(did), reason=reason)
            retired.append({**exhausted[did], "delivery_id": did})
        if not retired:
            return None
        return _registry_document(current, rows, pending, exhausted=exhausted)

    try:
        update_json_locked(_registry_path(drive_root), _mutate, strict_existing_dict=True)
    except ValueError:
        _log_registry_corrupt(drive_root, ",".join(ids), op="retire_exhausted")
        return
    except Exception:
        log.warning("terminal-delivery exhaustion bookkeeping failed", exc_info=True)
        return
    for row in retired:
        _disclose_exhausted_delivery(drive_root, row, reason=reason)


def _stamp_exhausted_copy(drive_root: Any, delivery_id: str, preserved: str) -> None:
    """Record the preserved copy's path on the exhausted row: the Done card's pointer. Fail-soft."""
    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        exhausted = _mapping_rows(current, "exhausted", strict=True)
        row = exhausted.get(delivery_id)
        if not isinstance(row, dict) or row.get("preserved_path") == preserved:
            return None
        exhausted[delivery_id] = {**row, "preserved_path": preserved}
        return _registry_document(current, _delivered_rows(current, strict=True),
                                  _mapping_rows(current, "pending", strict=True), exhausted=exhausted)

    try:
        update_json_locked(_registry_path(drive_root), _mutate, strict_existing_dict=True)
    except Exception:
        log.debug("exhausted-delivery copy path could not be recorded for %s", delivery_id, exc_info=True)


def _replay_attempts(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("replay_attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _disclose_exhausted_delivery(
    drive_root: Any, row: Dict[str, Any], *, reason: str = "replay_exhausted",
) -> None:
    """Disclose an outbox row the host gave up delivering (its registry row is already ``exhausted``).

    Replay exhaustion and capacity eviction share this seam. Preserve THIS row's
    full message (a receipt may hold only an excerpt) and stamp the copy's path on
    the exhausted row (the Done card points at it; a failed copy stays an honest
    empty path, never a claimed copy), emit ``terminal_delivery_exhausted`` even
    if chat fails, and say sending was not confirmed. The separate ⚠️ chat row and
    the card settlement belong to the supervisor process only: a worker-side
    capacity eviction records the mark and the copy and leaves the rest to the
    delivery/reconcile duties. Only a cancel receipt proving this delivery reached
    this destination suppresses the notice; a bare delivery id proves no
    destination. Every step is fail-soft."""
    from ouroboros.utils import in_worker_process

    did = str(row.get("delivery_id") or "")
    tid = str(row.get("task_id") or "")
    if row.get("terminal_projection_token"):
        # The canonical readiness retains bytes, destination and retry timing.
        # Evicting its transport cache slot is not an exhausted owner message.
        log.info("Main delivery %s left transport cache; canonical obligation remains", did)
        return
    text = str(row.get("text") or "")
    capacity = reason == "outbox_capacity"
    try:
        attempts = int(row.get("attempts") if "attempts" in row else row.get("replay_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    if not capacity:
        attempts = max(attempts, _PENDING_MAX_REPLAYS)
    try:
        chat_id = int(row.get("chat_id") or 0)
    except (TypeError, ValueError):
        chat_id = 0
    detail = ("pending outbox over capacity; oldest owed row evicted" if capacity
              else "send never confirmed; replay cap reached")
    log.error("Terminal message %s (task %s) dropped with no confirmed send (%s); disclosing", did, tid, detail)
    preserved = ""
    try:
        from ouroboros.observability import preserve_salvaged_output

        if tid and text:
            preserved = preserve_salvaged_output(
                pathlib.Path(drive_root), f"{tid}.undelivered", text,
            )
    except Exception:
        log.debug("exhausted-delivery preservation failed for %s", did, exc_info=True)
    if preserved and did:
        _stamp_exhausted_copy(drive_root, did, preserved)
    try:  # positive evidence only: the cancel receipt stamped THIS delivery at THIS chat
        from ouroboros.task_results import load_task_result

        receipt = (load_task_result(pathlib.Path(drive_root), tid) or {}).get("cancel_receipt") if tid and did and chat_id != 0 else None
        reached = isinstance(receipt, dict) and receipt.get("delivery_id") == did and receipt.get("delivered_chat_id") == chat_id
    except Exception:
        reached = False
    try:
        from ouroboros.utils import append_jsonl

        append_jsonl(
            pathlib.Path(drive_root) / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(), "type": "terminal_delivery_exhausted",
                "task_id": tid, "delivery_id": did, "chat_id": chat_id,
                "reason": reason,
                "attempts": attempts, "preserved_path": preserved, "detail": detail,
                **({"owner_notice": "not_sent_same_delivery_recorded_at_this_chat"} if reached else {}),
            },
        )
    except Exception:
        log.debug("exhausted-delivery event append failed for %s", did, exc_info=True)
    if in_worker_process():
        return  # the mark and the copy are recorded; chat and the card are the supervisor's
    if tid:
        try:
            from ouroboros.terminal_projection import settle_terminal_projection

            # The card may have been held behind this very debt: let it settle now,
            # stating the exhausted delivery (prepare=False: readiness stays task_done's).
            settle_terminal_projection(drive_root, tid, prepare=False)
        except Exception:
            log.debug("terminal projection settlement after exhausted delivery deferred", exc_info=True)
    # A live notice needs a reader; the hidden partition (0) has none.
    if chat_id == 0 or reached:
        return
    try:
        from supervisor.message_bus import send_with_budget

        copy_note = (
            f"A copy of this message is saved at {preserved}." if preserved
            else "No copy of this message could be saved."
        )
        cause = (
            f"its pending-outbox slot was taken by newer owed messages (capacity {_PENDING_CAP})"
            if capacity else
            f"no send was confirmed after {_PENDING_MAX_REPLAYS} replay attempts (chat "
            "transport failing or the supervisor kept crashing mid-send)"
        )
        send_with_budget(
            chat_id,
            f"⚠️ Sending a message for task {tid} was not confirmed: {cause}. {copy_note}",
            role="system",
            system_type="terminal_incident",
        )
    except Exception:
        log.debug("exhausted-delivery chat notice failed for %s", did, exc_info=True)


def replay_pending_deliveries(drive_root: Any, *, event_queue: Any = None) -> List[str]:
    """Continue deferred writes/sends/receipts off-consumer; replay lost queued sends.

    Deferred work uses the same outbox and exact id; queued originals keep bus membership.
    Lost sends keep the age/backoff/attempt cap; a strict receipt retires debt atomically.
    Corruption is disclosed, never proof that no debt remains. Returns continued/replayed ids.
    """
    rows = pending_deliveries(drive_root, disclose_corruption=True)
    from supervisor.events_chat_delivery import replay_deferred_deliveries
    continued = replay_deferred_deliveries(drive_root, rows, event_queue=event_queue)
    rows = [row for row in rows if not row.get("delivery_deferred")]
    # Cached Main debt goes through its settlement (supervisor queue): the canonical claim is its one retry clock.
    from ouroboros.terminal_projection import settle_terminal_projection
    from ouroboros.install_identity import StoreForeign
    for task_id in sorted({str(r.get("task_id") or "") for r in rows if r.get("terminal_projection_token")} - {""}):
        try:
            settle_terminal_projection(drive_root, task_id, event_queue=event_queue)
        except StoreForeign as exc:  # a copy's debt never stops this replay; the room shows it foreign
            log.debug("Main delivery of %s retained: %s", task_id, exc)
    owed = [row for row in rows if not row.get("terminal_projection_token") and _replay_due(row)]
    if not owed:
        return continued
    if event_queue is None:
        try:
            from supervisor import workers

            event_queue = workers.get_event_q(create=False)
        except Exception:
            log.warning("terminal-delivery replay could not reach the event queue", exc_info=True)
            return continued
    spent = [row for row in owed if _replay_attempts(row) >= _PENDING_MAX_REPLAYS]
    if spent:
        # The cap is reached: the debt TRANSITIONS to exhausted (payload kept, copy
        # preserved, disclosed) instead of being enqueued once more, but only once the
        # bus says its last copy left the handler: a copy still queued or being handled,
        # or unobservable membership, is not a given-up delivery (the next sweep decides).
        from supervisor.event_observation import delivery_outstanding

        done = [row for row in spent if delivery_outstanding(event_queue, str(row.get("delivery_id") or "")) is False]
        if done:
            _retire_exhausted(drive_root, done)
        owed = [row for row in owed if row not in spent]
    if not owed:
        return continued
    replayed: List[str] = []
    for row in owed:
        did = str(row.get("delivery_id") or "")
        if already_delivered(drive_root, did):
            continue  # a receipt landed since the rows were read: nothing to replay
        event = {**row, "type": "send_message"}
        for key in ("registered_at", "replay_attempts", "last_replay_at"):
            event.pop(key, None)
        admitted = _put_unless_outstanding(event_queue, event)
        if admitted is None:
            log.warning("terminal-delivery replay skipped this sweep: bus membership is unobservable")
            break
        if admitted:
            replayed.append(did)
    if replayed:
        # Attempts count only a copy that really entered the bus (owner: never burn
        # retries while the message already stands in the queue).
        _bump_replay_attempts(drive_root, replayed)
        log.info("Replayed %d undelivered terminal answer(s): %s", len(replayed), replayed)
    return continued + replayed


def _put_unless_outstanding(event_queue: Any, event: Dict[str, Any]) -> Optional[bool]:
    """Atomically enqueue only with proven absent delivery membership (INTERFACES I1).

    The bus holds each id from put through physical handler completion. True spends
    one retry; False means queued/in-flight; None means missing/broken observation.
    Neither refusal spends a retry; the next sweep or fresh boot bus can retry.
    """
    put = getattr(event_queue, "put_unless_outstanding", None)
    if put is None:
        return None
    try:
        return bool(put(event))
    except Exception:
        log.warning("terminal-delivery replay: bus membership unobservable for %s",
                    event.get("delivery_id"), exc_info=True)
        return None


def _replay_due(row: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """Whether an owed row is old enough that its send is presumed lost.

    Spaced with exponential backoff (AR2-7): attempt N waits
    ``_REPLAY_MIN_AGE_SEC * 2**N`` after the PREVIOUS replay (60s, 120s, 240s,
    480s, 960s — ~31 minutes of coverage), measured from ``last_replay_at``
    once one exists. Without the stamp all five attempts burned on consecutive
    supervisor ticks and the cap covered a ~5-minute outage at best.
    """
    raw = str(row.get("last_replay_at") or row.get("registered_at") or "").replace("Z", "+00:00")
    if not raw:
        return True
    try:
        attempts = int(row.get("replay_attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    try:
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(raw)
        stamped = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        current = now if now is not None else datetime.now(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return True
    return (current - stamped) >= _REPLAY_MIN_AGE_SEC * (2 ** min(attempts, 6))


def salvage_cancelled_output(
    drive_root: Any, task_drive: Any, task_id: str,
) -> tuple[str, str, str]:
    """(result-suffix note, full salvaged text, preserved-copy path) — fail-soft.

    The kill-path salvage: the note rides the terminal result, the FULL text is
    preserved on the drive that OUTLIVES the child drive publication deletes
    (BIBLE P1 — a bounded preview alone is not a rescue when it is about to
    become the only copy), and the path feeds this seam's delivery receipt.
    """
    try:
        from ouroboros.observability import (
            latest_llm_response_text, preserve_salvaged_output, salvaged_output_note,
        )

        note = salvaged_output_note(
            pathlib.Path(task_drive), str(task_id),
            preserve_root=pathlib.Path(drive_root),
        )
        text = latest_llm_response_text(pathlib.Path(task_drive), str(task_id))
        path = ""
        if text:
            try:
                path = preserve_salvaged_output(pathlib.Path(drive_root), str(task_id), text)
            except Exception:
                path = ""
        return note, text, path
    except Exception:
        log.debug("Failed to salvage last LLM response for cancelled %s", task_id, exc_info=True)
        return "", "", ""


def unreconciled_runs_note(unreconciled_runs: Optional[List[str]]) -> str:
    """GR6-5a: the ONE outcome-independent disclosure line for open delegated runs.

    Carried on the owner's custody row (never inside the model's answer)
    whenever the list is non-empty, REGARDLESS of the outcome (completed,
    failed, cancelled, reaped): a task whose teardown left delegated runs open
    must never read as cleanly finished.
    Returns "" for an empty list.
    """
    runs = [str(rid) for rid in (unreconciled_runs or []) if str(rid)]
    if not runs:
        return ""
    return (
        f"\n\n⚠️ {len(runs)} delegated run(s) may still be live: "
        + ", ".join(runs)
    )


def build_completed_result_event(
    drive_root: Any, task: Optional[Dict[str, Any]], task_id: str,
    stored: Optional[Dict[str, Any]],
    *, unreconciled_runs: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Build (without sending) the completion-wins ``send_message`` event.

    Split from the enqueue half (GR2-4) so the cancel path can register the
    answer as OWED in the durable outbox BEFORE it settles the cancel intent —
    a crash between the settle and the send then replays the row instead of
    losing both the watchdog trigger and the answer. Returns None when there
    is nothing deliverable (no text, a silent wake with no custody line, no chat).

    ``unreconciled_runs``: a completed answer whose teardown left delegated
    runs open carries the outcome-independent disclosure line on its CUSTODY
    row, never inside the assistant answer — host words never speak as the
    model's (issue #1006). The note stays out of the delivery id too: the id
    digests the CORE answer only, so a replay whose rebuilt note shrank (runs
    reconciled meanwhile) dedups to the same owed message instead of minting a
    second one. This also makes the id byte-equal to the natural path's
    ``final:<tid>:<digest>`` for the same stored answer.
    """
    tid = str(task_id or "").strip()
    core_text = str((stored or {}).get("result") or "")
    task_row = task if isinstance(task, dict) and task else dict(stored or {})
    chat_id = lineage_chat_id(pathlib.Path(drive_root), task_row, tid)
    quiet = (stored or {}).get("wake_silent") is True  # its words stay internal; only custody may speak
    if not tid or not (core_text or quiet) or not chat_id:
        return None
    from ouroboros.owner_stop_receipt import terminal_event_fields
    from ouroboros.task_finalization import TERMINAL_ORIGIN_HOST_NOTICE, terminal_custody_notice_text

    runs = [str(rid) for rid in (unreconciled_runs or []) if str(rid)]
    custody = terminal_custody_notice_text(stored or {})
    note = unreconciled_runs_note(runs).lstrip("\n")
    if note and any(run not in custody for run in runs):
        custody = "\n\n".join(part for part in (note, custody) if part)
    if quiet and not (core_text := custody):  # a silent wake's row is the host's custody line alone
        return None
    event = {
        "type": "send_message", "chat_id": chat_id, "task_id": tid, "text": core_text,
        # A re-delivered copy that drops markdown renders as a different message.
        "format": "markdown",
        "delivery_id": delivery_id_for(tid, core_text),
        **({"terminal_custody_notice": custody} if custody and not quiet else {}),
        **({} if quiet else terminal_event_fields(drive_root, tid)),  # Wrap up's receipt row (#1564)
    }
    return project_terminal_result_event(
        pathlib.Path(drive_root), task_row, tid,
        result_text=core_text,
        terminal_origin=TERMINAL_ORIGIN_HOST_NOTICE if quiet else (stored or {}).get("terminal_origin"),
        base_event=event, provider_notice=str((stored or {}).get("terminal_provider_notice") or ""),
    )


def project_terminal_result_event(
    drive_root: Any,
    task: Optional[Dict[str, Any]],
    task_id: str,
    *,
    result_text: str,
    terminal_origin: Any,
    base_event: Optional[Dict[str, Any]] = None,
    provider_notice: str = "",
) -> Dict[str, Any]:
    """Project one terminal event from producer-stamped origin.

    ``host_salvage`` becomes one short keyed plain System receipt (inherited
    ``format``/``log_text`` dropped; the full bytes stay in task details); a
    subagent's receipt also names its card placement, a root's does not.
    ``host_notice`` is a text the host wrote alone, so it keeps its OWN words
    and inherited markdown and becomes a System row WITHOUT a system_type,
    which is what lets a replayed card conclude on it. ``model_final`` and a
    missing legacy origin keep the assistant projection untouched — no
    text/status/length inference. The delivery id always digests the stable
    core result rather than a mutable disclosure suffix.
    """
    tid = str(task_id or "")
    core_text = str(result_text or "")
    event = dict(base_event or {})
    event.setdefault("type", "send_message")
    event.setdefault("task_id", tid)
    event.setdefault("chat_id", lineage_chat_id(drive_root, task or {}, tid))
    event["delivery_id"] = delivery_id_for(tid, core_text)
    origin = str(terminal_origin or "")
    if origin in HOST_AUTHORED_TERMINAL_ORIGINS:
        salvage = origin == TERMINAL_ORIGIN_HOST_SALVAGE
        receipt = (provider_notice + "\n\nThe full intermediate output and technical details are preserved "
                   "in the task details.") if provider_notice else _HOST_SALVAGE_RECEIPT
        event.update({
            "text": receipt if salvage else (event.get("text") or core_text),
            "role": "system",
            "terminal_origin": origin,
            **({"system_type": "terminal_incident"} if salvage else {}),
        })
        if salvage:
            event.pop("log_text", None)
            event.pop("format", None)
            # A child's receipt is a row of that child's card. The row id derives
            # from the delivery id, so the live send, the owed outbox row and
            # its replay all name one row.
            from ouroboros.subagent_messages import subagent_message_meta

            meta = event.get("progress_meta") if isinstance(event.get("progress_meta"), dict) else {}
            if subagent_message_meta(task) or subagent_message_meta(meta):
                event["progress_meta"] = {**meta, "card_row": "timeline",
                                          "card_row_id": event["delivery_id"] + ":terminal_incident"}
        return event
    if origin == TERMINAL_ORIGIN_MODEL_FINAL:
        event["terminal_origin"] = TERMINAL_ORIGIN_MODEL_FINAL
    # A missing origin now identifies only a row written before every forced
    # rail stamped one; it stays absent so legacy rows replay byte-compatibly.
    return event


ENQUEUE_QUEUED = "queued"
ENQUEUE_ALREADY_DELIVERED = "already_delivered"
ENQUEUE_QUEUED_UNREGISTERED = "queued_unregistered"
ENQUEUE_UNAVAILABLE = "unavailable"
ENQUEUE_OUTCOMES = (
    ENQUEUE_QUEUED, ENQUEUE_ALREADY_DELIVERED, ENQUEUE_QUEUED_UNREGISTERED, ENQUEUE_UNAVAILABLE,
)


def enqueue_terminal_delivery_outcome(
    drive_root: Any, event: Dict[str, Any], *, event_queue: Any = None,
) -> str:
    """Dedupe, register as owed (idempotent), enqueue; answer one typed word.

    Safe after owed-before-settle: registration is keyed by ``delivery_id``
    and repeat-safe. Four facts a caller may branch on:
    ``queued`` (owed row written, live send queued), ``already_delivered``
    (this id already went out — nothing is owed, nothing failed),
    ``queued_unregistered`` (the live send is queued but the owed row could not
    be written: a crash before the send loses it — the ``register`` seam already
    emitted its typed event), ``unavailable`` (no event, or the queue refused).
    A boolean collapsed the first two with the last two; a receipt consumer read
    an idempotent repeat as a failure and a lost owed row as durable.
    """
    did = str((event or {}).get("delivery_id") or "")
    tid = str((event or {}).get("task_id") or "")
    if not event:
        return ENQUEUE_UNAVAILABLE
    if already_delivered(pathlib.Path(drive_root), did, task_id=tid,
                         publication_token=event.get("terminal_projection_token")):
        return ENQUEUE_ALREADY_DELIVERED
    registered = register_pending_delivery(pathlib.Path(drive_root), event)
    try:
        if event_queue is None:
            from supervisor import workers

            event_queue = workers.get_event_q(create=False)
        # After Main's clock claim, admit atomically; ordinary final/cancel retain their first put.
        if not event.get("terminal_projection_token"):
            event_queue.put(dict(event))
        elif not _put_unless_outstanding(event_queue, dict(event)):
            return ENQUEUE_UNAVAILABLE
    except Exception:
        log.warning("terminal-delivery enqueue failed for %s", tid, exc_info=True)
        return ENQUEUE_UNAVAILABLE
    return ENQUEUE_QUEUED if registered else ENQUEUE_QUEUED_UNREGISTERED


def enqueue_terminal_delivery(
    drive_root: Any, event: Dict[str, Any], *, event_queue: Any = None,
) -> bool:
    """Boolean projection of ``enqueue_terminal_delivery_outcome``: was a live
    send queued? (An already-delivered id and a refused queue both read False —
    callers that must tell those apart use the typed outcome.)"""
    outcome = enqueue_terminal_delivery_outcome(drive_root, event, event_queue=event_queue)
    return outcome in (ENQUEUE_QUEUED, ENQUEUE_QUEUED_UNREGISTERED)


def deliver_completed_result(
    drive_root: Any, task: Optional[Dict[str, Any]], task_id: str,
    stored: Optional[Dict[str, Any]], *, event_queue: Any = None,
    unreconciled_runs: Optional[List[str]] = None,
) -> bool:
    """Completion-wins delivery (owner 4=A + 5=A): ship a KEPT completed result.

    The worker died (or was already gone) before its own final delivery could be
    confirmed, so the completed answer goes out through this seam: owed BEFORE
    enqueued, deduped by the same ``final:<tid>:<digest>`` identity the natural
    path mints — a copy the worker already delivered is suppressed durably.
    Returns whether a send was enqueued. ``unreconciled_runs`` rides the custody
    row, never the answer text or the delivery id, so the owed registration and
    the publish half mint the same ids.
    """
    event = build_completed_result_event(
        pathlib.Path(drive_root), task, task_id, stored,
        unreconciled_runs=unreconciled_runs,
    )
    if event is None:
        return False
    return enqueue_terminal_delivery(pathlib.Path(drive_root), event, event_queue=event_queue)


def deliver_miss_lane_outcome(
    drive_root: Any, task_drive: Any, row: Dict[str, Any], task_id: str, status: str,
    *, event_queue: Any = None, unreconciled_runs: Optional[List[str]] = None,
) -> bool:
    """Owner 5=A on the finalize-on-miss lane: a task that was neither queued
    nor running when custody caught up still gets its answer delivered — a
    completed result as itself (completion wins, normal delivery), a cancelled
    outcome as an UNREVIEWED salvage. Other settled statuses (failed /
    rejected) keep their own paths' delivery. Fail-soft; the durable dedupe
    suppresses a copy an earlier path already sent. Subagent results flow to
    their parent through the ordinary handoff instead of chat.

    ``unreconciled_runs`` (GR5-3) rides the cancelled salvage message exactly
    like the kill path's disclosure — a miss-lane cancel that left delegated
    runs open must not read as a clean completion.

    Returns whether the answer is DURABLY accounted for (GR4-1, mirroring
    ``_register_owed_terminal_delivery``): owed in the outbox, already
    delivered/owed, or legitimately not deliverable here (subagent /
    non-delivering status / no lineage chat, which records the typed handoff
    row). ``False`` means a real durability gap — the owed registration
    failed — and the caller must leave the cancel intent OPEN (claim
    released) instead of settling over an unowed answer.
    """
    row = row if isinstance(row, dict) else {}
    if str(row.get("delegation_role") or "") == "subagent":
        return True
    try:
        if status == "completed":
            # The completed answer's custody row carries the outcome-independent
            # unreconciled-runs line; the answer text stays the model's.
            event = build_completed_result_event(
                pathlib.Path(drive_root), row, task_id, row,
                unreconciled_runs=list(unreconciled_runs or []),
            )
            if event is None:
                return True  # nothing deliverable (no text / no lineage chat)
            owed = register_pending_delivery(pathlib.Path(drive_root), event)
            enqueue_terminal_delivery(
                pathlib.Path(drive_root), event, event_queue=event_queue,
            )
            return owed
        if status != "cancelled":
            # failed/rejected outcomes keep their own paths' delivery — but an
            # open delegated run is a fact no outcome may swallow (GR6-5a):
            # deliver the ONE disclosure line as its own message when the list
            # is non-empty. Owed-before-enqueued and delivery-id-deduped like
            # every terminal send through this seam.
            note = unreconciled_runs_note(unreconciled_runs)
            if not note:
                return True
            chat_id = lineage_chat_id(pathlib.Path(drive_root), row, task_id)
            if not chat_id:
                return True
            # GR7-4: the id digests the STABLE core (task id + settled
            # status); the mutable note rides the text only, so a replay
            # whose list shrank dedups instead of owing a second message.
            core = f"Task {task_id} settled as {status}."
            event = {
                "type": "send_message", "chat_id": chat_id, "task_id": task_id,
                "text": core + note, "delivery_id": delivery_id_for(task_id, core),
                # Host-authored disclosure fact — typed SYSTEM (Q4 non-mimicry).
                "role": "system", "system_type": "cancel_receipt",
                "ts": utc_now_iso(),
            }
            owed = register_pending_delivery(pathlib.Path(drive_root), event)
            enqueue_terminal_delivery(
                pathlib.Path(drive_root), event, event_queue=event_queue,
            )
            return owed
        from ouroboros.observability import preserved_salvage_path
        from supervisor.cancel_publication import stop_notice_outcome

        _note, text, path = salvage_cancelled_output(
            pathlib.Path(drive_root), task_drive, task_id,
        )
        if not path:
            path = preserved_salvage_path(pathlib.Path(drive_root), task_id)
        if not text and path:
            try:
                text = pathlib.Path(path).read_text(encoding="utf-8")
            except Exception:
                text = ""
        # GR4-1: the owed registration happens FIRST so its result is
        # observable; the shared seam below re-registers idempotently (and
        # records the typed handoff row when there is no lineage chat).
        outcome = stop_notice_outcome(row.get("cancel_origin"))
        event = build_unreviewed_salvage_event(
            pathlib.Path(drive_root), row, task_id, outcome=outcome,
            salvaged_text=text, preserved_path=path, settled_status="cancelled",
            unreconciled_runs=list(unreconciled_runs or []),
        )
        owed = True if event is None else register_pending_delivery(pathlib.Path(drive_root), event)
        deliver_unreviewed_salvage(
            pathlib.Path(drive_root), row, task_id, outcome=outcome,
            salvaged_text=text, preserved_path=path,
            settled_status="cancelled", event_queue=event_queue,
            unreconciled_runs=list(unreconciled_runs or []),
        )
        return owed
    except Exception:
        log.warning("Miss-lane terminal delivery failed for %s", task_id, exc_info=True)
        return False


def salvage_preview(text: str, *, limit: int = SALVAGE_PREVIEW_CHARS) -> tuple[str, int]:
    """Bounded head preview plus the EXACT omitted char count (0 when whole)."""
    body = str(text or "")
    if len(body) <= limit:
        return body, 0
    return body[:limit], len(body) - limit


def _salvage_receipt(preserved_path: str) -> Dict[str, Any]:
    """Durable full-copy receipt: path + size + sha256 (empty when unreadable)."""
    receipt: Dict[str, Any] = {"path": str(preserved_path or "")}
    try:
        path = pathlib.Path(str(preserved_path or ""))
        data = path.read_bytes()
        receipt["size_bytes"] = len(data)
        receipt["sha256"] = hashlib.sha256(data).hexdigest()
        # The replay guard in _persist_cancel_receipt only lets a block with
        # preserved=True heal an earlier placeholder — mark the REAL receipt.
        receipt["preserved"] = True
    except Exception:
        receipt["unreadable"] = True
    return receipt


def _preview_note_line(preserved_path: str, omitted: int) -> str:
    """ONE plain sentence about the preview and where the technical facts live.

    Q5=A: path, sha256, byte count, and the children digest belong in the task
    DETAILS panel (the durable ``cancel_receipt`` block), never in chat. The
    chat keeps only the honesty half the owner must see inline: whether the
    preview is the WHOLE salvage (exact omitted count) and whether a durable
    full copy exists at all — a silently-truncated or silently-unpreserved
    fragment must not read as complete.
    """
    omitted_text = (
        f"{omitted} chars omitted from this preview" if omitted
        else "nothing omitted — this is the whole salvaged text"
    )
    if not str(preserved_path or "").strip():
        return (
            f"[{omitted_text}; NO durable full copy was preserved for this task "
            "(preservation did not run or found nothing)]"
        )
    return (
        f"[{omitted_text}; the full copy and technical details are in the "
        "task's details panel]"
    )


def lineage_chat_id(drive_root: Any, task: Dict[str, Any], task_id: str) -> int:
    """The task's OWN lineage chat (project binding first), never owner_chat_id."""
    try:
        from ouroboros.projects_registry import project_chat_for_task

        for candidate in (
            task_id,
            str(task.get("parent_task_id") or ""),
            str(task.get("root_task_id") or ""),
        ):
            if not candidate:
                continue
            bound = int(project_chat_for_task(drive_root, candidate) or 0)
            if bound:
                return bound
    except Exception:
        log.debug("terminal-delivery project chat lookup failed", exc_info=True)
    try:
        return int(task.get("chat_id") or 0)
    except (TypeError, ValueError):
        return 0


def _stop_episode_delivery_id(drive_root: Any, task_id: str) -> str:
    """CF-04: retain ``cancel:<tid>:<request_id>`` across settle and delivery.

    Replay may clear pending before publication; the retained task receipt or
    outcome keeps that episode. No recorded Stop keeps the ordinary fallback.
    """
    tid = str(task_id or "")
    try:
        from ouroboros.cancel_intents import active_intent

        rid = str((active_intent(pathlib.Path(drive_root), tid) or {}).get("request_id") or "")
        if rid:
            return f"cancel:{tid}:{rid}"
    except Exception:
        log.debug("stop-episode intent read failed for %s", tid, exc_info=True)
    try:
        data = _read_registry(drive_root)
        prefix = f"cancel:{tid}:"
        owed = [
            (str(row.get("registered_at") or ""), did)
            for did, row in _mapping_rows(data, "pending").items()
            if isinstance(row, dict) and did.startswith(prefix)
        ]
        if owed:
            return max(owed)[1]
    except Exception:
        log.debug("stop-episode owed-row read failed for %s", tid, exc_info=True)
    try:
        from ouroboros.task_results import load_task_result

        stored = load_task_result(pathlib.Path(drive_root), tid, strict=True) or {}
        receipt = stored.get("cancel_receipt")
        did = str(receipt.get("delivery_id") or "") if isinstance(receipt, dict) else ""
        if did.startswith(f"cancel:{tid}:"):
            return did
        origin = stored.get("cancel_origin")
        rid = str(origin.get("request_id") or "") if isinstance(origin, dict) else ""
        if rid:
            return f"cancel:{tid}:{rid}"
    except Exception:
        log.debug("stop-episode retained identity read failed for %s", tid, exc_info=True)
    return ""


def _persist_cancel_receipt(
    drive_root: Any, task_id: str, *,
    settled_status: str, outcome: str, delivery_id: str,
    preserved_path: str, preview_omitted: int,
    children: Optional[List[Dict[str, Any]]] = None,
    unreconciled_runs: Optional[List[str]] = None,
    reason_code: str = "", files_rescued: Optional[Dict[str, Any]] = None,
) -> None:
    """Q5=A: the technical stop facts live in the task DETAILS panel.

    Merges ONE typed ``cancel_receipt`` block into the EXISTING durable task
    result (``TaskDetailResponse`` is an open shape, so the panel gets it with
    no contract change): full-copy path + size + full sha256 (or an honest
    unreadable/unpreserved marker), exact preview-omitted count, the stop
    reason when the intent is still readable, and the historical children
    digest for a cascade root. Never creates the result record (a later full
    write would clobber a block-only row) and never clobbers previously
    persisted non-empty facts with an emptier rebuild. Fail-soft.
    ``files_rescued`` (TZ-2 C2) is the typed stat-only store count the receipt text speaks.
    """
    tid = str(task_id or "")
    try:
        block: Dict[str, Any] = {
            "settled_status": str(settled_status or ""),
            "outcome": str(outcome or ""),
            "delivery_id": str(delivery_id or ""),
            "preview_omitted_chars": int(preview_omitted or 0),
            "salvage": (
                _salvage_receipt(preserved_path)
                if str(preserved_path or "").strip()
                else {"path": "", "preserved": False}
            ),
            "ts": utc_now_iso(),
            **({"files_rescued": dict(files_rescued)} if files_rescued else {}),
        }
        if str(reason_code or ""):
            block["reason_code"] = str(reason_code)  # TZ-2 C1: the typed rail beside its sentence
        rows = [
            {"task_id": str(c.get("task_id") or ""),
             "outcome": str(c.get("outcome") or ""),
             "salvaged": bool(c.get("salvaged"))}
            for c in list(children or []) if isinstance(c, dict)
        ]
        if rows:
            block["children"] = rows[:40]
            if len(rows) > 40:
                block["children_omitted"] = len(rows) - 40
        runs = [str(rid) for rid in (unreconciled_runs or []) if str(rid)]
        if runs:
            block["unreconciled_runs"] = runs
        try:
            from ouroboros.cancel_intents import active_intent
            from ouroboros.task_results import load_task_result

            # The cause outlives the intent: once custody settles, `cancel_origin`
            # on the stored result is where the same scalars live, and a receipt
            # rebuilt after the settle must name the stop the owner actually made.
            cause = active_intent(pathlib.Path(drive_root), tid) or {}
            if not cause:
                stored = load_task_result(pathlib.Path(drive_root), tid) or {}
                origin = stored.get("cancel_origin")
                cause = origin if isinstance(origin, dict) else {}
            for key, field in (("reason", "stop_reason"), ("requested_at", "stop_requested_at")):
                if cause.get(key):
                    block[field] = str(cause[key])
        except Exception:
            log.debug("cancel-receipt stop cause read failed for %s", tid, exc_info=True)

        def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            if not isinstance(current, dict) or not current:
                return None  # no durable row yet — never mint a block-only file
            merged = dict(current.get("cancel_receipt") or {}) if isinstance(
                current.get("cancel_receipt"), dict) else {}
            if block["delivery_id"] != merged.get("delivery_id"):
                merged.pop("delivered_chat_id", None)
            for key, value in block.items():
                if (
                    key == "salvage"
                    and key in merged
                    and not (isinstance(value, dict) and value.get("preserved"))
                ):
                    # A replay/rebuild without a REAL salvage (the always-truthy
                    # {"path":"","preserved":False} placeholder) must not clobber
                    # a previously persisted preserved-copy fact.
                    continue
                if value or key not in merged:
                    merged[key] = value
            current = dict(current)
            current["cancel_receipt"] = merged
            return current

        patch_document(pathlib.Path(drive_root), tid, _mutate)
    except Exception:
        log.debug("cancel-receipt persistence failed for %s", tid, exc_info=True)


def record_cancel_receipt_delivery(
    drive_root: Any, task_id: str, delivery_id: str, chat_id: int, *, strict: bool = False,
) -> None:
    """Record the actual sent destination on the matching existing receipt.

    This write is separate from delivery registration. A failed write leaves
    destination evidence unknown; it never authorizes suppressing an excerpt.
    """
    try:
        def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            receipt = current.get("cancel_receipt")
            if (str(current.get("task_id") or "") != task_id
                    or not isinstance(receipt, dict)
                    or not delivery_id or receipt.get("delivery_id") != delivery_id):
                return None
            return {**current, "cancel_receipt": {**receipt, "delivered_chat_id": chat_id}}

        patch_document(pathlib.Path(drive_root), task_id, _mutate)
    except Exception:
        if strict:
            raise
        log.debug("cancel-receipt delivery evidence failed for %s", task_id, exc_info=True)


def build_unreviewed_salvage_event(
    drive_root: Any,
    task: Optional[Dict[str, Any]],
    task_id: str,
    *,
    outcome: str,
    salvaged_text: str = "",
    preserved_path: str = "",
    children: Optional[List[Dict[str, Any]]] = None,
    unreconciled_runs: Optional[List[str]] = None,
    settled_status: str = "",
    delivery_id: str = "",
    reason_code: str = "",
) -> Optional[Dict[str, Any]]:
    """Build (without sending) the one salvage/terminal chat message.

    GR2-4: the cancel path owes this event before settling the intent.
    Returns None without a lineage chat.

    ``settled_status`` is the TYPED stored status (GR2-12): the completed-vs-
    salvage branch compares it against ``STATUS_COMPLETED`` instead of parsing
    the presentation prose in ``outcome``. The wording itself is unchanged.

    ``delivery_id`` overrides the content-derived identity (GR4-2): the cascade
    summary dedups per INTENT, not per content, because a replay's rebuilt
    digest can differ while still being the same owed message.

    CF-04: ``_stop_episode_delivery_id`` retains the exact Stop identity across
    replay/settle; terminal paths without a Stop keep ``final:...`` identities.

    Q4/Q5 presentation: the message is a typed SYSTEM receipt (``role`` +
    ``system_type`` ride the event end to end — Q4 non-mimicry), a raw
    fragment is named the last persisted intermediate model message (never an
    "answer"), and the technical facts (path/sha256/bytes/children digest)
    live in the durable ``cancel_receipt`` block on the task result — the
    details panel — not in chat.
    ``reason_code`` (TZ-2 C1): the TYPED rail; with an empty ``outcome`` the owner
    sentence comes from TASK_CAUSE_PHRASES here, and the code rides the receipt, not the prose.
    TZ-2 C2: text and receipt state the files rescued (positive/zero/unknown, stat-only walk, no hashes).
    """
    from ouroboros.owner_stop_receipt import terminal_event_fields
    from ouroboros.task_results import STATUS_COMPLETED

    tid = str(task_id or "").strip()
    if not tid:
        return None
    code = str(reason_code or "").strip()
    if code and not str(outcome or "").strip():
        from ouroboros.project_dialogue import TASK_CAUSE_PHRASES
        outcome = f"stopped by the supervisor. {TASK_CAUSE_PHRASES.get(code, code)}"
    task_row = task if isinstance(task, dict) else {}
    chat_id = lineage_chat_id(pathlib.Path(drive_root), task_row, tid)
    if not chat_id:
        log.debug("terminal-delivery skipped for %s: no lineage chat", tid)
        return None
    body = str(salvaged_text or "").strip()
    preview, omitted = salvage_preview(body)
    outcome_text = str(outcome or "stopped")
    descendants = (
        f" {len(children)} descendant task(s) were settled with it." if children else ""
    )
    # Only a receipt with a preview promises one below (new fallback ids; owed rows keep their bytes).
    if str(settled_status or "").strip().lower() == STATUS_COMPLETED:
        # Completion-wins (owner 4=A): the kept result is the real answer, not a
        # salvage — but it still bypassed the normal delivery path, so say so.
        lines = [f"✅ Task {tid} {outcome_text}." + (" Its completed result is preserved below." if preview else "")
                 + descendants]
    else:
        lines = [f"⚠️ Task {tid} was {outcome_text}." + (
            " Below is the last persisted intermediate model message, preserved WITHOUT review (salvaged "
            "best-effort; NOT a final answer)." if preview else "") + descendants]
    lines += ["", preview, "", _preview_note_line(preserved_path, omitted)] if preview else [
        "", "(no salvageable agent output was found for this task)"]
    # TZ-2 C2: "no salvageable text" never implies "no files": the stat-only store count
    # rides the mutable disclosure (never the content-derived identity), hashes not computed.
    rescued = rescued_files_fact(tid, artifact_store_roots(drive_root, tid, task=task_row))
    disclosure_lines: List[str] = ["", rescued_files_sentence(rescued)]
    if unreconciled_runs:
        # GR3-7: an audit-failure marker means run state is UNKNOWN — a
        # different honest sentence than "these named runs stayed open".
        unknown = [
            str(rid) for rid in unreconciled_runs
            if str(rid).startswith(RUN_STATE_UNKNOWN_PREFIX)
        ]
        named = [
            str(rid) for rid in unreconciled_runs
            if not str(rid).startswith(RUN_STATE_UNKNOWN_PREFIX)
        ]
        if named:
            disclosure_lines += [
                "",
                "⚠️ DELEGATED RUNS NOT RECONCILED: the teardown could not reach the "
                "engine to settle "
                + ", ".join(named)
                + ". They may still be running and mutating the workspace until the "
                "orphan sweep catches them.",
            ]
        if unknown:
            disclosure_lines += [
                "",
                "⚠️ DELEGATED RUN STATE UNKNOWN: the teardown audit itself failed "
                f"({', '.join(unknown)}), so whether this task holds live delegated "
                "runs could not be determined. Periodic reconciliation will settle "
                "any that are still open.",
            ]
    # GR7-4: identity digests the STABLE lines only; the mutable disclosure
    # rides the text after the salvage body.
    text = "\n".join(lines + disclosure_lines)
    did = (
        str(delivery_id or "").strip()
        or _stop_episode_delivery_id(pathlib.Path(drive_root), tid)
        or delivery_id_for(tid, "\n".join(lines))
    )
    _persist_cancel_receipt(
        pathlib.Path(drive_root), tid,
        settled_status=str(settled_status or ""), outcome=outcome_text,
        delivery_id=did, preserved_path=str(preserved_path or ""),
        preview_omitted=omitted, children=children,
        unreconciled_runs=unreconciled_runs, reason_code=code, files_rescued=rescued,
    )
    event = {
        "type": "send_message",
        "chat_id": chat_id,
        "task_id": tid,
        "text": text,
        # Q4 non-mimicry: a host-authored receipt is typed SYSTEM end to end
        # (live WS frame, chat.jsonl direction, history replay) — it is never
        # rendered as Ouroboros's own speech. Card-neutral: task_id is a
        # subject reference; only task-result/task_done closes the card.
        "role": "system",
        "system_type": "cancel_receipt",
        "delivery_id": did,
        "ts": utc_now_iso(),
    }
    if code:
        event["reason_code"] = code
    event.update(terminal_event_fields(drive_root, tid))
    return event


def deliver_unreviewed_salvage(
    drive_root: Any,
    task: Optional[Dict[str, Any]],
    task_id: str,
    *,
    outcome: str,
    salvaged_text: str = "",
    preserved_path: str = "",
    children: Optional[List[Dict[str, Any]]] = None,
    unreconciled_runs: Optional[List[str]] = None,
    settled_status: str = "",
    delivery_id: str = "",
    reason_code: str = "",
    event_queue: Any = None,
) -> bool:
    """Enqueue ONE unreviewed-salvage chat message for a cancelled/reaped task.

    Fail-soft and idempotent (durable ``delivery_id`` dedupe). Returns whether a
    message was actually enqueued. ``event_queue`` defaults to the live worker
    event queue; the supervisor's ``send_message`` handler performs the actual
    chat send with its ordinary formatting and transport behavior.

    ``unreconciled_runs`` names delegated runs the teardown could NOT settle
    (engine unreachable): the owner is being told the task is stopped, so a run
    that may still be mutating the tree belongs in the same message.
    """
    event = build_unreviewed_salvage_event(
        pathlib.Path(drive_root), task, task_id,
        outcome=outcome, salvaged_text=salvaged_text, preserved_path=preserved_path,
        children=children, unreconciled_runs=unreconciled_runs,
        settled_status=settled_status, delivery_id=delivery_id, reason_code=reason_code,
    )
    if event is None:
        # GR3-1c: a terminal outcome with NO resolvable lineage chat records a
        # typed handoff row — the crash-order evidence must show the message
        # was consciously not owed, never silently dropped. (The cascade
        # postcondition relies on this: it always calls through here before it
        # settles the root intent.)
        try:
            from ouroboros.utils import append_jsonl

            append_jsonl(
                pathlib.Path(drive_root) / "logs" / "supervisor.jsonl",
                {"ts": utc_now_iso(), "type": "terminal_delivery_handoff",
                 "task_id": str(task_id or ""),
                 "settled_status": str(settled_status or ""),
                 "outcome": str(outcome or ""), "reason": "no_lineage_chat"},
            )
        except Exception:
            log.debug("no-chat handoff record failed for %s", task_id, exc_info=True)
        return False
    # OWED before ENQUEUED: a crash between here and the send replays this row
    # instead of losing the only owner-visible copy of the answer.
    return enqueue_terminal_delivery(pathlib.Path(drive_root), event, event_queue=event_queue)


# The cascade summary family lives in supervisor/terminal_cascade.py (moved at this module's
# size ceiling); callers and tests keep this module as the one import and patch surface.
from supervisor.terminal_cascade import (  # noqa: E402,F401
    _cascade_descendant_rows, _cascade_root_outcome, deliver_cascade_summary,
)
