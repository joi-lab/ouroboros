import asyncio
import pathlib


def test_multi_model_review_async_uses_llm_client_chat_async(monkeypatch, tmp_path):
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools import review as review_module

    calls = []

    class FakeLLMClient:
        def __init__(self, *args, **kwargs):
            pass

        async def chat_async(self, **kwargs):
            calls.append(kwargs)
            return (
                {"content": '[{"item":"check","verdict":"PASS","severity":"advisory","reason":"ok"}]'},
                {
                    "provider": "openrouter",
                    "resolved_model": "anthropic/claude-sonnet-4.6",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "cached_tokens": 7,
                    "cache_write_tokens": 2,
                    "prompt_cache_ttl": "default",
                    "cost": 0.01,
                },
            )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(review_module, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(review_module, "load_governance_doc", lambda *_a, **_k: "Bible")

    ctx = ToolContext(repo_dir=pathlib.Path(tmp_path), drive_root=pathlib.Path(tmp_path))
    result = asyncio.run(
        review_module._multi_model_review_async(
            "review target",
            "review instructions",
            ["anthropic/claude-sonnet-4.6"],
            ctx,
        )
    )

    assert calls
    assert calls[0]["model"] == "anthropic/claude-sonnet-4.6"
    # The review author supplies a host hint; the real client resolves it on
    # the effective route. This recording fake observes intent, not wire bytes.
    assert calls[0]["temperature"] is None
    assert calls[0]["default_temperature"] == 0.2
    assert result["results"][0]["cached_tokens"] == 7
    assert result["results"][0]["cache_write_tokens"] == 2
    assert result["results"][0]["prompt_cache_ttl"] == "default"
    assert ctx.pending_events
    usage_event = next(event for event in ctx.pending_events if event.get("type") == "llm_usage")
    assert usage_event["usage"]["cached_tokens"] == 7
    assert usage_event["usage"]["cache_write_tokens"] == 2
    assert usage_event["usage"]["prompt_cache_ttl"] == "default"


def test_multi_model_review_async_works_without_openrouter_for_official_openai(monkeypatch, tmp_path):
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools import review as review_module

    calls = []

    class FakeLLMClient:
        def __init__(self, *args, **kwargs):
            pass

        async def chat_async(self, **kwargs):
            calls.append(kwargs)
            return (
                {"content": '[{"item":"check","verdict":"PASS","severity":"advisory","reason":"ok"}]'},
                {
                    "provider": "openai",
                    "resolved_model": "openai/gpt-5.2",
                    "prompt_tokens": 12,
                    "completion_tokens": 6,
                    "cost": 0.02,
                },
            )

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OUROBOROS_MODEL", "openai::gpt-5.2")
    monkeypatch.setattr(review_module, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(review_module, "load_governance_doc", lambda *_a, **_k: "Bible")

    ctx = ToolContext(repo_dir=pathlib.Path(tmp_path), drive_root=pathlib.Path(tmp_path))
    result = asyncio.run(
        review_module._multi_model_review_async(
            "review target",
            "review instructions",
            ["openai::gpt-5.2"],
            ctx,
        )
    )

    assert calls
    assert calls[0]["model"] == "openai::gpt-5.2"
    assert result["results"][0]["model"] == "openai/gpt-5.2"
    usage_event = next(event for event in ctx.pending_events if event.get("type") == "llm_usage")
    assert usage_event["provider"] == "openai"
    assert usage_event["api_key_type"] == "openai"
