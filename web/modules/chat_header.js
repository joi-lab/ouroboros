/** Main's header actions, declared once (docs/DESIGN.md §10 "Page header"): Restart, Panic, the More menu
 * (Consciousness, Evolve, Review), the budget capsule and the room status. page_header.js
 * `bindHeaderControlPlacement` moves the same Restart and budget nodes into ⋯ when the bar is narrow. */
import { SHELL_ICONS } from './page_icons.js';

export function mainHeaderActionsHtml() {
    return `
                <div class="chat-header-actions" id="chat-header-actions">
                    <button class="chat-header-btn ui-chrome-control" type="button" data-chat-command="restart" title="Restart agent">Restart</button>
                    <button class="chat-header-btn danger ui-chrome-control" type="button" data-chat-command="panic" title="Stop all workers">Panic</button>
                    <details class="chat-header-more">
                        <summary class="chat-header-btn ui-chrome-control" title="More agent controls"><span class="chat-header-more-word">More</span><span class="chat-header-more-glyph" aria-hidden="true">${SHELL_ICONS.more}</span></summary>
                        <div class="chat-header-menu">
                            <button class="chat-header-menu-item" type="button" data-chat-command="bg">Consciousness</button>
                            <button class="chat-header-menu-item" type="button" data-chat-command="evolve">Evolve</button>
                            <button class="chat-header-menu-item" type="button" data-chat-command="review" title="Run review now">Review</button>
                        </div>
                    </details>
                </div>
                <button class="chat-budget-pill ui-chrome-control" id="chat-budget-pill" type="button" title="Open budget controls" aria-label="Open budget controls">
                    <span class="chat-budget-text" id="chat-budget-text">Loading…</span>
                    <div class="chat-budget-bar">
                        <div class="chat-budget-bar-fill" id="chat-budget-bar-fill"></div>
                    </div>
                </button>
                <span id="chat-status" class="status-badge offline">Connecting…</span>
            `;
}
