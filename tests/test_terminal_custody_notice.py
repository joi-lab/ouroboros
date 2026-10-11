"""Final delivery and continuation show current custody beside unchanged prose."""

import copy

from ouroboros import delegate_custody as custody, delegate_terminal
from ouroboros.agent_startup_checks import task_result_authority_projection
from ouroboros.outcomes import public_task_result
from ouroboros.task_finalization import prepare_terminal_send_event, terminal_host_notice_text
from ouroboros.task_results import load_task_result, write_task_result
from ouroboros.tools.join_ledger import _child_result_sha256


def _cancelled_with_patch(tmp_path):
    text = "The last observed delegated run was still working; its outcome is unconfirmed."
    write_task_result(tmp_path, "root", "completed", result=text,
                      reason_code="budget_exhausted", delegated_runs_unreconciled=["run-one"],
                      continuation_narrative={"text": text})
    assert custody.emit(tmp_path, custody.STARTED, {
        "run_id": "run-one", "task_id": "root", "route": "fixture",
        "model": "fixture-model", "profile_id": "fixture-profile",
        "selected_subagent_id": "fixture-actor",
        "snapshot_id": "snapshot-one", "shape": {},
    })
    assert custody.emit(tmp_path, custody.SETTLED, {
        "run_id": "run-one", "task_id": "root", "route": "fixture", "state": "cancelled",
        "cost_usd": 0.0, "cost_final": True, "spend_disclosed": True,
    })
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "root")
    return text, load_task_result(tmp_path, "root")


def test_cleanup_receipt_is_carried_before_final_delivery_without_rewriting_answer(tmp_path):
    text, stored = _cancelled_with_patch(tmp_path)
    audit = stored["delegate_terminal_reconciliation"]
    assert audit["open_run_ids"] == audit["pending_invocation_ids"] == []
    assert audit["undisposed_patch_run_ids"] == ["run-one"]
    assert audit["terminal_runs"] == [{
        "run_id": "run-one", "state": "cancelled", "model": "fixture-model",
        "profile_id": "fixture-profile", "selected_subagent_id": "fixture-actor",
    }]
    before = copy.deepcopy(stored)
    usage = {"terminal_origin": "model_final", "terminal_host_notice": "Budget stop retained."}
    event = prepare_terminal_send_event(
        tmp_path, {"id": "root", "chat_id": 1}, text, usage,
        {"type": "send_message", "task_id": "root", "chat_id": 1, "text": text},
        presence=False,
    )
    assert event["text"] == text
    # One voice: the base host notice stays a field of the RESULT and never rides
    # the send event; custody travels on its own field (#1006). Single-body
    # transports still read the stored join below.
    assert "terminal_host_notice" not in event
    # The leaf's own model rides the replayed row, so the nanny's terminal is
    # not read as a verdict about the role the host played (I9).
    assert "run-one: cancelled on fixture-model" in event["terminal_custody_notice"]
    assert "Pending patch decisions: run-one" in event["terminal_custody_notice"]
    joined = public_task_result({**stored, **usage})["terminal_host_notice"]
    assert joined.startswith("Budget stop retained.")
    assert event["terminal_custody_notice"] in joined
    assert load_task_result(tmp_path, "root") == before


def test_late_settlement_and_disposition_refresh_inspection_and_continuation_only(tmp_path):
    text, stored = _cancelled_with_patch(tmp_path)
    first_hash = _child_result_sha256(stored)
    public = public_task_result(stored)
    authority = task_result_authority_projection(stored, drive_root=tmp_path)
    assert "run-one: cancelled" in public["terminal_host_notice"]
    assert authority["terminal_host_notice"] == public["terminal_host_notice"]
    assert authority["result"] == public["result"] == text
    assert authority["continuation_narrative"]["text"] == text
    assert public["reason_code"] == "budget_exhausted"
    assert custody.emit(tmp_path, custody.PATCH_DISPOSED, {
        "run_id": "run-one", "task_id": "root", "disposition": "rejected",
    })
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "root")
    refreshed = load_task_result(tmp_path, "root")
    notice = terminal_host_notice_text(refreshed)
    assert "run-one: cancelled" in notice
    assert "Pending patch decisions: none recorded" in notice
    assert refreshed["result"] == text
    assert refreshed["continuation_narrative"]["text"] == text
    assert refreshed["reason_code"] == "budget_exhausted"
    assert _child_result_sha256(refreshed) != first_hash
    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "root") is False


def test_absent_execution_is_not_a_cancelled_receipt_and_unreadable_audit_stays_unknown():
    row = {"delegate_terminal_reconciliation": {
        "audit_status": "ok", "open_run_ids": [], "pending_invocation_ids": [],
        "undisposed_patch_run_ids": ["run-unknown"], "terminal_runs": [],
    }}
    notice = terminal_host_notice_text(row)
    assert "cancelled" not in notice
    assert "Pending patch decisions: run-unknown" in notice
    row["delegate_terminal_reconciliation"]["audit_status"] = "failed"
    notice = terminal_host_notice_text(row)
    assert "could not be verified" in notice
    assert "none recorded" not in notice


def test_custody_notice_is_bounded_and_does_not_duplicate_on_public_projection():
    row = {"task_id": "root", "result": "answer", "terminal_host_notice": "Other host fact.",
           "delegate_terminal_reconciliation": {
               "audit_status": "ok", "open_run_ids": [], "pending_invocation_ids": [],
               "undisposed_patch_run_ids": [f"run-{i}" for i in range(12)],
               "terminal_runs": [{"run_id": f"run-{i}", "state": "cancelled",
                                  "model": "leaf-model"} for i in range(12)],
           }}
    public = public_task_result(row)
    assert "+2 more in task details" in public["terminal_host_notice"]
    assert "run-11: cancelled" not in public["terminal_host_notice"]
    assert public_task_result(public)["terminal_host_notice"] == public["terminal_host_notice"]
    assert row["terminal_host_notice"] == "Other host fact."


def test_successful_settled_runs_add_no_unrelated_terminal_notice():
    row = {"delegate_terminal_reconciliation": {
        "audit_status": "ok", "terminal_runs": [{"run_id": "run-ok", "state": "succeeded"}],
        "open_run_ids": [], "pending_invocation_ids": [], "undisposed_patch_run_ids": [],
    }}
    assert terminal_host_notice_text(row) == ""


def test_a_tasks_own_reviewers_never_become_its_unreconciled_custody(tmp_path):
    """Issue #1006: a task whose only open rows are its acceptance-review slots
    audits CLEAN. Its terminal was «Done with warnings» over a run the review
    panel owns, so the audit must not name it, and the notice stays empty."""
    write_task_result(tmp_path, "root", "completed", result="verdict",
                      delegated_runs_unreconciled=["run-review"])
    assert custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-review", task_id="root", route_id="codex", model="review-model",
        source="review_substrate", category="task_acceptance_review",
        review_slot_id="triad_286lhb"))
    assert custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-review-done", task_id="root", route_id="codex",
        source="review_substrate:task_acceptance"))
    assert custody.emit(tmp_path, custody.SETTLED, {
        "run_id": "run-review-done", "task_id": "root", "route": "codex",
        "state": "failed", "source": "review_substrate"})
    assert custody.record_start_requested(
        tmp_path, run_id="", task_id="root", invocation_id="inv-review",
        idempotency_key="inv-review", request={"prompt": "packet"},
        route="codex", source="review_substrate.extraction")
    custody._CUSTODY.clear()

    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "root")
    audit = load_task_result(tmp_path, "root")["delegate_terminal_reconciliation"]
    assert audit["audit_status"] == "ok"
    assert audit["open_run_ids"] == []
    assert audit["pending_invocation_ids"] == []
    assert audit["terminal_runs"] == []
    assert audit["unreconciled"] == []
    assert terminal_host_notice_text(load_task_result(tmp_path, "root")) == ""
    custody._CUSTODY.clear()


def test_the_tasks_own_open_run_still_shows_beside_its_reviewers(tmp_path):
    """The regression pin for the exclusion above: only the REVIEW rows leave."""
    write_task_result(tmp_path, "root", "completed", result="verdict",
                      delegated_runs_unreconciled=["stale"])
    assert custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-review", task_id="root", route_id="codex",
        source="review_substrate"))
    assert custody.record_started(tmp_path, custody.RunCustody(
        run_id="run-leaf", task_id="root", route_id="codex", model="leaf-model"))
    custody._CUSTODY.clear()

    assert delegate_terminal.refresh_terminal_reconciliation(tmp_path, "root")
    audit = load_task_result(tmp_path, "root")["delegate_terminal_reconciliation"]
    assert audit["open_run_ids"] == audit["unreconciled"] == ["run-leaf"]
    assert "Open delegated execution: run-leaf" in terminal_host_notice_text(
        load_task_result(tmp_path, "root"))
    custody._CUSTODY.clear()
