import json
import inspect

import pytest


def test_commit_attempt_does_not_infer_author_finish_from_success():
    from ouroboros.tools import commit_gate

    source = inspect.getsource(commit_gate._record_commit_attempt)
    assert 'author_disposition = _req("author_disposition", None)' in source
    assert 'if author_disposition is None and status == "succeeded"' not in source
    assert 'disposition="accepted"' not in source


def test_author_disposition_is_hash_bound_and_rejects_malformed():
    from ouroboros.review_records import (
        build_author_disposition,
        validate_author_disposition,
    )

    record = build_author_disposition(
        disposition="rejected",
        rationale="The remaining note is outside this task.",
        subject_hash="abc123",
        reviewer_signal="REVISE_PLAN",
        enforcement="advisory",
    )
    assert record["subject_hash"] == "abc123"
    assert validate_author_disposition(record, subject_hash="abc123") == record
    assert validate_author_disposition(record, subject_hash="stale") is None
    with pytest.raises(ValueError, match="rationale"):
        build_author_disposition(
            disposition="accepted", rationale="", subject_hash="abc123",
        )


def test_plan_author_finish_is_projected_without_closing_blocking_gate():
    from ouroboros.task_results import _validated_plan_review_state

    state = {
        "schema_version": 2,
        "series_id": "s",
        "cycles_paid": 1,
        "need_evidence_seen": [],
        "current_attempt": {"fingerprint": "a" * 64, "status": "open", "reason": ""},
        "waves": [{
            "request_fingerprint": "a" * 64,
            "aggregate": "REVISE_PLAN",
            "closed": False,
            "spec": {"goal": "g"},
            "findings": [{"finding_id": "f", "class": "blocking"}],
            "dispositions": [],
            "author_disposition": {
                "disposition": "rejected",
                "rationale": "The reviewer request is out of scope.",
                "subject_hash": "a" * 64,
                "reviewer_signal": "REVISE_PLAN",
                "enforcement": "advisory",
                "recorded_at": "2026-01-01T00:00:00Z",
                "source": "author",
            },
        }],
    }
    loaded = _validated_plan_review_state(state)
    assert loaded["waves"][0]["author_disposition"]["disposition"] == "rejected"
    assert loaded["waves"][0]["closed"] is False
    bad = json.loads(json.dumps(state))
    bad["waves"][0]["author_disposition"]["subject_hash"] = "b" * 64
    with pytest.raises(ValueError, match="author_disposition"):
        _validated_plan_review_state(bad)


def test_skill_review_state_round_trips_current_hash_author_finish(tmp_path):
    from ouroboros.skill_loader import SkillReviewState, load_review_state, save_review_state

    state = SkillReviewState(
        status="blockers",
        content_hash="c" * 64,
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "advisory"}],
        author_disposition={
            "disposition": "partial",
            "rationale": "The advisory finding is understood and accepted for this revision.",
            "subject_hash": "c" * 64,
            "reviewer_signal": "blockers",
            "enforcement": "advisory",
            "recorded_at": "2026-01-01T00:00:00Z",
            "source": "author",
        },
    )
    save_review_state(tmp_path, "demo", state)
    loaded = load_review_state(tmp_path, "demo")
    assert loaded.author_disposition["subject_hash"] == "c" * 64
    assert loaded.to_dict()["author_disposition"]["disposition"] == "partial"


def test_predeclared_stance_cannot_hide_the_first_feedback(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod
    from ouroboros.loop_acceptance_review import _apply_task_acceptance_result
    from ouroboros.review_substrate import ReviewRunResult

    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "advisory")
    monkeypatch.setattr(loop_mod, "_end_task_acceptance_fence", lambda *_a, **_k: True)
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "3")
    tools_ctx = SimpleNamespace(_task_acceptance_reviewed=False)
    ctx = loop_mod._TaskAcceptanceContext(
        tools=SimpleNamespace(_ctx=tools_ctx), content="initial answer", task_id="t", task_type="task",
        llm_trace={"tool_calls": [], "acceptance_decision": {
            "agent_disposition": "rejected", "agent_rationale": "Premature declaration.",
        }}, drive_root=None, messages=[], emit_progress=lambda *_a, **_k: None,
        mode="required", subtree_statuses=[], budget_profile={"max_improvement_passes": 3},
        passes_done=0, review_binding={"binding_hash": "first-binding"},
    )
    result = ReviewRunResult(
        request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
        actors=[{"slot_id": "s0", "signal": "FAIL", "parsed": {
            "verdict": "FAIL", "outcome_tier": "best_effort", "completion_coach": "Fix the output.",
        }}], parsed_findings=[], aggregate_signal="FAIL",
    )
    assert _apply_task_acceptance_result(ctx, result) is True
    assert ctx.llm_trace["acceptance_decision"]["reason"] == "improvement_capsule"
    assert ctx.llm_trace["review_runs"][-1]["feedback_offered"]
    assert not ctx.llm_trace["review_runs"][-1].get("feedback_delivered")
    assert ctx.messages and "Fix the output" in ctx.messages[-1]["content"]
    assert not tools_ctx._task_acceptance_reviewed


@pytest.mark.parametrize("change", ["none", "owner", "evidence", "tools", "blocking", "ordinary_evidence", "late_service"])
def test_post_review_finish_handles_revised_answer_without_another_panel(monkeypatch, tmp_path, change):
    from types import SimpleNamespace
    import ouroboros.loop as loop_mod
    import ouroboros.loop_acceptance_review as review
    from ouroboros.loop_acceptance import merge_agent_acceptance_stance, _set_acceptance_decision
    from ouroboros.review_substrate import ReviewRunResult
    from tests.test_loop_acceptance_gate import _seed_acceptance_root

    monkeypatch.setattr(loop_mod, "get_task_review_mode", lambda: "required")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "3")
    enforcement = {"value": "advisory"}
    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: enforcement["value"])
    tools_ctx = SimpleNamespace(_task_acceptance_reviewed=False, is_direct_chat=False,
                               drive_root=str(tmp_path), _owner_directives=[])
    _seed_acceptance_root(tmp_path, "author-root", tools_ctx)
    tools = SimpleNamespace(_ctx=tools_ctx)
    trace = {"tool_calls": [{"tool": "write_file", "args": {"path": "answer.txt"}}]}
    messages = [{"role": "user", "content": "Solve the task."}]
    calls = []
    def panel(ctx):
        calls.append(ctx.content)
        return ReviewRunResult(
            request={"surface": "task_acceptance", "policy": {"min_successful_slots": 1}},
            actors=[{"slot_id": "critic", "signal": "FAIL", "parsed": {
                "verdict": "FAIL", "outcome_tier": "best_effort", "completion_coach": "Fix the output.",
            }}], parsed_findings=[], aggregate_signal="FAIL",
        )
    monkeypatch.setattr(loop_mod, "_execute_task_acceptance_panel", panel)
    run = lambda text: review._run_task_acceptance_review_once(
        tools=tools, content=text, task_id="author-root", task_type="task", llm_trace=trace,
        drive_root=tmp_path, messages=messages, emit_progress=lambda *_a, **_k: None,
    )
    assert run("initial answer") is True
    critic_hash = trace["review_runs"][-1]["binding_hash"]
    from ouroboros.acceptance_settlement import expose_acceptance_feedback
    expose_acceptance_feedback(trace, messages, "author-root")
    trace["tool_calls"].append({"tool": "task_acceptance_review", "args": {}})
    merge_agent_acceptance_stance(trace, {"disposition": "partial", "explicit_finish": change != "ordinary_evidence", "rationale": "I fixed the material issue."}, tools_ctx)
    if change in {"owner", "evidence"}:
        _set_acceptance_decision(trace, {"status": "revision_requested",
                                        "reason": "owner_followup" if change == "owner" else "evidence_refresh"})
    elif change == "tools":
        trace["tool_calls"].append({"tool": "write_file", "args": {"path": "later.txt"}})
    elif change == "blocking":
        enforcement["value"] = "blocking"
    elif change == "late_service":
        trace["verification_events"] = [{"kind": "service_finalization_error",
            "services": [{"service_id": "service", "artifact_output_failed": True}]}]
    run("verified revised answer")
    if change == "none":
        assert calls == ["initial answer"]
        decision = trace["acceptance_decision"]
        assert decision["reason"] == "author_finish"
        assert decision["status"] == "finalized_unaccepted"
        assert decision["reviewer_binding_hash"] == critic_hash
        assert decision["author_disposition"]["subject_hash"] != critic_hash
        assert trace["review_runs"][0]["aggregate_signal"] == "FAIL"
        assert trace["review_runs"][0]["applied_decision"]["reason"] == "improvement_capsule"
    else:
        assert len(calls) == 2
        assert trace["acceptance_decision"]["reason"] != "author_finish"


def test_skill_author_finish_uses_existing_review_without_dispatch_same_hash(
    monkeypatch, tmp_path,
):
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state
    from ouroboros.tool_access_types import ResolvedResourceBinding
    from ouroboros.tools import skill_exec as skill_exec_mod
    from tests.test_skill_exec import _build_skill, _make_ctx

    skills_root = tmp_path / "skills"
    skill_dir = _build_skill(skills_root, "demo")
    ctx = _make_ctx(tmp_path)
    binding = ResolvedResourceBinding(
        profile="self_modification", root="skill_payload", operation="review",
        base_path=skill_dir, target_path=skill_dir, source="test",
        skill_name="demo", state_drive_root=tmp_path,
    )
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(skill_exec_mod, "build_resolved_resource_binding", lambda *a, **k: binding)
    monkeypatch.setattr(skill_exec_mod, "_skill_tool_preflight", lambda *a, **k: "")
    prior_hash = compute_content_hash(skill_dir)
    save_review_state(tmp_path, "demo", SkillReviewState(
        status="blockers", content_hash=prior_hash,
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "critical", "reason": "review finding"}],
        raw_actor_records=[{"slot_id": "s0", "status": "ok"}],
    ))
    monkeypatch.setattr(
        skill_exec_mod, "run_skill_review_lifecycle_blocking",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("author finish dispatched a new panel")),
        raising=False,
    )
    out = skill_exec_mod._handle_review_skill(
        ctx, skill="demo", _resolved_binding=binding,
        author_disposition="rejected", author_rationale="The finding is outside this task.",
    )
    assert "Author finish recorded" in out
    assert "review finding" in out
    loaded = __import__("ouroboros.skill_loader", fromlist=["load_review_state"]).load_review_state(tmp_path, "demo")
    assert loaded.author_disposition["subject_hash"] == prior_hash
    assert loaded.status == "blockers"


def test_skill_author_finish_binds_changed_hash_after_preflight_without_panel(
    monkeypatch, tmp_path,
):
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, load_review_state, save_review_state
    from ouroboros.tool_access_types import ResolvedResourceBinding
    from ouroboros.tools import skill_exec as skill_exec_mod
    from tests.test_skill_exec import _build_skill, _make_ctx

    skills_root = tmp_path / "skills"
    skill_dir = _build_skill(skills_root, "demo", script_body="print('old')\n")
    ctx = _make_ctx(tmp_path)
    binding = ResolvedResourceBinding(
        profile="self_modification", root="skill_payload", operation="review",
        base_path=skill_dir, target_path=skill_dir, source="test",
        skill_name="demo", state_drive_root=tmp_path,
    )
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setattr(skill_exec_mod, "build_resolved_resource_binding", lambda *a, **k: binding)
    monkeypatch.setattr(skill_exec_mod, "_skill_tool_preflight", lambda *a, **k: "")
    old_hash = compute_content_hash(skill_dir)
    save_review_state(tmp_path, "demo", SkillReviewState(
        status="blockers", content_hash=old_hash,
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "severity": "critical", "reason": "old finding"}],
        raw_actor_records=[{"slot_id": "s0", "status": "ok"}],
    ))
    (skill_dir / "scripts" / "hello.py").write_text("print('fixed')\n", encoding="utf-8")
    monkeypatch.setattr(
        skill_exec_mod, "run_skill_review_lifecycle_blocking",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("changed-hash finish dispatched a panel")),
        raising=False,
    )
    out = skill_exec_mod._handle_review_skill(
        ctx, skill="demo", _resolved_binding=binding,
        author_disposition="partial", author_rationale="The fix addresses the actionable part.",
    )
    current_hash = compute_content_hash(skill_dir)
    loaded = load_review_state(tmp_path, "demo")
    assert current_hash != old_hash
    assert loaded.content_hash == old_hash
    assert loaded.gate_for(current_hash)["reviewed_content_hash"] == old_hash
    assert loaded.is_stale_for(current_hash)
    assert loaded.gate_for(current_hash)["executable_review"]
    assert loaded.gate_for(current_hash)["author_accepted"] is True
    assert loaded.gate_for(current_hash, enforcement="blocking")["author_accepted"] is False
    assert loaded.gate_for(current_hash, enforcement="blocking")["executable_review"] is False
    assert loaded.author_disposition["subject_hash"] == current_hash
    assert loaded.status == "blockers"
    assert "raw reviewer findings" in out


def test_ordinary_advisory_commit_does_not_invent_author_finish(tmp_path):
    from types import SimpleNamespace
    from ouroboros.review_state import load_state
    from ouroboros.tools.commit_gate import _record_commit_attempt

    ctx = SimpleNamespace(
        drive_root=tmp_path,
        repo_dir=tmp_path,
        task_id="",
        _review_advisory=["advisory finding"],
        _current_review_attempt_number=0,
    )
    _record_commit_attempt(ctx, commit_message="ordinary advisory", status="succeeded")
    attempts = load_state(tmp_path).attempts
    assert attempts
    assert attempts[-1].author_disposition == {}


@pytest.mark.parametrize("status", ["clean", "warnings"])
@pytest.mark.parametrize("kind", ["script", "extension"])
def test_repeated_skill_finish_preserves_critic_and_independent_blocking(tmp_path, monkeypatch, status, kind):
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, load_skill, save_enabled, save_review_state
    from ouroboros.tool_access_types import ResolvedResourceBinding
    from ouroboros.tools import skill_exec
    from ouroboros.gateway.extensions import _review_fields
    from ouroboros.extension_liveness import _extension_runtime_state
    from tests.test_skill_exec import _build_skill, _make_ctx

    ctx = _make_ctx(tmp_path)
    skills = ctx.drive_root / "skills" / "external"
    manifest = None if kind == "script" else (
        "---\nname: demo\ndescription: Test extension.\nversion: 0.1.0\ntype: extension\nentry: plugin.py\nplugin_api_version: '2.0'\n---\nTest.\n"
    )
    directory = _build_skill(skills, "demo", manifest=manifest)
    changed = directory / "scripts/hello.py" if kind == "script" else directory / "plugin.py"
    if kind == "extension":
        changed.write_text("def register(api):\n    pass\n")
    original_hash = compute_content_hash(directory)
    findings = [] if status == "clean" else [{"item": "bug_hunting", "verdict": "FAIL", "severity": "advisory", "reason": "Original note"}]
    save_review_state(ctx.drive_root, "demo", SkillReviewState(
        status=status, content_hash=original_hash, findings=findings,
        raw_actor_records=[{"slot_id": "critic", "status": "ok"}],
    ))
    save_enabled(ctx.drive_root, "demo", True)
    binding = ResolvedResourceBinding(profile="self_modification", root="skill_payload", operation="review",
        base_path=directory, target_path=directory, source="test", skill_name="demo", state_drive_root=ctx.drive_root)
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    for revision in (1, 2):
        changed.write_text(f"print('author-{revision}')\n" if kind == "script" else f"def register(api):\n    value = {revision}\n")
        result = skill_exec._author_finish_existing_skill_review(ctx, binding, "demo",
            disposition="partial", rationale=f"Accepted revision {revision} after correcting the issue.")
        assert "error" not in result, result
        loaded = load_skill(directory, ctx.drive_root)
        assert loaded.review.content_hash == original_hash
        assert loaded.review.findings == findings
        assert loaded.review.is_stale_for(loaded.content_hash)
        assert loaded.review.gate_for(loaded.content_hash)["executable_review"]
        projected = _review_fields(loaded, github_token_configured=False)
        assert projected["review_stale"] and projected["executable_review"]
        assert projected["reviewed_content_hash"] == original_hash
        if kind == "script":
            actual = json.loads(skill_exec._handle_skill_exec(ctx, skill="demo", script="hello.py"))
            assert actual["exit_code"] == 0
            assert f"author-{revision}" in actual["stdout"]
        else:
            assert _extension_runtime_state(loaded, drive_root=ctx.drive_root, skills=[loaded])["desired_live"]
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    loaded = load_skill(directory, ctx.drive_root)
    assert not loaded.review.gate_for(loaded.content_hash)["executable_review"]
    if kind == "script":
        assert "SKILL_EXEC_BLOCKED" in skill_exec._handle_skill_exec(ctx, skill="demo", script="hello.py")
    else:
        assert not _extension_runtime_state(loaded, drive_root=ctx.drive_root, skills=[loaded])["desired_live"]
    save_review_state(ctx.drive_root, "demo", SkillReviewState(status="clean", content_hash=loaded.content_hash))
    refreshed = load_skill(directory, ctx.drive_root)
    assert refreshed.review.gate_for(refreshed.content_hash)["executable_review"]
