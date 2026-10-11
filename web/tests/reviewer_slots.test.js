import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { createClaudexorStatusStore } from '../modules/claudexor_status_store.js';
import { serviceBannerLine } from '../modules/harness_accounts.js';
import { renderSettingsPage } from '../modules/settings_ui.js';

import { configuredApiProviders } from '../modules/route_editor_primitives.js';
import {
    API_CHOICE_PREFIX,
    API_ROUTE_CHOICE,
    CATEGORIES,
    ROUTE_KIND_API,
    ROUTE_KIND_SESSION,
    advisoryRouteTransition,
    buildReviewerSlotsSetting,
    capabilityBadge,
    composeSessionTarget,
    decodeRouteChoice,
    deepReviewDeliveryNote,
    deepReviewMetaNotes,
    describeLastExecution,
    describeSubagentReference,
    encodeRouteChoice,
    harnessModelsKnown,
    mintSlotId,
    modelsGapNote,
    pinnedAccountWarning,
    profileOptionsFor,
    renderReviewerSlotsSection,
    repaintSignature,
    reviewerRouteIdentityMarkup,
    routeChoiceGroups,
    sessionModelOptions,
    sessionVerdictNote,
    splitSessionTarget,
    subagentOptionsFor,
    SUBAGENT_CHOICE_PREFIX,
    advisoryDeliveryNote,
    advisoryReferenceTransition,
    encodeReviewerChoice,
    lastRunMetaPrefix,
    lastRunRouteChanged,
    reviewerChoiceGroups,
    savedTargetNote,
} from '../modules/reviewer_slots.js';

test('reviewer routes reuse the shared visible harness identity without claiming execution', () => {
    const harnesses = {
        claude: { id: 'claude', display_name: 'Claude Code Max' },
    };
    const known = reviewerRouteIdentityMarkup({
        kind: ROUTE_KIND_SESSION,
        target_id: 'claude=claude-fable-5',
    }, harnesses, { catalogKnown: true });
    assert.match(known, /data-harness-identity="claude"/);
    // The chip names the SOURCE, not the channel alone (owner decision 4A).
    assert.match(known, />Claude Code Max · agent</);
    assert.match(known, /fill="currentColor"/);
    assert.doesNotMatch(known, /executed|status|available/i);

    const unread = reviewerRouteIdentityMarkup({
        kind: ROUTE_KIND_SESSION,
        target_id: 'claude=claude-fable-5',
    }, harnesses, { catalogKnown: false });
    assert.match(unread, />Claude Code · agent</);
    assert.doesNotMatch(unread, /Claude Code Max/);

    // An API row says WHICH provider serves it; an unprefixed id is OpenRouter.
    const api = reviewerRouteIdentityMarkup({ kind: ROUTE_KIND_API, target_id: 'openai/gpt' });
    assert.match(api, /data-presentation-kind="channel"/);
    assert.match(api, />API · OpenRouter</);
    assert.match(reviewerRouteIdentityMarkup({ kind: ROUTE_KIND_API, target_id: 'openai::gpt-5.6-terra' }),
        />API · OpenAI</);
    // A subscription row names its model source, never the aggregator.
    assert.match(reviewerRouteIdentityMarkup({ kind: ROUTE_KIND_API, target_id: 'claudexor::codex-models=gpt' },
        {}, { modelSources: [{ id: 'codex-models', label: 'Codex' }] }), />Codex · model</);
});


test('the category table is the one driver of the multi-row editor and matches its markup', () => {
    // ONE table replaced three `group === 'scope' ? … : …` ternaries (lookup,
    // add, remove): a new multi-row category is a table entry, and every id
    // the table names must exist in the static section it paints into.
    const markup = renderReviewerSlotsSection();
    assert.deepEqual(Object.keys(CATEGORIES), ['triad', 'scope']);
    for (const [group, cat] of Object.entries(CATEGORIES)) {
        assert.equal(cat.stateKey, group);
        assert.equal(cat.limitKey, group);
        for (const id of [cat.rowsId, cat.limitId, cat.addId]) {
            assert.match(markup, new RegExp(`id="${id}"`), `${group}: ${id} missing from the section markup`);
        }
        assert.ok(cat.idPrefix && cat.surfaceDefault && cat.empty, `${group}: incomplete table entry`);
    }
    assert.equal(CATEGORIES.scope.surfaceDefault, 'scope review effort');
    assert.equal(CATEGORIES.triad.surfaceDefault, 'review effort');
});

test('the deep self-review row rides the composed setting on the shared vocabulary (R6/R7)', () => {
    const base = {
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai/x' }, effort: '' }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, route: { kind: ROUTE_KIND_API, target_id: '' }, effort: 'low' },
    };
    // Legacy callers that know nothing about the singleton keep their bytes:
    // no `deep_review` key is ever invented.
    assert.equal('deep_review' in JSON.parse(buildReviewerSlotsSetting(base)), false);

    // A direct api row: the inspection episode. '' effort is OMITTED (the
    // Behavior-tab deep effort keeps deciding); the synthesized label and the
    // fixed identity never reach the saved bytes.
    const api = JSON.parse(buildReviewerSlotsSetting({
        ...base,
        deepReview: { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-sol-pro' }, effort: '',
                      subagent_id: '', synthesizedFrom: 'OUROBOROS_MODEL_DEEP_SELF_REVIEW' },
    }));
    assert.deepEqual(api.deep_review, { route: { kind: 'api_chat', target_id: 'openai/gpt-5.6-sol-pro' } });
    assert.doesNotMatch(JSON.stringify(api), /synthesized|slot_id":"deep|enabled":true,"route":\{"kind":"api_chat","target_id":"openai\/gpt-5\.6-sol-pro/);

    // A session row keeps its pin and an explicit effort.
    const session = JSON.parse(buildReviewerSlotsSetting({
        ...base,
        deepReview: { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'koshak' }, effort: 'xhigh', subagent_id: '' },
    }));
    assert.deepEqual(session.deep_review, {
        route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'koshak' }, effort: 'xhigh',
    });

    // A configured-subagent reference carries no route knobs (decision 5A).
    const ref = JSON.parse(buildReviewerSlotsSetting({
        ...base,
        deepReview: { subagent_id: 'deep-critic', route: { kind: ROUTE_KIND_API, target_id: 'stash' }, effort: '' },
    }));
    assert.deepEqual(ref.deep_review, { subagent_id: 'deep-critic' });
});

test('the deep self-review block states the delivery where the owner picks', () => {
    const markup = renderReviewerSlotsSection();
    assert.match(markup, /<h4[^>]*>Deep self-review<\/h4>/);
    assert.match(markup, /id="reviewer-deep-review-row"/);
    // Same delivery as the advisory: an API model runs the inspection episode.
    assert.match(markup, /Delivery is the same\s+as the advisory's/);
    assert.doesNotMatch(markup, /packed|Atlas/);
    assert.match(markup, /a model you name or a configured subagent/);
    assert.match(markup, /native\s+inspection episode with host-observed reads/);
    assert.match(markup, /core rules supplied inline/);
    assert.match(markup, /reference books available on demand/);
    assert.match(markup, /reads not host-observed/);
    assert.match(markup, /memory whitelist reaches the reviewer\s+inline byte-exact/);
    assert.match(markup, /outranks the Behavior-tab deep\s+self-review effort/);

    const roster = [
        { subagent_id: 'api-critic', route: { kind: 'api_model', target_id: 'openai/gpt-5.6-terra' } },
        { subagent_id: 'sess', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol' } },
    ];
    // A bare API route and a configured subagent on an API model say the SAME
    // thing, because they are the same delivery.
    assert.equal(deepReviewDeliveryNote({ route: { kind: ROUTE_KIND_API, target_id: 'openai/x' } }),
        deepReviewDeliveryNote({ subagent_id: 'api-critic' }, { roster }));
    assert.match(deepReviewDeliveryNote({ route: { kind: ROUTE_KIND_API, target_id: 'openai/x' } }), /Native inspection episode/);
    assert.match(deepReviewDeliveryNote({ route: { kind: ROUTE_KIND_API, target_id: 'openai/x' } }), /host-observed/);
    assert.match(deepReviewDeliveryNote({ subagent_id: 'api-critic' }, { roster }), /Native inspection episode/);
    assert.match(deepReviewDeliveryNote({ subagent_id: 'api-critic' }, { roster }), /host-observed/);
    assert.match(deepReviewDeliveryNote({ subagent_id: 'api-critic' }, { roster }), /memory whitelist reaches it inline byte-exact/);
    assert.match(deepReviewDeliveryNote({ subagent_id: 'sess' }, { roster }), /Agent session .* not host-observed/);
    const direct = deepReviewDeliveryNote({ route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } }, { harnesses: { codex: { status: 'ok' } } });
    assert.match(direct, /agent session — retrieves context with its own tools · route ok — reads not host-observed/);
    // Absence claims follow provenance, as everywhere in this editor.
    assert.match(deepReviewDeliveryNote({ subagent_id: 'gone' }, { roster }), /none exists with this ID/);
    assert.match(deepReviewDeliveryNote({ subagent_id: 'gone' }, { roster: [], rosterKnown: false }), /could not be read/);
});

test('the scope group states the delivery and asks for no window confirmation', () => {
    // Owner decision (2026-09-17): a scope reviewer's context window is not a
    // condition of its authority, so the note must not promise authority "once
    // that agent's context window is confirmed at 200K or more" — the owner has no
    // window to confirm and no place to confirm it. Both scope deliveries retrieve;
    // what differs is who executed the reads, which is what the note now says.
    const markup = renderReviewerSlotsSection();
    const scopeAt = markup.indexOf('class="reviewer-slots-heading">Scope slots');
    const note = markup.slice(scopeAt, markup.indexOf('id="reviewer-scope-rows"'));
    assert.ok(scopeAt > 0, 'the Scope slots group exists');
    assert.match(note, /Every scope row reads the repository itself/);
    assert.match(note, /bounded inspection episode with host read-only tools/);
    assert.match(note, /recovered from the harness run journal/);
    assert.doesNotMatch(markup, /200K|200,000/);
    assert.doesNotMatch(markup, /context window is\s+confirmed/);
    assert.doesNotMatch(markup, /does not attest which files the agent opened/);
});

test('the Models tab no longer authors the deep self-review model (R7)', () => {
    // The row lives in Review lanes; the key survives only as the backend's
    // invisible migration source, so no Settings control writes it.
    const page = renderSettingsPage();
    assert.doesNotMatch(page, /s-deep-self-review-model|Deep Self-Review Model/);
    assert.match(page, /id="s-websearch-model"/, 'the sibling field in Other Model Slots stays');
    assert.match(page, /id="s-effort-deep-self-review"/, 'the Behavior-tab surface effort stays (the row effort outranks it)');
});

test('the standing note states the POLICY, never the current routing', () => {
    // The section's inline note is STATIC markup: it renders identically for an
    // owner with three subscriptions and for one with none, whose every row is
    // an API model. It used to open "Commit and scope review run on
    // subscriptions and never fall back to API spend", so the second owner read
    // Settings and believed their commit reviews were spending subscription
    // windows and would wait for capacity — while in fact they were spending
    // API budget on the next commit (BIBLE P1). The only conditional sentence
    // lives server-side: `reviewer_slot_config.reviewer_slot_save_check` returns
    // the one-time R12 disclosure when a save first gives the triad a retrieving
    // row; a static copy cannot carry that condition, so this note must state
    // the rule instead of the situation.
    const markup = renderReviewerSlotsSection();

    // The section ships with NO rows — they are painted from the saved setting
    // at runtime — so this is exactly the markup an all-API owner sees.
    assert.doesNotMatch(markup, /data-route-kind|session:/,
        'the static section must not ship a pre-rendered row');

    assert.match(markup, /Rows routed to a subscription never fall back to API spend/);
    assert.match(markup, /waits for capacity/);
    // The unconditional claim, in the shapes it could come back as.
    assert.doesNotMatch(markup, /review runs? on subscriptions/i);
    assert.doesNotMatch(markup, /reviews? run on your subscription/i);
    assert.match(markup, /skill\s+review and task acceptance all follow their configured rows/i);
    // Owner R2 (2026-09-01): task acceptance follows the triad rows on their own
    // delivery — the former "remains API-only" pin must not come back in any spelling.
    assert.match(markup, /task acceptance runs\s+the triad rows on their own delivery/i);
    assert.doesNotMatch(markup, /API-only|shipped defaults when none remain/i);
});


test('each group carries its own Add action in its head, above the rows it adds to', () => {
    // docs/DESIGN.md "List editors": a group's add action lives in its head,
    // never in a footer toolbar under the rows, and the new row lands at the
    // group's end, revealed by `revealNewRow`.
    const markup = renderReviewerSlotsSection();
    for (const [head, rows, button] of [
        ['Triad slots', 'reviewer-triad-rows', 'btn-add-triad-slot'],
        ['Scope slots', 'reviewer-scope-rows', 'btn-add-scope-slot'],
    ]) {
        const headingAt = markup.indexOf(`class="reviewer-slots-heading">${head}`);
        assert.ok(headingAt > 0, `${head} heading exists`);
        const headOpen = markup.lastIndexOf('<div class="reviewer-slots-head">', headingAt);
        const headClose = markup.indexOf('</div>', headingAt);
        assert.ok(headOpen > 0 && headOpen < headingAt, `${head} heading sits inside a group head`);
        assert.match(markup.slice(headOpen, headClose), new RegExp(`id="${button}"`),
            `${button} lives in the ${head} head`);
        assert.ok(headClose < markup.indexOf(`id="${rows}"`), `${head} head precedes its rows`);
    }
    assert.doesNotMatch(markup, /settings-toolbar/, 'no footer Add toolbar remains');
});

test('a saved account pin survives a discovery list that no longer contains it', () => {
    // The select's value must EXIST as an option or the browser silently selects the
    // first one — "automatic rotation" — so a row pinned to one account redrew as
    // unpinned whenever the daemon was down or that account was signed out. Nothing
    // looked wrong, and saving the panel made the widening real.
    const discovered = profileOptionsFor(['koshak', 'valentine'], 'koshak');
    assert.deepEqual(discovered.map((o) => o.value), ['', 'koshak', 'valentine']);

    const undiscovered = profileOptionsFor(['valentine'], 'koshak');
    assert.deepEqual(undiscovered.map((o) => o.value), ['', 'valentine', 'koshak']);
    assert.match(undiscovered[2].label, /not in discovery/);

    // Discovery empty entirely (daemon down) is the SAME case, not a special one.
    assert.deepEqual(profileOptionsFor([], 'koshak').map((o) => o.value), ['', 'koshak']);
    // No pin: nothing invented, and the rotation entry stays the only default.
    assert.deepEqual(profileOptionsFor([], '').map((o) => o.value), ['']);
    assert.deepEqual(profileOptionsFor(null, '').map((o) => o.value), ['']);
});

test('a disabled account is offered with a "(disabled)" label, still selectable', () => {
    // The index carries {id, enabled} entries; the shared builder says the
    // fact instead of offering a disabled account bare. The option stays
    // SELECTABLE: the engine's typed refusal is the authority on a pin it
    // will not serve (D-U6) — this is honesty, not a client-side gate.
    const options = profileOptionsFor([
        { id: 'koshak', enabled: true },
        { id: 'retired', enabled: false },
    ], '');
    assert.deepEqual(options.map((o) => o.value), ['', 'koshak', 'retired']);
    assert.equal(options[1].label, 'Account: koshak (pinned)');
    assert.equal(options[2].label, 'Account: retired (pinned) (disabled)');
    assert.ok(options.every((o) => !o.disabled), 'every account stays selectable');
});

test('an account is offered under the name the Accounts tab gives it', () => {
    // The index carries the Accounts-tab name beside the id, so the migrated
    // default login — named by its login email there — stopped reading as a
    // missing account here. The id still rides the label (it is what the
    // setting stores) and stays the option VALUE.
    const options = profileOptionsFor([
        { id: 'codex-default', enabled: true, name: 'owner@example.com' },
        { id: 'koshak', enabled: true, name: 'koshak' },
        'koshak',
    ], '');
    assert.deepEqual(options.map((o) => o.value), ['', 'codex-default', 'koshak', 'koshak']);
    assert.equal(options[1].label, 'Account: owner@example.com · codex-default (pinned)');
    // A name that IS the id says it once.
    assert.equal(options[2].label, 'Account: koshak (pinned)');
    // A plain id string carries no name of its own and reads unchanged.
    assert.equal(options[3].label, 'Account: koshak (pinned)');

    const disabled = profileOptionsFor([
        { id: 'codex-default', enabled: false, name: 'owner@example.com' },
    ], '');
    assert.equal(disabled[1].label,
        'Account: owner@example.com · codex-default (pinned) (disabled)');
});

test('the provider shown for a delegated row is the harness name, never Claudexor', () => {
    const groups = routeChoiceGroups({
        harnesses: [{ id: 'codex', display_name: 'Codex CLI', status: 'ok', enabled: true }],
        providers: [{ id: 'openrouter', label: 'OpenRouter' }],
    });
    const flat = JSON.stringify(groups);
    assert.ok(flat.includes('Codex CLI'));
    assert.ok(!/claudexor/i.test(flat), 'the aggregator brand must not appear as a provider');
    // The route select carries SOURCES only (finding #6): the subscription
    // models, one entry per API provider with a stored key, one entry per
    // harness — never the flat model catalog. Every group is labeled.
    assert.deepEqual(groups.map((group) => group.label),
        ['Subscriptions · models', 'API keys', 'Agents · sessions']);
    assert.deepEqual(groups[1].options,
        [{ value: `${API_CHOICE_PREFIX}openrouter`, label: 'OpenRouter' },
         { value: '', disabled: true, label: 'Add a key in Accounts for more' }]);
    assert.equal(groups[2].options[0].value, 'session:codex');
    // The legacy bare choice is still ACCEPTED as input; nothing emits it.
    assert.deepEqual(decodeRouteChoice(API_ROUTE_CHOICE), { kind: ROUTE_KIND_API, provider: 'openrouter' });
});

test('the API keys group lists only providers this install has a credential for', () => {
    // Decision 2=A: an unusable provider is not offered, the group always says
    // where keys are added, and a saved provider whose key is gone stays
    // SELECTABLE and labelled — a save must not silently re-home the lane.
    const providers = configuredApiProviders({ OPENROUTER_API_KEY: 'k', OPENAI_API_KEY: '***set***' });
    const apiOptions = (args) => routeChoiceGroups({ providers, ...args })
        .find((group) => group.label === 'API keys').options;
    assert.deepEqual(apiOptions({}).map((option) => option.label),
        ['OpenRouter', 'OpenAI', 'Add a key in Accounts for more']);
    assert.equal(apiOptions({}).at(-1).disabled, true);
    assert.deepEqual(routeChoiceGroups({}).find((group) => group.label === 'API keys').options,
        [{ value: '', disabled: true, label: 'Add a key in Accounts for more' }]);

    const keyless = apiOptions({ currentChoice: `${API_CHOICE_PREFIX}anthropic` });
    const saved = keyless.find((option) => option.value === `${API_CHOICE_PREFIX}anthropic`);
    assert.equal(saved.label, 'Anthropic (no key)');
    assert.ok(!saved.disabled, 'the saved choice stays selectable');
    // The owner never sees the stored separator in any option value.
    for (const group of routeChoiceGroups({ providers, modelSources: [{ id: 'codex-models' }], harnesses: [{ id: 'codex' }] })) {
        for (const option of group.options) assert.ok(!option.value.includes('::'), option.value);
    }
});

test('a saved session route survives a discovery list that no longer contains its harness', () => {
    // Same rule as profileOptionsFor: the select's value must EXIST as an
    // option or the browser silently redraws the row as the first choice.
    const groups = routeChoiceGroups({ harnesses: [{ id: 'codex' }], currentChoice: 'session:claude' });
    const session = groups[2].options;
    assert.deepEqual(session.map((o) => o.value), ['session:codex', 'session:claude']);
    assert.match(session[1].label, /not in discovery/);
    // A choice discovery DOES list gains no duplicate.
    const listed = routeChoiceGroups({ harnesses: [{ id: 'codex' }], currentChoice: 'session:codex' });
    assert.deepEqual(listed[2].options.map((o) => o.value), ['session:codex']);
});

test('no :: syntax anywhere in encoded choices or composed targets', () => {
    const groups = routeChoiceGroups({
        harnesses: [{ id: 'codex' }, { id: 'claude' }],
    });
    for (const group of groups) {
        for (const option of group.options) {
            assert.ok(!option.value.includes('::'), option.value);
        }
    }
    assert.equal(composeSessionTarget('codex', 'gpt-5.6-sol'), 'codex=gpt-5.6-sol');
    assert.equal(composeSessionTarget('codex', ''), 'codex');
});

test('route choice round-trips through encode/decode', () => {
    // The API choice no longer carries the model id — the free-text input
    // does — so encode collapses every api row to its PROVIDER's option, and a
    // fresh row (target '') displays exactly that option, not the first
    // catalog model (finding #6c).
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' } };
    assert.equal(encodeRouteChoice(apiRow), `${API_CHOICE_PREFIX}openrouter`);
    assert.equal(encodeRouteChoice({ route: { kind: ROUTE_KIND_API, target_id: '' } }), `${API_CHOICE_PREFIX}openrouter`);
    assert.equal(encodeRouteChoice({ route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } }), `${API_CHOICE_PREFIX}openai`);
    assert.deepEqual(decodeRouteChoice(encodeRouteChoice(apiRow)),
        { kind: ROUTE_KIND_API, provider: 'openrouter' });
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol' } };
    assert.deepEqual(decodeRouteChoice(encodeRouteChoice(sessionRow)),
        { kind: ROUTE_KIND_SESSION, harness: 'codex' });
    assert.deepEqual(splitSessionTarget('codex=gpt-5.6-sol'),
        { harness: 'codex', model: 'gpt-5.6-sol' });
});

test('advisory route switching never wipes a stored target (finding #7c)', () => {
    // Saved: api with an explicit target. Flip to a session and back: the
    // saved api target is restored, not written to ''.
    const savedApi = { kind: ROUTE_KIND_API, target_id: 'anthropic/claude-opus-5' };
    let memory = { api: { ...savedApi }, session: null };
    const toSession = advisoryRouteTransition(savedApi, { kind: ROUTE_KIND_SESSION, harness: 'codex' }, memory);
    assert.deepEqual(toSession.route, { kind: ROUTE_KIND_SESSION, target_id: 'codex' });
    const back = advisoryRouteTransition(toSession.route, { kind: ROUTE_KIND_API }, toSession.memory);
    assert.deepEqual(back.route, savedApi);

    // Saved: session with a model spec. Kind round-trip restores the FULL
    // spec (harness=model), not the bare harness.
    const savedSession = { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-opus-5' };
    memory = { api: null, session: { ...savedSession } };
    const toApi = advisoryRouteTransition(savedSession, { kind: ROUTE_KIND_API }, memory);
    assert.deepEqual(toApi.route, { kind: ROUTE_KIND_API, target_id: '' });
    const restored = advisoryRouteTransition(toApi.route, { kind: ROUTE_KIND_SESSION, harness: 'claude' }, toApi.memory);
    assert.deepEqual(restored.route, savedSession);

    // Re-selecting the CURRENT kind/harness is a no-op, never a reset.
    const noop = advisoryRouteTransition(savedSession, { kind: ROUTE_KIND_SESSION, harness: 'claude' }, memory);
    assert.deepEqual(noop.route, savedSession);
});

test('the composed setting carries stable ids, per-row routes/efforts and the optional pin', () => {
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [
            { slot_id: 't_api', route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' }, effort: 'high' },
            { slot_id: 't_sess', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'koshak' }, effort: '' },
        ],
        scope: [{ slot_id: 's_1', route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-terra' }, effort: 'xhigh' }],
        advisory: { enabled: false, route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' }, effort: 'low' },
    }));
    assert.deepEqual(setting.triad[0], {
        slot_id: 't_api',
        route: { kind: 'api_chat', target_id: 'openai/gpt-5.6-luna' },
        effort: 'high',
    });
    // '' effort means "surface default" and is OMITTED, never written as ''.
    assert.equal('effort' in setting.triad[1], false);
    // The optional manual pin (Q2-в) rides only when set; rotation is default.
    assert.equal(setting.triad[1].route.profile_id, 'koshak');
    assert.equal('profile_id' in setting.scope[0].route, false);
    assert.equal(setting.advisory.enabled, false);
    assert.equal(setting.advisory.effort, 'low');
});

test('an advisory session preserves route-default effort instead of inventing low', () => {
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'm/t' } }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'm/s' } }],
        advisory: {
            enabled: true,
            route: { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-fable-5' },
            effort: '',
        },
    }));
    assert.equal(setting.advisory.effort, '');

    const api = JSON.parse(buildReviewerSlotsSetting({
        triad: setting.triad,
        scope: setting.scope,
        advisory: { enabled: true, route: { kind: ROUTE_KIND_API, target_id: '' }, effort: '' },
    }));
    assert.equal(api.advisory.route.kind, ROUTE_KIND_API);
    assert.equal(api.advisory.effort, 'low');
});

test('minted slot ids are prefixed, unique, and never an array index', () => {
    const taken = ['triad_abc123'];
    const minted = mintSlotId('triad', taken);
    assert.match(minted, /^triad_[a-z0-9]{4,}$/);
    assert.ok(!taken.includes(minted));
    assert.notEqual(mintSlotId('scope', []), mintSlotId('scope', []));
});

test('the runs-as line is the capability_delta projection, compact and honest', () => {
    const line = describeLastExecution({
        ts: '2026-08-03T10:00:00Z',
        effective: { route: 'agent_session:codex', model: 'gpt-5.6-sol', effort: 'xhigh',
                     verdict_method: 'light_model_extraction' },
        capability_delta: [{ reason: 'extraction_instead_of_schema' }],
    });
    assert.ok(line.includes('codex session'));
    assert.ok(line.includes('gpt-5.6-sol'));
    assert.ok(line.includes('verdict via light model extraction'));
    assert.ok(line.includes('1 capability delta disclosed'));
    // The timestamp is humanized for the visible line; the raw ISO instant
    // stays recoverable in the row tooltip, never inline.
    assert.ok(!line.includes('2026-08-03T10:00:00Z'));
    assert.equal(describeLastExecution(null), '');
});

test('capability badges display facts and never configure', () => {
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } };
    assert.ok(capabilityBadge(sessionRow, { codex: { status: 'ok' } }).includes('route ok'));
    assert.ok(capabilityBadge(sessionRow, {}).includes('not discovered'));
    // An API badge names the credential the row spends, never a bare channel.
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai/gpt-5.6-luna' } };
    assert.equal(capabilityBadge(apiRow, {}), 'OpenRouter API key');
    assert.equal(capabilityBadge({ route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } }, {}),
        'OpenAI API key');
    assert.equal(capabilityBadge({ route: { kind: ROUTE_KIND_API, target_id: 'claudexor::codex=gpt' } }, {}),
        'Model call through subscription');
});

test('a review-lane session row carries the live availability verdict beside its badge', () => {
    // The quiet meta line used to say only what the harness DOES ("agent
    // session … route ok") — a fact about the harness, never about whether any
    // account can actually run the selected model. It now carries the same
    // sentence the Available-subagents cards show. An API row keeps its line
    // unchanged: an API model is only ever checked when the call starts.
    const view = (models) => ({
        catalogKnown: true, accountsKnown: true, quotaKnown: true,
        harnesses: [{ id: 'codex', status: 'ok', enabled: true, models }],
        snapshot: {
            profiles: { harnessAccounts: [], profiles: [
                { profile: { harness_id: 'codex', profile_id: 'signed-out', enabled: true }, status: { verification: '' } },
                { profile: { harness_id: 'codex', profile_id: 'koshak', enabled: true }, status: { verification: 'passed' } },
            ] },
            quota: [{ subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh', constraints: [] }],
        },
    });
    const session = { slot_id: 't1', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-x', profile_id: '' } };
    // Only the signed-out account lists the model, so no ONE account can run it.
    assert.equal(sessionVerdictNote(session, view([{ id: 'gpt-x', credential_profile_id: 'signed-out' }])),
        'codex · no usable account currently carries gpt-x');
    // The verified account carries it: the row says so instead of accusing.
    assert.equal(sessionVerdictNote(session, view([{ id: 'gpt-x', credential_profile_id: 'koshak' }])),
        'codex · available now');
    // The badge beside it is untouched, and says what it always said.
    assert.match(capabilityBadge(session, { codex: { status: 'ok' } }), /agent session — retrieves context with its own tools · route ok/);
    // An API row and a configured-subagent reference add no verdict sentence.
    const empty = view([{ id: 'gpt-x', credential_profile_id: 'koshak' }]);
    assert.equal(sessionVerdictNote({ slot_id: 't2', route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } }, empty), '');
    assert.equal(sessionVerdictNote({ slot_id: 't3', subagent_id: 'scout' }, empty), '');
});

test('the harness status tail yields to a row verdict, and the repaint gate watches what the verdict reads', () => {
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-x', profile_id: 'koshak' } };
    // One availability claim per line: with a verdict beside it the badge drops `route ok`.
    assert.equal(capabilityBadge(sessionRow, { codex: { status: 'ok' } }, { routeStatus: false }),
        'agent session — retrieves context with its own tools');
    assert.match(capabilityBadge(sessionRow, { codex: { status: 'ok' } }), / · route ok$/);
    // A pinned account that logs in, or spends its window, must repaint the row
    // even though the catalog, the facets and the pin index are unchanged.
    const view = (verification, constraints) => ({
        catalogKnown: true, accountsKnown: true, quotaKnown: true, triad: [sessionRow], scope: [],
        advisory: { route: { kind: ROUTE_KIND_API, target_id: '' } }, deepReview: { route: { kind: ROUTE_KIND_API, target_id: '' } },
        harnesses: [{ id: 'codex', status: 'ok', enabled: true, models: [{ id: 'gpt-x', credential_profile_id: 'koshak' }] }],
        profilesByHarness: { codex: [{ id: 'koshak', enabled: true }] },
        snapshot: {
            profiles: { harnessAccounts: [], profiles: [{ profile: { harness_id: 'codex', profile_id: 'koshak', enabled: true }, status: { verification } }] },
            quota: [{ subject: { harness: 'codex', subject_id: 'koshak' }, freshness: 'fresh', constraints }],
        },
    });
    const loggedOut = repaintSignature(view('', []));
    const loggedIn = repaintSignature(view('passed', []));
    const spent = repaintSignature(view('passed', [{ id: 'w', used_ratio: 1, window_seconds: 3600, resets_at: '2999-01-01T00:00:00Z' }]));
    assert.notEqual(loggedOut, loggedIn);
    assert.notEqual(loggedIn, spent);
    assert.equal(repaintSignature(view('passed', [])), loggedIn, 'an unchanged tick repaints nothing');
});

test('the session model-options fragment guards a saved model discovery no longer lists', () => {
    // ONE fragment for every session model select (triad, scope, advisory,
    // Subagents): "Engine default model" first, discovery next, and a saved
    // model the daemon cannot see right now keeps an option — otherwise the
    // browser silently redraws the select as "Engine default" and the next
    // Save really erases the pin (REG:1190 #7 class).
    const listed = sessionModelOptions({ models: [{ id: 'sonnet' }, { id: 'claude-opus-5' }] }, 'sonnet');
    assert.deepEqual(listed.map((o) => o.value), ['', 'sonnet', 'claude-opus-5']);
    assert.equal(listed[0].label, 'Engine default model');

    const unlisted = sessionModelOptions({ models: [{ id: 'claude-opus-5' }] }, 'sonnet');
    assert.deepEqual(unlisted.map((o) => o.value), ['', 'claude-opus-5', 'sonnet']);
    assert.match(unlisted[2].label, /not in discovery/);

    // Daemon down (no harness at all) is the SAME case, not a special one.
    assert.deepEqual(sessionModelOptions(null, 'sonnet').map((o) => o.value), ['', 'sonnet']);
    assert.deepEqual(sessionModelOptions(null, '').map((o) => o.value), ['']);
});

test('a per-harness model-read failure is not a discovery: the not-in-discovery claim is withdrawn', () => {
    // Discovery is TWO reads. The endpoint answers `models: []` with a typed
    // `models_error` for one harness while the daemon stays globally running
    // — so `catalogKnown` is true and the empty list is NOT authoritative for
    // this harness. Reading it as one labelled the saved model
    // "gpt-saved (not in discovery)": a successful discovery cited as proof of
    // absence, for a discovery that never happened.
    const refused = { id: 'codex', models: [], models_error: 'models_probe_failed' };
    assert.equal(harnessModelsKnown(refused, true), false, 'the catalog read says nothing about this list');
    assert.equal(harnessModelsKnown({ id: 'codex', models: [{ id: 'x' }] }, true), true);
    assert.equal(harnessModelsKnown({ id: 'codex', models: [{ id: 'x' }] }, false), false,
        'an unread catalog still withdraws the claim');

    const options = sessionModelOptions(refused, 'gpt-saved', { catalogKnown: true });
    assert.deepEqual(options.map((o) => o.value), ['', 'gpt-saved'], 'the saved pin keeps its option');
    assert.equal(options[1].label, 'gpt-saved (not checked)',
        'and trades the absence verdict for the honest "not checked"');
    assert.doesNotMatch(JSON.stringify(options), /not in discovery/);

    // …and the gap is SAID, not left as a silently short list.
    assert.match(modelsGapNote(refused, true), /model list could not be read/);
    assert.equal(modelsGapNote({ id: 'codex', models: [{ id: 'x' }] }, true), '');
    // With the catalog itself unread the section note already explains it; a
    // second sentence for the same silence would be noise.
    assert.equal(modelsGapNote(refused, false), '');

    // A harness whose list really WAS read keeps the honest accusation.
    const read = { id: 'codex', models: [{ id: 'gpt-5.6-sol' }] };
    assert.match(sessionModelOptions(read, 'gpt-saved', { catalogKnown: true })[2].label,
        /not in discovery/);
});

test('an advisory session model composes into the target and survives save-load', () => {
    // Compose exactly as the data-advisory-model handler does…
    const target = composeSessionTarget('codex', 'gpt-5.6-luna');
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai/x' }, effort: '' }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, route: { kind: ROUTE_KIND_SESSION, target_id: target, profile_id: 'koshak' }, effort: 'low' },
    }));
    // …and the saved value round-trips model AND profile pin intact.
    assert.equal(setting.advisory.route.kind, ROUTE_KIND_SESSION);
    assert.equal(setting.advisory.route.target_id, 'codex=gpt-5.6-luna');
    assert.equal(setting.advisory.route.profile_id, 'koshak');
    assert.ok(!setting.advisory.route.target_id.includes('::'));
    assert.deepEqual(splitSessionTarget(setting.advisory.route.target_id),
        { harness: 'codex', model: 'gpt-5.6-luna' });
});

test('switching the advisory harness resets the model to the bare harness', () => {
    // The model belongs to the harness it was picked for (same rule as the
    // triad rows and the Subagents tail): A→B lands on B's engine default,
    // spelled as the bare harness — never A's model carried across.
    const saved = { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-opus-5' };
    const out = advisoryRouteTransition(saved, { kind: ROUTE_KIND_SESSION, harness: 'codex' },
        { api: null, session: { ...saved } });
    assert.deepEqual(out.route, { kind: ROUTE_KIND_SESSION, target_id: 'codex' });
});

test('the advisory api route writes the shared api_chat kind, never the retired api alias', () => {
    // Successor pin for the retired Claude-SDK 'api' kind: the advisory row is
    // on the SHARED vocabulary now. A stale 'api' kind in a loaded draft still
    // normalizes to api_chat on save, the routed target rides verbatim, and no
    // profile pin is emitted on the api kind.
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai/x' }, effort: '' }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, route: { kind: 'api', target_id: 'anthropic/claude-sonnet-5', profile_id: 'koshak' }, effort: 'low' },
    }));
    assert.equal(setting.advisory.route.kind, ROUTE_KIND_API);
    assert.equal(setting.advisory.route.target_id, 'anthropic/claude-sonnet-5');
    assert.equal('profile_id' in setting.advisory.route, false);
    // The retired spelling never appears anywhere in the composed bytes.
    assert.doesNotMatch(JSON.stringify(setting), /"kind":"api"/);
});

test('a configured-subagent reference serializes without route knobs (decision 5A)', () => {
    // The stored forms are mutually exclusive: a reference never duplicates
    // route/model/account knobs (the roster row is their SSOT), and an empty
    // explicit effort is OMITTED so the roster row's own effort keeps deciding.
    const setting = JSON.parse(buildReviewerSlotsSetting({
        triad: [
            { slot_id: 't_ref', subagent_id: 'deep-reviewer', route: { kind: ROUTE_KIND_API, target_id: 'stash/kept' }, effort: 'high' },
            { slot_id: 't_ref2', subagent_id: 'fast-reviewer', effort: '' },
        ],
        scope: [{ slot_id: 's_1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, subagent_id: 'deep-reviewer', route: { kind: ROUTE_KIND_API, target_id: 'stash' }, effort: '' },
    }));
    assert.deepEqual(setting.triad[0], { slot_id: 't_ref', subagent_id: 'deep-reviewer', effort: 'high' });
    assert.deepEqual(setting.triad[1], { slot_id: 't_ref2', subagent_id: 'fast-reviewer' });
    assert.deepEqual(setting.advisory, { enabled: true, subagent_id: 'deep-reviewer' });
    // A direct row in the same panel keeps its inline route untouched.
    assert.equal(setting.scope[0].route.kind, ROUTE_KIND_API);
});

test('the roster select survives a saved reference the roster no longer lists', () => {
    // Same rule as profileOptionsFor: the select's value must EXIST as an
    // option or the browser silently redraws the row as the first roster entry
    // and the next Save really rewires the reviewer. The absence claim follows
    // provenance: only a roster that was READ may say "not in the roster".
    const roster = [
        { subagent_id: 'deep', recommended_use: 'Long reasoning over big diffs',
          route: { kind: 'api_model', target_id: 'openai/gpt-5.6-sol' }, effort: 'high' },
        { subagent_id: 'fast', recommended_use: '',
          route: { kind: 'agent_session', target_id: 'cursor=grok-4.6' } },
    ];
    const listed = subagentOptionsFor(roster, 'deep');
    assert.deepEqual(listed.map((o) => o.value), ['deep', 'fast']);
    // Label contract: the row's HANDLE leads (route target plus its own set
    // facets), the description is a caption; the stored id is the VALUE only.
    assert.equal(listed[0].label, 'openai/gpt-5.6-sol/high — Long reasoning over big diffs');
    assert.equal(listed[1].label, 'cursor=grok-4.6');

    const missing = subagentOptionsFor(roster, 'gone');
    assert.deepEqual(missing.map((o) => o.value), ['deep', 'fast', 'gone']);
    assert.match(missing[2].label, /not in the roster/);
    // An unreadable roster licenses no absence claim.
    assert.match(subagentOptionsFor([], 'gone', { rosterKnown: false })[0].label, /not checked/);
    assert.doesNotMatch(subagentOptionsFor([], 'gone', { rosterKnown: false })[0].label, /not in the roster/);
});

test('the one flat reviewer picker leads with roster references, then the inline channels (1=B)', () => {
    const roster = [
        { subagent_id: 'deep', recommended_use: 'Long reasoning',
          route: { kind: 'api_model', target_id: 'openai/gpt-5.6-sol' }, effort: 'high' },
    ];
    const harnesses = [{ id: 'claude', display_name: 'Claude Code' }];

    // A reference row: its select value is the prefixed id, the roster group
    // leads, and the stashed inline route adds NO undiscovered session entry.
    const refRow = { subagent_id: 'deep', route: { kind: ROUTE_KIND_SESSION, target_id: 'gone=old' } };
    assert.equal(encodeReviewerChoice(refRow), `${SUBAGENT_CHOICE_PREFIX}deep`);
    const refGroups = reviewerChoiceGroups({ roster, row: refRow, harnesses });
    assert.equal(refGroups[0].label, 'Available subagents');
    assert.deepEqual(refGroups[0].options.map((o) => o.value), [`${SUBAGENT_CHOICE_PREFIX}deep`]);
    assert.match(refGroups[0].options[0].label, /^openai\/gpt-5\.6-sol\/high — /);
    assert.deepEqual(refGroups.slice(1).map((g) => g.label),
        ['Subscriptions · models', 'API keys', 'Agents · sessions']);
    assert.ok(!refGroups.slice(1).flatMap((g) => g.options).some((o) => o.value === 'session:gone'));

    // An inline row: its own choice threads down so an undiscovered harness
    // keeps its option (the survive-the-save rule).
    const inlineRow = { subagent_id: '', route: { kind: ROUTE_KIND_SESSION, target_id: 'gone=old' } };
    assert.equal(encodeReviewerChoice(inlineRow), 'session:gone');
    const inlineGroups = reviewerChoiceGroups({ roster, row: inlineRow, harnesses });
    assert.ok(inlineGroups.at(-1).options.some((o) => o.value === 'session:gone'));

    // A saved reference missing from the roster survives as a prefixed option;
    // an empty roster contributes no group at all.
    const missingGroups = reviewerChoiceGroups({ roster, row: { subagent_id: 'gone' }, harnesses });
    assert.ok(missingGroups[0].options.some(
        (o) => o.value === `${SUBAGENT_CHOICE_PREFIX}gone` && /not in the roster/.test(o.label)));
    const emptyGroups = reviewerChoiceGroups({ roster: [], row: { subagent_id: '' }, harnesses });
    assert.notEqual(emptyGroups[0].label, 'Available subagents');

    // The configured providers ride through to the shared API keys group; no
    // surface narrows that group's wording for itself any more.
    const providers = configuredApiProviders({ OPENAI_API_KEY: 'k' });
    const advisory = reviewerChoiceGroups({
        roster: [], row: { subagent_id: '', route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } },
        harnesses, providers,
    });
    assert.deepEqual(advisory.find((g) => g.label === 'API keys').options.map((o) => o.value),
        [`${API_CHOICE_PREFIX}openai`, '']);
    // …and an unconfigured row's own provider is rescued, never swapped away.
    const keyless = reviewerChoiceGroups({ roster: [], row: { subagent_id: '' }, harnesses, providers });
    assert.deepEqual(keyless.find((g) => g.label === 'API keys').options.map((o) => o.label),
        ['OpenAI', 'OpenRouter (no key)', 'Add a key in Accounts for more']);
});

test('an advisory reference switch keeps an explicit effort override; crossing from inline clears it', () => {
    // Sol delta-review finding: the merged picker unconditionally cleared the
    // advisory effort on ANY reference pick, silently erasing a saved
    // {subagent_id, effort: 'xhigh'} override on a reference-to-reference
    // switch. Inline branches still shed their effort ('' = the roster row's
    // own effort decides).
    assert.deepEqual(
        advisoryReferenceTransition({ subagent_id: 'deep', effort: 'xhigh' }, 'fast'),
        { subagent_id: 'fast', effort: 'xhigh' });
    assert.deepEqual(
        advisoryReferenceTransition({ subagent_id: '', effort: 'low', route: { kind: ROUTE_KIND_API } }, 'deep'),
        { subagent_id: 'deep', effort: '' });
    assert.deepEqual(
        advisoryReferenceTransition({ subagent_id: '', effort: 'high', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=x' } }, 'deep'),
        { subagent_id: 'deep', effort: '' });
});

test('picker captions strip directional marks and never split a surrogate pair', () => {
    const marked = subagentOptionsFor([{
        subagent_id: 'row',
        recommended_use: '\u200Efast\u200F \u061Ccheap\u202Eevil',
        route: { kind: 'api_model', target_id: 'openai/gpt-5.6-luna' },
    }], '')[0].label;
    assert.equal(marked, 'openai/gpt-5.6-luna — fast cheap' + 'evil');

    const emoji = '\u{1F9EA}'.repeat(60); // 60 code points, 120 UTF-16 units
    const long = subagentOptionsFor([{
        subagent_id: 'row',
        recommended_use: emoji,
        route: { kind: 'api_model', target_id: 'x' },
    }], '')[0].label;
    const caption = long.split(' — ')[1];
    assert.equal(caption, '\u{1F9EA}'.repeat(45) + '…');
});

test('the derived disclosure reports the roster row facts read-only, with honest absence', () => {
    const roster = [
        {
            subagent_id: 'sess',
            name: 'Session Reviewer',
            route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', credential_profile_id: 'koshak' },
            effort: 'xhigh',
        },
        { subagent_id: 'apirow', name: 'Api Reviewer', route: { kind: 'api_model', target_id: 'openai/gpt-5.6-luna' } },
    ];
    const session = describeSubagentReference('sess', roster);
    assert.ok(session.includes('codex session'));
    assert.ok(session.includes('gpt-5.6-sol'));
    assert.ok(session.includes('account koshak'));
    assert.ok(session.includes('effort xhigh'));
    assert.match(session, /roster row/);

    const api = describeSubagentReference('apirow', roster);
    assert.ok(api.includes('API model openai/gpt-5.6-luna'));

    // A missing roster row is said plainly — kept, refusing, never rerouted —
    // and an UNREAD roster never claims the row does not exist.
    assert.match(describeSubagentReference('gone', roster), /no roster row with this ID exists/);
    assert.match(describeSubagentReference('gone', roster), /refuse rather than reroute/);
    assert.match(describeSubagentReference('gone', [], { rosterKnown: false }), /roster could not be read/);
});

test('the runs-as line shows APPLIED account/access and honest absence for an undisclosed model', () => {
    const applied = describeLastExecution({
        effective: { route: 'agent_session:codex', model: 'gpt-5.6-sol',
                     profile_id: 'koshak', access: 'readonly', effort: 'xhigh' },
    });
    assert.ok(applied.includes('account koshak'));
    assert.ok(applied.includes('access readonly'));
    // Old telemetry: a session with NO resolved model says so — the requested
    // model never masquerades as the applied one.
    const bare = describeLastExecution({ effective: { route: 'agent_session:codex' } });
    assert.ok(bare.includes('model not disclosed'));
    assert.ok(!bare.includes('account'));
    // An api row keeps its sent-model-is-applied-model reading with no noise.
    const api = describeLastExecution({ effective: { route: 'api_chat', model: 'openai/x' } });
    assert.ok(api.includes('openai/x') && !api.includes('not disclosed'));
});

// ---------------------------------------------------------------------------
// Phase 2: a row may only be labeled "(not in discovery)" after a SUCCESSFUL
// discovery. With the daemon stopped (or the endpoint unreachable) the backend
// answers `harnesses: []` by construction, and this page used to stamp that
// label onto every saved row while explaining nothing — the exact screen the
// owner reported (2026-08-08). The saved option itself must survive either
// way: that guard is what stops the next Save from erasing the pin.
// ---------------------------------------------------------------------------

test('an unread facet never accuses a saved row of being undiscovered', () => {
    // Same empty discovery, two different worlds.
    const discoveredMiss = routeChoiceGroups({ harnesses: [], currentChoice: 'session:codex' });
    assert.match(discoveredMiss[2].options[0].label, /not in discovery/);

    const cannotAsk = routeChoiceGroups({ harnesses: [], currentChoice: 'session:codex', catalogKnown: false });
    assert.equal(cannotAsk[2].options[0].value, 'session:codex', 'the saved option SURVIVES');
    assert.equal(cannotAsk[2].options[0].label, 'codex (not checked)',
        'and is labelled unchecked, never undiscovered');
    assert.doesNotMatch(JSON.stringify(cannotAsk), /not in discovery/);
    // The empty-group placeholder stops promising a sign-in that would not help.
    const emptyGroup = routeChoiceGroups({ harnesses: [], catalogKnown: false })[2].options[0];
    assert.doesNotMatch(emptyGroup.label, /sign in under Providers/);
});

test('an unread facet is labelled "not checked", never "not in discovery"', () => {
    // Rebased provenance pin (invariant 5): the suffix is the FACET's own
    // verdict — the account pin answers to the ACCOUNTS facet, the model and
    // route to the CATALOG facet. Hardcoding "not in discovery" into any suffix
    // helper stays green through every behavioural test that passes the
    // authoritative default, so this walks the unread worlds explicitly.
    const pins = profileOptionsFor(['valentine'], 'koshak', { accountsKnown: false });
    const pin = pins.find((o) => o.value === 'koshak');
    assert.ok(pin, 'an unread account store dropped the saved pin');
    assert.match(pin.label, /not checked/);
    assert.doesNotMatch(pin.label, /not in discovery/);
    // A facet that WAS read keeps the honest accusation.
    assert.match(profileOptionsFor(['valentine'], 'koshak', { accountsKnown: true })
        .find((o) => o.value === 'koshak').label, /not in discovery/);

    const models = sessionModelOptions({ models: [{ id: 'other' }] }, 'gpt-5.6-sol', { catalogKnown: false });
    const savedModel = models.find((o) => o.value === 'gpt-5.6-sol');
    assert.ok(savedModel, 'an unread catalog dropped the saved model');
    assert.match(savedModel.label, /not checked/);
    assert.doesNotMatch(savedModel.label, /not in discovery/);

    // A refused per-harness model read (catalog itself fine) is the SAME
    // unread world for this one list: `models_error` withdraws the claim.
    const refused = sessionModelOptions({ models: [], models_error: 'daemon_unreachable' },
        'gpt-5.6-sol', { catalogKnown: true });
    assert.match(refused.find((o) => o.value === 'gpt-5.6-sol').label, /not checked/);

    // The saved ROUTE is not called undiscovered before the catalog was read.
    const route = routeChoiceGroups({ harnesses: [], currentChoice: 'session:codex', catalogKnown: false });
    assert.match(route[2].options[0].label, /not checked/);
    assert.doesNotMatch(route[2].options[0].label, /not in discovery/);

    // Emptiness states ABSENCE only when the catalog was actually read.
    const emptyRead = routeChoiceGroups({ harnesses: [], catalogKnown: true })[2].options[0];
    assert.match(emptyRead.label, /None available/);
    const emptyUnread = routeChoiceGroups({ harnesses: [], catalogKnown: false })[2].options[0];
    assert.doesNotMatch(emptyUnread.label, /None available/);
});

test('the model and account pins survive a daemon-down save without the undiscovered label', () => {
    const models = sessionModelOptions({ models: [] }, 'gpt-5.6-sol', { catalogKnown: false });
    assert.deepEqual(models.map((o) => o.value), ['', 'gpt-5.6-sol'], 'the pin keeps its option');
    assert.equal(models[1].label, 'gpt-5.6-sol (not checked)');
    assert.match(sessionModelOptions({ models: [] }, 'gpt-5.6-sol')[1].label, /not in discovery/);

    const pins = profileOptionsFor([], 'koshak', { accountsKnown: false });
    assert.deepEqual(pins.map((o) => o.value), ['', 'koshak']);
    assert.match(pins[1].label, /not checked/);
    assert.doesNotMatch(pins[1].label, /not in discovery/);
    assert.match(profileOptionsFor([], 'koshak')[1].label, /not in discovery/);

    // …and the pin still reaches the save payload unchanged, which is the
    // whole point of keeping the option (a Save with the daemon down must not
    // silently widen which account a reviewer may spend).
    const row = { slot_id: 'triad_a', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'koshak' } };
    const saved = JSON.parse(buildReviewerSlotsSetting({ triad: [row], scope: [], advisory: {} }));
    assert.deepEqual(saved.triad[0].route, { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'koshak' });
});

test('the delivery badge does not claim "route not discovered" when nobody could be asked', () => {
    const row = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } };
    assert.match(capabilityBadge(row, {}), /route not discovered/);
    assert.doesNotMatch(capabilityBadge(row, {}, { catalogKnown: false }), /not discovered/);
    assert.match(capabilityBadge(row, {}, { catalogKnown: false }), /agent session/);
});

test('facets are independent: an unread ACCOUNT store does not silence the CATALOG verdict', () => {
    // The concrete mislabel a single global verdict produces: the harness
    // catalog was read fine and genuinely no longer lists `claude`, while the
    // credential-profile read never happened. The route option must keep its
    // earned "(not in discovery)" and the account pin must NOT be given one.
    const groups = routeChoiceGroups({
        harnesses: [{ id: 'codex' }], currentChoice: 'session:claude', catalogKnown: true,
    });
    assert.match(groups[2].options.at(-1).label, /not in discovery/);

    const pins = profileOptionsFor([], 'koshak', { accountsKnown: false });
    assert.doesNotMatch(pins[1].label, /not in discovery/);
    assert.deepEqual(pins.map((o) => o.value), ['', 'koshak']);
});

test('neither facet gap is dropped: the tab banner names it and the section claims nothing', async () => {
    // This section renders two facets — the route/model lists from the catalog,
    // the account pins from the credential profiles — and a gap in EITHER used
    // to vanish. The sentence now belongs to the tab's ONE service banner (the
    // sections moved onto the Agents tab, and three scattered service notes
    // became one); what this section owes is the other half of the same rule —
    // making no claim the unread facet never licensed.
    const store = (reads) => createClaudexorStatusStore({
        fetchImpl: async () => ({
            ok: true,
            status: 200,
            json: async () => ({ daemon: { state: 'running' }, harnesses: [], profiles: {}, quota: [], reads }),
        }),
        doc: { hidden: false, addEventListener() {}, removeEventListener() {} },
    });
    const pins = (accountsKnown) => profileOptionsFor(['koshak'], 'gone', { accountsKnown });
    const pinnedRows = {
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_SESSION, target_id: 'cursor:x', profile_id: 'gone' } }],
        profilesByHarness: { cursor: ['koshak'] },
    };

    const accountsDied = store({ catalog: 'ok', accounts: 'failed', quota: 'ok' });
    await accountsDied.refresh();
    assert.match(serviceBannerLine(accountsDied).text, /Your agent accounts could not be read/,
        'the banner NAMES the gap this section would otherwise have dropped');
    // …and the rows say nothing they did not earn: a pin is not "gone" because
    // nobody could ask, and the missing-account warning stays silent.
    assert.doesNotMatch(pins(accountsDied.accountsKnown)[1].label, /not in discovery/);
    assert.equal(pinnedAccountWarning({ ...pinnedRows, accountsKnown: accountsDied.accountsKnown }), '');
    accountsDied.dispose();

    const catalogDied = store({ catalog: 'failed', accounts: 'ok', quota: 'ok' });
    await catalogDied.refresh();
    const catalogLine = serviceBannerLine(catalogDied);
    assert.match(catalogLine.text, /Your agents could not be read/);
    assert.doesNotMatch(catalogLine.text, /agent accounts could not be read/, 'a healthy facet is not accused');
    // …and a read that merely did not land never claims nobody asked.
    assert.doesNotMatch(catalogLine.text, /was not asked/);
    // The route select points at that one sentence instead of writing a second.
    const empty = routeChoiceGroups({ harnesses: [], catalogKnown: catalogDied.catalogKnown })[2].options[0];
    assert.match(empty.label, /see the service banner above/);
    // With the accounts read, the SAME pin is now genuinely missing and says so.
    assert.match(pinnedAccountWarning({ ...pinnedRows, accountsKnown: catalogDied.accountsKnown }),
        /pinned to an account the agent service no longer lists/);
    catalogDied.dispose();

    // Both facets in the SAME state: the account pins are STILL named. They
    // used to be dropped for matching the catalog's enum, so the note spoke
    // only of agents while the pins sat on screen unexplained — equal state is
    // not equal subject.
    const both = store({ catalog: 'not_read', accounts: 'not_read', quota: 'not_read' });
    await both.refresh();
    const bothLine = serviceBannerLine(both);
    assert.match(bothLine.text, /daemon was not asked/);
    assert.equal(bothLine.text.match(/could not be read/g), null, 'one sentence covers both');
    both.dispose();

    const healthy = store({ catalog: 'ok', accounts: 'ok', quota: 'ok' });
    await healthy.refresh();
    assert.doesNotMatch(serviceBannerLine(healthy).text, /could not be read/,
        'nothing to say when everything was read');
    healthy.dispose();
});

test('an untouched deep self-review placeholder is omitted from the save; an edited or saved row rides it (items 1/10)', () => {
    const base = {
        triad: [{ slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai/x' }, effort: '' }],
        scope: [{ slot_id: 's1', route: { kind: ROUTE_KIND_API, target_id: 'openai/y' }, effort: '' }],
        advisory: { enabled: true, route: { kind: ROUTE_KIND_API, target_id: '' }, effort: 'low' },
    };
    // Synthesized (shown from the key) but untouched: OMITTED — the runtime
    // synthesizes the identical row and nothing is written behind the owner.
    const untouched = { route: { kind: ROUTE_KIND_API, target_id: 'openai/from-key' }, effort: '', subagent_id: '',
                        synthesizedFrom: 'OUROBOROS_MODEL_DEEP_SELF_REVIEW', materialized: false };
    assert.equal('deep_review' in JSON.parse(buildReviewerSlotsSetting({ ...base, deepReview: untouched })), false);
    // An empty placeholder (older server answered no row) is omitted too — a
    // triad/scope edit is never blocked by the singleton.
    const empty = { route: { kind: ROUTE_KIND_API, target_id: '' }, effort: '', subagent_id: '', synthesizedFrom: '', materialized: false };
    assert.equal('deep_review' in JSON.parse(buildReviewerSlotsSetting({ ...base, deepReview: empty })), false);
    // Edited (materialized) or loaded as SAVED: emitted verbatim.
    const edited = { ...untouched, materialized: true };
    assert.deepEqual(JSON.parse(buildReviewerSlotsSetting({ ...base, deepReview: edited })).deep_review,
        { route: { kind: 'api_chat', target_id: 'openai/from-key' } });
    // A blanked model box on an edited row IS emitted (the server's typed 400
    // is the refusal; the row itself says so before the save).
    const blanked = { ...edited, route: { kind: ROUTE_KIND_API, target_id: '' } };
    assert.deepEqual(JSON.parse(buildReviewerSlotsSetting({ ...base, deepReview: blanked })).deep_review,
        { route: { kind: 'api_chat', target_id: '' } });
    assert.match(deepReviewMetaNotes(blanked).join(' '), /Model id required — an empty model id is refused at save/);
    assert.match(deepReviewMetaNotes(untouched).join(' '), /Not saved as a row yet — shown from OUROBOROS_MODEL_DEEP_SELF_REVIEW/);
    assert.match(deepReviewMetaNotes(untouched).join(' '), /stored when edited or when the default panel is first saved/);
    assert.deepEqual(deepReviewMetaNotes(edited), []);
    assert.deepEqual(deepReviewMetaNotes({ ...blanked, subagent_id: 'deep' }), []);
    assert.deepEqual(deepReviewMetaNotes({ ...blanked, route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } }), []);
});

test('the missing-account warning walks the deep self-review row too (items 2/21)', () => {
    const deepReview = { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-5.6-sol', profile_id: 'gone' }, effort: '', subagent_id: '' };
    const warning = pinnedAccountWarning({ deepReview, profilesByHarness: { codex: ['koshak'] }, accountsKnown: true });
    assert.match(warning, /A review row is\s+pinned to an account the agent service no longer lists \(codex · gone\)/);
    // A present account, a reference row and an api row raise nothing.
    assert.equal(pinnedAccountWarning({ deepReview, profilesByHarness: { codex: ['gone'] }, accountsKnown: true }), '');
    assert.equal(pinnedAccountWarning({ deepReview: { subagent_id: 'deep' }, profilesByHarness: {}, accountsKnown: true }), '');
    assert.equal(pinnedAccountWarning({ deepReview: { route: { kind: ROUTE_KIND_API, target_id: 'openai/x' } }, profilesByHarness: {}, accountsKnown: true }), '');
    // An unread accounts facet licenses no claim.
    assert.equal(pinnedAccountWarning({ deepReview, profilesByHarness: {}, accountsKnown: false }), '');
});

// ---------------------------------------------------------------------------
// The source is CHOSEN, never spelled (docs/DESIGN.md §7). A reviewer row's
// provider comes from the one grouped picker; the editor composes the stored
// `provider::model` spelling, and the row says which provider serves it.
// ---------------------------------------------------------------------------

test('an API reviewer row names its provider and discloses the exact stored id', () => {
    const profiles = { openai: { label: 'OpenAI' } };
    const row = { slot_id: 't1', route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } };
    // The picker value carries the provider and never the stored separator.
    assert.equal(encodeReviewerChoice(row), `${API_CHOICE_PREFIX}openai`);
    assert.ok(!encodeReviewerChoice(row).includes('::'));
    // The chip and the badge both name the provider; the meta line carries the
    // exact stored spelling, which no placeholder ever asks the owner to type.
    assert.match(reviewerRouteIdentityMarkup(row.route, {}, { providerProfiles: profiles }),
        />API · OpenAI</);
    assert.equal(capabilityBadge(row, {}, { providerProfiles: profiles }), 'OpenAI API key');
    assert.equal(savedTargetNote(row), 'stored as openai::gpt-x');

    // A bare id IS the OpenRouter spelling, and reads as OpenRouter everywhere.
    const bare = { slot_id: 't2', route: { kind: ROUTE_KIND_API, target_id: 'x-ai/grok-4.6' } };
    assert.equal(encodeReviewerChoice(bare), `${API_CHOICE_PREFIX}openrouter`);
    assert.match(reviewerRouteIdentityMarkup(bare.route), />API · OpenRouter</);
    assert.equal(savedTargetNote(bare), 'stored as x-ai/grok-4.6');
    // An empty provider draft has no model to disclose yet.
    assert.equal(savedTargetNote({ slot_id: 't3', route: { kind: ROUTE_KIND_API, target_id: 'openai::' } }), '');

    // A session row spells its harness and model in its own controls, so the
    // meta line adds nothing; an empty draft has no stored id to disclose.
    assert.equal(savedTargetNote({ route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt' } }), '');
    assert.equal(savedTargetNote({ route: { kind: ROUTE_KIND_API, target_id: '' } }), '');
    assert.equal(savedTargetNote({ subagent_id: 'deep' }), '');
});

test('choosing a provider starts an empty draft; returning to one restores ITS draft', () => {
    // What the picker's change handler does: a new source starts empty (it must
    // not inherit another provider's model), while the per-source memory hands
    // back the draft the owner had typed for the provider they return to.
    const openai = { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' };
    const switched = advisoryRouteTransition(openai, decodeRouteChoice(`${API_CHOICE_PREFIX}anthropic`));
    assert.deepEqual(switched.route, { kind: ROUTE_KIND_API, target_id: 'anthropic::' },
        'the provider is kept as a transient draft, the model is not carried across');
    const drafted = { kind: ROUTE_KIND_API, target_id: 'anthropic::claude-opus-5' };
    const back = advisoryRouteTransition(drafted, decodeRouteChoice(`${API_CHOICE_PREFIX}openai`), switched.memory);
    assert.deepEqual(back.route, openai, 'the OpenAI draft comes back, not an empty field');
    const again = advisoryRouteTransition(back.route, decodeRouteChoice(`${API_CHOICE_PREFIX}anthropic`), back.memory);
    assert.deepEqual(again.route, drafted);
    // A provider that was never drafted still starts empty.
    assert.deepEqual(advisoryRouteTransition(back.route, decodeRouteChoice(`${API_CHOICE_PREFIX}deepseek`), back.memory).route,
        { kind: ROUTE_KIND_API, target_id: 'deepseek::' });
    // Re-picking the provider the row already uses changes nothing at all.
    assert.deepEqual(advisoryRouteTransition(openai, decodeRouteChoice(`${API_CHOICE_PREFIX}openai`), back.memory).route,
        openai);
});

test('a last-run receipt is read against the route that produced it', () => {
    const receipt = (requested) => ({
        ts: '2026-09-13T10:00:00Z',
        requested,
        effective: { route: 'agent_session:claude', model: 'claude-opus-5', profile_id: 'proton5' },
    });
    const apiRow = { route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } };
    const sessionRow = { route: { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-opus-5' } };
    const ranOnClaude = { route_kind: 'agent_session', model: '', session_target: 'claude=claude-opus-5', subagent_id: '' };

    // Same route: nothing to warn about.
    assert.equal(lastRunRouteChanged(receipt(ranOnClaude), sessionRow), false);
    assert.equal(lastRunMetaPrefix(receipt(ranOnClaude), sessionRow), 'Last run');
    // The model inside a session is APPLIED evidence, not the assignment.
    assert.equal(lastRunRouteChanged(receipt(ranOnClaude),
        { route: { kind: ROUTE_KIND_SESSION, target_id: 'claude=claude-fable-5' } }), false);
    // A different harness, a different kind, a different API model, and a
    // reference the row no longer holds are each a changed route.
    assert.equal(lastRunRouteChanged(receipt(ranOnClaude), apiRow), true);
    assert.equal(lastRunRouteChanged(receipt(ranOnClaude),
        { route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } }), true);
    assert.equal(lastRunRouteChanged(
        receipt({ route_kind: 'api_chat', model: 'openai::gpt-old', session_target: '', subagent_id: '' }), apiRow), true);
    assert.equal(lastRunRouteChanged(
        receipt({ route_kind: 'api_chat', model: 'openai::gpt-x', session_target: '', subagent_id: '' }), apiRow), false);
    assert.equal(lastRunRouteChanged(
        receipt({ route_kind: 'api_chat', model: '', session_target: '', subagent_id: 'deep' }), apiRow), true);
    assert.equal(lastRunRouteChanged(
        receipt({ route_kind: 'api_chat', model: '', session_target: '', subagent_id: 'deep' }),
        { subagent_id: 'deep' }), false);

    // A receipt older than the `requested` field has no comparison subject, so
    // it claims nothing rather than accusing the row of having changed.
    assert.equal(lastRunRouteChanged({ effective: { route: 'api_chat', model: 'openai/x' } }, apiRow), false);
    assert.equal(lastRunMetaPrefix({ effective: {} }, apiRow), 'Last run');

    // The wording NAMES the earlier route, so the receipt stays readable as
    // evidence about something the row no longer is.
    assert.equal(
        lastRunMetaPrefix(receipt(ranOnClaude), apiRow, { harnesses: { claude: { display_name: 'Claude Code' } } }),
        'Last run, before this row changed (it ran as a Claude Code session)');
    assert.equal(
        lastRunMetaPrefix(receipt({ route_kind: 'api_chat', model: 'openai::gpt-old' }), sessionRow,
            { providerProfiles: { openai: { label: 'OpenAI' } } }),
        'Last run, before this row changed (it ran as an API model on OpenAI)');
    assert.equal(
        lastRunMetaPrefix(receipt({ route_kind: 'api_chat', model: 'claudexor::codex-models=gpt' }), sessionRow,
            { modelSources: [{ id: 'codex-models', label: 'Codex' }] }),
        'Last run, before this row changed (it ran as a model on Codex)');
    // A reference is named from the receipt's OWN recorded route — never by the
    // stored id, and never from today's roster (that would relabel the past).
    assert.equal(
        lastRunMetaPrefix(receipt({ route_kind: 'api_chat', subagent_id: 'deep', model: 'openai/gpt-5.6-sol',
            effort: 'high', processing_preference: 'fast' }), apiRow),
        'Last run, before this row changed (it ran as the configured subagent openai/gpt-5.6-sol/high/fast)');
    assert.equal(
        lastRunMetaPrefix(receipt({ route_kind: ROUTE_KIND_SESSION, subagent_id: 'deep', model: 'ignored',
            session_target: 'codex=gpt-6-astra', profile_id: 'koshak' }), apiRow),
        'Last run, before this row changed (it ran as the configured subagent codex=gpt-6-astra/@koshak)');
    assert.equal(
        lastRunMetaPrefix(receipt({ route_kind: 'api_chat', subagent_id: 'deep' }), apiRow),
        'Last run, before this row changed (it ran as a configured subagent)');
});

test('no reviewer control teaches the stored prefix, and the advisory says what it delivers', () => {
    // docs/DESIGN.md §7: the stored spellings are never a field placeholder or
    // a help-text instruction. The retired "provider/model-id" placeholder is
    // exactly that instruction, so it may not come back in any control.
    const source = readFileSync(new URL('../modules/reviewer_slots.js', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /provider\/model-id/);
    assert.match(source, /modelPlaceholder: 'Empty uses the default model'/);
    assert.match(source, /modelPlaceholder: 'Choose a model'/);

    // The advisory's ONE delivery difference is said beside the row that picks
    // it, now that the picker's API entry is a plain provider name.
    assert.equal(advisoryDeliveryNote({ route: { kind: ROUTE_KIND_API, target_id: 'openai::gpt-x' } }, {}),
        'OpenAI API key \u2014 runs a bounded inspection episode');
    assert.equal(advisoryDeliveryNote({ route: { kind: ROUTE_KIND_API, target_id: 'claudexor::codex=gpt' } }, {}),
        'Model call through subscription \u2014 runs a bounded inspection episode');
    // A session reviewer reads the repository itself; it runs no episode here.
    const session = advisoryDeliveryNote({ route: { kind: ROUTE_KIND_SESSION, target_id: 'codex' } },
        { codex: { status: 'ok' } });
    assert.match(session, /agent session — retrieves context with its own tools/);
    assert.doesNotMatch(session, /inspection episode/);
});

test('an owner-disabled roster row leaves the picker but a saved reference to it stays', () => {
    // The roster's per-row switch is withdrawal from NEW choices, not a silent
    // rewiring: the save-time parser refuses a fresh reference to a switched-off
    // row, so offering it here would only manufacture a 400 the owner cannot see
    // coming. An ALREADY selected reference keeps its option (the same
    // survive-the-save rule as a row that fell out of the roster) and says why.
    const roster = [
        { subagent_id: 'deep', recommended_use: 'Long reasoning over big diffs',
          route: { kind: 'api_model', target_id: 'openai/gpt-5.6-sol' }, effort: 'high' },
        { subagent_id: 'paused', recommended_use: 'Paused seat',
          route: { kind: 'api_model', target_id: 'openai/gpt-5.6-luna' }, enabled: false },
    ];
    assert.deepEqual(subagentOptionsFor(roster, '').map((o) => o.value), ['deep']);
    assert.deepEqual(subagentOptionsFor(roster, 'deep').map((o) => o.value), ['deep']);

    const holding = subagentOptionsFor(roster, 'paused');
    assert.deepEqual(holding.map((o) => o.value), ['deep', 'paused']);
    // The handle leads, the owner's switch rides with the facts, the caption follows.
    assert.equal(holding[1].label, 'openai/gpt-5.6-luna · switched off — Paused seat');
    assert.equal(holding[0].label, 'openai/gpt-5.6-sol/high — Long reasoning over big diffs');
    assert.doesNotMatch(holding[1].label, /not in the roster/, 'the row exists; it is switched off');

    // The flat picker keeps the same behaviour through its group builder.
    const groups = reviewerChoiceGroups({ roster, row: { subagent_id: 'paused' }, harnesses: [] });
    const rosterGroup = groups.find((group) => group.label === 'Available subagents');
    assert.deepEqual(rosterGroup.options.map((o) => o.value),
        [`${SUBAGENT_CHOICE_PREFIX}deep`, `${SUBAGENT_CHOICE_PREFIX}paused`]);

    // The read-only disclosure states the consequence and the way out.
    const described = describeSubagentReference('paused', roster);
    assert.match(described, /switched off/);
    assert.match(described, /refused at save rather than rerouted/);
    assert.match(described, /turn the row back on/);
    assert.doesNotMatch(describeSubagentReference('deep', roster), /switched off/);
});


test('referenced captions follow row effort overrides while compound routes stay immutable', () => {
    const roster = [
        { subagent_id: 'ordinary', effort: 'high', route: { kind: ROUTE_KIND_SESSION, target_id: 'codex=gpt-model' } },
        { subagent_id: 'compound', effort: 'xhigh', route: { kind: ROUTE_KIND_SESSION, target_id: 'cursor=cursor-grok-xhigh' } },
    ];
    assert.match(describeSubagentReference('ordinary', roster, { effort: 'low' }), /preferred effort low/);
    assert.doesNotMatch(describeSubagentReference('ordinary', roster, { effort: 'low' }), /effort high/);
    assert.match(describeSubagentReference('ordinary', roster), /preferred effort high/);
    const compound = describeSubagentReference('compound', roster, { effort: 'low' });
    assert.match(compound, /cursor-grok-xhigh/);
    assert.match(compound, /preferred effort xhigh/);
    assert.doesNotMatch(compound, /effort low/);
    assert.equal(roster[1].route.target_id, 'cursor=cursor-grok-xhigh');
});
