// Review lanes UI (phase 6.2/6.3, revised per owner finding #6) — the rows in
// Agents → Review lanes over the ONE structured setting
// (OUROBOROS_REVIEWER_SLOTS; the settings key keeps its name, the section does
// not — D-10 moved these rows out of the Models tab and renamed them).
//
// Shape rules are the owner's:
//  * ONE flat picker per row (decision 1=B replaced the earlier two-level
//    source+route pair): the roster rows from Available subagents lead as
//    references, then the inline sources — one entry per subscription model
//    source, one per API provider whose key is stored, one per login-capable
//    harness — never the full flat model catalog (hundreds of options made the
//    list unusable, finding #6). A referenced row's route/
//    model/effort/account are shown as READ-ONLY derived facts (the roster
//    stays their SSOT); the stored forms are mutually exclusive:
//    {slot_id, route, effort} XOR {slot_id, subagent_id, effort}.
//  * Model ids use the same editable chooser as Models and Available subagents.
//    Source/account/effort remain short native selects. Catalogs suggest values;
//    they never assign one or erase an undiscovered saved selection.
//  * The provider shown for a delegated row is the HARNESS NAME (codex,
//    claude, cursor, …) — never "Claudexor", and never a `provider::model`
//    string syntax.
//  * Effort is the EXISTING per-model effort mechanism, one dropdown per row.
// Capability badges DISPLAY facts; they configure nothing.
//
// Pure helpers live at the top and are node-tested without a DOM.

import { apiFetch } from './api_client.js';
import { accountRows, bindStatusSurface, boundedStatusRefresh, claudexorStatus } from './claudexor_status_store.js';
import { harnessIdentityMarkup, harnessPresentation } from './harness_presentation.js';
import { formatRelativeAge, revealNewRow } from './ui_helpers.js';
import * as routeEditor from './route_editor_primitives.js';
import { sessionRouteVerdict } from './subagent_status_primitives.js';
import {
    availableSubagentsLoadValue,
    parseAvailableSubagentsSetting,
} from './subagents_settings.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { modelChooserHtml, bindModelChoosers } from './model_chooser.js';
import { mergeModelCatalog, catalogReadNote, mergeHarnessModelCatalog } from './settings_catalog.js';

export const ROUTE_KIND_API = 'api_chat';
export const ROUTE_KIND_SESSION = routeEditor.ROUTE_KIND_AGENT_SESSION;

// Select-value prefix marking a configured-subagent reference in the one
// flat reviewer picker (decision 1=B); everything else is a route choice.
export const SUBAGENT_CHOICE_PREFIX = 'subagent:';
// The legacy single API entry, kept as an accepted input spelling. Every
// encoder now emits `api:<provider>`; the target model id never lives in the
// select — display always matches the stored target (finding #6c: a fresh row
// used to DISPLAY the first catalog model while storing '').
export const API_ROUTE_CHOICE = routeEditor.API_ROUTE_CHOICE;
export const API_CHOICE_PREFIX = routeEditor.API_CHOICE_PREFIX;

export const EFFORT_CHOICES = routeEditor.EFFORT_CHOICES;

export function reviewerProcessingInheritance(row, globalPreference, rolePreferences = null, resolved = {}) {
    if (row.synthesizedFrom && !row.materialized) {
        if (rolePreferences !== null) return rolePreferences.deep_review || globalPreference;
        return resolved.deep_review_slot_1 ?? globalPreference;
    }
    return globalPreference;
}

function processingInheritance(row) {
    return reviewerProcessingInheritance(row, state.processingPreference, state.roleProcessingPreferences, state.resolvedProcessingPreferences);
}

// ---------------------------------------------------------------------------
// Pure helpers.
// ---------------------------------------------------------------------------

export function mintSlotId(prefix, takenIds) {
    return routeEditor.mintStableId(prefix, takenIds);
}

export function encodeRouteChoice(row) {
    return routeEditor.encodeRouteChoice(row);
}

export function decodeRouteChoice(value) {
    return routeEditor.decodeRouteChoice(value, { apiKind: ROUTE_KIND_API });
}

// Claudexor's own reviewer-panel spelling: harness[=model]. Never '::'.
export function composeSessionTarget(harness, model) {
    return routeEditor.composeSessionTarget(harness, model);
}

export function splitSessionTarget(target) {
    return routeEditor.splitSessionTarget(target);
}

// `catalogKnown` / `accountsKnown` = the matching facet of the status payload
// was actually READ (claudexor_status_store.facetReadState). Only then does the
// absence of a harness/model/account mean anything about that harness/model/
// account. With the daemon stopped, unreachable, or that one read refused, the
// backend answers `harnesses: []` by construction, and this UI used to decorate
// EVERY saved row with "(not in discovery)" — an accusation nobody had earned,
// and the exact screen the owner reported (2026-08-08). The saved value keeps
// its option either way (that guard is what stops a Save from erasing a pin);
// only the LABEL's claim follows the facet: "(not in discovery)" is a VERDICT —
// it says we looked and did not find it — and is licensed ONLY by a facet that
// was read. An unread facet says "(not checked)": we did not look, and the
// owner's saved value is almost certainly still there. The two facets are
// INDEPENDENT: a failed account read must not silence the catalog's own honest
// verdict.
export function routeChoiceGroups({
    harnesses = [], modelSources = [], providers = [], currentChoice = '',
    catalogKnown = true, accountsKnown = true, providerProfiles = {},
} = {}) {
    return routeEditor.routeChoiceGroups({
        harnesses, modelSources, providers, currentChoice,
        catalogKnown, accountsKnown, providerProfiles,
    });
}

export function indexProfilesByHarness(payload) {
    return routeEditor.indexProfilesByHarness(payload);
}

// One index entry, whichever spelling it arrived in: the index emits
// `{id, enabled, name}` objects; older call sites and tests still hand plain id
// strings, which read as enabled and named by their id (the same fail-open
// rule as the index).
function profileEntry(entry) {
    return routeEditor.profileEntry(entry);
}

// A direct api_chat TRIAD row's saved delivery (#1334); a row loaded without it is a
// pre-#1334 packet row and stays bare unless the owner switches it.
export const DELIVERY_NATIVE = 'native', DELIVERY_PACKET = 'packet';

export function buildReviewerSlotsSetting(state) {
    const triadOut = (row) => ({ ...rowOut(row), ...(!row.subagent_id && row.route?.kind !== ROUTE_KIND_SESSION
        && [DELIVERY_NATIVE, DELIVERY_PACKET].includes(row.delivery) ? { delivery: row.delivery } : {}) });
    const rowOut = (row) => {
        // The two stored forms are mutually exclusive: a configured-subagent
        // reference never duplicates route knobs (the roster row is their
        // SSOT), and an empty explicit effort is OMITTED so the roster row's
        // own effort keeps deciding.
        if (row.subagent_id) {
            const out = {
                slot_id: String(row.slot_id || ''),
                subagent_id: String(row.subagent_id),
            };
            if (row.effort) out.effort = String(row.effort);
            return out;
        }
        const out = {
            slot_id: String(row.slot_id || ''),
            route: routeEditor.serializeRouteSpec(row.route, {
                apiKind: ROUTE_KIND_API,
                credentialField: 'profile_id',
            }),
        };
        if (row.effort) out.effort = String(row.effort);
        if (row.processing_preference) out.processing_preference = String(row.processing_preference);
        return out;
    };
    const advisory = state.advisory || {};
    let advisoryOut;
    if (advisory.subagent_id) {
        advisoryOut = {
            enabled: advisory.enabled !== false,
            subagent_id: String(advisory.subagent_id),
        };
        if (advisory.effort) advisoryOut.effort = String(advisory.effort);
    } else {
        // The advisory row is on the SHARED closed route vocabulary now:
        // api_chat (routed catalog model, bounded native inspection episode)
        // or agent_session. The retired legacy kind 'api' (Claude-SDK
        // spellings) still parses server-side but is never WRITTEN again.
        advisoryOut = {
            enabled: advisory.enabled !== false,
            route: routeEditor.serializeRouteSpec(advisory.route, {
                apiKind: ROUTE_KIND_API, credentialField: 'profile_id',
            }),
            effort: advisory.route?.kind === ROUTE_KIND_SESSION
                ? String(advisory.effort || '') : (advisory.effort || 'low'),
        };
        if (advisory.processing_preference) advisoryOut.processing_preference = String(advisory.processing_preference);
    }
    const setting = {
        triad: (state.triad || []).map(triadOut),
        scope: (state.scope || []).map(rowOut),
        advisory: advisoryOut,
    };
    // First materialization of a default panel preserves its displayed deep row:
    // synthesis may change once the panel has saved provenance. Unrelated Save
    // still omits the whole untouched panel in reviewerSlotsSavePayload. Existing
    // structured panels and repair placeholders keep their legacy omission.
    if (state.deepReview && (state.deepReview.materialized !== false
        || (state.source === 'default' && state.deepReview.synthesizedFrom))) {
        setting.deep_review = rowOut({ ...state.deepReview, slot_id: '' });
        delete setting.deep_review.slot_id;
    }
    return JSON.stringify(setting);
}

export function deepReviewMetaNotes(row) {
    // The deep row's two owner-facing facts beside its badge: a synthesized
    // row has not been saved yet, and a blanked model box is a
    // typed save refusal (owner fork 3 = A) — said HERE, before the 400.
    const notes = [];
    if (row?.synthesizedFrom && row.materialized === false) {
        notes.push(`Not saved as a row yet — shown from ${row.synthesizedFrom}; stored when edited or when the default panel is first saved`);
    }
    if (row?.materialized !== false && !row?.subagent_id && row?.route?.kind !== ROUTE_KIND_SESSION
        && !String(row?.route?.target_id || '').trim()) {
        notes.push('Model id required — an empty model id is refused at save; enter one, or pick a configured subagent or an agent');
    }
    return notes;
}

const DEEP_NATIVE_NOTE = 'Native inspection episode — reads the repository with host read-only tools (reads host-observed); the memory whitelist reaches it inline byte-exact';

export function deepReviewDeliveryNote(row, { roster = [], rosterKnown = true, harnesses = {}, catalogKnown = true, routeStatus = true } = {}) {
    // Delivery is the same class the advisory row picks from: an API MODEL —
    // a bare route or a configured subagent — runs the bounded native
    // inspection episode; an agent reads with its own tools in a session.
    if (row?.subagent_id) {
        const ref = (roster || []).find((item) => String(item.subagent_id || '') === String(row.subagent_id || ''));
        if (!ref) {
            return rosterKnown
                ? 'Delivery follows the roster row — none exists with this ID, so the review will refuse rather than reroute'
                : 'Delivery follows the roster row — the roster could not be read, so it is not shown';
        }
        return ref.route?.kind === ROUTE_KIND_SESSION
            ? 'Agent session — reads the repository with its own tools (reads not host-observed); the memory whitelist reaches it inline byte-exact'
            : DEEP_NATIVE_NOTE;
    }
    if (row?.route?.kind === ROUTE_KIND_SESSION) {
        return `${capabilityBadge(row, harnesses, { catalogKnown, routeStatus })} — reads not host-observed`;
    }
    return DEEP_NATIVE_NOTE;
}

// ---------------------------------------------------------------------------
// Configured-subagent references (decision 5A). The roster list reuses the
// Available-subagents data source: settings.js hands the loaded settings
// document to adoptSubagentRoster below, parsed by the SAME parser that
// section uses — never a second schema.
// ---------------------------------------------------------------------------

export function advisoryReferenceTransition(advisory, subagentId) {
    // Crossing from an inline branch drops the inline effort ('' = the roster
    // row's own effort decides — the api default 'low' must not silently
    // override the reference); a reference-to-reference switch keeps an
    // explicit owner override (sol delta-review finding).
    return {
        subagent_id: String(subagentId || ''),
        effort: advisory?.subagent_id ? String(advisory.effort || '') : '',
    };
}

export function encodeReviewerChoice(row) {
    return row?.subagent_id
        ? SUBAGENT_CHOICE_PREFIX + String(row.subagent_id)
        : encodeRouteChoice(row);
}

export function reviewerChoiceGroups({
    roster = [], rosterKnown = true, row = {}, harnesses = [], modelSources = [],
    providers = [], catalogKnown = true, accountsKnown = true, providerProfiles = {}, processingPreference = '',
} = {}) {
    // Decision 1=B: ONE flat picker — the Available-subagents references lead
    // (facts-first labels, decision 2=A), then the inline channels. A saved
    // reference that fell out of the roster keeps its option (the same
    // survive-the-save rule the roster select carried); an inline row passes
    // its own choice down so an undiscovered harness stays displayable too.
    const groups = [];
    const savedId = String(row?.subagent_id || '');
    const rosterOptions = subagentOptionsFor(roster, savedId, { rosterKnown, processingPreference })
        .map((option) => ({ ...option, value: SUBAGENT_CHOICE_PREFIX + option.value }));
    if (rosterOptions.length) {
        groups.push({ label: 'Available subagents', options: rosterOptions });
    }
    const currentChoice = savedId ? '' : encodeRouteChoice(row);
    groups.push(...routeChoiceGroups({
        harnesses, modelSources, providers, currentChoice,
        catalogKnown, accountsKnown, providerProfiles,
    }));
    return groups;
}

export function subagentOptionLabel(row, handle = routeEditor.subagentHandle(row)) {
    // The owner's row switch is a FACT, so it rides with the facts: after the
    // caption it would read as part of the owner's own prose.
    const facts = row?.enabled === false ? `${handle} · switched off` : handle;
    // Free text is a caption, never identity: one line, bounded, with the
    // characters that could visually reorder or break the facts stripped
    // (bidi controls, newlines).
    const use = String(row?.recommended_use || '')
        .replace(/[\u202A-\u202E\u2066-\u2069\u200E\u200F\u061C]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
    // Code POINTS, not UTF-16 units: a slice must never split a surrogate pair.
    const points = Array.from(use);
    const hint = points.length > 48 ? `${points.slice(0, 45).join('')}…` : use;
    return facts + (hint ? ` — ${hint}` : '');
}

export function subagentOptionsFor(roster, savedId, { rosterKnown = true, processingPreference = '' } = {}) {
    // Same survive-the-save rule as profileOptionsFor: the select's value must
    // EXIST as an option, or the browser silently redraws the row as the first
    // roster entry and the next Save really rewires the reviewer. And the
    // absence claim follows provenance: only a roster that was actually READ
    // may say a saved reference is not in it.
    // Label contract: the row's HANDLE leads — its route target plus its own
    // effective facets, the same name Ouroboros sees — so the delivery is visible
    // BEFORE selection and no stored label can disguise it; the description is
    // a trimmed, sanitized single-line caption after it. The stored id is the
    // option VALUE only (the reviewer reference follows the row through it).
    // An owner-disabled roster row is withdrawn from NEW choices (the parser
    // refuses a fresh reference to it); an already-selected one stays visible
    // and annotated rather than being silently cleared.
    const handles = routeEditor.rosterHandles(roster, processingPreference);
    const saved = String(savedId || '');
    const options = (roster || [])
        .filter((row) => row.enabled !== false || String(row.subagent_id || '') === saved)
        .map((row) => ({
            value: String(row.subagent_id || ''),
            label: subagentOptionLabel(row, handles.get(String(row.subagent_id || ''))),
        }));
    if (saved && !options.some((option) => option.value === saved)) {
        options.push({
            value: saved,
            label: `${saved} (${rosterKnown ? 'not in the roster' : 'not checked'})`,
        });
    }
    return options;
}

export function describeSubagentReference(subagentId, roster, { rosterKnown = true, processingPreference = '', effort = '' } = {}) {
    // Route/model/account come from the roster; the reviewer row may override
    // the separate effort preference. Compound slugs retain their identity.
    const row = (roster || []).find(
        (item) => String(item.subagent_id || '') === String(subagentId || ''));
    if (!row) {
        return rosterKnown
            ? 'Configured subagent — no roster row with this ID exists; the saved reference is kept and the review will refuse rather than reroute'
            : 'Configured subagent — the roster could not be read, so its route is not shown; the saved reference is unchanged';
    }
    const route = row.route || {};
    const parts = [];
    if (route.kind === ROUTE_KIND_SESSION) {
        const split = splitSessionTarget(route.target_id);
        parts.push(split.harness ? `${split.harness} session` : 'agent session');
        if (split.model) parts.push(split.model);
    } else {
        const fields = routeEditor.routeModelFields(route);
        parts.push(fields.subscription ? `${fields.source} model · ${fields.model}`
            : (route.target_id ? `API model ${route.target_id}` : 'API model (unset)'));
    }
    if (routeEditor.routeSupportsAccount(route) && route.credential_profile_id) parts.push(`account ${route.credential_profile_id}`);
    const preferredEffort = (route.kind === ROUTE_KIND_SESSION
        ? routeEditor.compoundSessionEffort(route.target_id) : '') || effort || row.effort;
    if (preferredEffort) parts.push(`preferred effort ${preferredEffort}`);
    parts.push(`processing ${routeEditor.processingIntentLabel(row.processing_preference, processingPreference)}`);
    const off = row.enabled === false
        ? '. That row is switched off, so this reference is refused at save rather than rerouted — choose another reviewer or turn the row back on'
        : '';
    return `Runs as ${parts.join(' · ')} — route from its roster row under Available subagents${off}`;
}

export function describeLastExecution(entry) {
    if (!entry || typeof entry !== 'object') return '';
    const effective = entry.effective || {};
    const parts = [];
    // APPLIED facts only: a session run whose telemetry predates the engine
    // receipt shows honest absence, never the requested value as applied.
    // One quiet line (owner feedback): the route is spelled only when it says
    // more than the row's own delivery badge (which agent really ran),
    // and the timestamp is humanized — the raw route + ISO instant stay
    // recoverable in the line's tooltip (lastRunMetaTitle).
    const route = String(effective.route || '');
    if (route.startsWith('agent_session')) {
        const harness = route.slice('agent_session'.length).replace(/^:/, '');
        parts.push(harness ? `${harness} session` : 'agent session');
        parts.push(effective.model || 'model not disclosed');
    } else if (effective.model) {
        parts.push(effective.model);
    }
    if (effective.profile_id) parts.push(`account ${effective.profile_id}`);
    if (effective.access) parts.push(`access ${effective.access}`);
    // Effort reports stay structured and in Logs; prepared options are not applied facts.
    if (effective.verdict_method && effective.verdict_method !== 'structured'
        && effective.verdict_method !== 'strict_parse') {
        parts.push(`verdict via ${effective.verdict_method.replace(/_/g, ' ')}`);
    }
    const deltas = Array.isArray(entry.capability_delta) ? entry.capability_delta.length : 0;
    if (deltas) parts.push(`${deltas} capability delta${deltas === 1 ? '' : 's'} disclosed`);
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    const processing = routeEditor.processingExecutionText(effective.processing || entry.processing);
    if (processing) parts.push(processing);
    if (when) parts.push(when);
    return parts.join(' · ');
}

/**
 * Whether the receipt was produced by a DIFFERENT route than the row now holds.
 * A receipt older than the `requested` field carries no comparison subject, so
 * it claims nothing: absence of evidence is not evidence of a change.
 */
export function lastRunRouteChanged(entry, row) {
    const requested = entry?.requested;
    if (!requested || typeof requested !== 'object') return false;
    const wasReference = String(requested.subagent_id || '');
    const isReference = String(row?.subagent_id || '');
    if (wasReference || isReference) return wasReference !== isReference;
    const wasSession = String(requested.route_kind || '') === ROUTE_KIND_SESSION;
    const isSession = row?.route?.kind === ROUTE_KIND_SESSION;
    if (wasSession !== isSession) return true;
    // A session is bound to its harness; the model it ran is APPLIED evidence,
    // not the row's assignment, so a model edit alone is not a route change.
    if (wasSession) {
        return splitSessionTarget(requested.session_target).harness
            !== splitSessionTarget(row?.route?.target_id).harness;
    }
    return String(requested.model || '') !== String(row?.route?.target_id || '');
}

/** The earlier route, named the way the owner picked it. */
function lastRunRanAs(entry, { harnesses = {}, modelSources = [], providerProfiles = {} } = {}) {
    const requested = entry?.requested || {};
    if (requested.subagent_id) {
        // Named from the receipt's OWN recorded route, never from today's roster.
        const session = String(requested.route_kind || '') === ROUTE_KIND_SESSION;
        const handle = routeEditor.subagentHandle({
            route: {
                kind: requested.route_kind,
                target_id: session ? requested.session_target : requested.model,
                credential_profile_id: requested.profile_id,
            },
            effort: requested.effort, processing_preference: requested.processing_preference,
        });
        return handle ? `the configured subagent ${handle}` : 'a configured subagent';
    }
    if (String(requested.route_kind || '') === ROUTE_KIND_SESSION) {
        const { harness } = splitSessionTarget(requested.session_target);
        const label = harnessPresentation(harness, {
            label: String(harnesses?.[harness]?.display_name || ''),
        }).label;
        return harness ? `a ${label} session` : 'an agent session';
    }
    const fields = routeEditor.routeModelFields(
        { kind: ROUTE_KIND_API, target_id: String(requested.model || '') },
        modelSources, { providerProfiles },
    );
    return fields.subscription
        ? `a model on ${fields.sourceLabel || fields.source}`
        : `an API model on ${fields.providerLabel}`;
}

/**
 * The meta line's own prefix for a last-run receipt. A receipt earned under a
 * route the row no longer holds is still shown — it is the last thing that
 * really ran — but it may not read as evidence about the current assignment.
 */
export function lastRunMetaPrefix(entry, row, context = {}) {
    return lastRunRouteChanged(entry, row)
        ? `Last run, before this row changed (it ran as ${lastRunRanAs(entry, context)})`
        : 'Last run';
}

function lastRunMetaTitle(entry) {
    // The tooltip keeps the raw facts the visible line compresses away.
    const route = String(entry?.effective?.route || 'api_chat');
    const ts = String(entry?.ts || '');
    return `UI projection of capability_delta (D22) — ran as ${route}${ts ? ` at ${ts}` : ''}`;
}

export function harnessModelsKnown(harness, catalogKnown = true) {
    // Discovery is TWO reads, not one. The catalog read can land — daemon
    // globally `running`, `catalogKnown` true — while THIS harness's model
    // list refuses: the endpoint answers `models: []` with a typed
    // `models_error` for exactly that harness (claudexor_accounts.py, the
    // per-harness `harness_models` probe). Reading the empty list as discovery
    // then labelled a saved model "(not in discovery)", which claims a
    // successful discovery proved its absence — while no discovery happened.
    return routeEditor.harnessModelsKnown(harness, catalogKnown);
}

export function modelsGapNote(harness, catalogKnown = true) {
    // The typed gap, SAID rather than left as a silently short list. Only when
    // the catalog itself was read: with the catalog unread the section note
    // above already explains everything, and this would be a second sentence
    // for the same silence.
    return routeEditor.modelsGapNote(harness, catalogKnown);
}

export function sessionModelOptions(harness, currentModel, { catalogKnown = true } = {}) {
    // The ONE model-options fragment for a session row's model select (triad,
    // scope, advisory, and the Subagents section import it too). "Engine
    // default model" is the empty tail; a SAVED model discovery no longer
    // lists keeps a "(not in discovery)" option, or the browser silently
    // redraws the select as the first entry and the next Save erases the pin
    // (same rule as profileOptionsFor / routeChoiceGroups).
    return routeEditor.sessionModelOptions(harness, currentModel, { catalogKnown });
}

export function profileOptionsFor(profiles, savedPin, { accountsKnown = true } = {}) {
    // Mirrors the model list's own rule: a SAVED pin the daemon no longer discovers
    // (account signed out, daemon down, profile renamed) matched no option, so the
    // select fell back to its first entry and redrew the row as "automatic rotation".
    // The pin only LOOKED gone — until the owner saved the panel, which then really
    // did delete it, silently widening which account the reviewer may spend.
    // A DISABLED account stays selectable, labeled "(disabled)": the engine's
    // typed refusal is the authority on whether a pinned run may use it
    // (D-U6), so the label is honesty, not a gate — hiding or greying the
    // option would be a second, client-side gate the design rejected.
    return routeEditor.profileOptionsFor(profiles, savedPin, { accountsKnown });
}

export function pinnedAccountWarning({ triad = [], scope = [], advisory = null, deepReview = null,
                                       profilesByHarness = {}, modelSources = [], accountsKnown = false } = {}) {
    // A removed (or signed-out) account must not silently reroute the row that
    // pinned it. `profileOptionsFor` already keeps such a pin selectable, so
    // the row itself never changes under the owner — this is the ONE actionable
    // sentence that says why it now reads "(not in discovery)".
    //
    // Only an ACTUALLY READ account list licenses the claim (BIBLE P1): with
    // the accounts facet unread every pin would look missing, and the tab's
    // service banner is already saying nobody could be asked.
    if (!accountsKnown) return '';
    const missing = [];
    const rows = [...triad, ...scope, ...(advisory ? [{ ...advisory, slot_id: 'advisory' }] : []),
                  ...(deepReview ? [{ ...deepReview, slot_id: 'deep_review' }] : [])];
    for (const row of rows) {
        // A configured-subagent reference carries no pin of its own — the
        // roster row is that route's SSOT, checked on its own surface.
        if (row?.subagent_id) continue;
        const route = row?.route || {};
        if (!routeEditor.routeSupportsAccount(route)) continue;
        const pin = String(route.profile_id || '');
        if (!pin) continue;
        const { harness } = routeEditor.routeModelFields(route, modelSources);
        if (!harness) continue;
        if ((profilesByHarness[harness] || []).some((entry) => profileEntry(entry).id === pin)) continue;
        const label = `${harness} · ${pin}`;
        if (!missing.includes(label)) missing.push(label);
    }
    if (!missing.length) return '';
    return `${missing.length === 1 ? 'A review row is' : `${missing.length} review rows are`} `
        + `pinned to an account the agent service no longer lists (${missing.join(', ')}). `
        + 'Those rows are shown as-is and will refuse rather than reroute — pick another '
        + 'account or automatic rotation below, or sign that account back in under Accounts.';
}

export function capabilityBadge(row, harnessesById, {
    catalogKnown = true, modelSources = [], providerProfiles = {}, routeStatus = true,
} = {}) {
    // DISPLAY-only facts: never a control (6.2).
    if (row.route.kind === ROUTE_KIND_SESSION) {
        // "route not discovered" is a claim about the ROUTE; with no successful
        // discovery it is a claim about the read. Say nothing rather than
        // something false — the one section note above already says why.
        if (!catalogKnown) return 'agent session — retrieves context with its own tools';
        const harness = harnessesById?.[splitSessionTarget(row.route.target_id).harness];
        const status = harness ? (harness.status || 'unknown') : 'not discovered';
        // The harness status tail yields to a row verdict: one availability claim per line.
        return `agent session — retrieves context with its own tools${routeStatus ? ` · route ${status}` : ''}`;
    }
    // An API row names the credential it spends, not a generic channel: the
    // owner chose a provider, so the badge says which key pays for the call.
    const fields = routeEditor.routeModelFields(row.route, modelSources, { providerProfiles });
    return fields.subscription ? 'Model call through subscription'
        : `${fields.providerLabel} API key`;
}

/** The advisory's one delivery difference, said beside the row that picks it. */
export function advisoryDeliveryNote(row, harnessesById, {
    catalogKnown = true, modelSources = [], providerProfiles = {}, routeStatus = true,
} = {}) {
    const badge = capabilityBadge(row, harnessesById, { catalogKnown, modelSources, providerProfiles, routeStatus });
    return row?.route?.kind === ROUTE_KIND_SESSION ? badge
        : `${badge} — runs a bounded inspection episode`;
}

export function advisoryRouteTransition(prev, decoded, memory = {}) {
    // Restore each source's own model/account when the owner returns to it.
    // A new source starts empty; it must not inherit another source's pin.
    // Legacy api/session memory remains readable for existing callers.
    //
    // The key is the FULL encoded choice, provider included: memory is written
    // under `encodeRouteChoice` (`api:openai`), so a bare `api` key looked up
    // nothing and every return to an API provider silently dropped the target
    // the owner had drafted for it.
    const current = (prev && typeof prev === 'object' && prev.kind)
        ? prev : { kind: ROUTE_KIND_API, target_id: '' };
    const choice = decoded.kind === ROUTE_KIND_SESSION ? `session:${decoded.harness}`
        : decoded.source ? `subscription:${decoded.source}`
            : `${API_CHOICE_PREFIX}${decoded.provider || routeEditor.DEFAULT_API_PROVIDER}`;
    const next = { ...memory, [encodeRouteChoice({ route: current })]: { ...current } };
    const stash = next[choice] || next[decoded.kind === ROUTE_KIND_SESSION ? 'session' : 'api'];
    return {
        route: stash && encodeRouteChoice({ route: stash }) === choice ? { ...stash }
            : routeEditor.changeRouteChoice(current, choice, { apiKind: ROUTE_KIND_API }),
        memory: next,
    };
}

// ---------------------------------------------------------------------------
// DOM section (Agents → Review lanes). State is module-local; collect is synchronous.
// ---------------------------------------------------------------------------

const state = {
    loaded: false,
    loadedDraft: null,
    loadedSetting: '',
    configError: '',
    loadError: '',
    source: '',
    triad: [],
    scope: [],
    advisory: { enabled: true, route: { kind: ROUTE_KIND_API, target_id: '' }, effort: 'low', subagent_id: '' },
    // The deep self-review singleton; `synthesizedFrom` names the legacy model
    // key when the server showed a row that is not saved yet (Save stores it).
    deepReview: { route: { kind: ROUTE_KIND_API, target_id: '' }, effort: '', subagent_id: '', synthesizedFrom: '', materialized: false },
    limits: { triad: 10, scope: 4, advisory: 1, deep_review: 1 },
    lastExecutions: {},
    catalogModels: [],
    modelSources: [],
    // The API providers this install can actually send to, and the setup
    // contract's names for them. Both come from the loaded settings document
    // (setReviewerSourceContext); an unread document offers no provider, so a
    // saved one is rescued as "(no key)" rather than silently swapped.
    providers: [],
    providerProfiles: {},
    modelCatalogNote: '',
    processingPreference: '',
    roleProcessingPreferences: null,
    resolvedProcessingPreferences: {},
    harnesses: [],
    profilesByHarness: {},
    // The configured-subagent roster (OUROBOROS_SUBAGENTS items) the reference
    // selects offer. Display-only here; the Available-subagents section owns
    // editing. rosterKnown follows the same provenance rule as the facets: an
    // unreadable roster licenses no absence claim about a saved reference.
    roster: [],
    rosterKnown: false,
    // PER-FACET provenance: the route/model lists come from the CATALOG facet,
    // the account pins from the ACCOUNTS facet, and one can be authoritative
    // while the other was never read. Only an `ok` facet may license a row-level
    // "(not in discovery)" label.
    catalogKnown: false,
    accountsKnown: false,
    // The live session verdict reads accounts and quota, not just the catalog.
    quotaKnown: false,
    snapshot: null,
    store: claudexorStatus,
    disposers: [],
    disposeChoosers: () => {},
    saveAttempted: false,
    onChange: () => {},
};

// Per-kind memory for the singleton route selects (see advisoryRouteTransition,
// which is singleton-generic despite its name): seeded from the LOADED setting
// so a kind round-trip restores the saved target.
const advisoryRouteMemory = { api: null, session: null };
const deepReviewRouteMemory = { api: null, session: null };

// The two single-row categories, rendered and bound by ONE renderer/binder
// (`singletonHtml` / `bindSingletonEvents`) parameterized by this table: the
// advisory (with its Enabled switch and the api-route `low` default) and the
// deep self-review row (no switch; '' = the Behavior-tab deep effort).
const SINGLETONS = {
    advisory: {
        attr: 'advisory', stateKey: 'advisory', lastKey: 'advisory_slot_1', ariaName: 'Advisory',
        rowId: 'reviewer-advisory-row', enabledToggle: true, apiEffortDefault: 'low', apiEffortLabel: 'low',
        // An empty advisory model is legal (the route default decides), so the
        // placeholder says that instead of dictating an id spelling.
        modelPlaceholder: 'Empty uses the default model',
        memory: advisoryRouteMemory, badgeOnReference: false,
        badge: (row, opts = {}) => advisoryDeliveryNote({ route: row.route || {} }, harnessesById(), {
            catalogKnown: state.catalogKnown, modelSources: state.modelSources,
            providerProfiles: state.providerProfiles, ...opts,
        }),
        extraMeta: () => [],
    },
    deepReview: {
        attr: 'deep-review', stateKey: 'deepReview', lastKey: 'deep_review_slot_1', ariaName: 'Deep self-review',
        rowId: 'reviewer-deep-review-row', enabledToggle: false, apiEffortDefault: '', apiEffortLabel: 'deep self-review effort',
        modelPlaceholder: 'Choose a model',
        memory: deepReviewRouteMemory, badgeOnReference: true, materializeOnEdit: true,
        badge: (row, opts = {}) => deepReviewDeliveryNote(row, {
            roster: state.roster, rosterKnown: state.rosterKnown, harnesses: harnessesById(), catalogKnown: state.catalogKnown, ...opts,
        }),
        extraMeta: deepReviewMetaNotes,
    },
};

// The multi-row categories the editor paints. ONE table drives rendering,
// lookup, adding and removal — a category is a table entry, never another
// `group === 'scope' ? … : …` ternary (three of those used to encode it).
export const CATEGORIES = {
    triad: {
        stateKey: 'triad', limitKey: 'triad', idPrefix: 'triad',
        rowsId: 'reviewer-triad-rows', limitId: 'reviewer-triad-limit', addId: 'btn-add-triad-slot',
        surfaceDefault: 'review effort', empty: 'No triad slots configured.',
    },
    scope: {
        stateKey: 'scope', limitKey: 'scope', idPrefix: 'scope',
        rowsId: 'reviewer-scope-rows', limitId: 'reviewer-scope-limit', addId: 'btn-add-scope-slot',
        surfaceDefault: 'scope review effort', empty: 'No scope slots configured.',
    },
};

function categoryRows(group) {
    return state[CATEGORIES[group].stateKey];
}

export function renderReviewerSlotsSection() {
    return `
        <div class="form-section" id="reviewer-slots-section">
            <h3>Review lanes</h3>
            <div class="settings-section-copy">
                Who reviews each commit: the triad rows, the scope rows, and one optional advisory
                pre-reviewer. Each row picks its reviewer from one list — a configured subagent
                from Available subagents above, an API model, or an agent on your subscription —
                plus its own reasoning effort.
            </div>
            <div class="settings-inline-note">
                Saved lane changes apply from the next task: a task that is already running
                keeps the reviewer configuration it started with.
            </div>
            <div class="settings-inline-note">
                Rows routed to a subscription never fall back to API spend: if every eligible window
                is exhausted, the review waits for capacity. Commit, plan, scope, advisory, skill
                review and task acceptance all follow their configured rows — task acceptance runs
                the triad rows on their own delivery (API packet or native inspection,
                configured-subagent inspection, or agent session), so an all-subscription triad puts every substantive
                task's acceptance panel on the subscription as well.
            </div>
            <div id="reviewer-slots-error" class="ui-status" data-tone="error" hidden></div>
            <div id="reviewer-model-catalog-status" class="ui-status" data-tone="warn" role="status" hidden></div>
            <div id="reviewer-slots-pins" class="settings-inline-status" data-tone="warn" hidden></div>
            <div class="reviewer-slots-group">
                <div class="reviewer-slots-head">
                    <h4 class="reviewer-slots-heading">Triad slots <span class="muted" id="reviewer-triad-limit" title="The commit gate's real ceiling"></span></h4>
                    <button type="button" class="btn btn-default" id="btn-add-triad-slot">Add triad slot</button>
                </div>
                <div id="reviewer-triad-rows" class="reviewer-slot-rows"></div>
            </div>
            <div class="reviewer-slots-group">
                <div class="reviewer-slots-head">
                    <h4 class="reviewer-slots-heading">Scope slots <span class="muted" id="reviewer-scope-limit" title="The scope pool's real width"></span></h4>
                    <button type="button" class="btn btn-default" id="btn-add-scope-slot">Add scope slot</button>
                </div>
                <div class="settings-inline-note">Every scope row reads the repository itself instead of being handed one
                    assembled pack. An API model runs a bounded inspection episode with host read-only tools, and
                    Ouroboros records which sources it read. An agent reads with its own tools; those reads are
                    recovered from the harness run journal when it records them, and disclosed as unobserved when it
                    does not. Read coverage is diagnostic and does not invalidate the review.</div>
                <div id="reviewer-scope-rows" class="reviewer-slot-rows"></div>
            </div>
            <div class="reviewer-slots-group">
                <h4 class="reviewer-slots-heading">Advisory pre-reviewer</h4>
                <div id="reviewer-advisory-row" class="reviewer-slot-rows"></div>
            </div>
            <div class="settings-inline-note">
                Disabling the advisory is a standing decision with a constitutional consequence:
                every reviewed commit then records an <strong>audited bypass</strong> instead of an
                advisory verdict. Nothing is skipped silently.
            </div>
            <div class="reviewer-slots-group">
                <h4 class="reviewer-slots-heading">Deep self-review</h4>
                <details class="settings-subsection">
                <summary>How whole-system review works</summary>
                <div class="settings-inline-note settings-subsection-body">
                    Who runs <code>/review</code>, the whole-system review against BIBLE.md. Delivery is the same
                    as the advisory's: an API model here — a model you name or a configured subagent — reads the
                    repository itself in a bounded native
                    inspection episode with host-observed reads, core rules supplied inline and reference books available on demand; an
                    agent on your subscription reads it in its
                    own session (reads not host-observed). Either way the memory whitelist reaches the reviewer
                    inline byte-exact — memory is never receipt-checked. The row's effort outranks the Behavior-tab deep
                    self-review effort; every report starts with a provenance header naming the delivery.
                </div>
                </details>
                <div id="reviewer-deep-review-row" class="reviewer-slot-rows"></div>
            </div>
        </div>
    `;
}

function harnessesById() {
    const map = {};
    for (const h of state.harnesses) map[h.id] = h;
    return map;
}

function selectHtml(attrs, groups, selected) {
    return routeEditor.selectHtml(attrs, groups, selected);
}

/** How a direct API triad row receives the work: it reads it, or it gets the packet. */
export const deliverySelectHtml = (attrs, row) => selectHtml(attrs, [{ label: '', options: [
    { value: DELIVERY_NATIVE, label: 'Reads the work itself' },
    { value: DELIVERY_PACKET, label: 'Packet — for models without tool calling' },
] }], row?.delivery === DELIVERY_NATIVE ? DELIVERY_NATIVE : DELIVERY_PACKET);

function effortSelectHtml(attrs, selected, surfaceDefault) {
    // Compact closed state (owner feedback on field proportions): the wordy
    // "Default (scope review effort)" label made this select as wide as the
    // model field. Which setting the default follows moves to the tooltip.
    return routeEditor.effortSelectHtml(attrs, selected, surfaceDefault);
}

/**
 * The chip names the SOURCE, never the channel alone (owner decision 4A): an
 * API row says which provider serves it, a subscription row its model source,
 * a session row the agent. The mark stays the harness's own.
 */
export function reviewerRouteIdentityMarkup(route, harnesses = {}, {
    catalogKnown = false, modelSources = [], providerProfiles = {},
} = {}) {
    const fields = routeEditor.routeModelFields(route, modelSources, { providerProfiles });
    const label = (descriptors) => routeEditor.sourceIdentityLabel(route, {
        modelSources, providerProfiles, harnesses: descriptors,
    });
    if (fields.subscription) return harnessIdentityMarkup(fields.harness || fields.source, {
        label: label([]), className: 'reviewer-slot-route-identity',
    });
    if (route?.kind !== ROUTE_KIND_SESSION) {
        return harnessIdentityMarkup('api', {
            channel: 'api',
            label: label([]),
            className: 'reviewer-slot-route-identity',
        });
    }
    const split = splitSessionTarget(route.target_id);
    const harness = harnesses?.[split.harness];
    // A retained daemon name is evidence only while the catalog read is known;
    // during a gap the presentation catalog supplies the stable product name.
    const display = harnessPresentation(split.harness, {
        label: catalogKnown ? String(harness?.display_name || '') : '',
    }).label;
    return harnessIdentityMarkup(split.harness, {
        label: label([{ id: split.harness, display_name: display }]),
        className: 'reviewer-slot-route-identity',
    });
}

function reviewerPickerHtml(attrs, row) {
    // The one flat reviewer picker (decision 1=B): roster references first,
    // then the inline sources.
    const groups = reviewerChoiceGroups({
        roster: state.roster,
        rosterKnown: state.rosterKnown,
        row,
        harnesses: state.harnesses,
        modelSources: state.modelSources,
        providers: state.providers,
        providerProfiles: state.providerProfiles,
        catalogKnown: state.catalogKnown,
        accountsKnown: state.accountsKnown,
        processingPreference: state.processingPreference,
    });
    return selectHtml(attrs, groups, encodeReviewerChoice(row));
}

function identityContext() {
    return {
        catalogKnown: state.catalogKnown,
        modelSources: state.modelSources,
        providerProfiles: state.providerProfiles,
    };
}

/** The exact stored spelling, disclosed where it can inform but not instruct. */
export function savedTargetNote(row) {
    const route = row?.route || {};
    if (route.kind === ROUTE_KIND_SESSION) return '';
    const target = String(route.target_id || '').trim();
    // An empty draft (`openai::` with no model yet) has nothing to disclose.
    if (!target || !routeEditor.routeModelFields(route).model.trim()) return '';
    return `stored as ${target}`;
}

/**
 * The meta line: badge facts, then the stored id in its own span (patched live
 * while the owner types a model), then the last-run projection.
 */
function metaLineHtml(parts, row, last) {
    const saved = savedTargetNote(row);
    const lastRun = lastRunParts(last, row);
    return `<div class="reviewer-slot-meta muted"${last ? ` title="${escapeHtml(lastRunMetaTitle(last))}"` : ''}>${escapeHtml(parts.join(' · '))}<span data-saved-target>${saved ? escapeHtml(` · ${saved}`) : ''}</span>${lastRun.length ? escapeHtml(` · ${lastRun.join(' · ')}`) : ''}</div>`;
}

/** Keep the disclosed stored id current without repainting the row (and losing the caret). */
function refreshSavedTarget(element, row) {
    const span = element?.querySelector?.('[data-saved-target]');
    if (!span) return;
    const note = savedTargetNote(row);
    span.textContent = note ? ` · ${note}` : '';
}

function lastRunParts(entry, row) {
    if (!entry) return [];
    const text = describeLastExecution(entry);
    if (!text) return [];
    return [`${lastRunMetaPrefix(entry, row, {
        harnesses: harnessesById(),
        modelSources: state.modelSources,
        providerProfiles: state.providerProfiles,
    })}: ${text}`];
}

function subagentIdentityMarkup(row) {
    // Identity follows the DERIVED roster route without changing its delivery.
    const rosterRow = (state.roster || []).find(
        (item) => String(item.subagent_id || '') === String(row.subagent_id || ''));
    const route = rosterRow?.route || { kind: ROUTE_KIND_API, target_id: '' };
    return reviewerRouteIdentityMarkup(route, harnessesById(), identityContext());
}

/** Everything a status tick can change on screen: facets, catalogs, pins, and each session row's verdict. */
export function repaintSignature(view = state) {
    const rows = [...(view.triad || []), ...(view.scope || []), view.advisory, view.deepReview];
    return JSON.stringify([view.catalogKnown, view.accountsKnown, view.quotaKnown, view.harnesses, view.profilesByHarness,
        accountRows(view.snapshot), view.snapshot?.quota || [], rows.map((row) => sessionVerdictNote(row, view))]);
}

/** The same live verdict the Available-subagents cards show, for a session row. */
export function sessionVerdictNote(row, view = state) {
    if (row?.route?.kind !== ROUTE_KIND_SESSION) return '';
    return sessionRouteVerdict(row, { snapshot: { ...(view.snapshot || {}), harnesses: view.harnesses },
        catalogKnown: view.catalogKnown, accountsKnown: view.accountsKnown, quotaKnown: view.quotaKnown }).text;
}

function rowHtml(row, group) {
    const { catalogKnown, accountsKnown } = state;
    const label = `${group === 'triad' ? 'Triad' : 'Scope'} reviewer ${categoryRows(group).indexOf(row) + 1}`;
    if (row.subagent_id) {
        const last = state.lastExecutions[row.slot_id];
        const metaParts = [
            describeSubagentReference(row.subagent_id, state.roster, { rosterKnown: state.rosterKnown, processingPreference: state.processingPreference, effort: row.effort }),
        ];
        return `
        <div class="reviewer-slot-row" data-slot-group="${group}" data-slot-id="${escapeHtml(row.slot_id)}">
            ${subagentIdentityMarkup(row)}
            <div class="reviewer-slot-controls">
                ${reviewerPickerHtml(`data-slot-route aria-label="${label} source"`, row)}
                ${effortSelectHtml(`data-slot-effort aria-label="${label} reasoning effort"`, row.effort || '', 'subagent default')}
                <button type="button" class="btn btn-default" data-slot-remove title="Remove this slot">Remove</button>
            </div>
            ${metaLineHtml(metaParts, row, last)}
        </div>
    `;
    }
    const session = row.route.kind === ROUTE_KIND_SESSION;
    const split = routeEditor.routeModelFields(row.route, state.modelSources);
    const harness = session ? routeEditor.accountScopedModelCatalog(harnessesById()[split.harness], row.route.profile_id) : null;
    const modelOptions = sessionModelOptions(harness, split.model, { catalogKnown });
    const profiles = state.profilesByHarness[split.harness] || [];
    const profileOptions = profileOptionsFor(profiles, row.route.profile_id, { accountsKnown: accountsKnown && Boolean(split.harness) });
    const last = state.lastExecutions[row.slot_id];
    // ONE quiet meta line per row (owner feedback): the delivery badge, the
    // exact stored id and the last-run projection share it; nothing is dropped
    // — the raw route + ISO timestamp live in the tooltip.
    const verdict = sessionVerdictNote(row);
    const metaParts = [capabilityBadge(row, harnessesById(), {
        catalogKnown, modelSources: state.modelSources, providerProfiles: state.providerProfiles, routeStatus: !verdict,
    }), verdict].filter(Boolean);
    const modelsGap = session ? modelsGapNote(harness, catalogKnown) : '';
    if (modelsGap) metaParts.push(modelsGap);
    const surfaceDefault = CATEGORIES[group]?.surfaceDefault || 'review effort';
    return `
        <div class="reviewer-slot-row" data-slot-group="${group}" data-slot-id="${escapeHtml(row.slot_id)}">
            ${reviewerRouteIdentityMarkup(row.route, harnessesById(), identityContext())}
            <div class="reviewer-slot-controls">
                ${reviewerPickerHtml(`data-slot-route aria-label="${label} source"`, row)}
                ${session ? modelChooserHtml(`data-slot-model aria-label="${label} agent model"`, split.model, `reviewer-${row.slot_id}-models`, modelOptions, { placeholder: 'Engine default model' })
                    : routeEditor.routeModelInputHtml(`data-slot-custom-api aria-label="${label} model"`, row.route, state.catalogModels, `reviewer-${row.slot_id}-models`)}
                ${routeEditor.routeSupportsAccount(row.route) ? selectHtml(`data-slot-profile aria-label="${label} account"`, [{ label: '', options: profileOptions }], row.route.profile_id || '') : ''}
                ${effortSelectHtml(`data-slot-effort aria-label="${label} reasoning effort"`, row.effort, surfaceDefault)}
                ${group === 'triad' && !session ? deliverySelectHtml(`data-slot-delivery aria-label="${label} delivery"`, row) : ''}
                <button type="button" class="btn btn-default" data-slot-remove title="Remove this slot">Remove</button>
            </div>
            ${routeEditor.processingDetailsHtml(`data-slot-processing aria-label="${label} processing"`, row.processing_preference, state.processingPreference)}
            ${metaLineHtml(metaParts, row, last)}
        </div>
    `;
}

function singletonHtml(spec) {
    // ONE renderer for both single-row categories (advisory, deep self-review):
    // the same picker, model/account/effort controls and meta line the triad
    // rows use, parameterized by the SINGLETONS entry — never a second copy
    // per category.
    const row = state[spec.stateKey];
    const { catalogKnown, accountsKnown } = state;
    const a = spec.attr;
    const last = state.lastExecutions[spec.lastKey];
    const enabled = spec.enabledToggle
        ? `<label class="local-toggle ui-field ui-field-inline"><input class="ui-checkbox" type="checkbox" data-${a}-enabled aria-label="${spec.ariaName} enabled" ${row.enabled !== false ? 'checked' : ''}> Enabled</label>`
        : '';
    const meta = (parts) => metaLineHtml(parts, row, last);
    if (row.subagent_id) {
        const metaParts = [
            describeSubagentReference(row.subagent_id, state.roster, { rosterKnown: state.rosterKnown, processingPreference: state.processingPreference, effort: row.effort }),
            ...(spec.badgeOnReference ? [spec.badge(row)] : []),
            ...spec.extraMeta(row),
        ];
        return `
        <div class="reviewer-slot-row" data-${a}-row>
            ${subagentIdentityMarkup(row)}
            <div class="reviewer-slot-controls">
                ${enabled}
                ${reviewerPickerHtml(`data-${a}-route aria-label="${spec.ariaName} reviewer"`, row)}
                ${effortSelectHtml(`data-${a}-effort aria-label="${spec.ariaName} effort"`, row.effort || '', 'subagent default')}
            </div>
            ${meta(metaParts)}
        </div>
    `;
    }
    // Both singletons share the closed route vocabulary (api_chat |
    // agent_session), labeled neutrally, never a vendor name (the retired
    // Claude-SDK 'api' kind is parse-only migration now).
    const session = row.route?.kind === ROUTE_KIND_SESSION;
    const split = routeEditor.routeModelFields(row.route, state.modelSources);
    // Session branch: the SAME model-options fragment the triad rows use —
    // rewriting it here would lose the "(not in discovery)" guard and let a
    // Save with the daemon down erase the owner's model. Api branch: the same
    // catalog-assisted free-text entry the triad rows use.
    const harness = routeEditor.accountScopedModelCatalog(harnessesById()[split.harness], row.route?.profile_id);
    const modelOptions = session ? sessionModelOptions(harness, split.model, { catalogKnown }) : [];
    const profiles = state.profilesByHarness[split.harness] || [];
    const profileOptions = profileOptionsFor(profiles, row.route?.profile_id, { accountsKnown: accountsKnown && Boolean(split.harness) });
    const verdict = sessionVerdictNote(row);
    const metaParts = [spec.badge(row, { routeStatus: !verdict }), verdict, ...spec.extraMeta(row)].filter(Boolean);
    const modelsGap = session ? modelsGapNote(harness, catalogKnown) : '';
    if (modelsGap) metaParts.push(modelsGap);
    return `
        <div class="reviewer-slot-row" data-${a}-row>
            ${reviewerRouteIdentityMarkup(row.route, harnessesById(), identityContext())}
            <div class="reviewer-slot-controls">
                ${enabled}
                ${reviewerPickerHtml(`data-${a}-route aria-label="${spec.ariaName} reviewer"`, row)}
                ${session
                    ? modelChooserHtml(`data-${a}-model aria-label="${spec.ariaName} agent model"`, split.model, `${a}-models`, modelOptions, { placeholder: 'Engine default model' })
                    : routeEditor.routeModelInputHtml(`data-${a}-api-model aria-label="${spec.ariaName} model"`, row.route, state.catalogModels, `${a}-models`, { placeholder: split.subscription ? 'Choose a model' : spec.modelPlaceholder })}
                ${routeEditor.routeSupportsAccount(row.route) ? selectHtml(`data-${a}-profile aria-label="${spec.ariaName} credential account"`, [{ label: '', options: profileOptions }], row.route?.profile_id || '') : ''}
                ${effortSelectHtml(
                    `data-${a}-effort aria-label="${spec.ariaName} effort"`,
                    session ? row.effort : (row.effort === spec.apiEffortDefault ? '' : row.effort),
                    session ? 'route default' : spec.apiEffortLabel,
                )}
            </div>
            ${routeEditor.processingDetailsHtml(`data-${a}-processing aria-label="${spec.ariaName} processing"`, row.processing_preference, processingInheritance(row))}
            ${meta(metaParts)}
        </div>
    `;
}

function reviewerRowError(row, allowEmpty = false) {
    const none = { message: '', field: '' };
    if (row.subagent_id) return none;
    const route = row.route || {};
    if (row.processing_preference && !routeEditor.PROCESSING_CHOICES.includes(row.processing_preference)) return { message: 'Choose Standard, Fast, Economy or inherited processing.', field: 'processing' };
    if (!allowEmpty && !String(route.target_id || '').trim()) return { message: 'Choose a model or reviewer source.', field: 'model' };
    if (routeEditor.routeModelFields(route).subscription && !routeEditor.routeModelFields(route).model.trim()) return { message: 'Choose a subscription model.', field: 'model' };
    const conflict = route.kind === ROUTE_KIND_SESSION
        ? routeEditor.compoundSessionEffortConflict(route.target_id, row.effort) : '';
    return conflict ? { message: `Reasoning effort conflicts with the model’s ${conflict} effort.`, field: 'effort' } : none;
}

function reviewerValidationRows(draft) {
    return [
        ...['triad', 'scope'].flatMap((group) => (draft[group] || []).map((row, index) => ({
            row, key: row.slot_id, label: `${group === 'triad' ? 'Triad' : 'Scope'} reviewer ${index + 1}`,
        }))),
        ...(draft.advisory ? [{ row: draft.advisory, key: 'advisory', label: 'Advisory', allowEmpty: true }] : []),
        ...(draft.deepReview && draft.deepReview.materialized !== false
            ? [{ row: draft.deepReview, key: 'deep-review', label: 'Deep self-review' }] : []),
    ];
}

export function reviewerSlotsDraftErrors(draft) {
    if (!draft.loaded) return [];
    const errors = ['triad', 'scope'].filter((group) => !draft[group]?.length)
        .map((group) => `Add at least one ${group} reviewer.`);
    for (const { row, label, allowEmpty } of reviewerValidationRows(draft)) {
        const { message: error } = reviewerRowError(row, allowEmpty);
        if (error) errors.push(`${label}: ${error}`);
    }
    return errors;
}

export function validateReviewerSlots() { return reviewerSlotsDraftErrors(state); }

export function noteReviewerSlotsSaveAttempt() {
    state.saveAttempted = true;
    reviewerValidationRows(state).forEach(({ row }) => { row._uiAttempted = true; });
    paintReviewerValidation();
}

function paintReviewerValidation() {
    if (typeof document === 'undefined') return;
    const section = document.getElementById('reviewer-slots-section');
    if (!section) return;
    for (const { row, key, allowEmpty } of reviewerValidationRows(state)) {
        const el = [...section.querySelectorAll('[data-slot-id]')].find((node) => node.dataset.slotId === key)
            || section.querySelector(`[data-${key}-row]`);
        if (!el) continue;
        const { message: error, field: invalidField } = row._uiAttempted ? reviewerRowError(row, allowEmpty) : { message: '', field: '' };
        const processingSummary = el.querySelector('[data-processing-summary]');
        if (processingSummary) processingSummary.textContent = routeEditor.processingIntentLabel(row.processing_preference, processingInheritance(row));
        let message = el.querySelector('[data-reviewer-validation]');
        if (!message) { message = document.createElement('div'); message.dataset.reviewerValidation = ''; message.className = 'ui-status ui-field-help'; el.appendChild(message); }
        message.id = `reviewer-${key}-error`;
        Object.assign(message, { textContent: error, hidden: !error });
        message.dataset.tone = 'error';
        el.toggleAttribute('data-invalid', Boolean(error));
        const meta = el.querySelector('.reviewer-slot-meta');
        if (meta) meta.id = `reviewer-${key}-meta`;
        el.querySelectorAll('input, select').forEach((field) => {
            field.setAttribute('aria-describedby', `${meta?.id || ''} ${message.id}`.trim());
            const isModel = field.hasAttribute('data-model-chooser');
            const isEffort = field.matches('[data-slot-effort], [data-advisory-effort], [data-deep-review-effort]');
            const isProcessing = field.matches('[data-slot-processing], [data-advisory-processing], [data-deep-review-processing]');
            field.setAttribute('aria-invalid', String(Boolean(error) && (invalidField === 'model' ? isModel : invalidField === 'processing' ? isProcessing : isEffort)));
        });
    }
    if (state.saveAttempted && !state.loadError && !state.configError) {
        const box = document.getElementById('reviewer-slots-error');
        const errors = validateReviewerSlots();
        if (box) Object.assign(box, { textContent: errors.join(' '), hidden: !errors.length });
    }
}

function renderRows({ discoveryOnly = false } = {}) {
    const catalogStatus = document.getElementById('reviewer-model-catalog-status');
    if (catalogStatus) Object.assign(catalogStatus, { hidden: !state.modelCatalogNote, textContent: state.modelCatalogNote });
    const active = document.activeElement;
    const owner = active?.closest?.('.reviewer-slot-row');
    const marker = owner && active.getAttributeNames().find((name) => name.startsWith('data-'));
    const rowAttrs = owner ? [...owner.attributes].filter((attr) => attr.name.startsWith('data-')) : [];
    const selection = marker && typeof active.selectionStart === 'number'
        ? [active.selectionStart, active.selectionEnd] : null;
    const errorBox = document.getElementById('reviewer-slots-error');
    if (errorBox) {
        errorBox.hidden = !(state.configError || state.loadError);
        errorBox.textContent = state.configError
            ? `Saved reviewer-slot configuration is invalid and blocks reviews: ${state.configError}. `
              + 'To repair it, add at least one triad slot and one scope slot from the group headers below, then Save'
              + (state.triad.length && state.scope.length ? '.' : ' — Save will report the missing rows.')
            : (state.loadError
                ? `Could not reach the reviewer-slot settings — ${state.loadError}. Your saved configuration is unchanged; retry when the connection is back.`
                : '');
    }
    // Why the agent service could not be read is the TAB's banner now (one
    // place, not one per section). What stays here is the fact only this
    // section knows: a row pinned to an account that is really gone.
    const pinsBox = document.getElementById('reviewer-slots-pins');
    if (pinsBox) {
        const text = pinnedAccountWarning(state);
        pinsBox.hidden = !text;
        pinsBox.textContent = text;
    }
    const singles = Object.values(SINGLETONS).map((spec) => [spec, document.getElementById(spec.rowId)]);
    const boxes = Object.entries(CATEGORIES).map(([group, cat]) => [group, cat, document.getElementById(cat.rowsId)]);
    if (singles.some(([, box]) => !box) || boxes.some(([, , box]) => !box)) return;
    const updateDiscovery = (current, html) => {
        if (!current) return;
        const template = document.createElement('template');
        template.innerHTML = html;
        const desired = template.content.firstElementChild;
        routeEditor.updateRouteControlOptions(current, desired);
        for (const selector of ['.reviewer-slot-meta', '.reviewer-slot-route-identity']) {
            const before = current.querySelector(selector), after = desired.querySelector(selector);
            if (before && after) { before.innerHTML = after.innerHTML; before.title = after.title; }
        }
    };
    if (discoveryOnly) {
        for (const [group, , box] of boxes) for (const row of categoryRows(group)) {
            updateDiscovery([...box.querySelectorAll('[data-slot-id]')].find((node) => node.dataset.slotId === row.slot_id), rowHtml(row, group));
        }
        for (const [spec, box] of singles) updateDiscovery(box.firstElementChild, singletonHtml(spec));
        paintReviewerValidation();
        return;
    }
    state.disposeChoosers();
    for (const [group, cat, box] of boxes) {
        box.innerHTML = categoryRows(group).map((row) => rowHtml(row, group)).join('')
            || `<div class="muted">${cat.empty}</div>`;
        // The count stays in the heading; what the limit MEANS moved to the
        // span's title (owner feedback: headers carried parenthetical jargon).
        const limitEl = document.getElementById(cat.limitId);
        if (limitEl) limitEl.textContent = `${categoryRows(group).length}/${state.limits[cat.limitKey]}`;
        const addEl = document.getElementById(cat.addId);
        if (addEl) addEl.disabled = categoryRows(group).length >= state.limits[cat.limitKey];
    }
    for (const [spec, box] of singles) box.innerHTML = singletonHtml(spec);
    bindRowEvents();
    state.disposeChoosers = bindModelChoosers(document.getElementById('reviewer-slots-section'));
    paintReviewerValidation();
    if (marker) {
        const row = [...document.querySelectorAll('.reviewer-slot-row')]
            .find((element) => rowAttrs.every((attr) => element.getAttribute(attr.name) === attr.value));
        const field = row?.querySelector(`[${marker}]`);
        field?.focus({ preventScroll: true });
        if (selection && field?.setSelectionRange) field.setSelectionRange(...selection);
    }
}

function findRow(group, slotId) {
    return (categoryRows(group) || []).find((row) => row.slot_id === slotId) || null;
}

function bindRowEvents() {
    const section = document.getElementById('reviewer-slots-section');
    if (!section) return;
    section.querySelectorAll('.reviewer-slot-row[data-slot-id]').forEach((rowEl) => {
        const group = rowEl.dataset.slotGroup;
        const row = findRow(group, rowEl.dataset.slotId);
        if (!row) return;
        rowEl.querySelector('[data-slot-route]')?.addEventListener('change', (event) => {
            const value = String(event.target.value || '');
            if (value.startsWith(SUBAGENT_CHOICE_PREFIX)) {
                // A reference pick. The inline route stays stashed on the row
                // object — picking a channel again restores it.
                row.subagent_id = value.slice(SUBAGENT_CHOICE_PREFIX.length);
                renderRows();
                state.onChange();
                return;
            }
            row.subagent_id = '';
            if (!row.route?.kind) row.route = { kind: ROUTE_KIND_API, target_id: '' };
            const next = advisoryRouteTransition(row.route, decodeRouteChoice(value), row.routeMemory);
            row.route = next.route;
            row.routeMemory = next.memory;
            renderRows();
            state.onChange();
        });
        rowEl.querySelector('[data-slot-custom-api]')?.addEventListener('input', (event) => {
            const previous = encodeRouteChoice(row);
            row.route.target_id = routeEditor.routeTargetFromModel(row.route, event.target.value);
            if (previous !== encodeRouteChoice(row)) {
                delete row.route.profile_id;
                renderRows();
            } else refreshSavedTarget(rowEl, row);
            state.onChange();
        });
        rowEl.querySelector('[data-slot-model]')?.addEventListener('input', (event) => {
            const split = splitSessionTarget(row.route.target_id);
            row.route.target_id = composeSessionTarget(split.harness, event.target.value);
            state.onChange();
        });
        rowEl.querySelector('[data-slot-profile]')?.addEventListener('change', (event) => {
            row.route.profile_id = String(event.target.value || '');
            renderRows({ discoveryOnly: true });
            state.onChange();
        });
        rowEl.querySelector('[data-slot-effort]')?.addEventListener('change', (event) => {
            row.effort = String(event.target.value || '');
            renderRows({ discoveryOnly: true });
            state.onChange();
        });
        // Only an explicit switch writes delivery; a model or account change never does.
        rowEl.querySelector('[data-slot-delivery]')?.addEventListener('change', (event) => {
            row.delivery = event.target.value === DELIVERY_NATIVE ? DELIVERY_NATIVE : DELIVERY_PACKET;
            state.onChange();
        });
        rowEl.querySelector('[data-slot-processing]')?.addEventListener('change', (event) => {
            if (event.target.value) row.processing_preference = event.target.value;
            else delete row.processing_preference;
            state.onChange();
        });
        rowEl.querySelector('[data-slot-remove]')?.addEventListener('click', () => {
            const rows = categoryRows(group);
            const index = rows.indexOf(row);
            if (index >= 0) rows.splice(index, 1);
            renderRows();
            state.onChange();
        });
    });
    for (const spec of Object.values(SINGLETONS)) bindSingletonEvents(section, spec);
}

function bindSingletonEvents(section, spec) {
    // ONE binder for both single-row categories; the row object is the
    // category's state entry and the per-kind route memory is the spec's.
    const el = section.querySelector(`[data-${spec.attr}-row]`);
    if (!el) return;
    const a = spec.attr;
    const row = state[spec.stateKey];
    // An edit MATERIALIZES an optional singleton: from here on the save
    // payload carries it (an untouched placeholder is omitted, see
    // buildReviewerSlotsSetting).
    const edited = () => {
        if (spec.materializeOnEdit) row.materialized = true;
        state.onChange();
    };
    el.querySelector(`[data-${a}-enabled]`)?.addEventListener('change', (event) => {
        row.enabled = Boolean(event.target.checked);
        edited();
    });
    el.querySelector(`[data-${a}-route]`)?.addEventListener('change', (event) => {
        const value = String(event.target.value || '');
        if (value.startsWith(SUBAGENT_CHOICE_PREFIX)) {
            const next = advisoryReferenceTransition(row, value.slice(SUBAGENT_CHOICE_PREFIX.length));
            row.subagent_id = next.subagent_id;
            row.effort = next.effort;
            if (spec.materializeOnEdit) row.materialized = true;
            renderRows();
            state.onChange();
            return;
        }
        row.subagent_id = '';
        if (!row.route?.kind) row.route = { kind: ROUTE_KIND_API, target_id: '' };
        const result = advisoryRouteTransition(row.route, decodeRouteChoice(value), spec.memory);
        row.route = result.route;
        Object.assign(spec.memory, result.memory);
        if (row.route.kind !== ROUTE_KIND_SESSION && !row.effort && spec.apiEffortDefault) {
            row.effort = spec.apiEffortDefault;
        }
        if (spec.materializeOnEdit) row.materialized = true;
        renderRows();
        state.onChange();
    });
    // Mirrors of the triad-row model/api/profile handlers. These controls
    // exist only on their own kind's branch of singletonHtml, so each
    // querySelector binds at most one of them per render.
    el.querySelector(`[data-${a}-model]`)?.addEventListener('input', (event) => {
        const split = splitSessionTarget(row.route?.target_id);
        row.route.target_id = composeSessionTarget(split.harness, event.target.value);
        edited();
    });
    el.querySelector(`[data-${a}-api-model]`)?.addEventListener('input', (event) => {
        const previous = encodeRouteChoice(row);
        row.route.target_id = routeEditor.routeTargetFromModel(row.route, event.target.value);
        if (previous !== encodeRouteChoice(row)) {
            delete row.route.profile_id;
            renderRows();
        } else refreshSavedTarget(el, row);
        edited();
    });
    el.querySelector(`[data-${a}-profile]`)?.addEventListener('change', (event) => {
        row.route.profile_id = String(event.target.value || '');
        renderRows({ discoveryOnly: true });
        edited();
    });
    el.querySelector(`[data-${a}-effort]`)?.addEventListener('change', (event) => {
        const selected = String(event.target.value || '');
        // Empty means "the surface's default": the api default (low for the
        // advisory, the Behavior-tab deep effort for deep self-review), the
        // route/roster-row default on a session or subagent reference.
        row.effort = selected
            || (row.subagent_id || row.route?.kind === ROUTE_KIND_SESSION ? '' : spec.apiEffortDefault);
        renderRows({ discoveryOnly: true });
        edited();
    });
    el.querySelector(`[data-${a}-processing]`)?.addEventListener('change', (event) => {
        if (event.target.value) row.processing_preference = event.target.value;
        else delete row.processing_preference;
        edited();
    });
}

function addRow(group) {
    const cat = CATEGORIES[group];
    const rows = categoryRows(group);
    if (rows.length >= state.limits[cat.limitKey]) return;
    // Ids are unique across EVERY category: one identity space, one history per row.
    const taken = Object.keys(CATEGORIES).flatMap((g) => categoryRows(g)).map((row) => row.slot_id);
    rows.push({
        slot_id: mintSlotId(cat.idPrefix, taken),
        route: { kind: ROUTE_KIND_API, target_id: '' },
        subagent_id: '',
        effort: '',
        // #1334: a new API triad row reads the work itself; Packet is a choice.
        ...(group === 'triad' ? { delivery: DELIVERY_NATIVE } : {}),
    });
    renderRows();
    // The Add button sits in the group's header while the new row lands at
    // the group's end — reveal it there and hand the caret to its picker.
    const added = document.getElementById(group === 'scope' ? 'reviewer-scope-rows' : 'reviewer-triad-rows')
        ?.lastElementChild;
    revealNewRow(added, added?.querySelector?.('[data-slot-route]'));
    state.onChange();
}

// One projection for both persisted settings and the unsaved setup compiler.
export function applyReviewerSlotsDraft(data) {
    state.loadedDraft = structuredClone(data);
    state.saveAttempted = false;
    state.loadError = '';
    state.configError = String(data.config_error || '');
    state.source = String(data.source || '');
    state.limits = data.limits || state.limits;
    state.lastExecutions = data.last_executions || {};
    state.resolvedProcessingPreferences = data.resolved_processing_preferences || {};
    // Rows spread as-is so a subagent_id reference rides along; a direct
    // route gets its own object. The advisory's non-session kind is
    // normalized to the shared api_chat spelling here — the retired legacy
    // 'api' (Claude-SDK) kind is parse-only server-side and this UI never
    // writes it again.
    const rowIn = (row) => ({ ...row, _uiAttempted: false, route: { ...(row.route || {}) } });
    state.triad = Array.isArray(data.triad) ? data.triad.map(rowIn) : [];
    state.scope = Array.isArray(data.scope) ? data.scope.map(rowIn) : [];
    state.advisory = data.advisory ? rowIn(data.advisory) : state.advisory;
    if (state.advisory.route?.kind !== ROUTE_KIND_SESSION) {
        state.advisory.route = { ...(state.advisory.route || {}), kind: ROUTE_KIND_API };
    }
    // The deep self-review singleton: the server answers with the saved row
    // or, unsaved, the row synthesized from the legacy model key (labeled
    // so); beside a config_error that synthesized row is a legacy-derived
    // REPAIR PLACEHOLDER — no row is effective until the setting is repaired.
    // The label rides the state, never the saved bytes.
    const deep = data.deep_review ? rowIn(data.deep_review) : {};
    state.deepReview = {
        route: deep.route?.kind === ROUTE_KIND_SESSION ? deep.route : { ...(deep.route || {}), kind: ROUTE_KIND_API },
        effort: String(deep.effort || ''),
        processing_preference: String(deep.processing_preference || ''),
        subagent_id: String(deep.subagent_id || ''),
        synthesizedFrom: String(deep.synthesized_from || ''),
        // Only a SAVED row is materialized on load; a synthesized one (or
        // no row at all, e.g. beside a config_error on an older server)
        // stays synthesized until edited or its default panel is first saved.
        materialized: Boolean(data.deep_review) && !deep.synthesized_from,
    };
    // Reload is an explicit discard: source drafts belong to this loaded form,
    // never to a previous Settings document or a different account's choice.
    for (const spec of Object.values(SINGLETONS)) {
        for (const key of Object.keys(spec.memory)) delete spec.memory[key];
        const route = state[spec.stateKey].route;
        spec.memory[encodeRouteChoice({ route })] = { ...route };
    }
    // The VIEW loaded even when the saved value is invalid — that is exactly the
    // state the owner repairs from, and treating it as "not loaded" made the
    // save drop the repair (see collectReviewerSlots).
    state.loaded = true;
    // The panel exactly as loaded: an untouched shipped default is not written.
    state.loadedSetting = buildReviewerSlotsSetting(state);
    renderRows();
}

/** Explicit discard returns to the last loaded view, including unsaved defaults. */
export function discardReviewerSlotsDraft() {
    if (state.loadedDraft) applyReviewerSlotsDraft(state.loadedDraft);
}

export async function reloadReviewerSlots({ isCurrent = () => true } = {}) {
    try {
        const resp = await apiFetch('/api/reviewer-slots', { cache: 'no-store' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        if (!isCurrent()) return;
        applyReviewerSlotsDraft(data);
    } catch (error) {
        if (!isCurrent()) return;
        // A transport failure is NOT a verdict on the saved configuration: the
        // config-error banner accuses the owner's settings of blocking review, and
        // a network blip must never say that. Separate field, separate sentence.
        state.loaded = false;
        state.loadError = `could not load reviewer slots: ${error.message || error}`;
    }
    // ONE status read for the whole app (the accounts panel and the Subagents
    // section share this request; `includeModels` is sticky, so no later read
    // downgrades the snapshot these selects depend on). Awaited only for a
    // BOUNDED beat: this read can wake a cold Claudexor daemon and walk
    // per-harness model discovery, and awaiting it outright held the Save
    // button (loadSettings awaits this function) hostage for the whole probe.
    // A warm daemon settles inside the beat and keeps the exact old
    // semantics; a cold one keeps refreshing in the background and the status
    // surface binding repaints these rows when the snapshot lands.
    await boundedStatusRefresh(state.store);
    adoptStatusSnapshot();
    renderRows({ discoveryOnly: true });
}

function adoptStatusSnapshot() {
    // Each facet answers for itself. A never-read catalog and a never-read
    // account store are separate gaps, and neither is evidence that a saved
    // route or pin no longer exists. The tab's ONE service banner explains the
    // gap — every facet it lost, named — so this section adds no second
    // sentence about it; a facet that WAS read keeps its authoritative list.
    state.catalogKnown = state.store.catalogKnown;
    state.accountsKnown = state.store.accountsKnown;
    state.quotaKnown = state.store.quotaKnown;
    const snapshot = state.store.snapshot || {};
    state.snapshot = snapshot;
    state.harnesses = state.catalogKnown && Array.isArray(snapshot.harnesses)
        ? snapshot.harnesses.map((harness) => mergeHarnessModelCatalog(state.harnesses.find((old) => old.id === harness.id), harness)) : state.harnesses;
    state.profilesByHarness = state.accountsKnown ? indexProfilesByHarness(snapshot) : {};
}

// The roster the «Configured subagent» selects offer, adopted from the SAME
// settings document (and parser) the Available-subagents section loads —
// settings.js calls this from applySettings, right beside that section's own
// load. A roster that could not be parsed licenses no absence claim about a
// saved reference (rosterKnown=false), exactly like an unread status facet.
export function adoptSubagentRoster(settings) {
    const parsed = parseAvailableSubagentsSetting(availableSubagentsLoadValue(settings));
    state.roster = parsed.setting ? parsed.setting.items : [];
    state.rosterKnown = Boolean(parsed.setting);
    renderRows({ discoveryOnly: true });
}

/**
 * The API providers this install can send to, derived from the SAME settings
 * document the rows were loaded from (Settings) or the wizard's live draft.
 * A masked secret such as `***set***` is a stored credential; only an absent
 * or blank value withdraws a provider from the picker.
 */
export function setReviewerSourceContext({ settings = {}, providerProfiles = {} } = {}) {
    state.providerProfiles = providerProfiles || {};
    state.providers = routeEditor.configuredApiProviders(settings, state.providerProfiles);
    if (typeof document !== 'undefined') renderRows({ discoveryOnly: true });
}

export function setReviewerProcessingPreference(value, rolePreferences) {
    state.processingPreference = String(value || '');
    if (rolePreferences !== undefined) state.roleProcessingPreferences = { ...rolePreferences };
    if (typeof document !== 'undefined') renderRows({ discoveryOnly: true });
}

export function initReviewerSlots({ onChange, store = claudexorStatus } = {}) {
    destroyReviewerSlots();
    state.onChange = () => { paintReviewerValidation(); if (typeof onChange === 'function') onChange(); };
    state.store = store;
    // Seed from whatever the shared store already holds, so a render triggered
    // before the first notify (the model-catalog event) is not rendered from a
    // blank derived state.
    adoptStatusSnapshot();
    // Follow the shared read: when the daemon comes up while Settings is open
    // the rows stop claiming "(not in discovery)" without a page reload. That
    // promise needs the SHARED surface binding — a bare subscribe() carries no
    // visibility predicate, and the store never polls for a subscriber that
    // cannot say it is on screen, so nothing ever arrived to react to. Only a
    // change this section RENDERS repaints: a repaint on every poll tick would
    // drop the caret out of the API-model field mid-typing.
    let signature = '';
    state.disposers.push(bindStatusSurface(state.store, {
        elementId: 'reviewer-triad-rows',
        includeModels: true,
        listener: () => {
            adoptStatusSnapshot();
            const next = repaintSignature();
            if (next === signature) return;
            signature = next;
            renderRows({ discoveryOnly: true });
        },
    }));
    for (const [group, cat] of Object.entries(CATEGORIES)) {
        document.getElementById(cat.addId)?.addEventListener('click', () => addRow(group));
    }
    const onCatalog = (event) => {
        const catalog = mergeModelCatalog({ items: state.catalogModels, model_sources: state.modelSources }, event?.detail);
        state.modelSources = catalog.model_sources;
        state.catalogModels = catalog.items;
        state.modelCatalogNote = catalogReadNote(catalog);
        renderRows({ discoveryOnly: true });
    };
    document.addEventListener('settings-model-catalog:updated', onCatalog);
    state.disposers.push(() => document.removeEventListener('settings-model-catalog:updated', onCatalog));
    // Settings makes the loaded document usable immediately. Late reviewer
    // enrichment updates its baseline only while that draft remains clean.
}

export function destroyReviewerSlots() {
    state.disposeChoosers();
    for (const dispose of state.disposers.splice(0)) {
        try { dispose(); } catch (err) { /* a broken disposer must not block the rest */ }
    }
}

// #126, pure and node-tested: what the settings save sends for reviewer slots.
// Never author the setting from an UNLOADED view (an unrelated save must not
// overwrite the owner's configuration with an empty page), and a transport
// failure is not a verdict on the saved value either. But a LOADED view always
// serializes what it shows, including empty groups. Settings validates the
// current draft before submitting; this pure serializer never hides invalid
// rows. Backend validation remains authoritative for every other caller.
// An UNTOUCHED shipped default panel (`source: 'default'`, unchanged since load) is omitted
// like the deep-review placeholder: an unrelated save never materializes it (#1334).
export function reviewerSlotsSavePayload({ loaded = false, loadError = '', source = '', loadedSetting = '',
    triad = [], scope = [], advisory, deepReview } = {}) {
    if (loadError || !loaded) return {};
    const setting = buildReviewerSlotsSetting({ source, triad, scope, advisory, deepReview });
    if (source === 'default' && loadedSetting && setting === loadedSetting) return {};
    return { OUROBOROS_REVIEWER_SLOTS: setting };
}

export function collectReviewerSlots() {
    // A config_error is NOT the unloaded case — the stored value is already
    // invalid and blocking review, the endpoint returns no rows with it, and
    // refusing here made the documented repair path swallow the owner's
    // replacement rows and still report success.
    return reviewerSlotsSavePayload(state);
}
