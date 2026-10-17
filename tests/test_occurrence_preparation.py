"""References/callers start from admitted paths; source facts are prepared on demand.

The inventory boundary between path admission (containment, name, size) and
source-fact extraction lets the two token operations scan current sources under
their own deadline instead of first parsing the whole repository. Selector
ordering parses only the token-matched files. A bounded inventory refresh keeps
valid unvisited facts as disposable cache only. Other operations keep their
eager inventory (an explicit residual, witnessed below).
"""
from __future__ import annotations

import subprocess
from collections import Counter
from types import SimpleNamespace

import pytest

from ouroboros import code_intelligence as ci
from ouroboros import code_navigation as nav
from ouroboros import code_occurrences as occ
from ouroboros.tools import query_code as qc
from ouroboros.tools.query_code import _query_code
from ouroboros.tools.registry import ToolContext


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo, ToolContext(repo_dir=repo, drive_root=tmp_path / "data")


def write(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="")


def body(result):
    return result.split("\n\n", 1)[1] if "\n\n" in result else ""


def count_parses(monkeypatch, clock=None, cost=(0.0,)):
    """Count Python source-fact extractions; optionally charge them fake time (``cost[0]``)."""
    parsed, original = Counter(), ci._python_facts

    def python_facts(text, rel_path, **kwargs):
        parsed[str(rel_path)] += 1
        if clock is not None:
            clock[0] += cost[0]
        return original(text, rel_path, **kwargs)

    monkeypatch.setattr(ci, "_python_facts", python_facts)
    return parsed


def fake_clock(monkeypatch):
    clock = [0.0]
    timer = SimpleNamespace(monotonic=lambda: clock[0])
    for module in (ci, nav, occ, qc):
        monkeypatch.setattr(module, "time", timer)
    return clock


def _needle_project(repo, unrelated=0):
    write(repo, "a.py", "def needle():\n    return 1\n")
    write(repo, "b.py", "from a import needle\nneedle()\n")
    for index in range(unrelated):
        write(repo, f"pkg/unrelated{index}.py", "VALUE = 1\n")


def test_references_and_callers_parse_only_token_matched_sources(project, monkeypatch):
    repo, ctx = project
    _needle_project(repo, unrelated=30)
    parsed = count_parses(monkeypatch)
    refs = _query_code(ctx, "references", query="needle")
    assert "a.py:1:5" in body(refs) and "b.py:2:1" in body(refs), refs
    callers = _query_code(ctx, "callers", query="needle")
    assert body(callers).startswith("b.py:2:1 ") and "call?" in body(callers).splitlines()[0], callers
    assert "files: 32 selected, 32 text-scanned, 2 matched, 2 parsed" in callers, callers
    assert not parsed, parsed  # no inventory fact extraction before or during the scans
    ordered = _query_code(ctx, "callers", query="needle", path="a.py")
    assert parsed == Counter({"a.py": 1, "b.py": 1}), parsed  # facts for selector ordering only
    assert "1 outline declarations share this name; selected file a.py" in ordered, ordered
    assert "QUERY_CODE_TRUNCATED" not in ordered and "b.py:2:1" in body(ordered), ordered
    cache = ci.inventory_cache_path(repo, ctx.drive_root)
    assert not cache.exists(), "a path-only view must never be stored as indexed facts"
    parsed.clear()
    symbols = _query_code(ctx, "symbols", query="needle")  # other operations keep their eager outline
    assert "a.py:1 function needle()" in symbols and sum(parsed.values()) == 32 and cache.exists()
    stored = cache.read_bytes()
    _query_code(ctx, "references", query="needle")
    _query_code(ctx, "callers", query="needle", path="a.py")
    assert cache.read_bytes() == stored  # nor may it overwrite the shared full-source cache


def test_cold_parse_cost_no_longer_starves_callers_under_the_same_deadline(project, monkeypatch):
    repo, ctx = project
    _needle_project(repo)
    write(repo, "c.py", "x = 1\n")
    clock, cost = fake_clock(monkeypatch), [16.0]
    count_parses(monkeypatch, clock, cost)  # three cold parses would exceed the 45 s budget
    callers = _query_code(ctx, "callers", query="needle")
    assert "QUERY_CODE_TRUNCATED" not in callers and "b.py:2:1" in body(callers), callers
    assert "3 text-scanned" in callers and clock[0] == 0.0
    # Residual, explicit: an eager-outline operation still pays the cold parse first.
    clock[0] = 0.0
    definition = _query_code(ctx, "definition", query="needle")
    assert "QUERY_CODE_TRUNCATED" in definition and "wall budget hit" in definition, definition
    # Selector ordering spends the SAME deadline; rows survive a partial order.
    clock[0], cost[0] = 0.0, 30.0
    ordered = _query_code(ctx, "callers", query="needle", path="a.py")
    assert "b.py:2:1" in body(ordered), ordered
    assert "import candidate ordering time limit; partial heuristic order" in ordered, ordered


def test_path_only_views_equal_parsed_views_with_admission_and_ordering(project):
    repo, ctx = project
    write(repo, "lib/a.py", "def needle():\n    return 1\n")
    write(repo, "use.py", "from lib.a import needle\nneedle()\n")
    write(repo, "other.py", "def needle():\n    pass\nneedle()\n")
    write(repo, "notes.md", "needle is documented here\n")
    write(repo, "api_token.json", '{"needle": "fixture"}\n')  # sensitive by name: never read
    write(repo, "big.py", "needle()\n" + "#" * 1_500_000 + "\n")  # beyond the scan size limit
    (repo / "blob.py").write_bytes(b"needle()\n\x00binary\n")
    protected = repo / "oracle.py"
    write(repo, "oracle.py", "needle()\n")
    for op in ("references", "callers"):
        for selector in ("", "lib/a.py"):
            views = [nav.inventory_view(ci.build_code_inventory(repo, parse=parse, exclude_paths=[protected]),
                                        op=op, query="needle", path=selector) for parse in (True, False)]
            parsed, paths = views
            assert paths.rows == parsed.rows and paths.limits == parsed.limits, (op, selector)
            assert paths.notes == parsed.notes and paths.incomplete == parsed.incomplete
            assert not any(path in "\n".join(paths.rows) for path in ("api_token", "big.py", "blob.py", "oracle"))
            assert {"sensitive: 1", "oversized: 1", "binary: 1"} <= set(paths.limits), paths.limits
    ordered = nav.inventory_view(ci.build_code_inventory(repo, parse=False), op="callers", query="needle",
                                 path="lib/a.py")
    assert [row.split(":")[0] for row in ordered.rows][:2] == ["use.py", "other.py"], ordered.rows
    assert "2 outline declarations share this name; selected file lib/a.py" in ordered.notes


def test_missing_grammar_parses_current_python_calls_without_text_callers(project, monkeypatch):
    repo, ctx = project
    write(repo, "a.py", "def needle():\n    return 1\n")
    write(repo, "b.py", "import a\n\n\ndef run():\n    return a.needle()\n")
    write(repo, "c.py", "# needle() is only mentioned here\nTEXT = 'needle()'\n")
    write(repo, "broken.py", "def needle(:\n    needle()\n")
    monkeypatch.setattr(ci, "_ts_parser", lambda grammar: None)
    inventory = ci.build_code_inventory(repo, parse=False)
    assert {file.path: file.disposition for file in inventory.files} == dict.fromkeys(
        ("a.py", "b.py", "broken.py", "c.py"), "not_parsed")
    scan = occ.scan_occurrences(inventory, "needle", mode="callers")
    assert [(row.path, row.line, row.slot, row.enclosing) for row in scan.rows] == [
        ("b.py", 5, "ast.Call.func", "run")]
    assert "python ast local calls" in scan.methods and not scan.incomplete
    assert scan.limits["syntax error (no Python AST calls)"] == 1
    full = occ.scan_occurrences(ci.build_code_inventory(repo), "needle", mode="callers")
    assert [row.render() for row in full.rows] == [row.render() for row in scan.rows]
    result = _query_code(ctx, "callers", query="needle")
    assert body(result).splitlines() == ["b.py:5 ast.Call.func call? in run |     return a.needle()"], result


def test_bounded_refresh_keeps_unvisited_cache_entries_as_disposable_facts(project, monkeypatch):
    repo, ctx = project
    data = ctx.drive_root
    for name in ("a.py", "b.py", "c.py"):
        write(repo, name, f"def {name[0]}_needle():\n    pass\n")
    full = ci.build_code_inventory(repo, drive_root=data)
    with monkeypatch.context() as bounded:
        bounded.setattr(ci, "_MAX_ENUMERATION_PATHS", 1)
        partial = ci.build_code_inventory(repo, drive_root=data)
    assert [f.path for f in partial.files] == ["a.py"]  # returned rows: only what this refresh saw
    assert any(note.startswith("enumeration_entry_limit") for note in partial.enumeration_limits)
    stored = ci.load_cached_inventory(repo, data)
    assert stored.files == full.files and stored.coverage == full.coverage  # unvisited tail retained
    calls, original = [], ci._file_fact

    def counted(root, path):
        calls.append(path.name)
        return original(root, path)

    monkeypatch.setattr(ci, "_file_fact", counted)
    again = ci.build_code_inventory(repo, drive_root=data)
    assert calls == [] and again.files == full.files  # baseline reparsed b.py and c.py here
    with monkeypatch.context() as bounded:  # every reuse rechecks path and hash
        bounded.setattr(ci, "_MAX_ENUMERATION_PATHS", 1)
        ci.build_code_inventory(repo, drive_root=data)
    write(repo, "c.py", "def changed_needle():\n    pass\n")
    (repo / "b.py").unlink()
    refreshed = ci.build_code_inventory(repo, drive_root=data)
    assert calls == ["c.py"], calls
    assert [f.path for f in refreshed.files] == ["a.py", "c.py"]
    assert refreshed.files[1].symbols[0].name == "changed_needle"
    assert [f.path for f in ci.load_cached_inventory(repo, data).files] == ["a.py", "c.py"]  # deletion dropped


def test_deadline_cut_refresh_keeps_only_walked_unvisited_entries(project, monkeypatch):
    repo, ctx = project
    data = ctx.drive_root
    for name in ("a.py", "b.py", "c.py", "d.py"):
        write(repo, name, "x = 1\n")
    ci.build_code_inventory(repo, drive_root=data)
    for name in ("a.py", "b.py", "c.py"):
        write(repo, name, "x = 2\n")
    (repo / "d.py").unlink()  # a complete walk proves this deletion
    clock = fake_clock(monkeypatch)
    enumerate_files, file_fact = ci._enumerate_files, ci._file_fact

    def slow_walk(*args, **kwargs):
        found = enumerate_files(*args, **kwargs)
        clock[0] += 44
        return found

    def slow_fact(root, path):
        clock[0] += 2
        return file_fact(root, path)

    monkeypatch.setattr(ci, "_enumerate_files", slow_walk)
    monkeypatch.setattr(ci, "_file_fact", slow_fact)
    partial = ci.build_code_inventory(repo, drive_root=data)
    assert [f.path for f in partial.files] == ["a.py"]
    assert any(note.startswith("inventory_time_limit") for note in partial.enumeration_limits)
    stored = {f.path: f for f in ci.load_cached_inventory(repo, data).files}
    assert sorted(stored) == ["a.py", "b.py", "c.py"]
    assert stored["a.py"] == partial.files[0]
    monkeypatch.setattr(ci, "_enumerate_files", enumerate_files)
    monkeypatch.setattr(ci, "time", __import__("time"))
    calls = []

    def counted(root, path):
        calls.append(path.name)
        return file_fact(root, path)

    monkeypatch.setattr(ci, "_file_fact", counted)
    current = ci.build_code_inventory(repo, drive_root=data)
    assert calls == ["b.py", "c.py"]  # stale tail facts are rechecked, never served as current
    assert [f.path for f in current.files] == ["a.py", "b.py", "c.py"]


def test_scoped_and_restricted_bounded_views_never_replace_the_full_cache(project, monkeypatch):
    repo, ctx = project
    data = ctx.drive_root
    for name in ("a.py", "sub/b.py", "sub/c.py"):
        write(repo, name, "x = 1\n")
    ci.build_code_inventory(repo, drive_root=data)
    cache = ci.inventory_cache_path(repo, data)
    before = cache.read_bytes()
    write(repo, "sub/b.py", "x = 2\n")
    with monkeypatch.context() as bounded:
        bounded.setattr(ci, "_MAX_ENUMERATION_PATHS", 1)
        assert ci.build_code_inventory(repo, drive_root=data, scope="sub").enumeration_limits
        ci.build_code_inventory(repo, drive_root=data, path_allowed=lambda path: path.name != "a.py")
    assert cache.read_bytes() == before
    with monkeypatch.context() as bounded:
        bounded.setattr(ci, "_MAX_ENUMERATION_PATHS", 1)
        ci.build_code_inventory(repo, drive_root=data, parse=False)
    assert cache.read_bytes() == before
