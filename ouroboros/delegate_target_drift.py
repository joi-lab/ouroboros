"""Durable, read-only authority-tree drift evidence for delegated captures."""

from __future__ import annotations

import pathlib
import os
import subprocess
from typing import Any, Dict, List


def _target_drift_evidence(entry: Any) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {"checked": False, "paths": [], "error": ""}
    target = pathlib.Path(str(entry.target_root or "")).resolve(strict=False)
    baseline = str(entry.baseline_sha or "").strip()
    if str(getattr(entry, "authority_source", "") or "") == "skill_payload":
        return {"checked": True, "paths": [], "error": "", "not_applicable": "skill_payload"}
    if not baseline:
        evidence["error"] = "delegated baseline is missing"
        return evidence
    if not (target / ".git").exists():
        evidence["error"] = f"authority target is not a Git worktree: {target}"
        return evidence
    try:
        git_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
        def git_error(proc, fallback):
            detail = proc.stderr or proc.stdout or b""
            return (detail.decode("utf-8", "replace") if isinstance(detail, bytes) else str(detail)).strip() or fallback

        baseline_files = subprocess.run(
            ["git", "ls-tree", "-r", "-z", "--name-only", baseline],
            cwd=str(target), capture_output=True, check=False, env=git_env,
        )
        if baseline_files.returncode != 0:
            evidence["error"] = git_error(baseline_files, f"git ls-tree exited {baseline_files.returncode}")
            return evidence
        baseline_paths = {
            item.decode("utf-8", errors="surrogateescape")
            for item in baseline_files.stdout.split(b"\0") if item
        }
        from ouroboros.subagent_worktrees import find_execution_snapshot
        # Provisioning and capture use the snapshot registry's data root. The
        # custody ledger may live on a different canonical budget drive.
        snapshot = find_execution_snapshot(getattr(entry, "snapshot_id", "")) or {}
        excluded = {
            str(row.get("path")) for row in snapshot.get("excluded_untracked", [])
            if isinstance(row, dict) and row.get("path")
        }
        identity_sets = [
            snapshot.get("file_baseline") if isinstance(snapshot.get("file_baseline"), dict) else {},
            snapshot.get("untracked_baseline") if isinstance(snapshot.get("untracked_baseline"), dict) else {},
        ]
        excluded_rows = snapshot.get("excluded_untracked", [])
        from ouroboros.workspace_file_outputs import _matches, _side
        baseline_untracked = set(excluded)
        for identities in identity_sets:
            baseline_untracked.update(identities)
            for relative, before in identities.items():
                if not isinstance(before, dict):
                    evidence["error"] = f"baseline identity unavailable for {relative}"
                    return evidence
                if not _matches(_side(target / relative), before, exact_mode=True):
                    evidence["paths"].append(relative)
        for row in excluded_rows:
            if not isinstance(row, dict) or not row.get("path"):
                continue
            if row.get("reason") == "nested_repository" and not isinstance(row.get("baseline"), dict):
                continue
            relative, before = str(row["path"]), row.get("baseline")
            if not isinstance(before, dict):
                evidence["error"] = f"baseline identity unavailable for excluded path {relative}"
                return evidence
            if not _matches(_side(target / relative), before, exact_mode=True):
                evidence["paths"].append(relative)
        diff = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--no-renames", baseline, "--"],
            cwd=str(target), capture_output=True, check=False, env=git_env,
        )
        if diff.returncode != 0:
            evidence["error"] = git_error(diff, f"git diff exited {diff.returncode}")
            return evidence
        evidence["paths"].extend(
            item.decode("utf-8", errors="surrogateescape")
            for item in diff.stdout.split(b"\0")
            if item and item.decode("utf-8", errors="surrogateescape") not in baseline_untracked
        )
        untracked = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard"],
            cwd=str(target), capture_output=True, check=False, env=git_env,
        )
        if untracked.returncode != 0:
            evidence["error"] = (untracked.stderr or b"").decode("utf-8", "replace").strip() or \
                                  f"git ls-files --others exited {untracked.returncode}"
            return evidence
        evidence["paths"].extend(
            item.decode("utf-8", errors="surrogateescape")
            for item in untracked.stdout.split(b"\0") if item
            and item.decode("utf-8", errors="surrogateescape") not in baseline_paths
            and item.decode("utf-8", errors="surrogateescape") not in baseline_untracked
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        return evidence
    evidence["checked"] = True
    evidence["paths"] = sorted(set(evidence["paths"]))
    return evidence


def _target_drift_paths(entry: Any) -> List[str]:
    evidence = _target_drift_evidence(entry)
    return list(evidence.get("paths") or []) if evidence.get("checked") else []


def _persist_target_drift(manifest_path: pathlib.Path, manifest: Dict[str, Any],
                         evidence: Dict[str, Any]) -> Dict[str, Any]:
    from ouroboros.utils import atomic_write_json, utc_now_iso

    updated = dict(manifest)
    updated["authority_drift_source_status"] = str(manifest.get("status") or "")
    updated["authority_drift"] = {
        "checked": bool(evidence.get("checked")),
        "paths": list(evidence.get("paths") or []),
        "error": str(evidence.get("error") or ""),
        "checked_at": utc_now_iso(),
    }
    updated["authority_drift_status"] = (
        "unknown" if evidence.get("error") else "changed" if evidence.get("paths") else "clean")
    atomic_write_json(manifest_path, updated, trailing_newline=True)
    return updated
