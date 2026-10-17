"""A contended historical control read must not become a permanently latched Stop."""
from __future__ import annotations

import queue
import threading

import pytest

from ouroboros import acceptance_history, model_wait, review_operation, store
from ouroboros.task_results import load_task_result
from tests.test_acceptance_late_consumers import late as late, delivered
from tests.test_acceptance_history import _caller, _request, _source
from tests.test_review_operation_collection import fresh_sends as fresh_sends
from tests.test_review_operation_lifetime import until
from tests.test_admission_contention import database_reader

pytestmark = pytest.mark.serial


def test_real_historical_panel_waits_through_nested_control_contention(late, tmp_path, monkeypatch):
    f = delivered(tmp_path, monkeypatch)
    ctx = _caller(f)
    ctx.event_queue = queue.Queue()
    original = acceptance_history.historical_review_controls
    once, lock = [], threading.Lock()

    def contend(*args, **kwargs):
        with lock:
            first = model_wait.control_probe_requires_current() and not once
            if first:
                once.append(True)
        if first:
            # The outer subject read already succeeded. The real lower
            # historical control owner now meets an independently held store.
            with database_reader(f.root, exclusive=True):
                return original(*args, **kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(acceptance_history, "historical_review_controls", contend)
    result = _request(f, ctx, _source(ctx, text="Review this delivered historical answer."))
    assert result["status"] in {"pending", "announced", "published", "settled"}
    until(lambda: any(p.get("late_settlement") for p in
                     (load_task_result(f.root, f.tid).get("review_projection") or {}).get("panels", [])))
    until(lambda: not review_operation._LIVE)
    assert once == [True] and len(late.calls) == 3
    row = load_task_result(f.root, f.tid)
    assert len(row["review_operations"]) == 1
    assert len(row["task_acceptance_review_accounting"]["claims_by_binding"]) == 1
    panel = next(p for p in row["review_projection"]["panels"] if p.get("late_settlement"))
    assert panel["coverage"]["transport_success"] == 3


def test_actual_stop_still_refuses_historical_review_after_a_busy_probe(late, tmp_path, monkeypatch):
    from ouroboros import cancel_intents
    from types import SimpleNamespace

    f = delivered(tmp_path, monkeypatch)
    row = load_task_result(f.root, f.tid)
    debt = row["acceptance_debt"]
    purpose = {"schema_version": 1, "task_id": f.tid, "debt_id": debt["debt_id"],
               "source_ref": debt["source_ref"], "delivery": debt["delivery"],
               "accounting_root_task_id": debt["accounting_root_task_id"], "automatic": False}
    parent = model_wait.TaskModelWait(task={"id": f.tid}, drive_root=f.root, event_queue=None,
                                     worker_slot_held=False)
    op = review_operation.ReviewOperation(parent=parent, request=SimpleNamespace(
        surface="task_acceptance", retry_key="same", policy={"historical_acceptance": purpose}),
        task_id=f.tid, result_root=f.root)
    with database_reader(f.root, exclusive=True):
        with pytest.raises(store.StoreBusy):
            op.wait.control_reason(strict_current=True)
    assert not op._control
    cancel_intents.request_cancel(f.root, f.tid, reason="owner_stop", allow_settled_target=True)
    assert op.wait.control_reason(strict_current=True) == "cancelled"
    assert not late.calls


@pytest.mark.parametrize("error", [store.StoreBusy("permission", reason="uninspectable"),
                                  store.StoreLost("corrupt"), PermissionError("denied")])
def test_historical_control_other_failures_keep_the_existing_refusal(tmp_path, monkeypatch, error):
    from ouroboros import task_results

    monkeypatch.setattr(task_results, "load_task_result", lambda *_a, **_kw: (_ for _ in ()).throw(error))
    row = {"task_id": "root", "status": "completed"}
    debt = {"accounting_root_task_id": "root"}
    blocked = []
    def control():
        blocked.extend(acceptance_history.historical_review_controls(
            tmp_path, row, debt, caller_task_id="root", automatic=False))
        return "cancelled" if blocked else None
    owner = model_wait.TaskModelWait(task={"id": "root"}, drive_root=tmp_path, event_queue=None,
                                    worker_slot_held=False, owner_control=control)
    assert owner.control_reason(strict_current=True) == "cancelled"
    assert blocked == ["control_authority_unavailable"]
