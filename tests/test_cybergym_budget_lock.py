"""Budget admission stays exclusive when the caller cannot import fcntl."""

import builtins

import pytest

from devtools.benchmarks.cybergym import cybergym_adapter
from ouroboros.platform_layer import file_lock_exclusive_nb, file_unlock


def test_budget_ledger_lock_excludes_another_handle_without_local_fcntl(monkeypatch, tmp_path):
    native_import = builtins.__import__

    def import_without_local_fcntl(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "fcntl" and (globals or {}).get("__name__") == cybergym_adapter.__name__:
            raise ImportError("fcntl is unavailable to the platform-neutral caller")
        return native_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_local_fcntl)
    ledger = cybergym_adapter.BudgetLedger(tmp_path / "claims.jsonl", cap_usd=20)
    with ledger._lock():
        with (tmp_path / "claims.jsonl.lock").open("a+", encoding="utf-8") as contender:
            with pytest.raises(BlockingIOError):
                file_lock_exclusive_nb(contender.fileno())
    with (tmp_path / "claims.jsonl.lock").open("a+", encoding="utf-8") as contender:
        file_lock_exclusive_nb(contender.fileno())
        file_unlock(contender.fileno())
