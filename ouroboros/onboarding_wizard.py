"""Shared onboarding wizard helpers for desktop and web.

ONE onboarding host (D-8): the wizard is a real page served by the gateway on
every host — desktop, ordinary browser, Docker, and the Linux browser fallback.
The page LINKS its stylesheet and its ES module from ``/static`` instead of
inlining them, because the wizard's steps must be able to import ordinary
``web/modules/*`` code (the account/login components), which a self-contained
inlined string can never do. This is the same contract, the same validator and
the same settings authority as before — only the delivery changed.
"""

from __future__ import annotations

import json
import pathlib
from typing import Tuple

from ouroboros.settings_setup_contract import (
    build_setup_bootstrap,
    validate_setup_payload,
)

_ASSET_ROOT = pathlib.Path(__file__).resolve().parents[1] / "web"
_TEMPLATE_PATH = _ASSET_ROOT / "onboarding_template.html"


from ouroboros.utils import read_text as _read_asset


def _bootstrap_script_json(bootstrap: dict) -> str:
    """Serialize the bootstrap for an inline ``<script>`` body.

    ``ensure_ascii`` alone does not neutralize ``</script>``; a stored provider
    value containing it would close the element and inject markup. Escaping
    ``<`` as ``\\u003c`` stays valid JSON *and* valid JavaScript.
    """
    return json.dumps(bootstrap, ensure_ascii=True).replace("<", "\\u003c")


def build_onboarding_html(settings: dict, host_mode: str = "desktop", *, fresh_install: bool = False) -> str:
    """Render the onboarding page for a live gateway.

    ``host_mode`` still selects the setup contract's host profile; every server
    route passes ``"web"`` now that the wizard always runs against a live API.
    """
    normalized_host_mode = "web" if host_mode == "web" else "desktop"
    bootstrap = build_setup_bootstrap(settings, normalized_host_mode, fresh_install=fresh_install)
    return _read_asset(_TEMPLATE_PATH).replace(
        "__ONBOARDING_BOOTSTRAP__", _bootstrap_script_json(bootstrap)
    )


def prepare_onboarding_settings(data: dict, current_settings: dict) -> Tuple[dict, str | None]:
    return validate_setup_payload(data, current_settings)
