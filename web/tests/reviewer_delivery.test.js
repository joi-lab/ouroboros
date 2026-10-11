// #1334: a direct API triad row's saved delivery, and the untouched default panel.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
    DELIVERY_NATIVE,
    DELIVERY_PACKET,
    buildReviewerSlotsSetting,
    deliverySelectHtml,
    reviewerSlotsSavePayload,
} from '../modules/reviewer_slots.js';

const api = (slot_id, extra = {}) => ({ slot_id, route: { kind: 'api_chat', target_id: 'm/one' }, effort: '', ...extra });
const advisory = { enabled: true, route: { kind: 'api_chat', target_id: '' }, effort: 'low' };

test('a bare triad row stays bare and an explicit delivery is written only on direct API triad rows', () => {
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [api('bare'), api('native', { delivery: DELIVERY_NATIVE }), api('packet', { delivery: DELIVERY_PACKET }),
            { slot_id: 'sess', route: { kind: 'agent_session', target_id: 'codex' }, delivery: DELIVERY_NATIVE },
            { slot_id: 'ref', subagent_id: 'actor', delivery: DELIVERY_NATIVE }],
        scope: [api('scope', { delivery: DELIVERY_NATIVE })],
        advisory,
    }));
    assert.deepEqual(setting.triad.map((row) => row.delivery), [undefined, 'native', 'packet', undefined, undefined]);
    assert.equal('delivery' in setting.scope[0], false);
});

test('the delivery select shows a bare row as Packet without writing it', () => {
    assert.match(deliverySelectHtml('data-x', api('bare')), /<option value="packet" selected>Packet — for models without tool calling/);
    assert.match(deliverySelectHtml('data-x', api('n', { delivery: DELIVERY_NATIVE })), /<option value="native" selected>Reads the work itself/);
});

test('an untouched shipped default panel is not written by an unrelated save', () => {
    const view = { loaded: true, loadError: '', triad: [api('slot_1', { delivery: DELIVERY_NATIVE })], scope: [api('scope_slot_1')], advisory };
    const loadedSetting = buildReviewerSlotsSetting(view);
    assert.deepEqual(reviewerSlotsSavePayload({ ...view, source: 'default', loadedSetting }), {});
    // An edit writes the panel; a saved (structured) panel always round-trips.
    const edited = { ...view, triad: [api('slot_1', { delivery: DELIVERY_PACKET })] };
    assert.ok('OUROBOROS_REVIEWER_SLOTS' in reviewerSlotsSavePayload({ ...edited, source: 'default', loadedSetting }));
    assert.ok('OUROBOROS_REVIEWER_SLOTS' in reviewerSlotsSavePayload({ ...view, source: 'structured', loadedSetting }));
});

test('first default-panel materialization preserves the shown deep row; other saves keep omission', () => {
    const deepReview = { route: { kind: 'api_chat', target_id: 'openai-compatible::main' },
        processing_preference: 'priority', effort: '', materialized: false,
        synthesizedFrom: 'OUROBOROS_MODEL_DEEP_SELF_REVIEW' };
    const view = { loaded: true, source: 'default', triad: [api('t')], scope: [api('s')], advisory, deepReview };
    view.loadedSetting = buildReviewerSlotsSetting(view);
    assert.deepEqual(reviewerSlotsSavePayload(view), {});
    const edited = { ...view, triad: [api('t', { effort: 'high' })] };
    const saved = JSON.parse(reviewerSlotsSavePayload(edited).OUROBOROS_REVIEWER_SLOTS);
    assert.deepEqual(saved.deep_review, { route: deepReview.route, processing_preference: 'priority' });
    assert.equal(view.deepReview.materialized, false, 'collecting does not mutate the draft');
    for (const source of ['structured', '']) {
        assert.equal('deep_review' in JSON.parse(reviewerSlotsSavePayload({ ...edited, source })
            .OUROBOROS_REVIEWER_SLOTS), false, 'unknown-provenance panels and repair drafts retain omission');
    }
    const placeholder = { ...view, deepReview: { materialized: false } };
    assert.equal('deep_review' in JSON.parse(buildReviewerSlotsSetting(placeholder)), false);
});
