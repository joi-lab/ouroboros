"""Static web helpers extracted from server.py."""

from __future__ import annotations

import importlib.util
import pathlib

from starlette.responses import HTMLResponse, FileResponse
from starlette.staticfiles import StaticFiles


def resolve_web_dir(repo_dir: pathlib.Path) -> pathlib.Path:
    repo_web_dir = repo_dir / "web"
    if repo_web_dir.exists():
        return repo_web_dir

    spec = importlib.util.find_spec("web")
    origin = getattr(spec, "origin", None) if spec else None
    if origin:
        package_dir = pathlib.Path(origin).resolve().parent
        if package_dir.exists():
            return package_dir

    return repo_web_dir


def read_author_kit_assets(repo_dir: pathlib.Path) -> dict[str, str]:
    """Read the installed portable controls for an opt-in author page.

    Extension handlers pass ``request.app.state.repo_dir`` so in-process and
    isolated route workers read the same serving tree. Authors return these
    texts through their own route or embed them in their HTML; this helper
    creates no endpoint, cache, state or authentication exception. Read at
    mount, not registration, so a new page receives the installed UI source.
    """
    web_dir = resolve_web_dir(pathlib.Path(repo_dir))
    return {
        "css": (web_dir / "ui.css").read_text(encoding="utf-8"),
        "javascript": (web_dir / "modules" / "ui_primitives.js").read_text(encoding="utf-8"),
    }


class NoCacheStaticFiles:
    """Wrap StaticFiles to add Cache-Control: no-cache headers."""

    def __init__(self, **kwargs):
        self._app = StaticFiles(**kwargs)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            async def send_with_no_cache(message):
                if message["type"] == "http.response.start":
                    headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-cache, must-revalidate"))
                    message = {**message, "headers": headers}
                await send(message)

            await self._app(scope, receive, send_with_no_cache)
        else:
            await self._app(scope, receive, send)


def make_index_page(web_dir: pathlib.Path):
    async def index_page(_request) -> FileResponse | HTMLResponse:
        index = web_dir / "index.html"
        if index.exists():
            if _request.headers.get("X-Ouroboros-Telegram-MiniApp") == "1":
                # The authenticated sidecar proxy already stamps this presentation
                # hint. Bootstrap navigates to this document, so it needs its own SDK.
                html = index.read_text(encoding="utf-8")
                html = html.replace("<html", '<html data-ouroboros-host="telegram"', 1)
                html = html.replace("<head>", '<head><script async src="https://telegram.org/js/telegram-web-app.js"></script>', 1)
                return HTMLResponse(html)
            return FileResponse(str(index), media_type="text/html")
        return HTMLResponse("<html><body><h1>Ouroboros — web/ not found</h1></body></html>", status_code=404)

    return index_page
