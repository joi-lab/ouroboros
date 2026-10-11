"""Main/Project composer clearance and wizard checkbox geometry in real UI flows."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data
from tests.test_subscription_setup_browser import subscription_ui as subscription_ui
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET, _emit_ws_frame, _SETTLE_RESTORE_FRAMES


_MEASURE = """messages => {
    messages.scrollTop = messages.scrollHeight;
    const composer = messages.parentElement.querySelector('#chat-input-area, .chat-input-area');
    const last = [...messages.children].filter(el => el.getBoundingClientRect().height > 0).at(-1);
    const end = getComputedStyle(messages, '::after');
    return {height: messages.clientHeight, scrollHeight: messages.scrollHeight,
        scrollTop: messages.scrollTop, padding: getComputedStyle(messages).paddingBottom,
        spacer: end.content, basis: end.flexBasis, shrink: end.flexShrink,
        lastBottom: last?.getBoundingClientRect().bottom ?? null,
        composerTop: composer.getBoundingClientRect().top,
        reserve: getComputedStyle(messages).getPropertyValue('--chat-input-reserve')};
}"""


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_main_and_project_composer_clearance(direct_server_with_data, engine):
    from playwright.sync_api import sync_playwright
    from ouroboros.projects_registry import create_project

    data = direct_server_with_data["data_dir"]
    project = create_project(data, "composer-room", name="Composer room")
    (data / "logs" / "chat.jsonl").write_text(json.dumps({
        "ts": "2026-09-17T10:00:00Z", "direction": "out", "chat_id": project["chat_id"],
        "text": "Saved Project answer.\n" + "\n".join(f"History line {i}" for i in range(12)),
        "format": "markdown",
    }) + "\n", encoding="utf-8")
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", data.parent))
    evidence.mkdir(parents=True, exist_ok=True)
    observations = []
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1187, "height": 734})
            page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
            page.goto(direct_server_with_data["url"], wait_until="domcontentloaded")
            page.wait_for_function("() => window.__testSockets?.some(s => s.readyState === 1)")
            for width in (1187, 390):
                page.set_viewport_size({"width": width, "height": 734})
                for surface in ("main", "project"):
                    if surface == "project":
                        page.locator('.nav-project-row[data-project-id="composer-room"]').evaluate("el => el.click()")
                        messages = page.locator('#project-panel .chat-messages')
                        messages.locator('.chat-bubble').filter(has_text="Saved Project answer").wait_for(state="visible")
                    else:
                        page.locator('[data-nav-page="chat"]').evaluate("el => el.click()")
                        messages = page.locator('#chat-messages')
                    page.evaluate(_SETTLE_RESTORE_FRAMES)
                    for text in ("", "one\ntwo\nthree\nfour\nfive"):
                        composer_input = messages.locator('xpath=..').locator('textarea')
                        composer_input.fill(text)
                        page.evaluate(_SETTLE_RESTORE_FRAMES)
                        metrics = messages.evaluate(_MEASURE)
                        observations.append({"width": width, "surface": surface, "multiline": bool(text), **metrics})
                        assert metrics["padding"] == "0px" and metrics["shrink"] == "0", metrics
                        assert metrics["lastBottom"] is None or metrics["lastBottom"] <= metrics["composerTop"] + 1, metrics
                    chat_id = project["chat_id"] if surface == "project" else 1
                    _emit_ws_frame(page, {"type": "chat", "role": "assistant", "chat_id": chat_id,
                        "content": "Growing answer.\n" + "\n".join(f"New line {i}" for i in range(50)),
                        "ts": "2026-09-18T10:00:00Z"})
                    page.evaluate(_SETTLE_RESTORE_FRAMES)
                    metrics = messages.evaluate(_MEASURE)
                    assert metrics["lastBottom"] <= metrics["composerTop"] + 1, metrics
                    page.screenshot(path=str(evidence / f"composer-{engine}-{surface}-{width}.png"), full_page=True)
                    if width == 390:
                        page.evaluate("() => {document.documentElement.style.setProperty('--vvh','500px'); document.body.classList.add('keyboard-open'); document.documentElement.classList.add('keyboard-open');}")
                        metrics = messages.evaluate(_MEASURE)
                        assert metrics["spacer"] == "none", metrics
                        assert metrics["lastBottom"] <= metrics["composerTop"] + 1, metrics
                        page.evaluate("() => {document.body.classList.remove('keyboard-open'); document.documentElement.classList.remove('keyboard-open');}")
            (evidence / f"composer-{engine}.json").write_text(json.dumps(observations, indent=2), encoding="utf-8")
        finally:
            browser.close()


@pytest.mark.ui_browser
@pytest.mark.serial
def test_wizard_enabled_checkbox_stays_inline(subscription_ui):
    """The real wizard keeps its already-fixed checkbox width at phone scale."""
    from tests.test_ui_smoke_agents_panel import _wizard_step_until, _WIZARD_ON_AGENTS_JS
    from tests.test_subscription_setup_browser import capture

    page = subscription_ui["page"]
    page.goto(subscription_ui["url"] + "/onboarding")
    _wizard_step_until(page, _WIZARD_ON_AGENTS_JS, forward=True)
    label = page.locator('#onboarding-available-subagents .available-subagents-toolbar .local-toggle')
    for width in (1440, 390):
        page.set_viewport_size({"width": width, "height": 900})
        label.scroll_into_view_if_needed()
        geometry = label.evaluate("""label => {
            const input = label.querySelector('input');
            const box = input.getBoundingClientRect();
            const text = [...label.childNodes].find(n => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
            const range = new Range(); range.selectNodeContents(text);
            const word = range.getBoundingClientRect();
            return {checkboxWidth:box.width, height:label.getBoundingClientRect().height,
                textTop:word.top, textBottom:word.bottom, checkboxTop:box.top, checkboxBottom:box.bottom};
        }""")
        assert geometry["checkboxWidth"] == 14, geometry
        assert geometry["height"] <= 28, geometry
        assert geometry["textBottom"] <= geometry["checkboxBottom"] + 6, geometry
        capture(page, f"wizard-inline-checkbox-{width}")
