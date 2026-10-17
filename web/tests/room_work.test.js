import assert from 'node:assert/strict';
import test from 'node:test';
import { createRoomWork, latestRepresentedRoot, roomWorkTone, rowControls, waitedQuestion } from '../modules/room_work.js';
import { censusTaskPhase, desiredLiveCardPhase } from '../modules/task_phase_chip.js';
import { installDom, restoreDom } from './chat_dom_fixture.js';

test('a Project keeps its latest represented root: an unfinished one first, never a child or a removed card', () => {
    const done = { root: { isConnected: true }, finished: true };
    const active = { root: { isConnected: true }, finished: false };
    const child = { root: { isConnected: true }, isSubagent: true };
    const removed = { root: { isConnected: false } };
    assert.equal(latestRepresentedRoot([done, active, child, removed]), active);
    active.finished = true;
    assert.equal(latestRepresentedRoot([done, active, child]), active);
    assert.equal(latestRepresentedRoot([child, removed]), null);
});

test('an older prepended card at the same timestamp does not become the latest root', () => {
    const card = (offset) => ({ root: { isConnected: true, dataset: { ts: '1000' } },
        historyPosition: { source: 'progress', offset }, finished: true });
    const newer = card(500), older = card(100);
    assert.equal(latestRepresentedRoot([newer, older]), newer);
});

test('a row takes its colour from the shared presenter, never from its words', () => {
    const tone = (activity, { connected = true, access = false } = {}) => roomWorkTone(
        censusTaskPhase(activity, null, connected, access), { confirmed: connected && !activity._activityUnconfirmed });
    assert.equal(tone({ phase: 'working' }), 'live');
    assert.equal(tone({ phase: 'queued' }), 'queued');
    assert.equal(tone({ phase: 'working', owner_wait: { quiz_id: 'q1', owner_wait_state: 'waiting', quiz_state: 'open' } }), 'wait');
    assert.equal(tone({ phase: 'working' }, { access: true }), 'wait');
    assert.equal(tone({ phase: 'budget_paused', pause_cause: 'owner' }), 'wait');
    assert.equal(tone({ phase: 'budget_pausing' }), 'wait');
    assert.equal(tone({ phase: 'working', project_admission_hold: { label: 'Waiting for Project access' } }), 'wait');
    assert.equal(tone({ phase: 'working' }, { connected: false }), 'unknown');
    assert.equal(tone({ phase: 'working', _activityUnconfirmed: true }), 'unknown');
    // A known outcome owns the colour; Finalizing… is a second, quieter fact beside it.
    const failed = censusTaskPhase({ phase: 'finalizing', status: 'failed',
        root_phase_checkpoint: { post_task_synthesis: 'running' },
        outcome_axes: { lifecycle: { status: 'failed' }, execution: { status: 'infra_failed' } } });
    assert.deepEqual([failed.text, failed.secondary], ['Failed', 'Finalizing…']);
    assert.equal(roomWorkTone(failed), 'failed');
    assert.equal(roomWorkTone(desiredLiveCardPhase({ finished: true }, 'error'), { settled: true }), 'failed');
    assert.equal(roomWorkTone({ phase: 'ended', text: 'No longer listed as active' }, { settled: true }), 'ended');
});

test('View question names the exact waited quiz and nothing without positive, confirmed evidence', () => {
    const waiting = { phase: 'working', owner_wait: { quiz_id: 'q-main', owner_wait_state: 'waiting', quiz_state: 'open' } };
    assert.deepEqual(waitedQuestion(waiting), { quizId: 'q-main' });
    assert.deepEqual(waitedQuestion({ phase: 'working', owner_wait: { owner_wait_state: 'waiting', quiz_state: 'open' },
        required_question: { quiz_id: 'q-project' } }), { quizId: 'q-project' });
    assert.equal(waitedQuestion({ ...waiting, _activityUnconfirmed: true }), null);
    assert.equal(waitedQuestion({ phase: 'working', owner_wait: { quiz_id: 'q-main', owner_wait_state: 'resumed', quiz_state: 'open' } }), null);
    assert.equal(waitedQuestion({ phase: 'working', owner_wait: { quiz_id: 'q-main', owner_wait_state: 'waiting', quiz_state: 'answered' } }), null);
    // A wait without its quiz id (an older server) still reads as waiting, but leads nowhere.
    assert.equal(waitedQuestion({ phase: 'working', owner_wait: { owner_wait_state: 'waiting', quiz_state: 'open' } }), null);
    assert.equal(waitedQuestion(null), null);
});

test('controls follow the current census for the exact id; stale, offline or reusable rows offer none', () => {
    const working = { phase: 'working', kind: 'managed_task' };
    const paused = { phase: 'budget_paused', kind: 'managed_task' };
    assert.deepEqual(rowControls({ id: 't1', activity: working, confirmed: true }), { menu: true, resume: false });
    assert.deepEqual(rowControls({ id: 't1', activity: paused, confirmed: true }), { menu: true, resume: true });
    assert.deepEqual(rowControls({ id: 't1', activity: working, confirmed: false }), { menu: false, resume: false });
    assert.deepEqual(rowControls({ id: 't1', activity: null, confirmed: true }), { menu: false, resume: false });
    assert.deepEqual(rowControls({ id: 'active', activity: working, confirmed: true }), { menu: false, resume: false });
    // A loaded card answers with its own Stop predicate, never the row's.
    const card = { groupId: 't1', root: { dataset: {} } };
    assert.deepEqual(rowControls({ id: 't1', record: card, activity: working, confirmed: true }), { menu: false, resume: false });
    assert.deepEqual(rowControls({ id: 't1', record: card, activity: working, confirmed: true,
        stopEligible: (record) => record === card }), { menu: true, resume: false });
    assert.deepEqual(rowControls({ id: 't1', record: { ...card, cancelPendingPolicy: 'finalize' }, activity: paused,
        confirmed: true }), { menu: false, resume: false });
});

test('the mounted list holds its order while open, settles ended work and only navigates to a question', async () => {
    const calls = [];
    const { prior, mount } = installDom(async (url, init) => {
        calls.push([String(url), init?.method || 'GET']);
        return { ok: true, json: async () => ({}) };
    });
    let work = null;
    try {
        const doc = globalThis.document;
        const node = (className, parent) => { const el = doc.createElement('div'); el.className = className; parent.appendChild(el); return el; };
        const page = node('page', mount);
        const input = doc.createElement('textarea');
        input.id = 'chat-input'; // Main has the id; only namespaced Project inputs gain its mirror class.
        page.appendChild(input);
        const header = node('chat-page-header', page);
        // The product's bar: the title row (where the status goes) and the trailing commands (where it was).
        const titleRow = node('app-page-title-row', node('app-page-title-block', header));
        const group = node('app-page-actions ui-chrome-group', header);
        const badge = doc.createElement('span');
        badge.className = 'status-badge thinking';
        badge.textContent = 'Working';
        Object.assign(badge.dataset, { tone: 'live', dots: 'moving' });  // what setStatus writes from the producer
        group.appendChild(badge);
        const messages = node('chat-messages', page);
        const activities = new Map([['t-run', { activity_id: 't-run', kind: 'managed_task', phase: 'working', chat_id: 1 }]]);
        const reveals = [];
        work = createRoomWork({
            page, statusBadge: badge, messagesDiv: messages, ws: { isConnected: () => true },
            reading: { stick: true, cancel() {}, scroll() {} },
            activeDirectActivities: activities, liveCardRecords: new Map(), pendingSubmissions: new Map(),
            revealQuestion: async (...args) => { reveals.push(args); return true; },
        });
        // The status follows the room's title (1A) and its Shade hangs from the bar's bottom edge (one Shade).
        const trigger = titleRow.children.find((child) => child.classList.contains('rw-trigger'));
        assert.equal(group.children.some((child) => child.classList.contains('rw-trigger')), false);
        const anchor = header.children.find((child) => child.classList.contains('rw-shade-anchor'));
        const surface = anchor.children.find((child) => child.classList.contains('rw-surface'));
        assert.ok(surface.classList.contains('rw-shade'));
        const list = surface.children[0];
        const ids = () => list.children.map((row) => row.dataset.id);
        assert.ok(trigger.contains(badge), 'the real status badge is the control’s word');
        assert.equal(trigger.dataset.tone, 'live');
        assert.equal(trigger.disabled, false);

        trigger.click();
        assert.equal(work.isOpen(), true);
        assert.deepEqual(ids(), ['t-run']);
        // A question arriving while open joins at the end: order holds under the reader.
        activities.set('t-ask', { activity_id: 't-ask', kind: 'managed_task', phase: 'working', chat_id: 1,
            owner_wait: { quiz_id: 'q-ask', owner_wait_state: 'waiting', quiz_state: 'open' } });
        work.update();
        assert.deepEqual(ids(), ['t-run', 't-ask']);
        // Work that ends while open keeps its row, says so, and offers nothing.
        activities.delete('t-run');
        work.update();
        const ended = list.children[0];
        assert.equal(ended.dataset.settled, '1');
        assert.equal(ended.dataset.tone, 'ended');
        assert.equal(ended.children.find((child) => child.classList.contains('rw-more-slot')).children.length, 0);
        // View question goes to the exact waited question, closes the list and posts nothing.
        const verb = list.children[1].children.find((child) => child.classList.contains('rw-verb'));
        verb.children[0].click();
        await Promise.resolve();
        assert.deepEqual(reveals, [['t-ask', 'q-ask', { focus: 'question' }]]);
        assert.equal(work.isOpen(), false);
        assert.ok(calls.every(([url, method]) => method === 'GET' && !url.includes('/api/decisions')), JSON.stringify(calls));
        // Closed, the list sorts again: the owner's turn first.
        activities.set('t-run', { activity_id: 't-run', kind: 'managed_task', phase: 'working', chat_id: 1 });
        trigger.click();
        assert.deepEqual(ids(), ['t-ask', 't-run']);
        // Reconcile ended-only Main membership before returning focus: a now-disabled
        // status trigger cannot be the keyboard owner's next place.
        activities.clear();
        work.update();
        assert.equal(work.isOpen(), true);
        trigger.click();
        assert.equal(work.isOpen(), false);
        assert.equal(trigger.disabled, true);
        assert.ok(doc.activeElement === input, 'closing an ended-only list returns focus to the composer');
    } finally {
        work?.destroy();
        restoreDom(prior);
    }
});

// A progress card can be painted before the slow census identifies the same send.
test('a pending send never replaces an already represented turn or leaves a ghost node', () => {
    const { prior, mount } = installDom(async () => ({ ok: true, json: async () => ({}) }));
    let work;
    try {
        const node = (tag, cls, parent) => {
            const el = document.createElement(tag); el.className = cls; parent.appendChild(el); return el;
        };
        const page = node('div', 'page', mount);
        const header = node('div', 'chat-page-header', page);
        const titleRow = node('div', 'app-page-title-row', node('div', 'app-page-title-block', header));
        const group = node('div', 'app-page-actions ui-chrome-group', header);
        const badge = node('span', 'status-badge thinking', group);
        const messages = node('div', 'chat-messages', page);
        const activities = new Map(), records = new Map();
        const pending = new Map([['send-1', { timestamp: 1000 }]]);
        work = createRoomWork({ page, statusBadge: badge, messagesDiv: messages,
            ws: { isConnected: () => true }, reading: { cancel() {}, scroll() {} },
            activeDirectActivities: activities, liveCardRecords: records, pendingSubmissions: pending,
            stopEligible: () => true });
        const trigger = titleRow.children.find(el => el.classList.contains('rw-trigger'));
        const list = header.children.find(el => el.classList.contains('rw-shade-anchor')).children[0].children[0];
        const turns = () => list.children.filter(el => el.dataset.id === 'turn-1');
        trigger.click();
        const card = node('div', 'chat-live-block', messages);
        records.set('turn-1', { root: card, groupId: 'turn-1', direct: true, phase: 'working' });
        work.update();
        assert.equal(turns().length, 1);
        const existing = turns()[0];
        existing.querySelector('.rw-main').focus();
        activities.set('turn-1', { activity_id: 'turn-1', kind: 'direct_chat', phase: 'working',
            chat_id: 1, client_message_id: 'send-1', task_attempt: 1 });
        pending.clear();
        work.update();
        assert.equal(turns().length, 1, 'the send and existing card converge to one tracked view');
        assert.equal(turns()[0], existing, 'keep the already represented turn node');
        assert.equal(existing.dataset.settled, '0');
        assert.ok(existing.contains(document.activeElement));
        trigger.click(); trigger.click();
        assert.equal(turns().length, 1, 'reopening leaves no orphan Working row');
        assert.equal(turns()[0].dataset.settled, '0');
    } finally { work?.destroy(); restoreDom(prior); }
});

test('the one mark after the word turns amber for the owner\'s question beside any other word; the control paints the producer\'s tone', () => {
    const { prior, mount } = installDom(async () => ({ ok: true, json: async () => ({}) }));
    let work;
    try {
        const node = (tag, cls, parent) => {
            const el = document.createElement(tag); el.className = cls; parent.appendChild(el); return el;
        };
        const page = node('div', 'page', mount);
        const header = node('div', 'chat-page-header', page);
        const titleRow = node('div', 'app-page-title-row', node('div', 'app-page-title-block', header));
        const group = node('div', 'app-page-actions ui-chrome-group', header);
        const badge = node('span', 'status-badge thinking', group);
        const messages = node('div', 'chat-messages', page);
        const ask = { activity_id: 't-ask', kind: 'managed_task', phase: 'working', chat_id: 1,
            owner_wait: { quiz_id: 'q-1', owner_wait_state: 'waiting', quiz_state: 'open' } };
        const activities = new Map([['t-q', { activity_id: 't-q', kind: 'managed_task', phase: 'queued', chat_id: 1 }], ['t-ask', ask]]);
        work = createRoomWork({ page, statusBadge: badge, messagesDiv: messages,
            ws: { isConnected: () => true }, reading: { cancel() {}, scroll() {} },
            activeDirectActivities: activities, liveCardRecords: new Map(), pendingSubmissions: new Map() });
        const trigger = titleRow.children.find(el => el.classList.contains('rw-trigger'));
        // The word leads; its one mark (three dots or one) follows it, and nothing else is a dot.
        const order = trigger.children.map(el => el.className.split(' ').find(c => /^(status-badge|rw-mark|rw-dots|rw-caret)$/.test(c)))
            .filter(Boolean);
        assert.deepEqual(order, ['status-badge', 'rw-mark', 'rw-dots', 'rw-caret']);
        assert.equal(trigger.children.some(el => el.classList.contains('rw-amber')), false);
        const paint = (text, tone, dots) => {
            badge.textContent = text;
            Object.assign(badge.dataset, { tone, dots });
            work.update();
            return [trigger.dataset.tone, trigger.dataset.wait !== '1'];
        };
        // Before the producer's first answer (no tone on the badge) the control never claims Online.
        work.update();
        assert.equal(trigger.dataset.tone, 'offline');
        // Queued outranks the answer wait in the word; the question still turns the mark amber.
        assert.deepEqual(paint('Queued', 'queued', 'still'), ['queued', false]);
        assert.deepEqual(paint('Working', 'live', 'moving'), ['live', false]);
        assert.deepEqual(paint('Paused · owner pause', 'held', 'one'), ['held', false]);
        // The word already says it: the mark keeps the word's own tone.
        assert.deepEqual(paint('Waiting for your answer', 'wait', 'one'), ['wait', true]);
        // Without a question there is no amber, whatever the word.
        activities.delete('t-ask');
        assert.deepEqual(paint('Queued', 'queued', 'still'), ['queued', true]);
        assert.deepEqual(paint('Paused', 'held', 'one'), ['held', true]);
    } finally { work?.destroy(); restoreDom(prior); }
});

// DESIGN "Room work status": a row goes to its message wherever it lies in the room's history.
test('an unloaded row reads the page around its task and lands on its card; one with no message says so', async () => {
    const { prior, mount } = installDom(async () => ({ ok: true, json: async () => ({}) }));
    let work;
    try {
        const node = (tag, cls, parent) => {
            const el = document.createElement(tag); el.className = cls; parent.appendChild(el); return el;
        };
        const page = node('div', 'page', mount);
        const header = node('div', 'chat-page-header', page);
        const titleRow = node('div', 'app-page-title-row', node('div', 'app-page-title-block', header));
        const group = node('div', 'app-page-actions ui-chrome-group', header);
        const badge = node('span', 'status-badge thinking', group);
        const messages = node('div', 'chat-messages', page);
        const records = new Map(), asked = [];
        const activity = (id, extra = {}) => [id, { activity_id: id, kind: 'direct_chat', phase: 'working', chat_id: 1, ...extra }];
        const activities = new Map([activity('t-old', { phase: 'budget_paused', pause_cause: 'owner' }), activity('t-wake'),
            activity('t-own', { client_message_id: 'msg-1' })]);
        const historyPager = {
            pageOf: () => 'replay:recent', revisit: async () => ({ status: 'applied' }),
            around: async (address, wanted) => {
                asked.push([address, wanted()]);
                if (address !== 'task:t-old') return { status: 'anchor', anchor: { state: 'absent', task_id: address.slice(5) } };
                const card = node('div', 'chat-live-card', messages);  // the page around it mounts its card
                records.set('t-old', { root: card, groupId: 't-old' });
                return { status: 'applied', anchor: { state: 'found', task_id: 't-old', history_id: 'chat:100' } };
            },
        };
        work = createRoomWork({ page, statusBadge: badge, messagesDiv: messages, ws: { isConnected: () => true },
            reading: { stick: false, cancel() {}, scroll() {}, claim: () => () => true }, historyPager,
            captureVisibleTimelineAnchor: () => ({ historyId: 'chat:900' }),
            activeDirectActivities: activities, liveCardRecords: records, pendingSubmissions: new Map() });
        const trigger = titleRow.children.find((el) => el.classList.contains('rw-trigger'));
        const anchor = header.children.find((el) => el.classList.contains('rw-shade-anchor'));
        const list = anchor.children.find((el) => el.classList.contains('rw-surface')).children[0];
        const row = (id) => list.children.find((el) => el.dataset.id === id);
        const main = (id) => row(id).querySelector('.rw-main');
        const text = (el) => [el.textContent, ...el.children.map(text)].join(' ');
        const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
        trigger.click();
        // Before it asks, a row claims nothing about where its message is.
        assert.match(main('t-old').getAttribute('aria-label'), /^Open: /);
        assert.doesNotMatch(main('t-old').getAttribute('aria-label'), /loaded/);
        assert.equal(main('t-old').dataset.target, 'history');
        assert.doesNotMatch(text(list), /Not in loaded messages|cannot take you to it/);

        main('t-old').click();
        await settle();
        assert.deepEqual(asked, [['task:t-old', true]]);
        assert.equal(work.isOpen(), false, 'landed: the reader came to read it');
        assert.equal(page.children.find((el) => el.classList.contains('rw-back')).hidden, false, 'Back returns to the passage left');

        trigger.click();
        assert.match(main('t-old').getAttribute('aria-label'), /^Go to task: /, 'its card is loaded now');
        main('t-wake').click();
        await settle();
        assert.equal(work.isOpen(), true, 'nothing to land on: the list stays and says so');
        assert.match(text(row('t-wake')), /No message yet\./);
        assert.match(main('t-wake').getAttribute('aria-label'), /^Details: /);
        main('t-wake').click();  // its details close; it does not ask again
        await settle();
        assert.equal(asked.length, 2);
        // Work whose census facts change may have a message now: its row claims nothing again and a press asks.
        activities.set('t-wake', { ...activities.get('t-wake'), phase: 'budget_paused', pause_cause: 'owner' });
        work.update();
        await new Promise((resolve) => setTimeout(resolve, 20));
        assert.match(main('t-wake').getAttribute('aria-label'), /^Open: /);
        main('t-wake').click();
        await settle();
        assert.equal(asked.length, 3);
        assert.match(text(row('t-wake')), /No message yet\./, 'the press answered again');
        // Its details are open now: another census change keeps them open, without the stale answer.
        activities.set('t-wake', { ...activities.get('t-wake'), phase: 'working', pause_cause: undefined });
        work.update();
        await new Promise((resolve) => setTimeout(resolve, 20));
        assert.equal(main('t-wake').getAttribute('aria-expanded'), 'true', 'opened details survive');
        assert.match(main('t-wake').getAttribute('aria-label'), /^Open: /, 'without the stale answer: a press asks again');
        main('t-own').click();
        await settle();
        assert.match(text(row('t-own')), /Its message could not be checked\./, 'an owner’s message is never “no message”');
        assert.doesNotMatch(text(list), /Not in loaded messages|cannot take you to it/);
        trigger.click();
        trigger.click();
        assert.match(main('t-wake').getAttribute('aria-label'), /^Open: /, 'a later open asks again');
        // A list dismissed while a row looks drops that look: nothing moves when it answers later.
        let answer, wantedThen = null;
        historyPager.around = (address, wanted) => new Promise((resolve) => {
            answer = () => { wantedThen = wanted(); resolve({ status: 'superseded' }); };
        });
        main('t-wake').click();
        await settle();
        trigger.click();  // dismissed
        answer();
        await settle();
        assert.equal(wantedThen, false, 'the dismissed list no longer wants that page');
        // Pressed again after the list reopened, then dismissed again: the first look's late answer never undoes
        // the second look's cancellation.
        const answers = [], wanted = [];
        historyPager.around = (address, isWanted) => new Promise((resolve) => {
            answers.push(() => { wanted.push(isWanted()); resolve({ status: 'superseded' }); });
        });
        trigger.click();
        main('t-wake').click();
        await settle();
        trigger.click();  // dismissed
        trigger.click();
        main('t-wake').click();  // the same row again
        await settle();
        answers[0]();
        await settle();
        trigger.click();  // dismissed again
        answers[1]();
        await settle();
        assert.deepEqual(wanted, [false, false], 'neither look applies once its list closed');
    } finally { work?.destroy(); restoreDom(prior); }
});

test('Back reads the page it left again, stays after a failed read and takes a follower back to the present', async () => {
    const { prior, mount } = installDom(async () => ({ ok: true, json: async () => ({}) }));
    let work;
    try {
        const node = (tag, cls, parent) => {
            const el = document.createElement(tag); el.className = cls; parent.appendChild(el); return el;
        };
        const page = node('div', 'page', mount);
        const header = node('div', 'chat-page-header', page);
        const titleRow = node('div', 'app-page-title-row', node('div', 'app-page-title-block', header));
        const badge = node('span', 'status-badge thinking', node('div', 'app-page-actions ui-chrome-group', header));
        const messages = node('div', 'chat-messages', page);
        const records = new Map(), revisits = [], presents = [];
        let mounted = false, fails = 1, gone = false;
        const historyPager = {
            pageOf: () => 'replay:left',
            revisit: async (cursor, wanted) => {
                revisits.push([cursor, wanted()]);
                if (gone) return { status: 'error', error: { status: 409 } };
                if (fails-- > 0) return { status: 'error' };
                mounted = true;  // the page the reader left is drawn again
                return { status: 'applied' };
            },
            around: async () => {
                records.set('t-old', { root: node('div', 'chat-live-card', messages), groupId: 't-old' });
                return { status: 'applied', anchor: { state: 'found', task_id: 't-old', history_id: 'chat:100' } };
            },
        };
        const reading = { stick: false, nearBottom: () => false, cancel() {}, scroll() {}, claim: () => () => true };
        work = createRoomWork({ page, statusBadge: badge, messagesDiv: messages, ws: { isConnected: () => true }, reading,
            historyPager, showLatest: async () => presents.push('newest'),
            captureVisibleTimelineAnchor: () => ({ historyId: 'chat:900' }),
            restoreVisibleTimelineAnchor: (_anchor, { exact } = {}) => Boolean(exact && mounted),
            activeDirectActivities: new Map([['t-old', { activity_id: 't-old', kind: 'direct_chat', phase: 'working', chat_id: 1 }]]),
            liveCardRecords: records, pendingSubmissions: new Map() });
        const trigger = titleRow.children.find((el) => el.classList.contains('rw-trigger'));
        const list = header.children.find((el) => el.classList.contains('rw-shade-anchor')).children[0].children[0];
        const main = () => list.children.find((el) => el.dataset.id === 't-old').querySelector('.rw-main');
        const back = page.children.find((el) => el.classList.contains('rw-back'));
        const live = page.children.find((el) => el.getAttribute('role') === 'status');
        const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
        trigger.click();
        main().click();
        await settle();
        back.click();
        await settle();
        assert.deepEqual(revisits, [['replay:left', true]], 'the passage was let go: its page is read again, once');
        assert.equal(back.hidden, false, 'a failed read keeps Back for another try');
        assert.match(live.textContent, /could not be read/);
        back.click();
        await settle();
        assert.equal(revisits.length, 2);
        assert.equal(back.hidden, true);
        assert.equal(live.textContent, 'Returned to where you were reading.');
        assert.deepEqual(presents, []);
        // A page that cannot be read again (the room's view changed) ends Back instead of asking for a retry.
        mounted = false;
        gone = true;
        trigger.click();
        main().click();
        await settle();
        back.click();
        await settle();
        assert.equal(revisits.length, 3);
        assert.equal(live.textContent, 'The earlier reading place is no longer loaded.');
        gone = false;
        // A reader following the present goes back to it and reads no left page.
        Object.assign(reading, { stick: true, nearBottom: () => true });
        trigger.click();
        main().click();
        await settle();
        back.click();
        await settle();
        assert.deepEqual(presents, ['newest']);
        assert.equal(revisits.length, 3);
    } finally { work?.destroy(); restoreDom(prior); }
});
