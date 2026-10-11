"""The local battery entry point keeps the gate's recipe and a bounded serial overlap."""

import importlib.util
import pathlib
import types

import pytest

from ouroboros.preflight_runner import LANE_EXCLUSION_EXPR, PARALLEL_PASS_FLAGS
from tests import conftest

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def run_tests():
    spec = importlib.util.spec_from_file_location("run_tests_script", REPO / "scripts" / "run_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _item(nodeid, *, serial):
    markers = [types.SimpleNamespace(name="serial")] if serial else []
    item = types.SimpleNamespace(nodeid=nodeid, added=[])
    item.get_closest_marker = lambda name: next((m for m in markers if m.name == name), None)
    item.add_marker = item.added.append
    return item


def _group(item):
    (mark,) = item.added
    assert mark.name == "xdist_group"
    return mark.args[0]


@pytest.mark.parametrize("workers, shards", [(2, 1), (4, 1), (7, 1), (8, 2), (12, 3), (16, 4), (64, 4)])
def test_serial_overlap_is_bounded_by_the_worker_count(run_tests, workers, shards):
    assert run_tests.serial_shards(workers) == shards


def test_sequential_mode_keeps_the_gate_marker_split_and_xdist_flags(run_tests, monkeypatch):
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TEST_WORKERS", "6")
    (parallel_label, parallel), (serial_label, serial) = run_tests.battery_commands(sequential=True)
    assert (parallel_label, serial_label) == ("parallel", "serial")
    assert parallel[3:] == ["tests/", "-m", f"not serial and {LANE_EXCLUSION_EXPR}", *PARALLEL_PASS_FLAGS]
    assert serial[3:] == ["tests/", "-m", f"serial and {LANE_EXCLUSION_EXPR}"]


def test_default_mode_is_one_run_over_both_lanes(run_tests, monkeypatch):
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TEST_WORKERS", "8")
    ((label, command),) = run_tests.battery_commands(sequential=False)
    assert label == "overlapped"
    assert command[:3] == [run_tests.sys.executable, "-m", "pytest"]
    command = command[3:]
    assert command[command.index("-m") + 1] == LANE_EXCLUSION_EXPR
    assert command[command.index("--dist") + 1] == "loadgroup"
    assert command[command.index("-n") + 1] == "auto"
    assert command[-1] == "--serial-shards=2"
    untouched = [flag for flag in PARALLEL_PASS_FLAGS if flag.startswith("--") and flag != "--dist"]
    assert all(flag in command for flag in untouched)


def test_the_worker_override_steers_xdist_like_the_gate(run_tests, monkeypatch):
    seen = {}
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TEST_WORKERS", "3")
    monkeypatch.setattr(run_tests.subprocess, "call", lambda argv, cwd, env: seen.update(env=env) or 0)
    assert run_tests._run("x", [run_tests.sys.executable, "-m", "pytest"], run_tests.REPO) == 0
    assert seen["env"]["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "3"


def test_sequential_with_pytest_arguments_is_refused(run_tests, monkeypatch):
    monkeypatch.setattr(run_tests, "_run", lambda label, argv, cwd: pytest.fail("nothing must run"))
    with pytest.raises(SystemExit) as exit_info:
        run_tests.main(["--sequential", "tests/test_x.py"])
    assert exit_info.value.code == 2


def test_a_bare_call_never_means_a_subset(run_tests, monkeypatch):
    calls = []
    monkeypatch.setattr(run_tests, "_node_lane", lambda: 0)
    monkeypatch.setattr(run_tests, "_run", lambda label, argv, cwd: calls.append((label, argv)) or 0)
    assert run_tests.main([]) == 0
    assert [label for label, _ in calls] == ["overlapped"]
    calls.clear()
    assert run_tests.main(["tests/test_x.py", "-k", "one"]) == 0
    assert calls == [("focused", [run_tests.sys.executable, "-m", "pytest", "tests/test_x.py", "-k", "one"])]


def test_a_missing_node_is_a_red_not_run_never_a_silent_skip(run_tests, monkeypatch):
    monkeypatch.setattr(run_tests, "resolve_node", lambda: None, raising=False)
    monkeypatch.setattr(run_tests, "_run", lambda label, argv, cwd: pytest.fail("pytest must not start"))
    from ouroboros import preflight_node
    monkeypatch.setattr(preflight_node, "resolve_node", lambda: None)
    monkeypatch.setattr(preflight_node, "candidate_node_tests", lambda repo: ["tests/x.test.js"])
    assert run_tests.main([]) == 1


def test_a_red_lane_stops_the_battery_and_an_empty_battery_is_red(run_tests, monkeypatch):
    monkeypatch.setattr(run_tests, "_node_lane", lambda: 0)
    codes = iter([1])
    seen = []
    monkeypatch.setattr(run_tests, "_run", lambda label, argv, cwd: seen.append(label) or next(codes))
    assert run_tests.main(["--sequential"]) == 1
    assert seen == ["parallel"]
    monkeypatch.setattr(run_tests, "_run", lambda label, argv, cwd: 5)
    assert run_tests.main(["--sequential"]) == 1


def test_lane_groups_pin_serial_files_and_keep_other_files_whole():
    items = [
        _item("tests/test_a.py::test_one", serial=False),
        _item("tests/test_s.py::test_one", serial=True),
        _item("tests/test_a.py::test_two", serial=False),
        _item("tests/test_s.py::test_two[x]", serial=True),
        _item("tests/test_t.py::test_one", serial=True),
        _item("tests/test_c.py::TestBox::test_one", serial=False),
        _item("tests/test_s.py::TestSer::test_three", serial=True),
    ]
    conftest._pin_lane_groups(items, 2)
    assert [item.nodeid.split("::")[0] for item in items[:4]] == ["tests/test_s.py", "tests/test_s.py", "tests/test_t.py", "tests/test_s.py"]
    groups = {item.nodeid: _group(item) for item in items}
    assert groups["tests/test_a.py::test_one"] == groups["tests/test_a.py::test_two"] == "tests/test_a.py"
    assert groups["tests/test_c.py::TestBox::test_one"] == "tests/test_c.py::TestBox"
    assert groups["tests/test_s.py::TestSer::test_three"] == groups["tests/test_s.py::test_one"]
    assert groups["tests/test_s.py::test_one"] == groups["tests/test_s.py::test_two[x]"]
    assert {groups["tests/test_s.py::test_one"], groups["tests/test_t.py::test_one"]} <= {"serial0", "serial1"}


def test_lane_groups_are_inert_without_the_option():
    items = [_item("tests/test_s.py::test_one", serial=True), _item("tests/test_a.py::test_one", serial=False)]
    conftest._pin_lane_groups(items, 0)
    assert [item.added for item in items] == [[], []]
    assert [item.nodeid for item in items] == ["tests/test_s.py::test_one", "tests/test_a.py::test_one"]


def test_the_documented_command_is_the_script():
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "test:\n\tuv run --locked python scripts/run_tests.py\n" in makefile
    for doc in ("CONTRIBUTING.md", "README.md", "docs/development/14-build-and-ci.md", "prompts/SYSTEM.md"):
        assert "scripts/run_tests.py" in (REPO / doc).read_text(encoding="utf-8"), doc
