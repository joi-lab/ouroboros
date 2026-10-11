"""Two author-owned routes; neither changes the widget bridge or host policy."""

import json
from pathlib import Path
import secrets

from starlette.responses import HTMLResponse, JSONResponse

from ouroboros.server_web import read_author_kit_assets


def author_kit(request):
    return JSONResponse(
        read_author_kit_assets(request.app.state.repo_dir),
        headers={"Cache-Control": "no-store"},
    )


def page(request):
    # Resolve at GET, not register time: a new mount sees the installed source.
    source = dict(read_author_kit_assets(request.app.state.repo_dir))
    source["application"] = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")
    embedded = json.dumps(source).replace("<", "\\u003c")
    nonce = secrets.token_urlsafe(18)
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Author controls</title></head><body>
<main id="root"><p role="status">Loading controls…</p></main>
<script id="author-kit-source" type="application/json">{embedded}</script>
<script nonce="{nonce}" type="module">
const root = document.getElementById('root');
root.dataset.styleNonce = {json.dumps(nonce)};
const source = JSON.parse(document.getElementById('author-kit-source').textContent);
const url = URL.createObjectURL(new Blob([source.application], {{type: 'text/javascript'}}));
try {{ await import(url); }}
catch (error) {{ root.textContent = 'Controls unavailable: ' + error.message; }}
finally {{ URL.revokeObjectURL(url); }}
</script></body></html>"""
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            f"default-src 'none'; script-src 'nonce-{nonce}' blob:; "
            f"style-src 'nonce-{nonce}'; img-src data:; base-uri 'none'"
        ),
    })


def register(api):
    api.register_route("author-kit", author_kit, methods=("GET",))
    api.register_route("page", page, methods=("GET",))
    api.register_ui_tab("module", "Shared controls", render={
        "kind": "module", "entry": "widget.js", "appearance": "host",
    })
    api.register_ui_tab("page", "Independent page", render={
        "kind": "iframe", "route": "page",
    })
