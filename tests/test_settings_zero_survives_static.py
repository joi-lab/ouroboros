"""A stored 0 survives the Settings form round trip.

The load path applied ``fallback && !s[key] ? fallback : s[key]`` to every
INPUT_FIELDS row, so a saved ``0`` (``OUROBOROS_CONSCIOUSNESS_DAILY_USD``, the
documented "may not spend" value) rendered as its fallback ``20`` and the next
save of any tab wrote ``20`` back. Only a missing value may take the fallback.

Source pin (the pattern of the other *_static tests) plus the typed server read
the helper relies on. The helper itself runs in the node lane
(``web/tests/settings_stored_zero.test.js``); the load/save/reload round trip
runs against a real server in ``tests/test_ui_smoke_settings_drafts.py``.
"""

from __future__ import annotations

import pathlib

SETTINGS_JS = pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "settings.js"


def _source() -> str:
    return SETTINGS_JS.read_text(encoding="utf-8")


def test_input_fields_load_through_the_helper():
    src = _source()
    assert "applyInputValue(id, storedOrFallback(s[key], fallback))" in src
    assert "fallback && !s[key]" not in src


def test_server_read_keeps_a_saved_zero_and_defaults_only_a_blank():
    # GET /api/settings serves this coercion: the form gets the number 0, never ''
    # or null, so the helper's absence branch cannot swallow it; a blank reads as the default.
    from ouroboros import config as cfg

    key = "OUROBOROS_CONSCIOUSNESS_DAILY_USD"
    assert cfg._coerce_setting_value(key, "0") == 0.0
    assert cfg._coerce_setting_value(key, 0) == 0.0
    assert cfg._coerce_setting_value(key, "") == cfg.SETTINGS_DEFAULTS[key]
