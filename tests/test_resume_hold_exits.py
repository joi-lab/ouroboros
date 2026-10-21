"""Every queue budget hold has a named exit and keeps its locator on disk."""
import ast
import copy
import importlib
from pathlib import Path

import pytest

from ouroboros.queue_observation import load_queue_document
from supervisor import events_budget as eb
from tests._budget_pause_exact_helpers import _install_queue


EXITS = {
    "root_fence_lifted_pending_selection": "selection",
    "root_fence_member_pending_selection": "selection",
    "owner_restart_hold": "selection",
    "continuation_writer_unsettled": "supervisor.continuation_admission.release_settled_continuations",
    "panic_hold": "selection", "saved_work_hold": "selection", "unsaved_pause_intent": "selection",
    "restore_refused:": "exact_grant",
    "restore_refused:invalid_acceptance_fence_snapshot": "exact_grant",
    "restore_refused:invalid_budget_fence_snapshot": "exact_grant",
    "root_acceptance_fenced_at_restore": "exact_grant",
    "resume_grant_revocation_unwritten": "exact_grant",
    "pause_record_unreadable_at_revoke": "exact_grant",
    "restart_revocation_unwritten": "exact_grant",
    "malformed_resume_identity": "cancel",
    "stale_queue_row_grant_consumed": "supervisor.budget_resume.revalidate_consumed_resume_holds",
    "stale_resume_grant_superseded": "supervisor.budget_resume.revalidate_consumed_resume_holds",
    "saved_sleep_recovery": "cancel",
    "dispatch_outcome_unknown": "cancel",
}


def test_all_queue_hold_writers_have_snapshot_surviving_exits(tmp_path, monkeypatch):
    q, _state, workers = _install_queue(tmp_path, monkeypatch)
    reasons = {value for name, value in vars(eb).items() if name.startswith("HOLD_") and isinstance(value, str)}
    # A new literal writer or constant cannot quietly evade the table.
    for path in (Path(__file__).resolve().parents[1] / "supervisor").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("HOLD_") for t in node.targets):
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    reasons.add(node.value.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                    "hold_budget_row", "hold_restored_budget_pause"}:
                for kw in node.keywords:
                    if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
                        reasons.add(kw.value.value)
    assert reasons == EXITS.keys()
    for index, reason in enumerate(sorted(reasons)):
        task = {"id": f"exit-{index}", "type": "task", "_attempt": 2}
        if EXITS[reason] == "exact_grant":
            task["_budget_pause"] = {"exact_continuation": True, "checkpoint": {"pause_id": "p"}}
        eb.hold_budget_row(task, reason=reason, extra={"pause_id": "p", "grant_id": "g"})
        workers.PENDING.append(task)
    assert q.persist_queue_snapshot(reason="hold_exit_contract")
    rows = [row["task"] for row in load_queue_document(tmp_path)["pending"]]
    assert len(rows) == len(reasons)
    for task in rows:
        hold = eb.budget_hold_fact(task)
        assert hold["pause_id"] == "p" and hold["grant_id"] == "g"
        exit_kind = EXITS[hold["reason"]]
        if exit_kind == "selection":
            assert eb.hold_selectable(task, hold)
        elif exit_kind == "exact_grant":
            assert task["_budget_pause"]["checkpoint"]["pause_id"] == "p"
        elif exit_kind == "cancel":
            from ouroboros.cancel_intents import request_cancel, has_active_intent
            request_cancel(tmp_path, task["id"], reason="explicit test stop", source="test")
            assert has_active_intent(tmp_path, task["id"])
        else:
            module, name = exit_kind.rsplit(".", 1)
            assert callable(getattr(importlib.import_module(module), name))
        if hold["reason"] in {eb.HOLD_GRANT_CONSUMED_STALE_ROW, eb.HOLD_STALE_GRANT_SUPERSEDED}:
            assert not eb.hold_selectable(task, hold)


def test_tick_redecides_spent_hold_without_reissuing_resume(tmp_path, monkeypatch):
    from supervisor.budget_resume import revalidate_consumed_resume_holds
    from ouroboros.task_results import write_task_result
    from ouroboros.cancel_intents import has_active_intent

    q, _state, workers = _install_queue(tmp_path, monkeypatch)
    for tid, basis in (("continuing", True), ("duplicate", False)):
        write_task_result(tmp_path, tid, "running", task_attempt=1)
        task = {"id": tid, "_attempt": 2}
        if basis:
            task["_working_recovery"] = {"source_task_id": tid, "from_attempt": 1, "cause": "update_aborted"}
        eb.hold_budget_row(task, reason=eb.HOLD_GRANT_CONSUMED_STALE_ROW, extra={"pause_id": "p", "grant_id": "g"})
        workers.PENDING.append(task)
    revalidate_consumed_resume_holds()
    assert eb.budget_hold_fact(workers.PENDING[0]) is None
    assert eb.budget_hold_fact(workers.PENDING[1])
    assert not has_active_intent(tmp_path, "continuing") and has_active_intent(tmp_path, "duplicate")
    assert all("_budget_pause_resume" not in task for task in workers.PENDING)
    assert load_queue_document(tmp_path)["pending"][0]["task"][eb.BUDGET_HOLD_KEY] is None


@pytest.mark.parametrize("populated", [False, True])
def test_tick_without_spent_holds_does_no_store_work(tmp_path, monkeypatch, populated):
    from ouroboros import budget_pause, delegate_recovery, store
    from supervisor.budget_resume import revalidate_consumed_resume_holds

    q, _state, workers = _install_queue(tmp_path, monkeypatch)
    if populated:
        workers.PENDING.extend([
            {"id": "ordinary", "_attempt": 2},
            {"id": "other-hold", eb.BUDGET_HOLD_KEY: {"reason": eb.HOLD_OWNER_RESTART}},
            {"id": "exact-pause", "_budget_pause": {"exact_continuation": True},
             eb.BUDGET_HOLD_KEY: {"reason": eb.HOLD_STALE_GRANT_SUPERSEDED}},
        ])
    before = copy.deepcopy(workers.PENDING)

    def unexpected(*_args, **_kwargs):
        pytest.fail("a tick without spent holds must not read/write durable state")

    monkeypatch.setattr(store, "open_store", unexpected)
    monkeypatch.setattr(budget_pause, "budget_pause_row", unexpected)
    monkeypatch.setattr(delegate_recovery, "_read", unexpected)
    monkeypatch.setattr(q, "persist_queue_snapshot", unexpected)
    with q._queue_lock:
        revalidate_consumed_resume_holds()
        revalidate_consumed_resume_holds()
    assert workers.PENDING == before


def test_the_assignment_tick_entry_re_decides_spent_holds(tmp_path, monkeypatch):
    """``revalidate_project_holds`` (the assignment pass's hold revalidation) runs the re-decision first,
    so a stranded continuation returns on the next tick even when no Project hold is pending."""
    from ouroboros.task_results import write_task_result
    from supervisor.task_admission import revalidate_project_holds

    q, _state, workers = _install_queue(tmp_path, monkeypatch)
    write_task_result(tmp_path, "continuing", "running", task_attempt=1)
    task = {"id": "continuing", "_attempt": 2,
            "_working_recovery": {"source_task_id": "continuing", "from_attempt": 1, "cause": "update_aborted"}}
    eb.hold_budget_row(task, reason=eb.HOLD_GRANT_CONSUMED_STALE_ROW, extra={"pause_id": "p", "grant_id": "g"})
    workers.PENDING.append(task)
    assert not any(row.get("_project_admission_restore_hold") for row in workers.PENDING)
    with q._queue_lock:
        revalidate_project_holds()
    assert eb.budget_hold_fact(workers.PENDING[0]) is None
