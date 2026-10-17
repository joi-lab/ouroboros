"""Commit/PR test scope: a contract floor, direct imports and authored widening.

Selection is computed from the candidate's files, including dirty and untracked
inputs. It is a minimum, not a claim about dynamic or indirect consumers.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Iterable


# Minimal witnesses of the contracts verification and every install rely on.
# Replacing a witness is a verifier change and requires full qualification.
# The whole Node lane is unconditional in both runners, outside this Python list.
FLOOR_TESTS = {
    "tests/test_contracts.py": "frozen ABI and tool/context protocols",
    "tests/test_gateway_parity.py": "HTTP/WS wire schemas and router parity",
    "tests/test_packaging_sync.py": "release identity and public install carriers",
    "tests/test_process_custody.py": "every long-lived child has a lifetime owner",
    "tests/test_hermetic_data_root.py": "tests cannot bind the live data roots",
    "tests/test_preflight_process_containment.py": "a leaked test process cannot pass the gate",
    "tests/test_smoke.py": "the shipped Python modules import",
    "tests/test_safety_policy.py": "every built-in tool has an explicit safety policy",
}

SELECTION_RULE = "floor + changed tests + direct importers + authored widening"
# Bind the policy this process loaded, even if its source changes later on disk.
_POLICY_SOURCE = Path(__file__).read_bytes()
_APPARATUS = frozenset({
    "pyproject.toml", "pytest.ini", ".pytest.ini", "setup.cfg", "tox.ini",
    "uv.lock", "requirements.txt", "requirements-runtime.lock",
    "web/package.json", "web/package-lock.json", "site/pnpm-lock.yaml",
    "ouroboros/preflight_runner.py", "ouroboros/preflight_node.py",
    "ouroboros/preflight_receipts.py", "ouroboros/commit_admission.py",
    "ouroboros/test_selection.py", "ouroboros/preflight_inventory.py",
    "ouroboros/preflight_qualification.py", "scripts/run_tests.py", "scripts/safe_test.py",
    "ouroboros/test_environment.py", ".github/workflows/ci.yml",
    "ouroboros/process_containment.py", "ouroboros/process_lifeline.py",
    "ouroboros/process_overload.py", "ouroboros/test_parallelism.py",
})


def _git(repo: Path, *args: str) -> bytes:
    # Git inspection is bounded transport, never a test-runner timeout.
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          check=True, timeout=30).stdout


def changed_paths(repo: Path, base: str) -> tuple[str, tuple[str, ...]]:
    """Resolve once; --no-renames keeps both sides of a module rename in scope."""
    oid = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
    raw = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z", oid, "--")
    raw += _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return oid, tuple(sorted({os.fsdecode(p) for p in raw.split(b"\0") if p}))


def is_test_module(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(p.parts) and p.parts[0] == "tests" and p.name.startswith("test_") and p.suffix == ".py"


def apparatus_change(path: str) -> bool:
    p = PurePosixPath(path)
    # Under tests/, everything but a test module (helpers, conftest, fixture data) is shared apparatus.
    return (path in _APPARATUS or path in FLOOR_TESTS
            or (p.parts[0] == "tests" and not is_test_module(path))
            or (p.name.startswith("requirements") and p.suffix in {".txt", ".lock"}))


# Release carriers bumped by every numbered release (P9): only a change beyond the
# release version makes them apparatus, or selection would never apply to a release.
_RELEASE_CARRIERS = frozenset({"pyproject.toml", "uv.lock", "web/package.json", "web/package-lock.json"})
_PYPROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"', re.M)


def _release_versions(repo: Path, base: str) -> set[str]:
    """Both spellings of the base and candidate release version (VERSION and its PEP 440 form)."""
    versions = set()
    for name in ("VERSION", "pyproject.toml"):
        texts = []
        try:
            texts.append(_git(repo, "show", f"{base}:{name}").decode("utf-8"))
        except (subprocess.SubprocessError, UnicodeError):
            pass
        try:
            texts.append((repo / name).read_text(encoding="utf-8"))
        except OSError:
            pass
        for text in texts:
            if name == "VERSION":
                versions.add(text.strip())
            elif match := _PYPROJECT_VERSION.search(text):
                versions.add(match.group(1))
    return {version for version in versions if version}


def _version_only_change(repo: Path, base: str, path: str, versions: set[str]) -> bool:
    try:
        # Both sides with universal newlines, as read_text gives the checkout: a blob a
        # Windows checkout stored with CRLF bytes must not turn a version bump into apparatus.
        old = _git(repo, "show", f"{base}:{path}").decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        new = (repo / path).read_text(encoding="utf-8")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return False
    for version in sorted(versions, key=len, reverse=True):
        old, new = old.replace(version, "\0"), new.replace(version, "\0")
    return bool(versions) and old == new


def _module(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def direct_imports(source: str, path: str) -> set[str]:
    """Include imports in functions/TYPE_CHECKING; do not follow transitive edges."""
    imports: set[str] = set()
    package = path.removesuffix(".py").split("/")[:-1]
    for node in ast.walk(ast.parse(source, filename=path)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = ".".join(package[:len(package) - node.level + 1]) if node.level else ""
            module = ".".join(part for part in (prefix, node.module or "") if part)
            if module:
                imports.add(module)
                imports.update(f"{module}.{alias.name}" for alias in node.names if alias.name != "*")
    return imports


# Every proof check re-selects; parsing ~2,000 test modules each time cost seconds per commit.
# Keyed by content, so an edited module is parsed again; the bound only caps memory.
_IMPORT_SCANS: dict[tuple[str, bytes], frozenset[str]] = {}
_IMPORT_SCAN_ENTRIES = 50_000


def _scanned_imports(repo: Path, path: str) -> frozenset[str]:
    source = (repo / path).read_text(encoding="utf-8")
    key = (path, hashlib.blake2b(source.encode("utf-8"), digest_size=16).digest())
    if key not in _IMPORT_SCANS:
        if len(_IMPORT_SCANS) >= _IMPORT_SCAN_ENTRIES:
            _IMPORT_SCANS.clear()
        _IMPORT_SCANS[key] = frozenset(direct_imports(source, path))
    return _IMPORT_SCANS[key]


def validate_widening(repo: Path, extra_tests=(), full_tests=False, reason="") -> tuple[str, ...]:
    if type(full_tests) is not bool or not isinstance(extra_tests, (list, tuple)):
        raise ValueError("extra_tests must be a list of paths and full_tests must be a boolean")
    if not isinstance(reason, str):
        raise ValueError("test_reason must be a string")
    if (extra_tests or full_tests) and not reason.strip():
        raise ValueError("test_reason is required when widening with extra_tests or full_tests")
    normalized = set()
    for path in extra_tests:
        if not isinstance(path, str) or "\\" in path or ".." in PurePosixPath(path).parts:
            raise ValueError(f"extra_tests needs repository-relative test modules: {path!r}")
        p = PurePosixPath(path)
        if p.is_absolute() or not is_test_module(path) or not (repo / path).is_file():
            raise ValueError(f"extra_tests module is missing or invalid: {path!r}")
        if not (repo / path).resolve().is_relative_to(repo.resolve()):
            raise ValueError(f"extra_tests module escapes the candidate: {path!r}")
        normalized.add(p.as_posix())
    return tuple(sorted(normalized))


@dataclass(frozen=True)
class TestSelection:
    __test__ = False
    tier: str
    base: str
    files: tuple[str, ...]
    reasons: tuple[tuple[str, str], ...]
    changed: tuple[str, ...]
    policy: str
    available: int
    extra_tests: tuple[str, ...] = ()
    full_tests: bool = False
    test_reason: str = ""

    @property
    def targets(self) -> tuple[str, ...]:
        return ("tests/",) if self.tier == "full" else self.files

    def record(self) -> dict:
        record = {**asdict(self), "rule": SELECTION_RULE, "omitted_files": self.available - len(self.files)}
        # A full run would list every module; its count and the rule say the same far smaller.
        record["files"] = record["expected_files"] = list(self.files) if self.tier == "selected" else []
        record["file_count"] = len(self.files)
        return record


def select_from_paths(repo: Path, paths: Iterable[str], *, base: str,
                      extra_tests=(), full_tests=False, test_reason="") -> TestSelection:
    """Pure candidate scan seam, also used for historical shadow comparisons."""
    repo = Path(repo)
    extra = validate_widening(repo, extra_tests, full_tests, test_reason)
    missing = [path for path in FLOOR_TESTS if not (repo / path).is_file()]
    if missing:
        raise ValueError("required gate floor is missing: " + ", ".join(missing))
    changed = tuple(sorted(set(paths)))
    files = tuple(sorted(p.relative_to(repo).as_posix() for p in (repo / "tests").rglob("test_*.py")))
    reasons = [(path, f"floor: {invariant}") for path, invariant in FLOOR_TESTS.items()]
    versions = _release_versions(repo, base) if _RELEASE_CARRIERS & set(changed) else set()
    full = [(p, "verification apparatus changed") for p in changed if apparatus_change(p)
            and not (p in _RELEASE_CARRIERS and _version_only_change(repo, base, p, versions))]
    if full_tests:
        full.append(("*", "author requested full: " + test_reason.strip()))
    selected = set(FLOOR_TESTS)
    modules = {_module(p) for p in changed if p.endswith(".py") and not p.startswith("tests/")}
    for path in files:
        if path in changed:
            selected.add(path)
            reasons.append((path, "changed test module"))
        if not full and modules:
            try:
                imports = _scanned_imports(repo, path)
            except (SyntaxError, UnicodeError) as exc:
                # An unavailable graph grants no extra automatic escalation rule.
                raise ValueError(f"import scan unavailable for {path}; repair it or request full_tests with test_reason") from exc
            bound = sorted(name for name in modules if any(i == name or i.startswith(name + ".") for i in imports))
            if bound:
                selected.add(path)
                reasons.append((path, "direct import: " + ", ".join(bound)))
    reasons.extend((p, "author widening: " + test_reason.strip()) for p in extra)
    selected.update(extra)
    # Source digest binds policy changes even before someone updates a version label.
    policy = hashlib.sha256(_POLICY_SOURCE + json.dumps(FLOOR_TESTS, sort_keys=True).encode()).hexdigest()
    return TestSelection("full" if full else "selected", base, files if full else tuple(sorted(selected)),
                         tuple(sorted(reasons + full)), changed, policy, len(files), extra, full_tests, test_reason.strip())


def select_tests(repo: Path, *, base="HEAD", extra_tests=(), full_tests=False, test_reason="") -> TestSelection:
    oid, paths = changed_paths(Path(repo), base)
    return select_from_paths(Path(repo), paths, base=oid, extra_tests=extra_tests,
                             full_tests=full_tests, test_reason=test_reason)


def refresh_selection(repo: Path, selection: TestSelection) -> TestSelection:
    return select_tests(repo, base=selection.base, extra_tests=selection.extra_tests,
                        full_tests=selection.full_tests, test_reason=selection.test_reason)


def widening_properties() -> dict:
    """Tool-schema SSOT: the model chooses consumers the import graph cannot see."""
    return {
        "extra_tests": {"type": "array", "items": {"type": "string"}, "default": [],
                        "description": "Add test module paths for indirect/dynamic, prompt, skill or cross-language consumers beyond the floor and direct imports; explain in test_reason."},
        "full_tests": {"type": "boolean", "default": False,
                       "description": "Request the full battery when impact is unresolved or broad qualification is needed; explain in test_reason. Adoption, managed updates and evolution publication require full regardless."},
        "test_reason": {"type": "string", "default": "",
                        "description": "Why extra_tests or full_tests widens verification; retained with the commit's test evidence."},
    }


def bind_commit_test_request(ctx, kwargs) -> str:
    request = {key: kwargs.pop(key, default) for key, default in
               (("extra_tests", []), ("full_tests", False), ("test_reason", ""))}
    try:
        request["extra_tests"] = validate_widening(Path(ctx.repo_dir), **{
            "extra_tests": request["extra_tests"], "full_tests": request["full_tests"], "reason": request["test_reason"]})
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR: {exc}. Nothing was staged or reviewed."
    ctx._commit_test_request = request
    return ""


def commit_test_options(ctx, *, force=False) -> dict:
    request = getattr(ctx, "_commit_test_request", None)
    return {"selected": True, **request} if request is not None and not force else {}
