"""The onboarding page route: one wizard host for every shell.

``GET /onboarding`` serves the first-run wizard as an ordinary page so it can
load ``web/modules/*`` as real ES modules. Three consumers share it:

* the desktop launcher, which opens this URL in its setup window AFTER the
  managed gateway is healthy (there is no pre-server wizard any more);
* the blocking overlay of an already-running but unconfigured app, which
  frames this URL instead of an inlined ``srcdoc`` document;
* an ordinary browser / Docker / Linux-fallback owner, who can open it directly.

The route is deliberately SIDE-EFFECT-FREE. Provider-default normalization is
computed for display only and never persisted: on a genuinely fresh install the
first bytes of ``settings.json`` must be the owner's own onboarding save, which
is exactly what the fresh-install proofs (safety-coverage authorship, install
presets) are gated on.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse

from ouroboros.config import load_settings
from ouroboros.onboarding_wizard import build_onboarding_html
from ouroboros.server_runtime import apply_runtime_provider_defaults


async def onboarding_page(_request: Request) -> HTMLResponse:
    from ouroboros.config import SETTINGS_PATH

    settings, _changed, _keys = apply_runtime_provider_defaults(load_settings())
    return HTMLResponse(
        build_onboarding_html(settings, host_mode="web", fresh_install=not SETTINGS_PATH.exists()),
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["onboarding_page"]
