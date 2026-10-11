"""Regression tests for the host-service rate limiter (#27/#30) and its token bucket.

The `_RateLimiter._hits` defaultdict previously grew unbounded: keys (one per
`{skill}:{endpoint}`) were never deleted, only their stale timestamps popped. A
periodic sweep now frees keys that have gone idle past the window, without
changing any rate-limit decision.

The WS relay lane (`allow_burst`) is a token bucket — a 60-message burst reserve
refilling one message per second — whose refusals are aggregated per burst and
reported once through the `on_burst_end` sink.
"""

from __future__ import annotations

import time

from ouroboros.gateway.host_service import WS_RELAY_BURST, WS_RELAY_REFILL_PER_SEC, _RateLimiter


def test_sweep_frees_idle_keys():
    rl = _RateLimiter(limit=10, window_sec=0.01)
    for i in range(5):
        assert rl.allow(f"k{i}") is True
    assert len(rl._hits) == 5

    # Sweep with a timestamp well past the window → every key is now idle/empty.
    rl._sweep(time.monotonic() + 1.0)
    assert len(rl._hits) == 0


def test_allow_triggers_amortized_sweep():
    rl = _RateLimiter(limit=10, window_sec=0.01)
    for i in range(5):
        rl.allow(f"k{i}")
    assert len(rl._hits) == 5

    # Make the existing keys stale and mark a sweep overdue, then a single allow()
    # must reclaim the idle keys (only the new key remains).
    rl._last_sweep = time.monotonic() - 100
    time.sleep(0.02)  # > window_sec, so k0..k4 timestamps are stale
    assert rl.allow("fresh") is True
    assert set(rl._hits.keys()) == {"fresh"}


def test_rate_limit_decisions_unchanged():
    rl = _RateLimiter(limit=3, window_sec=60.0)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False  # 4th hit within the window is blocked
    # A different key has its own independent budget.
    assert rl.allow("other") is True


def test_swept_key_is_recreated_cleanly():
    rl = _RateLimiter(limit=2, window_sec=0.01)
    rl.allow("k")
    rl._sweep(time.monotonic() + 1.0)
    assert "k" not in rl._hits
    # Re-using a swept key works (defaultdict recreates it); budget is fresh.
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False


def test_burst_reserve_admits_capacity_then_refills_one_per_second():
    ended: list[tuple[str, int, float]] = []
    rl = _RateLimiter(on_burst_end=lambda key, dropped, duration: ended.append((key, dropped, duration)))
    assert WS_RELAY_BURST == 60 and WS_RELAY_REFILL_PER_SEC == 1.0
    for _ in range(WS_RELAY_BURST):
        assert rl.allow_burst("s:ws")["allowed"] is True
    refused = rl.allow_burst("s:ws")
    assert refused["allowed"] is False and refused["dropped_in_burst"] == 1
    assert 0 < refused["retry_after_sec"] <= 1.0
    assert rl.allow_burst("s:ws")["dropped_in_burst"] == 2
    # Half a second later there is still no whole token.
    rl._buckets["s:ws"][1] -= 0.5
    assert rl.allow_burst("s:ws")["allowed"] is False
    # A full second after the last refill exactly one message passes, which
    # closes the burst and reports its aggregate ONCE.
    rl._buckets["s:ws"][1] -= 0.5
    assert rl.allow_burst("s:ws")["allowed"] is True
    assert len(ended) == 1 and ended[0][0] == "s:ws" and ended[0][1] == 3
    fresh = rl.allow_burst("s:ws")
    assert fresh["allowed"] is False
    assert fresh["dropped_in_burst"] == 1, "a new burst starts counting from one"
    # The sliding-window lanes are untouched by the bucket.
    assert rl.allow("s:inject") is True
    assert "s:ws" not in rl._hits


def test_burst_reserve_keys_are_independent_and_idle_buckets_are_swept():
    ended: list[tuple[str, int, float]] = []
    rl = _RateLimiter(window_sec=0.01, on_burst_end=lambda *args: ended.append(args))
    for _ in range(WS_RELAY_BURST):
        rl.allow_burst("a:ws")
    assert rl.allow_burst("a:ws")["allowed"] is False
    assert rl.allow_burst("b:ws")["allowed"] is True, "another skill keeps its own reserve"
    # The skill stops sending after the refusal: the sweep (any lane's admission,
    # once per window) drops the refilled bucket and still reports the burst.
    rl._buckets["a:ws"][1] -= WS_RELAY_BURST / WS_RELAY_REFILL_PER_SEC
    rl._last_sweep = time.monotonic() - 100
    assert rl.allow("c:inject") is True
    assert "a:ws" not in rl._buckets
    assert [(key, dropped) for key, dropped, _duration in ended] == [("a:ws", 1)]


def test_burst_sink_failure_never_breaks_admission():
    def _boom(*_args):
        raise RuntimeError("sink down")

    rl = _RateLimiter(on_burst_end=_boom)
    for _ in range(WS_RELAY_BURST + 1):
        rl.allow_burst("s:ws")
    rl._buckets["s:ws"][1] -= 1.0
    assert rl.allow_burst("s:ws")["allowed"] is True
