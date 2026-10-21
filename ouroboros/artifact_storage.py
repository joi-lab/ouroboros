"""Lossless filesystem storage for artifacts. Clone-on-copy gives a new inode sharing blocks where it
can give one (macOS APFS ``clonefile(2)``, Linux ``FICLONE`` on Btrfs and reflink-enabled XFS); any
other platform, filesystem or volume boundary answers ``False`` and the caller streams a copy as before.
Never a hard link: an in-place write to the source would change the stored result, while a clone
diverges at the first write to either side. The platform calls are function-local under ``sys.platform``
guards (docs/development/10-platform-abstraction-rule.md names this module's exception)."""
from __future__ import annotations

import contextlib
import os
import pathlib
import stat
import subprocess
import sys

from ouroboros.temp_sweep import atomic_temp_path as temporary_sibling

_FICLONE = 0x40049409  # _IOW(0x94, 9, int), <linux/fs.h>


def clone_file(source: pathlib.Path, destination: pathlib.Path) -> bool:
    """Make DESTINATION (which must not exist) a block-sharing clone of SOURCE; ``False`` when this
    platform or filesystem cannot, with nothing left at DESTINATION, so the caller copies instead.
    Only a regular file is cloned: a directory would be cloned whole and a FIFO's open would block,
    so anything else is left to the streamed copy's own typed refusal. The clone's times are its
    own: ``clonefile`` copies the source's, and a temporary that looks as old as the result it copies
    would be reaped as a hard kill's orphan by the temp sweep's age guard while the copy is in flight."""
    source, destination = pathlib.Path(source), pathlib.Path(destination)
    try:
        if not stat.S_ISREG(os.stat(source).st_mode):
            return False
        if sys.platform == "darwin":
            cloned = _clonefile(source, destination)
        elif sys.platform.startswith("linux"):
            cloned = _ficlone(source, destination)
        else:
            return False
    except OSError:
        return False
    if cloned:
        with contextlib.suppress(OSError):  # the clone stands either way; the caller sets the published times
            os.utime(destination)
    return cloned


def _clonefile(source: pathlib.Path, destination: pathlib.Path) -> bool:
    import ctypes

    try:
        clonefile = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):
        return False
    clonefile.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int)
    clonefile.restype = ctypes.c_int
    # A refusal (another volume, a non-APFS filesystem, an unreadable source) leaves no file behind:
    # the streamed copy then succeeds or reports the real fault with its own message.
    return clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0


def _ficlone(source: pathlib.Path, destination: pathlib.Path) -> bool:
    import fcntl

    try:
        # Non-blocking open: a FIFO swapped in since the regular-file check never parks this thread.
        reader = os.open(source, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(reader).st_mode):
            return False
        with open(destination, "xb") as writer:
            try:
                fcntl.ioctl(writer.fileno(), _FICLONE, reader)
                return True
            except OSError:
                pass
    except OSError:
        return False
    finally:
        os.close(reader)
    destination.unlink(missing_ok=True)  # the empty file of a refused clone
    return False


def compress_in_place(path: pathlib.Path) -> bool:
    """Best-effort transparent compression, preserving logical size, bytes and sha256.

    macOS verifies a ditto-compressed sibling before replacement; NTFS compresses
    through its native file control. Linux/Docker/Colab and other platforms keep
    ordinary files: Btrfs needs its compression attribute BEFORE the data write.
    A refusal is only a missed space saving, never a failed patch publication.
    """
    try:
        if sys.platform == "darwin":
            return _compress_macos(pathlib.Path(path))
        if sys.platform == "win32":
            return _compress_ntfs(pathlib.Path(path))
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def _compress_macos(path: pathlib.Path) -> bool:
    from ouroboros.artifacts import stream_artifact_file
    from ouroboros.config import ARTIFACT_COMPRESSION_TIMEOUT_SEC

    expected = stream_artifact_file(path)
    temporary = temporary_sibling(path)  # the atomic signature: a hard kill's leftover is swept, never a deliverable
    try:
        subprocess.run(
            ["/usr/bin/ditto", "--hfsCompression", str(path), str(temporary)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=ARTIFACT_COMPRESSION_TIMEOUT_SEC,
        )
        stream_artifact_file(temporary, expected=expected)
        if not temporary.stat().st_flags & stat.UF_COMPRESSED:
            return False
        os.replace(temporary, path)
        return True
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _compress_ntfs(path: pathlib.Path) -> bool:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    control = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
    control.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    control.restype = wintypes.BOOL
    mode, returned = ctypes.c_ushort(1), wintypes.DWORD()  # COMPRESSION_FORMAT_DEFAULT
    with path.open("r+b") as source:
        return bool(control(msvcrt.get_osfhandle(source.fileno()), 0x9C040,  # FSCTL_SET_COMPRESSION
                            ctypes.byref(mode), ctypes.sizeof(mode), None, 0, ctypes.byref(returned), None))
