"""Administrative closure retains uncertain charges and accepts one late receipt."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time

import pytest

from ouroboros import usage_accounting as ua
from ouroboros import usage_compaction as compaction
from ouroboros.transport_custody import ProviderNotDispatched, release_pre_dispatch_attempt


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    return root


def reserve(root):
    value = ua.reserve_attempt(ua.AttemptRequest(
        model="fixture", provider="openai", reservation_usd=1.25,
        drive_root=root, task_id="child", root_task_id="parent"))
    ua.mark_dispatched(value)
    return value


def rows(root):
    with ua._locked(root):
        return ua._read_records_locked_cached(root)


def test_late_receipt_replaces_the_bound_once(root):
    attempt = reserve(root)
    ua.mark_unresolved(attempt, "response lost")
    assert ua.terminalize_abandoned_attempt(attempt, reason="owner_task_terminal") == "settled"
    unknown = ua.usage_projection(root)
    assert unknown["accounted_usd"] == 1.25 and unknown["confirmed_usd"] == 0
    assert unknown["cost_final"] is False and unknown["unknown_unmetered"] == 1
    usage = {"prompt_tokens": 10, "completion_tokens": 2}
    ua.settle_attempt(attempt, usage, cost_usd=.3, cost_final=True)
    before = rows(root)
    ua.settle_attempt(attempt, usage, cost_usd=.3, cost_final=True)
    assert rows(root) == before
    final = ua.usage_breakdown(root)
    assert final["accounted_usd"] == final["confirmed_usd"] == .3
    assert final["physical_calls"] == 1 and final["cost_final"] is True
    with pytest.raises(ua.UsageAccountingError, match="conflicting usage settlement"):
        ua.settle_attempt(attempt, usage, cost_usd=.4, cost_final=True)
    assert rows(root) == before


def test_carried_bound_and_observed_revision_are_authoritative(root):
    attempt = reserve(root)
    observed = rows(root)[-1]["seq"]
    ua.mark_unresolved(attempt, "newer evidence")
    assert ua.terminalize_abandoned_attempt(attempt, reason="dead", expected_seq=observed) == "unresolved"
    assert len(rows(root)) == 3
    forged_bound = replace(attempt, reservation_upper_bound_usd=99)
    assert ua.terminalize_abandoned_attempt(forged_bound, reason="dead") == "settled"
    assert ua.usage_projection(root)["accounted_usd"] == 1.25
    assert ua.terminalize_abandoned_attempt(forged_bound, reason="again") == "settled"
    assert len(rows(root)) == 4


def test_real_settlement_wins_race_with_administrative_close(root, monkeypatch):
    attempt = reserve(root)
    entered, release = threading.Event(), threading.Event()
    append = ua._append_rows_locked

    def held_append(root, records, additions, **kwargs):
        if additions[0].get("cost_usd") == .2:
            entered.set()
            assert release.wait(5)
        return append(root, records, additions, **kwargs)

    monkeypatch.setattr(ua, "_append_rows_locked", held_append)
    with ThreadPoolExecutor(2) as pool:
        actual = pool.submit(ua.settle_attempt, attempt, cost_usd=.2, cost_final=True)
        try:
            assert entered.wait(5)
            abandoned = pool.submit(ua.terminalize_abandoned_attempt, attempt, reason="owner ended")
        finally:
            release.set()
        actual.result(5)
        assert abandoned.result(5) == "settled"
    assert len(rows(root)) == 3
    assert ua.usage_projection(root)["confirmed_usd"] == .2


def test_late_never_started_proof_releases_without_a_physical_call(root):
    attempt = reserve(root)
    ua.terminalize_abandoned_attempt(attempt, reason="owner ended")
    assert release_pre_dispatch_attempt(attempt, ProviderNotDispatched("engine proves not_started"))
    projection = ua.usage_breakdown(root)
    assert projection["accounted_usd"] == 0 and projection["physical_calls"] == 0
    assert projection["cost_final"] is True


def test_late_receipt_remains_writable_after_real_compaction(root, monkeypatch):
    for _ in range(20):
        ua.settle_attempt(reserve(root), cost_usd=.01, cost_final=True)
    attempt = reserve(root)
    ua.terminalize_abandoned_attempt(attempt, reason="owner ended")
    monkeypatch.setattr(compaction, "_fold_clock", lambda: time.time() + 1_000_000)
    with ua._locked(root) as heartbeat:
        assert compaction.compact_usage_ledger_locked(root, heartbeat=heartbeat)
    assert any(row["kind"] == "usage_baseline" for row in rows(root))
    ua.settle_attempt(attempt, {"prompt_tokens": 4}, cost_usd=.1, cost_final=True)
    final = ua.usage_breakdown(root)
    assert final["confirmed_usd"] == .3 and final["physical_calls"] == 21
    assert final["cost_final"] is True
