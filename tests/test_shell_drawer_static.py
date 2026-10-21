"""The phone drawer (docs/DESIGN.md "Shell", plan P4/P5, owner Q1=A): the same sidebar is a layer that lies under
the room, and opening pushes the room aside as a card (owner 19.10, 1A of the iOS board), three quarters of the
screen; its head row shows the one × exactly where a page header's ☰ is; a room (Main, an open Project) carries its ☰
as the composer row's first control instead (owner 19.10, 1A), and the × stays in the drawer's head (2A); the closed
drawer is inert and out of the tab order; there is no empty 64px band."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_drawer_is_a_layer_with_one_close_control_where_the_menu_button_was():
    css = _read("web/style.css")
    html = _read("web/index.html")
    app_js = _read("web/app.js")
    drawer = css.split('html[data-layout="stack"] #primary-sidebar {', 1)[1].split("}", 1)[0]
    assert "width: var(--drawer-w);" in drawer, drawer
    assert "--drawer-w: min(75vw, 360px);" in css.split('html[data-layout="stack"] {', 1)[1].split("}", 1)[0]
    assert "position: fixed" in drawer
    # The head row holds the × at the header's inset; there is no empty top band any more.
    assert "padding-top: calc(64px" not in css
    head = css.split('html[data-layout="stack"] .drawer-head {', 1)[1].split("}", 1)[0]
    assert "display: flex" in head and "var(--inset-top)" in head and "var(--chrome-inset)" in head and "min-height: 56px" in head
    assert ".mobile-nav-toggle,\n.drawer-head {\n    display: none;" in css, "docked: no drawer head"
    assert 'id="nav-drawer-close"' in html and 'aria-controls="primary-sidebar"' in html
    # The head row comes first in the head zone; app.js appends Main's row after it (pages.js).
    head = html.split('class="sidebar-head"', 1)[1].split('id="nav-projects"', 1)[0]
    assert 'class="drawer-head"' in head and "rendered from the page registry" in head
    assert "querySelector('.sidebar-head')?.insertAdjacentHTML('beforeend', renderNavRows('head'))" in app_js
    assert "navDrawerClose.innerHTML = SHELL_ICONS.close;" in app_js
    assert "navDrawerClose.addEventListener('click', () => setMobileDrawerOpen(false));" in app_js
    # A room's ☰ is its composer's first control; its header opts out; every other page keeps ☰ in its header.
    assert "body.project-panel-open .mobile-nav-toggle" not in css
    chat_js = _read("web/modules/chat.js")
    row = _read("web/modules/composer.js").split('<div class="chat-toolbar-row">', 1)[1]  # the room's composer markup
    assert row.lstrip().startswith("${renderMobileNavToggle({ inComposer: true })}"), "the composer row starts with ☰"
    assert "showMobileNav: false,  // a room's ☰ is its composer row's first control" in chat_js
    assert "showMobileNav: false," in app_js.split("projectPanel?.insertAdjacentHTML('afterbegin', renderPageHeader({", 1)[1].split("}));", 1)[0]
    composer = css.split('html[data-layout="stack"] .chat-toolbar-row > .composer-menu {', 1)[1].split("}", 1)[0]
    assert "height: var(--composer-control-height);" in composer, "as tall as the capsules beside it"
    # The closed drawer is inert in the stack layout; the scrim stays live under the modal layer.
    assert "primarySidebar.inert = isStackLayout() && !drawerOpen;" in app_js
    assert "live: [navDrawerBackdrop].filter(Boolean)" in app_js
    # The keyboard is released before the drawer opens, as before.
    assert "if (nextOpen) releaseMobileKeyboardForDrawer();" in app_js
    # The scrim is a plain shade: no blur behind a moving layer.
    scrim = css.split('html[data-layout="stack"] .nav-drawer-backdrop {', 1)[1].split("}", 1)[0]
    assert "backdrop-filter" not in scrim


def test_the_room_is_the_card_the_drawer_pushes_aside():
    """Owner 19.10 (the iOS board, 1A): opening slides the room (Main, or a Project open over it) right by the drawer's
    width as a card and brings the drawer up from under it; nothing darkens; the quarter left showing is the way back."""
    css = _read("web/style.css")
    app_js = _read("web/app.js")
    drawer = css.split('html[data-layout="stack"] #primary-sidebar {', 1)[1].split("}", 1)[0]
    assert "z-index: var(--z-sidebar);" in drawer and "transform: translateX(-16%);" in drawer, "under the room, a step to the left"
    room = css.split('html[data-layout="stack"] #content {', 1)[1].split("}", 1)[0]
    assert "z-index: var(--z-room);" in room and "background: var(--bg-primary);" in room, "the opaque card over the drawer"
    card = css.split('html[data-layout="stack"] body.nav-drawer-open :is(#content, .project-panel.open) {', 1)[1].split("}", 1)[0]
    assert "transform: translateX(var(--drawer-w)) scale(0.96);" in card and "border-radius: var(--room-card-radius);" in card
    dragged = css.split('html[data-layout="stack"] body :is(#content, .project-panel.open).dragging {', 1)[1].split("}", 1)[0]
    assert "var(--drawer-p, 0) * var(--drawer-w)" in dragged, "the card follows the finger by the drawer's number"
    # As specific as the open card's and the open veil's rules, and after them: a finger on the open drawer moves both.
    for opened, held in (('body.nav-drawer-open :is(#content, .project-panel.open) {',
                          'body :is(#content, .project-panel.open).dragging {'),
                         ('body.nav-drawer-open .room-veil {', 'body .room-veil.dragging {')):
        assert css.index(f'html[data-layout="stack"] {opened}') < css.index(f'html[data-layout="stack"] {held}'), held
    scrim = css.split('html[data-layout="stack"] .nav-drawer-backdrop {', 1)[1].split("}", 1)[0]
    assert "inset: 0 0 0 var(--drawer-w);" in scrim and "background: transparent;" in scrim, "a clear way back over the quarter"
    assert "rgba(0, 0, 0, 0.48)" not in scrim
    project_scrim = css.split('html[data-layout="stack"] .project-panel-backdrop {', 1)[1].split("}", 1)[0]
    assert "display: none;" in project_scrim, "a Project's scrim never lies over the drawer"
    veil = css.split('html[data-layout="stack"] body .room-veil.dragging {', 1)[1].split("}", 1)[0]
    assert "var(--drawer-p, 0)" in veil, "the veil fades the words by the same number"
    # The finger moves the rooms and their veils with the drawer (drawer_drag.js `also`).
    assert "also: movingRooms," in app_js and "veil.className = 'room-veil';" in app_js
