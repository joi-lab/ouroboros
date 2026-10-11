import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as portable from '../modules/ui_primitives.js';
import * as existing from '../modules/ui_helpers.js';
import { escapeHtmlAttr } from '../modules/utils.js';

test('host consumers and author kit share the same portable functions', () => {
    for (const name of ['renderSafeField', 'collectSafeFieldValues', 'normalizeTone', 'setInlineStatus']) {
        assert.equal(existing[name], portable[name]);
    }
    assert.equal(escapeHtmlAttr, portable.escapeHtmlAttr);
});

test('portable source imports in an isolated document without host dependencies', async () => {
    const source = fs.readFileSync(new URL('../modules/ui_primitives.js', import.meta.url), 'utf8');
    assert.doesNotMatch(source, /^\s*import\b/m);
    const exported = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
    assert.equal(exported.normalizeTone('error'), 'danger');
    const result = exported.renderSafeField({ name: 'city', label: '<City>', default: '" value' });
    assert.match(result, /&lt;City&gt;/);
    assert.match(result, /value="&quot; value"/);
    assert.match(result, /class="ui-control"/);
});

test('field rendering escapes values and preserves secret/non-secret semantics', () => {
    const field = portable.renderSafeField({ name: 'token', type: 'password', label: 'Token', default: 'not-a-default', help: '<help>' }, {token: 'not-a-draft'});
    assert.match(field, /type="password"/);
    assert.match(field, /value=""/);
    assert.doesNotMatch(field, /not-a-/);
    assert.match(field, /&lt;help&gt;/);
    const textarea = portable.renderSafeField({name: 'text', type: 'textarea'}, {text: '</textarea><script>x</script>'});
    assert.doesNotMatch(textarea, /<script>/);
    assert.match(textarea, /&lt;\/textarea&gt;/);
    const select = portable.renderSafeField({name: 'source', type: 'select', options: ['one','two']}, {source: 'two'});
    assert.match(select, /value="two" selected/);
    const checkbox = portable.renderSafeField({name: 'enabled', type: 'checkbox'}, {enabled: true});
    assert.match(checkbox, /class="ui-checkbox"/);
    assert.match(checkbox, / checked/);
});

test('collection follows field type and never silently persists passwords when excluded', () => {
    const inputs={enabled:{checked:false,value:'on'},n:{value:'2'},key:{value:'secret'}};
    const form={elements:{namedItem:name=>inputs[name]}};
    const fields=[{name:'enabled',type:'checkbox'},{name:'n',type:'number'},{name:'key',type:'password'}];
    assert.deepEqual(portable.collectSafeFieldValues(form,fields), {enabled:false,n:'2',key:'secret'});
    assert.deepEqual(portable.collectSafeFieldValues(form,fields,{includePasswords:false}), {enabled:false,n:'2'});
});

test('status updates text and tone without replacing the host or resetting identical text', () => {
    let writes=0;let text='ready';
    const el={dataset:{},get textContent(){return text;},set textContent(v){text=v;writes++;}};
    portable.setInlineStatus(el,'ready','success');
    assert.equal(writes,0);assert.equal(el.dataset.tone,'ok');
    portable.setInlineStatus(el,'failed','error');
    assert.equal(writes,1);assert.equal(el.textContent,'failed');assert.equal(el.dataset.tone,'danger');
});

test('safe controls name the field independently of options, help and secret values', () => {
    for (const type of ['text','textarea','select','checkbox']) {
        const html = portable.renderSafeField({name:'view',label:'View',type,options:['List','Grid'],help:'Choose <one>'});
        assert.match(html, /aria-label="View"/);
        assert.match(html, /aria-description="Choose &lt;one&gt;"/);
    }
});
