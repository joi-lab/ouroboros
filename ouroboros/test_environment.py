"""Stdlib-only environment boundary for local pytest, preflight and UI servers.

May be loaded by file path before importing the package. These are disposable
filesystem defaults, not an OS sandbox. Tests can still explicitly select their
own synthetic roots. No dependency installation happens here.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path


def settings_keys() -> set[str]:
    """Read the settings vocabulary without importing the settings runtime."""
    keys = set()
    for name in ("settings_defaults.py", "update_channels.py"):
        tree = ast.parse(Path(__file__).with_name(name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys.update(key.value for key in node.keys
                            if isinstance(key, ast.Constant) and isinstance(key.value, str)
                            and key.value.isupper())
    return keys


def scrub_environment(source, *, keep=()) -> dict[str, str]:
    """Drop owner configuration, credentials and process/import overrides.

    The caller names any test controls it wants to retain. Containment membership
    (OURO_PROC_CONTAINER_*) is deliberately inherited by every descendant.
    """
    projected = settings_keys()
    suffixes = ("_API_KEY", "_TOKEN", "_PASSWORD", "_CREDENTIALS", "_SECRET")
    prefixes = ("OUROBOROS_", "GH_", "GIT_", "PYTEST_", "PYTHON", "UV_", "PIP_")
    return {key: value for key, value in source.items()
            if key in keep or not (key.startswith(prefixes) or key.endswith(suffixes)
                                  or key in projected or key in {"NODE_OPTIONS", "VIRTUAL_ENV"})}


# A disposable tree whose processes were not PROVEN gone keeps this file at its
# top. Every enclosing cleanup layer (candidate checkout, pytest session root,
# safe_test launcher) refuses to delete a tree that contains one: retention is
# in place, so a surviving process never loses the directory it is writing into.
RETENTION_MARKER = "OUROBOROS_RETAINED.txt"


def retain_tree(root, reason: str) -> bool:
    """Mark ``root`` retained; False when the marker itself could not be written."""
    try:
        with (Path(root) / RETENTION_MARKER).open("a", encoding="utf-8") as stream:
            stream.write(reason.rstrip() + "\n")
    except OSError:
        return False
    return True


def retention_markers(root) -> list[Path]:
    """Markers beneath ``root``. A subtree that cannot be walked counts as one."""
    found: list[Path] = []
    for directory, _dirs, files in os.walk(
            root, followlinks=False,
            onerror=lambda error: found.append(Path(error.filename or root))):
        if RETENTION_MARKER in files:
            found.append(Path(directory) / RETENTION_MARKER)
    return found


def isolated_environment(root: Path, repo: Path, *, source=None, keep=(), create=True) -> dict[str, str]:
    """Create all writable defaults beneath a caller-owned disposable directory."""
    root, repo = Path(root).resolve(), Path(repo).resolve()
    env = scrub_environment(os.environ if source is None else source, keep=keep)
    paths = {
        "HOME": root / "home", "USERPROFILE": root / "home",
        "OUROBOROS_APP_ROOT": root / "app",
        "OUROBOROS_DATA_DIR": root / "data",
        "OUROBOROS_SUBAGENT_PROJECTS_ROOT": root / "projects",
        "OUROBOROS_SUBAGENT_WORKTREE_ROOT": root / "worktrees",
        "OUROBOROS_DELIVERABLES_ROOT": root / "Deliverables",
        "OUROBOROS_BENCH_RUNS_ROOT": root / "bench_runs",
        "PYTHONPYCACHEPREFIX": root / "pycache", "PYTHONUSERBASE": root / "userbase",
        "XDG_CACHE_HOME": root / "cache", "XDG_CONFIG_HOME": root / "config",
        "XDG_DATA_HOME": root / "xdg-data", "XDG_STATE_HOME": root / "xdg-state",
        "APPDATA": root / "config", "LOCALAPPDATA": root / "cache",
        "TMPDIR": root / "tmp", "TEMP": root / "tmp", "TMP": root / "tmp",
        # Chromium on macOS ignores TMPDIR for initial download staging.
        "MAC_CHROMIUM_TMPDIR": root / "tmp",
        "PYTEST_DEBUG_TEMPROOT": root / "tmp",
        "UV_CACHE_DIR": root / "cache" / "uv",
        "UV_PYTHON_INSTALL_DIR": root / "python",
        "PIP_CACHE_DIR": root / "cache" / "pip",
    }
    for key, path in paths.items():
        if create:
            path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    env.update({
        "OUROBOROS_REPO_DIR": str(repo),
        "OUROBOROS_SETTINGS_PATH": str(root / "data" / "settings.json"),
        "OUROBOROS_PYTEST_ACTIVE": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        # No PYTHONUTF8 / PYTHONIOENCODING: forcing UTF-8 mode on every test and
        # commit-gate child would hide the cp1252 bugs Windows CI exists to catch.
        "LC_ALL": "C", "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0", "GIT_CEILING_DIRECTORIES": str(root),
        "PIP_REQUIRE_VIRTUALENV": "true",
    })
    return env
