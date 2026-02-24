"""Deterministic structured operations for non-code files."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List


def merge_env_additions(env_example_path: Path, additions: Iterable[str]) -> List[str]:
    """Ensure env keys exist as KEY= lines."""
    p = Path(env_example_path)
    if p.exists():
        lines = p.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    existing_keys = set()
    for line in lines:
        striped = line.strip()
        if not striped or striped.startswith("#") or "=" not in striped:
            continue
        existing_keys.add(striped.split("=", 1)[0].strip())

    appended: List[str] = []
    for raw in additions:
        key = str(raw).strip()
        if not key or key in existing_keys:
            continue
        lines.append(f"{key}=")
        appended.append(key)
        existing_keys.add(key)

    p.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    p.write_text(text, encoding="utf-8")
    return appended


def merge_python_dependencies(requirements_path: Path, additions: Iterable[str]) -> List[str]:
    """Append missing dependencies to requirements.txt preserving order."""
    p = Path(requirements_path)
    existing_lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    existing = {line.strip() for line in existing_lines if line.strip() and not line.strip().startswith("#")}

    appended: List[str] = []
    for dep in additions:
        d = str(dep).strip()
        if not d or d in existing:
            continue
        existing_lines.append(d)
        appended.append(d)
        existing.add(d)

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(("\n".join(existing_lines).rstrip() + "\n"), encoding="utf-8")
    return appended


def apply_structured_ops(
    *,
    project_root: Path,
    env_additions: Iterable[str],
    python_dependencies: Iterable[str],
) -> Dict[str, list]:
    """Apply all structured operations and return exact outcomes."""
    root = Path(project_root).resolve()
    env_added = merge_env_additions(root / ".env.example", env_additions)
    deps_added = merge_python_dependencies(root / "requirements.txt", python_dependencies)
    return {
        "env_additions_applied": env_added,
        "python_dependencies_applied": deps_added,
    }

