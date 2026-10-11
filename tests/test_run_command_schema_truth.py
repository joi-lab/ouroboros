"""The ``run_command`` schema states exactly what its handler does (CHECKLISTS
critical surface 2: the schema is the tool's contract).

Two handler facts the description used to misstate or omit:

* only a bare shell builtin as ``cmd[0]`` is refused (``cd``, ``export``, ...);
  inside ``["sh", "-c", "cd x && ..."]`` a ``cd`` works;
* a background child (``&``, ``nohup``) is not a service: while it holds
  stdout/stderr the call waits until ``timeout_sec``; afterwards nothing tracks
  or stops it — the shell that spawned it has already exited, so the timeout's
  group kill finds no tree to terminate (the ``&`` child is reparented to init
  with a group id nothing resolves any more), and a child that detached its
  stdio outlives the call the same way — so ``start_service`` is the long-lived
  path.

Issue #502 asks schemas to shrink, so the truthful words are paid for inside the
same description: its length is pinned to the pre-change size plus a small
allowance.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import time
from types import SimpleNamespace

import pytest

from ouroboros.tools.shell import _SHELL_BUILTINS, _run_shell, get_tools
from ouroboros.tools.shell_process import _active_subprocesses

_DESCRIPTION_CHARS_BEFORE = 481  # measured on ef33a0a956c2; T1 allowance ~80
_ALLOWANCE = 80


def _schema() -> dict:
    return next(entry.schema for entry in get_tools() if entry.name == "run_command")


def _ctx(tmp_path: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo_dir=tmp_path, drive_root=tmp_path,
        drive_logs=lambda: pathlib.Path(str(tmp_path)),
    )


def test_description_pays_for_its_words_inside_the_issue_502_budget():
    description = _schema()["description"]
    assert len(description) <= _DESCRIPTION_CHARS_BEFORE + _ALLOWANCE, len(description)


def test_description_states_the_bare_builtin_refusal_and_the_sh_c_escape():
    description = _schema()["description"]
    assert "cmd[0]" in description, "the refusal is about a builtin as cmd[0], not about cd anywhere"
    assert "cd is rejected" not in description, "cd inside sh -c works; the blanket sentence was false"
    assert '["sh", "-c"' in description
    # The cwd parameter no longer repeats the refusal the description already names.
    assert "rejected cd" not in _schema()["parameters"]["properties"]["cwd"]["description"]


def test_description_states_what_happens_to_a_background_child():
    description = _schema()["description"]
    # What the empirical tests below prove on every platform: the call stalls until
    # timeout_sec while the child holds stdio, and nothing tracks the child afterwards.
    # Whether the timeout kill still REACHES it is the platform's business (Linux can
    # resolve the exited shell's process group and kills it, macOS cannot and it
    # survives), so the description promises neither a cleanup nor a survival.
    for word in ("&", "nohup", "stalls the call until timeout_sec", "once the call returns nothing tracks it", "start_service"):
        assert word in description, word
    assert "killed" not in description and "cleaned up" not in description  # no promise the host does not keep


@pytest.mark.parametrize("builtin", sorted(_SHELL_BUILTINS))
def test_handler_refuses_only_a_bare_builtin_as_cmd0(tmp_path, builtin):
    result = _run_shell(_ctx(tmp_path), [builtin, "x"])
    assert result.startswith("⚠️ SHELL_CMD_ERROR"), result


@pytest.mark.skipif(os.name != "posix", reason="uses sh")
def test_handler_runs_cd_inside_sh_c(tmp_path):
    target = tmp_path / "inner"
    target.mkdir()
    result = _run_shell(_ctx(tmp_path), ["sh", "-c", f"cd {target.as_posix()} && pwd"])
    assert "SHELL_CMD_ERROR" not in result
    assert target.resolve().name in result, result


@pytest.mark.skipif(os.name != "posix", reason="uses sh")
def test_background_child_holding_stdout_makes_the_call_wait_until_timeout(tmp_path):
    marker = f"sleep 9.{os.getpid() % 1000}1"
    started = time.monotonic()
    result = _run_shell(_ctx(tmp_path), ["sh", "-c", f"{marker} & echo started"], timeout_sec=1)
    elapsed = time.monotonic() - started
    try:
        assert result.startswith("⚠️ TOOL_TIMEOUT"), result
        assert elapsed >= 0.9, elapsed
        # "once the call returns nothing tracks it": the shell exited at once. On
        # Linux the timeout kill still resolves the dead shell's process group and
        # takes the child with it; on macOS it cannot and the child keeps running.
        # Either way the host holds no handle on it.
        alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        assert not any(str(proc.pid) in alive for proc in list(_active_subprocesses)), "nothing tracks it"
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)


@pytest.mark.skipif(os.name != "posix", reason="uses sh")
def test_detached_background_child_returns_at_once_and_is_untracked(tmp_path):
    marker = f"sleep 9.{os.getpid() % 1000}2"
    result = _run_shell(_ctx(tmp_path), ["sh", "-c", f"nohup {marker} >/dev/null 2>&1 & echo started"], timeout_sec=5)
    try:
        assert "TOOL_TIMEOUT" not in result and "started" in result, result
        alive = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        assert alive, "the detached child outlives the call"
        assert not any(str(proc.pid) in alive for proc in list(_active_subprocesses)), "nothing tracks it"
    finally:
        subprocess.run(["pkill", "-f", marker], check=False)
