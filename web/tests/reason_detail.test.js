import assert from 'node:assert/strict';
import test from 'node:test';
import { readdirSync, readFileSync } from 'node:fs';

import {
    taskDoneIsTerminal, taskPresentation, taskReasonDetail, taskReasonPhrase, taskTerminalPhase, summarizeChatLiveEvent,
} from '../modules/log_events.js';

// A degraded delivery used to name one generic cause on every card. The record
// keeps the machine code; the card says what actually happened.

test('a typed cause is stated in the owner\'s words', () => {
    // No 'Reason:' / 'Acceptance:' prefix: a label naming an internal machine
    // concept in front of an owner sentence is the same leak in a politer font.
    assert.equal(
        taskReasonDetail({ reason_code: 'plan_review_advisory' }),
        'The plan review was never closed; the work went on with what the reviewers said.',
    );
    assert.equal(
        taskReasonDetail({ reason_code: 'delivery_control_degraded' }),
        "Ouroboros's final delivery instruction could not be applied, so the answer stands as delivered.",
    );
});

test('an unknown cause stays raw rather than becoming a wrong sentence', () => {
    assert.equal(taskReasonDetail({ reason_code: 'some_future_code' }), 'some_future_code');
    assert.equal(taskReasonPhrase('some_future_code'), 'some_future_code');
});

test('no cause and an owner-requested stop both render nothing', () => {
    assert.equal(taskReasonDetail({}), '');
    assert.equal(taskReasonDetail({ reason_code: '' }), '');
    // The soft stop is a SUCCESS and carries its own marker instead.
    assert.equal(taskReasonDetail({ reason_code: 'owner_requested_finalization' }), '');
});

test('every typed cause the loop can record has a sentence', () => {
    // Cross-language completeness: the reachable literals live in the Python
    // loop, the sentences live here, and a new cause must not silently fall
    // back to its machine code on the card.
    // v7 split ouroboros/loop.py into leaves — the reachable literals now sit in
    // loop_budget.py and loop_delivery.py — so scan the whole loop family
    // instead of the one file that used to hold them all.
    const pkg = new URL('../../ouroboros/', import.meta.url);
    const loop = readdirSync(pkg)
        .filter((name) => /^loop.*\.py$/.test(name))
        .sort()
        .map((name) => readFileSync(new URL(name, pkg), 'utf8'))
        .join('\n');
    const literals = new Set(
        [...loop.matchAll(/degraded_reason\s*=\s*"([a-z_]+)"/g)].map((m) => m[1]),
    );
    assert.ok(literals.size >= 3, `expected the known typed causes, saw ${[...literals]}`);
    for (const code of literals) {
        assert.notEqual(
            taskReasonPhrase(code), code,
            `no owner-facing sentence for degraded_reason "${code}" — add one to TASK_CAUSE_PHRASES`,
        );
    }
});

test('an accepted decision with a sentence still states its cause', () => {
    // Owner fork 1=B (2026-09-16): reviewers who approved the earlier revision
    // accept the task, and the row says which revision they approved. A clean
    // accepted decision keeps rendering nothing.
    const accepted = (reason) => taskReasonDetail({
        status: 'completed', reason_code: 'final_message',
        outcome_axes: { execution: { status: 'ok' },
            review: { status: 'pass', acceptance_decision: { status: 'accepted', reason } } },
    });
    assert.equal(accepted('previous_revision_accepted'),
        'The reviewers approved an earlier version of this answer; the current version was not re-reviewed.');
    // Owner 2A (2026-09-21): a blocking install accepts a reviewer-approved answer whose
    // admission close the supervisor never confirmed, and the row says so.
    assert.equal(accepted('admission_close_unconfirmed'),
        'Reviewers approved this answer; the supervisor did not confirm that task admission was closed.');
    assert.equal(accepted('clean_pass'), '');
    assert.equal(accepted(''), '');
});

test('every acceptance reason the host can record has a sentence', () => {
    // The second half of the same gate. Acceptance reasons are written as
    // `"reason": "<code>"` inside the four acceptance/finalization/settlement
    // leaves; the bypass family is a dict of literals in outcomes.py, four more
    // arrive through named constants there and the settlement leaf names its
    // own reason as a constant, so all three shapes are read explicitly.
    const pkg = new URL('../../ouroboros/', import.meta.url);
    const read = (name) => readFileSync(new URL(name, pkg), 'utf8');
    const decisions = [
        'loop_acceptance_review.py', 'loop_acceptance.py', 'loop_delivery.py', 'loop_forced_finalization.py',
        'acceptance_settlement.py',
    ].map(read).join('\n');
    const outcomes = read('outcomes.py');
    const acceptance = new Set([
        ...[...decisions.matchAll(/"reason":\s*(?:\n\s*)?"([a-z_]+)"/g)].map((m) => m[1]),
        ...[...outcomes.matchAll(/"(acceptance_bypassed_[a-z_]+)"/g)].map((m) => m[1]),
        ...[...outcomes.matchAll(
            /^REASON_(?:REVIEW_CYCLES_EXHAUSTED|IDENTICAL_ACCEPTANCE_REFUSED|ACCEPTANCE_REVIEW_SKIPPED_DEADLINE_RESERVE|ACCEPTANCE_SKIPPED_OWNER_HURRY) = "([a-z_]+)"$/gm,
        )].map((m) => m[1]),
        ...[...read('acceptance_settlement.py').matchAll(/^REASON_[A-Z_]+ = "([a-z_]+)"$/gm)].map((m) => m[1]),
    ]);
    assert.ok(acceptance.has('previous_revision_accepted'), 'the settlement leaf is scanned');
    // A CLEAN accepted decision renders no clause, the owner stop carries its
    // own marker instead, and queue_inspection_failed is a `{status, reason}`
    // probe shape rather than an acceptance reason.
    const exempt = new Set([
        'clean_pass', 'clean_pass_obligations_closed', 'queue_inspection_failed',
        'acceptance_bypassed_owner_requested_finalization',
    ]);
    assert.ok(acceptance.size >= 20, `expected the acceptance vocabulary, saw ${acceptance.size}`);
    for (const code of acceptance) {
        if (exempt.has(code)) continue;
        assert.notEqual(
            taskReasonPhrase(code), code,
            `no owner-facing sentence for acceptance reason "${code}" — add one to TASK_CAUSE_PHRASES`,
        );
    }
});

test('one status-word family: the card phase matches the host over the shared fixture', () => {
    // The same fixture is read by tests/test_project_plain_rows.py, so a
    // divergence between this severity fold and the host's durable label word
    // fails on both sides of the boundary.
    const fixture = JSON.parse(
        readFileSync(new URL('./fixtures/outcome_phase_parity.json', import.meta.url), 'utf8'),
    );
    assert.ok(fixture.cases.length >= 10);
    for (const { name, record, phase, headline, acceptance_clause: clause } of fixture.cases) {
        const resolved = taskDoneIsTerminal(record) ? taskTerminalPhase(record) : 'working';
        assert.deepEqual(taskPresentation(resolved), { phase, headline }, name);
        if (clause) {
            // The host composes the same sentence for its durable prose rows and
            // terminates it there; the card line adds no punctuation of its own.
            const detail = taskReasonDetail(record);
            assert.ok(clause === detail || clause === `${detail}.`, `${name}: ${detail} vs ${clause}`);
        }
    }
});

// A review-caused warning used to be explained by whatever execution reason sat
// beside it ('Reason: final_message'), which named the delivery step rather than
// the actual cause. The host's acceptance decision now speaks for itself.

const A4 = {
    status: 'completed',
    reason_code: 'final_message',
    outcome_axes: {
        execution: { status: 'ok' },
        review: {
            status: 'degraded',
            acceptance_decision: {
                status: 'finalized_unaccepted',
                reason: 'review_degraded',
                rationale: 'Acceptance reviewers did not reach a valid quorum.',
            },
        },
    },
};

test('an unaccepted decision explains the warning in its own words', () => {
    assert.equal(
        taskReasonDetail(A4),
        'The reviewers did not reach a verdict on this answer.',
    );
    assert.doesNotMatch(taskReasonDetail(A4), /final_message/);
    // The stored reviewer rationale belongs to the card body, the task result
    // and Logs; the row states one sentence and never the machine words.
    assert.doesNotMatch(taskReasonDetail(A4), /quorum|finalized_unaccepted/);
});

test('an accepted decision omits neutral final_message and preserves substantive reasons', () => {
    const accepted = {
        ...A4,
        outcome_axes: {
            execution: { status: 'ok' },
            review: { status: 'pass', acceptance_decision: { status: 'accepted', rationale: 'Quorum reached.' } },
        },
    };
    assert.equal(taskReasonDetail(accepted), '');
    assert.equal(taskReasonDetail({ ...accepted, reason_code: 'custom_reason' }), 'custom_reason');
});

test('a decision carrying no typed reason states no cause at all', () => {
    // A status word is not a cause, and the collapsed status is already the
    // headline; a historical record without a reason therefore says nothing.
    const record = {
        outcome_axes: { review: { acceptance_decision: { status: 'revision_requested' } } },
        status: 'completed',
    };
    assert.equal(taskReasonDetail(record), '');
});

test('a decision with no reason code still reaches the acceptance branch', () => {
    // The old single early return swallowed this frame before the branch.
    const record = {
        status: 'completed',
        review_status: { acceptance_decision: { status: 'revision_requested', reason: 'owner_followup' } },
    };
    assert.equal(taskReasonDetail(record), 'A new message from you arrived, so the review was set aside for it.');
});

test('a hard failure keeps explaining itself by its execution reason', () => {
    // The row carries the debt the code names: the host renderer
    // (project_dialogue._completion_verdict) states this cause only while
    // delegated_runs_unreconciled is non-empty, because the debt heals from the
    // write side while the stored reason_code may not be rewritten. The fixture
    // keeps the two twins describing the same record.
    const failed = {
        ...A4, status: 'failed', reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: ['run-a1'],
    };
    assert.equal(taskReasonDetail(failed), 'Some delegated work was never reconciled.');
});

test('a stored rationale never reaches the row, however it is written', () => {
    // The rationale used to be flattened into the line; it is free reviewer
    // text up to 500 characters and belongs where the full copy lives.
    const noisy = {
        status: 'completed',
        outcome_axes: {
            review: {
                acceptance_decision: {
                    status: 'revision_requested', reason: 'evidence_refresh',
                    rationale: 'Two\n\nlines   here.',
                },
            },
        },
    };
    assert.equal(
        taskReasonDetail(noisy),
        'The work changed after the review was frozen, so it no longer covered the answer.',
    );
    assert.doesNotMatch(taskReasonDetail(noisy), /lines/);
});

// The custody overlay stamps `delegated_custody_unreconciled` as the row's
// reason_code while a delegated run is still unreconciled. The debt then heals
// from the WRITE side while the stored code may not be rewritten, so the code
// outlives the fact. The host renderer already selects on the row's own debt
// list (project_dialogue._custody_debt_reason); these cases mirror
// tests/test_terminal_truth_projection_p5.py so the card and the plain rows
// name the same cause on the same record.

test('a healed custody debt yields the current execution reason on the card', () => {
    assert.equal(taskReasonDetail({
        status: 'completed',
        reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: [],
        outcome_axes: { execution: { status: 'degraded', reason_code: 'tool_failure' } },
    }), 'A tool this task used failed and nothing recovered it');
});

test('an open custody debt is still named on the card', () => {
    assert.equal(taskReasonDetail({
        status: 'completed',
        reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: ['run-a1'],
        outcome_axes: { execution: { status: 'ok' } },
    }), 'Some delegated work was never reconciled.');
});

test('a real debt beside a real execution cause is one card line', () => {
    assert.equal(taskReasonDetail({
        status: 'failed',
        reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: ['run-a1', 'run-b2'],
        outcome_axes: { execution: { status: 'failed', reason_code: 'provider_unavailable' } },
    }), 'The model provider stopped answering, so the task could not finish · Some delegated work was never reconciled.');
});

// A LIVE task_done event carries the row's own debt list too
// (agent_task_pipeline._custody_debt_event_fields), so the card reads the SAME
// list the durable row holds and the stamped code is never a second source. A
// record that carries no readable list therefore states nothing about the debt,
// exactly as project_dialogue._custody_debt_reason reads the same record.

test('a live event carrying an open debt list names the debt through the list', () => {
    assert.equal(taskReasonDetail({
        status: 'completed',
        reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: ['run-a1'],
        outcome_axes: { execution: { status: 'ok', reason_code: 'tool_failure' } },
    }), 'A tool this task used failed and nothing recovered it · Some delegated work was never reconciled.');
});

test('a record carrying no debt list states nothing about the debt', () => {
    assert.equal(taskReasonDetail({
        status: 'failed',
        reason_code: 'delegated_custody_unreconciled',
        outcome_axes: { execution: { status: 'failed', reason_code: 'provider_unavailable' } },
    }), 'The model provider stopped answering, so the task could not finish');
    assert.equal(taskReasonDetail({
        status: 'completed',
        reason_code: 'delegated_custody_unreconciled',
        outcome_axes: { execution: { status: 'ok' } },
    }), '');
});

test('a debt list of an unreadable shape carries no readable debt', () => {
    // The host reads the same value the same way: only a list is a debt list.
    assert.equal(taskReasonDetail({
        status: 'completed',
        reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: 'run-a1',
        outcome_axes: { execution: { status: 'ok' } },
    }), '');
});

test('a healed debt with no execution cause states nothing on the card', () => {
    // The warn headline still comes from the frozen objective warning; inventing
    // a Reason line here is exactly the false statement this rule removes.
    assert.equal(taskReasonDetail({
        status: 'completed',
        reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: [],
        outcome_axes: { execution: { status: 'ok' } },
    }), '');
});

// The plan review's outcome CLASS at delivery (outcome_axes.execution.plan_review) picks the
// sentence for an open plan review; the stamped code stays `plan_review_advisory` on the
// record. A record naming no class keeps the general sentence; an unknown class word is
// never turned into a wrong sentence.

const openPlan = (plan_review, extra = {}) => ({
    status: 'completed', reason_code: 'plan_review_advisory', terminal_plan_review_open: true,
    outcome_axes: { execution: { status: 'degraded', reason_code: 'plan_review_advisory', ...(plan_review ? { plan_review } : {}) } },
    ...extra,
});

test('the plan review class picks the owner sentence for an open plan review', () => {
    assert.equal(taskReasonDetail(openPlan('unanswered')),
        'Only some of the plan reviewers answered; the work went on with their notes.');
    assert.equal(taskReasonDetail(openPlan('none_answered')),
        'None of the plan reviewers answered; the work went on without their notes.');
    assert.equal(taskReasonDetail(openPlan('answered_open')),
        'The plan reviewers answered, but the review was never closed; the work went on with their notes.');
    // Legacy rows without a class, and a class nobody has a sentence for, keep the general sentence.
    assert.equal(taskReasonDetail(openPlan('')),
        'The plan review was never closed; the work went on with what the reviewers said.');
    assert.equal(taskReasonDetail(openPlan('some_future_class')),
        'The plan review was never closed; the work went on with what the reviewers said.');
    // The class is a plan-review fact: another execution reason is never reworded by it, and the
    // class states the standing limitation beside that reason (it rides the live event and the
    // replayed row where the result-only flag does not); a record with neither stays silent.
    assert.equal(taskReasonDetail({
        status: 'completed', reason_code: 'budget_exhausted',
        outcome_axes: { execution: { status: 'degraded', reason_code: 'budget_exhausted', plan_review: 'unanswered' } },
    }), 'The task ran out of budget before it could finish cleanly · Only some of the plan reviewers answered; the work went on with their notes.');
    assert.equal(taskReasonDetail({
        status: 'completed', reason_code: 'budget_exhausted',
        outcome_axes: { execution: { status: 'degraded', reason_code: 'budget_exhausted' } },
    }), 'The task ran out of budget before it could finish cleanly');
});

test('a held task states why it was held and never that the work went on', () => {
    const held = (source, reason) => taskReasonDetail({
        status: 'completed', reason_code: 'final_message',
        outcome_axes: { execution: { status: 'ok' }, review: { status: 'skipped' },
            objective: { status: 'fail', source, reason, outcome_tier: 'blocked_with_evidence' } },
    });
    assert.equal(held('plan_review_quorum_unreachable', 'plan_review_quorum_unreachable'),
        'Too few plan reviewers could answer, so the work was held.');
    assert.equal(held('plan_review_cycles_exhausted', 'review_cycles_exhausted'),
        'The task used up its review rounds before the answer was signed off.');
    assert.equal(held('plan_review_author_stop', 'author_stop'),
        'Ouroboros stopped with unfinished work; no review approval was granted.');
    for (const line of [held('plan_review_quorum_unreachable', 'plan_review_quorum_unreachable')]) {
        assert.doesNotMatch(line, /went on/);
    }
    // A Failed card held by anything else keeps stating nothing on a neutral final_message.
    assert.equal(held('task_acceptance_review', 'review_fail'), '');
});

test('standing limitations are stated beside the primary cause, each once', () => {
    const deferred = {
        status: 'completed', reason_code: 'child_results_deferred', terminal_plan_review_open: true,
        outcome_axes: { execution: { status: 'degraded', reason_code: 'child_results_deferred' },
            objective: { status: 'best_effort', source: 'child_result_disposition', deferred_count: 2 } },
    };
    assert.equal(taskReasonDetail(deferred),
        'Some sub-task results were deferred instead of being folded into this answer · The plan review was still open when this answer was delivered');
    // The open plan review is worded by its class when the record names one; the plan clause is second.
    assert.equal(taskReasonDetail({ ...deferred, outcome_axes: { ...deferred.outcome_axes,
        execution: { ...deferred.outcome_axes.execution, plan_review: 'unanswered' } } }),
    'Some sub-task results were deferred instead of being folded into this answer · Only some of the plan reviewers answered; the work went on with their notes.');
    // A clause that is not last drops its own full stop; equivalent clauses state themselves once.
    assert.equal(taskReasonDetail(openPlan('unanswered', { delegated_runs_unreconciled: ['run-a1'] })),
        'Only some of the plan reviewers answered; the work went on with their notes.');
    assert.equal(taskReasonDetail({ ...openPlan('unanswered'), reason_code: 'delegated_custody_unreconciled',
        delegated_runs_unreconciled: ['run-a1'] }),
    'Only some of the plan reviewers answered; the work went on with their notes · Some delegated work was never reconciled.');
    // Without the flag or a deferred count the primary cause stands alone; an owner stop
    // contributes no primary cause but still carries its limitation.
    assert.equal(taskReasonDetail({ ...deferred, terminal_plan_review_open: false,
        outcome_axes: { execution: deferred.outcome_axes.execution } }),
    'Some sub-task results were deferred instead of being folded into this answer');
    assert.equal(taskReasonDetail({ ...deferred, reason_code: 'owner_requested_finalization' }),
        'Some sub-task results were deferred instead of being folded into this answer · The plan review was still open when this answer was delivered');
});


test('an unfinished child keeps the complete authored result and its stop cause on the collapsed line', () => {
    const result = 'Complete selected answer, including what has already been done.';
    const outcome_axes = { execution: { status: 'ok', task_completion: {
        action: 'stop', rationale: 'The **export** still fails on empty input.',
    } }, objective: { status: 'fail', source: 'task_completion', reason: 'author_stop' } };
    const view = summarizeChatLiveEvent({ type: 'send_message', is_progress: true, delegation_role: 'subagent',
        subagent_task_id: 'child', parent_task_id: 'root', subagent_event: 'completed',
        status: 'completed', reason_code: 'final_message', outcome_axes, result });
    assert.equal(view.phase, 'error');
    assert.equal(view.activityPreview, 'Ouroboros stopped with unfinished work; no review approval was granted · The export still fails on empty input.');
    assert.ok(view.fullBody.includes(result));
});
