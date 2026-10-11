"""Read-only external workspace preflight helpers."""

from __future__ import annotations

import json
import pathlib
import shlex
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Tuple

from ouroboros.platform_layer import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    bootstrap_process_path,
    node_runtime_health,
    resolve_bundled_node,
)
from ouroboros.utils import utc_now_iso


_CURATED_TOOLS = (
    "git",
    "python",
    "python3",
    "pip",
    "pip3",
    "pytest",
    "uv",
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "go",
)
_MACOS_PACKAGE_TOOLS = (
    "brew",
)
_LINUX_PACKAGE_TOOLS = (
    "apt-get",
    "apt",
    "dnf",
    "yum",
    "apk",
    "pacman",
)
_WINDOWS_PACKAGE_TOOLS = (
    "winget",
)
# The exec probe covers ONLY node (the incident class: `which` succeeds but the
# kernel SIGKILLs the binary on launch). npm/npx/pnpm/yarn follow node — they
# get no exec probes of their own; python/git/other tools are out of class.
# The probe timeout is SHORT so the bounded admission path (8s wall-clock cap in
# workspace_admission.bounded_workspace_preflight) stays safe; verdicts are
# memoized in the platform layer.
_NODE_PROBE_TIMEOUT_SEC = 3.0
_NODE_DEPENDENT_TOOLS = ("npm", "npx", "pnpm", "yarn")
_MANIFEST_NAMES = (
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-runtime.lock",
    "uv.lock",
    "tox.ini",
    "go.mod",
    "Cargo.toml",
    "Makefile",
)


def collect_workspace_preflight(workspace_root: pathlib.Path) -> Dict[str, Any]:
    """Collect a compact, read-only snapshot of a target workspace."""

    added_dirs = bootstrap_process_path()
    root = pathlib.Path(workspace_root).resolve(strict=False)
    manifests = _collect_manifests(root)
    inferred_tools = sorted(set(_infer_tools_from_manifests(manifests)))
    probe_tools = sorted(set(_platform_curated_tools()).union(inferred_tools))
    tools = _probe_tools(probe_tools)
    git = _git_snapshot(root)
    return {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "workspace_root": str(root),
        "git": git,
        "manifests": manifests,
        "tools": tools,
        "path_bootstrap": {
            "added_dirs": added_dirs,
        },
    }


def summarize_workspace_preflight(preflight: Dict[str, Any]) -> Dict[str, Any]:
    """Return the task-metadata/prompt-safe preflight summary."""

    git = preflight.get("git") if isinstance(preflight.get("git"), dict) else {}
    manifests = preflight.get("manifests") if isinstance(preflight.get("manifests"), list) else []
    tools = preflight.get("tools") if isinstance(preflight.get("tools"), dict) else {}
    available = sorted(k for k, v in tools.items() if isinstance(v, dict) and v.get("available"))
    broken_names = sorted(
        k for k, v in tools.items()
        if isinstance(v, dict)
        and not v.get("available")
        and v.get("status") in ("found_but_broken", "interpreter_broken")
    )
    missing = sorted(
        k for k, v in tools.items()
        if isinstance(v, dict) and not v.get("available") and k not in set(broken_names)
    )
    broken: List[Dict[str, str]] = []
    for name in broken_names:
        entry = tools[name]
        reason = str(entry.get("probe_reason") or entry.get("reason") or entry.get("status") or "broken")
        if entry.get("bundled_fallback"):
            reason += "; bundled fallback available"
        broken.append({"tool": name, "reason": reason})
    manifest_summary = []
    for item in manifests[:20]:
        if not isinstance(item, dict):
            continue
        manifest_summary.append({
            "path": item.get("path", ""),
            "type": item.get("type", ""),
            "scripts": list(item.get("scripts", []) or [])[:20],
        })
    return {
        "schema_version": 1,
        "workspace_root": str(preflight.get("workspace_root") or ""),
        "git": {
            "head": str(git.get("head") or ""),
            "branch": str(git.get("branch") or ""),
            "dirty": bool(git.get("dirty")),
            "status_count": int(git.get("status_count") or 0),
        },
        "manifests": manifest_summary,
        "tools": {
            "available": available,
            "missing": missing,
            # Present only when a which-found tool failed its exec probe (or
            # follows a broken interpreter) — the healthy-path summary stays
            # byte-identical.
            **({"broken": broken} if broken else {}),
        },
    }


def render_workspace_preflight_summary(summary: Dict[str, Any]) -> str:
    """Render a stable short block for the headless task prompt."""

    git = summary.get("git") if isinstance(summary.get("git"), dict) else {}
    tools = summary.get("tools") if isinstance(summary.get("tools"), dict) else {}
    manifests = summary.get("manifests") if isinstance(summary.get("manifests"), list) else []
    lines = [
        "workspace_preflight:",
        f"- git_head: {git.get('head') or '<unknown>'}",
        f"- git_branch: {git.get('branch') or '<unknown>'}",
        f"- git_dirty: {bool(git.get('dirty'))} ({int(git.get('status_count') or 0)} status entries)",
        "- manifests: "
        + (", ".join(str(item.get("path") or "") for item in manifests if isinstance(item, dict)) or "<none detected>"),
        # "on PATH" is what shutil.which actually measured; "available" over-
        # claimed executability (a broken binary on PATH counted as available).
        # The structured summary keys stay unchanged — they ride durable
        # task metadata and old rows must keep replaying.
        "- tools_on_path: " + (", ".join(tools.get("available") or []) or "<none detected>"),
        "- tools_missing_from_path: " + (", ".join(tools.get("missing") or []) or "<none detected>"),
    ]
    broken = [item for item in (tools.get("broken") or []) if isinstance(item, dict)]
    if broken:
        # Which-found but execution-probe-dead tools: NOT available, and the
        # reason (plus a bundled-node fallback fact, when one exists) is what
        # keeps the agent from treating the miss as a syntax/config problem.
        lines.append(
            "- broken_tools: "
            + ", ".join(
                f"{item.get('tool') or '<tool>'} ({item.get('reason') or 'broken'})"
                for item in broken
            )
        )
    if summary.get("error"):
        lines.append(f"- preflight_error: {summary.get('error')}")
    return "\n".join(lines)


def _git_snapshot(root: pathlib.Path) -> Dict[str, Any]:
    head = _git_stdout(root, ["git", "rev-parse", "HEAD"], timeout=5).strip()
    branch = _git_stdout(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=5).strip()
    status = _git_stdout(root, ["git", "status", "--porcelain=v1", "--untracked-files=all"], timeout=8)
    entries = [line for line in status.splitlines() if line.strip()]
    return {
        "head": head,
        "branch": branch,
        "dirty": bool(entries),
        "status_count": len(entries),
        "status_porcelain": entries[:200],
        "status_truncated": len(entries) > 200,
    }


def _collect_manifests(root: pathlib.Path) -> List[Dict[str, Any]]:
    manifests: List[Dict[str, Any]] = []
    for name in _MANIFEST_NAMES:
        path = root / name
        if not path.is_file():
            continue
        entry: Dict[str, Any] = {"path": name, "type": _manifest_type(name), "scripts": []}
        if name == "package.json":
            scripts, script_commands = _package_json_scripts(path)
            entry["scripts"] = scripts
            entry["script_commands"] = script_commands
        elif name == "Makefile":
            entry["scripts"] = _makefile_targets(path)
        manifests.append(entry)
    return manifests


def _manifest_type(name: str) -> str:
    if name == "package.json":
        return "node"
    if name == "go.mod":
        return "go"
    if name in {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "tox.ini"}:
        return "python"
    if name == "Cargo.toml":
        return "rust"
    if name == "Makefile":
        return "make"
    return "manifest"


def _platform_curated_tools() -> List[str]:
    tools = list(_CURATED_TOOLS)
    if IS_MACOS:
        tools.extend(_MACOS_PACKAGE_TOOLS)
    elif IS_LINUX:
        tools.extend(_LINUX_PACKAGE_TOOLS)
    elif IS_WINDOWS:
        tools.extend(_WINDOWS_PACKAGE_TOOLS)
    return tools


def _package_json_scripts(path: pathlib.Path) -> Tuple[List[str], Dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], {}
    scripts = data.get("scripts") if isinstance(data, dict) else {}
    if not isinstance(scripts, dict):
        return [], {}
    names = sorted(str(key) for key in scripts.keys())[:50]
    commands = {str(key): str(value) for key, value in scripts.items() if isinstance(value, str)}
    return names, {key: commands[key] for key in names if key in commands}


def _makefile_targets(path: pathlib.Path) -> List[str]:
    out: List[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        if line.startswith(("\t", " ", ".", "#")) or ":" not in line:
            continue
        target = line.split(":", 1)[0].strip()
        if target and all(ch not in target for ch in " $/\\"):
            out.append(target)
    return sorted(set(out))[:50]


def _infer_tools_from_manifests(manifests: Iterable[Dict[str, Any]]) -> List[str]:
    tools: set[str] = set()
    for entry in manifests:
        kind = str(entry.get("type") or "")
        if kind == "node":
            tools.update({"node", "npm", "npx"})
        elif kind == "go":
            tools.add("go")
        elif kind == "python":
            tools.update({"python", "python3", "pytest", "pip", "uv"})
        elif kind == "rust":
            tools.update({"cargo", "rustc"})
        elif kind == "make":
            tools.add("make")
        script_commands = entry.get("script_commands") if isinstance(entry.get("script_commands"), dict) else {}
        for script in script_commands.values():
            binary = _first_script_binary(str(script))
            if binary:
                tools.add(binary)
    return sorted(tools)


def _first_script_binary(script: str) -> str:
    try:
        parts = shlex.split(str(script))
    except ValueError:
        parts = str(script).split()
    skip_next = False
    for token in parts:
        if skip_next:
            skip_next = False
            continue
        text = str(token)
        if not text:
            continue
        if "=" in text and not text.startswith(("=", "-", "./", "../", "/")):
            continue
        name = pathlib.PurePath(text).name
        if name in {"cd", "set", "export", "source", ".", "&&", "||", ";"}:
            if name == "cd":
                skip_next = True
            continue
        if name.startswith("-"):
            continue
        return name
    return ""


def _probe_tools(names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        clean = str(name or "").strip()
        if not clean:
            continue
        path = shutil.which(clean)
        out[clean] = {"available": bool(path), "path": path or ""}
    _apply_node_exec_probe(out)
    return out


def _apply_node_exec_probe(tools: Dict[str, Dict[str, Any]]) -> None:
    """Exec-probe node in place; a dead node also downgrades its dependents.

    ``shutil.which`` proves existence, not runnability. Statuses on the node
    entry: ``available`` / ``found_but_broken`` / ``missing``. When node is
    execution-probed broken OR absent entirely, npm/npx/pnpm/yarn that
    ``which`` found are marked unavailable with reason ``interpreter_broken``
    (no exec probes on them — their `#!/usr/bin/env node` launchers cannot run
    without node), and a healthy bundled node is disclosed as a fallback fact
    for the agent-facing summary.
    """
    node = tools.get("node")
    if not isinstance(node, dict):
        return
    if node.get("path"):
        health = node_runtime_health(str(node["path"]), timeout_sec=_NODE_PROBE_TIMEOUT_SEC)
        if health.healthy:
            node["status"] = "available"
            node["version"] = health.version
            return
        node["available"] = False
        # A which-hit that stat/access-fails mid-probe is missing, not broken.
        node["status"] = "found_but_broken" if health.status == "broken" else "missing"
        node["probe_reason"] = health.reason or health.status
    else:
        node["status"] = "missing"
    bundled = resolve_bundled_node()
    if bundled and node_runtime_health(bundled, timeout_sec=_NODE_PROBE_TIMEOUT_SEC).healthy:
        node["bundled_fallback"] = bundled
    # npm/npx/pnpm/yarn start through `#!/usr/bin/env node`: with node broken OR
    # absent they are equally dead, however present their own files are.
    for name in _NODE_DEPENDENT_TOOLS:
        entry = tools.get(name)
        if isinstance(entry, dict) and entry.get("path"):
            entry["available"] = False
            entry["status"] = "interpreter_broken"
            entry["reason"] = "interpreter_broken"


def _git_stdout(root: pathlib.Path, cmd: List[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return ""
    return result.stdout if result.returncode == 0 else ""


__all__ = [
    "collect_workspace_preflight",
    "render_workspace_preflight_summary",
    "summarize_workspace_preflight",
]
