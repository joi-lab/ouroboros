"""Complete binary run transport, separate from the diagnostic preview."""

import hashlib
import json

import httpx
import pytest

from ouroboros.gateways.claudexor import ClaudexorGateway, ClaudexorUnavailable, DaemonEndpoint


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False

    def __iter__(self):
        yield from self.chunks
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


def gateway(handler):
    client = ClaudexorGateway(DaemonEndpoint("127.0.0.1", 1, "test-token"))
    client._client.close()
    client._client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler))
    return client


def test_large_binary_stream_is_complete_and_verified(tmp_path):
    chunk = bytes(range(256)) * 4096
    stream = Chunks([chunk] * 6)
    expected = {"sizeBytes": len(chunk) * 6,
                "sha256": "sha256:" + hashlib.sha256(chunk * 6).hexdigest()}
    with gateway(lambda req: httpx.Response(200, stream=stream)) as client:
        path = tmp_path / "video.bin"
        with path.open("wb") as sink:
            actual = client.stream_run_artifact("run-a", "final/files/video.bin", sink, expected=expected)
    assert path.stat().st_size == expected["sizeBytes"]
    assert actual["sha256"] == expected["sha256"][7:]
    assert stream.closed


@pytest.mark.parametrize("status", [200, 403])
def test_incomplete_stream_preserves_received_http_status(tmp_path, status):
    stream = Chunks([b"partial"], httpx.ReadError("connection ended"))
    with gateway(lambda req: httpx.Response(status, stream=stream)) as client:
        with (tmp_path / "unpublished").open("wb") as sink:
            with pytest.raises(ClaudexorUnavailable) as failure:
                client.stream_run_artifact("run-a", "final/files/result", sink)
    assert failure.value.status_code == status
    assert failure.value.observation_timeout is (status == 200)
    assert stream.closed


def test_stream_rejects_changed_manifest_bytes(tmp_path):
    with gateway(lambda req: httpx.Response(200, content=b"changed")) as client:
        with (tmp_path / "unpublished").open("wb") as sink:
            with pytest.raises(ClaudexorUnavailable, match="differs") as failure:
                client.stream_run_artifact("run-a", "final/files/result", sink,
                                          expected={"size": 7, "sha256": "0" * 64})
    assert failure.value.code == "artifact_integrity_error"


def test_apply_retains_identity_and_selection_without_automatic_retry():
    calls = []
    body = {"target": {"kind": "original_project"}, "mode": "apply", "paths": ["report.pdf"]}

    def handle(request):
        calls.append(request)
        assert request.headers["Idempotency-Key"] == "existing-apply-intent"
        assert json.loads(request.content) == body
        return httpx.Response(200, json={"applied": True, "deliveryStatus": "applied"})

    with gateway(handle) as client:
        result = client.apply_run("run-a", body, idempotency_key="existing-apply-intent")
    assert result["deliveryStatus"] == "applied"
    assert len(calls) == 1
    assert calls[0].url.path == "/v2/runs/run-a/apply"
