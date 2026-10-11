// Editable suggestions shared by the product's model editors. The input is
// the draft authority; catalogs enrich its list and never assign a value.
import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { bindPopoverPosition } from './ui_interactions.js';

const choosers = new WeakMap();

export function modelOptions(items = []) {
    const seen = new Set();
    return items.map((item) => typeof item === 'string' ? { value: item, label: item }
        : { value: String(item.value ?? item.id ?? ''), label: String(item.label || item.name || item.value || item.id || '') })
        .filter((item) => !seen.has(item.value) && seen.add(item.value));
}

export function matchingModelOptions(items, query) {
    const search = String(query || '').trim().toLocaleLowerCase();
    return modelOptions(items).filter(({ value, label }) => !search
        || value.toLocaleLowerCase().includes(search) || label.toLocaleLowerCase().includes(search));
}

function optionsHtml(items, listId) {
    return modelOptions(items).map(({ value, label }, index) =>
        `<div class="ui-model-option" role="option" id="${escapeHtml(listId)}-${index}"
            data-model-value="${escapeHtml(value)}" aria-selected="false">${escapeHtml(label || value)}</div>`).join('');
}

export function modelChooserHtml(attrs, value, listId, items = [], { placeholder = 'Choose a model' } = {}) {
    return `<span class="ui-model-chooser"><input class="ui-control" type="text" ${attrs}
        data-model-chooser role="combobox" aria-autocomplete="list" aria-expanded="false"
        aria-controls="${escapeHtml(listId)}" value="${escapeHtml(value)}"
        placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
        <div id="${escapeHtml(listId)}" class="ui-popup ui-model-options" role="listbox" hidden>${optionsHtml(items, listId)}</div></span>`;
}

function readOptions(list) {
    return [...list.querySelectorAll('[data-model-value]')].map((node) => ({
        value: node.dataset.modelValue, label: node.textContent,
    }));
}

export function updateModelChooser(input, items) {
    choosers.get(input)?.update(items);
}

/** Bind once per real input; dispose before its owner replaces or removes it. */
export function bindModelChooser(input) {
    if (!input || choosers.has(input)) return () => {};
    const doc = input.ownerDocument;
    const list = doc.getElementById(input.getAttribute('aria-controls'));
    if (!list) return () => {};
    const owner = list.parentElement;
    list.setAttribute('aria-label', input.getAttribute('aria-label') || 'Models');
    let items = readOptions(list);
    let visible = [];
    let active = -1;
    let composing = false;
    let opened = false;
    let position = null;
    const disposers = [];
    const listen = (target, type, listener) => {
        target.addEventListener(type, listener);
        disposers.push(() => target.removeEventListener(type, listener));
    };

    function highlight(index) {
        active = index;
        [...list.querySelectorAll('[role="option"]')].forEach((node, ordinal) => {
            node.setAttribute('aria-selected', String(ordinal === active));
            if (ordinal === active) {
                input.setAttribute('aria-activedescendant', node.id);
                node.scrollIntoView({ block: 'nearest' });
            }
        });
        if (active < 0) input.removeAttribute('aria-activedescendant');
    }

    function render() {
        const selected = visible[active]?.value;
        visible = matchingModelOptions(items, input.value);
        list.innerHTML = optionsHtml(visible, list.id)
            || '<div class="ui-model-empty">No matching suggestions. You can keep this model ID.</div>';
        highlight(selected === undefined ? -1 : visible.findIndex((item) => item.value === selected));
        position?.reposition();
    }

    function close() {
        opened = false;
        list.hidden = true;
        input.setAttribute('aria-expanded', 'false');
        input.removeAttribute('aria-activedescendant');
        active = -1;
        position?.destroy(); position = null;
        if (list.parentElement !== owner) owner.appendChild(list);
        doc.removeEventListener('pointerdown', outside);
        doc.defaultView.removeEventListener('blur', close);
    }

    function open() {
        if (input.disabled) return;
        opened = true;
        list.hidden = false;
        if (list.parentElement !== doc.body) doc.body.appendChild(list);
        input.setAttribute('aria-expanded', 'true');
        render();
        if (!position) position = bindPopoverPosition(list, { anchor: input });
        doc.addEventListener('pointerdown', outside);
        doc.defaultView.addEventListener('blur', close);
    }

    function outside(event) {
        if (event.target !== input && !list.contains(event.target)) close();
    }

    function choose(value) {
        input.value = value;
        // Existing domain input handlers retain serialization and source pins.
        input.dispatchEvent(new doc.defaultView.Event('input', { bubbles: true }));
        input.dispatchEvent(new doc.defaultView.Event('change', { bubbles: true }));
        close();
        input.focus({ preventScroll: true });
    }

    listen(input, 'focus', open);
    listen(input, 'click', () => { if (!opened) open(); });
    listen(input, 'input', () => { if (!composing) open(); });
    listen(input, 'compositionstart', () => { composing = true; });
    listen(input, 'compositionend', () => { composing = false; open(); });
    listen(input, 'blur', close);
    listen(input, 'keydown', (event) => {
        if (composing || event.isComposing) return;
        if (event.key === 'Escape' && opened) {
            event.preventDefault(); event.stopPropagation(); close();
        } else if (event.key === 'Tab') close();
        else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            if (!opened) open();
            const delta = event.key === 'ArrowDown' ? 1 : -1;
            highlight(visible.length ? (active < 0 ? (delta > 0 ? 0 : visible.length - 1)
                : (active + delta + visible.length) % visible.length) : -1);
        } else if (event.key === 'Enter' && opened && active >= 0) {
            event.preventDefault(); choose(visible[active].value);
        }
    });
    // Keep focus/caret in the editable input; a touch scroll is still native.
    listen(list, 'mousedown', (event) => event.preventDefault());
    listen(list, 'click', (event) => {
        const option = event.target.closest('[data-model-value]');
        if (option && list.contains(option)) choose(option.dataset.modelValue);
    });
    choosers.set(input, { update(next) {
        items = modelOptions(next);
        if (opened && !composing) render();
    } });
    return () => {
        close();
        for (const dispose of disposers) dispose();
        choosers.delete(input);
    };
}

export function bindModelChoosers(container) {
    const disposers = [...(container?.querySelectorAll('[data-model-chooser]') || [])].map(bindModelChooser);
    return () => { for (const dispose of disposers) dispose(); };
}

/** Refresh only discovery-owned options; actual editable nodes stay mounted. */
export function updateModelChooserOptions(current, desired) {
    for (const input of current.querySelectorAll('[data-model-chooser]')) {
        const id = input.getAttribute('aria-controls');
        const list = [...desired.querySelectorAll('[role="listbox"]')].find((node) => node.id === id);
        if (list) updateModelChooser(input, readOptions(list));
    }
}
