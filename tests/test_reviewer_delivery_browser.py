"""Actual Settings renderer: delivery edits survive Save and reload."""
import json

import pytest
from tests import test_subscription_role_routes_browser as roles

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
subscription_ui = roles.subscription_ui
role_ui = roles.role_ui


@pytest.mark.parametrize("width", [1360, 390])
def test_delivery_choice_saves_and_reloads_without_changing_legacy_row(role_ui, width):
    ui = role_ui
    roles.configure_mixed(ui)
    ui["page"].set_viewport_size({"width": width, "height": 900})
    page = roles.open_agents(ui)
    row = page.locator('[data-slot-id="triad_1"]')
    delivery = row.locator('[data-slot-delivery]')
    assert delivery.input_value() == "packet"
    delivery.select_option("native")
    row.scroll_into_view_if_needed()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    roles.capture(page, f"reviewer-delivery-native-{width}")
    with page.expect_response('**/api/settings'):
        page.locator('#btn-save-settings').click()
    saved = [body for path, body in ui["posts"] if path == '/api/settings'][-1]
    assert json.loads(saved['OUROBOROS_REVIEWER_SLOTS'])['triad'][0]['delivery'] == 'native'
    with page.expect_response('**/api/reviewer-slots'):
        page.locator('#btn-reload-settings').click()
    assert row.locator('[data-slot-delivery]').input_value() == 'native'
    row.locator('[data-slot-delivery]').select_option('packet')
    with page.expect_response('**/api/settings'):
        page.locator('#btn-save-settings').click()
    saved = [body for path, body in ui["posts"] if path == '/api/settings'][-1]
    assert json.loads(saved['OUROBOROS_REVIEWER_SLOTS'])['triad'][0]['delivery'] == 'packet'


@pytest.fixture
def settings_gateway(tmp_path, monkeypatch):
    """Real settings read/write and reviewer resolution, isolated from runtime startup."""
    from types import SimpleNamespace
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient
    from ouroboros import config as cfg
    from ouroboros.gateway import settings as gateway

    data = tmp_path / "settings-data"
    data.mkdir()
    path = data / "settings.json"
    monkeypatch.setattr(cfg, "DATA_DIR", data)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", path)
    for key in cfg.SETTINGS_DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    cfg.reset_runtime_mode_baseline_for_tests()

    def project(settings):
        environment = {}
        cfg.apply_settings_to_env(settings, environ=environment)
        for key, value in environment.items():
            monkeypatch.setenv(key, value)

    monkeypatch.setattr(gateway, "_apply_settings_to_env", project)
    monkeypatch.setattr(gateway, "_start_supervisor_if_needed_for_request", lambda *a: False)
    monkeypatch.setattr(gateway, "_apply_settings_save_side_effects", lambda *a: [])
    monkeypatch.setattr(gateway, "_has_started_agent_tasks", lambda: False)
    app = Starlette(routes=[
        Route("/api/settings", gateway.api_settings_get, methods=["GET"]),
        Route("/api/settings", gateway.api_settings_post, methods=["POST"]),
        Route("/api/reviewer-slots", gateway.api_reviewer_slots),
    ])
    app.state.drive_root = data
    app.state.repo_dir = tmp_path
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, path=path, project=project, cfg=cfg)
    cfg.reset_runtime_mode_baseline_for_tests()


@pytest.mark.parametrize("case", ["fresh", "saved_unknown", "legacy_pin", "openrouter", "openai"])
def test_first_panel_save_preserves_displayed_deep_route(subscription_ui, settings_gateway, case):
    from ouroboros.deep_self_review import deep_review_route
    from ouroboros.settings_defaults import OPENROUTER_DEFAULTS

    ui, gateway = subscription_ui, settings_gateway
    initial = {"OUROBOROS_RUNTIME_MODE": "advanced", "OUROBOROS_CONTEXT_MODE": "max",
               "OUROBOROS_MODEL": "openai-compatible::glm-5.3",
               "OPENAI_COMPATIBLE_BASE_URL": "https://llm.example/v1",
               "OPENAI_COMPATIBLE_API_KEY": "test-only-key"}
    if case in ("openrouter", "openai"):
        initial.pop("OPENAI_COMPATIBLE_BASE_URL")
        initial.pop("OPENAI_COMPATIBLE_API_KEY")
        initial.update({("OPENROUTER_API_KEY" if case == "openrouter" else "OPENAI_API_KEY"): "test-only-key",
                        "OUROBOROS_MODEL": "openai/gpt-5.6-sol" if case == "openrouter" else "openai::gpt-5.6-sol"})
    if case == "legacy_pin":
        initial["OUROBOROS_MODEL_DEEP_SELF_REVIEW"] = OPENROUTER_DEFAULTS["deep_self_review"]
    from ouroboros.server_runtime import apply_runtime_provider_defaults
    initial, _, _ = apply_runtime_provider_defaults(initial)
    gateway.path.write_text(json.dumps(initial))
    gateway.project(gateway.cfg.load_settings())
    if case == "saved_unknown":
        view = gateway.client.get("/api/reviewer-slots").json()
        initial["OUROBOROS_REVIEWER_SLOTS"] = json.dumps({k: view[k] for k in ("triad", "scope", "advisory")})
        gateway.path.write_text(json.dumps(initial))
        gateway.project(gateway.cfg.load_settings())
    view = gateway.client.get("/api/reviewer-slots").json()
    expected_model = view["deep_review"]["route"]["target_id"]
    before_admission = deep_review_route()
    before_bytes = gateway.path.read_bytes()
    assert view["deep_review"]["synthesized_from"] == "OUROBOROS_MODEL_DEEP_SELF_REVIEW"
    page = ui["page"]
    writes = []

    def endpoint(route):
        if route.request.method == "POST":
            payload = route.request.post_data_json
            writes.append(payload)
            response = gateway.client.post("/api/settings", json=payload)
        else:
            endpoint_path = "/api/reviewer-slots" if "reviewer-slots" in route.request.url else "/api/settings"
            response = gateway.client.get(endpoint_path)
        route.fulfill(status=response.status_code, content_type="application/json", body=response.text)

    page.route("**/api/settings", endpoint)
    page.route("**/api/reviewer-slots", endpoint)
    # Keep discovery empty: it must neither replace the configured provider nor draft a session panel.
    ui["fixture"]["status"].update(harnesses=[], profiles={}, model_sources=[])
    ui["fixture"]["catalog"].update(items=[], model_sources=[])
    page.goto(ui["url"] + "/#settings")
    page.locator('[data-settings-tab="agents"]').click()
    page.wait_for_selector('[data-slot-effort]')
    deep = page.locator('[data-deep-review-api-model]')
    assert deep.input_value() == expected_model.split("::", 1)[-1]
    assert gateway.path.read_bytes() == before_bytes  # both GETs are passive
    deep.focus()
    deep.press("Escape")
    roles.capture(page, f"first-panel-{case}-before")
    # An unrelated save must not materialize a fresh default panel or its deep row.
    page.locator('[data-settings-tab="behavior"]').click()
    # Use the real Settings collector even when this setting's widget is not on this tab.
    with page.expect_response('**/api/settings'):
        page.locator('#btn-save-settings').click()
    if case != "saved_unknown":
        assert "OUROBOROS_REVIEWER_SLOTS" not in writes[-1]
        assert not json.loads(gateway.path.read_text()).get("OUROBOROS_REVIEWER_SLOTS")
    assert deep_review_route() == before_admission
    page.locator('[data-settings-tab="agents"]').click()
    triad = page.locator('[data-slot-effort]').first
    triad.select_option("high" if triad.input_value() != "high" else "low")
    with page.expect_response('**/api/settings'):
        page.locator('#btn-save-settings').click()
    saved = json.loads(gateway.path.read_text())["OUROBOROS_REVIEWER_SLOTS"]
    saved = json.loads(saved) if isinstance(saved, str) else saved
    with page.expect_response('**/api/reviewer-slots'):
        page.locator('#btn-reload-settings').click()
    page.wait_for_function("() => !document.querySelector('#btn-reload-settings').disabled")
    page.locator('[data-deep-review-api-model]').focus()
    page.locator('[data-deep-review-api-model]').press("Escape")
    roles.capture(page, f"first-panel-{case}-after")
    reloaded = gateway.client.get("/api/reviewer-slots").json()
    assert reloaded["deep_review"]["route"]["target_id"] == expected_model
    assert deep_review_route() == before_admission
    if case == "fresh":
        assert before_admission == ("", "openai-compatible::glm-5.3")
        assert saved["deep_review"]["route"]["target_id"] == expected_model
    if case == "saved_unknown":
        assert "deep_review" not in saved and before_admission[0]
    if case == "legacy_pin":
        assert json.loads(gateway.path.read_text())["OUROBOROS_MODEL_DEEP_SELF_REVIEW"] == initial["OUROBOROS_MODEL_DEEP_SELF_REVIEW"]
    import os
    from pathlib import Path
    if output := os.environ.get("OUROBOROS_UI_EVIDENCE_DIR"):
        Path(output, f"settings-flow-{case}.json").write_text(json.dumps({
            "before": view, "saved_panel": saved, "after": reloaded,
            "reviewer_posts": [p.get("OUROBOROS_REVIEWER_SLOTS") for p in writes],
            "admission_before": before_admission, "admission_after": deep_review_route(),
        }, indent=2))
