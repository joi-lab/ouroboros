"""Small cross-platform process and file primitives for the Mini App skill."""

from __future__ import annotations

import contextlib
import os
import platform
import signal
import stat
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any


IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:  # pragma: no cover - imported and exercised on Windows CI
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt
else:
    import fcntl


class PlatformSupportError(RuntimeError):
    """A platform primitive could not preserve the required safety invariant."""


def machine_architecture() -> str:
    """Use the interpreter build when a scrubbed Windows env hides machine()."""
    machine = platform.machine().lower()
    if not machine and platform.system() == "Windows":
        machine = {"win-amd64": "amd64", "win-arm64": "arm64", "win32": "x86"}.get(
            sysconfig.get_platform().lower(), ""
        )
    return machine


def path_is_link_or_reparse(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points."""

    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(reparse and attributes & reparse)


def acquire_file_lock(path: Path, *, timeout_sec: float) -> int:
    """Acquire one bounded exclusive byte-range/file lock and return its fd."""

    if path_is_link_or_reparse(path):
        raise PlatformSupportError("Private process lock must not be a link.")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PlatformSupportError("Could not open a private process lock.") from exc
    try:
        opened = os.fstat(fd)
        named = path.stat()
        if path_is_link_or_reparse(path) or (
            opened.st_dev,
            opened.st_ino,
        ) != (
            named.st_dev,
            named.st_ino,
        ):
            raise PlatformSupportError("Private process lock changed during open.")
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
    deadline = time.monotonic() + max(0.0, float(timeout_sec))
    try:
        while True:
            try:
                if IS_WINDOWS:
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.fstat(fd).st_size < 1:
                        os.write(fd, b"\0")
                        os.fsync(fd)
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise PlatformSupportError("Another process still holds the private lock.")
                time.sleep(0.1)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def release_file_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        if IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    with contextlib.suppress(OSError):
        os.close(fd)


def write_lock_owner(fd: int, pid: int) -> None:
    """Write a diagnostic PID while preserving the held lock."""

    try:
        # Byte zero remains the Windows msvcrt lock byte. Keep diagnostics
        # strictly after it so writing/truncating never resizes the lock range.
        os.lseek(fd, 1, os.SEEK_SET)
        payload = f"{int(pid)}\n".encode("ascii")
        os.write(fd, payload)
        os.ftruncate(fd, len(payload) + 1)
        os.fsync(fd)
    except OSError as exc:
        raise PlatformSupportError("Could not record the private lock owner.") from exc


def fsync_directory(path: Path) -> None:
    """Durably persist a rename where directory fsync is supported."""

    if IS_WINDOWS:
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def secure_executable(path: Path) -> None:
    if IS_WINDOWS:
        return
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise PlatformSupportError("Could not secure the executable mode.") from exc


def minimal_process_environment(home: Path) -> dict[str, str]:
    """Return a proxy-free environment sufficient for one pinned executable."""

    if IS_WINDOWS:
        result = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
        }
        result.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "TEMP": str(home),
                "TMP": str(home),
            }
        )
        system_root = result.get("SYSTEMROOT") or result.get("WINDIR")
        if system_root:
            result["PATH"] = str(Path(system_root) / "System32")
        return result
    return {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(home),
    }


def require_isolated_process_group() -> None:
    if not IS_WINDOWS and os.getpgrp() != os.getpid():
        raise PlatformSupportError("Mini App companion is not in an isolated process group.")


def process_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if IS_WINDOWS:
        # On Windows CPython, os.kill(pid, 0) is destructive: signal 0 is
        # implemented through TerminateProcess. Query the process handle only.
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE,
            False,
            int(pid),
        )
        if not handle:
            # Access denied means the process exists but cannot be inspected.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def start_parent_lifeline(
    parent_pid: int,
    loop: Any,
    stop_event: Any,
    *,
    hard_kill_after_sec: float = 6.0,
) -> tuple[threading.Event, threading.Thread]:
    """Detect a vanished server even when the asyncio loop is wedged."""

    cancelled = threading.Event()

    def watch() -> None:
        while not cancelled.wait(0.25):
            if os.getppid() == parent_pid and process_alive(parent_pid):
                continue
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(stop_event.set)
            if not cancelled.wait(max(0.1, hard_kill_after_sec)):
                if IS_WINDOWS:
                    os._exit(1)
                if os.getpgrp() == os.getpid():
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.killpg(os.getpgrp(), signal.SIGKILL)
                else:
                    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                        os.kill(os.getpid(), signal.SIGKILL)
            return

    thread = threading.Thread(
        target=watch,
        daemon=True,
        name="telegram-miniapp-parent-lifeline",
    )
    thread.start()
    return cancelled, thread


if IS_WINDOWS:  # pragma: no cover - definitions are unavailable on POSIX
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _STILL_ACTIVE = 259

    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


class WindowsKillJob:
    """Kill assigned processes when this companion's final handle closes."""

    def __init__(self) -> None:
        self._handle: Any = None
        if not IS_WINDOWS:
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if handle in (0, _INVALID_HANDLE_VALUE):
            raise PlatformSupportError("Could not create a Windows tunnel Job Object.")
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            _kernel32.CloseHandle(handle)
            raise PlatformSupportError("Could not configure the Windows tunnel Job Object.")
        self._handle = handle

    def assign(self, pid: int) -> None:
        if not IS_WINDOWS:
            return
        process = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
            False,
            int(pid),
        )
        if not process:
            raise PlatformSupportError("Could not open the Windows tunnel process.")
        try:
            if not _kernel32.AssignProcessToJobObject(self._handle, process):
                raise PlatformSupportError("Could not assign the tunnel to its Windows Job Object.")
        finally:
            _kernel32.CloseHandle(process)

    def terminate(self) -> None:
        if IS_WINDOWS and self._handle is not None:
            _kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if IS_WINDOWS and self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None


__all__ = [
    "IS_WINDOWS",
    "PlatformSupportError",
    "WindowsKillJob",
    "acquire_file_lock",
    "fsync_directory",
    "machine_architecture",
    "minimal_process_environment",
    "path_is_link_or_reparse",
    "process_alive",
    "release_file_lock",
    "require_isolated_process_group",
    "secure_executable",
    "start_parent_lifeline",
    "write_lock_owner",
]
