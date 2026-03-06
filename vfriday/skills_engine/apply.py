"""Deterministic skill application pipeline."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from vfriday.skills_engine.backup import clear_backup, create_backup, restore_backup
from vfriday.skills_engine.manifest import SkillManifest, read_manifest
from vfriday.skills_engine.merge import copy_file, merge_file
from vfriday.skills_engine.state import base_dir, compute_file_hash, init_skills_state, load_state, record_skill_application
from vfriday.skills_engine.structured import apply_structured_ops


@dataclass(frozen=True)
class ApplyResult:
    """Outcome returned by apply_skill."""

    success: bool
    skill: str
    version: str
    conflict_files: List[str]
    structured_outcomes: Dict[str, Any]
    message: str


def _project_rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _check_dependencies(manifest: SkillManifest, state: Dict[str, Any]) -> None:
    applied = {str(x.get("name")) for x in (state.get("applied_skills") or [])}
    missing = [dep for dep in manifest.depends if dep not in applied]
    if missing:
        raise ValueError(f"skill_missing_dependencies:{','.join(missing)}")


def _check_conflicts(manifest: SkillManifest, state: Dict[str, Any]) -> None:
    applied = {str(x.get("name")) for x in (state.get("applied_skills") or [])}
    bad = [name for name in manifest.conflicts if name in applied]
    if bad:
        raise ValueError(f"skill_conflicts_with:{','.join(bad)}")


def _run_shell(command: str, cwd: Path) -> None:
    proc = subprocess.run(command, cwd=str(cwd), shell=True, capture_output=True, text=True)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"command_failed:{command}:{msg}")


def apply_skill(project_root: Path, skill_dir: Path) -> ApplyResult:
    """Apply a skill package with deterministic flow and rollback safety."""
    root = Path(project_root).resolve()
    sdir = Path(skill_dir).resolve()
    state = init_skills_state(root)
    manifest = read_manifest(sdir)
    _check_dependencies(manifest, state)
    _check_conflicts(manifest, state)

    bdir = base_dir(root)
    conflict_files: List[str] = []

    files_to_backup = set()
    for rel in manifest.adds + manifest.modifies:
        files_to_backup.add((root / rel).resolve())
    files_to_backup.add((root / ".env.example").resolve())
    files_to_backup.add((root / "requirements.txt").resolve())

    create_backup(root, files_to_backup)
    try:
        for rel in manifest.adds:
            src = sdir / "add" / rel
            dst = root / rel
            if not src.exists():
                raise FileNotFoundError(f"skill_add_file_missing:{src}")
            copy_file(src, dst)

        for rel in manifest.modifies:
            current = root / rel
            base = bdir / rel
            theirs = sdir / "modify" / rel
            if not theirs.exists():
                raise FileNotFoundError(f"skill_modify_file_missing:{theirs}")

            if not current.exists():
                copy_file(theirs, current)
                continue

            if not base.exists():
                copy_file(current, base)

            result = merge_file(current=current, base=base, theirs=theirs)
            if result.conflicted:
                conflict_files.append(rel)

        if conflict_files:
            raise RuntimeError(f"skill_merge_conflicts:{','.join(conflict_files)}")

        structured_outcomes = apply_structured_ops(
            project_root=root,
            env_additions=manifest.structured.env_additions,
            python_dependencies=manifest.structured.python_dependencies,
        )

        for cmd in manifest.post_apply:
            _run_shell(cmd, cwd=root)
        if manifest.test:
            _run_shell(manifest.test, cwd=root)

        file_hashes: Dict[str, str] = {}
        for rel in manifest.adds + manifest.modifies:
            p = root / rel
            if p.exists():
                file_hashes[rel] = compute_file_hash(p)

        record_skill_application(
            root,
            skill_name=manifest.skill,
            version=manifest.version,
            file_hashes=file_hashes,
            structured_outcomes=structured_outcomes,
        )
        clear_backup(root)
        return ApplyResult(
            success=True,
            skill=manifest.skill,
            version=manifest.version,
            conflict_files=[],
            structured_outcomes=structured_outcomes,
            message="skill_applied",
        )
    except Exception as exc:
        restore_backup(root)
        clear_backup(root)
        return ApplyResult(
            success=False,
            skill=manifest.skill,
            version=manifest.version,
            conflict_files=conflict_files,
            structured_outcomes={},
            message=str(exc),
        )

