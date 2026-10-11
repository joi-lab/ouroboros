"""Main-loop subscription cache-affinity regressions."""

import queue

from ouroboros import config
from ouroboros.llm_claudexor import _request, cache_key_for_model
from ouroboros.loop_llm_call import call_llm_with_retry


def test_claudexor_request_projects_explicit_affinity_to_cache_key():
    payload = _request(
        {"source": "codex", "resolved_model": "model"},
        [{"role": "user", "content": "work"}],
        None,
        {"cache_affinity": "execution-7", "model_account_override": ""},
    )
    assert payload["options"]["cacheKey"] == "execution-7"


def test_cache_key_is_install_scoped_stable_and_header_safe(monkeypatch, tmp_path):
    key = cache_key_for_model("claudexor::codex=gpt-6-astra")
    assert key == cache_key_for_model("claudexor::codex=gpt-6-astra"), "same install + model -> same key"
    assert key.startswith("ouroboros-gpt-6-astra-")
    assert all(ch.isalnum() or ch in "._-" for ch in key), "must be a valid HTTP header value"
    assert key != cache_key_for_model("claudexor::codex=gpt-5.6-sol"), "one shard per model"
    assert cache_key_for_model("openrouter::openai/model") == ""
    assert cache_key_for_model("openai/gpt-5.6-sol") == ""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "other-install")
    assert cache_key_for_model("claudexor::codex=gpt-6-astra") != key, "another data root is another install"


def test_main_loop_shares_one_install_affinity_across_executions_on_claudexor(tmp_path):
    captured = []

    class LLM:
        def chat(self, **kwargs):
            captured.append(kwargs)
            return ({"content": "done", "tool_calls": [], "finish_reason": "stop"}, {
                "provider": "fixture", "resolved_model": kwargs["model"], "cost": 0.0,
                "prompt_tokens": 1, "completion_tokens": 1,
            })

    logs = tmp_path / "logs"
    logs.mkdir()
    for model, execution in (("claudexor::codex=model", "execution-7"), ("claudexor::codex=model", "execution-8"),
                             ("openrouter::openai/model", "execution-9")):
        usage = {"execution_id": execution}
        message, _cost = call_llm_with_retry(
            LLM(), [{"role": "user", "content": "work"}], model, None, "medium", 1,
            logs, "task", 1, queue.Queue(), usage,
        )
        assert message["content"] == "done"

    # Two executions of one install share the key, so the second one's first
    # round is served the governance prefix the first one already paid for.
    assert captured[0]["cache_affinity"] == cache_key_for_model("claudexor::codex=model")
    assert captured[1]["cache_affinity"] == captured[0]["cache_affinity"]
    assert "execution-7" not in captured[0]["cache_affinity"]
    assert captured[2]["cache_affinity"] == ""


def test_main_loop_keeps_legacy_claudexor_options_in_log_without_notice_state(tmp_path):
    """Legacy options_honored is retained as an opaque log fact, never notice state."""
    class LLM:
        def chat(self, **_kwargs):
            return ({"content": "done", "tool_calls": [], "finish_reason": "stop"}, {
                "provider": "claudexor", "resolved_model": "claudexor::codex=model",
                "cost": 0.0, "prompt_tokens": 1, "completion_tokens": 1,
                "claudexor": {"route": {"credentialProfileId": "account-a"},
                               "requested_options": {"reasoningEffort": "high"},
                               "applied_options": {"reasoningEffort": "medium"},
                               "options_honored": "mismatch"},
            })

    logs = tmp_path / "logs"
    logs.mkdir()
    usage = {"execution_id": "execution-7"}
    call_llm_with_retry(LLM(), [{"role": "user", "content": "work"}],
                        "claudexor::codex=model", None, "high", 1,
                        logs, "task", 1, queue.Queue(), usage)

    assert usage["_model_route"] == {"credentialProfileId": "account-a"}
    assert "_options" not in usage  # No second, stale copy for automatic chat notices.
    import json

    rounds = [json.loads(line) for line in (logs / "events.jsonl").read_text().splitlines()
              if json.loads(line).get("type") == "llm_round"]
    assert rounds[-1]["claudexor"]["requested_options"] == {"reasoningEffort": "high"}
    assert rounds[-1]["claudexor"]["applied_options"] == {"reasoningEffort": "medium"}
    assert rounds[-1]["claudexor"]["options_honored"] == "mismatch"
