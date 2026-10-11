"""Received outcomes, pending custody and informed author choices at real owners."""
import json
import pytest
from ouroboros.tools import git
from ouroboros.tools.scope_review import ScopeReviewResult
from ouroboros.mutation_attribution import capture_mutation_baseline
from ouroboros.task_results import write_task_result
from ouroboros.review_state import load_state
from tests.test_advisory_inline_freshness import candidate  # noqa: F401


@pytest.mark.parametrize("basis", ["none", "unrelated_prior", "explicit_prior", "custody_lost"])
def test_commit_finish_requires_received_outcome(candidate, monkeypatch, basis):  # noqa: F811
    ctx = candidate
    monkeypatch.setenv('OUROBOROS_REVIEW_ENFORCEMENT', 'advisory')
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE', 'pro')
    monkeypatch.setenv('OUROBOROS_REVIEW_MAX_CYCLES', 'unlimited')
    ctx.branch_dev = git.run_cmd(['git', 'branch', '--show-current'], cwd=ctx.repo_dir).strip()
    git.run_cmd(['git', 'reset', '--hard', 'HEAD'], cwd=ctx.repo_dir)
    (ctx.repo_dir / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
    git.run_cmd(['git', 'add', 'VERSION'], cwd=ctx.repo_dir)
    git.run_cmd(['git', 'commit', '-m', 'fixture version'], cwd=ctx.repo_dir)
    write_task_result(ctx.drive_root, ctx.task_id, 'running')
    capture_mutation_baseline(ctx.drive_root, ctx.task_id, [{'surface_type':'system_repo','host_root':str(ctx.repo_dir)}])
    (ctx.repo_dir / 'change.py').write_text('value = 2\n', encoding='utf-8')
    monkeypatch.setattr(git, '_run_review_preflight_tests', lambda *_a, **_kw: None)
    monkeypatch.setattr(git, '_post_commit_result', lambda *_a, **_kw: None)
    monkeypatch.setattr(git, '_auto_push', lambda *_a, **_kw: '')
    calls = []
    def reviewer(_ctx, message, **kw):
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        calls.append(kw["review_binding_fingerprint"])
        if basis in {"unrelated_prior", "explicit_prior"} and len(calls) == 1:
            ctx._last_triad_raw_results = [{"status": "responded", "raw_text": "Address the amount."}]
            ctx._last_review_critical_findings = [{"item": "amount", "severity": "critical"}]
            ctx._last_scope_raw_result = {"status": "responded"}
            return "Critical feedback", ScopeReviewResult(blocked=False, status="responded"), "critical_findings", []
        state = "custody_lost" if basis == "custody_lost" else "in_flight"
        ctx._last_triad_raw_results = [{'slot_id': 'critic', 'status':'pending', 'operation_state':state,'operation_id':'pending-triad','late_result_pending':True}]
        ctx._last_scope_raw_result = {'status':'pending','operation_state':state,'operation_id':'pending-scope','late_result_pending':True}
        return 'All reviewers still running; no feedback received.', ScopeReviewResult(blocked=True,status='pending'), 'infra_failure', []
    monkeypatch.setattr(git, '_run_parallel_review', reviewer)
    head = git.run_cmd(['git','rev-parse','HEAD'], cwd=ctx.repo_dir)
    if basis in {"unrelated_prior", "explicit_prior"}:
        prior = git._repo_commit_push(ctx, "Prior attempt", skip_advisory_review=True)
        assert "Review outcome returned before commit" in prior
        (ctx.repo_dir / "change.py").write_text("value = 3\n", encoding="utf-8")
    first = git._repo_commit_push(ctx, 'Fix amount', skip_advisory_review=True)
    reference = json.loads(first.split('\n',1)[1])['review_reference']
    if basis == "explicit_prior":
        reference = json.loads(prior.split('\n', 1)[1])['review_reference']
    row = load_state(ctx.drive_root).attempts[-1]
    from ouroboros.tools.claude_advisory_review import _handle_review_status
    projected = json.loads(_handle_review_status(ctx))
    assert ("author_disposition" in projected["next_step"]) is (basis == "custody_lost")
    assert ("review_reference" in projected) is (basis == "custody_lost")
    assert row.status == 'reviewing' and row.late_result_pending
    assert not row.critical_findings
    second = git._repo_commit_push(ctx, 'Fix amount', review_reference=reference,
        author_disposition={'disposition':'accepted','rationale':'Proceed without any reviewer response.'})
    changed = git.run_cmd(['git','rev-parse','HEAD'],cwd=ctx.repo_dir) != head
    assert changed is (basis in {"explicit_prior", "custody_lost"}), second
    if basis in {"none", "unrelated_prior"}:
        assert "needs received feedback" in second
        assert "continuation is not available yet" in first
    assert len(calls) == (2 if basis in {"unrelated_prior", "explicit_prior"} else 1)
    retained = next(item for item in load_state(ctx.drive_root).attempts if item.attempt == row.attempt)
    assert retained.triad_raw_results == row.triad_raw_results and retained.late_result_pending


from types import SimpleNamespace
from tests.test_plan_review_engine import harness, _call  # noqa: F401
from ouroboros.task_results import load_plan_review_state
from ouroboros.tools import plan_review as pr


def test_no_feedback_pending_plan_cannot_author_finish(harness, monkeypatch):  # noqa: F811
    h = harness
    h.state['enforcement'] = 'advisory'
    monkeypatch.setenv('OUROBOROS_REVIEW_ENFORCEMENT', 'advisory')
    monkeypatch.setenv('OUROBOROS_REVIEW_MAX_CYCLES', '1')
    import ouroboros.review_substrate as review_substrate
    def substrate(request, *, slots, drive_root, llm, usage_ctx=None):
        return SimpleNamespace(actors=[{
            'slot_id':slot.slot_id,'model':slot.model,'status':'error','raw_text':'',
            'error':'logical wait expired','usage':{'physical_attempt_state':'dispatched'},
            'prompt_ref':{},'response_ref':{},'operation_id':f'op-{slot.slot_id}',
            'operation_state':'in_flight','late_result_pending':True} for slot in slots])
    monkeypatch.setattr(review_substrate, 'run_review_request', substrate)
    ctx = h.make_ctx()
    _call(ctx)
    wave = load_plan_review_state(h.drive, ctx.task_id)['waves'][-1]
    assert wave['custody_pending'] and not wave['findings']
    second = pr._handle_plan_task(ctx, review_disposition={
        'review_fingerprint':wave['request_fingerprint'],'items':[], 'author_action':'finish',
        'author_disposition':{'disposition':'accepted','rationale':'Proceed without reviewer feedback.'}})
    assert 'Advisory author finish permits proceeding' not in second, 'Accepted plan with every reviewer unresolved and no feedback'


def test_unavailable_triad_requires_author_handback(candidate, monkeypatch):  # noqa: F811
    ctx = candidate
    monkeypatch.setenv('OUROBOROS_RUNTIME_MODE','pro')
    monkeypatch.setenv('OUROBOROS_REVIEW_ENFORCEMENT','advisory')
    git._reset_commit_review_state(ctx)
    monkeypatch.setattr(git, '_run_review_preflight_tests', lambda *_a, **_kw: None)
    from ouroboros.tools import review
    monkeypatch.setattr(review, '_handle_multi_model_review', lambda *_a, **_kw: json.dumps({'error':'Review service unavailable.'}))
    def reviewer(_ctx, message, **kw):
        from ouroboros.review_dispatch import invoke_review_paid_stamp
        invoke_review_paid_stamp(ctx._review_paid_stamp)
        error = review._dispatch_unified_review(ctx, message, {
            'blocking_review':False,'prompt':'fixture','models':['critic'],'stable_prefix_len':0,
            'routes':['api_chat'],'session_task':'','target_repo':ctx.repo_dir,'row_plan':{},'retry_key':'fixture'})
        scope = ScopeReviewResult(blocked=False,status='responded')
        ctx._last_scope_raw_result = {'status':'responded'}
        return error, scope, ctx._last_review_block_reason, list(ctx._review_advisory)
    monkeypatch.setattr(git, '_run_parallel_review', reviewer)
    result = git._run_reviewed_stage_cycle(ctx,'Fix amount',0,skip_advisory_review=True,require_release_tag=False)
    assert result['status'] == 'reviewed', 'Unavailable triad proceeded to commit without first returning its failure for author choice'


from ouroboros.review_records import review_outcome_received


@pytest.mark.parametrize("rows,terminal,expected", [
    ([{"operation_state": "in_flight", "error": "logical wait ended"}], False, False),
    ([{"operation_state": "pending_dispatch", "status": "error"}], True, False),
    ([{"raw_results": [{"operation_state": "in_flight"}]}], True, False),
    ([{"operation_state": "in_flight"}, {"status": "responded"}], False, True),
    ([{"operation_state": "in_flight", "raw_text": "Pending host placeholder", "parsed": [{"item": "host note"}]}], False, False),
    ([{"operation_state": "in_flight"}, {"operation_state": "settled", "ok": True}], False, True),
    ([{"operation_state": "custody_lost", "late_result_pending": True}], False, True),
    ([{"operation_state": "settled", "error": "reviewer unavailable"}], False, True),
    ([], True, True),
    ([], False, False),
])
def test_received_outcome_keeps_live_custody_distinct(rows, terminal, expected):
    import copy
    before = copy.deepcopy(rows)
    assert review_outcome_received(rows, terminal=terminal) is expected
    assert rows == before


@pytest.mark.parametrize("action", ["finish", "stop"])
def test_plan_all_pending_retains_custody_and_allows_stop(harness, monkeypatch, action):  # noqa: F811
    import ouroboros.review_substrate as substrate
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    def running(request, *, slots, **kwargs):
        return SimpleNamespace(actors=[{"slot_id": slot.slot_id, "status": "error", "raw_text": "",
            "operation_state": "pending_dispatch", "operation_id": "held-" + slot.slot_id,
            "late_result_pending": True, "error": "No response yet"} for slot in slots])
    monkeypatch.setattr(substrate, "run_review_request", running)
    ctx = h.make_ctx()
    _call(ctx)
    before = load_plan_review_state(h.drive, ctx.task_id)
    wave = before["waves"][-1]
    result = pr._handle_plan_task(ctx, review_disposition={
        "review_fingerprint": wave["request_fingerprint"], "items": [], "author_action": action,
        "author_disposition": {"disposition": "deferred", "rationale": "Keep all original review operations."}})
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert ("Current author plan saved" in result) is (action == "stop")
    from ouroboros.tools.plan_review_artifacts import authority_wave
    assert len(after["waves"]) == len(before["waves"])
    for old, new in zip(before["waves"], after["waves"]):
        assert old["wave_artifact"] == new["wave_artifact"]
        assert authority_wave(h.drive, ctx.task_id, old) == authority_wave(h.drive, ctx.task_id, new)
    assert after["cycles_paid"] == before["cycles_paid"]


def test_invalid_task_author_action_publishes_typed_error(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tools.tool_result import ToolResult
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **kw: (True, ""))
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    result = registry.execute_result("task_acceptance_review", {"author_action": "finish", "rationale": ""})
    assert isinstance(result, ToolResult)
    assert result.code == "TOOL_ARG_ERROR" and result.status == "error"
    assert "author_action requires" in result.text


@pytest.mark.parametrize("kind", ["clean", "advisory"])
def test_clean_commit_path_and_structured_advice_are_distinct(candidate, monkeypatch, kind):  # noqa: F811
    ctx = candidate
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    git._reset_commit_review_state(ctx)
    monkeypatch.setattr(git, "_run_review_preflight_tests", lambda *a, **kw: None)
    finding = {"item": "observable review advice", "severity": "advisory", "reason": "Check this tradeoff"}
    def reviewer(*args, **kwargs):
        ctx._last_review_advisory_findings = [finding] if kind == "advisory" else []
        ctx._last_triad_raw_results = [{"status": "responded", "raw_text": "[]"}]
        ctx._last_scope_raw_result = {"status": "responded"}
        return None, ScopeReviewResult(blocked=False, status="responded"), "", []
    monkeypatch.setattr(git, "_run_parallel_review", reviewer)
    result = git._run_reviewed_stage_cycle(ctx, "Review changed candidate", 0,
        skip_advisory_pre_review=True, require_release_tag=False)
    assert result["status"] == ("passed" if kind == "clean" else "reviewed")


@pytest.mark.parametrize("basis", ["partial", "custody_lost", "explicit_prior", "unrelated_prior"])
def test_plan_choice_uses_only_its_named_outcome(harness, monkeypatch, basis):  # noqa: F811
    import ouroboros.review_substrate as substrate
    from tests.test_plan_review_engine import _finding
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "unlimited")
    ctx = h.make_ctx()
    criticism = json.dumps([_finding("amount", "blocking", breaks="claim_1")])
    prior_fp = ""
    if basis in {"explicit_prior", "unrelated_prior"}:
        h.install({slot: criticism for slot in ("s1", "s2", "s3")})
        _call(ctx)
        prior_fp = load_plan_review_state(h.drive, ctx.task_id)["waves"][-1]["request_fingerprint"]
    calls = []
    def current(request, *, slots, **kwargs):
        calls.append(request)
        actors = []
        for index, slot in enumerate(slots):
            received = basis == "partial" and index == 0
            state = "settled" if received else "custody_lost" if basis == "custody_lost" else "in_flight"
            actors.append({"slot_id": slot.slot_id, "status": "ok" if received else "error",
                "raw_text": criticism if received else "", "error": "" if received else "Outcome unavailable",
                "operation_id": "current-" + slot.slot_id, "operation_state": state,
                "late_result_pending": not received})
        return SimpleNamespace(actors=actors)
    monkeypatch.setattr(substrate, "run_review_request", current)
    _call(ctx, plan="A distinct plan whose review is still being collected.")
    before = load_plan_review_state(h.drive, ctx.task_id)
    wave = before["waves"][-1]
    if basis == "partial":
        assert wave["custody_pending"] and wave["counts"]["parseable"] == 1
        assert wave["actors"][0]["ok"] is True and wave["findings"]
    result = _call(ctx, plan="Current author-selected revised plan.", review_disposition={
        "review_fingerprint": prior_fp if basis == "explicit_prior" else wave["request_fingerprint"],
        "items": [], "author_action": "finish",
        "author_disposition": {"disposition": "partial", "rationale": "I am responding to the explicitly named outcome."}})
    assert ("Current author plan saved" in result) is (basis != "unrelated_prior"), result
    after = load_plan_review_state(h.drive, ctx.task_id)
    assert len(calls) == 1 and after["cycles_paid"] == before["cycles_paid"]
    from ouroboros.tools.plan_review_artifacts import authority_wave
    assert len(after["waves"]) == len(before["waves"])
    for old, new in zip(before["waves"], after["waves"]):
        assert old["wave_artifact"] == new["wave_artifact"]
        assert authority_wave(h.drive, ctx.task_id, old) == authority_wave(h.drive, ctx.task_id, new)


@pytest.mark.parametrize("prior_feedback", [False, True])
def test_repeated_exposed_host_failure_does_not_pump_author_rounds(tmp_path, monkeypatch, prior_feedback):
    import ouroboros.loop as loop
    import ouroboros.loop_acceptance_review as acceptance
    import ouroboros.review_substrate as substrate
    from ouroboros.acceptance_settlement import expose_acceptance_feedback
    from ouroboros.contracts.task_contract import build_task_contract
    from tests.test_acceptance_publication import _context
    ctx = _context(tmp_path)
    ctx.task_contract = build_task_contract({"id": "applied", "root_task_id": "applied"})
    ctx._task_acceptance_reviewed = False
    ctx.is_direct_chat = False
    write_task_result(tmp_path, "applied", "running", task_contract=ctx.task_contract)
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "required")
    monkeypatch.setattr(substrate, "triad_delivery_slots", lambda **kw: [])
    panels, applications = [], []
    result = substrate.ReviewRunResult(request={"surface": "task_acceptance"}, aggregate_signal="PASS",
        actors=[{"status": "ok", "signal": "PASS", "slot_id": "s1", "parsed": {"verdict": "PASS"}}], parsed_findings=[])
    def review(context):
        panels.append(context)
        return result
    def fail_apply(*args, **kwargs):
        applications.append(args)
        raise RuntimeError("persistent host application failure")
    monkeypatch.setattr(loop, "_execute_task_acceptance_panel", review)
    monkeypatch.setattr(acceptance, "_apply_task_acceptance_result", fail_apply)
    trace = {"tool_calls": []}
    if prior_feedback:
        trace["review_runs"] = [{"authority": "host_root", "binding_hash": "earlier", "feedback_delivered": True}]
    messages = []
    kwargs = dict(tools=SimpleNamespace(_ctx=ctx), content="Unchanged complete answer", task_id="applied", task_type="task",
                  llm_trace=trace, drive_root=tmp_path, messages=messages, emit_progress=lambda *a, **kw: None)
    assert acceptance._run_task_acceptance_review_once(**kwargs) is True
    assert len(applications) == len(panels) == 1
    assert trace["acceptance_decision"]["status"] == "revision_requested"
    run_count = len(trace["review_runs"])
    expose_acceptance_feedback(trace, messages, "applied")
    assert trace["acceptance_review_outcome"]["feedback_delivered"]
    assert acceptance._run_task_acceptance_review_once(**kwargs) is False
    assert len(panels) == 1 and len(applications) == 2
    assert len(trace["review_runs"]) == run_count
    assert trace["acceptance_decision"]["reason"] == "review_degraded"
    failures = [run for run in trace["review_runs"] if run.get("aggregate_signal") == "DEGRADED"]
    assert failures == []  # Host failure stays beside the real PASS, not a fake panel.
    assert "persistent host application failure" in trace["review_decision"]["host_failure"]["detail"]
    assert trace["acceptance_decision"]["origin"] == "host_acceptance_processing"
