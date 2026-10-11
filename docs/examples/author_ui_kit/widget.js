/* Author-owned application; the kit supplies only controls and their styling. */
(async () => {
    const root = document.getElementById('root');
    const embedded = document.getElementById('author-kit-source');
    let themeOff = () => {};
    const dispose = () => { themeOff(); themeOff = () => {}; };
    if (typeof window.OuroborosWidget?.onTheme === 'function') {
        themeOff = window.OuroborosWidget.onTheme((theme) => {
            document.documentElement.dataset.theme = theme;
        });
    }
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(dispose);
    }
    // Author layout and readable native colors also work before the kit loads.
    const style = document.createElement('style');
    if (root.dataset.styleNonce) style.nonce = root.dataset.styleNonce;
    const layout = '#root { padding: 16px; color: CanvasText; background: Canvas; }';
    style.textContent = layout;
    document.head.append(style);
    root.innerHTML = '<p role="status">Loading controls…</p>';
    try {
        let source;
        if (embedded) source = JSON.parse(embedded.textContent);
        else {
            const response = await OuroborosWidget.fetch('/api/extensions/author_ui_kit/author-kit');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            source = await response.json();
        }
        style.textContent = source.css + '\n' + layout;
        const url = URL.createObjectURL(new Blob([source.javascript], { type: 'text/javascript' }));
        let kit;
        try { kit = await import(url); }
        finally { URL.revokeObjectURL(url); }

        const fields = [
            { name: 'title', label: 'Title', help: 'A name for this example.', default: 'My notes' },
            { name: 'view', label: 'View', type: 'select', options: ['List', 'Grid'], default: 'List' },
            { name: 'enabled', label: 'Enabled', type: 'checkbox', default: true },
        ];
        root.classList.add('ouro-ui');
        root.innerHTML = '<h2>Author controls</h2><form>'
            + fields.map(field => kit.renderSafeField(field)).join('')
            + '<button class="btn btn-default" type="button">Preview</button>'
            + '<p class="ui-status" role="status" aria-live="polite"></p></form>';
        const form = root.querySelector('form');
        form.querySelector('button').addEventListener('click', () => {
            const values = kit.collectSafeFieldValues(form, fields);
            kit.setInlineStatus(form.querySelector('[role="status"]'),
                values.title ? `${values.title}: ${values.view}, ${values.enabled ? 'enabled' : 'disabled'}` : 'Enter a title.',
                values.title ? 'ok' : 'danger');
        });
    } catch (error) {
        root.querySelector('[role="status"]').textContent = `Controls unavailable: ${error.message}`;
    }
})();
