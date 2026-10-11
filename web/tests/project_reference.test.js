import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

import { PAGE_ICONS } from '../modules/page_icons.js';
import { nameProjectReference, projectReference } from '../modules/project_reference.js';

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// Minimal element stub: the reference is built with createElement/append/textContent,
// so a flat stub proves the DOM contract without a browser.
class NodeStub {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.attributes = {};
        this.dataset = {};
        this.listeners = new Map();
        this.className = '';
        this.innerHTML = '';
        this.title = '';
        this._text = '';
    }
    set textContent(value) { this._text = String(value ?? ''); }
    get textContent() { return this._text; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    append(...nodes) { this.children.push(...nodes); }
    addEventListener(type, fn) { this.listeners.set(type, fn); }
    querySelector(selector) { return this.children.find((node) => `.${node.className}` === selector) || null; }
}

function withDom(fn) {
    const prior = { document: globalThis.document, window: globalThis.window, CustomEvent: globalThis.CustomEvent };
    const sent = [];
    globalThis.document = { createElement: (tag) => new NodeStub(tag) };
    globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
    globalThis.window = { dispatchEvent: (event) => { sent.push({ type: event.type, detail: event.detail }); } };
    try { return fn(sent); } finally { Object.assign(globalThis, prior); }
}

const shape = (node) => ({
    tag: node.tagName, type: node.type, intent: node.dataset.intent,
    parts: node.children.map((child) => child.className),
    icon: node.children[0].innerHTML, iconHidden: node.children[0].attributes['aria-hidden'],
    arrowHidden: node.children[2].attributes['aria-hidden'],
});

test('one intent, one control: every layout is the same node with the same name and the same press', () => {
    withDom((sent) => {
        const project = { id: 'launch', name: 'OpenClaw 2.0 <b>x</b>', chat_id: 42 };
        const inline = projectReference(project);
        const bar = projectReference(project, { layout: 'bar', state: 'background' });
        const footer = projectReference(project, { layout: 'footer' });
        // The callers pick a layout; the classes are the door's own business.
        assert.equal(inline.className, 'chat-live-project-card-btn chat-quiz-project');
        assert.equal(bar.className, 'chat-live-project-card-btn');
        assert.equal(footer.className, 'chat-live-project-card-btn chat-live-bound-pointer');
        assert.deepEqual(shape(inline), shape(bar));
        assert.deepEqual(shape(inline), shape(footer));
        assert.deepEqual(shape(inline), {
            tag: 'BUTTON', type: 'button', intent: 'open-project',
            parts: ['chat-live-project-icon', 'chat-live-project-name', 'chat-live-project-status'],
            icon: PAGE_ICONS.projects, iconHidden: 'true', arrowHidden: 'true',
        });
        // The name is text: a Project name can never inject markup.
        for (const node of [inline, bar, footer]) assert.equal(node.children[1].textContent, 'OpenClaw 2.0 <b>x</b>');
        // The arrow alone says "opens"; words appear only as a closed state of the same control.
        assert.equal(inline.children[2].textContent, '↗');
        assert.equal(footer.children[2].textContent, '↗');
        assert.equal(bar.children[2].textContent, 'running in background ↗');
        // Phones and the Telegram shell have no hover: the accessible name says everything the pixels do.
        assert.equal(inline.attributes['aria-label'], 'Open project OpenClaw 2.0 <b>x</b>');
        assert.equal(inline.title, inline.attributes['aria-label']);
        assert.equal(bar.attributes['aria-label'], 'Open project OpenClaw 2.0 <b>x</b>, running in background');
        for (const node of [inline, bar, footer]) node.listeners.get('click')();
        assert.deepEqual(sent, Array(3).fill({
            type: 'ouro:open-project', detail: { project, task_id: '', quiz_id: '' },
        }));
    });
});

test('a question reference opens that exact question, and an unknown state or layout adds nothing', () => {
    withDom((sent) => {
        const node = projectReference({ id: 'launch', name: 'Launch', chat_id: 7 }, { taskId: 't1', quizId: 'q1' });
        assert.equal(node.attributes['aria-label'], 'Open this question in Launch');
        node.listeners.get('click')();
        assert.deepEqual(sent[0].detail, { project: { id: 'launch', name: 'Launch', chat_id: 7 }, task_id: 't1', quiz_id: 'q1' });
        const odd = projectReference({ id: 'launch', name: 'Launch' }, { layout: 'sidebar', state: 'paused' });
        assert.equal(odd.className, 'chat-live-project-card-btn');
        assert.equal(odd.children[2].textContent, '↗');
        assert.equal(odd.dataset.state, undefined);
    });
});

test('a Project that was never named reads Project, and a name that arrives later reaches text, speech and the press', () => {
    withDom((sent) => {
        // The registry names an unnamed row by its minted id; that shape alone is a non-name.
        const unnamed = projectReference({ id: 'proj_5e8bbf266c89', name: 'proj_5e8bbf266c89' });
        assert.equal(unnamed.children[1].textContent, 'Project');
        assert.equal(unnamed.attributes['aria-label'], 'Open project Project');
        // An owner's own lowercase name derives the same id, and it IS the name.
        for (const own of ['blog', 'my-app', 'proj_notes']) {
            assert.equal(projectReference({ id: own, name: own }).children[1].textContent, own);
        }
        const late = projectReference({ id: 'launch', chat_id: 3 }, { quizId: 'q', taskId: 't' });
        assert.equal(late.children[1].textContent, 'Project');
        nameProjectReference(late, { id: 'launch', name: 'Launch' });
        assert.equal(late.children[1].textContent, 'Launch');
        assert.equal(late.title, 'Open this question in Launch');
        assert.equal(late.attributes['aria-label'], 'Open this question in Launch');
        late.listeners.get('click')();
        assert.deepEqual(sent[0].detail.project, { id: 'launch', chat_id: 3, name: 'Launch' });
    });
});

test('the door is the only place that raises the event or builds the control', () => {
    const sources = [path.join(WEB, 'app.js'), ...fs.readdirSync(path.join(WEB, 'modules'))
        .filter((name) => name.endsWith('.js')).map((name) => path.join(WEB, 'modules', name))];
    const raised = /CustomEvent\(\s*['"`]ouro:open-project['"`]/;
    // Code only: a comment may name the event. An event name held in a constant is out of this scan's reach.
    const code = (file) => fs.readFileSync(file, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    const raisers = sources.filter((file) => raised.test(code(file)));
    assert.deepEqual(raisers.map((file) => path.basename(file)), ['project_reference.js']);
    // The other half of the wire stays where it was: one listener, in the shell.
    const app = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');
    assert.equal((app.match(/addEventListener\('ouro:open-project'/g) || []).length, 1);
    // The superseded factories are gone, so no caller can pass a label or a class name again.
    const helpers = fs.readFileSync(path.join(WEB, 'modules', 'ui_helpers.js'), 'utf8');
    assert.doesNotMatch(helpers, /export function (renderProjectChip|createSystemMessageAction)\b/);
    assert.match(helpers, /export function createSystemMessageActions\(/);
});
