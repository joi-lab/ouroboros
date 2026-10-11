import test from 'node:test';
import assert from 'node:assert/strict';
import {
    POINTER_NAME_CHARS, bindProjectWorkPointer, projectWorkLabel, projectWorkTarget,
} from '../modules/project_work_pointer.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

test('Project pointer prefers represented unfinished roots without duplicating child cards',()=>{
    const done={root:{isConnected:true},finished:true};
    const active={root:{isConnected:true},finished:false};
    const child={root:{isConnected:true},isSubagent:true};
    const removed={root:{isConnected:false}};
    assert.equal(projectWorkTarget([done,active,child,removed]),active);
    active.finished=true;
    assert.equal(projectWorkTarget([done,active,child]),active);
    assert.equal(projectWorkTarget([child,removed]),null);
});

test('an older prepended card at the same timestamp does not become the latest task', () => {
    const card = offset => ({ root: { isConnected: true, dataset: { ts: '1000' } },
        historyPosition: { source: 'progress', offset }, finished: true });
    const newer = card(500), older = card(100);
    assert.equal(projectWorkTarget([newer, older]), newer);
});

test('the pointer label names the card and caps a status-line title to one line of text', () => {
    const card = (title, extra = {}) => ({ root: { isConnected: true }, finished: true,
        titleEl: { textContent: title }, ...extra });
    assert.equal(projectWorkLabel(null), '');
    assert.equal(projectWorkLabel(card('Task activity', { finished: false })), 'Working · Task activity');
    assert.equal(projectWorkLabel(card('Plan review: none of the 3 reviewers answered.',
        { suggestedName: 'Swarm dynamics survey' })), 'Latest task · Swarm dynamics survey');
    assert.equal(projectWorkLabel(card('  multi\n line\t title  ')), 'Latest task · multi line title');
    assert.equal(projectWorkLabel(card('')), 'Latest task · Task');
    const verdict = 'Plan review: 1 of 3 reviewers answered — 4 notes, nothing blocking. '
        + 'Plan reviewer codex=gpt-6-astra didn\'t answer — "Selected model is at capacity. Please try a different model." '.repeat(4);
    assert.ok(verdict.length > 300);
    const label = projectWorkLabel(card(verdict));
    assert.ok(label.startsWith('Latest task · Plan review: 1 of 3 reviewers answered'));
    assert.ok(label.endsWith('…'));
    assert.ok(label.length <= 'Latest task · '.length + POINTER_NAME_CHARS);
});

test('the bound pointer hides without a represented card and owns no competing history notice', () => {
    const { prior, mount } = installDom();
    try {
        const records = new Map();
        let navigated = null;
        const host = mount.ownerDocument.createElement('div');
        mount.appendChild(host);
        const pointer = bindProjectWorkPointer(host, {
            records, onNavigate: (root) => { navigated = root; },
        });
        const button = host.querySelector('.project-work-pointer');
        assert.equal(host.querySelector('.project-work-coverage'), null);
        const label = button.querySelector('.project-work-pointer-label');
        assert.equal(button.hidden, true);
        assert.equal(button.disabled, true);

        const root = mount.ownerDocument.createElement('div');
        mount.appendChild(root);
        root.dataset.ts = '1000';
        records.set('t1', { root, finished: true, titleEl: { textContent: 'x'.repeat(400) } });
        pointer.update();
        assert.equal(button.hidden, false);
        assert.equal(button.disabled, false);
        assert.ok(label.textContent.startsWith('Latest task · xxx'));
        assert.ok(label.textContent.length <= 'Latest task · '.length + POINTER_NAME_CHARS);

        pointer.update();
        for (const fn of button.listeners.get('click') || []) fn();
        assert.equal(navigated, root);

        records.clear();
        pointer.update();
        assert.equal(button.hidden, true);
        pointer.update();

        pointer.destroy();
        assert.equal(host.children.length, 0);
    } finally {
        restoreDom(prior);
    }
});
