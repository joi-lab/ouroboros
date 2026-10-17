"""Worker recovery uses the same identity ledger as other supervised processes."""

from types import SimpleNamespace

import pytest

from ouroboros import process_custody as custody
from ouroboros.server_maintenance import _startup_worker_pids
from supervisor import worker_pool_lifecycle, workers


@pytest.mark.parametrize("same_session,matches", [(False, True), (False, False), (True, True)])
def test_worker_registration_and_restart_reaping(tmp_path, monkeypatch, same_session, matches):
    proc = SimpleNamespace(pid=111, is_alive=lambda: True)
    slot = workers.Worker(wid=0, proc=proc, in_q=None)
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "WORKERS", {0: slot})
    monkeypatch.setattr(custody, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(custody, "process_start_time_legacy", lambda pid: "observed birth")
    monkeypatch.setattr(custody, "process_start_time", lambda pid: "observed birth")
    monkeypatch.setattr(custody, "_live_cmd_sha256", lambda pid: "fake-command-digest")
    worker_pool_lifecycle._record_worker_processes()
    assert not (tmp_path / "state/worker_pids.json").exists()
    assert _startup_worker_pids(tmp_path) == {111}
    readable, entries = custody._read_ledger_strict(tmp_path)
    assert readable and len(entries) == 1
    assert entries[0]["purpose"] == "worker:0" and entries[0]["scope"] == "session"
    assert slot.process_birth == "observed birth"
    if not same_session:
        monkeypatch.setattr(custody, "_SESSION_ID", "next-generation")
    monkeypatch.setattr(custody, "pid_is_alive", lambda pid: True)
    monkeypatch.setattr(custody, "pid_is_zombie", lambda pid: False)
    monkeypatch.setattr(custody, "_fingerprint_matches", lambda row: matches)
    killed = []
    monkeypatch.setattr(custody, "kill_process_group_id", lambda pid, **kw: killed.append(pid))
    reaped = custody.reap_orphaned_processes(tmp_path)
    assert reaped == killed == ([111] if matches and not same_session else [])


def test_corrupt_worker_ledger_never_proves_empty_ownership(tmp_path):
    assert _startup_worker_pids(tmp_path) == set()
    path = custody.ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken\n", encoding="utf-8")
    assert _startup_worker_pids(tmp_path) is None
