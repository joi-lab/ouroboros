"""The repository index a retrieving reviewer's brief carries.

The index is a MAP, not a pack: every tracked path is accounted for, the
policy-excluded classes collapse per directory, and only the change-relative
paths (the touched files and the files that import them) carry structural
detail. No budget decides what a reviewer may see, and no file body is ever
rendered here.
"""

from __future__ import annotations

from pathlib import Path

from ouroboros.tools.review_context_atlas import (
    INDEX_MAX_ROW_IMPORTS,
    INDEX_MAX_ROW_SYMBOLS,
    INDEX_MAX_TOUCHED_IMPORTERS,
    REPOSITORY_INDEX_SCHEMA_VERSION,
    repository_index,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _index_rows(text: str) -> dict[str, str]:
    """``path -> disposition`` for every row of the coverage index section."""
    body = text.split("```text\n", 1)[1].split("```", 1)[0]
    rows = {}
    for line in body.splitlines():
        if "\t" in line:
            disposition, path = line.split("\t", 1)
            rows[path] = disposition
        elif line.strip():
            rows[line] = "indexed"  # a bare row is the ordinary indexed class
    return rows


def test_index_accounts_for_every_tracked_path_with_no_file_body(tmp_path):
    _write(tmp_path / "app.py", "import helper\n\ndef run():\n    return helper.value()\n")
    _write(tmp_path / "helper.py", "def value():\n    return 42\n")
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "main.py", "from .helper import thing\n\nanswer = thing()\n")
    _write(tmp_path / "pkg" / "helper.py", "def thing():\n    return 7\n")
    _write(tmp_path / "docs" / "CHECKLISTS.md", "canonical checklist\n")
    tracked = ("app.py", "helper.py", "pkg/__init__.py", "pkg/main.py",
               "pkg/helper.py", "docs/CHECKLISTS.md")

    text, manifest = repository_index(tmp_path, touched_paths=["app.py"], tracked_paths=tracked)

    assert manifest["schema_version"] == REPOSITORY_INDEX_SCHEMA_VERSION
    assert manifest["strategy"] == "repository_index"
    assert manifest["tracked_count"] == len(tracked)
    assert set(_index_rows(text)) == set(tracked)
    assert manifest["dispositions"] == {"indexed": 6}
    # Facts, never bodies: no file content reaches the index.
    assert "def value():" not in text
    assert "canonical checklist" not in text
    assert manifest["index_chars"] == len(text)
    assert manifest["tokens_estimate"] > 0


def test_policy_excluded_classes_collapse_to_one_row_per_directory(tmp_path):
    _write(tmp_path / "app.py", "x = 1\n")
    for idx in range(4):
        _write(tmp_path / "tests" / f"test_mod_{idx}.py", "def test_ok():\n    assert True\n")
    _write(tmp_path / "devtools" / "helper.py", "y = 2\n")
    _write(tmp_path / "assets" / "notes.txt", "asset text\n")
    _write(tmp_path / "web" / "vendor" / "chart.umd.min.js", "minified();\n")
    (tmp_path / "web" / "icon.png").write_bytes(b"\x89PNG\r\n\x00")
    tracked = (
        "app.py",
        *(f"tests/test_mod_{idx}.py" for idx in range(4)),
        "devtools/helper.py",
        "assets/notes.txt",
        "web/vendor/chart.umd.min.js",
        "web/icon.png",
    )

    text, manifest = repository_index(tmp_path, touched_paths=["app.py"], tracked_paths=tracked)

    rows = _index_rows(text)
    assert rows["tests/ (4 files)"] == "excluded_test"
    assert rows["devtools/ (1 files)"] == "excluded_dir"
    assert rows["assets/ (1 files)"] == "excluded_dir"  # a physical class would win first
    assert rows["web/vendor/ (1 files)"] == "vendored_minified"
    assert rows["web/ (1 files)"] == "binary_media"
    assert rows["app.py"] == "indexed"
    # No per-path row for a collapsed member…
    assert "tests/test_mod_0.py" not in rows
    # …while the disposition counts still account for every path individually.
    assert manifest["dispositions"] == {
        "binary_media": 1, "excluded_dir": 2, "excluded_test": 4,
        "indexed": 1, "vendored_minified": 1,
    }
    assert manifest["tracked_count"] == len(tracked)


def test_rich_rows_cover_the_touched_paths_and_their_direct_importers(tmp_path):
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "core.py", "VERSION = 3\n\ndef run():\n    return VERSION\n")
    _write(tmp_path / "pkg" / "caller_a.py", "import pkg.core\n\nvalue = pkg.core.run()\n")
    _write(tmp_path / "pkg" / "caller_b.py", "from pkg import core\n\nvalue = core.run()\n")
    _write(tmp_path / "pkg" / "unrelated.py", "import json\n")
    tracked = ("pkg/__init__.py", "pkg/core.py", "pkg/caller_a.py",
               "pkg/caller_b.py", "pkg/unrelated.py")

    text, manifest = repository_index(
        tmp_path, touched_paths=["pkg/core.py"], tracked_paths=tracked)

    assert [row["path"] for row in manifest["touched"]] == ["pkg/core.py"]
    touched = manifest["touched"][0]
    assert touched["imported_by"] == ["pkg/caller_a.py", "pkg/caller_b.py"]
    assert touched["imported_by_total"] == 2
    assert touched["symbols"] == ["VERSION", "run"] and touched["symbols_total"] == 2
    assert touched["size"] == (tmp_path / "pkg" / "core.py").stat().st_size
    assert len(touched["sha256"]) == 16 and touched["language"] == "py"
    # `from pkg import core` counts exactly like `import pkg.core`.
    assert [row["path"] for row in manifest["importers"]] == [
        "pkg/caller_a.py", "pkg/caller_b.py"]
    # A file that imports nothing touched gets a coverage row and nothing more.
    assert "pkg/unrelated.py" in _index_rows(text)
    assert "- pkg/unrelated.py —" not in text
    assert "- pkg/core.py —" in text and "- pkg/caller_b.py —" in text


def test_javascript_importers_resolve_through_relative_specifiers(tmp_path):
    _write(tmp_path / "web" / "modules" / "chat.js", "export const chat = 1;\n")
    _write(tmp_path / "web" / "app.js", "import { chat } from './modules/chat.js';\n")
    _write(tmp_path / "web" / "modules" / "panel.js",
           "import { chat } from './chat.js';\nimport 'external-pkg';\n")
    tracked = ("web/modules/chat.js", "web/app.js", "web/modules/panel.js")

    _text, manifest = repository_index(
        tmp_path, touched_paths=["web/modules/chat.js"], tracked_paths=tracked)

    assert manifest["touched"][0]["imported_by"] == ["web/app.js", "web/modules/panel.js"]
    assert [row["path"] for row in manifest["importers"]] == [
        "web/app.js", "web/modules/panel.js"]
    # A bare package specifier names no tracked path, so it is not an edge.
    panel = [row for row in manifest["importers"] if row["path"] == "web/modules/panel.js"][0]
    assert panel["imports"] == ["web/modules/chat.js"]


def test_importer_list_is_capped_per_touched_path_with_the_total_disclosed(tmp_path):
    _write(tmp_path / "hub.py", "VALUE = 1\n")
    importers = [f"mod_{idx:03d}.py" for idx in range(INDEX_MAX_TOUCHED_IMPORTERS + 7)]
    for rel in importers:
        _write(tmp_path / rel, "import hub\n")

    text, manifest = repository_index(
        tmp_path, touched_paths=["hub.py"], tracked_paths=("hub.py", *importers))

    touched = manifest["touched"][0]
    assert touched["imported_by_total"] == len(importers)
    assert len(touched["imported_by"]) == INDEX_MAX_TOUCHED_IMPORTERS
    assert touched["imported_by"] == sorted(importers)[:INDEX_MAX_TOUCHED_IMPORTERS]
    assert manifest["importer_count"] == INDEX_MAX_TOUCHED_IMPORTERS
    assert manifest["importer_cap_per_touched_path"] == INDEX_MAX_TOUCHED_IMPORTERS
    # The cap is disclosed, not silent (BIBLE P1).
    assert f"(+7 more of {len(importers)})" in text


def test_symbol_and_import_lists_are_capped_with_their_totals(tmp_path):
    source = "\n".join(f"import pkg_{idx}" for idx in range(30))
    source += "\n" + "\n".join(f"def f_{idx}():\n    return {idx}\n" for idx in range(20))
    _write(tmp_path / "wide.py", source)

    _text, manifest = repository_index(
        tmp_path, touched_paths=["wide.py"], tracked_paths=("wide.py",))

    row = manifest["touched"][0]
    assert len(row["symbols"]) == INDEX_MAX_ROW_SYMBOLS and row["symbols_total"] == 20
    assert len(row["imports"]) == INDEX_MAX_ROW_IMPORTS and row["imports_total"] == 30


def test_a_touched_path_keeps_its_own_row_whatever_its_class(tmp_path):
    _write(tmp_path / "tests" / "test_touched.py", "import mod\n\ndef test_ok():\n    assert mod\n")
    _write(tmp_path / "tests" / "test_other.py", "def test_other():\n    assert True\n")
    _write(tmp_path / "devtools" / "touched_script.py", "import mod\n")
    _write(tmp_path / "mod.py", "x = 1\n")
    tracked = ("tests/test_touched.py", "tests/test_other.py",
               "devtools/touched_script.py", "mod.py")

    text, manifest = repository_index(
        tmp_path,
        touched_paths=["tests/test_touched.py", "devtools/touched_script.py"],
        tracked_paths=tracked,
    )

    rows = _index_rows(text)
    assert rows["tests/test_touched.py"] == "indexed"
    assert rows["devtools/touched_script.py"] == "indexed"
    # The unrelated wider test is still collapsed by policy.
    assert rows["tests/ (1 files)"] == "excluded_test"
    assert [row["path"] for row in manifest["touched"]] == [
        "tests/test_touched.py", "devtools/touched_script.py"]


def test_a_listed_importer_from_a_collapsed_class_keeps_its_own_indexed_row(tmp_path):
    """The row the index prints and the class it prints cannot disagree: a test
    that imports a touched module is shown per path, so it is not reported as an
    excluded one."""
    _write(tmp_path / "mod.py", "x = 1\n")
    _write(tmp_path / "tests" / "test_mod.py", "import mod\n")
    _write(tmp_path / "tests" / "test_other.py", "def test_other():\n    assert True\n")
    tracked = ("mod.py", "tests/test_mod.py", "tests/test_other.py")

    text, manifest = repository_index(
        tmp_path, touched_paths=["mod.py"], tracked_paths=tracked)

    rows = _index_rows(text)
    assert rows["tests/test_mod.py"] == "indexed"
    assert rows["tests/ (1 files)"] == "excluded_test"
    assert manifest["dispositions"] == {"excluded_test": 1, "indexed": 2}


def test_physical_classes_are_reported_and_secrets_are_never_read_into_the_index(tmp_path):
    _write(tmp_path / ".env.production", "TOKEN=secret-value\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x00")
    _write(tmp_path / "script.min.js", "minified();\n")
    (tmp_path / "huge.py").write_bytes(b"x" * (1_048_576 + 1))
    _write(tmp_path / "gone.py", "placeholder\n")
    (tmp_path / "gone.py").unlink()
    tracked = (".env.production", "image.png", "script.min.js", "huge.py", "gone.py")

    text, manifest = repository_index(
        tmp_path, touched_paths=[".env.production", "huge.py"], tracked_paths=tracked)

    rows = {row["path"]: row for row in manifest["touched"]}
    assert rows[".env.production"]["disposition"] == "sensitive"
    assert rows[".env.production"]["size"] == 0 and rows[".env.production"]["sha256"] == ""
    assert rows["huge.py"]["disposition"] == "oversized"
    index_rows = _index_rows(text)
    assert index_rows["gone.py"] == "missing"
    assert index_rows[".env.production"] == "sensitive"
    assert "secret-value" not in text
    assert manifest["dispositions"]["binary_media"] == 1
    assert manifest["dispositions"]["vendored_minified"] == 1


def test_a_touched_path_outside_the_tracked_set_is_disclosed_not_dropped(tmp_path):
    _write(tmp_path / "mod.py", "x = 1\n")

    _text, manifest = repository_index(
        tmp_path, touched_paths=["mod.py", "brand_new.py"], tracked_paths=("mod.py",))

    rows = {row["path"]: row for row in manifest["touched"]}
    assert rows["brand_new.py"]["disposition"] == "untracked"
    assert rows["brand_new.py"]["imported_by"] == []


def test_the_index_is_byte_identical_for_one_tree_and_one_change(tmp_path):
    for idx in range(6):
        _write(tmp_path / f"mod_{idx}.py", f"import mod_{(idx + 1) % 6}\n\nVALUE = {idx}\n")
    tracked = tuple(f"mod_{idx}.py" for idx in range(6))

    first = repository_index(tmp_path, touched_paths=["mod_2.py"], tracked_paths=tracked)
    second = repository_index(
        tmp_path, touched_paths=["mod_2.py"], tracked_paths=tuple(reversed(tracked)))

    assert first[0] == second[0]
    assert first[1] == second[1]


def test_the_index_walks_git_when_the_caller_names_no_tracked_paths(tmp_path):
    import subprocess

    _write(tmp_path / "tracked.py", "x = 1\n")
    _write(tmp_path / "untracked.py", "y = 2\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    import os

    for args in (["init", "-q"], ["add", "tracked.py"], ["commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                       capture_output=True, env={**os.environ, **env})

    text, manifest = repository_index(tmp_path, touched_paths=["tracked.py"])

    assert manifest["tracked_count"] == 1
    assert set(_index_rows(text)) == {"tracked.py"}
