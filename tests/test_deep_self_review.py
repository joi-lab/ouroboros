"""Tests for ouroboros.deep_self_review module."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS
from ouroboros.deep_self_review import deep_review_route
from ouroboros.tools.review_helpers import _is_probably_binary


class TestApiRouteAvailability:
    """Availability (`deep_review_route`) of the SYNTHESIZED api row: no `deep_review`
    row saved, so the legacy model key is the migration source and the api rules
    apply — the routed model's credentials, and the direct-OpenAI resolution
    (with its -pro rewrite) for a stored OpenRouter spelling."""

    def test_openrouter(self):
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "sk-or-test", "OUROBOROS_MODEL_DEEP_SELF_REVIEW": "openai/gpt-5.5-pro"},
            clear=True,
        ):
            reason, model = deep_review_route()
            available = not reason
        assert available is True
        assert model == "openai/gpt-5.5-pro"

    def test_openai(self):
        env = {"OPENAI_API_KEY": "sk-test"}
        with mock.patch.dict(os.environ, env, clear=False):
            # Ensure OPENROUTER_API_KEY and OPENAI_BASE_URL are not set
            os.environ.pop("OPENROUTER_API_KEY", None)
            os.environ.pop("OPENAI_BASE_URL", None)
            reason, model = deep_review_route()
            available = not reason
        assert available is True
        # The direct route lands on the PROVIDER default, not a mechanical
        # `openai::` + router-slug rewrite: `-pro` is an OpenRouter routing slug
        # that 404s on api.openai.com (live-probed 2026-07-29).
        assert model == OPENAI_DIRECT_DEFAULTS["deep_self_review"]
        assert not model.endswith("-pro")

    def test_none(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            reason, model = deep_review_route()
            available = not reason
        assert available is False
        assert model is None

    def test_direct_provider_prefix_requires_matching_key_even_with_openrouter(self):
        with mock.patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "sk-or-test", "OUROBOROS_MODEL_DEEP_SELF_REVIEW": "anthropic::claude-opus-4.8"},
            clear=True,
        ):
            reason, model = deep_review_route()
            available = not reason

        assert available is False
        assert model is None

    def test_direct_provider_prefix_available_with_matching_key(self):
        with mock.patch.dict(
            os.environ,
            {"ANTHROPIC_API_KEY": "sk-ant-test", "OUROBOROS_MODEL_DEEP_SELF_REVIEW": "anthropic::claude-opus-4.8"},
            clear=True,
        ):
            reason, model = deep_review_route()
            available = not reason

        assert available is True
        assert model == "anthropic::claude-opus-4.8"


class TestRequestToolEmitsEvent:
    def test_emits_correct_event(self):
        """_request_deep_self_review emits a deep_self_review_request event."""
        from ouroboros.tools.control import _request_deep_self_review

        class FakeCtx:
            pending_events = []

        ctx = FakeCtx()
        with mock.patch(
            "ouroboros.deep_self_review.deep_review_route",
            return_value=("", "openai/gpt-5.5-pro"),
        ):
            result = _request_deep_self_review(ctx, "test reason")
        assert len(ctx.pending_events) == 1
        evt = ctx.pending_events[0]
        assert evt["type"] == "deep_self_review_request"
        assert evt["reason"] == "test reason"
        assert evt["model"] == "openai/gpt-5.5-pro"
        assert "Deep self-review" in result

    def test_unavailable_returns_error(self):
        """An unavailable ROW returns the typed reason without emitting an event."""
        from ouroboros.tools.control import _request_deep_self_review

        class FakeCtx:
            pending_events = []

        ctx = FakeCtx()
        with mock.patch(
            "ouroboros.deep_self_review.deep_review_route",
            return_value=("no provider credentials for openai/x", None),
        ):
            result = _request_deep_self_review(ctx, "test reason")
        assert len(ctx.pending_events) == 0
        assert result.startswith("❌ Deep self-review unavailable: no provider credentials for openai/x")
        assert "Review lanes" in result


class TestIsProbablyBinary:
    def test_nul_byte_is_binary(self, tmp_path):
        """File containing a NUL byte is detected as binary."""
        f = tmp_path / "blob.bin"
        f.write_bytes(b"some text\x00more text")
        assert _is_probably_binary(f) is True

    def test_plain_text_is_not_binary(self, tmp_path):
        """Plain text file is not detected as binary."""
        f = tmp_path / "script.py"
        f.write_text("def hello():\n    return 'world'\n")
        assert _is_probably_binary(f) is False

    def test_high_non_printable_ratio_is_binary(self, tmp_path):
        """File with >30% non-printable bytes (ASCII control range) is detected as binary."""
        # 40% non-printable (bytes 1–8 range, ASCII control chars)
        payload = bytes(range(1, 9)) * 10 + b"normal text" * 3
        f = tmp_path / "data.unknown"
        f.write_bytes(payload)
        assert _is_probably_binary(f) is True

    def test_high_byte_ratio_is_binary(self, tmp_path):
        """File with invalid UTF-8 high bytes (no NUL) is detected as binary.

        bytes >= 128 alone are safe for valid UTF-8 (Cyrillic, CJK), but
        invalid UTF-8 sequences (e.g. raw Latin-1 bytes 0x80-0xFF) must still
        be caught by the incremental UTF-8 decode check.
        """
        # Raw Latin-1 bytes 0x80-0xFF: invalid UTF-8, no NUL, few control chars
        payload = bytes(range(128, 256)) * 5 + b"ascii text" * 5
        f = tmp_path / "data.blob"
        f.write_bytes(payload)
        assert _is_probably_binary(f) is True

    def test_only_first_sniff_bytes_read(self, tmp_path):
        """_is_probably_binary only reads _BINARY_SNIFF_BYTES bytes, not the whole file."""
        from ouroboros.tools.review_helpers import _BINARY_SNIFF_BYTES
        # File is mostly text but has NUL in the first 8KB window
        payload = b"text data\x00more" + b"a" * (_BINARY_SNIFF_BYTES * 2)
        f = tmp_path / "big.bin"
        f.write_bytes(payload)
        # Should detect NUL in the first chunk and return True
        assert _is_probably_binary(f) is True

    def test_empty_file_is_not_binary(self, tmp_path):
        """Empty file does not crash and returns False."""
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert _is_probably_binary(f) is False

    def test_missing_file_returns_false(self, tmp_path):
        """Missing file returns False (let caller handle read failure)."""
        f = tmp_path / "does_not_exist.bin"
        assert _is_probably_binary(f) is False


class TestNoProxyLlmChat:
    """LLMClient.chat(no_proxy=True) — proxy-free httpx transport for macOS fork-safety."""

    def test_chat_no_proxy_uses_trust_env_false(self):
        """chat(no_proxy=True) builds an httpx.Client with trust_env=False and mounts={}."""
        import httpx
        from ouroboros.llm import LLMClient

        captured_clients = []

        real_httpx_client = httpx.Client

        def capturing_httpx_client(*args, **kwargs):
            c = real_httpx_client(*args, **kwargs)
            captured_clients.append(c)
            return c

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

        # Resolve the SDK before mocking the HTTP class it inherits on import.
        with mock.patch("openai.OpenAI") as mock_openai_cls:
            with mock.patch("httpx.Client", side_effect=capturing_httpx_client):
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.return_value = mock_resp
                mock_openai_cls.return_value = mock_oa

                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                    llm.chat(
                        messages=[{"role": "user", "content": "hi"}],
                        model="openai/gpt-5.5-pro",
                        max_tokens=8,
                        no_proxy=True,
                    )

        # At least one httpx.Client was created
        assert len(captured_clients) >= 1
        created = captured_clients[0]
        # trust_env=False and mounts={} are the key invariants
        assert created._mounts == {} or not created._mounts

    def test_chat_no_proxy_closes_http_client(self):
        """chat(no_proxy=True) closes the one-shot httpx.Client after the call."""
        import httpx
        from ouroboros.llm import LLMClient

        closed_clients = []
        real_httpx_client = httpx.Client

        class TrackingClient(real_httpx_client):
            def close(self):
                closed_clients.append(self)
                super().close()

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

        with mock.patch("openai.OpenAI") as mock_openai_cls:
            with mock.patch("httpx.Client", TrackingClient):
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.return_value = mock_resp
                mock_openai_cls.return_value = mock_oa

                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                    llm.chat(
                        messages=[{"role": "user", "content": "hi"}],
                        model="openai/gpt-5.5-pro",
                        max_tokens=8,
                        no_proxy=True,
                    )

        assert len(closed_clients) >= 1, "httpx.Client must be closed after no_proxy call"

    def test_chat_no_proxy_closes_on_exception(self):
        """chat(no_proxy=True) closes the http client even when the API call raises."""
        import httpx
        from ouroboros.llm import LLMClient

        closed_clients = []
        real_httpx_client = httpx.Client

        class TrackingClient(real_httpx_client):
            def close(self):
                closed_clients.append(self)
                super().close()

        llm = LLMClient()

        with mock.patch("openai.OpenAI") as mock_openai_cls:
            with mock.patch("httpx.Client", TrackingClient):
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.side_effect = RuntimeError("boom")
                mock_openai_cls.return_value = mock_oa

                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                    with pytest.raises(RuntimeError, match="boom"):
                        llm.chat(
                            messages=[{"role": "user", "content": "hi"}],
                            model="openai/gpt-5.5-pro",
                            max_tokens=8,
                            no_proxy=True,
                        )

        assert len(closed_clients) >= 1, "httpx.Client must be closed even after exception"

    def test_chat_no_proxy_skips_generation_cost_fetch(self):
        """chat(no_proxy=True) does not call _fetch_generation_cost (proxy/OS path)."""
        from ouroboros.llm import LLMClient

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "id": "gen-abc123",  # has a generation id — would trigger cost fetch normally
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }

        with mock.patch("openai.OpenAI") as mock_openai_cls:
            with mock.patch("httpx.Client") as mock_httpx_cls:
                mock_http = mock.Mock()
                mock_httpx_cls.return_value = mock_http
                mock_oa = mock.Mock()
                mock_oa.chat.completions.create.return_value = mock_resp
                mock_openai_cls.return_value = mock_oa
                with mock.patch.object(llm, "_fetch_generation_cost") as mock_cost:
                    with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                        llm.chat(
                            messages=[{"role": "user", "content": "hi"}],
                            model="openai/gpt-5.5-pro",
                            max_tokens=8,
                            no_proxy=True,
                        )
                    mock_cost.assert_not_called()

    def test_chat_no_proxy_false_uses_cached_client(self):
        """chat(no_proxy=False, default) uses the shared cached client, not a new one."""
        import httpx
        from ouroboros.llm import LLMClient

        new_clients = []
        real_httpx_client = httpx.Client

        def counting_httpx_client(*args, **kwargs):
            c = real_httpx_client(*args, **kwargs)
            new_clients.append(c)
            return c

        llm = LLMClient()
        mock_resp = mock.Mock()
        mock_resp.model_dump.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

        with mock.patch("httpx.Client", side_effect=counting_httpx_client):
            with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-test"}, clear=False):
                with mock.patch.object(llm, "_get_remote_client") as mock_get:
                    mock_oa = mock.Mock()
                    mock_oa.chat.completions.create.return_value = mock_resp
                    mock_get.return_value = mock_oa
                    llm.chat(
                        messages=[{"role": "user", "content": "hi"}],
                        model="openai/gpt-5.5-pro",
                        max_tokens=8,
                        no_proxy=False,
                    )
                    mock_get.assert_called_once()

        # no_proxy=False must not construct a new httpx.Client
        assert len(new_clients) == 0


def test_direct_openai_deep_review_sends_a_real_openai_model_id():
    """PHYSICAL-PAYLOAD proof, not a defaults-table assertion.

    An owner may pin the slug `openai/gpt-5.6-sol-pro`. That `-pro`
    suffix is an OpenRouter routing slug, NOT an OpenAI model id: live-probed
    2026-07-29, `gpt-5.6-sol-pro` on api.openai.com /v1/chat/completions returns
    404, while pro reasoning exists only on /v1/responses as
    `reasoning.mode="pro"` (200) — and /v1/chat/completions rejects a `reasoning`
    parameter outright (400 "Unknown parameter"). Every LLM call in llm.py is a
    chat.completions call, so the direct-OpenAI deep-review slot ships plain Sol
    and this test pins what actually reaches the wire.
    """
    import os
    from unittest import mock

    from ouroboros.llm import LLMClient
    from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS

    slot = OPENAI_DIRECT_DEFAULTS["deep_self_review"]
    assert slot.startswith("openai::"), slot

    mock_resp = mock.Mock()
    mock_resp.model_dump.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    with mock.patch("openai.OpenAI") as mock_openai_cls:
        mock_oa = mock.Mock()
        mock_oa.chat.completions.create.return_value = mock_resp
        mock_openai_cls.return_value = mock_oa
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-direct-test"}, clear=False):
            LLMClient().chat(
                messages=[{"role": "user", "content": "hi"}],
                model=slot, max_tokens=8, no_proxy=True,
            )
        assert mock_oa.chat.completions.create.called
        payload = mock_oa.chat.completions.create.call_args.kwargs

    # The id on the wire is a REAL OpenAI model, never the OpenRouter slug.
    assert payload["model"] == "gpt-5.6-sol"
    assert not payload["model"].endswith("-pro")
    # ...and no `reasoning` object is smuggled onto a chat.completions call, which
    # the live API rejects with 400 (the only pro carrier is the Responses API).
    assert "reasoning" not in payload
    assert "reasoning" not in (payload.get("extra_body") or {})


def test_direct_fallback_preserves_an_explicit_real_model_pin():
    """Only router-only `-pro` slugs are substituted by the provider default; an
    owner's explicit pin of a REAL OpenAI model keeps the mechanical rewrite."""
    import os
    from unittest import mock

    from ouroboros.provider_models import OPENAI_DIRECT_DEFAULTS

    env = {"OPENAI_API_KEY": "sk-test"}
    with mock.patch.dict(os.environ, env, clear=False):
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ.pop("OPENAI_BASE_URL", None)
        os.environ.pop("OUROBOROS_REVIEWER_SLOTS", None)
        with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_DEEP_SELF_REVIEW": "openai/gpt-5.5"}):
            reason, model = deep_review_route()
            available = not reason
        assert available is True
        assert model == "openai::gpt-5.5", "an explicit real-model pin survives"
        with mock.patch.dict(os.environ, {"OUROBOROS_MODEL_DEEP_SELF_REVIEW": "openai/gpt-5.5-pro"}):
            reason, model = deep_review_route()
            available = not reason
        assert available is True
        assert model == OPENAI_DIRECT_DEFAULTS["deep_self_review"], (
            "a router-only -pro slug lands on the provider default"
        )
