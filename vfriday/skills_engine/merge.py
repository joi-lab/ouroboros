"""Deterministic merge helpers."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MergeResult:
    """Result of a single file merge."""

    clean: bool
    conflicted: bool
    output: str


def merge_file(current: Path, base: Path, theirs: Path) -> MergeResult:
    """Run git merge-file and write merged content to current file."""
    cmd = ["git", "merge-file", "-p", str(current), str(base), str(theirs)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    merged = proc.stdout or ""
    if merged:
        current.write_text(merged, encoding="utf-8")
    if proc.returncode == 0:
        return MergeResult(clean=True, conflicted=False, output=merged)
    if proc.returncode == 1:
        # 1 means conflicts were produced with markers.
        return MergeResult(clean=False, conflicted=True, output=merged)
    err = (proc.stderr or "").strip()
    raise RuntimeError(f"merge_file_failed:{err}")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def copy_file(src: Path, dst: Path) -> None:
    ensure_parent(dst)
    shutil.copy2(src, dst)

