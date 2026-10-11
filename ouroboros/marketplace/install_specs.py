"""Normalize third-party skill dependency install metadata.

OpenClaw skills can declare installer metadata intended for several package
managers. Ouroboros only auto-runs specs that can be mapped to a bounded,
per-skill install prefix. Everything else becomes manual setup guidance.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any, Dict, List, Tuple


AUTO_KINDS = frozenset({"pip", "pipx", "uv", "node", "npm", "download"})
MANUAL_KINDS = frozenset({"brew", "apt", "apt-get", "go", "cargo"})
_PIP_PACKAGE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}"
    r"(\[[A-Za-z0-9_.-]+(,[A-Za-z0-9_.-]+)*\])?"
    r"([=<>!~]=?[A-Za-z0-9_.!*+-]+(,[=<>!~]=?[A-Za-z0-9_.!*+-]+)*)?$"
)
# Version pins are valid automatic-install syntax (G3, capinv-447): npm accepts
# "name@1.2.3" / "name@latest" exactly as pip accepts "name==1.2.3" — parity.
_NPM_PACKAGE_RE = re.compile(
    r"^(@[a-z0-9_.-]+/)?[a-z0-9][a-z0-9_.-]{0,120}(@[A-Za-z0-9_.-]{1,64})?$"
)
_CARGO_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,120}$")


def _normalized_spec(kind: str, package: str, bins: List[str], raw: Dict[str, Any]) -> Dict[str, Any]:
    spec = {"kind": kind, "package": package, "bins": list(bins), "mode": "auto", "raw": dict(raw)}
    for key in ("url", "sha256", "size_bytes", "target", "version", "platforms", "steps", "outputs", "check",
                "allow_source_build", "allow_install_scripts"):
        if key in raw:
            spec[key] = raw[key]
    return spec


def relative_install_path(value: Any, *, allow_root: bool = False) -> str:
    """A portable path within the existing isolated dependency directory."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("install path must be a non-empty string")
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or "\\" in value or (not path.parts and not allow_root):
        raise ValueError(f"install path must stay relative to the isolated environment: {value!r}")
    return path.as_posix()


def _validate_install_actions(spec: Dict[str, Any], kind: str) -> None:
    for key, kinds in (("allow_source_build", {"pip", "pipx", "uv"}), ("allow_install_scripts", {"npm", "node"})):
        if key in spec and (type(spec[key]) is not bool or kind not in kinds):
            raise ValueError(f"{key} must be an explicit boolean on {sorted(kinds)}")
    platforms = spec.get("platforms", [])
    if not isinstance(platforms, list) or any(not isinstance(p, str) or not p.strip() for p in platforms):
        raise ValueError("platforms must be a list of platform names")
    steps = spec.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("steps must be a list of argv/cwd objects")
    check = spec.get("check")
    for step in [*steps, *([check] if check is not None else [])]:
        if not isinstance(step, dict) or not isinstance(step.get("argv"), list) or not step["argv"]:
            raise ValueError("each install step/check needs a non-empty argv list")
        if any(not isinstance(arg, str) or "\0" in arg for arg in step["argv"]) or not step["argv"][0]:
            raise ValueError("install argv must contain literal strings and a non-empty executable")
        relative_install_path(step.get("cwd", "."), allow_root=True)
    outputs = spec.get("outputs", [])
    if not isinstance(outputs, list):
        raise ValueError("outputs must be a list of relative file paths")
    for output in outputs:
        relative_install_path(output)
    if steps and (not outputs or check is None):
        raise ValueError("install steps require expected outputs and an executable readiness check")
    if (spec.get("allow_source_build") or spec.get("allow_install_scripts")) and check is None:
        raise ValueError("source builds/install scripts require an executable readiness check")
    if kind == "download":
        if not isinstance(spec.get("url"), str) or not spec["url"].strip():
            raise ValueError("download requires an exact source URL")
        if not isinstance(spec.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]):
            raise ValueError("download requires a lowercase SHA-256 digest")
        if type(spec.get("size_bytes")) is not int or spec["size_bytes"] < 0:
            raise ValueError("download requires a non-negative exact size_bytes")
        relative_install_path(spec.get("target"))


def _coerce_list(value: Any) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _packages_for_spec(spec: Dict[str, Any]) -> List[str]:
    for key in ("package", "packages", "name", "crate", "tool", "formula", "module"):
        items = _coerce_list(spec.get(key))
        if items:
            return items
    return []


def _safe_package_name(kind: str, value: str) -> bool:
    text = str(value or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    forbidden = "\"'`;$|&\\"
    if kind not in {"pip", "pipx", "uv"}:
        forbidden += "<>"
    if any(ch in text for ch in forbidden):
        return False
    if "://" in text or text.startswith((".", "/", "~")) or "+" in text or ":" in text:
        return False
    if kind in {"pip", "pipx", "uv"}:
        return bool(_PIP_PACKAGE_RE.match(text))
    if kind in {"node", "npm"}:
        return bool(_NPM_PACKAGE_RE.match(text))
    if kind == "cargo":
        return bool(_CARGO_PACKAGE_RE.match(text))
    return False


def normalize_install_specs(raw_specs: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """Return ``(auto_specs, manual_specs, warnings)`` for manifest metadata."""

    if raw_specs in (None, "", [], {}):
        return [], [], []
    specs = raw_specs if isinstance(raw_specs, list) else [raw_specs]
    auto: List[Dict[str, Any]] = []
    manual: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for item in specs:
        if not isinstance(item, dict):
            manual.append({"kind": type(item).__name__, "reason": "install spec is not an object", "raw": item})
            continue
        kind = str(item.get("kind") or "").strip().lower()
        packages = _packages_for_spec(item)
        bins = _coerce_list(item.get("bins") or item.get("bin"))
        problem = ""
        try:
            _validate_install_actions(item, kind)
        except ValueError as exc:
            problem = str(exc)
        if kind == "download" and not problem:
            auto.append(_normalized_spec(kind, str(item["target"]), bins, item))
            continue
        if kind in AUTO_KINDS and not problem and packages and all(_safe_package_name(kind, package) for package in packages):
            for package in packages:
                auto.append(_normalized_spec(kind, package, bins, item))
            continue
        package = packages[0] if packages else ""
        reason = ""
        if problem:
            reason = problem
        elif kind in MANUAL_KINDS:
            reason = f"kind {kind!r} may mutate global host state or downloads arbitrary artifacts"
        elif kind in AUTO_KINDS:
            reason = f"package name {package!r} is missing or unsafe"
        else:
            reason = f"kind {kind or '<missing>'!r} is not supported for automatic isolated installs"
        manual.append({"kind": kind, "package": package, "bins": bins, "reason": reason, "raw": item})
        warnings.append(f"Install spec requires manual setup: {reason}.")
    return auto, manual, warnings


def install_specs_hash(specs: List[Dict[str, Any]]) -> str:
    payload = json.dumps(specs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
