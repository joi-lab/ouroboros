"""Concurrent free collection must preserve already-recorded producer facts."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
from types import SimpleNamespace

import pytest

from ouroboros import review_dispatch

pytestmark = pytest.mark.serial


def _actors(*states):
    return [{"slot_id": str(i), "operation_state": state}
            for i, state in enumerate(states)]


def _run(panel_id="panel"):
    return {"authority": "host_root", "panel_id": panel_id,
            "request": {"surface": "task_acceptance"}, "slot_roster": ["0", "1"],
            "actors": _actors("pending_dispatch", "pending_dispatch")}


def _collect(run, tmp_path):
    return review_dispatch.reconcile_pending_acceptance_runs(
        {"review_runs": [run]}, drive_root=tmp_path, usage_ctx={})


@pytest.mark.parametrize("newer_states", [
    ("settled", "settled"), ("pending_dispatch", "settled"),
])
def test_delayed_collection_preserves_newer_facts(monkeypatch, tmp_path, newer_states):
    run = _run()
    request = run["request"]
    held, release = Event(), Event()
    snapshots = []

    def collect(snapshot, **_kwargs):
        if current_thread().name.startswith("older-collector"):
            states = tuple(actor["operation_state"] for actor in snapshot["actors"])
            snapshots.append(states)
            if len(snapshots) == 1:
                result = SimpleNamespace(actors=_actors("settled", "pending_dispatch"),
                                         aggregate_signal="PENDING")
                held.set()
                assert release.wait(5), "newer collector did not finish"
                return result
            # Fresh collection keeps the settled second slot and adds the first.
            assert states == ("pending_dispatch", "settled")
            return SimpleNamespace(actors=_actors("settled", "settled"),
                                   aggregate_signal="PASS")
        return SimpleNamespace(actors=_actors(*newer_states),
                               aggregate_signal="PASS" if newer_states[0] == "settled" else "PENDING",
                               request={"must_not_replace": True}, panel_id="wrong-panel")

    monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run", collect)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="older-collector") as pool:
        older = pool.submit(_collect, run, tmp_path)
        try:
            assert held.wait(5), "older collector did not reach the barrier"
            newer_advanced = _collect(run, tmp_path)
        finally:
            release.set()
        older_advanced = older.result(timeout=5)

    assert [a["operation_state"] for a in run["actors"]] == ["settled", "settled"]
    assert run["aggregate_signal"] == "PASS"
    assert run["request"] is request
    assert run["panel_id"] == "panel"
    assert older_advanced == 1  # this caller may still owe settlement publication
    assert newer_advanced == (1 if newer_states[0] == "settled" else 0)
    assert len(snapshots) == (1 if newer_states[0] == "settled" else 2)


def test_collection_io_does_not_lock_unrelated_panels(monkeypatch, tmp_path):
    blocked, free = _run("blocked"), _run("free")
    held, release = Event(), Event()

    def collect(snapshot, **_kwargs):
        if snapshot["panel_id"] == "blocked":
            held.set()
            assert release.wait(5), "unrelated panel could not finish"
        return SimpleNamespace(actors=_actors("settled", "settled"))

    monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run", collect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        blocked_result = pool.submit(_collect, blocked, tmp_path)
        try:
            assert held.wait(5)
            assert pool.submit(_collect, free, tmp_path).result(timeout=3) == 1
        finally:
            release.set()
        assert blocked_result.result(timeout=5) == 1
