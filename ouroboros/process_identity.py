"""Validated kernel-derived birth identity: the one parser death proof compares.

A living process keeps one birth from fork to exit, across exec, so two
validated tokens of ONE kind that differ prove that the process now at a pid is
not the recorded one. Equal tokens, two kinds, and any unvalidated spelling
prove nothing. Validated kinds:

- ``darwin-start:<sec>.<usec6>``: macOS ``p_starttime`` from
  ``platform_darwin_abi.kernel_birth``; the same bytes whatever the reader's
  timezone or locale. A wall-clock instant, so equality only, never order;
- ``win-filetime:<int>``: Windows creation FILETIME (UTC);
- ``<ticks>.<32-hex boot_id>`` and ``<ticks>``/``<ticks>.``: Linux start ticks,
  boot-qualified or boot-relative.

``ps -o lstart=`` text (the macOS ``platform_layer.process_start_time``) renders
the start in the writer's timezone and locale, so two readings of one living
process can differ: it is never a kind. ``process_kernel_birth`` is additive
evidence beside ``process_start_time``, whose token, ledger fields and equality
comparators stay as they are: processes of an older release, such as a
supervisor beside newer workers, keep agreeing with this one.
"""

from __future__ import annotations

import re

from ouroboros import platform_darwin_abi


def birth_identity(token: str) -> tuple | None:
    """The identity a validated birth token spells, or None for anything else."""
    if not isinstance(token, str):
        return None
    if match := re.fullmatch(r"darwin-start:([0-9]+)\.([0-9]{6})", token):
        return ("darwin", int(match.group(1)), int(match.group(2)))
    if match := re.fullmatch(r"win-filetime:([0-9]+)", token):
        return ("windows", int(match.group(1)))
    if match := re.fullmatch(r"([0-9]+)\.([0-9a-f]{32})", token):
        return ("linux_boot", int(match.group(1)), match.group(2))
    if match := re.fullmatch(r"([0-9]+)\.?", token):
        return ("ticks", int(match.group(1)))
    return None


def different_birth(live: str, recorded: str) -> bool:
    """Positive only when both identities are validated, of one kind, and unequal."""
    live_id, recorded_id = birth_identity(live), birth_identity(recorded)
    return bool(live_id and recorded_id and live_id[0] == recorded_id[0] and live_id != recorded_id)


def process_kernel_birth(pid: int) -> str | None:
    """``pid``'s validated birth token, or None when it cannot be read or validated.

    macOS reads the kernel record only (no ``ps`` fallback); elsewhere the
    existing ``process_start_time`` token counts only when it parses as a kind.
    """
    if platform_darwin_abi.IS_DARWIN:
        birth = platform_darwin_abi.kernel_birth(pid)
        return f"darwin-start:{birth[0]}.{birth[1]:06d}" if birth else None
    from ouroboros.platform_layer import process_start_time

    try:
        token = process_start_time(pid)
    except Exception:
        return None  # unknown evidence holds; it must never abort a custody sweep
    return token if birth_identity(token) else None


def process_zombie(pid: int) -> bool | None:
    """Whether ``pid`` is an exited, unreaped process, from the one macOS kernel read; None elsewhere
    or when unreadable (the caller then asks its platform's own probe)."""
    if platform_darwin_abi.IS_DARWIN:
        record = platform_darwin_abi.kernel_birth(pid)
        return record[2] if record else None
    return None
