// Pure presentation helpers for the sidebar activity dots.
//
// The server's active_chat_activities census is the only source of liveness.
// This module deliberately owns no timer, request, socket or durable state. A
// caller supplies the previous projection when reconciling a partial census.

import { activeModelWaits, mergeModelWaits } from './model_wait.js';
import { waitFacts } from './question_presentation.js';

const WORKING_PHASES = new Set(['thinking', 'working', 'finalizing']);
// budget_pausing: the task is still RUNNING but writing its exact pause record
// (#1196) — a stationary transition, never motion and never a queue.
const QUEUED_PHASES = new Set(['queued', 'budget_paused', 'budget_pausing']);

function activityId(row) {
    return String(row?.activity_id || '').trim();
}

function projectId(row) {
    return String(row?.project_id || '').trim();
}

function isChildActivity(row) {
    if (row?.is_child === true || row?.child === true) return true;
    const parent = String(row?.parent_task_id || '').trim();
    if (!parent) return false;
    const root = String(row?.root_task_id || '').trim();
    return !root || root !== activityId(row);
}

function waitingQuestion(row) {
    const question = row?.required_question;
    if (!question || typeof question !== 'object') return false;
    const state = question.quiz_state || question.state;
    return (!state || state === 'open') && waitFacts(question).waiting;
}

function waitingModel(row) {
    const waits = row?.model_waits;
    if (!waits || typeof waits !== 'object') return false;
    // The same admission rule the chat card applies: a malformed wait row is
    // dropped here exactly as there, so the sidebar cannot go static on a row
    // the card would never show.
    const admitted = mergeModelWaits({}, waits);
    return activeModelWaits(admitted, false, Number(row?.task_attempt) || 0).length > 0;
}

function waitLabels(row) {
    const labels = [];
    if (row.project_admission_hold?.label) labels.push(row.project_admission_hold.label);
    if (waitingModel(row)) labels.push('Waiting for access');
    if (waitingQuestion(row)) labels.push('Waiting for your answer');
    return labels;
}

function phaseLabel(phase) {
    if (phase === 'thinking') return 'Thinking';
    if (phase === 'working') return 'Working';
    if (phase === 'finalizing') return 'Finalizing';
    if (phase === 'queued') return 'Queued';
    if (phase === 'budget_paused') return 'Paused';
    if (phase === 'budget_pausing') return 'Pausing';
    return '';
}

/**
 * Summarize one or more census rows into one accessible marker state.
 *
 * The summary intentionally contains no counts. An independently working row
 * wins motion; a wait on that same row suppresses its motion while remaining in
 * the label so mixed projects keep both facts available to assistive
 * technology and the marker tooltip.
 */
export function summarizeProjectActivities(rows = []) {
    const phases = new Set();
    const waits = new Set();
    let motion = false;
    let unknown = false;
    for (const row of Array.isArray(rows) ? rows : []) {
        if (!row || typeof row !== 'object') continue;
        // A row whose owner-question detail the census could not read may be
        // blocked on an answer: it stays static unknown rather than moving.
        if (row._activityUnconfirmed || (row.required_question_unavailable === true && !row.project_admission_hold)) { unknown = true; continue; }
        const phase = String(row.phase || '').trim().toLowerCase();
        const rowWaits = waitLabels(row);
        if (row.project_admission_hold && /^budget_paus(ed|ing)$/.test(phase)) phases.add(phase);
        // A same-row wait supersedes its coarse queue/execution phase.
        if (!rowWaits.length) {
            if (WORKING_PHASES.has(phase) || QUEUED_PHASES.has(phase)) phases.add(phase);
            else unknown = true;
            motion ||= WORKING_PHASES.has(phase);
        }
        for (const label of rowWaits) waits.add(label);
    }

    const phaseParts = [];
    // Keep the strongest/most useful phase first while preserving a mixed
    // direct+managed fact when a project has both kinds of active turn.
    for (const phase of ['working', 'thinking', 'finalizing', 'queued', 'budget_pausing', 'budget_paused']) {
        if (phases.has(phase)) phaseParts.push(phaseLabel(phase));
    }
    if (unknown) phaseParts.push('Activity status unavailable');
    const parts = [...phaseParts, ...waits];
    const waiting = waits.size > 0 || phases.has('budget_paused') || phases.has('budget_pausing');
    const state = motion ? 'working' : waiting ? 'waiting'
        : phaseParts.some((part) => part === 'Queued' || part === 'Paused' || part === 'Pausing') ? 'queued'
            : phaseParts.length ? 'unknown' : 'idle';
    return {
        state,
        motion,
        waiting,
        label: parts.join(' · '),
    };
}

/** Build project and direct-conversation summaries from census rows. */
export function buildProjectActivityIndex(rows = []) {
    const byProjectRows = new Map();
    const seen = new Set();
    for (const row of Array.isArray(rows) ? rows : []) {
        const id = activityId(row);
        if (!id || seen.has(id) || isChildActivity(row)) continue;
        seen.add(id);
        const pid = projectId(row);
        if (pid) byProjectRows.set(pid, [...(byProjectRows.get(pid) || []), row]);
    }
    const byProject = new Map();
    for (const [pid, projectRows] of byProjectRows) {
        byProject.set(pid, summarizeProjectActivities(projectRows));
    }
    const aggregateSummary = summarizeProjectActivities([...byProjectRows.values()].flat());
    return {
        byProject,
        aggregate: aggregateSummary,
    };
}

/**
 * Reconcile a census against the previous rows. A complete supervisor-ready
 * response authorizes absence-based clearing. Partial, unavailable or
 * disconnected responses retain omitted rows as unknown, with no motion.
 * Positive rows in a partial census still describe their own observed state.
 */
export function reconcileProjectActivityCensus(previous = new Map(), data = {}) {
    const prior = previous instanceof Map ? previous : new Map();
    const incoming = data?.active_chat_activities;
    const complete = Array.isArray(incoming) && data.active_chat_activities_complete === true
        && data.supervisor_ready === true;
    const next = new Map();
    if (!complete) {
        for (const [id, row] of prior) next.set(id, { ...row, _activityUnconfirmed: true });
    }
    for (const row of Array.isArray(incoming) ? incoming : []) {
        const id = activityId(row);
        if (id && !isChildActivity(row)) next.set(id, { ...row, _activityUnconfirmed: false });
    }
    return {
        rows: next,
        complete,
    };
}
