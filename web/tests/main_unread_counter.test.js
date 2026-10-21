// Both sidebar numbers count the same thing (owner Q1 = B, 19.10.2026; DESIGN "Unread"): unread standalone
// messages, Main's from its room revision against the shared read cursor, the Projects' summed over the rows.
// app.js is a boot script, so the functions are lifted out of its source and run against stubbed module state.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../app.js', import.meta.url), 'utf8').replace(/\r\n?/g, '\n');

function lift(name) {
    const from = source.search(new RegExp(`\\n(async )?function ${name}\\(`));
    const until = source.indexOf('\n}\n', from + 1);
    assert.ok(from >= 0 && until > from, `${name} is a top-level function in app.js`);
    return source.slice(from + 1, until + 2);
}

function harness({ revision = null, seen = null, loaded = true, page = 'files', answer = () => ({}) } = {}) {
    const posts = [], badges = [], acks = [];
    const context = vm.createContext({
        state: { mainVisibleRevision: revision, mainSeenRevision: seen, mainSeenLoaded: loaded, activePage: page },
        updateUnreadBadge: () => badges.push(context.mainUnreadCount()),
        refreshSharedProjectSeen: () => posts.push(['shared']),
        acknowledgeMainAfterPaint: () => acks.push(context.state.mainVisibleRevision),
        fetchJson: async (url, init = {}) => {
            posts.push([url, init.method || 'GET', JSON.parse(init.body || '{}')]);
            return answer(JSON.parse(init.body || '{}'));
        },
    });
    vm.runInContext(['mainUnreadCount', 'observeMainRevision', 'mergeMainSeenRevision', 'markMainViewed']
        .map(lift).join('\n') + '\nglobalThis.mainUnreadCount = mainUnreadCount; globalThis.api = { observeMainRevision,'
        + ' mergeMainSeenRevision, markMainViewed, mainUnreadCount };', context);
    return { context, posts, badges, acks, api: context.api };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("Main's number is its room revision less its read cursor; unknown counts none", () => {
    assert.equal(harness({ revision: 42, seen: 40 }).api.mainUnreadCount(), 2);
    assert.equal(harness({ revision: 40, seen: 42 }).api.mainUnreadCount(), 0);
    assert.equal(harness({ revision: null, seen: 40 }).api.mainUnreadCount(), 0);
    assert.equal(harness({ revision: 42, seen: null }).api.mainUnreadCount(), 0);
});

test('a cursor the server never held is set once to what Main holds, never per arrival', async () => {
    const h = harness({ seen: null, answer: (body) => ({ main_seen_revision: body.main_seen_revision }) });
    h.api.observeMainRevision(161);
    await flush();
    assert.deepEqual(h.posts, [['/api/ui/preferences', 'POST', { main_seen_revision: 161 }]]);
    assert.equal(h.context.state.mainSeenRevision, 161);
    assert.equal(h.api.mainUnreadCount(), 0, 'the history before this version reads as read');
    h.api.observeMainRevision(163);
    await flush();
    assert.equal(h.api.mainUnreadCount(), 2, 'later messages count');
    assert.equal(h.posts.filter(([url]) => url === '/api/ui/preferences').length, 1, 'no second initial write');
});

test('before the shared cursors are read nothing is written and nothing counts; they are read again', async () => {
    const h = harness({ seen: null, loaded: false });
    h.api.observeMainRevision(7);
    await flush();
    assert.deepEqual(h.posts, [['shared']], 'a read, never a write');
    assert.equal(h.api.mainUnreadCount(), 0);
});

test('a failed cursor read initializes nothing and is read again; an answer with the key then counts', () => {
    const h = harness({ seen: null, loaded: false });
    for (const failed of [null, undefined, {}, { error: 'busy' }]) {
        assert.equal(h.api.mergeMainSeenRevision(failed), false);
        assert.equal(h.context.state.mainSeenLoaded, false, 'an answer without the key proves nothing');
    }
    h.api.observeMainRevision(5);
    assert.deepEqual(h.posts, [['shared']], 'the cursor is read again; no write sets it to what Main holds');
    assert.equal(h.api.mainUnreadCount(), 0);
    assert.equal(h.api.mergeMainSeenRevision({ main_seen_revision: 2 }), true);
    assert.equal(h.api.mainUnreadCount(), 3, 'the stored cursor, not the current revision');
});

test('an unreadable revision keeps the known one', () => {
    const h = harness({ revision: 9, seen: 5 });
    h.api.observeMainRevision(null);
    assert.equal(h.context.state.mainVisibleRevision, 9);
    assert.equal(h.api.mainUnreadCount(), 4);
});

test('a newer revision re-reads the shared cursors, and offers the read decision only while Main is shown', () => {
    const away = harness({ revision: 3, seen: 3, page: 'files' });
    away.api.observeMainRevision(5);
    assert.deepEqual(away.posts, [['shared']]);
    assert.deepEqual(away.acks, []);
    const shown = harness({ revision: 3, seen: 3, page: 'chat' });
    shown.api.observeMainRevision(5);
    assert.deepEqual(shown.acks, [5]);
});

test('the shared cursor only moves forward, and a write keeps what the server answers', async () => {
    const h = harness({ revision: 10, seen: 6, answer: () => ({ main_seen_revision: 8 }) });
    assert.equal(h.api.mergeMainSeenRevision({ main_seen_revision: 4 }), false, 'a stale answer never revives a read number');
    assert.equal(h.context.state.mainSeenRevision, 6);
    assert.equal(await h.api.markMainViewed(10), true);
    assert.equal(h.context.state.mainSeenRevision, 8, 'the clamped cursor the server confirmed');
    assert.equal(h.api.mainUnreadCount(), 2);
});

test('a failed write changes nothing', async () => {
    const h = harness({ revision: 10, seen: 6, answer: () => { throw new Error('offline'); } });
    assert.equal(await h.api.markMainViewed(10), false);
    assert.equal(h.context.state.mainSeenRevision, 6);
});

test('the Projects number sums the unread messages of the unread rows', () => {
    const count = { textContent: '', title: '', attrs: {},
        setAttribute(name, value) { this.attrs[name] = value; }, removeAttribute(name) { delete this.attrs[name]; } };
    const context = vm.createContext({
        navProjectsCount: count,
        state: { projectSeenRevision: { a: 1, b: 4, c: 0 } },
        lastProjectRows: [{ id: 'a', visible_revision: 4, _unread: true }, { id: 'b', visible_revision: 4, _unread: false },
            { id: 'c', visible_revision: 2, _unread: true }, { id: 'd', visible_revision: 3, _unread: false }],
    });
    vm.runInContext(`${lift('paintProjectsCount')}\nglobalThis.paint = paintProjectsCount;`, context);
    context.paint();
    assert.equal(count.textContent, '5');
    assert.equal(count.attrs['aria-label'], '5 unread messages in Projects');
    context.lastProjectRows = [];
    context.paint();
    assert.equal(count.textContent, '');
    assert.equal(count.attrs['aria-label'], undefined);
});
