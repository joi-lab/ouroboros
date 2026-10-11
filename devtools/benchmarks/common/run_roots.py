"""Run-root helpers for benchmark devtools."""

from __future__ import annotations

import os
import pathlib
import re
import time


_WORKSPACE_ROOT = pathlib.Path(__file__).resolve().parents[4]
DEFAULT_BENCH_RUNS_ROOT = _WORKSPACE_ROOT / "bench_runs"
_SAFE_BENCHMARK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


_RUN_ID_COUNTER = 0


def timestamp_run_id(prefix: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in prefix).strip("-_")
    # Append a short pid+counter suffix: bare second-granularity timestamps
    # collide when two runs start in the same second (observed run-dir merges
    # and split empty dirs in bench_runs).
    global _RUN_ID_COUNTER
    _RUN_ID_COUNTER += 1
    return f"{safe or 'run'}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid():d}{_RUN_ID_COUNTER:02d}"


def run_root(benchmark: str, run_id: str = "") -> pathlib.Path:
    root = pathlib.Path(os.environ.get("OUROBOROS_BENCH_RUNS_ROOT") or DEFAULT_BENCH_RUNS_ROOT)
    bench = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in benchmark).strip("-_")
    rid = run_id or timestamp_run_id(bench)
    return (root / bench / rid).resolve(strict=False)


def latest_run_root(benchmark: str) -> pathlib.Path | None:
    """Return the most recently modified existing ``<benchmark>_*`` run dir.

    Grading/inspection tools want to default to the run that just happened, not
    a brand-new empty timestamped dir. Returns None when no run exists yet so
    callers can fall back to a fresh ``run_root`` or error with a hint.
    """
    root = pathlib.Path(os.environ.get("OUROBOROS_BENCH_RUNS_ROOT") or DEFAULT_BENCH_RUNS_ROOT)
    bench = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in benchmark).strip("-_")
    base = (root / bench)
    try:
        candidates = [d for d in base.iterdir() if d.is_dir() and d.name.startswith(bench)]
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime).resolve(strict=False)


def assert_outside_repo(path: pathlib.Path, repo_dir: pathlib.Path) -> pathlib.Path:
    """PURE boundary check: resolve ``path`` and refuse it if it is under repo/ or live data/.

    Split out of ``ensure_outside_repo`` because the two halves are needed at different
    moments: a caller that must not mutate anything yet (a pre-admission argument check, or a
    path it will only create later) needs the refusal WITHOUT the ``mkdir`` side effect.
    Creating the directory as a by-product of validating it also hides the write's real
    precondition from the call site.
    """
    resolved = pathlib.Path(path).expanduser().resolve(strict=False)
    repo = pathlib.Path(repo_dir).resolve(strict=False)
    forbidden: list[tuple[str, pathlib.Path]] = [("repo/", repo)]
    forbidden.extend(("live runtime data/", root) for root in live_data_roots())
    for label, root in forbidden:
        try:
            resolved.relative_to(root.expanduser().resolve(strict=False))
        except ValueError:
            continue
        raise ValueError(f"benchmark run output must not be under {label}: {resolved}")
    return resolved


def ensure_outside_repo(path: pathlib.Path, repo_dir: pathlib.Path) -> pathlib.Path:
    """``assert_outside_repo`` plus the mkdir, for callers that want the directory now."""
    resolved = assert_outside_repo(path, repo_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def safe_benchmark_id(value: str, *, field: str = "instance_id") -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or pathlib.PurePath(text).is_absolute()
        or not _SAFE_BENCHMARK_ID_RE.fullmatch(text)
    ):
        raise ValueError(f"{field} must be a single safe path component")
    return text


def _comparison_path(path: pathlib.PurePath) -> pathlib.PurePath:
    """Compare resolved Windows paths in the same namespace; keep IO paths intact."""
    if isinstance(path, pathlib.PureWindowsPath):
        text = str(path)
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        else:
            text = text.removeprefix("\\\\?\\")
        return pathlib.PureWindowsPath(text)
    return path


def safe_join_under(root: pathlib.Path, *parts: str) -> pathlib.Path:
    base = pathlib.Path(root).expanduser().resolve(strict=False)
    resolved = base.joinpath(*[str(part or "") for part in parts]).resolve(strict=False)
    try:
        _comparison_path(resolved).relative_to(_comparison_path(base))
    except ValueError as exc:
        raise ValueError(f"benchmark output path escapes run root: {resolved}") from exc
    return resolved


def assert_file_output_outside_repo(path: pathlib.Path, repo_dir: pathlib.Path) -> pathlib.Path:
    """PURE form of ``ensure_file_output_outside_repo``: check the parent, create nothing.

    Every writer of these sidecars creates its own parent (``atomic_write_json`` and
    ``write_jsonl`` both do), so a caller that only needs the BOUNDARY — in particular one
    running before its run manifest is persisted — must not pre-create the directory.
    """
    resolved = pathlib.Path(path).expanduser().resolve(strict=False)
    assert_outside_repo(resolved.parent, repo_dir)
    return resolved


def ensure_file_output_outside_repo(path: pathlib.Path, repo_dir: pathlib.Path) -> pathlib.Path:
    resolved = pathlib.Path(path).expanduser().resolve(strict=False)
    ensure_outside_repo(resolved.parent, repo_dir)
    return resolved


def repo_root_from_devtools() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]


def workspace_root_from_devtools() -> pathlib.Path:
    return _WORKSPACE_ROOT


def default_settings_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("OUROBOROS_SETTINGS_PATH") or _WORKSPACE_ROOT / "data" / "settings.json")


def live_repo_roots() -> list[pathlib.Path]:
    """The LIVE Ouroboros checkout(s) a benchmark must never point itself at.

    Companion to ``live_data_roots()`` and derived from the same authority, because "live" is a
    property of the RUNTIME's configured layout, never of wherever the caller happens to be
    executing from. A launcher that answered "is this the live repo?" with
    ``Path(__file__).parents[N]`` was really asking "is this my own tree?": true of the live
    repo only in the development workspace, and false in the case we ship, where the launcher
    IS the pinned seed and a run is legitimately pointed at it (the smoke that found this had
    to clone the seed twice to get past the refusal). ``$OUROBOROS_REPO_DIR`` is the documented
    SSOT; the ``repo`` sibling of each live data root covers the default workspace layout,
    where a seed clone elsewhere on disk never collides.
    """
    roots: list[pathlib.Path] = []
    repo_env = os.environ.get("OUROBOROS_REPO_DIR")
    if repo_env:
        roots.append(pathlib.Path(repo_env).expanduser())
    roots.extend(root.expanduser().parent / "repo" for root in live_data_roots())
    unique: list[pathlib.Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = str(root.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(root)
    return unique


def live_data_roots() -> list[pathlib.Path]:
    roots = [_WORKSPACE_ROOT / "data"]
    data_env = os.environ.get("OUROBOROS_DATA_DIR")
    if data_env:
        roots.append(pathlib.Path(data_env).expanduser())
    settings_env = os.environ.get("OUROBOROS_SETTINGS_PATH")
    if settings_env:
        roots.append(pathlib.Path(settings_env).expanduser().parent)
    unique: list[pathlib.Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = str(root.expanduser().resolve(strict=False))
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(root)
    return unique
