import assert from 'node:assert/strict';
import test from 'node:test';
import {
    changeRouteChoice, composeModelSource, decodeRouteChoice, encodeRouteChoice,
    parseModelSource, routeChoiceGroups, routeModelFields, routeModelSuggestions,
    routeSupportsAccount, routeTargetFromModel, serializeRouteSpec,
} from '../modules/route_editor_primitives.js';
import {
    availableSubagentRowMarkup, buildAvailableSubagentsSetting,
    parseAvailableSubagentsSetting, validateAvailableSubagentsSetting,
} from '../modules/subagents_settings.js';
import {
    advisoryRouteTransition, buildReviewerSlotsSetting, deepReviewDeliveryNote,
    describeSubagentReference, pinnedAccountWarning, reviewerChoiceGroups,
} from '../modules/reviewer_slots.js';

const sources = [{ id: 'opaque-source', label: 'Subscription model', credentialHarness: 'codex' }];
const route = (kind = 'api_model', pin = 'personal') => ({
    kind, target_id: 'claudexor::opaque-source=gpt-test',
    [kind === 'api_model' ? 'credential_profile_id' : 'profile_id']: pin,
});
const actor = () => ({ subagent_id: 'native', recommended_use: 'Inspect the repository.', route: route() });
const roster = () => ({ enabled: true, items: [actor()] });

test('reviewer source roundtrips restore model/account without cross-source borrowing', () => {
    // The memory key is the FULL choice, provider included: a draft belongs to
    // the source it was typed for, and returning to THAT source restores it.
    const original = route('api_chat');
    const api = advisoryRouteTransition(original, { kind: 'api_chat', provider: 'openai' });
    assert.deepEqual(api.route, { kind: 'api_chat', target_id: 'openai::' });
    const edited = { ...api.route, target_id: 'openai::owner-choice' };
    const restored = advisoryRouteTransition(edited, { kind: 'api_chat', source: 'opaque-source' }, api.memory);
    assert.deepEqual(restored.route, original);
    const back = advisoryRouteTransition(restored.route, { kind: 'api_chat', provider: 'openai' }, restored.memory);
    assert.deepEqual(back.route, edited);
    // …while a provider that was never drafted starts empty rather than
    // inheriting the other provider's model.
    assert.deepEqual(advisoryRouteTransition(restored.route, { kind: 'api_chat' }, restored.memory).route,
        { kind: 'api_chat', target_id: '' });
});

test('source parsing is shared with Models and never assumes source id equals harness', () => {
    assert.deepEqual(parseModelSource(route().target_id), { source: 'subscription:opaque-source', model: 'gpt-test' });
    assert.equal(composeModelSource('subscription:opaque-source', 'gpt-test'), route().target_id);
    assert.equal(routeModelFields(route(), sources).harness, 'codex');
    assert.equal(routeModelFields(route(), []).harness, '');
    assert.equal(routeSupportsAccount(route()), true);
    assert.equal(routeSupportsAccount({ kind: 'api_model', target_id: 'openai::gpt-test' }), false);
});

test('each delivery owner retains its own kind and account field', () => {
    for (const kind of ['api_model', 'api_chat']) {
        const field = kind === 'api_model' ? 'credential_profile_id' : 'profile_id';
        assert.deepEqual(serializeRouteSpec(route(kind), { apiKind: kind, credentialField: field }), route(kind));
        assert.deepEqual(serializeRouteSpec({ ...route(kind), target_id: 'openai::gpt-test' },
            { apiKind: kind, credentialField: field }), { kind, target_id: 'openai::gpt-test' });
    }
    const parsed = parseAvailableSubagentsSetting(JSON.stringify(roster()));
    assert.equal(parsed.error, '');
    assert.deepEqual(buildAvailableSubagentsSetting(parsed.setting), roster());
    assert.deepEqual(validateAvailableSubagentsSetting(roster()), []);
});

test('subscription source choices round-trip and do not hide a saved undiscovered source', () => {
    assert.equal(encodeRouteChoice({ route: route() }), 'subscription:opaque-source');
    assert.deepEqual(decodeRouteChoice('subscription:opaque-source'), { kind: 'api_model', source: 'opaque-source' });
    const groups = routeChoiceGroups({ modelSources: sources, harnesses: [{ id: 'cursor' }] });
    assert.deepEqual(groups.map((group) => group.label), ['Subscriptions · models', 'API keys', 'Agents · sessions']);
    const missing = routeChoiceGroups({ currentChoice: 'subscription:removed', catalogKnown: true });
    assert.match(missing[0].options[0].label, /not checked/);
    assert.ok(reviewerChoiceGroups({ roster: roster().items, modelSources: sources })
        .flatMap((group) => group.options).some((option) => option.value === 'subscription:opaque-source'));
});

test('model and account edits retain native versus packed delivery', () => {
    for (const kind of ['api_model', 'api_chat']) {
        const original = route(kind);
        assert.deepEqual(changeRouteChoice(original, 'subscription:opaque-source', { apiKind: kind }), original);
        assert.equal(routeTargetFromModel(original, 'gpt-next'), 'claudexor::opaque-source=gpt-next');
        assert.equal(routeTargetFromModel(original, ''), 'claudexor::opaque-source=');
        assert.deepEqual(changeRouteChoice(original, 'subscription:other', { apiKind: kind }),
            { kind, target_id: 'claudexor::other=' });
        assert.deepEqual(changeRouteChoice(original, 'api', { apiKind: kind }), { kind, target_id: '' });
    }
    assert.ok(validateAvailableSubagentsSetting({ ...roster(), items: [
        { ...actor(), route: { ...route(), target_id: 'claudexor::opaque-source=' } },
    ] }).length);
});

test('catalog suggestions are source-scoped without borrowing session inventory', () => {
    assert.deepEqual(routeModelSuggestions(route(), [
        'claudexor::opaque-source=gpt-test', 'openai::api-only', 'claudexor::other=not-this-source',
    ]), ['gpt-test']);
});

test('all reviewer categories preserve subscription pins while references remain references', () => {
    const inline = { route: route('api_chat'), effort: 'high' };
    const result = JSON.parse(buildReviewerSlotsSetting({
        triad: [{ ...inline, slot_id: 'triad_1' }],
        scope: [{ slot_id: 'scope_1', subagent_id: 'native', route: route('api_chat') }],
        advisory: { ...inline, enabled: true },
        deepReview: { subagent_id: 'native', route: route('api_chat'), materialized: true },
    }));
    assert.equal(result.triad[0].route.profile_id, 'personal');
    assert.equal(result.advisory.route.profile_id, 'personal');
    assert.deepEqual(result.scope[0], { slot_id: 'scope_1', subagent_id: 'native' });
    assert.deepEqual(result.deep_review, { subagent_id: 'native' });
    assert.match(describeSubagentReference('native', roster().items), /account personal/);
    assert.match(deepReviewDeliveryNote({ subagent_id: 'native' }, { roster: roster().items }), /Native inspection episode/);
    const deepInline = JSON.parse(buildReviewerSlotsSetting({ deepReview: inline }));
    assert.equal(deepInline.deep_review.route.profile_id, 'personal');
});

test('raw subscription advisory source changes reset only source-bound knobs', () => {
    const previous = route('api_chat');
    assert.deepEqual(advisoryRouteTransition(previous, { kind: 'api_chat', source: 'opaque-source' }).route, previous);
    assert.deepEqual(advisoryRouteTransition(previous, { kind: 'api_chat', source: 'other' }).route,
        { kind: 'api_chat', target_id: 'claudexor::other=' });
});

test('unknown source mapping does not invent a missing-account verdict', () => {
    const args = { triad: [{ route: route('api_chat') }], accountsKnown: true };
    assert.equal(pinnedAccountWarning(args), '');
    assert.match(pinnedAccountWarning({ ...args, modelSources: sources }), /codex · personal/);
    assert.equal(pinnedAccountWarning({ ...args, modelSources: sources,
        profilesByHarness: { codex: ['personal'] } }), '');
});

test('actor subscription controls use the mapped account family and preserve unlisted pins', () => {
    const state = { catalogKnown: true, accountsKnown: true, modelSources: sources,
        apiModels: [route().target_id], snapshot: { harnesses: [], profiles: { profiles: [
            { profile: { harness_id: 'codex', profile_id: 'work' } },
        ] } } };
    const html = availableSubagentRowMarkup(actor(), state);
    assert.match(html, /data-subagent-field="account"/);
    assert.match(html, /value="work"/);
    assert.match(html, /value="personal" selected/);
    assert.match(html, /value="gpt-test"/);
    assert.match(html, /value="subscription:opaque-source" selected/);
    // The chip names the SOURCE and wears its CREDENTIAL HARNESS's mark — the
    // opaque source id is never split as if it were a session target.
    assert.match(html, /data-harness-identity="codex"/);
    assert.match(html, />Subscription model · model<\/span>/);
    const unknown = availableSubagentRowMarkup(actor(), { ...state, modelSources: [] });
    assert.match(unknown, /personal \(not checked\)/);
    // With no catalog to map the id, the chip falls back to the source id
    // itself and still never claims a harness it could not resolve.
    assert.match(unknown, /data-harness-identity="opaque-source"/);
});
