"""The census-path chip table speaks the server's own census vocabulary (docs/DESIGN.md "Status marks").

A task card without a projected work (a child, Main's owner-action message, an Activity row, a replayed card) draws
its census phase's shape from ``CENSUS_MARKS`` in ``web/modules/task_phase_chip.js``. The server reads the same
phases into a work's activity in ``gateway/work_status.classify_member`` and names its shape with ``mark_of``. This
test reads both sources: a phase the server classifies that the chip has no shape for, a chip phase the server does
not know, or a shape the server would name differently fails.
"""
from __future__ import annotations

import ast
import pathlib
import re

from ouroboros.gateway import work_status

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "ouroboros" / "gateway" / "work_status.py"
CHIP = ROOT / "web" / "modules" / "task_phase_chip.js"


def _server_phases(source: str) -> set[str]:
    """Every census phase literal ``classify_member`` compares its row's ``phase`` with."""
    tree = ast.parse(source)
    func = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == "classify_member")
    phases: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id == "phase":
            for comparator in node.comparators:
                phases |= {const.value for const in ast.walk(comparator)
                           if isinstance(const, ast.Constant) and isinstance(const.value, str)}
    return phases


def _chip_table(source: str) -> dict[str, str]:
    body = re.search(r"export const CENSUS_MARKS = Object\.freeze\(\{(.*?)\}\);", source, re.S)
    assert body, "CENSUS_MARKS moved: update this test"
    return dict(re.findall(r"(\w+): '(\w+)'", body.group(1)))


def _server_mark(phase: str, **facts) -> str:
    member = work_status.classify_member({"activity_id": "t", "phase": phase, "task_attempt": 1, **facts})
    return work_status.mark_of(member["activity"], member["confirmed"])


def test_every_census_phase_the_server_classifies_has_the_servers_shape_on_the_chip():
    table = _chip_table(CHIP.read_text(encoding="utf-8"))
    assert set(table) == _server_phases(SERVER.read_text(encoding="utf-8"))
    for phase, shape in table.items():
        # A census pause names its cause; the owner's own Pause is the plain one (a sleep is the case below).
        facts = {"pause_cause": "owner"} if phase.startswith("budget_") else {}
        assert _server_mark(phase, **facts) == shape, phase


def test_a_cold_sleep_waits_by_itself_on_both_sides():
    assert _server_mark("budget_paused", pause_cause="sleep") == "still"
    assert _server_mark("budget_paused", pause_cause="owner") == "pause"
    assert "phase === 'budget_paused' && pauseCause === 'sleep' ? 'still'" in CHIP.read_text(encoding="utf-8")


def test_the_pin_fires_on_a_new_server_phase_and_stays_quiet_on_the_current_one():
    source = SERVER.read_text(encoding="utf-8")
    current = _server_phases(source)
    assert {"unknown", "budget_paused", "budget_pausing", "queued", "finalizing", "working", "thinking",
            "preparing"} <= current
    marker = '    if phase == "queued":'
    assert marker in source, "classify_member moved: update this test"
    grown = source.replace(marker, '    if phase == "brand_new_phase":\n        pass\n' + marker, 1)
    assert _server_phases(grown) == current | {"brand_new_phase"}
    assert _server_phases(grown) != set(_chip_table(CHIP.read_text(encoding="utf-8")))
