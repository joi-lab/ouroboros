"""Maintenance discovery returns the sweep while preserving packer lifetime and packing authority."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from ouroboros import evidence_packer as packer, evidence_packs as packs, store, store_switch
from tests.test_evidence_packs import REPO, _FakeChild, _task, root as root

pytestmark = pytest.mark.serial


def _join(unit):
    unit._thread.join(10)
    assert not unit._thread.is_alive()


def test_reconcile_releases_its_latch_during_real_stamp_discovery(root, monkeypatch):
    from ouroboros import server_maintenance as maintenance

    _task(root, "candidate", blobs=1)
    unit = packer.EvidencePacker()
    assert not unit.tick(root, REPO)
    entered, release = threading.Event(), threading.Event()
    original = packer._stamp
    calls = []

    def stamp(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(packer, "_stamp", stamp)
    monkeypatch.setattr(packer, "packer", unit)
    monkeypatch.setattr(maintenance, "DATA_DIR", root)
    monkeypatch.setattr(maintenance, "_stop_requested", lambda *_: False)
    monkeypatch.setattr(maintenance, "_periodic_zombie_reconcile", lambda **_: None)
    monkeypatch.setattr(maintenance, "_run_drive_custody_pass", lambda *_: None)
    monkeypatch.setattr(maintenance, "_step_recovered", calls.append)
    for name in ("_STARTUP_PRUNES_OWED", "_STARTUP_TREES_OWED", "_STARTUP_SOURCE_PRUNES_OWED", "_STARTUP_TEMP_SWEEP_OWED"):
        monkeypatch.setattr(maintenance, name, [False])
    monkeypatch.setattr("supervisor.workspace_preparation.recover_interrupted_preparations", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.late_review_marks.replay_owed_late_reviews", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.review_operation.collect_orphaned_operations_softly", lambda *a, **k: None)
    spawns = []
    report = json.dumps({"status": "completed", **dict.fromkeys(packer._COUNTERS, 0)}).encode()
    monkeypatch.setattr(packer, "spawn_maintenance_job", lambda *a: spawns.append(a) or _FakeChild(report))
    latch, marker, finished = threading.Lock(), [0.0], threading.Event()
    latch.acquire()
    sweep = threading.Thread(target=lambda: (maintenance._run_periodic_reconcile_sweep(marker, latch=latch), finished.set()))
    sweep.start()
    try:
        assert entered.wait(5)
        assert finished.wait(1), "candidate discovery still occupies the reconcile pass"
        assert not latch.locked() and marker[0] > 0 and calls == ["reconcile_sweep"]
        assert not unit.tick(root, REPO), "overlapping discovery admitted"
        assert unit.live_children() == [unit._thread]
        unit.stop()
        assert unit.live_children() == [unit._thread], "Stop is not evidence of reader exit"
    finally:
        release.set()
        sweep.join(10)
        _join(unit)
    assert unit.live_children() == []
    assert spawns == [], "discovery spawned after Stop"


@pytest.mark.parametrize("count", [1, 128])
def test_changed_discovery_short_circuits_but_quiet_install_still_scans(root, monkeypatch, count):
    for n in range(count):
        _task(root, f"task-{n:04}", blobs=1)
    # Publish a valid cache before measuring; no packing child required to read it.
    with packs.open_index(root):
        pass
    calls, original = [], packer._stamp

    def stamp(*args, **kwargs):
        calls.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(packer, "_stamp", stamp)
    started, cpu = time.perf_counter(), time.process_time()
    assert packer.packing_pending(root, lambda: False)
    assert len(calls) == 1
    first = {"elapsed": time.perf_counter() - started, "cpu": time.process_time() - cpu}
    current = packer.changed_stamps(root)
    for task, value in current.items():
        packer.record_swept(root, task, value)
    calls.clear()
    started, cpu = time.perf_counter(), time.process_time()
    assert not packer.packing_pending(root, lambda: False)
    assert len(calls) == count
    print(json.dumps({"tasks": count, "changed_stamps": 1, "quiet_stamps": count,
                      "first": first, "quiet_elapsed": time.perf_counter() - started,
                      "quiet_cpu": time.process_time() - cpu}))


def test_archive_only_skips_task_discovery_and_spawns_existing_child(root, monkeypatch):
    archive = root / packs.USAGE_ARCHIVE_REL
    archive.mkdir(parents=True)
    (archive / "segment_ep1_0001.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(packer, "_loose_task_ids", lambda *a: pytest.fail("archive-only needs no task census"))
    calls = []
    report = json.dumps({"status": "completed", **dict.fromkeys(packer._COUNTERS, 0)}).encode()
    monkeypatch.setattr(packer, "spawn_maintenance_job", lambda *a: calls.append(a) or _FakeChild(report))
    unit = packer.EvidencePacker()
    assert not unit.tick(root, REPO)
    assert unit.tick(root, REPO)
    _join(unit)
    assert len(calls) == 1


def test_discovery_does_not_rebuild_a_missing_index(root, monkeypatch):
    _task(root, "candidate", blobs=1)
    packs.packs_dir(root).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(packs, "_rebuild_index", lambda *a: pytest.fail("discovery rebuilt cache"))
    assert packer.packing_pending(root, lambda: False)
    assert not (packs.packs_dir(root) / packs.INDEX_NAME).exists()


@pytest.mark.parametrize("busy", ["facts", "packs", "packs_sql", "packs_sql_cached", "money"])
def test_real_contention_defers_discovery_without_waiting_or_spawning(root, monkeypatch, busy):
    from ouroboros import usage_store

    _task(root, "candidate", blobs=1)
    with packs.open_index(root):
        pass
    if busy == "packs_sql_cached":
        with packs.open_index(root):
            pass
        assert str(packs.packs_dir(root) / packs.INDEX_NAME) in packs._VALIDATED
    if busy == "money":
        with usage_store.hold(root, write=True):
            pass
    spawns = []
    report = json.dumps({"status": "completed", **dict.fromkeys(packer._COUNTERS, 0)}).encode()
    monkeypatch.setattr(packer, "spawn_maintenance_job", lambda *a: spawns.append(a) or _FakeChild(report))
    unit = packer.EvidencePacker()
    assert not unit.tick(root, REPO)
    if busy in {"facts", "packs_sql", "packs_sql_cached", "money"}:
        path = {"facts": root / "state/current.sqlite", "money": root / usage_store.STORE_REL,
                "packs_sql": packs.packs_dir(root) / packs.INDEX_NAME}[busy.removesuffix("_cached")]
        conn = sqlite3.connect(path, timeout=0)
        conn.execute("BEGIN EXCLUSIVE")
        try:
            assert unit.tick(root, REPO)
            _join(unit)
        finally:
            conn.rollback()
            conn.close()
    else:
        with store.name_lock(root, packs.INDEX_LOCK_NAME, timeout_sec=0):
            assert unit.tick(root, REPO)
            _join(unit)
    unit.stop()
    assert not unit.live_children() and spawns == []


def test_stop_during_directory_discovery_does_not_read_stores(root, monkeypatch):
    _task(root, "candidate", blobs=1)
    stopped = threading.Event()
    original = packer._loose_task_ids

    def ids(*args):
        result = original(*args)
        stopped.set()
        return result

    monkeypatch.setattr(packer, "_loose_task_ids", ids)
    monkeypatch.setattr(store, "open_store", lambda *a, **k: pytest.fail("read after Stop"))
    assert not packer.packing_pending(root, stopped.is_set)


def test_actual_exit_consumers_keep_blocked_discovery_visible_then_accept_exit(root, monkeypatch):
    import server
    from ouroboros import body_adoption
    from tests.test_body_adoption_consumers import EXITED
    from tests.body_candidate_support import make_serving, make_ctx, rich_commit, restart_receipt, stub_adoption_evidence
    from ouroboros import body_candidate, body_switch

    stub_adoption_evidence(monkeypatch)  # this test exits maintenance, not the test battery
    serving = make_serving(root.parent / "body")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    # These are disposable repositories/data, never the installation.
    monkeypatch.setenv("OUROBOROS_SUBAGENT_WORKTREE_ROOT", str(root.parent / "copies"))
    ctx = make_ctx(serving, root, "maintenance-adoption")
    body_candidate.prepare(ctx)
    commit = rich_commit(ctx.repo_dir)
    body_candidate.record_reviewed_commit(ctx, commit)
    body_adoption.authorize(ctx, commit, reason="test maintenance exit")
    restart_receipt(root, commit, "test maintenance exit")
    assert body_adoption.bind_restart(lambda **_: (True, "ok"), root, "test maintenance exit")()[0]
    _task(root, "candidate", blobs=1)
    unit = packer.EvidencePacker()
    entered, release = threading.Event(), threading.Event()
    original = packer._stamp

    def stamp(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(packer, "_stamp", stamp)
    monkeypatch.setattr(packer, "packer", unit)
    spawns = []
    report = json.dumps({"status": "completed", **dict.fromkeys(packer._COUNTERS, 0)}).encode()
    monkeypatch.setattr(packer, "spawn_maintenance_job", lambda *a: spawns.append(a) or _FakeChild(report))
    monkeypatch.setattr(server, "DATA_DIR", root)
    monkeypatch.setattr(server, "begin_owned_stop", lambda *_: None)
    monkeypatch.setattr(server, "stop_owned_work", lambda *_: {"state": "completed", "unconfirmed": []})
    monkeypatch.setattr(server, "_stop_owned_daemon_for_new_pin", lambda: None)
    monkeypatch.setattr("supervisor.workers.kill_workers", lambda **_: None)
    monkeypatch.setattr("supervisor.workers.last_worker_exit_census", lambda: EXITED)
    monkeypatch.setattr("multiprocessing.active_children", lambda: [])
    monkeypatch.setattr("ouroboros.extension_companion.panic_kill_all", lambda: None)
    monkeypatch.setattr(server, "_restart_requested", threading.Event())
    monkeypatch.setattr(server, "_owner_restart_requested", threading.Event())
    server._restart_requested.set()
    assert not unit.tick(root, REPO)
    assert unit.tick(root, REPO)
    try:
        assert entered.wait(5)
        cleanup = server._emergency_process_cleanup()
        assert cleanup["live_children"] == [unit._thread]
        assert body_adoption.read(root)["phase"] == "authorized"
        assert not pathlib_pointer(serving).exists()
        store_switch.record_switch_intent(root, target_sha="target", target_ref="target", reason="test", door="ui_rollback")
        assert store_switch.switch_at_restart(root, serving, cleanup)["reason"] == "quiescence_unconfirmed"
    finally:
        release.set()
        _join(unit)
    assert spawns == [], "discovery spawned after Stop"
    handoff = body_adoption.read(root)
    handoff["restart_bound"] = True
    body_switch.write_handoff(str(body_adoption.helper_dir(root)), handoff)
    cleanup = server._emergency_process_cleanup()
    assert not cleanup["live_children"]
    assert body_adoption.read(root)["phase"] == "armed"
    assert pathlib_pointer(serving).exists()
    switches = []
    monkeypatch.setattr(store_switch, "switch_needs", lambda *a: {"handles": False, "money": False, "facts": False})
    monkeypatch.setattr(store_switch, "rev_parse", lambda *a: "target")
    monkeypatch.setattr(store_switch, "switching_owner", lambda *_: lambda intent: (switches.append(intent) or True, "switched"))
    # A refused switch withdraws its intent; only a new addressed switch requests it again.
    store_switch.record_switch_intent(root, target_sha="target", target_ref="target", reason="test retry", door="ui_rollback")
    assert store_switch.switch_at_restart(root, serving, cleanup)["status"] == "switched"
    assert len(switches) == 1


def pathlib_pointer(serving):
    import pathlib
    from ouroboros import body_switch
    return pathlib.Path(body_switch.pointer_path(str(serving)))
