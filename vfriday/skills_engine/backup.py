"""Backup/restore primitives for safe skill apply."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable, List

from vfriday.skills_engine.state import backup_dir

MANIFEST_FILE = "manifest.json"


def _safe_rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe_backup_path:{path}")
    return str(rel)


def create_backup(project_root: Path, files: Iterable[Path]) -> None:
    """Create backup for a list of target files."""
    root = Path(project_root).resolve()
    bdir = backup_dir(root)
    if bdir.exists():
        shutil.rmtree(bdir)
    bdir.mkdir(parents=True, exist_ok=True)

    manifest: List[dict] = []
    for target in files:
        t = Path(target).resolve()
        if not str(t).startswith(str(root)):
            raise ValueError(f"backup_target_outside_project:{t}")
        rel = _safe_rel(t, root)
        exists = t.exists()
        entry = {"rel": rel, "exists": bool(exists)}
        if exists and t.is_file():
            dst = bdir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(t, dst)
        manifest.append(entry)

    (bdir / MANIFEST_FILE).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def restore_backup(project_root: Path) -> None:
    """Restore files from backup snapshot."""
    root = Path(project_root).resolve()
    bdir = backup_dir(root)
    mp = bdir / MANIFEST_FILE
    if not mp.exists():
        return
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        return

    for row in manifest:
        rel = str(row.get("rel") or "")
        existed = bool(row.get("exists"))
        target = root / rel
        source = bdir / rel
        if existed:
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        else:
            if target.exists() and target.is_file():
                target.unlink()


def clear_backup(project_root: Path) -> None:
    """Delete backup directory."""
    bdir = backup_dir(Path(project_root).resolve())
    if bdir.exists():
        shutil.rmtree(bdir)

