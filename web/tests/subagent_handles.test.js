// A roster row is NAMED by a projection of its route. The Python owner is
// ouroboros/configured_subagents.py; this module pins the JS twin against the
// SAME table (tests/test_subagent_handles.py reads it too) and the one place
// the owner meets the name: the Review-lanes reviewer picker.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { rosterHandles, sameEngineAs, selectHtml, subagentHandle } from '../modules/route_editor_primitives.js';
import {
    SUBAGENT_CHOICE_PREFIX, encodeReviewerChoice, reviewerChoiceGroups,
} from '../modules/reviewer_slots.js';

const PARITY = JSON.parse(readFileSync(
    new URL('./fixtures/subagent_handle_parity.json', import.meta.url), 'utf8')).rosters;

for (const roster of PARITY) {
    test(`handle parity with Python: ${roster.case}`, () => {
        const inherited = roster.global_processing;
        const labels = rosterHandles(roster.items, inherited);
        roster.expected.forEach((want, index) => {
            const row = roster.items[index];
            assert.equal(subagentHandle(row, inherited), want.handle);
            assert.equal(labels.get(row.subagent_id), want.roster);
            assert.equal(sameEngineAs(roster.items, index, inherited), want.same_engine_as ?? -1);
        });
    });
}

// The exact path reviewerPickerHtml takes: groups -> the shared select markup.
function pickerOptions(roster, row, processingPreference = '') {
    const html = selectHtml('data-slot-route', reviewerChoiceGroups({ roster, row, processingPreference }), encodeReviewerChoice(row));
    const group = html.match(/<optgroup label="Available subagents">([\s\S]*?)<\/optgroup>/)?.[1] || '';
    return [...group.matchAll(/<option value="([^"]*)"( selected)?>([^<]*)<\/option>/g)]
        .map((match) => ({ value: match[1], selected: Boolean(match[2]), label: match[3] }));
}

function rosterOf(count) {
    const efforts = ['', 'low', 'medium', 'high', 'xhigh'];
    return Array.from({ length: count }, (_, index) => (index % 2
        ? { subagent_id: `fast-scout_copy_${index}`, recommended_use: `Session notes ${index}`,
            route: { kind: 'agent_session', target_id: `codex=gpt-6-astra-${index}` },
            ...(efforts[index % 5] ? { effort: efforts[index % 5] } : {}) }
        : { subagent_id: index ? `fast-scout_copy_${index}` : 'fast-scout', recommended_use: '',
            route: { kind: 'api_model', target_id: `x-ai/grok-4.6-${index}` },
            ...(efforts[index % 5] ? { effort: efforts[index % 5] } : {}) }));
}

test('the reviewer picker names every row by its handle, identically with 1 row and with 10', () => {
    const ten = rosterOf(10);
    const row = { subagent_id: 'fast-scout', route: { kind: 'api_chat', target_id: '' } };
    const alone = pickerOptions(ten.slice(0, 1), row);
    const crowded = pickerOptions(ten, row);

    assert.deepEqual(alone, [{ value: `${SUBAGENT_CHOICE_PREFIX}fast-scout`, selected: true, label: 'x-ai/grok-4.6-0' }]);
    assert.equal(crowded.length, 10);
    // Scale invariant: a row's label is byte-identical however many siblings it has.
    assert.deepEqual(crowded[0], alone[0]);
    crowded.forEach((option, index) => {
        assert.equal(option.value, `${SUBAGENT_CHOICE_PREFIX}${ten[index].subagent_id}`, 'the stored id is the VALUE only');
        assert.ok(option.label.startsWith(subagentHandle(ten[index])), option.label);
        assert.doesNotMatch(option.label, /#|fast-scout|_copy_/, 'no stored label reaches the owner');
        assert.ok(option.label.length <= 80, `one compact line per row: ${option.label}`);
    });
    assert.equal(crowded[3].label, 'codex=gpt-6-astra-3/high — Session notes 3');
});

test('twins saved before the uniqueness rule stay distinguishable in the picker', () => {
    const twins = [
        { subagent_id: 'fast-scout', recommended_use: '', route: { kind: 'api_model', target_id: 'x-ai/grok-4.6' } },
        { subagent_id: 'fast-scout_copy_a1', recommended_use: '', route: { kind: 'api_model', target_id: 'x-ai/grok-4.6' } },
    ];
    const labels = pickerOptions(twins, { subagent_id: 'fast-scout' }).map((option) => option.label);
    assert.deepEqual(labels, ['x-ai/grok-4.6~fast-scout', 'x-ai/grok-4.6~fast-scout_copy_a1']);
});

test('the picker shows the EFFECTIVE name: an inherited fast is said, the standard baseline is not', () => {
    const roster = [
        { subagent_id: 'a', recommended_use: '', route: { kind: 'api_model', target_id: 'x-ai/grok-4.6' } },
        { subagent_id: 'b', recommended_use: '', route: { kind: 'api_model', target_id: 'openai/gpt-5.6-sol' }, processing_preference: 'standard' },
    ];
    const labels = (inherited) => pickerOptions(roster, { subagent_id: 'a' }, inherited).map((option) => option.label);
    assert.deepEqual(labels(''), ['x-ai/grok-4.6', 'openai/gpt-5.6-sol']);
    assert.deepEqual(labels('standard'), ['x-ai/grok-4.6', 'openai/gpt-5.6-sol']);
    assert.deepEqual(labels('fast'), ['x-ai/grok-4.6/fast', 'openai/gpt-5.6-sol']);
});

test('a switched-off row leaves new choices, keeps its name where it is still referenced, and never renames a neighbour', () => {
    const roster = [
        { subagent_id: 'fast-scout', recommended_use: 'Scouts', route: { kind: 'api_model', target_id: 'x-ai/grok-4.6' } },
        { subagent_id: 'fast-scout_copy_a1', recommended_use: 'Parked', enabled: false,
          route: { kind: 'api_model', target_id: 'moonshotai/kimi-k3' }, effort: 'high' },
    ];
    // Not offered for a NEW choice; the enabled neighbour reads exactly as it does alone.
    assert.deepEqual(pickerOptions(roster, { subagent_id: 'fast-scout' }).map((option) => option.label),
        pickerOptions(roster.slice(0, 1), { subagent_id: 'fast-scout' }).map((option) => option.label));
    // Still referenced: the option survives, named by its handle, with the switch as a fact before the caption.
    const held = pickerOptions(roster, { subagent_id: 'fast-scout_copy_a1' });
    assert.deepEqual(held.map((option) => option.label),
        ['x-ai/grok-4.6 — Scouts', 'moonshotai/kimi-k3/high · switched off — Parked']);
    assert.equal(held[1].selected, true);
    assert.doesNotMatch(held[1].label, /fast-scout|#/);
});
