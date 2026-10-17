"""Tests for scratchpad auto-consolidation.

Verifies:
- Threshold is 30000 chars
- should_consolidate_scratchpad triggers only for block scratchpads
- a pre-block flat scratchpad is never rewritten
- the consolidation route keeps an explicit local light model or inherits a local main one
"""
import importlib
import os


from ouroboros.memory import Memory

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_consolidator():
    import sys
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.consolidator")


def test_consolidation_threshold_is_30000():
    """Scratchpad auto-consolidation must trigger at 30000."""
    mod = _get_consolidator()
    assert mod.SCRATCHPAD_CONSOLIDATION_THRESHOLD == 30000, (
        f"Expected 30000, got {mod.SCRATCHPAD_CONSOLIDATION_THRESHOLD}"
    )


def test_should_not_consolidate_small_scratchpad(tmp_path):
    mod = _get_consolidator()
    drive = tmp_path / "data"
    (drive / "memory").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    mem = Memory(drive_root=drive)
    mem.scratchpad_path().write_text("x" * 10000, encoding="utf-8")
    assert not mod.should_consolidate_scratchpad(mem)


def test_should_not_consolidate_large_flat_scratchpad(tmp_path):
    mod = _get_consolidator()
    drive = tmp_path / "data"
    (drive / "memory").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    mem = Memory(drive_root=drive)
    mem.scratchpad_path().write_text("x" * 35000, encoding="utf-8")
    assert not mod.should_consolidate_scratchpad(mem)


def test_should_not_consolidate_missing_file(tmp_path):
    mod = _get_consolidator()
    drive = tmp_path / "data"
    (drive / "memory").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    mem = Memory(drive_root=drive)
    assert not mod.should_consolidate_scratchpad(mem)


def test_flat_scratchpad_consolidation_is_retired():
    """Pre-block flat scratchpads require manual upgrade; they are not rewritten."""
    mod = _get_consolidator()
    assert not hasattr(mod, "_consolidate_scratchpad_flat")


def test_consolidate_scratchpad_does_not_rewrite_flat_scratchpad(tmp_path):
    mod = _get_consolidator()
    drive = tmp_path / "data"
    (drive / "memory").mkdir(parents=True)
    (drive / "logs").mkdir(parents=True)
    mem = Memory(drive_root=drive)
    legacy = "legacy flat scratchpad\n" + ("x" * 35000)
    mem.scratchpad_path().write_text(legacy, encoding="utf-8")

    class FailingLLM:
        def chat(self, *args, **kwargs):
            raise AssertionError("flat scratchpad consolidation should not call the LLM")

    result = mod.consolidate_scratchpad(mem, FailingLLM())

    assert result is None
    assert mem.scratchpad_path().read_text(encoding="utf-8") == legacy


def test_consolidation_route_preserves_explicit_local_light(monkeypatch):
    mod = _get_consolidator()
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "local-light")
    monkeypatch.setenv("USE_LOCAL_LIGHT", "true")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert mod._consolidation_route() == ("local-light", True)


def test_consolidation_route_empty_light_inherits_local_main(monkeypatch):
    mod = _get_consolidator()
    monkeypatch.setenv("OUROBOROS_MODEL", "local-main")
    monkeypatch.delenv("OUROBOROS_MODEL_LIGHT", raising=False)
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert mod._consolidation_route() == ("local-main", True)
