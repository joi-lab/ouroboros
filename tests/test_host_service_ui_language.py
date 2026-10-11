"""Host Service ``POST /ui/language``: a skill relays the owner's interface-language choice
through the ONE language writer (ui_i18n.choose_language) — the same seam the browser's
POST /api/ui/i18n/language uses — under the inject_chat grant."""
from __future__ import annotations

import json
import pathlib

from starlette.testclient import TestClient

from ouroboros import i18n_memory as memory
from ouroboros.gateway.host_service import create_host_service_app
from tests.test_host_service_api import FakeBridge, _seed_token
import pytest


@pytest.fixture(autouse=True)
def _own_settings_file(tmp_path, monkeypatch):
    """The language writer goes through the owner settings writer, which writes `config.SETTINGS_PATH`:
    point it at this test's root so no test leaves a settings.json in the session-wide data root."""
    import ouroboros.config as cfg

    monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "settings.json")


def _client(tmp_path: pathlib.Path, permissions=("inject_chat",)) -> TestClient:
    _seed_token(tmp_path, skill="telegram", token="token", permissions=list(permissions))
    return TestClient(create_host_service_app(tmp_path, bridge_getter=lambda: FakeBridge()))


def test_language_relay_writes_the_install_language_and_audits_the_skill(tmp_path, monkeypatch):
    from ouroboros import config
    from ouroboros.gateway import ui_i18n

    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "")
    events = []
    monkeypatch.setattr(ui_i18n, "_LANGUAGE_HOOKS", [lambda event, root, tag: events.append((event, tag))])
    client = _client(tmp_path)
    response = client.post("/ui/language", headers={"X-Skill-Token": "token"},
                           json={"language": "ru", "label": "Русский"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True and body["language"] == "ru" and body["profile"]["label"] == "Русский"
    assert config.load_settings()["OUROBOROS_UI_LANGUAGE"] == "ru"
    assert memory.memory_path(tmp_path, "ru").exists()
    assert ("language_set", "ru") in events
    rows = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    audit = [row for row in rows if row.get("action") == "ui_language"]
    assert audit and audit[-1]["client_host"] == "host_service:telegram" and audit[-1]["ui_language"] == "ru"


def test_language_relay_needs_the_inject_grant_and_a_json_object(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "")
    forbidden = _client(tmp_path, permissions=("subscribe_event",)).post(
        "/ui/language", headers={"X-Skill-Token": "token"}, json={"language": "ru"})
    assert forbidden.status_code == 403
    client = _client(tmp_path)
    assert client.post("/ui/language", headers={"X-Skill-Token": "token"}, json=["ru"]).status_code == 400
    bad = client.post("/ui/language", headers={"X-Skill-Token": "token", "content-type": "application/json"},
                      content=b"{not json")
    assert bad.status_code == 400


def test_language_relay_passes_the_writers_typed_refusals_through(tmp_path, monkeypatch):
    """A name without a credentialed light model is the generator's typed 400, not a 500."""
    from ouroboros import config

    monkeypatch.setenv("OUROBOROS_UI_LANGUAGE", "")
    before = config.load_settings().get("OUROBOROS_UI_LANGUAGE", "")
    client = _client(tmp_path)
    response = client.post("/ui/language", headers={"X-Skill-Token": "token"}, json={"language": "Klingon"})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "language_needs_model"
    assert config.load_settings().get("OUROBOROS_UI_LANGUAGE", "") == before, "a refusal writes nothing"
