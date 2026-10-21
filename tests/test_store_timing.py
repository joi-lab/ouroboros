"""Store transactions report their lock-hold seconds per store kind, in memory; the hook never raises."""
from __future__ import annotations

import time

import pytest

from ouroboros import dialogue_store, store, store_timing, usage_store
from ouroboros.task_results import load_task_result, write_task_result


@pytest.fixture(autouse=True)
def clean_window():
    store.close_connections()
    store_timing.drain()
    yield
    store_timing.drain()
    store.close_connections()


def test_the_window_counts_holds_per_kind_and_side_and_drain_resets_it(tmp_path):
    write_task_result(tmp_path, "t", "running")
    assert load_task_result(tmp_path, "t")["status"] == "running"
    with dialogue_store.transaction(tmp_path, write=True):
        pass
    with usage_store.hold(tmp_path, write=True):
        pass
    with pytest.raises(RuntimeError, match="refused"):  # a rolled-back hold counts too
        with store.open_store(tmp_path, write=True):
            raise RuntimeError("refused")
    window = store_timing.drain()
    assert {"current", "dialog", "usage"} <= set(window)
    assert window["current"]["write"]["count"] >= 2 and window["current"]["read"]["count"] >= 1
    assert window["dialog"]["write"]["count"] >= 1 and window["usage"]["write"]["count"] >= 1
    for sides in window.values():
        for bucket in sides.values():
            assert bucket["count"] >= 1 and 0 <= bucket["max_sec"] <= bucket["total_sec"] and bucket["max_site"]
            assert bucket["max_site"].split(":")[0] not in {"ouroboros.store", "ouroboros.task_store", "contextlib"}
    assert store_timing.drain() == {}
    with store.open_store(tmp_path, write=True):
        time.sleep(0.07)  # the window's longest hold names the frame that opened it (a Windows tick's margin)
    longest = store_timing.drain()["current"]["write"]
    assert longest["max_sec"] >= 0.05 and longest["max_site"].startswith("tests.test_store_timing:test_the_window")


def test_a_failure_inside_the_hook_never_reaches_the_transaction(tmp_path, monkeypatch):
    def broken():
        raise RuntimeError("site walk failed")

    with monkeypatch.context() as patch:
        patch.setattr(store_timing, "_site", broken)
        write_task_result(tmp_path, "t", "running")  # the commit stands
        with pytest.raises(RuntimeError, match="refused"):  # so does the caller's own exception
            with store.open_store(tmp_path, write=True):
                raise RuntimeError("refused")
    assert load_task_result(tmp_path, "t")["status"] == "running"
    window = store_timing.drain()
    assert window["current"]["write"]["count"] >= 2 and window["current"]["write"]["max_site"] == ""
    write_task_result(tmp_path, "t", "completed")  # the healthy hook records the site again
    assert store_timing.drain()["current"]["write"]["max_site"]


def test_a_clock_too_coarse_to_see_a_short_hold_still_names_its_site(tmp_path, monkeypatch):
    """Windows' ``time.monotonic`` ticks at ~15.6 ms: a hold read by such a clock measures 0.0 and must still name its
    site, as the window's longest hold so far."""
    monkeypatch.setattr(store_timing, "clock", lambda: 1000.0)  # every hold measures 0.0
    write_task_result(tmp_path, "t", "running")
    with dialogue_store.transaction(tmp_path, write=True):
        pass
    window = store_timing.drain()
    for kind in ("current", "dialog"):
        bucket = window[kind]["write"]
        assert bucket["count"] >= 1 and bucket["max_sec"] == 0.0 and bucket["max_site"], (kind, bucket)
