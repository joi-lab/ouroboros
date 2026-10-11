"""The authenticated Telegram SPA retains its presentation SDK after bootstrap."""
import pathlib

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.server_web import make_index_page


def test_telegram_marker_only_changes_the_host_document():
    web = pathlib.Path(__file__).resolve().parents[1] / "web"
    with TestClient(Starlette(routes=[Route("/", make_index_page(web))])) as client:
        plain = client.get("/")
        assert plain.content == (web / "index.html").read_bytes()
        marked = client.get("/", headers={"X-Ouroboros-Telegram-MiniApp": "1"})
        assert marked.status_code == 200
        assert '<html data-ouroboros-host="telegram"' in marked.text
        assert '<script async src="https://telegram.org/js/telegram-web-app.js"></script>' in marked.text
        assert 'data-ouroboros-host' not in plain.text
        assert client.get("/", headers={"X-Ouroboros-Telegram-MiniApp": "0"}).content == plain.content
