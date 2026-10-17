// The owner message's routing receipt (routing_receipt.js) and its portal reconcile (work_portal.js): one line under
// his words — what happened to them, then the Project reference when they went to another room (a relay names the
// target works by title, CONTRACTS K4) — and the line steps aside only for the act its portal already shows.
import assert from 'node:assert/strict';
import test from 'node:test';
import { renderRoutingAnnotation } from '../modules/routing_receipt.js';
import { reconcilePortalNotes } from '../modules/work_portal.js';
import { installDomDouble } from './dom_double.js';

function owner(env) {
    const bubble = env.el('div', { className: 'chat-bubble user' }, env.el('div', { className: 'message', text: 'tell the audit' }),
        env.el('div', { className: 'msg-time', text: '09:37' }));
    env.doc.body.append(bubble);
    return bubble;
}
const note = (bubble) => bubble.querySelector('.msg-routing-annotation');

test('a relay receipt names the work by its title and the room by its reference, on one line', () => {
    const env = installDomDouble();
    try {
        const bubble = owner(env);
        renderRoutingAnnotation(bubble, { action: 'steer_task', status: 'delivered', project_id: 'p-1',
            project_name: 'Audit room', target_task_ids: ['t-1'], target_titles: ['Audit the projects'] }, 1);
        const line = note(bubble);
        assert.ok(line.classList.contains('has-reference'));
        assert.equal(line.children[0].textContent, 'Passed on to «Audit the projects» in');
        assert.equal(line.children[1].getAttribute('aria-label'), 'Open project Audit room');
        assert.equal(line.dataset.destinationKey, 'p-1||t-1', 'the target comes from the typed target list');
        assert.equal(bubble.children.indexOf(line), bubble.children.length - 2, 'right above the time, never a second row');
        renderRoutingAnnotation(bubble, { action: 'tell_task', status: 'delivered', project_id: 'p-1', project_chat_id: 7 }, 1);
        assert.equal(note(bubble).children[0].textContent, 'Passed on to the work in', 'without titles: the work');
        renderRoutingAnnotation(bubble, { action: 'promote_chat_to_task', status: 'scheduled', project_id: 'p-1', project_chat_id: 7,
            target: 't-2', target_label: 'Audit' }, 1);
        assert.equal(note(bubble).children[0].textContent, 'Started as a task in');
    } finally { env.restore(); }
});

test('the work reading the relayed words keeps the receipt\'s work title and room reference', () => {
    const env = installDomDouble();
    try {
        const bubble = owner(env);
        const relay = { action: 'steer_task', project_id: 'p-1', project_name: 'Audit room',
            target_task_ids: ['t-1'], target_titles: ['Audit the projects'] };
        renderRoutingAnnotation(bubble, { ...relay, status: 'delivered' }, 1);
        renderRoutingAnnotation(bubble, { ...relay, status: 'read' }, 1);
        const line = note(bubble);
        assert.ok(line.classList.contains('has-reference'), 'read is a stronger receipt, never a weaker line');
        assert.equal(line.children[0].textContent, 'Passed on to «Audit the projects» in');
        assert.equal(line.children[1].getAttribute('aria-label'), 'Open project Audit room');
        assert.equal(line.dataset.annotationStatus, 'read');
        assert.equal(line.dataset.destinationKey, 'p-1||t-1', 'the same work and room as the delivered receipt');
    } finally { env.restore(); }
});

test('in its own room, refused or pending, the receipt is its words alone', () => {
    const env = installDomDouble();
    try {
        const bubble = owner(env);
        renderRoutingAnnotation(bubble, { action: 'steer_task', status: 'delivered', project_id: 'p-1', project_chat_id: 7,
            target: 't-1', target_label: 'Audit' }, 7);
        assert.equal(note(bubble).classList.contains('has-reference'), false);
        assert.equal(note(bubble).textContent, 'Steered task · Audit');
        renderRoutingAnnotation(bubble, { action: 'steer_task', status: 'refused', project_id: 'p-1', project_chat_id: 7,
            cause: 'Not delivered: the work has ended' }, 1);
        assert.equal(note(bubble).textContent, 'Not delivered: the work has ended');
        assert.equal(note(bubble).classList.contains('has-reference'), false, 'a refused act names no destination');
        renderRoutingAnnotation(bubble, { status: 'pending' }, 1);
        assert.equal(note(bubble).textContent, 'Choosing the right destination…');
        assert.equal(renderRoutingAnnotation(bubble, null, 1), true);
        assert.equal(note(bubble), null, 'no annotation, no line');
    } finally { env.restore(); }
});

test('the portal stands for the act that moved its work; words passed on later keep their receipt', () => {
    const env = installDomDouble();
    try {
        const { el, doc } = env;
        const feed = el('div');
        doc.body.append(feed);
        const portal = el('div', { className: 'chat-live-card', dataset: { taskId: 't-1', projectId: 'p-1', portal: '1' } });
        const receipt = (action) => {
            const bubble = owner(env);
            feed.append(bubble);
            renderRoutingAnnotation(bubble, { action, status: 'delivered', project_id: 'p-1', project_chat_id: 7, target: 't-1' }, 1);
            return note(bubble);
        };
        const moved = receipt('promote_chat_to_task');
        const passed = receipt('steer_task');
        const delivered = receipt('mailbox_delivery');
        reconcilePortalNotes(feed);
        assert.deepEqual([moved.hidden, passed.hidden, delivered.hidden], [false, false, false], 'no portal yet: every receipt shows');
        feed.append(portal);
        assert.equal(reconcilePortalNotes(feed), true);
        assert.equal(moved.hidden, true, 'the portal is that receipt\'s whole content');
        assert.equal(passed.hidden, false, 'a message passed on later is its own fact');
        assert.equal(delivered.hidden, false);
        portal.remove();
        reconcilePortalNotes(feed);
        assert.equal(moved.hidden, false, 'the receipt returns when its portal leaves the feed');
    } finally { env.restore(); }
});
