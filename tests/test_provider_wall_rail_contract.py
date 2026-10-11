"""Contract tests for the no-repay provider rail (OB-01), pinned against the
accumulated_usage stamps the transport ACTUALLY leaves behind (reason_code
"llm_api_error" / "llm_empty_response"), not a bare marker dict:

1. A wall-exhausted provider death must terminalize with the typed
   ``reason_code="provider_unavailable"`` + ``infra_failed`` pair — exactly what
   fires the supervisor's "provider outage — NOT completed" owner notice
   (supervisor/events.py keys on that reason_code).
2. A scheduled Presence handoff remains addressable through its work_ref,
   while failure of the current turn retains its honest infrastructure outcome.
"""
import time
from types import SimpleNamespace

import ouroboros.loop as loop_mod
from ouroboros.loop_llm_call import RETRY_WALL_EXHAUSTED_KEY, call_llm_with_retry


class _TransientFailingLLM:
    def chat(self, **kwargs):
        error = Exception("upstream 502")
        error.status_code = 502
        raise error


def _rail_ctx(tmp_path, accumulated, tools=None):
    kwargs = dict(
        messages=[
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "PARTIAL RESULT."},
        ],
        llm=SimpleNamespace(), active_model="openai/gpt-5.5", active_effort="medium",
        max_retries=3, drive_logs=tmp_path, task_id="task-wall-contract",
        round_idx=7, event_queue=None, accumulated_usage=accumulated,
        task_type="task", active_use_local=False, max_rounds=200,
        drive_root=tmp_path,
    )
    if tools is not None:
        kwargs["tools"] = tools
    return loop_mod._RoundLimitContext(**kwargs)


def test_wall_exhausted_rail_carries_the_typed_provider_unavailable_reason(
    tmp_path, monkeypatch,
):
    """Use the REAL transport stamps: after a genuinely exhausted transient wall,
    accumulated carries reason_code="llm_api_error". The no-repay rail must still
    terminalize with the typed provider_unavailable + infra_failed pair, or the
    supervisor's provider-death owner notice never fires for the most common
    provider-death shape (transient retries exhausted)."""
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    accumulated = {}
    msg, _ = call_llm_with_retry(
        _TransientFailingLLM(), [{"role": "user", "content": "hi"}],
        "openai/gpt-5.5", None, "medium", 2, tmp_path, "task-wall-contract", 1,
        None, accumulated, "task", False,
    )
    assert msg is None and accumulated[RETRY_WALL_EXHAUSTED_KEY] is True
    monkeypatch.setattr(
        loop_mod, "_call_forced_model_once",
        lambda ctx: (_ for _ in ()).throw(AssertionError("no forced call")),
    )

    _text, usage, trace = loop_mod._handle_provider_unavailable(_rail_ctx(tmp_path, accumulated))

    assert trace["forced_finalization"]["source"] == "retry_wall_exhausted_no_repay"
    assert usage["execution_status"] == "infra_failed"
    assert usage["reason_code"] == "provider_unavailable"  # owner-notice trigger


def test_wall_exhausted_body_429_empty_is_infra_not_a_model_failure(tmp_path, monkeypatch):
    """The empty-response wall shape (body-429 with finish_reason="stop") leaves
    execution_status="failed"/reason_code="llm_empty_response" in accumulated; the
    no-repay rail must upgrade it to the typed infra pair — a rate-limited-out
    provider is an outage, not a model that answered badly."""
    monkeypatch.setattr(
        loop_mod, "_call_forced_model_once",
        lambda ctx: (_ for _ in ()).throw(AssertionError("no forced call")),
    )
    accumulated = {
        RETRY_WALL_EXHAUSTED_KEY: True,
        "execution_status": "failed",
        "reason_code": "llm_empty_response",
        "_last_llm_error_kind": "rate_limit",
    }

    _text, usage, _trace = loop_mod._handle_provider_unavailable(_rail_ctx(tmp_path, accumulated))

    assert usage["execution_status"] == "infra_failed"
    assert usage["reason_code"] == "provider_unavailable"


def test_scheduled_presence_handoff_survives_the_no_call_rail(tmp_path):
    """Presence keeps the admitted child without sending host-salvaged text."""
    from ouroboros.presence_runner import build_presence_result_event
    tools_ctx = SimpleNamespace(
        task_metadata={"presence": {"binding_id": "presence-binding"}},
        _presence_completion={"outcome": "deferred"},
        _swarm_handoff_attempt={"status": "scheduled", "task_id": "t-child-1"},
    )
    accumulated = {
        "_last_llm_error_kind": "provider_outcome_unknown",
        "execution_status": "infra_failed",
        "reason_code": "llm_api_error",
    }

    text, usage, _trace = loop_mod._handle_provider_unavailable(
        _rail_ctx(tmp_path, accumulated, tools=SimpleNamespace(_ctx=tools_ctx)),
    )

    assert text == "PARTIAL RESULT."
    assert usage["execution_status"] == "infra_failed"
    assert usage["reason_code"] == "provider_unavailable"
    terminal = build_presence_result_event(
        {"id": "presence-turn"}, text, tools_ctx,
        terminal_origin=usage["terminal_origin"],
        retain_scheduled_handoff=True,
    )
    assert terminal["work_ref"] == "t-child-1"
    assert terminal["outcome"] == "deferred"  # admitted work retains its polling custody after the parent failure
    assert usage["terminal_origin"] == "host_salvage"
    assert terminal["text"] == ""
