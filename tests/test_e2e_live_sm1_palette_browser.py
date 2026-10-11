"""SM1 palette delivery through both production documents; no model or runtime process.

Reuse the subscription UI fixture's real static server, templates, bootstrap and mock APIs.
The restart/commit are wiring inputs here, not a claimed paid self-modification result.
"""
from __future__ import annotations

import json
import types

import pytest

from devtools.e2e_live import scenarios
from devtools.e2e_live.ui_probe import UIProbe
from tests import test_subscription_setup_browser as setup_browser

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.mark.parametrize("fault", ["none", "wizard-missing-link", "wizard-stale-focus"])
def test_shared_palette_reaches_both_documents_and_rejects_divergence(subscription_ui, tmp_path, fault):
    ui = subscription_ui
    page = ui["page"]
    shared = scenarios.css_with_accent((setup_browser.WEB / "ui.css").read_text(), scenarios.SM1_NEW_ACCENT)
    # A visible fixture-only palette change. The paid model/review still chooses its own
    # coherent derivation; this tests that real consumers follow the shared roles.
    shared = shared.replace("201, 53, 69", "47, 125, 225").replace("#f07a86", "#8fc0ff")
    page.route("**/static/ui.css", lambda route: route.fulfill(content_type="text/css", body=shared))
    if fault == "wizard-missing-link":
        html = (setup_browser.WEB / "onboarding_template.html").read_text()
        link = '<link rel="stylesheet" href="/static/ui.css">'
        assert html.count(link) == 1
        bootstrap = (setup_browser.WEB / "tests/fixtures/onboarding_bootstrap.json").read_text()
        html = html.replace(link, "").replace("__ONBOARDING_BOOTSTRAP__", bootstrap)
        page.route("**/onboarding", lambda route: route.fulfill(content_type="text/html", body=html))
    elif fault == "wizard-stale-focus":
        local = (setup_browser.WEB / "onboarding.css").read_text() + "\n:root { --focus-accent-border: #00ff00; }"
        page.route("**/static/onboarding.css", lambda route: route.fulfill(content_type="text/css", body=local))
    probe = UIProbe(ui["url"])
    probe.page = page  # the existing fixture owns this browser's launch and cleanup
    server = types.SimpleNamespace(base_url=ui["url"])
    ctx = scenarios.LaneContext(server=server, clone=tmp_path, data_root=tmp_path, oracle=None, harness=None,
                                ui_resolver=lambda _: (probe, ""), ui_reason="", shots=tmp_path,
                                log=lambda _: None, task_timeout=1, restart=lambda: server)
    ctx.check("commit_landed", True)  # synthetic wiring input, never model/review evidence
    names = scenarios.SM1_REQUIRED_PALETTE | scenarios.sm1_palette_tokens(shared).keys()
    scenarios.check_sm1_rendered_palette(ctx, names)
    assert ctx.checks["ui_app_palette"] is True
    assert ctx.checks["ui_onboarding_palette"] is (fault != "wizard-missing-link")
    assert ctx.checks["ui_computed_style"] is (fault == "none"), json.dumps(ctx.facts)
    assert not ctx.ui_reason and len(ctx.screenshots) == 2
    setup_browser.capture(page, f"sm1-palette-{fault}")
    if fault == "none":
        assert probe.computed_property("#next-btn", "color") == "rgb(143, 192, 255)"
        assert probe.computed_property(".wizard-step.active", "background-color") == "rgba(47, 125, 225, 0.08)"
        probe.goto("/")
        assert probe.computed_property(".nav-row-main.active", "color") == "rgb(47, 125, 225)"
    ctx.close_ui()
