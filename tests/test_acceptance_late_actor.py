"""Late criticism follows the actor's ended drain, even before canonical copyback."""

import json
import queue
from types import SimpleNamespace

import pytest

from ouroboros import acceptance_settlement as settlement, review_operation
from ouroboros.headless import prepare_task_drive
from ouroboros.review_projection import publish_acceptance_checkpoint
from ouroboros.task_results import load_task_result, write_task_result
from supervisor.terminal_delivery import pending_deliveries, register_delivery
from tests.test_review_operation_collection import TASK, _HeldModel, _ctx, _released_panel, _settle


@pytest.mark.parametrize("actor_status", ["completed", "running", None])
def test_feedback_uses_actor_completion_before_copyback(tmp_path, monkeypatch, actor_status):
    child = prepare_task_drive(tmp_path, TASK, "empty")
    write_task_result(tmp_path, TASK, "running", chat_id=3, child_drive_root=str(child))
    if actor_status:
        write_task_result(child, TASK, actor_status)
    ctx = _ctx(tmp_path)
    ctx.drive_root = child
    routes = []
    monkeypatch.setattr(settlement, "attach_late_acceptance_settlement",
                        lambda *a, **kw: routes.append(("late", kw["result"]["status"])))
    monkeypatch.setattr("ouroboros.owner_mailbox.write_task_message",
                        lambda *a, **kw: routes.append(("mail", str(a[0]))))
    settlement.announce_acceptance_settlement(ctx, SimpleNamespace(task_id=TASK, retry_key="wave"),
                                             {"slots": {"a": "PASS"}, "total": 1})
    assert routes == ([("late", "running")] if actor_status == "completed" else [("mail", str(child))])


def _legacy_panel(tmp_path, monkeypatch, *, actor_status="completed"):
    """Real pending source and completed producer CAS, with the old pointerless task shape."""
    child = prepare_task_drive(tmp_path, TASK, "empty")
    write_task_result(tmp_path, TASK, "running", chat_id=3, child_drive_root=str(child),
                      result="authored partial answer", accounted_upper_bound_usd=12.5)
    write_task_result(child, TASK, actor_status)
    ctx, model = _ctx(tmp_path), _HeldModel()
    with monkeypatch.context() as quiet:
        quiet.setattr(settlement, "announce_acceptance_settlement", lambda *a, **kw: None)
        try:
            run = _released_panel(tmp_path, ctx, model, retry_key="legacy-wave")
            publish_acceptance_checkpoint(ctx, {"review_runs": [run]}, task_id=TASK)
        finally:
            model.release.set()
        _settle(model, run)
    # These old published panels carry their full request/roster, but no controller pointer.
    path = tmp_path / "task_results" / f"{TASK}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row.pop("review_operations", None)
    path.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request",
                        lambda *a, **kw: pytest.fail("catch-up dispatched a new review"))
    monkeypatch.setattr("ouroboros.review_substrate.ReviewCoordinator.run",
                        lambda *a, **kw: pytest.fail("catch-up started a coordinator"))
    monkeypatch.setattr("ouroboros.claudexor_daemon.ensure_owned_gateway",
                        lambda *a, **kw: pytest.fail("catch-up started a daemon"))
    sink = queue.Queue()
    monkeypatch.setattr("supervisor.workers.get_event_q", lambda: sink)
    return child, model, sink


def test_legacy_completed_actor_collects_once_without_finishing_canonical_task(tmp_path, monkeypatch):
    _child, model, sink = _legacy_panel(tmp_path, monkeypatch)
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert len(report["settled"]) == 1 and not report["errors"], report
    stored = load_task_result(tmp_path, TASK)
    assert stored["status"] == "running" and stored["result"] == "authored partial answer"
    assert stored["accounted_upper_bound_usd"] == 12.5 and "review_operations" not in stored
    panel = stored["review_projection"]["panels"][0]
    assert panel["aggregate_signal"] == "PASS" and panel["late_settlement"]
    events = [e for e in list(sink.queue) if e.get("system_type") == "acceptance_late_settlement"]
    assert [e["delivery_id"] for e in events] == ["acceptance-late:legacy-wave"]
    assert len(pending_deliveries(tmp_path)) == 1
    assert review_operation.recover_orphaned_acceptance_operations(tmp_path)["settled"] == []
    assert register_delivery(tmp_path, "acceptance-late:legacy-wave")
    assert review_operation.recover_orphaned_acceptance_operations(tmp_path)["settled"] == []
    assert model.calls == 1 and len(sink.queue) == 1


@pytest.mark.parametrize("actor_state", ["running", "missing", "unreadable"])
def test_legacy_catchup_does_not_treat_live_or_unknown_actor_as_ended(tmp_path, monkeypatch, actor_state):
    child, model, sink = _legacy_panel(tmp_path, monkeypatch, actor_status="running")
    path = child / "task_results" / f"{TASK}.json"
    if actor_state == "missing":
        path.unlink()
    elif actor_state == "unreadable":
        path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(settlement, "settle_acceptance_operation",
                        lambda *a, **kw: pytest.fail("unknown/live actor was collected"))
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert not report["settled"] and sink.empty() and model.calls == 1


def test_legacy_late_publication_recovers_failed_notice_after_losing_memory(tmp_path, monkeypatch):
    _child, model, sink = _legacy_panel(tmp_path, monkeypatch)
    with monkeypatch.context() as failing:
        failing.setattr("supervisor.terminal_delivery.enqueue_terminal_delivery_outcome",
                        lambda *a, **kw: "unavailable")
        report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["pending"][0]["status"] == "unpublished", report
    before = load_task_result(tmp_path, TASK)["review_projection"]["panels"][0]
    assert before["late_settlement"] and sink.empty()
    with settlement._LATE_LOCK:
        settlement._LATE_UNPUBLISHED.clear()
    monkeypatch.setattr(settlement, "settle_acceptance_operation",
                        lambda *a, **kw: pytest.fail("a saved notice re-collected or re-published criticism"))
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["settled"][0]["status"] == "announced", report
    assert load_task_result(tmp_path, TASK)["review_projection"]["panels"][0] == before
    assert len(pending_deliveries(tmp_path)) == 1 and sink.qsize() == 1 and model.calls == 1


def test_legacy_missing_applied_source_stays_unresolved_without_dispatch(tmp_path, monkeypatch):
    _child, model, sink = _legacy_panel(tmp_path, monkeypatch)
    from ouroboros.artifacts import task_artifact_dir_path

    row = load_task_result(tmp_path, TASK)
    ref = row["review_projection"]["panels"][0]["applied_source_ref"]
    (task_artifact_dir_path(tmp_path, TASK) / ref["path"]).unlink()
    before = (tmp_path / "task_results" / f"{TASK}.json").read_bytes()
    report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert report["errors"] and not report["settled"] and sink.empty()
    assert (tmp_path / "task_results" / f"{TASK}.json").read_bytes() == before
    assert model.calls == 1


@pytest.mark.parametrize("receipt_write_fails", [False, True])
def test_legacy_notice_does_not_reappear_after_outbox_eviction(tmp_path, monkeypatch, receipt_write_fails):
    _child, model, sink = _legacy_panel(tmp_path, monkeypatch)
    monkeypatch.setattr("supervisor.terminal_delivery._REGISTRY_CAP", 1)
    remember = review_operation._remember_legacy_notice
    def fail_handoff(*args, **kwargs):
        if kwargs.get("custody") == "publication":
            return remember(*args, **kwargs)
        raise OSError("receipt write failed")
    with monkeypatch.context() as first:
        if receipt_write_fails:
            first.setattr(review_operation, "_remember_legacy_notice", fail_handoff)
        report = review_operation.recover_orphaned_acceptance_operations(tmp_path)
    assert bool(report["errors"]) is receipt_write_fails
    # A retry hands no second event to the live queue: the outbox already owns this one.
    review_operation.recover_orphaned_acceptance_operations(tmp_path)
    stored = load_task_result(tmp_path, TASK)
    receipt = stored["review_projection"]["late_notice_receipts"]["acceptance-late:legacy-wave"]
    assert receipt["custody"] == "terminal_outbox"
    assert register_delivery(tmp_path, "acceptance-late:legacy-wave")
    assert register_delivery(tmp_path, "other-newer-delivery")
    from supervisor.terminal_delivery import already_delivered

    assert not already_delivered(tmp_path, "acceptance-late:legacy-wave"), "the bounded registry really evicted it"
    before = (tmp_path / "task_results" / f"{TASK}.json").read_bytes()
    assert review_operation.recover_orphaned_acceptance_operations(tmp_path)["settled"] == []
    assert sink.qsize() == 1 and model.calls == 1 and not pending_deliveries(tmp_path)
    assert (tmp_path / "task_results" / f"{TASK}.json").read_bytes() == before


def test_notice_receipt_does_not_overwrite_a_concurrently_replaced_panel(tmp_path, monkeypatch):
    _child, _model, _sink = _legacy_panel(tmp_path, monkeypatch)
    review_operation.recover_orphaned_acceptance_operations(tmp_path)
    row = load_task_result(tmp_path, TASK)
    panel = row["review_projection"]["panels"][0]
    stale = {**panel, "publication_revision": panel["publication_revision"] - 1}
    before = (tmp_path / "task_results" / f"{TASK}.json").read_bytes()
    review_operation._remember_legacy_notice(tmp_path, TASK, "stale-delivery", stale)
    assert (tmp_path / "task_results" / f"{TASK}.json").read_bytes() == before


def test_already_settled_legacy_panel_does_not_acquire_a_new_notice_on_upgrade(tmp_path, monkeypatch):
    _child, model, sink = _legacy_panel(tmp_path, monkeypatch)
    review_operation.recover_orphaned_acceptance_operations(tmp_path)
    path = tmp_path / "task_results" / f"{TASK}.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["review_projection"].pop("late_notice_receipts")  # old completed supplement, no new catch-up duty
    path.write_text(json.dumps(row), encoding="utf-8")
    monkeypatch.setattr("supervisor.terminal_delivery._REGISTRY_CAP", 1)
    assert register_delivery(tmp_path, "acceptance-late:legacy-wave")
    assert register_delivery(tmp_path, "newer-delivery")
    before = path.read_bytes()
    assert not review_operation.recover_orphaned_acceptance_operations(tmp_path)["settled"]
    assert path.read_bytes() == before and sink.qsize() == 1 and model.calls == 1


@pytest.mark.parametrize("legacy_order", [False, True])
def test_stale_review_projection_cannot_erase_or_regress_notice_handoffs(tmp_path, legacy_order):
    from copy import deepcopy

    delivery_id = "acceptance-late:old-wave"
    receipt = {"panel_id": "panel-old", "publication_revision": 2,
               "settled_at": "2026-09-27T10:00:00Z", "custody": "terminal_outbox"}
    projection = {"panels": [{"panel_id": "panel-old", "surface": "task_acceptance"}],
                  "late_notice_receipts": {delivery_id: receipt}}
    if not legacy_order:
        projection.update(publication_revision=2, task_attempt=1)
        projection["panels"][0].update(publication_revision=2, task_attempt=1)
    write_task_result(tmp_path, TASK, "completed", review_projection=projection, result="original answer")
    stale = deepcopy(projection)
    stale["late_notice_receipts"][delivery_id].update(custody="publication", publication_revision=1, settled_at=None)
    stale["late_notice_receipts"]["acceptance-late:other-wave"] = {
        "panel_id": "panel-other", "publication_revision": 1, "custody": "publication", "settled_at": None}
    stored = write_task_result(tmp_path, TASK, "completed", review_projection=stale)
    assert stored["review_projection"]["late_notice_receipts"][delivery_id] == receipt
    assert "acceptance-late:other-wave" in stored["review_projection"]["late_notice_receipts"]
    assert stored["result"] == "original answer"
