"""Fixture-backed UI static contracts for chat/logs/files surfaces.

Trimmed in v5.16.0-rc.2: only the fixture-backed parametrized contract
remains here. The previous CSS/JS source-string assertions (chat composer
geometry, live-card timeline, plan-mode dropdown, mobile keyboard layout,
etc.) were retired — those surfaces are exercised by the Playwright
``ui_browser`` smoke suite where it applies (`tests/test_ui_smoke_playwright.py`
and `tests/test_ui_smoke_review_checkpoint.py`)
and by manual UI review for the rest. Static pin-by-string assertions
were retired with the understanding that a regression in those visual
surfaces will surface in the ui_browser CI lane rather than in unit
tests.

The shape of this file is also pinned by
``docs/ARCHITECTURE.md`` (About sub-tab note) — keep
``test_static_ui_contracts`` discoverable here and keep the fixture file
``tests/fixtures/chat_logs_ui_static_checks.json`` as its single source
of truth.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
_CHECKS = json.loads(
    (REPO / "tests" / "fixtures" / "chat_logs_ui_static_checks.json").read_text(
        encoding="utf-8"
    )
)


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "check",
    _CHECKS,
    ids=lambda item: f"{item['id']}::{item['path']}::{item['kind']}",
)
def test_static_ui_contracts(check):
    source = _read(check["path"])
    value = check["value"]
    if check["kind"] == "contains":
        assert value in source
    elif check["kind"] == "not_contains":
        assert value not in source
    elif check["kind"] == "regex":
        assert re.search(value, source, re.S)
    else:  # pragma: no cover
        raise AssertionError(f"unknown check kind: {check['kind']}")
