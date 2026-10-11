"""Rendered Settings keeps reviewer overrides and describes effort as a preference."""
import json

import pytest

from tests import test_subscription_role_routes_browser as roles

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
subscription_ui = roles.subscription_ui
role_ui = roles.role_ui


@pytest.mark.parametrize("width", [1360, 390])
def test_reviewer_effort_override_caption_and_saved_preference(role_ui, tmp_path, width):
    ui = role_ui
    roles.configure_mixed(ui)
    ui["settings"]["OUROBOROS_SUBAGENTS"]["items"][0]["effort"] = "high"
    slots = ui["fixture"]["preview"]["reviewer_slots"]
    slots["scope"][0]["effort"] = "low"
    ui["settings"]["OUROBOROS_REVIEWER_SLOTS"] = json.dumps(slots)
    ui["page"].set_viewport_size({"width": width, "height": 900})
    page = roles.open_agents(ui)
    row = page.locator('[data-slot-id="scope_1"]')
    caption = row.locator('.reviewer-slot-meta')
    assert "preferred effort low" in caption.inner_text()
    row.locator('[data-slot-effort]').select_option('medium')
    assert "preferred effort medium" in caption.inner_text()
    deep = page.locator('[data-deep-review-row]')
    deep_caption = deep.locator('.reviewer-slot-meta')
    assert "preferred effort high" in deep_caption.inner_text()
    deep.locator('[data-deep-review-effort]').select_option('low')
    assert "preferred effort low" in deep_caption.inner_text()
    deep.locator('[data-deep-review-effort]').select_option('')
    assert "preferred effort high" in deep_caption.inner_text()
    deep.locator('[data-deep-review-effort]').select_option('medium')
    assert "preferred effort medium" in deep_caption.inner_text()
    row.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"effort-reviewer-{width}.png"))
    with page.expect_response('**/api/settings'):
        page.locator('#btn-save-settings').click()
    saved = [body for path, body in ui["posts"] if path == '/api/settings'][-1]
    assert json.loads(saved['OUROBOROS_REVIEWER_SLOTS'])['scope'][0]['effort'] == 'medium'
    assert json.loads(saved['OUROBOROS_REVIEWER_SLOTS'])['deep_review']['effort'] == 'medium'
    with page.expect_response('**/api/reviewer-slots'):
        page.locator('#btn-reload-settings').click()
    assert "preferred effort medium" in caption.inner_text()
    assert "preferred effort medium" in deep_caption.inner_text()
    page.locator('[data-settings-tab="behavior"]').click()
    help_text = page.get_by_text('Preferred reasoning effort per task type.', exact=False)
    assert "Requested, sent and reported effort are recorded in Logs" in help_text.inner_text()
    help_text.scroll_into_view_if_needed()
    page.screenshot(path=str(tmp_path / f"effort-settings-{width}.png"))
