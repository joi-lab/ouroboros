"""The onboarding page route: one wizard host for every shell.

``GET /onboarding`` serves the selected complete setup page, else the builtin
wizard. ``?builtin=1`` explicitly opens recovery. Three consumers share it:

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

``GET /api/onboarding/bootstrap`` is the same derivation as JSON for a client
that renders its own setup: the page's embedded bootstrap, byte for byte, plus
the readiness ``GET /api/onboarding`` answers with its status. Completing setup
stays ``POST /api/onboarding/complete``, the one atomic writer.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from ouroboros.config import load_settings
from ouroboros.onboarding_wizard import render_onboarding_html
from ouroboros.server_runtime import apply_runtime_provider_defaults, has_startup_ready_provider
from ouroboros.settings_setup_contract import build_setup_bootstrap

_NO_STORE = {"Cache-Control": "no-store"}


def _live_setup() -> tuple[dict, dict]:
    """The install's settings view and the web wizard bootstrap derived from it: the one
    derivation the page and its JSON projection share, computed and never persisted."""
    from ouroboros.config import SETTINGS_PATH

    settings, _changed, _keys = apply_runtime_provider_defaults(load_settings())
    return settings, build_setup_bootstrap(settings, "web", fresh_install=not SETTINGS_PATH.exists())


async def onboarding_page(_request: Request) -> HTMLResponse:
    served_ui = getattr(_request.app.state, "served_ui", None)
    if served_ui is not None and _request.query_params.get("builtin") != "1":
        page = served_ui.custom_page("onboarding.html")
        if page is not None:
            return HTMLResponse(page, headers=_NO_STORE)
    _settings, bootstrap = _live_setup()
    return HTMLResponse(render_onboarding_html(bootstrap), headers=_NO_STORE)


async def api_onboarding_bootstrap(_request: Request) -> JSONResponse:
    """``OnboardingBootstrapResponse``: readiness plus the wizard's bootstrap, written nowhere."""
    settings, bootstrap = _live_setup()
    return JSONResponse({"ready": has_startup_ready_provider(settings), "bootstrap": bootstrap}, headers=_NO_STORE)


__all__ = ["api_onboarding_bootstrap", "onboarding_page"]
