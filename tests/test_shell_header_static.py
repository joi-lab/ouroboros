"""One header for every page and the Project panel (docs/DESIGN.md "Shell", plan P5, owner Q3=A, Q4=A).

Docked: no visible title where the sidebar names the page (the h2 stays), the Project keeps its name, and a
header with no actions renders no bar. Stack: one row of at most 56px, `☰ · title · status … Panic · ⋯`, the
title one line and ellipsized; Restart and the budget inside ⋯. No icon anywhere."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    assert selector in css, selector
    return css.split(selector, 1)[1].split("}", 1)[0]


def test_docked_header_hides_a_title_the_sidebar_names_and_an_empty_bar():
    css = _read("web/style.css")
    hidden = _rule(css, 'html[data-layout="docked"] .app-page-header[data-sidebar-names] .app-page-title {')
    assert "position: absolute" in hidden and "clip-path: inset(50%)" in hidden, "an accessible h2, not a removed one"
    assert "display: none" in _rule(css, 'html[data-layout="docked"] .app-page-header[data-empty] {')
    title = _rule(css, ".app-page-title {")
    assert "font-size: var(--type-section)" in title and "font-weight: 500" in title
    assert "white-space: nowrap" in title and "text-overflow: ellipsis" in title
    assert ".app-page-icon" not in css and "app-page-description" not in css
    assert ".app-page-lede" not in css, "a page has no caption line"


def test_stack_header_is_one_row_of_at_most_56px_with_restart_and_budget_inside_the_menu():
    css = _read("web/style.css")
    row = _rule(css, 'html[data-layout="stack"] .app-page-header {')
    # 44px controls plus the 5px chrome inset above and below: 54, held at 56 by the min-height.
    assert "min-height: 56px" in row and "padding: var(--chrome-inset) var(--space-3)" in row, row
    assert "var(--inset-top)" in row
    assert "flex-wrap: nowrap" in _rule(css, 'html[data-layout="stack"] .chat-page-header {')
    assert "flex-wrap: nowrap" in _rule(css, 'html[data-layout="stack"] .chat-header-actions {')
    summary = _rule(css, 'html[data-layout="stack"] .chat-header-more > summary {')
    assert "width: 44px" in summary and "height: 44px" in summary
    assert 'html[data-layout="stack"] .chat-header-more-word { display: none; }' in css
    assert '.chat-header-menu > .chat-budget-pill {' in css
    # The note is a floating sibling under the bar, never a second row of it; the move into ⋯ follows the
    # bar's measured width as well as the layout (page_header.js), so a narrow docked column keeps one row too.
    assert '.chat-page-note {' in css and "top: var(--chat-header-bar, 56px)" in _rule(css, ".chat-page-note {")
    assert "createHistoryControls(messagesDiv, pageNote || page.querySelector('.chat-panel-statusbar'))" in _read("web/modules/chat.js")
    header_js = _read("web/modules/page_header.js")
    assert "export const HEADER_FULL_ROW_MIN_WIDTH = 640;" in header_js
    assert "bar.clientWidth < HEADER_FULL_ROW_MIN_WIDTH" in header_js and "new Observer(place)" in header_js
    # The ☰ and the × share the shell's one icon-control recipe; the Project's × is that recipe too.
    assert 'class="shell-icon-control project-panel-close"' in _read("web/app.js")
    header_js = _read("web/modules/page_header.js")
    assert "'shell-icon-control mobile-nav-toggle composer-menu ui-hit-area' : 'shell-icon-control mobile-nav-toggle'" in header_js
    assert ".project-panel-title" not in css, "one title style"


def test_header_actions_take_the_chrome_height_under_a_finger():
    css = _read("web/style.css")
    gated = css.split("@media (pointer: coarse) {\n    .app-page-toolbar :is(.btn, button),", 1)[1].split("}", 1)[0]
    assert "min-height: var(--chrome-control-h)" in gated
    assert re.search(r"@media \(pointer: coarse\) \{[^}]*--chrome-control-h: var\(--touch-target-min\)", _read("web/ui.css"), re.S)


def test_the_place_name_wins_over_the_status_word_and_the_status_keeps_its_target():
    room_work = _read("web/room_work.css")
    trigger = _rule(room_work, ":is(.chat-page-header, .project-panel-bar) .app-page-title-row > .rw-trigger {")
    assert "flex: 0 1000 auto" in trigger, "the status yields a thousand times faster than the title"
    assert "min-width: var(--touch-target-min)" in trigger and "5ch" not in trigger
    title = _rule(room_work, ":is(.chat-page-header, .project-panel-bar) .app-page-title-row > .app-page-title {")
    assert "flex: 0 1 auto" in title and "min-width: 0" in title and "text-overflow: ellipsis" in title


def test_main_title_is_the_registry_label_and_the_panel_bar_keeps_its_hooks():
    chat_js = _read("web/modules/chat.js")
    assert "asPanel = false, title = pageLabel('chat')," in chat_js
    assert "PAGE_ICONS" not in chat_js
    # room_work.js still finds the Project bar and Main's header to seat the status after the title.
    room_work = _read("web/modules/room_work.js")
    assert "panel?.querySelector?.('.project-panel-bar')" in room_work
    assert "bar?.querySelector('.app-page-title-row')" in room_work


def test_the_status_word_clips_past_its_line_so_descenders_stay_whole():
    # The word ellipsizes (overflow hidden); its clip box reaches past the line and the margin gives the room back,
    # so the tail of g, p or y is never cut ("Working" lost its g).
    badge = _rule(_read("web/room_work.css"), ".rw-trigger .status-badge {")
    assert "overflow: hidden" in badge and "text-overflow: ellipsis" in badge
    assert "padding-block: 0.15em" in badge and "margin-block: -0.15em" in badge


def test_the_desktop_title_band_keeps_an_empty_header_as_a_plain_strip():
    css = _read("web/style.css")
    band = _rule(css, ':root[data-window-chrome="integrated"][data-layout="docked"] .app-page-header[data-empty] {')
    assert "display: block" in band and "height: var(--window-chrome-top)" in band and "padding: 0" in band
    # Outside the desktop window an empty header still takes no room.
    assert "display: none" in _rule(css, 'html[data-layout="docked"] .app-page-header[data-empty] {')
