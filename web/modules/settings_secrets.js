import { apiClient } from './api_client.js';

// Controllers belong to mounted fields. The disclosed bytes live only in their
// text node, never in a Settings draft, applied snapshot or credential cache.
const reveals = new WeakMap();

export function resetSecretReveals(root) {
    root.querySelectorAll('[data-secret-reveal]').forEach((input) => reveals.get(input)?.reset());
}

export function bindSecretReveal(input, button, { savedSelector, savedLabel = () => '', identityInputs = [] } = {}) {
    const existing = reveals.get(input);
    if (existing) return existing;
    const row = input.closest('.secret-input-row');
    const doc = input.ownerDocument;
    const preview = doc.createElement('div');
    preview.id = `${input.id}-reveal`;
    preview.className = 'settings-secret-value ui-control';
    preview.tabIndex = 0;
    preview.setAttribute('role', 'region');
    const label = input.labels?.[0];
    if (label) {
        label.id ||= `${input.id}-label`;
        preview.setAttribute('aria-labelledby', label.id);
        button.setAttribute('aria-describedby', label.id);
    } else {
        preview.setAttribute('aria-label', input.getAttribute('aria-label') || 'Secret value');
    }
    const source = doc.createElement('div');
    source.id = `${input.id}-reveal-source`;
    source.className = 'settings-secret-source ui-field-help';
    const status = doc.createElement('div');
    status.className = 'settings-secret-status settings-inline-status';
    status.dataset.secretRevealStatus = '1';
    status.setAttribute('role', 'status');
    row.append(source, preview, status);
    button.setAttribute('aria-controls', preview.id);
    input.dataset.secretReveal = '1';
    let generation = 0;
    let open = false;

    function reset() {
        generation += 1;
        open = false;
        preview.textContent = '';
        preview.hidden = true;
        source.textContent = '';
        source.hidden = true;
        preview.removeAttribute('aria-describedby');
        status.textContent = '';
        status.hidden = true;
        button.textContent = 'Show';
        button.setAttribute('aria-expanded', 'false');
        button.removeAttribute('aria-busy');
    }

    async function toggle() {
        if (open) { reset(); return; }
        reset();
        open = true;
        const request = generation;
        const value = input.value;
        const applied = input.dataset.appliedValue;
        const identities = identityInputs.map((node) => node.value);
        const current = () => open && generation === request && input.isConnected
            && value === input.value && applied === input.dataset.appliedValue
            && identityInputs.every((node, index) => node.value === identities[index]);
        button.textContent = 'Hide';
        try {
            let disclosed = value;
            if (value && value === applied && input.dataset.forceClear !== '1') {
                const selector = savedSelector?.();
                if (!selector) throw new Error('The saved value has no lookup identity. Reload Settings or enter a new value.');
                const sourceLabel = savedLabel();
                button.setAttribute('aria-busy', 'true');
                status.textContent = 'Loading…';
                status.dataset.tone = 'muted';
                status.hidden = false;
                const response = await apiClient.revealSettingsSecret(selector);
                if (!current()) return;
                if (typeof response?.value !== 'string') throw new Error('The server did not return a secret value.');
                disclosed = response.value;
                source.textContent = sourceLabel;
                source.hidden = !sourceLabel;
                if (sourceLabel) preview.setAttribute('aria-describedby', source.id);
            }
            if (!current()) return;
            preview.textContent = disclosed;
            preview.hidden = false;
            status.textContent = '';
            status.hidden = true;
            button.setAttribute('aria-expanded', 'true');
        } catch (error) {
            if (!current()) return;
            reset();
            status.textContent = `Could not show this value: ${error.message || error}`;
            status.dataset.tone = 'danger';
            status.hidden = false;
        } finally {
            if (generation === request) button.removeAttribute('aria-busy');
        }
    }

    input.addEventListener('input', reset);
    identityInputs.forEach((node) => node.addEventListener('input', reset));
    button.addEventListener('click', toggle);
    const controller = { reset };
    reveals.set(input, controller);
    reset();
    return controller;
}
