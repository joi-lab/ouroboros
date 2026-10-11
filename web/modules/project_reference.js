/**
 * The one way the UI points at a Project (docs/DESIGN.md "References and actions").
 *
 * Every place that says "this belongs to that Project — take me there" gets its
 * control here, and only this module raises `ouro:open-project`. Callers choose a
 * LAYOUT and, where the surface needs it, a STATE; they never write a label, a
 * glyph or a class name, because that freedom is how one intent came to be drawn
 * several ways while each call still "reused the shared chip".
 *
 *   inline  the pill under a message or a System row, and in a quiz card's head
 *   bar     the whole converted task card
 *   footer  the strip that closes a task card bound to a Project
 */
import { PAGE_ICONS } from './page_icons.js';
import { tr } from './i18n.js';

const LAYOUT_CLASS = { inline: 'chat-quiz-project', bar: '', footer: 'chat-live-bound-pointer' };
// The arrow alone says "opens"; words appear only when the surface is itself the news.
const STATE_WORDS = { '': '', background: 'running in background' };

// What a press opens, per mounted reference: a name that arrives later reaches the payload too.
const targets = new WeakMap();

// A row that was never named carries its minted id as the name. Only that shape is a non-name:
// an owner's own `blog` has the id `blog` too, and it IS the name.
const MINTED_ID = /^proj_[0-9a-f]+$/;
function displayName(project) {
    const name = String(project?.name || '').trim();
    return name && !(name === String(project?.id || '') && MINTED_ID.test(name)) ? name : tr('project.reference.generic', 'Project');
}

/** One name for the visible text, the tooltip and the accessible name, so they cannot disagree. */
export function nameProjectReference(node, project) {
    const name = displayName(project);
    const target = targets.get(node);
    if (target && project?.name) target.name = project.name;
    const label = node.querySelector('.chat-live-project-name');
    if (label && label.textContent !== name) label.textContent = name;
    const words = STATE_WORDS[node.dataset.state || ''] || '';
    const base = node.dataset.opensQuestion ? `Open this question in ${name}` : `Open project ${name}`;
    const spoken = words ? `${base}, ${words}` : base;
    node.title = spoken;
    node.setAttribute('aria-label', spoken);
    return node;
}

export function projectReference(project, { layout = 'inline', state = '', taskId = '', quizId = '' } = {}) {
    const target = { ...(project || {}) };
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = ['chat-live-project-card-btn', LAYOUT_CLASS[layout] || ''].filter(Boolean).join(' ');
    btn.dataset.intent = 'open-project';
    if (STATE_WORDS[state]) btn.dataset.state = state;
    if (quizId) btn.dataset.opensQuestion = '1';
    const icon = document.createElement('span');
    icon.className = 'chat-live-project-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = PAGE_ICONS.projects;
    const nameEl = document.createElement('span');
    nameEl.className = 'chat-live-project-name';
    const statusEl = document.createElement('span');
    statusEl.className = 'chat-live-project-status';
    statusEl.setAttribute('aria-hidden', 'true');
    statusEl.textContent = [STATE_WORDS[state] || '', '↗'].filter(Boolean).join(' ');
    btn.append(icon, nameEl, statusEl);
    targets.set(btn, target);
    nameProjectReference(btn, target);
    btn.addEventListener('click', () => window.dispatchEvent(new CustomEvent('ouro:open-project', {
        detail: { project: { ...target }, task_id: String(taskId || ''), quiz_id: String(quizId || '') },
    })));
    return btn;
}
