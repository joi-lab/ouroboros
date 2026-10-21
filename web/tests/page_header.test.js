import test from 'node:test';
import assert from 'node:assert/strict';
import { bindHeaderControlPlacement, renderMobileNavToggle, renderPageHeader } from '../modules/page_header.js';

test('page headers retain their toolbar contract with an optional chrome recipe', () => {
    for (const field of ['toolbarHtml', 'actionsHtml']) {
        const basic = renderPageHeader({ title: 'Chat', [field]: '<button>Act</button>' });
        assert.match(basic, /class="app-page-toolbar app-page-actions"/);
        const grouped = renderPageHeader({ title: 'Chat', [field]: '<button>Act</button>', toolbarClass: 'ui-chrome-group' });
        assert.match(grouped, /class="app-page-toolbar app-page-actions ui-chrome-group"/);
        assert.equal((grouped.match(/<button>Act<\/button>/g) || []).length, 1);
    }
    assert.doesNotMatch(renderPageHeader({ title: 'Chat', toolbarClass: 'ui-chrome-group' }), /app-page-toolbar/);
});

test('one header: no icon, no caption line, the tabs are a sibling after the row, a Project keeps its name', () => {
    const page = renderPageHeader({ title: 'Widgets', icon: '<svg/>', description: 'Reviewed surfaces live here.' });
    assert.doesNotMatch(page, /app-page-icon|<svg\/>/, 'no page icon anywhere (owner 5A); the ☰ glyph is a control');
    assert.doesNotMatch(page, /Reviewed surfaces live here|app-page-lede|app-page-description/, 'a page has no caption line');
    assert.match(page, /<div class="page-header app-page-header" data-sidebar-names data-empty>/, 'named by the sidebar, nothing else to say');
    const tabbed = renderPageHeader({ title: 'Dashboard', tabsHtml: '<div role="tablist"></div>' });
    assert.match(tabbed, /<\/div>\s*<div class="app-page-tabs"><div role="tablist"><\/div><\/div>/, 'tabs are their own strip');
    assert.equal(tabbed.indexOf('app-page-tabs') > tabbed.indexOf('</div>'), true);
    assert.match(tabbed, /data-sidebar-names data-empty>/, 'tabs alone leave the row empty on a desk');
    const acting = renderPageHeader({ title: 'Files', actionsHtml: '<button>Refresh</button>' });
    assert.match(acting, /data-sidebar-names>/);
    assert.doesNotMatch(acting, /data-empty/);
    const project = renderPageHeader({ title: '', titleId: 'project-panel-title', named: false, trailingHtml: '<button>×</button>' });
    assert.match(project, /<h2 class="app-page-title" id="project-panel-title"><\/h2>/);
    assert.doesNotMatch(project, /data-sidebar-names|data-empty/, 'a Project is named by its owner: its name stays');
    assert.match(project, /app-page-trailing/);
    assert.match(project, /data-mobile-nav-toggle/, 'a header shows ☰ unless its room carries it in the composer');
    assert.match(renderPageHeader({ title: 'x', titleId: '"><i>' }), /id="&quot;&gt;&lt;i&gt;"/);
    assert.doesNotMatch(renderPageHeader({ title: 'x', showMobileNav: false }), /data-mobile-nav-toggle/);
    // A room's ☰ (owner 19.10, 1A): the composer row's first control, capsule-sized with the finger's hit area.
    assert.match(renderMobileNavToggle({ inComposer: true }), /class="shell-icon-control mobile-nav-toggle composer-menu ui-hit-area"/);
    assert.match(renderMobileNavToggle(), /class="shell-icon-control mobile-nav-toggle"/);
    // Both placements carry one status mark for what waits behind the ☰, hidden until app.js paints it.
    for (const toggle of [renderMobileNavToggle(), renderMobileNavToggle({ inComposer: true })]) {
        assert.match(toggle, /aria-label="Open navigation"[^>]*><svg[^]*<\/svg><span class="status-mark" aria-hidden="true" data-shape="none" data-hue="ink" hidden><i><\/i><i><\/i><i><\/i><\/span><\/button>/);
    }
});

test('Restart and the budget are the same nodes, placed by layout: the row on a desk, the ⋯ menu on a phone', () => {
    const node = (name, parent) => {
        const el = { name, children: [], parentElement: null,
            get nextSibling() { const i = this.parentElement.children.indexOf(this); return this.parentElement.children[i + 1] || null; },
            detach() { if (this.parentElement) this.parentElement.children.splice(this.parentElement.children.indexOf(this), 1); },
            prepend(x) { x.detach(); x.parentElement = this; this.children.unshift(x); },
            append(x) { x.detach(); x.parentElement = this; this.children.push(x); },
            insertBefore(x, ref) { x.detach(); x.parentElement = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(x); else this.children.splice(i, 0, x); },
            querySelector(sel) { const all = []; (function walk(n) { for (const c of n.children) { all.push(c); walk(c); } }(this)); return all.find((c) => c.sel === sel) || null; } };
        if (parent) parent.append(el);
        return el;
    };
    const toolbar = node('toolbar');
    const actions = node('actions', toolbar);
    const restart = node('restart', actions); restart.sel = '[data-chat-command="restart"]';
    const panic = node('panic', actions); panic.sel = '[data-chat-command="panic"]';
    const details = node('details', actions);
    const menu = node('menu', details); menu.sel = '.chat-header-menu';
    const budget = node('budget', toolbar);
    const status = node('status', toolbar);
    const listeners = new Map();
    globalThis.window = { ouroLayout: { layout: 'stack' }, addEventListener: (t, fn) => listeners.set(t, fn), removeEventListener: (t) => listeners.delete(t) };
    globalThis.document = { documentElement: { dataset: {} } };
    const dispose = bindHeaderControlPlacement({ headerActions: actions, budgetPill: budget });
    assert.deepEqual(menu.children.map((c) => c.name), ['budget', 'restart'], 'stack: the budget first, Restart last, inside ⋯');
    assert.deepEqual(actions.children.map((c) => c.name), ['panic', 'details']);
    globalThis.window.ouroLayout.layout = 'docked';
    listeners.get('ouro:layout-changed')({ detail: { layout: 'docked' } });
    assert.deepEqual(actions.children.map((c) => c.name), ['restart', 'panic', 'details'], 'docked: Restart back before Panic');
    assert.deepEqual(toolbar.children.map((c) => c.name), ['actions', 'budget', 'status'], 'the budget back after the actions');
    // A docked bar too narrow for the full row (a narrow content column) moves them too; a wide one keeps the row.
    const bar = { clientWidth: 500 };
    const narrow = bindHeaderControlPlacement({ headerActions: actions, budgetPill: budget, bar });
    assert.deepEqual(menu.children.map((c) => c.name), ['budget', 'restart'], 'a 500px bar: inside ⋯');
    narrow();
    bar.clientWidth = 900;
    const wide = bindHeaderControlPlacement({ headerActions: actions, budgetPill: budget, bar });
    assert.deepEqual(actions.children.map((c) => c.name), ['restart', 'panic', 'details'], 'a 900px bar: the row');
    wide();
    dispose();
    assert.equal(listeners.size, 0);
    assert.equal(typeof bindHeaderControlPlacement({ headerActions: null }), 'function', 'no actions: a no-op disposer');
    delete globalThis.window; delete globalThis.document;
});

test('the optional toolbar class cannot create an HTML attribute or node', () => {
    const html = renderPageHeader({ title: 'Chat', actionsHtml: '<button>Act</button>', toolbarClass: '\" onclick=\"alert(1)\"><img src=x>' });
    assert.doesNotMatch(html, /class="app-page-toolbar app-page-actions " onclick=/);
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&quot;/);
    assert.match(html, /&lt;img/);
});
