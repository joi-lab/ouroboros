"""A timeout retry keeps the Project its predecessor's work already has.

The incident: a Project-bound root that timed out was retried under a new physical
id which copied the owner-message origin ref but carried no durable binding, so the
retried work painted a Main card offering "Turn into project" — a second convertible
unit for one piece of work — until some later implicit act adopted it.

The reaper now binds the successor inside its own admission transaction
(``task_reaper._run_retry_admission_transaction`` ->
``worker_promotion.bind_retry_to_origin_project``), under the claim lock every
implicit claim holds, and only once cancellation has lost that boundary: a binding
is immutable, so a bound-but-never-admitted retry id would answer
``project_id_for_task`` forever.
"""

from __future__ import annotations

import json
import types

import pytest

from ouroboros import cancel_intents as ci
from ouroboros.contracts.chat_id_policy import project_chat_id
from ouroboros.project_dialogue import build_owner_message_ref
from ouroboros.projects_registry import (
    all_task_project_bindings,
    begin_project_deletion,
    bind_task_to_project,
    create_project,
    project_binding_for_task,
    project_id_for_task,
)
from ouroboros.task_results import STATUS_CANCELLED, STATUS_RUNNING, write_task_result
from tests._cancel_intents_shared import qenv as _qenv

qenv = _qenv

OWNER_TEXT = "the importer keeps timing out, please fix it"


def _owner_ref(client_message_id: str = "msg-retry-1", chat_id: int = 1) -> dict:
    return build_owner_message_ref(
        chat_id=chat_id,
        client_message_id=client_message_id,
        ts="2026-09-15T09:30:00+00:00",
        text=OWNER_TEXT,
    )


def _patch_retry_input_handoff(monkeypatch):
    """Attachment/mailbox carry-over is exercised by the reaper's own suites; these
    tests are about what the admission transaction binds."""
    monkeypatch.setattr(
        "ouroboros.artifacts.handoff_task_attachments_for_retry",
        lambda *_args, **_kwargs: ({}, ""),
    )
    monkeypatch.setattr(
        "ouroboros.owner_mailbox.copy_owner_mailbox_for_retry",
        lambda *_args, **_kwargs: True,
    )


def _root_task(task_id: str, *, ref: dict | None = None) -> dict:
    """A pooled root exactly as the queue carries it. A root converted post-hoc
    ("Turn into project") carries NO ``project_id`` on its row — the durable
    binding is the only truth about its Project, and it is the shape the retry
    used to lose.
    """
    task = {
        "id": task_id,
        "type": "task",
        "chat_id": 1,
        "depth": 0,
        "root_task_id": task_id,
        "parent_task_id": "",
        "delegation_role": "root",
    }
    if ref is not None:
        task["origin_message_ref"] = dict(ref)
        task["origin_message_text"] = OWNER_TEXT
    return task


def _bind_root(drive, task_id: str, pid: str, *, ref: dict | None) -> dict:
    create_project(drive, pid, name="Importer room", origin="owner_ui")
    origin = (
        {"ref": dict(ref), "text": OWNER_TEXT} if ref is not None
        else {"absent": "mid_task_no_origin"}
    )
    return bind_task_to_project(drive, task_id, pid, origin=origin)


def _retry(qenv, task: dict, old_id: str, new_id: str):
    from supervisor import task_reaper as tr

    return tr._enqueue_retry(
        qenv.q,
        task,
        task_id=old_id,
        retry_task_id=new_id,
        attempt=1,
        terminal_reason="idle_timeout",
        recon_fields={},
    )


def _events(drive) -> list[dict]:
    path = drive / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_retry_of_a_bound_root_is_in_the_project_before_its_first_round(qenv, monkeypatch):
    """(a) The successor is bound while it is still PENDING — no worker has picked
    it up, so the owner never sees the retried work as an unclaimed Main card."""
    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id, pid = "bound-old", "bound-new", "importer-room"
    ref = _owner_ref()
    root = _root_task(old_id, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    predecessor = _bind_root(qenv.drive, old_id, pid, ref=ref)

    requeued, new_attempt, _reason, suppression = _retry(qenv, root, old_id, new_id)

    assert (requeued, new_attempt, suppression) == (True, 2, {})
    assert [row["id"] for row in qenv.q.PENDING] == [new_id]
    assert qenv.q.RUNNING == {}
    assert project_id_for_task(qenv.drive, new_id) == pid
    # The UI census (/api/state) is what paints the card's room.
    assert all_task_project_bindings(qenv.drive)[new_id] == {
        "project_id": pid, "chat_id": project_chat_id(pid),
    }
    # The retry joins the SAME owner-message unit: its origin is the predecessor's
    # stored one, carried by value rather than re-derived.
    binding = project_binding_for_task(qenv.drive, new_id)
    assert binding["source_ref"] == predecessor["source_ref"]
    assert binding["source_text"] == predecessor["source_text"]


def test_retry_adopts_the_project_a_sibling_of_the_same_owner_message_holds(qenv, monkeypatch):
    """The origin-keyed half: the timed-out root was never bound itself, but the
    direct turn that received the same owner message was. One message, one room."""
    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id, pid = "adopt-old", "adopt-new", "sibling-room"
    ref = _owner_ref("msg-retry-sibling")
    root = _root_task(old_id, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    _bind_root(qenv.drive, "turn-that-received-it", pid, ref=ref)
    assert project_id_for_task(qenv.drive, old_id) == ""

    requeued, _attempt, _reason, suppression = _retry(qenv, root, old_id, new_id)

    assert (requeued, suppression) == (True, {})
    assert project_id_for_task(qenv.drive, new_id) == pid


def test_retry_of_a_cancelled_root_is_never_bound(qenv, monkeypatch):
    """(b) A root that already settled as cancelled has no successor to place: the
    admission transaction suppresses the retry, so nothing durable claims its id."""
    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id, pid = "cancelled-old", "cancelled-new", "cancelled-room"
    ref = _owner_ref("msg-retry-cancelled")
    root = _root_task(old_id, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    _bind_root(qenv.drive, old_id, pid, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_CANCELLED, result="owner stopped it")

    requeued, _attempt, reason, suppression = _retry(qenv, root, old_id, new_id)

    assert requeued is False
    assert reason == "terminal_result_retry_suppressed"
    assert suppression["kind"] == "terminal_result"
    assert qenv.q.PENDING == []
    assert project_binding_for_task(qenv.drive, new_id) is None
    assert new_id not in all_task_project_bindings(qenv.drive)
    # The predecessor keeps its own room; only the successor is absent.
    assert project_id_for_task(qenv.drive, old_id) == pid


def test_cancellation_winning_the_admission_leaves_no_retry_binding(qenv, monkeypatch):
    """(c) An intent recorded before the admission boundary suppresses the successor
    inside the same locked transition, so the immutable bind never lands."""
    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id, pid = "race-old", "race-new", "race-room"
    ref = _owner_ref("msg-retry-race")
    root = _root_task(old_id, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    _bind_root(qenv.drive, old_id, pid, ref=ref)
    ci.request_cancel(qenv.drive, old_id, reason="stop before retry")

    requeued, _attempt, reason, suppression = _retry(qenv, root, old_id, new_id)

    assert requeued is False
    assert reason == "cancel_pending_retry_suppressed"
    assert suppression == {"kind": "cancel_intent", "target": old_id}
    assert qenv.q.PENDING == []
    assert project_binding_for_task(qenv.drive, new_id) is None


def test_first_implicit_promote_from_a_retry_lands_in_the_origins_project(qenv, monkeypatch):
    """(d) What the owner actually sees next: work the retry promotes with no
    explicit target goes to the room, not to Main."""
    from ouroboros.tools.control_routing import _inherited_project_scope

    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id, pid = "promote-old", "promote-new", "promote-room"
    ref = _owner_ref("msg-retry-promote")
    root = _root_task(old_id, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    _bind_root(qenv.drive, old_id, pid, ref=ref)
    monkeypatch.setattr("ouroboros.config.DATA_DIR", qenv.drive)

    requeued, _attempt, _reason, _suppression = _retry(qenv, root, old_id, new_id)
    assert requeued is True

    # The retry worker's own scope copy is empty (a post-hoc conversion never
    # reached the dead attempt), so only the durable binding can answer.
    ctx = types.SimpleNamespace(task_id=new_id, task_metadata={}, project_id="")
    assert _inherited_project_scope(ctx) == pid


def test_a_room_that_stopped_accepting_bindings_refuses_before_retry_enqueue(qenv, monkeypatch):
    """Recorded room authority must be validated before any successor is runnable."""
    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id, pid = "fenced-old", "fenced-new", "fenced-room"
    ref = _owner_ref("msg-retry-fenced")
    root = _root_task(old_id, ref=ref)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    _bind_root(qenv.drive, old_id, pid, ref=ref)
    begin_project_deletion(qenv.drive, pid)

    requeued, _attempt, _reason, suppression = _retry(qenv, root, old_id, new_id)

    assert (requeued, suppression) == (False, {})
    assert _reason == "idle_timeout_retry_admission_blocked"
    assert not qenv.q.PENDING
    assert project_binding_for_task(qenv.drive, new_id) is None
    from ouroboros.task_results import load_task_result
    result = load_task_result(qenv.drive, old_id)
    assert result["status"] == "failed" and "project_routing_fence" in result["result"]
    assert load_task_result(qenv.drive, new_id) is None


@pytest.mark.serial
def test_prepared_timeout_retry_admits_benign_churn_but_preserves_deletion(qenv, monkeypatch):
    from ouroboros import projects_registry as registry
    from ouroboros.task_results import load_task_result

    _patch_retry_input_handoff(monkeypatch)
    for name in ("ADMISSION_RESERVATIONS", "ACCEPTANCE_FENCES", "BUDGET_ROOT_FENCES"):
        monkeypatch.setattr(qenv.q, name, {})
    monkeypatch.setattr(qenv.q, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    for deleted in (False, True):
        qenv.q.PENDING.clear()
        pid = "deleted" if deleted else "active"
        registry.create_project(qenv.drive, pid)
        registry.create_project(qenv.drive, "other")
        old_id, new_id = f"{pid}-old", f"{pid}-new"
        prepared = qenv.q.enqueue_task({**_root_task(old_id), "project_id": pid,
                                       "workspace_root": "/synthetic/frozen-resource"})
        qenv.q.PENDING.clear()  # simulate the already-executed attempt before reaping
        registry.bind_task_to_project(qenv.drive, old_id, pid, origin={"absent": "system"})
        write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
        real_enqueue = qenv.q.enqueue_task
        def enqueue(payload, **kwargs):
            registry.touch_project(qenv.drive, "other")
            if deleted:
                registry.begin_project_deletion(qenv.drive, pid)
            return real_enqueue(payload, **kwargs)
        with monkeypatch.context() as change:
            change.setattr(qenv.q, "enqueue_task", enqueue)
            requeued, attempt, reason, _ = _retry(qenv, prepared, old_id, new_id)
        assert attempt == (1 if deleted else 2)
        if deleted:
            assert not requeued and not qenv.q.PENDING
            assert reason.endswith("retry_admission_blocked")
            assert load_task_result(qenv.drive, old_id)["status"] == "failed"
            assert project_binding_for_task(qenv.drive, new_id) is None
        else:
            assert requeued and [row["id"] for row in qenv.q.PENDING] == [new_id]
            successor = qenv.q.PENDING[0]
            assert successor["_project_admission"] == prepared["_project_admission"]
            assert successor["workspace_root"] == "/synthetic/frozen-resource"
            assert load_task_result(qenv.drive, old_id)["status"] == "interrupted"
            assert project_id_for_task(qenv.drive, new_id) == pid


@pytest.mark.parametrize("evidence", ["own", "root", "origin"])
@pytest.mark.parametrize("failure", ["missing", "unreadable"])
def test_legacy_retry_rejects_lost_known_room_before_enqueue(qenv, monkeypatch, evidence, failure):
    from ouroboros import projects_registry as registry
    from ouroboros.task_results import load_task_result

    _patch_retry_input_handoff(monkeypatch)
    old_id, new_id = "legacy-old", "legacy-new"
    ref = _owner_ref("legacy-origin")
    root = _root_task(old_id, ref=ref)
    identity = old_id if evidence == "own" else "earlier"
    if evidence == "root":
        root["root_task_id"] = identity
    _bind_root(qenv.drive, identity, "known", ref=ref if evidence == "origin" else None)
    write_task_result(qenv.drive, old_id, STATUS_RUNNING, result="working")
    path = registry._registry_path(qenv.drive)
    path.write_text('{torn' if failure == "unreadable" else '{"projects": []}', encoding="utf-8")
    requeued, _attempt, reason, suppression = _retry(qenv, root, old_id, new_id)
    assert not requeued and not suppression and reason == "idle_timeout_retry_admission_blocked"
    assert not qenv.q.PENDING and project_binding_for_task(qenv.drive, new_id) is None
    assert load_task_result(qenv.drive, old_id)["status"] == "failed"


def test_unregistered_scope_retry_does_not_create_or_bind_a_room(qenv, monkeypatch):
    from ouroboros import projects_registry as registry

    _patch_retry_input_handoff(monkeypatch)
    root = {**_root_task("scope-old"), "project_id": "scope-only", "workspace_root": "/synthetic/frozen"}
    write_task_result(qenv.drive, "scope-old", STATUS_RUNNING, result="working")
    requeued, _attempt, _reason, suppression = _retry(qenv, root, "scope-old", "scope-new")
    assert requeued and not suppression
    [queued] = qenv.q.PENDING
    assert queued["project_id"] == "scope-only" and queued["workspace_root"] == root["workspace_root"]
    assert queued["_project_admission"]["project"] is None
    assert registry.get_reserved_project(qenv.drive, "scope-only") is None
    assert project_binding_for_task(qenv.drive, "scope-new") is None
    assert not any(row.get("type") == "project_binding_failed" for row in _events(qenv.drive))
