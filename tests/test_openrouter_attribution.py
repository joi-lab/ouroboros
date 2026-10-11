from __future__ import annotations

import urllib.request

from ouroboros.openrouter_attribution import OPENROUTER_APP_HEADERS
from scripts.run_external_review import _probe_model_for_key


def test_external_review_probe_uses_canonical_app_attribution(monkeypatch):
    import httpx

    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"choices": [{"message": {"content": "pong"}}]}

    def post(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(httpx, "post", post)

    assert _probe_model_for_key("secret", "openai/gpt-5.6-sol")[0] is True
    assert {
        key: captured["headers"][key] for key in OPENROUTER_APP_HEADERS
    } == OPENROUTER_APP_HEADERS


def test_gaia_judge_uses_canonical_app_attribution(monkeypatch):
    import devtools.benchmarks.gaia.audit_leakage as audit

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"verdict\\":\\"clean\\",\\"rationale\\":\\"ok\\"}"}}]}'

    def urlopen(req, timeout=0):
        captured["headers"] = req.headers
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    result = audit._judge("sample", "answer", [], "openai/gpt-5.6-luna", "secret")
    assert result["verdict"] == "clean"
    actual_headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert all(
        actual_headers.get(key.lower()) == value
        for key, value in OPENROUTER_APP_HEADERS.items()
    )
