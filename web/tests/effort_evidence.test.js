import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { summarizeLogEvent } from '../modules/log_events.js';

const paired = JSON.parse(readFileSync(new URL('../../tests/fixtures/claudexor_effort_resolution.json', import.meta.url)));
const variants = JSON.parse(readFileSync(new URL('../../tests/fixtures/claudexor_effort_resolution_variants.json', import.meta.url)));

for (const type of ['llm_usage', 'llm_round', 'llm_round_finished']) {
    test(`${type} separates effort preference, host carrier and absent report`, () => {
        const summary = summarizeLogEvent({ type, effort: {
            requested: 'ultra', sent: { reasoning_effort: 'max' }, sent_state: 'explicit', reported: null,
        } });
        assert.match(summary.body, /requested ultra/);
        assert.match(summary.body, /host sent .*max/);
        assert.match(summary.body, /reported unknown/);
        assert.doesNotMatch(summary.body, /applied/);
    });
}

test('Logs labels omission and a provider response echo with its source', () => {
    const summary = summarizeLogEvent({ type: 'llm_usage', effort: {
        requested: 'low', sent: {}, sent_state: 'omitted', reported: 'high', report_source: 'provider_applied_options',
    } });
    assert.match(summary.body, /host sent omitted/);
    assert.match(summary.body, /reported high \(provider_applied_options\)/);
    assert.equal(summarizeLogEvent({ type: 'llm_usage' }).body, '');
});

test('paired resolution fixture discloses preparation and keeps provider observation unknown', () => {
    const { body } = summarizeLogEvent({ type: 'llm_usage', effort_resolution: paired });
    assert.match(body, /prepared reasoning\.effort=xhigh \(downward; account_catalog\)/);
    assert.match(body, /reported unknown/);
    assert.match(body, /host sent unknown/);
    assert.doesNotMatch(body, /reported xhigh/);
});

test('provider observation uses its own reporter, not the resolution authority', () => {
    const { body } = summarizeLogEvent({ type: 'llm_usage', effort_resolution: {
        ...paired, observed: 'high', observedSource: 'provider_response',
    } });
    assert.match(body, /reported high \(provider_response\)/);
    assert.doesNotMatch(body, /reported high \(account_catalog\)/);
});

test('session settlement Logs use the final-attempt report and keep missing observation unknown', () => {
    const { body } = summarizeLogEvent({ type: 'delegate_run_settled', observed_attempt: { effort_resolution: paired } });
    assert.match(body, /Engine requested effort ultra/);
    assert.match(body, /prepared reasoning\.effort=xhigh/);
    assert.match(body, /reported unknown/);
});

for (const [name, report] of Object.entries(variants)) {
    for (const event of [
        { type: 'llm_usage', effort_resolution: report },
        { type: 'llm_round', usage: { effort_resolution: report } },
        { type: 'delegate_run_settled', observed_attempt: { effort_resolution: report } },
    ]) {
        test(`${event.type} preserves ${name} as engine preparation, without a provider echo`, () => {
            const { body } = summarizeLogEvent(event);
            const prepared = report.submitted === null ? 'omitted' : `${report.parameter}=${report.submitted}`;
            assert.equal(body, `Engine requested effort ${report.requested} · host sent unknown`
                + ` · prepared ${prepared} (${report.resolution}; ${report.source}) · reported unknown`);
        });
    }
}

for (const report of [undefined, null]) {
    test(`Logs keep absent or decoder-rejected evidence unknown (${report})`, () => {
        const { body } = summarizeLogEvent({ type: 'llm_usage', effort_resolution: report, effort: {
            requested: 'ultra', sent: { 'options.reasoningEffort': 'ultra' }, sent_state: 'explicit',
            reported: null, report_source: null,
        } });
        assert.match(body, /reported unknown/);
        assert.doesNotMatch(body, /prepared|reported ultra/);
        assert.equal(summarizeLogEvent({ type: 'delegate_run_settled',
            observed_attempt: { effort_resolution: report } }).body, '');
    });
}
