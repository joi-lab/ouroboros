"""Byte-accurate argv/env budget checks — the E2BIG hygiene SSOT (C5).

A kernel rejects an exec not by "number of characters" but by BYTES: POSIX
``ARG_MAX`` covers the ENCODED argv strings AND the environment block together
(each string NUL-terminated, plus per-pointer overhead), and Linux additionally
caps any SINGLE string at ``MAX_ARG_STRLEN`` (128 KiB) — the limit a
one-giant-prompt argv hits first, long before the total. Windows'
``CreateProcess`` caps the whole command line at ~32 767 UTF-16 code units.

The one prior art in this repo (``code_search_rg._ARGV_CHAR_BUDGET``) counted
CHARACTERS and ignored the environment, which under-counts UTF-8 by up to 4x.
This module counts encoded bytes, includes the environment, enforces the
per-arg cap, and is the single helper every subprocess-building surface
(skill_exec, the benchmark CLI adapters) asks before exec.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Iterable, Mapping, Optional

# Linux MAX_ARG_STRLEN: the hard per-string cap (32 pages). No POSIX API exposes
# it, and macOS has no per-string cap of its own; enforcing the Linux number
# everywhere keeps a command portable across the hosts Ouroboros actually runs on.
PER_ARG_LIMIT_BYTES = 128 * 1024

# Conservative fallback when sysconf cannot answer (and the floor we never trust
# less than): POSIX guarantees only _POSIX_ARG_MAX (4096), but every supported
# host provides at least 256 KiB.
_FALLBACK_ARG_MAX = 256 * 1024

# Windows CreateProcess command-line cap: 32 767 UTF-16 code units. Bytes ≥
# code units in UTF-8 for the ASCII-dominated command lines this guards, so a
# byte budget of the same number is the conservative portable reading.
_WINDOWS_CMDLINE_LIMIT = 32_767

# Slack for the pointer array, alignment, and the kernel's own bookkeeping that
# ARG_MAX charges beyond the raw string bytes.
DEFAULT_HEADROOM_BYTES = 8 * 1024


def encoded_arg_bytes(value: Any) -> int:
    """The bytes ONE argv/env string costs the kernel: UTF-8 + its NUL."""
    return len(str(value).encode("utf-8", errors="surrogateescape")) + 1


def argv_env_bytes(argv: Iterable[Any], env: Optional[Mapping[str, Any]] = None) -> int:
    """Total encoded bytes of ``argv`` plus the environment block.

    ``env=None`` measures the CURRENT process environment — what
    ``subprocess.Popen`` inherits when the caller passes none.
    """
    total = sum(encoded_arg_bytes(arg) for arg in argv)
    env_map = os.environ if env is None else env
    for key, value in env_map.items():
        # One env string costs "KEY=value\0": key bytes + '=' + value bytes + NUL.
        total += (len(str(key).encode("utf-8", errors="surrogateescape")) + 1
                  + encoded_arg_bytes(value))
    return total


def system_argv_limit_bytes() -> int:
    """This host's total argv+env byte budget, from the OS where it can be asked."""
    if sys.platform == "win32":
        return _WINDOWS_CMDLINE_LIMIT
    try:
        limit = int(os.sysconf("SC_ARG_MAX"))
    except (ValueError, OSError, AttributeError):
        limit = 0
    return limit if limit > 0 else _FALLBACK_ARG_MAX


def argv_budget_excess(
    argv: Iterable[Any],
    *,
    env: Optional[Mapping[str, Any]] = None,
    limit_bytes: Optional[int] = None,
    per_arg_bytes: int = PER_ARG_LIMIT_BYTES,
    headroom_bytes: int = DEFAULT_HEADROOM_BYTES,
) -> str:
    """Why this exec would (or could) fail with E2BIG — or "" when it fits.

    Checks the per-argument cap first (the limit a giant single argument hits
    first on Linux), then the total argv+env budget against the host limit
    minus headroom. The returned string names the offender and the numbers, so
    a refusal is actionable: move the payload to a file/stdin transport.
    """
    argv_list = [str(arg) for arg in argv]
    for index, arg in enumerate(argv_list):
        # The kernel measures the string WITH its NUL: `copy_strings` rejects a
        # string whose length including the terminator exceeds MAX_ARG_STRLEN, so
        # the largest string that still execs is `limit - 1` content bytes. Testing
        # `content > limit` let the exact-boundary case through and E2BIG'd at exec.
        if encoded_arg_bytes(arg) > per_arg_bytes:
            preview = arg[:80] + ("…" if len(arg) > 80 else "")
            return (
                f"argv[{index}] is {encoded_arg_bytes(arg) - 1} bytes (limit "
                f"{per_arg_bytes} per argument including its NUL, Linux "
                f"MAX_ARG_STRLEN): {preview!r}. Pass bulk payloads via a file "
                "or stdin, never argv."
            )
    # The SAME per-string cap applies to the environment: the kernel copies env
    # strings through the identical path, so one oversized `KEY=value` (a prompt
    # exported into the child's env) fails the exec exactly like an oversized
    # argument — and was not checked at all.
    env_map = os.environ if env is None else env
    for key, value in env_map.items():
        entry = f"{key}={value}"
        if encoded_arg_bytes(entry) > per_arg_bytes:
            return (
                f"environment variable {str(key)!r} is {encoded_arg_bytes(entry) - 1} "
                f"bytes as KEY=value (limit {per_arg_bytes} per string including its "
                "NUL, Linux MAX_ARG_STRLEN). Pass bulk payloads via a file or stdin, "
                "never the environment."
            )
    limit = int(limit_bytes) if limit_bytes else system_argv_limit_bytes()
    budget = max(4096, limit - max(0, int(headroom_bytes)))
    total = argv_env_bytes(argv_list, env)
    if total > budget:
        return (
            f"argv+env total {total} bytes exceeds the exec budget {budget} "
            f"(host limit {limit} minus {headroom_bytes} headroom). Pass bulk "
            "payloads via a file or stdin, never argv."
        )
    return ""


__all__ = [
    "DEFAULT_HEADROOM_BYTES",
    "PER_ARG_LIMIT_BYTES",
    "argv_budget_excess",
    "argv_env_bytes",
    "encoded_arg_bytes",
    "system_argv_limit_bytes",
]
