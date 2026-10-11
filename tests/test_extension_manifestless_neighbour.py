"""Execution discovery ignores a manifestless directory beside a valid skill.

Regression for the #1195 F4 review finding: the execution callers (liveness,
reconcile, the extension child) must resolve a selected identity with ORDINARY
discovery semantics. Only the repair/publication lane may opt a manifestless
directory in as a hidden ambiguity; a reviewed extension next to a same-named
directory without a manifest keeps executing.
"""
from __future__ import annotations

import pathlib

from ouroboros import extension_loader
from ouroboros.skill_loader import (
    discover_selected_skill_candidates,
    discover_skill_identity,
)

from tests._extension_loader_shared import _prepare_extension
from tests._extension_loader_shared import (  # noqa: F401  (autouse fixture applies on import)
    _clear_loader_state,
)


def _manifestless_neighbour(drive_root: pathlib.Path, name: str) -> pathlib.Path:
    # Same canonical identity, no SKILL.md: the layout the publish preflight
    # treats as ambiguous and ordinary discovery treats as absent.
    neighbour = drive_root / "skills" / "external" / name
    neighbour.mkdir(parents=True)
    (neighbour / "notes.txt").write_text("not a skill\n", encoding="utf-8")
    return neighbour


def test_manifestless_neighbour_is_invisible_to_execution_discovery(tmp_path):
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path, "neighbour_case", "def register(api):\n    pass\n", permissions=[],
    )
    _manifestless_neighbour(drive_root, loaded.name)

    ordinary = discover_skill_identity(drive_root, loaded.name, repo_path=str(repo_root))
    assert [s.identity_collision for s in ordinary] == [False]
    assert ordinary[0].skill_dir.resolve() == loaded.skill_dir.resolve()

    # The stricter repair/publication resolver still sees the ambiguity.
    strict = discover_selected_skill_candidates(drive_root, loaded.name, repo_path=str(repo_root))
    assert len(strict) == 2 and all(s.identity_collision for s in strict)


def test_reviewed_extension_still_loads_beside_a_manifestless_neighbour(tmp_path):
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path, "neighbour_exec", "def register(api):\n    pass\n", permissions=[],
    )
    _manifestless_neighbour(drive_root, loaded.name)

    state = extension_loader.reconcile_extension(
        loaded.name, drive_root, lambda: {}, repo_path=str(repo_root),
    )
    assert state["action"] == "extension_loaded", state
    assert state["live_loaded"] is True

    runtime = extension_loader.runtime_state_for_skill_name(
        loaded.name, drive_root, repo_path=str(repo_root),
    )
    assert runtime["live_loaded"] is True
    assert runtime.get("identity_collision", False) is False
    assert runtime["reason"] != "missing"
