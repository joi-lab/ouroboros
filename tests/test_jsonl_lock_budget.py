"""Append lock budgets preserve authority refusal and live-holder ownership."""
import json
import time
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("required", [False, True])
def test_zero_budget_keeps_required_lock_and_live_holder_semantics(tmp_path, monkeypatch, required):
    from ouroboros import platform_layer as pl
    from ouroboros.utils import append_jsonl, jsonl_append_lock_path

    path = tmp_path / "events.jsonl"
    lock = jsonl_append_lock_path(path)
    fd = pl.acquire_exclusive_file_lock(lock, timeout_sec=1.0)
    assert fd is not None
    original = pl._lock_identity(lock)
    sleeps = []
    clock = SimpleNamespace(**{name: getattr(time, name) for name in dir(time) if not name.startswith("__")})
    clock.sleep = lambda seconds: sleeps.append(seconds)
    monkeypatch.setattr(pl, "time", clock)
    try:
        answer = append_jsonl(path, {"event": "probe"}, require_lock=required, lock_timeout_sec=0.0)
        assert answer is (not required)
        assert sleeps == []
        assert pl._lock_identity(lock) == original
        if required:
            assert not path.exists()
        else:
            assert json.loads(path.read_text()) == {"event": "probe"}
    finally:
        pl.release_exclusive_file_lock(lock, fd)


def test_default_append_retains_its_lock_budget(tmp_path, monkeypatch):
    from ouroboros import platform_layer as pl
    from ouroboros.utils import append_jsonl

    original = pl.acquire_exclusive_file_lock
    budgets = []

    def observed(path, **kwargs):
        budgets.append(kwargs["timeout_sec"])
        return original(path, **kwargs)

    monkeypatch.setattr(pl, "acquire_exclusive_file_lock", observed)
    path = tmp_path / "events.jsonl"
    assert append_jsonl(path, {"event": "ordinary"})
    assert budgets == [2.0]
    assert json.loads(path.read_text()) == {"event": "ordinary"}
