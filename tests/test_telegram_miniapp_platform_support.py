from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "telegram" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import platform_support  # noqa: E402


def test_posix_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    if platform_support.IS_WINDOWS:
        pytest.skip("POSIX lock contract")
    path = tmp_path / "lease.lock"
    first = platform_support.acquire_file_lock(path, timeout_sec=0)
    platform_support.write_lock_owner(first, 12345)
    assert path.read_bytes()[1:] == b"12345\n"
    try:
        with pytest.raises(platform_support.PlatformSupportError):
            platform_support.acquire_file_lock(path, timeout_sec=0)
    finally:
        platform_support.release_file_lock(first)
    second = platform_support.acquire_file_lock(path, timeout_sec=0)
    platform_support.release_file_lock(second)


def test_minimal_environment_drops_proxy_and_secrets(tmp_path: Path) -> None:
    env = platform_support.minimal_process_environment(tmp_path)
    assert env["HOME"] == str(tmp_path)
    assert not any("PROXY" in key.upper() for key in env)
    assert "TELEGRAM_BOT_TOKEN" not in env


def test_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert platform_support.path_is_link_or_reparse(link)


@pytest.mark.parametrize("pid", [1, 4242])
def test_windows_liveness_probe_never_calls_os_kill(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    calls: list[tuple[int, int, int]] = []

    class Kernel:
        @staticmethod
        def OpenProcess(flags: int, inherit: int, pid: int) -> int:
            calls.append((flags, inherit, pid))
            return 99

        @staticmethod
        def GetExitCodeProcess(_handle: int, pointer: object) -> int:
            pointer._obj.value = 259  # type: ignore[attr-defined]
            return 1

        @staticmethod
        def CloseHandle(_handle: int) -> int:
            return 1

    monkeypatch.setattr(platform_support, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_support, "_kernel32", Kernel(), raising=False)
    monkeypatch.setattr(platform_support, "_PROCESS_QUERY_LIMITED_INFORMATION", 0x1000, raising=False)
    monkeypatch.setattr(platform_support, "_SYNCHRONIZE", 0x100000, raising=False)
    monkeypatch.setattr(platform_support, "_STILL_ACTIVE", 259, raising=False)
    monkeypatch.setattr(
        platform_support,
        "ctypes",
        SimpleNamespace(byref=lambda value: SimpleNamespace(_obj=value), get_last_error=lambda: 0),
        raising=False,
    )
    monkeypatch.setattr(
        platform_support,
        "wintypes",
        SimpleNamespace(DWORD=lambda: SimpleNamespace(value=0)),
        raising=False,
    )
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("os.kill is destructive on Windows"))
    assert platform_support.process_alive(pid)
    assert calls and calls[0][2] == pid


@pytest.mark.parametrize(
    "pid,outcome,alive",
    [(1, None, True), (4242, ProcessLookupError, False), (4242, PermissionError, True)],
)
def test_posix_liveness_probes_positive_pids_including_pid1(
    monkeypatch: pytest.MonkeyPatch, pid: int, outcome: type[BaseException] | None, alive: bool
) -> None:
    """A bare container server is PID 1: it must reach the real probe."""
    calls: list[tuple[int, int]] = []

    def fake_kill(target: int, sig: int) -> None:
        calls.append((target, sig))
        if outcome is not None:
            raise outcome()

    monkeypatch.setattr(platform_support, "IS_WINDOWS", False)
    monkeypatch.setattr(os, "kill", fake_kill)
    assert platform_support.process_alive(pid) is alive
    assert calls == [(pid, 0)]


@pytest.mark.parametrize("pid", [0, -1, -4242])
def test_invalid_pids_are_dead_without_any_system_probe(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("invalid PID reached os.kill"))
    assert platform_support.process_alive(pid) is False
