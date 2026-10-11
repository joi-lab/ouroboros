"""Regression tests: the wheel must ship every web/vendor asset.

``web/index.html`` unconditionally loads ``/static/vendor/...`` assets
(KaTeX css/js/fonts), so a wheel or uv install whose package-data misses
those files serves 404s at runtime. Guard two invariants without building
a wheel:

1. Every static vendor ``href``/``src`` referenced by ``web/index.html``
   exists on disk and is covered by at least one
   ``[tool.setuptools.package-data]`` glob of the ``web`` package.
2. Every file on disk under ``web/vendor/`` is covered as well, so newly
   vendored assets cannot silently be dropped from the wheel.

setuptools package-data globs are non-recursive (``*`` never crosses
``/``), so the matcher below models exactly that: each glob level matches
exactly one path level.
"""
from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent
WEB_DIR = REPO_ROOT / "web"


def _package_data_web_globs() -> list[str]:
    """Extract the ``web = [...]`` glob list from [tool.setuptools.package-data].

    Parsed with a targeted regex because ``tomllib`` is unavailable on the
    Python 3.10 CI floor and ``tomli`` is not a declared dependency.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    section = re.search(
        r"^\[tool\.setuptools\.package-data\]\s*$(.*?)(?=^\[|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert section is not None, "pyproject.toml lacks [tool.setuptools.package-data]"
    web_list = re.search(r"^web\s*=\s*\[(.*?)\]", section.group(1), flags=re.MULTILINE | re.DOTALL)
    assert web_list is not None, "[tool.setuptools.package-data] lacks a 'web' entry"
    globs = re.findall(r'"([^"]+)"', web_list.group(1))
    assert globs, "[tool.setuptools.package-data] web entry is empty"
    return globs


def _glob_matches(pattern: str, rel_path: str) -> bool:
    """Non-recursive setuptools-style glob match: '*' never crosses '/'."""
    regex = "".join(
        "[^/]*" if ch == "*" else ("[^/]" if ch == "?" else re.escape(ch))
        for ch in pattern
    )
    return re.fullmatch(regex, rel_path) is not None


def _covered(rel_path: str, globs: list[str]) -> bool:
    return any(_glob_matches(glob, rel_path) for glob in globs)


def test_index_html_vendor_references_covered_by_package_data():
    globs = _package_data_web_globs()
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    refs = re.findall(r'(?:href|src)="/static/([^"#?]+)', html)
    vendor_refs = sorted({ref for ref in refs if ref.startswith("vendor/")})
    assert vendor_refs, (
        "web/index.html no longer references /static/vendor assets; "
        "update this test alongside the vendor removal"
    )
    for ref in vendor_refs:
        assert (WEB_DIR / ref).is_file(), f"web/index.html references missing file web/{ref}"
        assert _covered(ref, globs), (
            f"web/{ref} is referenced by index.html but not covered by any "
            "[tool.setuptools.package-data] glob: wheel installs would 404"
        )


def test_vendor_tree_fully_covered_by_package_data():
    globs = _package_data_web_globs()
    vendor_dir = WEB_DIR / "vendor"
    vendor_files = sorted(
        path.relative_to(WEB_DIR).as_posix()
        for path in vendor_dir.rglob("*")
        if path.is_file()
    )
    assert vendor_files, "web/vendor is empty or missing"
    missing = [rel for rel in vendor_files if not _covered(rel, globs)]
    assert not missing, (
        "web/vendor files not covered by any [tool.setuptools.package-data] "
        f"glob (would be dropped from the wheel): {missing}"
    )
