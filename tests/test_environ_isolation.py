"""The autouse os.environ snapshot in conftest closes the env-leak class.

Any test may mutate the environment (apply_settings_to_env, direct writes);
the fixture must hand the next test the exact pre-test environment back, on
the REAL os._Environ mapping (a plain-dict swap would sever the putenv sync
subprocesses inherit from). The contract is pinned by driving the fixture's
own generator directly, so the proof is self-contained and order-free.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from tests.conftest import restored_os_environ


def test_restored_os_environ_reverts_mutations_on_the_real_mapping():
    canary = "OURO_TEST_LEAK_CANARY"
    assert canary not in os.environ
    gen = restored_os_environ()
    next(gen)
    os.environ[canary] = "leaked"
    try:
        next(gen)
    except StopIteration:
        pass
    assert canary not in os.environ, "the snapshot must revert any mutation"
    assert type(os.environ).__name__ == "_Environ", (
        "restore must mutate the real mapping, never swap in a plain dict"
    )


def test_restored_os_environ_restores_deleted_and_changed_values():
    key = "OURO_TEST_LEAK_BASELINE"
    os.environ[key] = "original"
    try:
        gen = restored_os_environ()
        next(gen)
        os.environ[key] = "mutated"
        del os.environ[key]
        try:
            next(gen)
        except StopIteration:
            pass
        assert os.environ.get(key) == "original"
    finally:
        os.environ.pop(key, None)


def test_the_snapshot_is_registered_autouse_for_every_test(request):
    """Pins the WIRING (autouse=True on the conftest fixture), not the helper
    body: without it the generator above is correct and never runs."""
    assert "_os_environ_isolation" in request.fixturenames


@pytest.mark.parametrize("prior_stop", [False, True])
def test_service_fixture_isolates_real_panic_admission_latches(tmp_path, monkeypatch, request, prior_stop):
    from ouroboros import workspace_executor
    from ouroboros.tools import services
    from tests.conftest import _isolate_workspace_executor_globals

    assert "_isolate_workspace_executor_globals" in request.fixturenames
    for owner in (services, workspace_executor):
        monkeypatch.setattr(owner, "_panic_requested", prior_stop)
    isolated = contextmanager(_isolate_workspace_executor_globals.__wrapped__)
    for _ in range(2):
        with isolated():
            assert not services._panic_requested and not workspace_executor._panic_requested
            # The real request owner retires BOTH admissions even with empty registries.
            assert services.kill_all_services(tmp_path, request_only=True) == []
            assert services._panic_requested and workspace_executor._panic_requested
            assert "Emergency Stop" in services._start_service(None, ["unused"], name="after_panic")
        assert services._panic_requested is prior_stop
        assert workspace_executor._panic_requested is prior_stop
