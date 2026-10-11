import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

// The real vendored parser: whether a block is code is marked's reading, not a guess.
// The sanitizer and the DOM post-pass are the browser's (test_markdown_reading_browser).
const priorMarked = globalThis.marked;
const priorDocument = globalThis.document;
const priorPurify = globalThis.DOMPurify;
vm.runInThisContext(readFileSync(new URL('../marked.min.js', import.meta.url), 'utf8'));
globalThis.document = {
    createElement: () => ({
        content: { querySelectorAll: () => [] },
        get innerHTML() { return this.value; },
        set innerHTML(value) { this.value = String(value); },
    }),
};
globalThis.DOMPurify = { sanitize: (html) => html };
const { prepareMarkdownSource, renderChatMarkdown } = await import('../modules/chat_markdown.js');
test.after(() => {
    globalThis.marked = priorMarked;
    globalThis.document = priorDocument;
    globalThis.DOMPurify = priorPurify;
});

const plainMarked = new globalThis.marked.Marked({ gfm: true, breaks: true });
const markedAlone = (text) => plainMarked.parse(prepareMarkdownSource(text).source);
const MATHLIKE = '$$ a &amp; b <https://x.test/?q=1> \\(c\\) \\[d\\] $$';

test('code holding math-like text is exactly the code marked writes, with its URL and entity literal', () => {
    const cases = [
        `~~~\n${MATHLIKE}\n~~~`,
        `~~~~tex\n~~~\n${MATHLIKE}\n~~~~`,
        `    ${MATHLIKE}`,
        `\t${MATHLIKE}`,
        `\`\`\`\n${MATHLIKE}\n\`\`\``,
        `Span \`\`${MATHLIKE} \` tick\`\` done`,
        `> quote\n>\n>     ${MATHLIKE}`,
        `- item\n\n      ${MATHLIKE}`,
        `- item\n\n  ~~~\n  ${MATHLIKE}\n  ~~~`,
    ];
    for (const source of cases) {
        const html = renderChatMarkdown(source);
        assert.equal(html, markedAlone(source), source);
        assert.match(html, /&lt;https:\/\/x\.test\/\?q=1&gt;/, source);
        assert.match(html, /&amp;amp;/, source);
        assert.doesNotMatch(html, /<https:|<a\b|OUROBOROSLATEX/, source);
    }
});

test('a $$ in one code block never pairs with one outside it', () => {
    for (const fence of [['~~~sh\n', '\n~~~'], ['    ', ''], ['```sh\n', '\n```']]) {
        const block = `${fence[0]}echo $$${fence[1]}`;
        const source = `${block}\n\nmiddle **prose** $$x_1$$\n\n${block}`;
        const html = renderChatMarkdown(source);
        assert.equal(html.match(/<pre><code[^>]*>echo \$\$\n<\/code><\/pre>/g)?.length, 2, source);
        assert.match(html, /<p>middle <strong>prose<\/strong> \$\$x_1\$\$<\/p>/, source);
    }
});

test('prose math stays the author text and never restores a raw tag', () => {
    const html = renderChatMarkdown('see $$ a <https://x.test> &amp; b_1 *c* $$ and \\(x_1 * y\\)');
    assert.equal(html, '<p>see $$ a &lt;https://x.test&gt; &amp; b_1 *c* $$ and \\(x_1 * y\\)</p>\n');
    // Display math may still span a blank line when no code lies between.
    assert.equal(renderChatMarkdown('$$a_1\n\nb_2$$'), '<p>$$a_1\n\nb_2$$</p>\n');
    assert.equal(renderChatMarkdown('$$ "q" \'s\' $$'), '<p>$$ &quot;q&quot; &#39;s&#39; $$</p>\n');
});

test('an escaped < reads as < in prose and keeps its backslash in parked math', () => {
    // The browser reads `&#060;` as `<` (test_markdown_reading_browser checks the page); marked keeps
    // references as written, so the prose here shows the stand-in, never `&amp;` before it.
    assert.equal(renderChatMarkdown('\\<tag> 5 \\< 6 a\\\\<b>'), '<p>&#060;tag&gt; 5 &#060; 6 a\\&#60;b&gt;</p>\n');
    assert.equal(renderChatMarkdown('$$a \\< b$$ \\<t>'), '<p>$$a \\&#60; b$$ &#060;t&gt;</p>\n');
});

test('a $$ in code nested in a quote or list item never pairs with the prose after it', () => {
    // The nested code keeps marked's literal bytes (its URL, image and delimiters inert);
    // the prose after it keeps its Markdown and its math, parked so marked cannot read `*c*`.
    const code = 'echo $$ <https://x.test> ![i](https://x.test/i.png) \\(';
    const prose = 'after **bold** $$b_1 *c*$$';
    for (const source of [
        `>     ${code}\n> ${prose}`,
        `> quote\n> > deep\n> >\n> >     ${code}\n> ${prose}`,
        `- item\n\n      ${code}\n\n  ${prose}`,
        `- [ ] task\n\n      ${code}\n\n  ${prose}`,
        `1. [x] task\n\n   ~~~\n   ${code}\n   ~~~\n\n   ${prose}`,
    ]) {
        const html = renderChatMarkdown(source);
        assert.equal(html, markedAlone(source).replace('$$b_1 <em>c</em>$$', () => '$$b_1 *c*$$'), source);
        assert.match(html, /<pre><code>echo \$\$ &lt;https:\/\/x\.test&gt; !\[i\]\(https:\/\/x\.test\/i\.png\) \\\(\n<\/code><\/pre>/, source);
        assert.match(html, /<p>after <strong>bold<\/strong> \$\$b_1 \*c\*\$\$<\/p>/, source);
        assert.doesNotMatch(html, /<img|<a\b|<https:|OUROBOROSLATEX/, source);
    }
});

test('a task item keeps its checkbox and its prose, and its math stays the author text', () => {
    // marked moves a task's `[ ] ` out of the item's text into a checkbox child.
    const box = (checked) => `<input ${checked ? 'checked="" ' : ''}disabled="" type="checkbox">`;
    for (const [source, html] of [
        ['- [ ] Compute \\(x_1 + 1\\)', `<ul>\n<li>${box(false)} Compute \\(x_1 + 1\\)</li>\n</ul>\n`],
        ['- [x] Compute \\(x_1 + 1\\)', `<ul>\n<li>${box(true)} Compute \\(x_1 + 1\\)</li>\n</ul>\n`],
        ['- [ ] Compute $$x_1 *c*$$', `<ul>\n<li>${box(false)} Compute $$x_1 *c*$$</li>\n</ul>\n`],
        // A loose item carries its box inside its first paragraph.
        ['- [X] **one** \\[a_1\\]\n\n- [ ] two $$b *c*$$',
            `<ul>\n<li><p>${box(true)} <strong>one</strong> \\[a_1\\]</p>\n</li>\n<li><p>${box(false)} two $$b *c*$$</p>\n</li>\n</ul>\n`],
        ['> 1. [x] quoted \\(q\\)\n>    - [ ] nested `\\(c\\)` $$i *c*$$',
            `<blockquote>\n<ol>\n<li>${box(true)} quoted \\(q\\)<ul>\n<li>${box(false)} nested <code>\\(c\\)</code> $$i *c*$$</li>\n</ul>\n</li>\n</ol>\n</blockquote>\n`],
    ]) {
        assert.equal(renderChatMarkdown(source), html, source);
    }
});

// Display math marked alone would read as Markdown; the renderer parks it as the author wrote it.
const parked = (html) => html.replace(/\$\$([xy])_1 <em>c<\/em>\$\$/, (_, name) => `$$${name}_1 *c*$$`);

test('a code span marked reads across lines keeps its $$ from pairing with the prose math after it', () => {
    // Each span holds one $$ and runs of other lengths; only a run of its own length ends it.
    for (const n of [1, 2, 3, 4]) {
        const [run, shorter, longer] = [n, n - 1, n + 1].map((length) => '`'.repeat(length));
        const code = `echo $$ ${shorter} <https://x.test/?q=1> &amp; ![i](https://x.test/i.png)`;
        for (const source of [
            `Run ${run}${code}\nnext ${longer} $$ tail${run} then $$x_1 *c*$$`,
            `> Run ${run}${code}\n> next ${longer} $$ tail${run} then $$x_1 *c*$$`,
            `- Run ${run}${code}\n  next ${longer} $$ tail${run} then $$x_1 *c*$$`,
            `Run ${run}${code}\nnext ${longer} $$ tail${run} then $$x_1 *c*$$\n---`,
        ]) {
            const html = renderChatMarkdown(source);
            assert.equal(html, parked(markedAlone(source)), source);
            assert.ok(html.includes(`<code>echo $$ ${shorter} &lt;https://x.test/?q=1&gt; &amp;amp; `
                + `![i](https://x.test/i.png) next ${longer} $$ tail</code> then $$x_1 *c*$$`), source);
            assert.doesNotMatch(html, /<a\b|<img|<em>|OUROBOROSLATEX/, source);
        }
    }
});

test('a code span ends with its paragraph, list item or table row, so math after an open run is parked', () => {
    for (const source of [
        'a `x $$y_1 *c*$$\n\nb` z',
        '- a `x $$y_1 *c*$$\n- b` z',
        '> a `x $$y_1 *c*$$\n>\n> b` z',
        '| a | `x $$y_1 *c*$$ |\n| - | - |\n| b` | z |',
    ]) {
        const html = renderChatMarkdown(source);
        assert.equal(html, parked(markedAlone(source)), source);
        assert.doesNotMatch(html, /<code>|<em>/, source);
    }
});

test('an escaped backtick opens no span, and math that is a ``` line\'s last backtick stays in view', () => {
    assert.equal(renderChatMarkdown('\\``a $$` then $$x_1 *c*$$'), '<p>`<code>a $$</code> then $$x_1 *c*$$</p>\n');
    assert.equal(renderChatMarkdown('\\\\`a $$\nb` then $$x_1 *c*$$'), '<p>\\<code>a $$ b</code> then $$x_1 *c*$$</p>\n');
    // marked reads the first line as text: parking its `$$`$$` away would open a fence over the rest.
    assert.equal(renderChatMarkdown('``` $$`$$\n**bold** $$x_1 *c*$$'),
        '<p>``` $$`$$<br><strong>bold</strong> $$x_1 *c*$$</p>\n');
});

test('only a ``` that could open a fence keeps its line\'s math in view', () => {
    for (const [source, html] of [
        // It leads the line marked reads, after a quote's or list's marks or a code span's end.
        ['> ``` $$`$$\n> **bold** $$x_1 *c*$$',
            '<blockquote>\n<p>``` $$`$$<br><strong>bold</strong> $$x_1 *c*$$</p>\n</blockquote>\n'],
        ['1) ``` $$a$$ \\[`\\]\n   **bold** $$x_1 *c*$$',
            '<ol>\n<li>``` $$a$$ \\[`\\]<br><strong>bold</strong> $$x_1 *c*$$</li>\n</ol>\n'],
        ['x ```a\n``` $$`$$\n**bold** $$x_1 *c*$$',
            '<p>x <code>a </code> $$`$$<br><strong>bold</strong> $$x_1 *c*$$</p>\n'],
        // Anywhere else it is text however the math is parked: mid-line, after a code span or
        // parked math, or in a task box's text, which marked reads inline.
        ['Mention ``` literally; compute $$x_1 *c* + \\text{`q\'}$$',
            '<p>Mention ``` literally; compute $$x_1 *c* + \\text{`q&#39;}$$</p>\n'],
        ['`a` ``` $$x_1 *c* `$$', '<p><code>a</code> ``` $$x_1 *c* `$$</p>\n'],
        ['$$a\n``` b$$ $$x_1 *c* `$$', '<p>$$a\n``` b$$ $$x_1 *c* `$$</p>\n'],
        ['- [ ] ``` \\[x_1 *c* `\\]',
            '<ul>\n<li><input disabled="" type="checkbox"> ``` \\[x_1 *c* `\\]</li>\n</ul>\n'],
    ]) {
        assert.equal(renderChatMarkdown(source), html, source);
    }
});
