"""The actual bundled skill form through the existing isolated keyless server."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import textwrap
from urllib.parse import urlsplit

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as direct_server_with_data  # noqa: F401

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def _install_form_fixture(data):
    from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_review_state
    name = "telegram_proxy_form"
    target = data / "skills" / "external" / name
    target.mkdir(parents=True)
    shutil.copytree(Path(__file__).resolve().parents[1] / "skills" / "telegram", target / "bundled",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (target / "SKILL.md").write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        description: Actual Telegram settings in a keyless form fixture.
        version: 0.1.0
        type: extension
        entry: plugin.py
        permissions: [route, widget, read_settings]
        ---
        # Telegram form fixture
        """), encoding="utf-8")
    # Capture the actual register() consumer and routes; only runtime start hooks
    # are disabled. No bot token, Telegram process or synthetic form schema.
    (target / "plugin.py").write_text(textwrap.dedent("""\
        from .bundled.plugin import register as register_telegram
        class FormOnlyAPI:
            def __init__(self, api): self.api = api
            def __getattr__(self, name): return getattr(self.api, name)
            def register_supervised_task(self, *args, **kwargs): pass
            def subscribe_event(self, *args, **kwargs): pass
            def register_companion_process(self, *args, **kwargs): pass
        def register(api):
            register_telegram(FormOnlyAPI(api))
        """), encoding="utf-8")
    save_review_state(data, name, SkillReviewState(
        status="pass", content_hash=compute_content_hash(target, manifest_entry="plugin.py")))
    return name


def test_actual_form_configures_edits_keeps_and_clears_proxy(direct_server_with_data):
    from playwright.sync_api import sync_playwright
    fixture = direct_server_with_data
    name = _install_form_fixture(fixture["data_dir"])
    state = fixture["data_dir"] / "state" / "skills" / name / "settings.json"
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(fixture["data_dir"].parent)))
    evidence.mkdir(parents=True, exist_ok=True)
    first = "socks5://owner:synthetic-secret@127.0.0.1:1080"
    edited = "http://owner:replacement-secret@127.0.0.1:3128"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        page.set_default_timeout(15000)
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).hostname in
                   ("127.0.0.1", "localhost") else route.abort())
        try:
            page.goto(fixture["url"], wait_until="domcontentloaded")
            toggled = page.evaluate("""async name => {
                const r = await fetch(`/api/skills/${name}/toggle`, {method:'POST',
                    headers:{'Content-Type':'application/json'}, body:JSON.stringify({enabled:true})});
                return {status:r.status, body:await r.json()};
            }""", name)
            assert toggled["status"] == 200 and toggled["body"].get("enabled") is True, toggled
            def form():
                page.click('[data-nav-page="settings"]')
                page.click('[data-settings-tab="advanced"]')
                found = page.locator(f'[data-extension-settings-form][data-skill="{name}"][data-route="settings/save"]')
                found.wait_for(state="visible")
                return found
            def saved_form():
                page.reload(wait_until="domcontentloaded")
                return form()
            current = form()
            password = current.locator('[name="TELEGRAM_PROXY"]')
            assert password.get_attribute("type") == "password"
            assert password.input_value() == ""
            assert current.locator('[name="telegram_proxy_status"]').input_value() == "Not configured"
            password.fill(first)
            current.screenshot(path=str(evidence / "telegram-proxy-entered.png"))
            current.get_by_role("button", name="Save Telegram settings").click()
            status = current.locator('[data-extension-settings-status]')
            from playwright.sync_api import expect
            expect(status).to_contain_text("Saved Telegram proxy: configured.")
            assert json.loads(state.read_text(encoding="utf-8"))["TELEGRAM_PROXY"] == first
            current = saved_form()
            assert current.locator('[name="telegram_proxy_status"]').input_value() == "Configured"
            assert current.locator('[name="TELEGRAM_PROXY"]').input_value() == ""
            assert "synthetic-secret" not in page.content()
            current.screenshot(path=str(evidence / "telegram-proxy-configured.png"))
            # An ordinary unrelated Save does not clear a masked empty field.
            current.locator('[name="TELEGRAM_COMMAND_MODE"]').select_option("strict")
            current.get_by_role("button", name="Save Telegram settings").click()
            expect(current.locator('[data-extension-settings-status]')).to_contain_text("Telegram settings saved.")
            assert json.loads(state.read_text(encoding="utf-8"))["TELEGRAM_PROXY"] == first
            current.locator('[name="TELEGRAM_PROXY"]').fill("socks5://owner:invalid-secret@proxy")
            current.get_by_role("button", name="Save Telegram settings").click()
            expect(current.locator('[data-extension-settings-status]')).to_contain_text("TELEGRAM_PROXY must be")
            assert json.loads(state.read_text(encoding="utf-8"))["TELEGRAM_PROXY"] == first
            assert "invalid-secret" not in current.locator('[data-extension-settings-status]').inner_text()
            current.screenshot(path=str(evidence / "telegram-proxy-invalid.png"))
            current.locator('[name="TELEGRAM_PROXY"]').fill(edited)
            current.get_by_role("button", name="Save Telegram settings").click()
            expect(current.locator('[data-extension-settings-status]')).to_contain_text("Saved Telegram proxy: configured.")
            assert json.loads(state.read_text(encoding="utf-8"))["TELEGRAM_PROXY"] == edited
            current = saved_form()
            current.locator('[name="clear_telegram_proxy"]').check()
            current.get_by_role("button", name="Save Telegram settings").click()
            expect(current.locator('[data-extension-settings-status]')).to_contain_text("Saved Telegram proxy: cleared.")
            assert json.loads(state.read_text(encoding="utf-8"))["TELEGRAM_PROXY"] == ""
            current = saved_form()
            assert current.locator('[name="telegram_proxy_status"]').input_value() == "Not configured"
            assert current.locator('[name="TELEGRAM_PROXY"]').input_value() == ""
            assert not current.locator('[name="clear_telegram_proxy"]').is_checked()
            assert "replacement-secret" not in page.content()
            current.screenshot(path=str(evidence / "telegram-proxy-cleared.png"))
            hydrated = page.evaluate("""async name => {
                const r = await fetch(`/api/extensions/${name}/settings/save`); return await r.json();
            }""", name)
            assert "TELEGRAM_PROXY" not in hydrated
            assert hydrated["telegram_proxy_status"] == "Not configured"
            assert not errors, errors
        finally:
            browser.close()
