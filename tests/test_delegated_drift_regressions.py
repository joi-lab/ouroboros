"""Focused authority-drift regressions kept outside the size-ratcheted isolation module."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ouroboros import delegate_custody as custody
from ouroboros.subagent_worktrees import provision_execution_snapshot, find_execution_snapshot
from test_delegated_run_isolation import _isolated_entry, _nanny_ctx, _seed_target


def test_drift_capture_freezes_evidence_and_safe_reject_releases_empty_snapshot(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch
    from ouroboros.tools.subagent_integration import _integrate_delegated_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapDrift")
    entry = _isolated_entry(ctx, target, handle, run_id="run-drift")
    (target / "neighbor.txt").write_text("owner change\n", encoding="utf-8")

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_mutated_during_run"] == ["neighbor.txt"]
    manifest_path = custody.delegated_capture_dir(custody.custody_root(ctx), "t-nanny", "snapDrift") / "workspace_patch.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["authority_drift"]["paths"] == ["neighbor.txt"]
    (target / "neighbor.txt").unlink()

    replay = _capture_terminal_patch(ctx, entry)
    assert replay["status"] == "ready_no_changes"
    assert replay["target_mutated_during_run"] == ["neighbor.txt"]
    assert "Rejected delegated run" in _integrate_delegated_patch(
        ctx, "run-drift", "reject", "preserve for inspection")
    assert find_execution_snapshot("snapDrift") is None
    custody._CUSTODY.clear()


def test_excluded_untracked_baseline_drift_is_not_clean(tmp_path, monkeypatch):
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapSecretDrift")
    entry = _isolated_entry(ctx, target, handle, run_id="run-secret-drift")
    assert any(row.get("path") == ".env" and row.get("baseline") for row in handle.excluded_untracked)
    (target / ".env").write_text("SECRET=changed\n", encoding="utf-8")

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_mutated_during_run"] == [".env"]
    assert entry.patch_captured is True
    custody._CUSTODY.clear()


def test_nested_untracked_git_repository_does_not_break_snapshot_provision(tmp_path):
    target = _seed_target(tmp_path)
    nested = target / "vendor" / "inner"
    nested.mkdir(parents=True)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=nested, check=True)
    (nested / "README").write_text("nested\n", encoding="utf-8")

    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nested", snapshot_id="snapNested")

    assert Path(handle.path).exists()
    assert any(row.get("reason") == "nested_repository" for row in handle.excluded_untracked)


def test_drift_uses_snapshot_registry_when_custody_root_differs(tmp_path, monkeypatch):
    from ouroboros.delegate_target_drift import _target_drift_evidence
    from ouroboros.tools.delegate import _capture_terminal_patch

    target = _seed_target(tmp_path)
    ctx = _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(
        target_root=target, task_id="t-nanny", snapshot_id="snapSeparateRoots")
    entry = _isolated_entry(ctx, target, handle, run_id="run-separate-roots")
    assert find_execution_snapshot(handle.snapshot_id) is not None
    assert find_execution_snapshot(handle.snapshot_id, data_dir=entry.ledger_root) is None

    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_no_changes"
    assert capture["target_drift_checked"] is True
    assert capture.get("target_mutated_during_run", []) == []
    (target / "untracked.txt").write_text("changed\n", encoding="utf-8")
    assert _target_drift_evidence(entry)["paths"] == ["untracked.txt"]
    custody._CUSTODY.clear()
