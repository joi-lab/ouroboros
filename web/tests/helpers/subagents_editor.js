// Shared fixtures of the Available-subagents editor tests (subagents_settings, review_pool_empty,
// review_pool_copies): catalog rows and the fake DOM the real binder and painter run against.
import { ROUTE_KIND_AGENT_SESSION, ROUTE_KIND_API_MODEL } from '../../modules/route_editor_primitives.js';

export function apiRow(overrides = {}) {
    return {
        subagent_id: 'api_scout',
        recommended_use: 'Fast independent research and verification.',
        route: { kind: ROUTE_KIND_API_MODEL, target_id: 'openai/gpt-5.6-luna' },
        effort: 'high',
        ...overrides,
    };
}

export function sessionRow(overrides = {}) {
    return {
        subagent_id: 'codex_builder',
        access: 'full',
        recommended_use: 'Implementation in a real workspace.',
        route: {
            kind: ROUTE_KIND_AGENT_SESSION,
            target_id: 'codex=gpt-5.6-sol-high',
            credential_profile_id: 'koshak',
        },
        ...overrides,
    };
}

export function setting(items = [apiRow(), sessionRow()]) {
    return { enabled: true, items };
}

// A small event surface for the real editor binder. Only the controls these
// tests operate are parsed; rendered geometry/chooser behavior is browser QA.
export function accessEditorDom() {
    const field = (name) => ({
        dataset: { subagentField: name }, attributes: {}, listeners: {},
        addEventListener(type, handler) { this.listeners[type] = handler; },
        setAttribute(name, value) { this.attributes[name] = value; },
        emit(type, value) { this.listeners[type]({ target: { value } }); },
        toggle(checked) { this.listeners.change({ target: { checked } }); },
    });
    let rows = [];
    let markup = '';
    // The toolbar controls the real binder wires beside the rows, and the
    // review-pool lines the painter fills in place.
    const toolbar = {
        add: field('add'), listEnabled: field('listEnabled'), allowEmpty: field('allowEmpty'), keepOne: field('keepOne'),
    };
    const pool = Object.fromEntries(['count', 'stays', 'empty', 'empty-text', 'confirm', 'note', 'history', 'history-text',
        'copies', 'copies-text']
        .map((name) => [name, { textContent: '', hidden: false }]));
    const container = {
        scrollTop: 0,
        toolbar,
        set innerHTML(html) {
            markup = html;
            rows = [...html.matchAll(/<article[^>]*data-subagent-row="([^"]+)"[^>]*>([\s\S]*?)<\/article>/g)].map((match) => {
                const fields = new Map([...match[2].matchAll(/data-subagent-field="([^"]+)"/g)]
                    .map((entry) => [entry[1], field(entry[1])]));
                const duplicate = field('duplicate');
                const meta = { dataset: {}, toggleAttribute() {}, textContent: '' };
                const facts = { textContent: '' };
                const notes = { textContent: '', hidden: true };
                const status = { dataset: {}, textContent: '', title: '' };
                const reason = { textContent: '', hidden: true };
                const lastReview = { hidden: true, dd: { textContent: '' }, querySelector() { return this.dd; } };
                return {
                    dataset: { subagentRow: match[1] }, toggleAttribute() {}, meta, facts, notes, status, reason, lastReview, html: match[2],
                    querySelector(selector) {
                        if (selector === '[data-subagent-duplicate]') return duplicate;
                        if (selector === '[data-subagent-meta]') return meta;
                        if (selector === '[data-subagent-review-facts]') return facts;
                        if (selector === '[data-subagent-review-notes]') return notes;
                        if (selector === '[data-subagent-status]') return status;
                        if (selector === '[data-subagent-status-reason]') return reason;
                        if (selector === '[data-subagent-last-review]') return lastReview;
                        return fields.get(selector.match(/data-subagent-field="([^"]+)"/)?.[1]) || null;
                    },
                    querySelectorAll: (selector) => selector === '[data-subagent-field]' ? [...fields.values()] : [],
                };
            });
        },
        querySelector(selector) {
            if (selector === '[data-subagent-add]') return toolbar.add;
            if (selector === '[data-subagents-enabled]') return toolbar.listEnabled;
            if (selector === '[data-review-pool-allow-empty]') return toolbar.allowEmpty;
            if (selector === '[data-review-pool-keep-one]') return toolbar.keepOne;
            const line = selector.match(/^\[data-review-pool-([a-z-]+)\]$/)?.[1];
            if (line) return pool[line] || null;
            const key = selector.match(/data-subagent-row="([^"]+)"/)?.[1];
            return rows.find((row) => row.dataset.subagentRow === key) || null;
        },
        querySelectorAll: (selector) => selector === '[data-subagent-row]' ? rows : [],
    };
    return {
        doc: { getElementById: () => container }, toolbar, pool,
        row: (index = 0) => rows[index], html: () => markup,
    };
}
