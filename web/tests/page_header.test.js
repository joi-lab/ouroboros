import test from 'node:test';
import assert from 'node:assert/strict';
import { renderPageHeader } from '../modules/page_header.js';

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

test('the optional toolbar class cannot create an HTML attribute or node', () => {
    const html = renderPageHeader({ title: 'Chat', actionsHtml: '<button>Act</button>', toolbarClass: '\" onclick=\"alert(1)\"><img src=x>' });
    assert.doesNotMatch(html, /class="app-page-toolbar app-page-actions " onclick=/);
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&quot;/);
    assert.match(html, /&lt;img/);
});
