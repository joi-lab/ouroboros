"""The fixture observes nested pytest completion and timeout without changing either."""
import json
import sys

import pytest

from ouroboros import preflight_runner

pytestmark = pytest.mark.serial


@pytest.mark.parametrize("hang", [False, True])
def test_preflight_diagnostics_preserve_exit_and_timeout(tmp_path, monkeypatch, preflight_timeout_diagnostics, hang):
    trace_dir = preflight_timeout_diagnostics(dump_after=2)
    root = tmp_path / "nested"
    repo = root / "repo"
    tests = repo / "tests"
    tests.mkdir(parents=True)
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (repo / "conftest.py").write_text(
        "import atexit, os, time\ndef hold_exit():\n    time.sleep(60)\n"
        # xdist has its own ten-second worker reap. Hold the controller instead
        # so startup has room and the unchanged preflight timeout owns the kill.
        f"if {hang!r} and not os.environ.get('PYTEST_XDIST_WORKER'):\n    atexit.register(hold_exit)\n",
        encoding="utf-8",
    )
    (tests / "test_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TEST_WORKERS", "2")
    module = preflight_runner._install_worker_probe(root)
    # Own the nested pytest temporary tree. Otherwise its controller may spend
    # teardown cleaning unrelated numbered runs instead of reaching hold_exit.
    code, output, containment = preflight_runner._execute_pytest_pass(
        sys.executable, repo, root,
        ["tests", "-q", "-n", "2", "-p", module, "--basetemp", str(root / "pytest")], 20,
    )
    assert code == (None if hang else 0)
    assert not containment
    logs = [path.read_text(encoding="utf-8") for path in trace_dir.glob("*.log")]
    events = [json.loads(line) for text in logs for line in text.splitlines() if line.startswith('{"event":')]
    assert {row["worker"] for row in events if row["event"] == "probe_import"} == {"controller", "gw0", "gw1"}
    if hang:
        assert "[100%]" in output  # The test passed; interpreter teardown is still live.
        assert any(row["event"] == "before_kill" and row["returncode"] is None for row in events)
        assert any("Timeout (0:00:02)!" in text and "hold_exit" in text for text in logs)
    else:
        assert not any(row["event"] == "before_kill" for row in events)
        assert {row["worker"] for row in events if row["event"] == "atexit"} == {"controller", "gw0", "gw1"}
