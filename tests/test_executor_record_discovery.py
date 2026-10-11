"""Generic record discovery keeps retained roots and late service publication."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.serial
NAME = "workspace_executor_processes"


def _record(folder, name="service", kind="service"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name + ".json")
    path.write_text(json.dumps({
        "id": name, "schema_version": 1, "owner": "ouroboros_workspace_executor",
        "record_type": kind, "executor_type": "docker_exec", "executor_id": "fixture",
        "container_name": "never-contact-this-fixture", "backend_pid": "42",
        "backend_pidfile": "/tmp/ouroboros-exec-fixture.pid", "service_id": name,
    }), encoding="utf-8")
    return path


def test_discovery_matches_recursive_roots_and_directory_symlinks(tmp_path, monkeypatch):
    from ouroboros import workspace_executor as executor

    root = tmp_path / "data"
    state = root / "state"
    expected = {
        _record(state / NAME, "canonical"),
        _record(state / "headless_tasks/old/data/state" / NAME, "child"),
        _record(state / "arbitrary/.hidden/evolved" / NAME, "nonlayout"),
        _record(state / "arbitrary/.hidden/evolved" / NAME / "nested" / NAME, "nested"),
    }
    target = tmp_path / "record-target"
    _record(target, "linked")
    parent_target = tmp_path / "parent-target"
    _record(parent_target / "nested" / NAME, "not-descended")
    named = state / "linked" / NAME
    named.parent.mkdir()
    (state / "broken").mkdir()
    try:
        named.symlink_to(target, target_is_directory=True)
        (state / "parent-link").symlink_to(parent_target, target_is_directory=True)
        (state / "loop-link").symlink_to(state, target_is_directory=True)
        (state / "broken" / NAME).symlink_to(tmp_path / "absent", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this host")
    expected.add(named / "linked.json")
    monkeypatch.setattr(executor, "_FOREGROUND", {})
    assert {path for path, row in executor._iter_process_records(root)} == expected
    # Same public matching contract as the prior generic discovery, including
    # records nested inside a matching root and the matching symlink itself.
    previous = {path for folder in state.rglob(NAME) if folder.is_dir()
                for path in folder.glob("*.json")}
    assert previous == expected


def test_discovery_does_not_stat_a_candidate_in_every_unrelated_directory(tmp_path, monkeypatch):
    from ouroboros import workspace_executor as executor

    root = tmp_path / "data"
    expected = _record(root / "state" / NAME)
    monkeypatch.setattr(executor, "_FOREGROUND", {})
    original = Path.stat
    candidates = []

    def stat(path, *args, **kwargs):
        if path.name == NAME:
            candidates.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    assert [path for path, row in executor._iter_process_records(root)] == [expected]
    baseline = len(candidates)
    for index in range(100):
        (root / "state" / "payload" / str(index) / "cache").mkdir(parents=True)
    candidates.clear()
    assert [path for path, row in executor._iter_process_records(root)] == [expected]
    assert len(candidates) == baseline


def test_service_cleanup_rediscovers_a_record_published_after_foreground_scan(tmp_path, monkeypatch):
    from ouroboros import workspace_executor as executor

    root = tmp_path / "data"
    _record(root / "state" / NAME, "foreground", "foreground")
    monkeypatch.setattr(executor, "_FOREGROUND", {})
    monkeypatch.setattr(executor, "_SERVICES", {})
    calls = []

    def stop(row, *, wait):
        calls.append(row["id"])
        if row["record_type"] == "foreground":
            _record(root / "state" / "arbitrary/late/owner" / NAME, "late-service")
        return True

    monkeypatch.setattr(executor, "_kill_docker_record", stop)
    monkeypatch.setattr(executor, "_retire_docker_completion", lambda path: True)
    assert len(executor.kill_all_foreground(root, wait=False)) == 1
    stopped = executor.kill_all_services(root, wait=False)
    assert calls == ["foreground", "late-service"]
    assert [row["service_id"] for row in stopped] == ["late-service"]
    assert not list((root / "state").rglob("*.json"))
