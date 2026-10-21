"""Static guards for the appearance pass: the facts a browser cannot show.

Three of the four claims here are invisible to the Playwright suite:

* ``private_mode=False`` decides whether the DESKTOP shell keeps localStorage
  across a full quit-and-reopen. The browser tests drive a browser, and a server
  restart never reproduced the loss — only closing the app did. This source guard verifies the flag, not
  actual retention: the rebuilt desktop launcher still needs a cold-launch test.
* a colour baked into a ``data:`` URI cannot be themed, because a CSS custom
  property cannot be interpolated into the URI string. The fix is to make the
  whole image a token, which is a structural fact about the stylesheet.
* the appearance choice is client-local. ``web/modules/settings.js`` collects
  ``input[id^="s-"]`` into the ``/api/settings`` payload, so the guarantee
  "Appearance never writes runtime settings" rests on the panel owning no such
  input. The browser suite asserts the consequence; this asserts the cause.

Pattern follows ``tests/test_web_dialogs_static.py``: read the sources, assert
the structural fact, no browser needed.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "web"

# Every first-party window must explicitly request persistent storage.
WEBVIEW_HOSTS = ("launcher.py", "ouroboros/launcher_onboarding.py")


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _assert_persistent_webview_starts(source: str, rel: str) -> int:
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "webview" and node.func.attr == "start"]
    for call in calls:
        assert any(keyword.arg == "private_mode" and isinstance(keyword.value, ast.Constant)
                   and keyword.value.value is False for keyword in call.keywords), (
            f"{rel}:{call.lineno}: webview.start() must explicitly pass private_mode=False; "
            "a private WebView discards the appearance choice when the window closes"
        )
    return len(calls)


def test_every_first_party_window_uses_a_persistent_profile():
    calls = sum(_assert_persistent_webview_starts(_read(rel), rel) for rel in WEBVIEW_HOSTS)
    # The count is asserted so that a NEW window added without the flag fails
    # here rather than silently using the library default.
    assert calls == 5, f"expected 5 first-party webview.start() calls, found {calls}"


def test_persistent_profile_guard_reads_multiline_calls_and_ignores_non_calls():
    source = '''
# webview.start()
example = "webview.start(private_mode=True)"
other.start(private_mode=True)
webview.start(
    debug=False,
    private_mode=False,
    menu=build_menu(),
)
'''
    assert _assert_persistent_webview_starts(source, "fixture.py") == 1


@pytest.mark.parametrize("argument", ["", "private_mode=True", "private_mode=0"])
def test_persistent_profile_guard_rejects_missing_or_non_false_argument(argument):
    with pytest.raises(AssertionError, match="must explicitly pass private_mode=False"):
        _assert_persistent_webview_starts(f"webview.start(\n    {argument}\n)", "fixture.py")


def test_the_select_arrow_is_a_theme_token_not_a_baked_colour():
    ui = _read("web/ui.css")
    dark, light = ui.split(':root[data-theme="light"]', 1)
    assert "--select-arrow:" in dark and "--select-arrow:" in light
    # Two DIFFERENT inks: the pale arrow on a white field was the reported defect.
    dark_ink = re.search(r"--select-arrow:.*?stroke='(%23[0-9a-fA-F]{6})'", dark).group(1)
    light_ink = re.search(r"--select-arrow:.*?stroke='(%23[0-9a-fA-F]{6})'", light).group(1)
    assert dark_ink.lower() != light_ink.lower()
    for rel in ("web/ui.css", "web/settings.css", "web/style.css", "web/onboarding.css"):
        text = _read(rel)
        baked = [line for line in text.splitlines()
                 if "data:image/svg+xml" in line and "--select-arrow:" not in line]
        assert not baked, f"{rel}: inline SVG carries a fixed colour: {baked}"


def test_selected_advisory_reads_like_every_other_setting_value():
    """A setting's value is never amber: the selected Advisory takes the shared selected look of a chosen option,
    the themed soft crimson pair, so the old ~2:1 amber wash on the light surface cannot come back."""
    css = _read("web/settings.css")
    assert not re.search(r'advisory"\][^{]*\{', css), "Advisory has a look of its own again"
    selected = re.search(r':root\[data-theme\] \.settings-effort-btn\.active,?[^{]*\{([^}]*)\}', css)
    assert selected, "the shared selected-value rule is gone"
    assert "var(--tone-crimson-soft)" in selected.group(1) and "var(--tone-crimson-soft-ink)" in selected.group(1)
    assert ".wizard-choice.advisory" not in _read("web/onboarding.css")


def test_appearance_is_a_named_destination_that_never_reaches_the_server():
    ui = _read("web/modules/settings_ui.js")
    assert re.search(r"value:\s*'appearance'", ui), "Appearance needs its own tab"
    panel = re.search(
        r'data-settings-panel="appearance"(.*?)</section>', ui, re.S).group(1)
    assert "data-theme-control" in panel
    # An id starting with s- would be swept into the /api/settings payload.
    assert not re.search(r'\bid="s-[\w-]*"', panel.replace('id="s-appearance-theme-label"', '')), \
        "a settings-collected input would POST the client-local appearance choice"
    # Appearance now owns a second client-local block (notifications), so the
    # guard is stated as its own intent instead of "no control at all": every
    # control here must lack the ONE handle the collector reads.
    for control in re.finditer(r"<(?:input|select)\b[^>]*>", panel):
        assert not re.search(r'\bid="s-', control.group(0)), (
            f"{control.group(0)} would be swept into the /api/settings payload"
        )
    collector = _read("web/modules/settings.js")
    assert 'input[id^="s-"]' in collector, "the collector moved; re-check this guard"


def test_the_onboarding_wizard_is_no_longer_pinned_to_the_viewport():
    # body{min-height:100vh;padding:16px} plus a shell pinned to
    # calc(100vh - 32px) exactly fills the window, so ANY control above #root
    # overflowed by its own height.
    css = _read("web/onboarding.css")
    assert "calc(100vh - 32px)" not in css
    assert ".onboarding-appearance" in css
    template = _read("web/onboarding_template.html")
    assert "data-theme-control" in template
    assert "data-theme-toggle" not in template


def test_status_text_uses_foreground_roles_not_raw_hues():
    # Raw hues remain valid for fills/borders, never small status text.
    for rel in ('web/style.css', 'web/settings.css'):
        assert not re.search(r'(?:^|[;{])\s*color:\s*var\(--(?:amber|red|green|blue)\)',
                             _read(rel), re.M), rel


def test_chat_palette_redraw_is_local_viewport_work():
    chat = _read('web/modules/chat.js')
    mount = chat.split('function enhanceMountedMarkdown(root)', 1)[1].split('const {', 1)[0]
    assert 'onThemeDomWrite: foldedWrite(withStableViewport)' in mount
    assert 'const foldedWrite = writer => (mutate, options) => writer(() => {' in mount
    assert mount.index('const changed = mutate();') < mount.index('refreshProjectAnswerFold(') < mount.index('return changed;')
    assert 'refreshProjectAnswerFold(root.closest?.(\'.project-answer\'))' in mount


def test_surface_sheets_carry_no_near_white_ink():
    """Light is only as complete as the last hard-coded near-white literal.

    `rgba(250, 250, 250, .1)` is a border on the Dark shell and nothing at all on the Light one;
    near-white text is simply unreadable there. Such ink goes through the palette channels
    (`rgba(var(--neutral-rgb), a)`) or a foreground role; only `web/ui.css`, the token sheet,
    may name a literal, because it declares both palettes side by side.
    """
    near_white = re.compile(r"rgba\(\s*(2[0-5]\d)\s*,\s*(2[0-5]\d)\s*,\s*(2[0-5]\d)\s*,")
    leftovers = [
        f"{sheet.relative_to(REPO_ROOT)}:{number}: {line.strip()}"
        for sheet in sorted(WEB.glob("*.css")) if sheet.name != "ui.css"
        for number, line in enumerate(sheet.read_text(encoding="utf-8").splitlines(), 1)
        if near_white.search(line)
    ]
    assert not leftovers, "near-white literals outside the token sheet:\n" + "\n".join(leftovers)


def test_an_old_desktop_app_is_named_where_its_choices_are_lost():
    """In-app updates replace the core, never the frozen launcher that decides WebView storage, so
    the Appearance panel carries the line ``desktop_shell.js`` fills for an app that erases it."""
    panel = re.search(
        r'data-settings-panel="appearance"(.*?)</section>', _read("web/modules/settings_ui.js"), re.S).group(1)
    assert "data-shell-storage-status" in panel
    settings = _read("web/modules/settings.js")
    assert "mountDesktopShell(page" in settings and "disposeShell()" in settings
    shell = _read("web/modules/desktop_shell.js")
    assert "PERSISTENT_STORAGE_SINCE = Object.freeze([7, 2, 0])" in shell, (
        "7.2.0 is the first release whose launcher passes private_mode=False (git tag --contains e30957143)")
