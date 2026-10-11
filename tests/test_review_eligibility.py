"""Effect-gated task-acceptance review eligibility + cognitive recovery + persistence.

Covers v6.6.0 changes: `required` mode reviews only turns with observable
reviewable effects (or non-direct tasks); cognitive-memory updates are not
effects; cognitive/root redirect blocks recover via the correct tool; the
review decision surfaces into loop_outcome; and the draft-final invariant holds.
"""
from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from ouroboros.outcomes import (
    derive_loop_outcome,
    turn_has_reviewable_effects,
    _unresolved_tool_errors,
)
from ouroboros.loop_acceptance import _task_acceptance_eligible


def _call(tool, *, is_error=False, status="ok", root=None):
    args = {}
    if root is not None:
        args["root"] = root
    return {"tool": tool, "is_error": is_error, "status": status, "args": args}


# ----- turn_has_reviewable_effects -----

def test_commit_is_a_reviewable_effect():
    assert turn_has_reviewable_effects({"tool_calls": [_call("commit_reviewed")]})


def test_blocked_or_errored_commit_is_not_an_effect():
    # A commit_reviewed that did NOT commit (REVIEW_BLOCKED -> status "blocked",
    # GIT_ERROR -> status "error") is excluded by the _OK_TOOL_STATUSES gate, so
    # only a successful commit (status "ok") counts.
    assert not turn_has_reviewable_effects({"tool_calls": [_call("commit_reviewed", status="blocked")]})
    assert not turn_has_reviewable_effects({"tool_calls": [_call("commit_reviewed", is_error=True, status="error")]})


def test_review_blocked_commit_end_to_end_not_an_effect():
    # End-to-end: the real REVIEW_BLOCKED / GIT_ERROR commit_reviewed text maps to a
    # non-OK status via _extract_result_metadata, so the effect gate excludes it and
    # a blocked/failed commit never triggers a false required review.
    from ouroboros.loop_tool_execution import _extract_result_metadata, _is_tool_execution_failure

    for txt in (
        "⚠️ REVIEW_BLOCKED (attempt 1): Critical issues found by reviewers.\nCommit has NOT been created.",
        "⚠️ GIT_ERROR (commit): hook rejected the commit.",
    ):
        is_err = _is_tool_execution_failure(True, txt)
        meta = _extract_result_metadata("commit_reviewed", txt, is_err)
        assert meta.get("status") != "ok"
        call = {"tool": "commit_reviewed", "is_error": is_err, **meta}
        assert not turn_has_reviewable_effects({"tool_calls": [call]})


def test_user_files_write_is_a_reviewable_effect():
    assert turn_has_reviewable_effects({"tool_calls": [_call("write_file", root="user_files")]})


def test_artifact_and_repo_writes_are_effects():
    assert turn_has_reviewable_effects({"tool_calls": [_call("write_file", root="artifact_store")]})
    assert turn_has_reviewable_effects({"tool_calls": [_call("edit_text", root="system_repo")]})


def test_scratch_write_is_not_an_effect():
    assert not turn_has_reviewable_effects({"tool_calls": [_call("write_file", root="task_drive")]})


def test_skill_payload_and_runtime_data_writes_are_effects():
    # Exclusion model: any non-scratch successful write is real work. runtime_data
    # in light mode is a skill-payload write (documented skill authoring).
    assert turn_has_reviewable_effects({"tool_calls": [_call("write_file", root="skill_payload")]})
    assert turn_has_reviewable_effects({"tool_calls": [_call("write_file", root="runtime_data")]})


def test_cognitive_updates_are_not_effects():
    assert not turn_has_reviewable_effects({"tool_calls": [
        _call("update_identity"), _call("update_scratchpad"), _call("knowledge_write"),
    ]})


def test_direct_cognitive_turn_does_not_receive_acceptance_selector(monkeypatch, tmp_path):
    """A direct identity update must continue as natural conversation.

    Acceptance observations are internal protocol text. Injecting one after a
    cognitive-only tool call makes Main answer the selector instead of the
    owner's introduction, even though the turn is ineligible for review.
    """
    import queue

    from ouroboros import loop
    from ouroboros.loop_acceptance_review import prepare_acceptance_observation

    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    ctx = SimpleNamespace(
        task_id="direct-cognitive", task_attempt=1, drive_root=tmp_path,
        task_metadata={}, task_contract={}, is_direct_chat=True,
        _loop_mailbox_seen_ids=set(), _owner_directives=[],
    )
    messages = [{"role": "user", "content": "Please remember my name."}]
    trace = {"tool_calls": [_call("update_identity")], "reasoning_notes": []}
    schemas = [{"function": {"name": "task_acceptance_review"}}]

    prepare_acceptance_observation(ctx, trace, queue.Queue(), messages, schemas)

    assert not any(row.get("acceptance_observation") for row in messages)
    assert "ACCEPTANCE_SUBJECT_OBSERVATION" not in str(messages)


def test_errored_write_is_not_an_effect():
    assert not turn_has_reviewable_effects({"tool_calls": [
        _call("write_file", is_error=True, status="light_mode_blocked", root="user_files"),
    ]})


def test_empty_trace_has_no_effects():
    assert not turn_has_reviewable_effects({"tool_calls": []})
    assert not turn_has_reviewable_effects({})


def test_process_outputs_are_reviewable_effects():
    # run_command / run_script that declare deliverable outputs produce artifacts.
    assert turn_has_reviewable_effects({"tool_calls": [
        {"tool": "run_command", "is_error": False, "status": "ok",
         "args": {"cmd": ["python", "build.py"], "outputs": ["Desktop/deck.pptx"]}},
    ]})
    assert turn_has_reviewable_effects({"tool_calls": [
        {"tool": "run_script", "is_error": False, "status": "ok",
         "args": {"outputs": ["report.html"]}},
    ]})


def test_registered_artifact_flag_is_an_effect():
    # e.g. stop_service / user_files write that registered a canonical artifact.
    # Detection uses the structured flag captured from the full result, so it
    # holds even when the ARTIFACT_OUTPUTS marker is past the 700-char trace preview.
    assert turn_has_reviewable_effects({"tool_calls": [
        {"tool": "stop_service", "is_error": False, "status": "ok",
         "args": {}, "artifact_registered": True, "result": "x" * 2000},
    ]})


def test_process_without_outputs_is_not_an_effect():
    assert not turn_has_reviewable_effects({"tool_calls": [
        {"tool": "run_command", "is_error": False, "status": "ok",
         "args": {"cmd": ["ls"]}},
    ]})


def test_cwd_coding_effect_class_is_empty_since_d10():
    # D10 retired the one cwd-based coding tool; the effect CLASS stays wired
    # (an empty set), so a successor lands as one entry, not a new mechanism.
    from ouroboros.outcomes import _EFFECT_CODING_TOOLS

    assert _EFFECT_CODING_TOOLS == frozenset()
    assert not turn_has_reviewable_effects({"tool_calls": [
        {"tool": "claude_code_edit", "is_error": False, "status": "ok", "args": {"cwd": "Desktop"}},
    ]})


def test_start_service_with_outputs_is_an_effect():
    assert turn_has_reviewable_effects({"tool_calls": [
        {"tool": "start_service", "is_error": False, "status": "ok",
         "args": {"cmd": ["serve"], "outputs": ["report.html"]}},
    ]})


# ----- _task_acceptance_eligible (mode, llm_trace, is_direct_chat) -----

def test_off_never_eligible():
    assert _task_acceptance_eligible("off", {"tool_calls": [_call("commit_reviewed")]}, False) == (False, "off")


def test_auto_host_reviews_effectful_work():
    assert _task_acceptance_eligible(
        "auto", {"tool_calls": [_call("commit_reviewed")]}, True,
    ) == (True, "auto_effect")


def test_auto_direct_conversation_is_skipped():
    assert _task_acceptance_eligible("auto", {"tool_calls": []}, True) == (False, "skipped_conversation")


def test_auto_nondirect_conversation_is_skipped():
    assert _task_acceptance_eligible("auto", {"tool_calls": []}, False) == (False, "skipped_conversation")


def test_required_direct_chat_no_effect_is_skipped():
    # "Привет" / "2+7" case: direct chat, zero effects -> no review even in required.
    assert _task_acceptance_eligible("required", {"tool_calls": []}, True) == (False, "skipped_conversation")


def test_direct_declared_deliverable_is_host_reviewed_without_write_effect():
    assert _task_acceptance_eligible(
        "required",
        {"tool_calls": []},
        True,
        task_contract={"expected_output": "A cited research report"},
    ) == (True, "required_contract")


def test_read_or_search_activity_alone_does_not_request_review():
    assert _task_acceptance_eligible(
        "auto", {"tool_calls": [_call("web_search")]}, True,
    ) == (False, "skipped_conversation")
    assert _task_acceptance_eligible(
        "auto", {"tool_calls": [_call("read_file", status="error", is_error=True)]}, True,
    ) == (False, "skipped_conversation")


def test_direct_meta_only_activity_remains_pure_conversation():
    for tool in ("list_available_tools", "enable_tools", "compact_context"):
        assert _task_acceptance_eligible(
            "required", {"tool_calls": [_call(tool)]}, True,
        ) == (False, "skipped_conversation")


def test_addressing_only_direct_turn_does_not_request_acceptance():
    assert _task_acceptance_eligible(
        "required",
        {"tool_calls": [_call("route_to_project")]},
        True,
    ) == (False, "skipped_conversation")


def test_required_with_effect_is_eligible():
    assert _task_acceptance_eligible(
        "required", {"tool_calls": [_call("write_file", root="user_files")]}, True
    ) == (True, "required_effect")


def test_required_nondirect_is_eligible_even_without_effect():
    assert _task_acceptance_eligible("required", {"tool_calls": []}, False) == (True, "required_nondirect")


@pytest.fixture
def host_acceptance(monkeypatch, tmp_path):
    """Run the real host gate and packet builder; replace only the reviewer call."""
    import ouroboros.review_substrate as substrate
    from ouroboros.contracts.task_contract import build_task_contract
    from ouroboros.loop_acceptance_review import _run_task_acceptance_review_once
    from ouroboros.task_results import STATUS_RUNNING, write_task_result
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path, task_id="root")
    ctx.repo_dir.mkdir()
    ctx.task_metadata = {"root_task_id": "root", "delegation_role": "root"}
    ctx.task_contract = build_task_contract({"id": "root", **ctx.task_metadata})
    write_task_result(
        tmp_path, "root", STATUS_RUNNING, task_contract=ctx.task_contract, **ctx.task_metadata,
    )
    requests = []
    monkeypatch.setattr(substrate, "triad_delivery_slots", lambda **_kw: [object()])

    def review(request, **_kwargs):
        requests.append(request)
        # Dispatch is not semantic success: an unavailable reviewer remains degraded.
        return substrate.ReviewRunResult(
            request={"surface": request.surface, "task_id": request.task_id},
            actors=[], parsed_findings=[], aggregate_signal="DEGRADED", degraded=True,
            degraded_reasons=["test reviewer unavailable"],
        )

    monkeypatch.setattr(substrate, "run_review_request", review)

    def run(trace):
        again = _run_task_acceptance_review_once(
            tools=SimpleNamespace(_ctx=ctx), content="The cited research result.",
            task_id="root", task_type="task", llm_trace=trace, drive_root=tmp_path,
            messages=[{"role": "system", "content": ""}, {"role": "user", "content": "Study the sources."}],
            emit_progress=lambda _msg, *, incident=None: None,
        )
        assert again is bool(requests)
        if requests:
            assert trace["acceptance_decision"]["status"] == "revision_requested"
            assert trace["review_runs"][-1]["aggregate_signal"] == "DEGRADED"

    return ctx, run, requests


@pytest.mark.parametrize("direct", [False, True], ids=["queued", "direct"])
@pytest.mark.parametrize("calls,contract,expected", [
    ([], {}, 0),
    ([_call("read_file"), _call("web_search")], {}, 0),
    ([_call("update_scratchpad")], {}, 0),
    ([_call("enable_tools")], {}, 0),
    ([_call("write_file", root="user_files")], {}, 1),
    ([_call("read_file")], {"expected_output": "A cited research report"}, 1),
    ([], {"acceptance_claims": [{"id": "claim_1", "claim": "Compare the sources"}]}, 1),
], ids=["conversation", "exploration", "cognitive", "meta", "effect", "report", "claim"])
def test_auto_host_dispatch_is_transport_independent(
    monkeypatch, host_acceptance, direct, calls, contract, expected,
):
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    ctx, run, requests = host_acceptance
    ctx.is_direct_chat = direct
    ctx.task_contract = contract
    trace = {"tool_calls": calls}
    run(trace)
    assert len(requests) == expected, trace.get("acceptance_decision", {}).get("degraded_reasons")
    assert len(trace.get("review_runs", [])) == expected
    if expected:
        assert trace["review_runs"][0]["authority"] == "host_root"
        assert trace["acceptance_decision"]["reason"] == "improvement_capsule"
    else:
        assert trace["review_decision"]["eligibility"] == "not_eligible"
        assert "acceptance_decision" not in trace


@pytest.mark.parametrize("direct", [False, True], ids=["queued", "direct"])
def test_agent_requested_readonly_review_reaches_host_dispatch(
    monkeypatch, host_acceptance, direct,
):
    from ouroboros.loop_tool_execution import process_tool_results
    from ouroboros.tools.review import _handle_task_acceptance_review

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    ctx, run, requests = host_acceptance
    ctx.is_direct_chat = direct
    trace = {"tool_calls": [_call("read_file")]}
    args = {"claim": "Sources disagree about the date.", "goal": "Compare the sources."}
    result = _handle_task_acceptance_review(ctx, **args)
    assert requests == []  # The tool records intent; the host alone runs the panel.
    process_tool_results([{
        "fn_name": "task_acceptance_review", "tool_call_id": "review-request",
        "result": result, "is_error": False, "args_for_log": args,
        "tool_args": args, "result_meta": {"status": "deferred_to_host_acceptance"},
    }], [], trace, emit_progress=lambda _msg, *, incident=None: None)
    assert not turn_has_reviewable_effects(trace)
    assert trace["acceptance_evidence_calls"][0]["authoritative"] is False
    run(trace)
    assert len(requests) == 1
    assert requests[0].evidence["agent_supplied"]["acceptance_request"]["claim"] == args["claim"]
    assert trace["review_runs"][0]["authority"] == "host_root"
    assert trace["acceptance_decision"]["status"] == "revision_requested"
    assert trace["acceptance_decision"]["reason"] == "improvement_capsule"


@pytest.mark.parametrize("mode,direct,child,expected", [
    ("off", False, False, 0),
    ("auto", False, True, 0),
    ("required", False, True, 0),
    ("required", True, False, 1),
    ("required", False, False, 1),
], ids=["off", "auto-child", "required-child", "required-direct", "required-queued"])
def test_agent_request_preserves_existing_mode_and_lineage_boundaries(
    monkeypatch, host_acceptance, mode, direct, child, expected,
):
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", mode)
    ctx, run, requests = host_acceptance
    ctx.is_direct_chat = direct
    if child:
        ctx.task_metadata = {"root_task_id": "parent", "parent_task_id": "parent", "delegation_role": "worker"}
    trace = {"tool_calls": [_call("task_acceptance_review")]}
    run(trace)
    assert len(requests) == expected
    assert len(trace.get("review_runs", [])) == expected


@pytest.mark.parametrize("requested", [False, True])
def test_auto_forced_exit_records_review_debt_only_when_requested(
    monkeypatch, host_acceptance, requested,
):
    from ouroboros.loop_acceptance import _record_forced_acceptance_bypass

    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    ctx, _run, requests = host_acceptance
    trace = {"tool_calls": [_call("task_acceptance_review")] if requested else []}
    _record_forced_acceptance_bypass(SimpleNamespace(
        tools=SimpleNamespace(_ctx=ctx), task_id="root",
        accumulated_usage={"reason_code": "budget_exhausted"},
    ), trace, "budget_exhausted")
    assert requests == []
    assert not trace.get("review_runs")
    if requested:
        assert trace["acceptance_decision"]["reason"] == "acceptance_bypassed_budget_exhausted"
        assert trace["acceptance_decision"]["status"] == "finalized_unaccepted"
    else:
        assert trace["review_decision"]["eligibility"] == "not_eligible"
        assert "acceptance_decision" not in trace


# ----- cognitive / root redirect recovery -----

def test_cognitive_block_is_advisory_not_a_failure():
    # A self-initiated cognitive write via the wrong tool is an advisory redirect:
    # it must never fail the task, even with no follow-up correction.
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "cognitive_tool_required",
         "args": {"root": "runtime_data", "path": "memory/identity.md"}},
    ]}
    assert _unresolved_tool_errors(trace) == []


def test_cognitive_block_advisory_even_with_unrelated_followup():
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "cognitive_tool_required",
         "args": {"root": "runtime_data", "path": "memory/knowledge/topicA.md"}},
        {"tool": "update_scratchpad", "is_error": False, "status": "ok", "args": {}},
    ]}
    assert _unresolved_tool_errors(trace) == []


def test_root_required_recovers_via_user_files_write():
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_user_files",
         "args": {"path": "/home/x/Desktop/a.html"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"root": "user_files", "path": "Desktop/a.html"}},
    ]}
    assert _unresolved_tool_errors(trace) == []


def test_root_required_not_recovered_by_non_user_files_write():
    # A blocked home write must NOT be masked by a write to a different root.
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_user_files",
         "args": {"path": "/home/x/Desktop/a.html"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"root": "active_workspace", "path": "notes.txt"}},
    ]}
    unresolved = _unresolved_tool_errors(trace)
    assert len(unresolved) == 1
    assert unresolved[0]["status"] == "root_required_user_files"


def test_root_required_not_recovered_by_different_user_files_filename():
    # Same root, different file is NOT recovery of the originally blocked deliverable.
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_user_files",
         "args": {"path": "/home/x/Desktop/a.html"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"root": "user_files", "path": "Desktop/b.html"}},
    ]}
    unresolved = _unresolved_tool_errors(trace)
    assert len(unresolved) == 1
    assert unresolved[0]["status"] == "root_required_user_files"


def test_root_required_recovers_via_user_files_batch_files():
    # Recovery via a batched files[] user_files write counts (path+files[] both sides).
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_user_files",
         "args": {"path": "/home/x/Desktop/a.html"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"root": "user_files", "files": [{"path": "Desktop/a.html", "content": "x"}]}},
    ]}
    assert _unresolved_tool_errors(trace) == []


def test_root_required_active_workspace_recovers_via_active_workspace_write():
    # Parity with the user_files contract (cumulative review r2): the
    # ACTIVE_WORKSPACE redirect is recovered only by a later write of the SAME
    # file via root=active_workspace.
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_active_workspace",
         "args": {"path": "/app/src/main.py"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"root": "active_workspace", "path": "src/main.py"}},
    ]}
    assert _unresolved_tool_errors(trace) == []


def test_root_required_active_workspace_recovers_via_default_root_write():
    # active_workspace is the DEFAULT root for write_file/edit_text (scope r2
    # finding): a retry that simply omits root writes to the demanded place and
    # must earn the recovery credit — no false execution-axis degradation.
    # user_files stays explicit-only (it is never a default root).
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_active_workspace",
         "args": {"path": "/app/src/main.py"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"path": "src/main.py"}},
    ]}
    assert _unresolved_tool_errors(trace) == []
    trace_uf = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_user_files",
         "args": {"path": "/home/x/Desktop/a.html"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"path": "Desktop/a.html"}},  # rootless != user_files
    ]}
    assert len(_unresolved_tool_errors(trace_uf)) == 1


def test_root_required_active_workspace_not_recovered_by_user_files_write():
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_active_workspace",
         "args": {"path": "/app/src/main.py"}},
        {"tool": "write_file", "is_error": False, "status": "ok",
         "args": {"root": "user_files", "path": "main.py"}},
    ]}
    unresolved = _unresolved_tool_errors(trace)
    assert len(unresolved) == 1
    assert unresolved[0]["status"] == "root_required_active_workspace"


def test_root_required_terminal_not_recovered_by_artifact_output():
    # Terminal branch: a run_command with ARTIFACT_OUTPUTS must NOT clear a
    # ROOT_REQUIRED block via the generic recovery path — only a real user_files
    # write of the blocked file recovers it.
    trace = {"tool_calls": [
        {"tool": "write_file", "is_error": True, "status": "root_required_user_files",
         "args": {"path": "/home/x/Desktop/a.html"}},
        {"tool": "run_command", "is_error": False, "status": "ok",
         "args": {"cmd": ["build"], "outputs": ["a.html"]}, "artifact_registered": True},
    ]}
    unresolved = _unresolved_tool_errors(trace)
    assert len(unresolved) == 1
    assert unresolved[0]["status"] == "root_required_user_files"


# ----- persistence into loop_outcome -----

def test_loop_outcome_surfaces_review_decision():
    outcome = derive_loop_outcome(
        "final answer",
        {},
        {"tool_calls": [], "review_decision": {"eligibility": "eligible", "trigger": "required_effect"}},
    )
    assert outcome["schema_version"] == 3
    assert outcome["review_eligibility"] == "eligible"
    assert outcome["review_trigger"] == "required_effect"


def test_loop_outcome_defaults_when_no_decision():
    outcome = derive_loop_outcome("final answer", {}, {"tool_calls": []})
    assert outcome["review_eligibility"] == "not_eligible"
    assert outcome["review_trigger"] == "not_evaluated"


# ----- label-only invariant (v6.35.0): host-forced `required` review must NOT
# inject reviewer output or a draft assistant turn into the transcript — the old
# re-loop that rewrote the deliverable into a meta-essay is removed. The audit
# round can no longer silently replace the normal final answer because it no
# longer touches the transcript at all. -----

def test_acceptance_review_is_label_only_no_transcript_injection():
    # v7 L-B split: the negatives sweep the whole loop family so no leaf can
    # revive the injection; the verdict recording lives with the acceptance
    # review owner, which loop.py re-exports.
    loop_dir = pathlib.Path(__file__).resolve().parents[1] / "ouroboros"
    src = "".join(
        path.read_text(encoding="utf-8")
        for path in [loop_dir / "loop.py", *sorted(loop_dir.glob("loop_*.py"))]
    )
    # The old injection strings are gone (no draft re-commit, no review payload).
    assert "Do NOT replace your user-facing answer with a status report" not in src
    assert "[TASK ACCEPTANCE REVIEW]" not in src
    # The verdict is still recorded on the objective axis (immune signal kept).
    assert "review_runs" in (loop_dir / "loop_acceptance_review.py").read_text(encoding="utf-8")
