"""Reviewable skill payload: what a reviewer may see, and how much of it.

Owns the assembly of the skill file pack a packet reviewer reads: the pack-level
token budget derived from the review stack's prompt-budget SSOT, the text read
that refuses unreadable or non-UTF-8 runtime payloads, the binary extensions
named early on the refusal path, and the split of an over-budget skill into
budget-sized packs reviewed in separate passes. A retrieving reviewer is not
bound by that budget: it gets the whole payload when its first send holds it,
otherwise the per-file index of the frozen copy it reads (``frozen_payload_files``).
The three typed refusals live here with the reads that raise them, so a caller
can distinguish a shrink-the-file case from an opaque-payload case without
parsing a message.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.skill_review_passes import (
    WASM_MAGIC,
    SkillBinaryPayload as _SkillBinaryPayload,
    binary_file_descriptor,
    executable_magic_kind,
)
from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET
from ouroboros.utils import estimate_tokens


# The reviewable skill payload is bound by ONE pack-level token budget (the review
# stack's SSOT REVIEW_PROMPT_TOKEN_BUDGET), not per-file / file-count BYTE caps: a 76 KB
# data file or a 41-file skill is fully reviewable when the whole pack fits a 1M-context
# reviewer. Loadable executables / unreadable files are still refused (safety, not size).
# Headroom reserves the rest of the reviewer prompt (governance docs + checklist + framing).

_SKILL_PACK_TOKEN_HEADROOM = 120_000


def _skill_pack_token_budget() -> int:
    """Estimated-token budget for the assembled skill file pack alone (SSOT
    REVIEW_PROMPT_TOKEN_BUDGET minus headroom for the rest of the reviewer prompt)."""
    return max(1, REVIEW_PROMPT_TOKEN_BUDGET - _SKILL_PACK_TOKEN_HEADROOM)


# Lexical download filter retained ONLY for the marketplace fetcher's coarse pre-gate
# (ouroboros/marketplace/fetcher.py). Skill REVIEW itself judges file CONTENT — loader
# magic bytes, see ``skill_review_passes.executable_magic_kind`` — never filenames
# (X4/В21): a renamed ELF is still blocked; a text file with a scary extension stays reviewable.
_LOADABLE_BINARY_EXTENSIONS = frozenset(
    {".so", ".dylib", ".dll", ".pyc", ".pyo", ".node", ".exe", ".bin"}
)


class _SkillFileOverBudget(RuntimeError):
    """Raised when a SINGLE skill file alone exceeds the reviewer token budget, so it
    cannot be placed in any budget-sized review pack without truncating it (which
    review refuses). Honest-pending: the maintainer must shrink/split that one file.

    The whole-skill over-budget case is NOT an error — it is split into multiple
    budget-sized packs and reviewed in separate passes (see ``_build_skill_file_packs``
    and ``_run_chunked_skill_review``)."""

    def __init__(self, relpath: str, tokens: int, budget: int) -> None:
        super().__init__(
            f"Skill file {relpath!r} alone is ~{tokens} tokens > {budget} reviewer budget."
        )
        self.relpath = relpath
        self.tokens = tokens
        self.budget = budget


class _SkillFileUnreadable(RuntimeError):
    """Raised when a runtime-reachable file cannot be read; review fails closed."""

    def __init__(self, relpath: str, err: BaseException) -> None:
        super().__init__(
            f"Skill file {relpath!r} unreadable: {type(err).__name__}: {err}"
        )
        self.relpath = relpath
        self.err = err


def _read_skill_file(
    path: pathlib.Path, *, relpath: str = ""
) -> tuple[Optional[str], bytes, Optional[Dict[str, Any]]]:
    """Read one skill file: ``(text, sha256_digest, descriptor)`` — exactly one set.
    Native executable magic blocks ordinary review; Cyber retains these bytes
    through a descriptor. Binary formats never become text merely because they
    decode as UTF-8. Unreadable source remains an actual read failure."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        # Fail closed; placeholders would let review pass over missing payload.
        raise _SkillFileUnreadable(relpath or path.name, exc) from exc
    rel = relpath or path.name
    try:
        text: Optional[str] = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    kind = executable_magic_kind(data, is_utf8_text=text is not None)
    if kind:
        from ouroboros.config import get_runtime_mode
        from ouroboros.runtime_mode_policy import runtime_mode_at_least

        if not runtime_mode_at_least(get_runtime_mode(), "cyber_pro"):
            raise _SkillBinaryPayload(rel, len(data), kind)
    digest = hashlib.sha256(data).digest()
    if text is not None and not kind and not data.startswith(WASM_MAGIC):
        return text, digest, None
    descriptor = binary_file_descriptor(rel, data, filename=path.name)
    if kind:
        descriptor["format_from_magic"] = kind
    return None, digest, descriptor


def _build_skill_file_packs(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
    expected_content_hash: str = "",
    split: bool = True,
) -> List[str]:
    """Return the fenced-code review pack(s) mirroring the skill content-hash surface.

    Normally ONE pack. When the whole pack would exceed the reviewer token budget,
    the files are split into multiple budget-sized packs (greedy by file) so each is
    reviewed in a SEPARATE pass and EVERY byte is still reviewed — never silently
    truncated. A single file that alone exceeds the budget cannot be split without
    truncating it, so it raises ``_SkillFileOverBudget`` (honest-pending). The
    budget is the packet seat's: ``split=False`` (a panel without packet seats)
    returns the whole payload as one pack and refuses no file for its size.

    The bound is ONE pack-level token budget, not per-file BYTE caps. Loadable
    executables (content magic bytes) / unreadable files are still refused by
    ``_read_skill_file``; other non-UTF-8 files enter the pack as descriptors."""
    from ouroboros.skill_identity import payload_bucket, release_declaration, release_digest_from_file_digests
    from ouroboros.skill_loader import _iter_payload_files  # pylint: disable=W0212

    skill_dir = skill_dir.resolve()
    files = _iter_payload_files(
        skill_dir,
        manifest_entry=manifest_entry,
        manifest_scripts=manifest_scripts,
    )
    if not files:
        if expected_content_hash and release_digest_from_file_digests(
                [], release_declaration(skill_dir), bucket=payload_bucket(skill_dir)) != expected_content_hash:
            raise _SkillFileUnreadable("(payload snapshot)", RuntimeError("skill payload changed after hashing"))
        return ["(empty skill directory — no manifest, no payload)"]

    budget = _skill_pack_token_budget()
    packs: List[str] = []
    current: List[str] = []
    current_tokens = 0
    file_digests: List[tuple[str, bytes]] = []
    for file_path in files:
        rel = file_path.relative_to(skill_dir).as_posix()
        body, file_digest, descriptor = _read_skill_file(file_path, relpath=rel)
        file_digests.append((rel, file_digest))
        if descriptor is not None:  # typed descriptor, never raw non-UTF-8 bytes
            body = json.dumps(descriptor, indent=2, sort_keys=True)
            rel_head = f"{rel} (binary file — descriptor only, content not inlined)"
            block = f"### {rel_head}\n\n```json\n{body}\n```"
        else:
            block = f"### {rel}\n\n```\n{body}\n```"
        block_tokens = estimate_tokens(block) if split else 0
        if block_tokens > budget:
            # One file too large to review in a single pass without truncating it.
            raise _SkillFileOverBudget(rel, block_tokens, budget)
        if current and current_tokens + block_tokens > budget:
            packs.append("\n\n".join(current))
            current, current_tokens = [], 0
        current.append(block)
        current_tokens += block_tokens
    if current:
        packs.append("\n\n".join(current))
    if expected_content_hash and release_digest_from_file_digests(
            file_digests, release_declaration(skill_dir), bucket=payload_bucket(skill_dir)) != expected_content_hash:
        raise _SkillFileUnreadable("(payload snapshot)", RuntimeError("skill payload changed after hashing"))
    return packs


def frozen_payload_files(
    view: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Per-file facts of the frozen payload copy a retrieving reviewer reads.

    The same file set and reads as the packs (``_iter_payload_files``,
    ``_read_skill_file``): a text file carries the identity a ``read_file``
    receipt stamps (``source_text_identity``), a binary file stays its
    descriptor — a descriptor never claims its bytes were inspected.
    """
    from ouroboros.skill_loader import _iter_payload_files  # pylint: disable=W0212
    from ouroboros.tools.scope_required_sources import source_text_identity

    view = pathlib.Path(view).resolve()
    entries: List[Dict[str, Any]] = []
    for file_path in _iter_payload_files(view, manifest_entry=manifest_entry, manifest_scripts=manifest_scripts):
        rel = file_path.relative_to(view).as_posix()
        text, digest, descriptor = _read_skill_file(file_path, relpath=rel)
        if descriptor is not None:
            entries.append({"path": rel, "binary": descriptor})
            continue
        raw = text.encode("utf-8")
        entries.append({"path": rel, "size": len(raw), "sha256": digest.hex(), "text": text,
                        **source_text_identity(raw)})
    return entries
