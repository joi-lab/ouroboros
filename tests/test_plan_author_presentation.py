"""A real current author plan reaches the existing review UI without a fake wave."""

import json
from pathlib import Path
import subprocess

import pytest

from ouroboros.outcomes import public_task_result
from ouroboros.task_results import load_plan_review_state, load_task_result
from ouroboros.preflight_node import resolve_node
from tests.test_plan_review_engine import DECK_SPEC, _call, _finding, harness as _harness
from tests.test_preflight_node import requires_node

harness = _harness
pytestmark = pytest.mark.serial


@requires_node
@pytest.mark.parametrize("action", ["finish", "stop"])
@pytest.mark.parametrize("cap", ["1", "2"])
def test_current_plan_author_is_not_an_extra_reviewer(harness, monkeypatch, action, cap):
    h = harness
    h.state["enforcement"] = "advisory"
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", cap)
    ctx = h.make_ctx()
    feedback = json.dumps([_finding("first", "blocking", breaks="claim_1")])
    transport = h.install({slot: feedback for slot in ("s1", "s2", "s3")})
    _call(ctx)
    old = load_plan_review_state(h.drive, ctx.task_id)
    result = _call(ctx, {**DECK_SPEC, "acceptance_claims": ["Corrected current claim"]},
        plan="Corrected plan without another critic.", review_disposition={
            "review_fingerprint": old["current_attempt"]["fingerprint"], "items": [],
            "author_action": action,
            "author_disposition": {"disposition": "partial", "rationale": "Considered the actual feedback."},
        })
    assert "Current author plan saved" in result
    assert len(transport.calls) == 1
    detail = public_task_result(load_task_result(h.drive, ctx.task_id))
    module = Path(__file__).resolve().parents[1] / "web/modules/review_presentation.js"
    script = "import {planReviewGroupFromTaskDetail, renderReviewsSection, mergeReviewGroup} from " + json.dumps(module.as_uri()) + ";" + r"""
        import assert from 'node:assert/strict';
        import { readFileSync } from 'node:fs';
        const detail = JSON.parse(readFileSync(0, 'utf8'));
        const subject = detail.plan_review_state.current_attempt.author_subject;
        const action = subject.author_disposition.action;
        const group = planReviewGroupFromTaskDetail(detail);
        assert.equal(group.activeCount, 0);
        assert.equal(group.attemptCount, detail.plan_review_state.waves.length);
        assert.equal(group.attempts.length, 1);
        assert.equal(group.attempts[0].superseded, false);
        assert.notEqual(group.attempts[0].verdict, 'PASS');
        assert.match(group.authorDecisionText, new RegExp(`Author ${action}: partial`));
        assert.ok(group.authorDecisionText.includes(subject.source_ref.path));
        assert.ok(group.authorDecisionText.includes(subject.author_disposition.subject_hash));
        assert.ok(group.authorDecisionText.includes(subject.review_fingerprint));
        const disclosure = { sectionExpanded: true, expandedGroups: new Set([group.id]) };
        const html = renderReviewsSection([group], disclosure);
        assert.ok(html.includes('Plan author decision'));
        assert.ok(!html.includes(' active'));
        assert.ok(!html.includes('Review result unavailable'));
        assert.equal(disclosure.expandedGroups.size, 1);
        const store = new Map();
        mergeReviewGroup(store, group);
        assert.equal(mergeReviewGroup(store, group).attempts.length, 1);
        // The same source shape with real remaining custody is still active;
        // author finality must not hide an unfinished physical reviewer.
        detail.plan_review_state.waves[0].custody_pending = true;
        const pending = planReviewGroupFromTaskDetail(detail);
        assert.equal(pending.activeCount, 1);
        assert.equal(pending.attempts.length, 1);
        assert.equal(pending.state, 'running');
        assert.equal(pending.authorDecisionText, group.authorDecisionText);
    """
    completed = subprocess.run([resolve_node(), "--input-type=module", "-e", script],
        input=json.dumps(detail), capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
