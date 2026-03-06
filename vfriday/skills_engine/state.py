"""State and hash tracking for the skills engine."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any, Dict, List

import yaml

SKILLS_SYSTEM_VERSION = "0.1.0"


def _vfriday_dir(project_root: Path) -> Path:
    return Path(project_root).resolve() / ".vfriday"


def state_path(project_root: Path) -> Path:
    return _vfriday_dir(project_root) / "state.yaml"


def base_dir(project_root: Path) -> Path:
    return _vfriday_dir(project_root) / "base"


def backup_dir(project_root: Path) -> Path:
    return _vfriday_dir(project_root) / "backup"


def _core_version(project_root: Path) -> str:
    p = Path(project_root).resolve() / "VERSION"
    return p.read_text(encoding="utf-8").strip() if p.exists() else "0.0.0"


def _snapshot_base(project_root: Path, dst_base: Path) -> None:
    root = Path(project_root).resolve()
    if dst_base.exists():
        shutil.rmtree(dst_base)
    dst_base.mkdir(parents=True, exist_ok=True)

    excluded = {
        ".git",
        ".pytest_cache",
        ".mypy_cache",
        "__pycache__",
        ".vfriday",
        "data",
    }
    for src in root.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(root)
        if any(part in excluded for part in rel.parts):
            continue
        dst = dst_base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def init_skills_state(project_root: Path, *, force: bool = False) -> Dict[str, Any]:
    """Initialize `.vfriday/state.yaml` and base snapshot."""
    root = Path(project_root).resolve()
    vf = _vfriday_dir(root)
    vf.mkdir(parents=True, exist_ok=True)
    sp = state_path(root)
    bd = base_dir(root)
    if sp.exists() and not force:
        return load_state(root)

    state: Dict[str, Any] = {
        "skills_system_version": SKILLS_SYSTEM_VERSION,
        "core_version": _core_version(root),
        "applied_skills": [],
        "custom_patches": [],
    }
    write_state(root, state)
    _snapshot_base(root, bd)
    return state


def load_state(project_root: Path) -> Dict[str, Any]:
    """Load state file, initializing if missing."""
    root = Path(project_root).resolve()
    sp = state_path(root)
    if not sp.exists():
        return init_skills_state(root)
    state = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
    if not isinstance(state, dict):
        raise ValueError(f"invalid_state:{sp}")
    return state


def write_state(project_root: Path, state: Dict[str, Any]) -> None:
    """Persist state deterministically."""
    root = Path(project_root).resolve()
    sp = state_path(root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(yaml.safe_dump(state, sort_keys=False, allow_unicode=True), encoding="utf-8")


def compute_file_hash(path: Path) -> str:
    """SHA-256 hash for drift and replay checks."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 64), b""):
            h.update(chunk)
    return h.hexdigest()


def record_skill_application(
    project_root: Path,
    *,
    skill_name: str,
    version: str,
    file_hashes: Dict[str, str],
    structured_outcomes: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Append a skill application record to state."""
    state = load_state(project_root)
    applied: List[Dict[str, Any]] = list(state.get("applied_skills") or [])
    applied = [x for x in applied if str(x.get("name")) != skill_name]
    applied.append(
        {
            "name": skill_name,
            "version": version,
            "file_hashes": file_hashes,
            "structured_outcomes": structured_outcomes or {},
        }
    )
    state["applied_skills"] = applied
    write_state(project_root, state)
    return state

