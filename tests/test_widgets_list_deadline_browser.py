"""F5: the Widgets list deadline as the owner meets it, in real engines.

Chromium AND WebKit: desktop packaging ships WebKit, so a Chromium-only pass is
not evidence for the shipped surface.  A missing engine is a SKIP, never a PASS.

Each journey stalls a real gateway response (headers withheld) and waits out the
real 25 s deadline — the constant is not shortened for the test, because the
thing under test IS the deadline the owner lives with.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data as _direct_server_with_data
from tests.test_widgets_ui_browser import _write_module_widget_smoke_extension

direct_server_with_data = _direct_server_with_data

DEADLINE_MS = 25_000
ENGINES = ("chromium", "webkit")


def _evidence_dir(data_dir: pathlib.Path) -> pathlib.Path:
    directory = pathlib.Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(data_dir.parent)))
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _launch(pw, engine: str):
    from playwright.sync_api import Error as PlaywrightError

    try:
        return getattr(pw, engine).launch(headless=True)
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(f"{engine} is not installed: {exc}")
        raise


def _enable(page, skill: str) -> None:
    toggled = page.evaluate(
        """async (skill) => {
            const response = await fetch(`/api/skills/${encodeURIComponent(skill)}/toggle`, {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: true}),
            });
            return {status: response.status, body: await response.json()};
        }""",
        skill,
    )
    assert toggled["status"] == 200, toggled


def _open_widgets(page) -> None:
    page.evaluate(
        """() => {
            const button = [...document.querySelectorAll('[data-nav-page="widgets"]')]
                .find((item) => getComputedStyle(item).display !== 'none');
            button?.click();
        }"""
    )


def _open_dashboard(page) -> None:
    page.evaluate(
        """() => {
            const button = [...document.querySelectorAll('[data-nav-page="dashboard"]')]
                .find((item) => getComputedStyle(item).display !== 'none');
            button?.click();
        }"""
    )


def _error_text(page) -> str:
    return page.locator("#widgets-list-error [data-widget-list-error]").inner_text()


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ENGINES)
def test_list_deadline_reports_a_timeout_keeps_last_good_cards_and_retries(
    direct_server_with_data, engine
):
    """Stall the list, meet the deadline, keep the cards, recover on Retry."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    evidence = _evidence_dir(data_dir)
    skill = _write_module_widget_smoke_extension(data_dir)
    card = f'[data-widget-key="{skill}:auto"]'

    with sync_playwright() as pw:
        browser = _launch(pw, engine)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _enable(page, skill)
            _open_widgets(page)
            page.locator(card).wait_for(state="visible", timeout=30_000)
            # Mark the live card node so a preserved card is provably the SAME node.
            page.evaluate(
                "(selector) => { document.querySelector(selector).dataset.ouroMark = 'first-generation'; }",
                card,
            )
            assert page.locator("#widgets-list-error").is_hidden()

            stalled = []
            page.route("**/api/widgets", lambda route: stalled.append(route))
            _open_dashboard(page)
            page.wait_for_timeout(200)
            _open_widgets(page)
            for _ in range(200):
                if stalled:
                    break
                page.wait_for_timeout(50)
            assert stalled, "the re-entry did not re-read the list"

            # Nothing may fail early; the banner appears only at the deadline.
            page.wait_for_timeout(3_000)
            assert page.locator("#widgets-list-error").is_hidden(), "the list failed before its deadline"

            page.wait_for_selector("#widgets-list-error:not([hidden])", timeout=DEADLINE_MS + 15_000)
            message = _error_text(page)
            assert "timed out" in message.lower(), message
            retry = page.locator("#widgets-list-error [data-widget-list-retry]")
            assert retry.is_visible()
            assert retry.is_enabled()
            # The last good cards survive the failure, as the same DOM node.
            assert page.locator(card).count() == 1
            assert page.locator(card).get_attribute("data-ouro-mark") == "first-generation"
            page.screenshot(path=str(evidence / f"f5-{engine}-list-timeout.png"), full_page=True)

            for route in stalled:
                try:
                    route.abort()
                except Exception:
                    pass
            page.unroute("**/api/widgets")
            retry.click()
            page.wait_for_selector("#widgets-list-error", state="hidden", timeout=30_000)
            page.locator(card).wait_for(state="visible", timeout=30_000)
            assert page.locator(card).get_attribute("data-ouro-mark") == "first-generation", \
                "an unchanged list signature must not replace the card node"
            page.screenshot(path=str(evidence / f"f5-{engine}-list-recovered.png"), full_page=True)
        finally:
            browser.close()


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ENGINES)
def test_preferences_only_stall_times_out_and_navigating_away_stays_silent(
    direct_server_with_data, engine
):
    """A preferences-only stall is still a list deadline; leaving is not an error."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    evidence = _evidence_dir(data_dir)
    skill = _write_module_widget_smoke_extension(data_dir)
    card = f'[data-widget-key="{skill}:auto"]'

    with sync_playwright() as pw:
        browser = _launch(pw, engine)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _enable(page, skill)
            _open_widgets(page)
            page.locator(card).wait_for(state="visible", timeout=30_000)

            # 1) Leaving during a stall must not paint a failure.
            held = []
            page.route("**/api/ui/preferences", lambda route: held.append(route))
            _open_dashboard(page)
            page.wait_for_timeout(200)
            _open_widgets(page)
            for _ in range(200):
                if held:
                    break
                page.wait_for_timeout(50)
            assert held, "the re-entry did not re-read preferences"
            _open_dashboard(page)
            page.wait_for_timeout(1_000)
            _open_widgets(page)
            page.wait_for_timeout(500)
            assert page.locator("#widgets-list-error").is_hidden(), \
                "an owner-initiated navigation must not look like a failure"

            # 2) A preferences stall that is never released still hits the deadline.
            page.wait_for_selector("#widgets-list-error:not([hidden])", timeout=DEADLINE_MS + 20_000)
            message = _error_text(page)
            assert "timed out" in message.lower(), message
            assert page.locator(card).count() == 1, "the cards survive a preferences timeout"
            page.screenshot(path=str(evidence / f"f5-{engine}-prefs-timeout.png"), full_page=True)

            for route in held:
                try:
                    route.abort()
                except Exception:
                    pass
            page.unroute("**/api/ui/preferences")
            page.locator("#widgets-list-error [data-widget-list-retry]").click()
            page.wait_for_selector("#widgets-list-error", state="hidden", timeout=30_000)
            page.screenshot(path=str(evidence / f"f5-{engine}-prefs-recovered.png"), full_page=True)
        finally:
            browser.close()


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ENGINES)
def test_a_failing_preferences_read_never_blanks_the_widgets_list(
    direct_server_with_data, engine
):
    """Regression for the tolerance the deadline rewrite briefly dropped."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    evidence = _evidence_dir(data_dir)
    skill = _write_module_widget_smoke_extension(data_dir)
    card = f'[data-widget-key="{skill}:auto"]'

    with sync_playwright() as pw:
        browser = _launch(pw, engine)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            _enable(page, skill)
            page.route(
                "**/api/ui/preferences",
                lambda route: route.fulfill(
                    status=500, content_type="application/json",
                    body=json.dumps({"error": "preferences unavailable"}),
                ) if route.request.method == "GET" else route.continue_(),
            )
            _open_widgets(page)
            page.locator(card).wait_for(state="visible", timeout=30_000)
            page.wait_for_timeout(1_000)
            assert page.locator("#widgets-list-error").is_hidden(), \
                "a preferences failure must not blank the Widgets list"
            page.screenshot(path=str(evidence / f"f5-{engine}-prefs-error-tolerated.png"), full_page=True)
        finally:
            browser.close()
