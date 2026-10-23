"""The provider usage/cost normalization every paid attempt settles through (``usage_accounting.usage_from_response``).

Moved verbatim out of the retired skill-review usage suite: these two contracts are the ledger's, not a surface's.
"""

from __future__ import annotations

from ouroboros import usage_accounting as ua


def test_api_token_normalization_preserves_missing_zero_and_body_error_contracts():
    missing, cost, final = ua.usage_from_response({"choices": [{"message": {"content": "ok"}}]})
    assert (missing["prompt_tokens"], missing["completion_tokens"]) == (None, None)
    assert (missing["cached_tokens"], missing["cache_write_tokens"]) == (None, None)
    assert cost is None and final is False

    cache_only, cost, final = ua.usage_from_response({"usage": {"cached_tokens": 7}})
    assert cache_only["prompt_tokens"] is None and cache_only["cached_tokens"] == 7
    assert cost is None and final is False

    generic_mixed, cost, final = ua.usage_from_response({
        "usage": {"input_tokens": 100, "cached_tokens": 40},
    })
    assert generic_mixed["prompt_tokens"] == 100 and generic_mixed["cached_tokens"] == 40
    assert cost is None and final is False

    anthropic, cost, final = ua.usage_from_response({
        "usage": {"input_tokens": 3, "cache_read_input_tokens": 7},
    })
    assert anthropic["prompt_tokens"] == 10 and anthropic["cached_tokens"] == 7
    assert cost is None and final is False

    zeros, cost, final = ua.usage_from_response({
        "usage": {
            "prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens": 0, "cache_write_tokens": 0,
        },
    })
    assert (
        zeros["prompt_tokens"], zeros["completion_tokens"],
        zeros["cached_tokens"], zeros["cache_write_tokens"],
    ) == (0, 0, 0, 0)
    assert cost is None and final is False

    rejected, cost, final = ua.usage_from_response({
        "error": {"code": 429, "message": "rate limited"}, "usage": None,
    })
    assert (rejected["prompt_tokens"], rejected["completion_tokens"]) == (None, None)
    assert cost is None and final is False

    rejected, cost, final = ua.usage_from_response({
        "error": {"code": 429, "message": "rate limited"},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
    })
    assert (rejected["prompt_tokens"], rejected["completion_tokens"]) == (0, 0)
    assert cost == 0.0 and final is True

    for usage, field in (
        ({"cached_tokens": 7}, "cached_tokens"),
        ({"cache_write_tokens": 7}, "cache_write_tokens"),
        ({"prompt_tokens_details": {"cached_tokens": 7}}, "cached_tokens"),
        ({"cache_creation": {"ephemeral_5m_input_tokens": 7}}, "cache_write_tokens_by_ttl"),
    ):
        rejected_cache, cost, final = ua.usage_from_response({
            "error": {"code": 429}, "usage": usage,
        })
        assert rejected_cache["prompt_tokens"] is None
        assert rejected_cache["completion_tokens"] is None
        assert rejected_cache[field]
        assert cost is None and final is False


def test_api_cost_extraction_rejects_untrustworthy_provider_amounts():
    """The AUTHORITATIVE cost boundary: whatever survives `usage_from_response` is
    what the durable attempt ledger settles as money.

    `_number` previously rejected only non-finite values, so a JSON `true` passed
    `float()` and `math.isfinite()` and settled as a FINAL $1.00 — real budget
    admission spent on an amount no provider ever reported — and a negative amount
    settled as a final credit. Both are unknown now, applying the same predicate
    `loop_llm_call._provider_cost_value` applies to the projection lane. Both lanes
    must reject, or an invalid cost still reaches the ledger.
    """
    for bad in (
        True, False, -5, -0.01, float("nan"), float("inf"), float("-inf"),
        "abc", object(), [1.0], {"usd": 1.0},
    ):
        _normalized, cost, final = ua.usage_from_response({"usage": {"cost": bad}})
        assert cost is None, f"{bad!r} must never settle as money"
        assert final is False

    for good, expected in ((0.0, 0.0), (0, 0.0), (1.23, 1.23), ("2.5", 2.5)):
        _normalized, cost, final = ua.usage_from_response({"usage": {"cost": good}})
        assert cost == expected and final is True

    # Rejecting one field does not abandon the candidate chain: the next reported
    # amount is still consulted, exactly as it is when `cost` is simply absent.
    _normalized, cost, final = ua.usage_from_response(
        {"usage": {"cost": True, "total_cost": 2.0}},
    )
    assert cost == 2.0 and final is True
