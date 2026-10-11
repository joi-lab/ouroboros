/** Portable UI primitives shared by first-party forms and optional author pages.
 * No host, network, document-global or framework dependency. Callers own data,
 * validation and lifecycle; renderers escape their own HTML/attribute contexts.
 */

export function escapeHtmlAttr(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/`/g, '&#96;');
}

const TONES = new Set(['ok', 'danger', 'warn', 'muted', 'info']);
const TONE_ALIASES = Object.freeze({
    error: 'danger',
    success: 'ok',
    warning: 'warn',
    neutral: 'muted',
});
const SAFE_FIELD_TYPES = new Set(['text', 'number', 'url', 'email', 'password', 'textarea', 'select', 'checkbox']);

function safeFieldType(value) {
    const type = String(value || 'text').toLowerCase();
    return SAFE_FIELD_TYPES.has(type) ? type : 'text';
}

function safeNumericAttribute(name, value) {
    if (value === '' || value === null || value === undefined || !Number.isFinite(Number(value))) return '';
    return ` ${name}="${escapeHtmlAttr(value)}"`;
}

/** Render the narrow host-owned field contract shared by Widgets and Settings. */
export function renderSafeField(field = {}, savedValues = {}, options = {}) {
    const rawName = String(field.name || '');
    const name = escapeHtmlAttr(rawName);
    const label = escapeHtmlAttr(field.label || rawName);
    const type = safeFieldType(field.type);
    const accessible = ` aria-label="${label}"`
        + (field.help ? ` aria-description="${escapeHtmlAttr(field.help)}"` : '');
    const hasSaved = type !== 'password' && Object.prototype.hasOwnProperty.call(savedValues || {}, rawName);
    const saved = type === 'password' ? '' : (hasSaved ? savedValues[rawName] : field.default);
    const value = escapeHtmlAttr(saved ?? '');
    const placeholder = field.placeholder ? ` placeholder="${escapeHtmlAttr(field.placeholder)}"` : '';
    const required = field.required ? ' required' : '';
    const disabled = field.disabled || options.disabled ? ' disabled' : '';
    const fieldClass = escapeHtmlAttr(`ui-field ${options.fieldClass || 'widget-field'}`);
    const inlineClass = escapeHtmlAttr(`ui-field ui-field-inline ${options.inlineClass || `${options.fieldClass || 'widget-field'} widget-field-inline`}`);
    const helpClass = escapeHtmlAttr(`ui-field-help ${options.helpClass || 'widget-field-help'}`);
    const maxSpan = Math.max(1, Math.min(4, Number(options.maxSpan) || 4));
    const span = Math.max(1, Math.min(maxSpan, Number(field.span) || 1));
    const spanClass = options.spanClassPrefix ? ` ${escapeHtmlAttr(options.spanClassPrefix)}${span}` : '';
    const help = field.help ? `<small class="${helpClass}">${escapeHtmlAttr(field.help)}</small>` : '';
    if (type === 'textarea') {
        return `<label class="${fieldClass}${spanClass}"><span>${label}</span><textarea class="ui-control" name="${name}"${accessible}${placeholder}${required}${disabled}>${value}</textarea>${help}</label>`;
    }
    if (type === 'select') {
        const optionsHtml = (Array.isArray(field.options) ? field.options : []).map((option) => {
            const optionValue = typeof option === 'object' && option !== null ? option.value : option;
            const optionLabel = typeof option === 'object' && option !== null ? (option.label ?? option.value) : option;
            const selected = String(optionValue ?? '') === String(saved ?? '') ? ' selected' : '';
            return `<option value="${escapeHtmlAttr(optionValue ?? '')}"${selected}>${escapeHtmlAttr(optionLabel ?? '')}</option>`;
        }).join('');
        return `<label class="${fieldClass}${spanClass}"><span>${label}</span><select class="ui-control" name="${name}"${accessible}${required}${disabled}>${optionsHtml}</select>${help}</label>`;
    }
    if (type === 'checkbox') {
        return `<label class="${inlineClass}${spanClass}"><input class="ui-checkbox" type="checkbox" name="${name}"${accessible}${saved ? ' checked' : ''}${required}${disabled}> <span>${label}</span>${help}</label>`;
    }
    const numeric = type === 'number'
        ? `${safeNumericAttribute('min', field.min)}${safeNumericAttribute('max', field.max)}${safeNumericAttribute('step', field.step)}`
        : '';
    const autocomplete = type === 'password' ? ' autocomplete="new-password"' : '';
    return `<label class="${fieldClass}${spanClass}"><span>${label}</span><input class="ui-control" type="${type}" name="${name}"${accessible} value="${value}"${placeholder}${numeric}${required}${disabled}${autocomplete}>${help}</label>`;
}

/** Collect values according to the same closed field contract used for rendering. */
export function collectSafeFieldValues(form, fields = [], { includePasswords = true } = {}) {
    const values = {};
    for (const field of Array.isArray(fields) ? fields : []) {
        const name = String(field?.name || '');
        const type = safeFieldType(field?.type);
        if (!name || (type === 'password' && !includePasswords)) continue;
        const input = form?.elements?.namedItem
            ? form.elements.namedItem(name)
            : form?.elements?.[name];
        if (!input) continue;
        values[name] = type === 'checkbox' ? Boolean(input.checked) : input.value;
    }
    return values;
}

export function normalizeTone(tone = 'muted', fallback = 'muted') {
    const canonical = (value) => {
        const clean = String(value || '').toLowerCase();
        const normalized = TONE_ALIASES[clean] || clean;
        return TONES.has(normalized) ? normalized : '';
    };
    return canonical(tone) || canonical(fallback) || 'muted';
}

export function setInlineStatus(el, text, tone = 'muted') {
    if (!el) return;
    const next = text || '';
    if (el.textContent !== next) el.textContent = next;
    el.dataset.tone = normalizeTone(tone);
}
