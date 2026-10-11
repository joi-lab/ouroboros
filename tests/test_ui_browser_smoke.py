from __future__ import annotations

import pathlib

import pytest


@pytest.mark.ui_browser
def test_gateway_frontend_uses_api_client_boundary():
    """Minimal UI-browser lane sentinel for the Gateway Boundary refactor.

    Full browser launch coverage remains in the ``test_ui_smoke_playwright.py``
    and ``test_ui_smoke_review_checkpoint.py`` smoke modules.
    This focused check keeps the lane aware of the new frontend boundary even
    when Playwright is unavailable locally.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    modules = root / "web" / "modules"
    assert (root / "web" / "package.json").is_file()
    assert (modules / "api_client.js").is_file()
    assert (modules / "api_types.js").is_file()
    raw_fetch_hits = []
    for path in modules.glob("*.js"):
        if path.name in {"api_client.js", "onboarding_wizard.js"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "fetch(" in text and "overrides fetch()" not in text:
            raw_fetch_hits.append(path.name)
    assert raw_fetch_hits == []
