"""Skill manifest parsing and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass(frozen=True)
class StructuredOps:
    """Deterministic operations over structured files."""

    env_additions: List[str] = field(default_factory=list)
    python_dependencies: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkillManifest:
    """Canonical skill metadata loaded from manifest.yaml."""

    skill: str
    version: str
    description: str
    core_version: str
    adds: List[str]
    modifies: List[str]
    structured: StructuredOps
    depends: List[str]
    conflicts: List[str]
    post_apply: List[str]
    test: str | None


def _ensure_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    raise ValueError("Expected list in manifest")


def _validate_paths(paths: List[str], *, field_name: str) -> List[str]:
    clean: List[str] = []
    for rel in paths:
        if not rel:
            continue
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Invalid path in {field_name}: {rel}")
        clean.append(rel)
    return clean


def read_manifest(skill_dir: Path) -> SkillManifest:
    """Load and validate a skill manifest."""
    manifest_path = Path(skill_dir) / "manifest.yaml"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest_not_found:{manifest_path}")

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"invalid_manifest:{manifest_path}")

    skill = str(raw.get("skill") or "").strip()
    version = str(raw.get("version") or "").strip()
    description = str(raw.get("description") or "").strip()
    core_version = str(raw.get("core_version") or "0.1.0").strip()
    if not skill or not version:
        raise ValueError(f"manifest_missing_required_fields:{manifest_path}")

    structured_raw = raw.get("structured") or {}
    if not isinstance(structured_raw, dict):
        raise ValueError("structured_must_be_mapping")
    structured = StructuredOps(
        env_additions=_ensure_list(structured_raw.get("env_additions")),
        python_dependencies=_ensure_list(structured_raw.get("python_dependencies")),
    )

    adds = _validate_paths(_ensure_list(raw.get("adds")), field_name="adds")
    modifies = _validate_paths(_ensure_list(raw.get("modifies")), field_name="modifies")
    depends = _ensure_list(raw.get("depends"))
    conflicts = _ensure_list(raw.get("conflicts"))
    post_apply = _ensure_list(raw.get("post_apply"))
    test = str(raw.get("test") or "").strip() or None

    return SkillManifest(
        skill=skill,
        version=version,
        description=description,
        core_version=core_version,
        adds=adds,
        modifies=modifies,
        structured=structured,
        depends=depends,
        conflicts=conflicts,
        post_apply=post_apply,
        test=test,
    )

