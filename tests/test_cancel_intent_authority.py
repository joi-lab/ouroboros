"""Cancellation authority is the durable intent, and only the intent (invariant 14).

Phantom cancelled-subagent card in Main (14.10.2026): a ``cancel_task`` event drained
51 minutes late ran custody with no open intent, a busy ownership read was reported as
"changed", and the resulting "❌ cancel … did not settle" row named a child without
lineage in the owner's chat. Three owner decisions close the class:

- A1: the event is a silent wake of ``drive_cancel_intent_scope``; no intent is a typed
  no-op that touches nothing.
- A2: ``cancel_task_custody`` refuses without a claimed intent or an explicit root-cascade
  claim; the dead no-intent facade is gone.
- A3: the ownership read is one signed transaction outside the queue lock; under the lock
  the re-check is non-blocking and tri-state, and the retry chain is walked only when the
  task has ``superseded_by``.

Every test here fails with the corresponding change reverted and keeps the still-working
case beside it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros import cancel_intents as ci
from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result
from tests._cancel_intents_shared import qenv as _qenv

qenv = _qenv


def _event_ctx(sent):
    return SimpleNamespace(load_state=lambda: {"owner_chat_id": 1},
                           send_with_budget=lambda *a, **k: sent.append((a, k)))


# ---------------------------------------------------------------- A1 -------------------------

def test_a1_cancel_event_without_open_intent_never_tears_down(qenv, monkeypatch):
    """A stale wake for a live task that nobody asked to cancel: nothing happens, nothing is said."""
    from supervisor.events_runtime_controls import _drive_cancel_task_event

    qenv.q.PENDING[:] = [{"id": "live-no-intent", "chat_id": 1}]
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    write_task_result(qenv.drive, "live-no-intent", "scheduled")
    sent = []
    _drive_cancel_task_event({"task_id": "live-no-intent"}, _event_ctx(sent))
    assert [t["id"] for t in qenv.q.PENDING] == ["live-no-intent"]
    assert load_task_result(qenv.drive, "live-no-intent")["status"] == "scheduled"
    assert sent == []


def test_a1_wake_with_a_closed_intent_is_typed_and_touches_the_queue_not_at_all(qenv, monkeypatch):
    """The incident shape: the watchdog settled the intent an hour ago; the event arrives now.
    ``already_settled`` is the answer; a queued row for that id is NOT scrubbed here (custody
    owns ghost rows, under its own claim)."""
    custody_calls = []
    monkeypatch.setattr(qenv.q, "cancel_task_custody",
                        lambda tid, **kw: custody_calls.append(tid) or qenv.tl.CANCEL_CANCELLED)
    write_task_result(qenv.drive, "settled-child", STATUS_CANCELLED)
    qenv.q.PENDING[:] = [{"id": "settled-child", "chat_id": 1}]

    assert qenv.tl.drive_cancel_intent_scope("settled-child") == qenv.tl.CANCEL_ALREADY_SETTLED
    assert [t["id"] for t in qenv.q.PENDING] == ["settled-child"]
    assert custody_calls == []

    write_task_result(qenv.drive, "live-child", "scheduled")
    qenv.q.PENDING.append({"id": "live-child", "chat_id": 1})
    assert qenv.tl.drive_cancel_intent_scope("live-child") == qenv.tl.CANCEL_NOT_FOUND
    assert [t["id"] for t in qenv.q.PENDING] == ["settled-child", "live-child"]
    assert custody_calls == []


def test_a1_wake_with_an_open_intent_drives_custody_once_and_still_says_nothing(qenv, monkeypatch):
    from supervisor.events_runtime_controls import _drive_cancel_task_event

    custody_calls = []
    monkeypatch.setattr(qenv.q, "cancel_task_custody",
                        lambda tid, **kw: custody_calls.append(tid) or qenv.tl.CANCEL_CANCELLED)
    write_task_result(qenv.drive, "wanted", "scheduled")
    qenv.q.PENDING[:] = [{"id": "wanted", "chat_id": 1}]
    ci.request_cancel(qenv.drive, "wanted", reason="stop")
    sent = []
    _drive_cancel_task_event({"task_id": "wanted"}, _event_ctx(sent))
    assert custody_calls == ["wanted"]
    assert sent == []


def test_a1_wake_claimed_by_another_worker_is_silent_and_leaves_the_claim(qenv, monkeypatch):
    """The eighth phantom row: the event lost the claim race to the watchdog 10 s before
    settlement and announced a failure. A refused claim is a typed outcome the wake ignores."""
    from supervisor.events_runtime_controls import _drive_cancel_task_event

    write_task_result(qenv.drive, "raced", "scheduled")
    qenv.q.PENDING[:] = [{"id": "raced", "chat_id": 1}]
    ci.request_cancel(qenv.drive, "raced", reason="stop")
    owner = ci.claim_intent(qenv.drive, "raced", owner="watchdog")
    assert owner and not owner.get("claim_refused")
    sent = []
    _drive_cancel_task_event({"task_id": "raced"}, _event_ctx(sent))
    assert sent == []
    current = ci.active_intent(qenv.drive, "raced")
    assert (current["state"], current["claim_owner"], current["generation"]) == (
        ci.INTENT_CLAIMED, "watchdog", owner["generation"])
    assert [t["id"] for t in qenv.q.PENDING] == ["raced"]
    assert load_task_result(qenv.drive, "raced")["status"] == "scheduled"


# ---------------------------------------------------------------- A2 -------------------------

def test_a2_the_no_intent_boolean_facade_is_gone(qenv, monkeypatch):
    """cancel_task_by_id is cascade-only by signature; the single shape ran custody without
    any intent. A cascade over a tree nobody asked to cancel tears nothing down."""
    import inspect

    assert list(inspect.signature(qenv.tl.cancel_task_by_id).parameters) == ["task_id"]
    assert not hasattr(qenv.q, "_cancel_task_by_id_single")
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    qenv.q.PENDING[:] = [{"id": "facade", "chat_id": 1}]
    write_task_result(qenv.drive, "facade", "scheduled")
    assert qenv.tl.cancel_task_by_id("facade") is False
    assert [t["id"] for t in qenv.q.PENDING] == ["facade"]
    assert load_task_result(qenv.drive, "facade")["status"] == "scheduled"


def test_a2_cascade_stops_a_child_whose_own_mint_failed_only_under_the_root_claim(qenv, monkeypatch):
    """The one authority besides a task's own intent is the root's durable cascade
    intent, passed explicitly by the sweep. Without it a cascade touches nothing; with
    it a child whose intent write failed is still stopped, its forensic row kept."""
    import json

    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    qenv.q.PENDING[:] = [
        {"id": "tree", "chat_id": 1, "root_task_id": "tree"},
        {"id": "kid", "chat_id": 1, "root_task_id": "tree", "parent_task_id": "tree"},
    ]
    write_task_result(qenv.drive, "tree", "scheduled")
    write_task_result(qenv.drive, "kid", "scheduled", parent_task_id="tree", root_task_id="tree")
    real_request = ci.request_cancel

    def _unwritable_for_the_child(root, tid, **kw):
        if tid == "kid":
            raise OSError("intent store unwritable")
        return real_request(root, tid, **kw)

    monkeypatch.setattr(ci, "request_cancel", _unwritable_for_the_child)

    assert qenv.tl.cancel_task_by_id("tree") is False
    assert sorted(t["id"] for t in qenv.q.PENDING) == ["kid", "tree"]
    assert load_task_result(qenv.drive, "kid")["status"] == "scheduled"

    real_request(qenv.drive, "tree", scope=ci.SCOPE_CASCADE, reason="stop the tree")
    assert qenv.tl.cancel_task_by_id("tree") is True
    assert qenv.q.PENDING == []
    assert load_task_result(qenv.drive, "kid")["status"] == STATUS_CANCELLED
    assert load_task_result(qenv.drive, "tree")["status"] == STATUS_CANCELLED
    rows = [json.loads(line) for line in
            (qenv.drive / "logs" / "supervisor.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(row.get("type") == "cascade_descendant_intent_write_failed" and row.get("task_id") == "kid"
               for row in rows)


def test_a2_retry_transfer_mints_no_successor_authority_after_an_empty_claim(qenv, monkeypatch):
    """The legacy retry-transfer lane runs before the main claim; an empty claim on the
    logical root must not mint an intent on the physical retry and tear it down."""
    from tests._cancel_intents_shared import _write_root_retry_pair

    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    monkeypatch.setattr(qenv.q, "QUEUE_MAX_RETRIES", 2)
    _write_root_retry_pair(qenv.drive, "old-root", "new-root")
    qenv.q.PENDING[:] = [{
        "id": "new-root", "type": "task", "chat_id": 0, "depth": 0, "root_task_id": "old-root",
        "parent_task_id": "", "delegation_role": "root", "original_task_id": "old-root",
        "timeout_retry_from": "old-root",
    }]

    assert qenv.tl.cancel_task_custody("old-root") == qenv.tl.CANCEL_NOT_FOUND
    assert ci.active_intents(qenv.drive) == {}
    assert [t["id"] for t in qenv.q.PENDING] == ["new-root"]

    minted = ci.request_cancel(qenv.drive, "old-root", reason="stop")
    assert minted["task_id"] == "new-root"
    assert qenv.tl.cancel_task_custody("new-root") == qenv.tl.CANCEL_CANCELLED
    assert qenv.q.PENDING == []
    assert load_task_result(qenv.drive, "new-root")["status"] == STATUS_CANCELLED


# ---------------------------------------------------------------- A3 -------------------------

def _signature_busy_at_zero_budget(monkeypatch):
    """A writer is active: a normal-budget read waits and succeeds; a zero-budget
    probe (``acquisition_budget(0)``) cannot acquire and raises StoreBusy. Returns
    a callable that lets the store answer again."""
    from ouroboros import store, task_store

    real_sig = task_store.signature

    def _sig(root, tid, *, txn=None):
        if store._WAIT_BUDGET.get() == 0:
            raise store.StoreBusy("writer active")
        return real_sig(root, tid, txn=txn)

    monkeypatch.setattr(task_store, "signature", _sig)
    return lambda: monkeypatch.setattr(task_store, "signature", real_sig)


def test_a3_load_outside_queue_lock_is_one_signed_transaction(tmp_path, monkeypatch):
    """The incident's false FAILED: body read, then a zero-budget signature re-check that a
    concurrent writer turned into "changed while reading". Outside Q the body and its
    signature come from one transaction with the normal budget and nothing is re-checked."""
    from ouroboros import task_store
    from supervisor.task_ownership import TaskOwnershipRead

    write_task_result(tmp_path, "t-a3", "running")
    _signature_busy_at_zero_budget(monkeypatch)
    reads = TaskOwnershipRead(tmp_path)
    assert reads.load("t-a3")["status"] == "running"
    assert reads.stamps["t-a3"] == task_store.signature(tmp_path, "t-a3")


@pytest.mark.parametrize("shape", ["subagent", "root"])
def test_a3_a_task_without_superseded_by_is_not_rechecked_under_the_lock(qenv, monkeypatch, shape):
    """'Walk the retry chain only if the task has superseded_by': a task whose live worker
    rewrites its own row between preparation and the locked resolver is still cancelled."""
    from supervisor import task_ownership

    lineage = ({"delegation_role": "subagent", "parent_task_id": "root-a3", "root_task_id": "root-a3"}
               if shape == "subagent" else {"delegation_role": "root", "root_task_id": "kid-a3"})
    ci.request_cancel(qenv.drive, "kid-a3", reason="stop")
    qenv.q.PENDING[:] = [{"id": "kid-a3", "chat_id": 1, **lineage}]
    write_task_result(qenv.drive, "kid-a3", "scheduled", **lineage)
    real_prepare = task_ownership.prepare_retry_chain

    def _prepare_then_worker_writes(q, task_id, read):
        real_prepare(q, task_id, read)
        write_task_result(qenv.drive, task_id, "scheduled", progress_note="worker wrote meanwhile")

    monkeypatch.setattr(task_ownership, "prepare_retry_chain", _prepare_then_worker_writes)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    assert qenv.tl.cancel_task_custody("kid-a3") == qenv.tl.CANCEL_CANCELLED
    assert load_task_result(qenv.drive, "kid-a3")["status"] == STATUS_CANCELLED
    assert qenv.q.PENDING == []


def _retry_chain(qenv, monkeypatch):
    from tests._cancel_intents_shared import _write_root_retry_pair

    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    monkeypatch.setattr(qenv.q, "QUEUE_MAX_RETRIES", 2)
    _write_root_retry_pair(qenv.drive, "old-root", "new-root")
    qenv.q.PENDING[:] = [{
        "id": "new-root", "type": "task", "chat_id": 0, "depth": 0, "root_task_id": "old-root",
        "parent_task_id": "", "delegation_role": "root", "original_task_id": "old-root",
        "timeout_retry_from": "old-root",
    }]
    minted = ci.request_cancel(qenv.drive, "old-root", reason="stop")
    assert minted["task_id"] == "new-root"


def test_a3_busy_store_under_the_lock_is_unknown_and_the_watchdog_finishes(qenv, monkeypatch, caplog):
    """With superseded_by the chain IS re-checked under Q with a zero budget: BUSY is
    neither a change nor sameness — no capture, no claim taken, the intent stays open
    (requested), the logged reason says busy (not the incident's false "changed"), and
    the next sweep settles it once the store answers."""
    import logging
    from datetime import datetime

    _retry_chain(qenv, monkeypatch)
    store_answers_again = _signature_busy_at_zero_budget(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="supervisor.task_lifecycle"):
        assert qenv.tl.cancel_task_custody("old-root") == qenv.tl.CANCEL_FAILED
    assert "ownership result busy during this pass" in caplog.text
    assert "changed" not in caplog.text
    intent = ci.active_intent(qenv.drive, "new-root")
    assert intent["state"] == ci.INTENT_REQUESTED
    assert [t["id"] for t in qenv.q.PENDING] == ["new-root"]
    assert load_task_result(qenv.drive, "new-root")["status"] == "scheduled"

    store_answers_again()
    requested_at = datetime.fromisoformat(intent["requested_at"].replace("Z", "+00:00")).timestamp()
    outcomes = qenv.tl.sweep_cancel_intents(now=requested_at + 60)
    assert outcomes == {"new-root": qenv.tl.CANCEL_CANCELLED}
    assert load_task_result(qenv.drive, "new-root")["status"] == STATUS_CANCELLED
    assert ci.active_intents(qenv.drive) == {}


def test_a3_a_changed_chain_row_under_the_lock_refuses_without_touching_anything(qenv, monkeypatch):
    from supervisor import task_ownership

    _retry_chain(qenv, monkeypatch)
    real_prepare = task_ownership.prepare_retry_chain

    def _prepare_then_leaf_rewritten(q, task_id, read):
        real_prepare(q, task_id, read)
        write_task_result(qenv.drive, "new-root", "scheduled", progress_note="rewritten meanwhile")

    monkeypatch.setattr(task_ownership, "prepare_retry_chain", _prepare_then_leaf_rewritten)
    assert qenv.tl.cancel_task_custody("old-root") == qenv.tl.CANCEL_FAILED
    assert ci.active_intent(qenv.drive, "new-root")["state"] == ci.INTENT_REQUESTED
    assert [t["id"] for t in qenv.q.PENDING] == ["new-root"]
    assert load_task_result(qenv.drive, "new-root")["status"] == "scheduled"


def test_a3_custody_parses_no_result_under_the_queue_lock(qenv, monkeypatch):
    """The capture judges the row prepared outside the lock; a result that settles
    between preparation and capture is still honoured (completion wins)."""
    from ouroboros import task_results
    from supervisor import task_ownership

    parses_under_lock = []
    real_load = task_results.load_task_result

    def _load(root, tid, **kw):
        if qenv.q._queue_lock._is_owned():
            parses_under_lock.append(tid)
        return real_load(root, tid, **kw)

    monkeypatch.setattr(task_results, "load_task_result", _load)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    ci.request_cancel(qenv.drive, "queued", reason="stop")
    qenv.q.PENDING[:] = [{"id": "queued", "chat_id": 1}]
    write_task_result(qenv.drive, "queued", "scheduled")
    assert qenv.tl.cancel_task_custody("queued") == qenv.tl.CANCEL_CANCELLED
    assert parses_under_lock == []

    ci.request_cancel(qenv.drive, "finisher", reason="stop")
    write_task_result(qenv.drive, "finisher", "running")
    real_prepare = task_ownership.prepare_retry_chain

    def _prepare_then_completes(q, task_id, read):
        real_prepare(q, task_id, read)
        write_task_result(qenv.drive, task_id, "completed", result="done just in time")

    monkeypatch.setattr(task_ownership, "prepare_retry_chain", _prepare_then_completes)
    assert qenv.tl.cancel_task_custody("finisher") == qenv.tl.CANCEL_ALREADY_SETTLED
    assert load_task_result(qenv.drive, "finisher")["status"] == "completed"
    assert parses_under_lock == []
    assert ci.active_intents(qenv.drive) == {}


# ------------------------------------------------- fix round 1: Presence-owned rows ----------

def _presence_row(drive, task_id, kind="presence"):
    from ouroboros.task_status import execution_owner_record

    write_task_result(drive, task_id, "running", result="Task is running.", _is_direct_chat=True,
                      execution_owner=execution_owner_record(drive, {"id": task_id}, kind))


def test_miss_lane_leaves_a_live_presence_turns_row_to_its_runner(qenv, monkeypatch):
    """Intent-first minting reaches custody for a RUNNING row no supervisor map owns. A
    Presence turn (its own thread, execution_owner kind 'presence') must not be rewritten
    as cancelled while it keeps running: the intent settles not_found, the row stays.
    A pooled row no map owns is still finalized on miss."""
    from supervisor import terminal_delivery

    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal_delivery, "deliver_miss_lane_outcome", lambda *a, **kw: True, raising=False)
    _presence_row(qenv.drive, "presence-turn")
    ci.request_cancel(qenv.drive, "presence-turn", reason="stop")

    assert qenv.tl.cancel_task_custody("presence-turn") == qenv.tl.CANCEL_NOT_FOUND
    assert load_task_result(qenv.drive, "presence-turn")["status"] == "running"
    assert ci.active_intents(qenv.drive) == {}

    _presence_row(qenv.drive, "orphan-pooled", kind="pooled")
    ci.request_cancel(qenv.drive, "orphan-pooled", reason="stop")
    assert qenv.tl.cancel_task_custody("orphan-pooled") == qenv.tl.CANCEL_CANCELLED
    assert load_task_result(qenv.drive, "orphan-pooled")["status"] == STATUS_CANCELLED
    assert ci.active_intents(qenv.drive) == {}


def test_http_stop_on_a_live_presence_turn_answers_404_and_leaves_no_intent(tmp_path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ouroboros.gateway.tasks import api_task_cancel
    from supervisor import queue, workers

    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(queue, "_emit_cancel_task_done", lambda *_a, **_k: None)
    _presence_row(tmp_path, "presence-http")
    app = Starlette(routes=[Route("/api/tasks/{task_id}/cancel", api_task_cancel, methods=["POST"])])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        response = client.post("/api/tasks/presence-http/cancel")
    assert response.status_code == 404, response.text
    assert load_task_result(tmp_path, "presence-http")["status"] == "running"
    assert ci.active_intents(tmp_path) == {}


def test_http_cascade_stop_on_a_live_presence_turn_answers_404_and_records_nothing(tmp_path, monkeypatch):
    """The cascade path reads the target row before minting; a live Presence turn's row
    is its runner's, so the whole-tree Stop answers 404 and records no intent, while an
    ordinary persisted task in the same shape is still accepted and stopped."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ouroboros.gateway.tasks import api_task_cancel
    from supervisor import queue, workers

    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(queue, "_emit_cancel_task_done", lambda *_a, **_k: None)
    _presence_row(tmp_path, "presence-tree")
    _presence_row(tmp_path, "pooled-tree", kind="pooled")
    app = Starlette(routes=[Route("/api/tasks/{task_id}/cancel", api_task_cancel, methods=["POST"])])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        presence = client.post("/api/tasks/presence-tree/cancel", json={"cascade": True})
        pooled = client.post("/api/tasks/pooled-tree/cancel", json={"cascade": True})
    assert presence.status_code == 404, presence.text
    assert load_task_result(tmp_path, "presence-tree")["status"] == "running"
    assert "presence-tree" not in ci.active_intents(tmp_path)
    assert pooled.status_code in (200, 202), pooled.text
    assert pooled.status_code == 202 or load_task_result(tmp_path, "pooled-tree")["status"] == "cancelled"
