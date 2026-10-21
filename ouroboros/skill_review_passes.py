"""Skill-review pass runner (P5): one multi-model review pass normally, or — when an
over-budget skill is split into packet-sized packs — a chunked pass per pack whose
per-model finding arrays are merged into one, so every byte is reviewed without
silent truncation and the existing quorum/aggregation produces one verdict.

A packet seat receives the host-built pack (chunk by chunk). A retrieving seat — a
native inspection episode or an agent session — reviews the WHOLE frozen payload
once, in the first pass: the retained loader inventory bound to the content hash
(``frozen_payload_source``; a writable seat the whole folder at that hash,
``skill_checking_source``), pasted in full only when the seat's measured first send
stays within its initial inline sizing hint, otherwise an index of that copy's files
plus a required-source manifest naming them and the governance it must read
(``_retrieving_seat``). Later chunk passes carry only packet seats, and a retrieving
seat's answer counts toward each chunk's quorum because it covered every file.

Lives outside ``skill_review`` (module-size discipline). The prompt builder and the
multi-model review callable are INJECTED so this module never imports ``skill_review``
(no circular dependency).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any, Callable, Dict, List, Optional, Tuple

# Payload-file binary policy (X4/В21). Loadable native code is unreviewable by
# LLMs: files whose CONTENT starts with a loader magic number are hard-blocked
# from the skill payload surface — judged by magic bytes, never by filename.
# These prefixes are unambiguous (never legitimate text), so they block
# regardless of UTF-8 validity. WebAssembly (``WASM_MAGIC``) is deliberately NOT
# here: the host never loads it natively — it runs only inside the browser's
# sandboxed widget frame — so a module is admitted as a content-hash-bound
# descriptor the reviewer sees, routed there by its magic even when its bytes
# happen to decode as UTF-8 (the canonical 8-byte module does).
WASM_MAGIC = b"\x00asm"
_EXECUTABLE_MAGICS: Tuple[Tuple[bytes, str], ...] = (
    (b"\x7fELF", "ELF executable / shared object"),
    (b"\xfe\xed\xfa\xce", "Mach-O executable (32-bit)"),
    (b"\xfe\xed\xfa\xcf", "Mach-O executable (64-bit)"),
    (b"\xce\xfa\xed\xfe", "Mach-O executable (32-bit, little-endian)"),
    (b"\xcf\xfa\xed\xfe", "Mach-O executable (64-bit, little-endian)"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal (fat) binary / Java class"),
    (b"\xca\xfe\xba\xbf", "Mach-O universal (fat, 64-bit) binary"),
    (b"\xbe\xba\xfe\xca", "Mach-O universal (fat, byte-swapped) binary"),
    (b"\xbf\xba\xfe\xca", "Mach-O universal (fat, 64-bit byte-swapped) binary"),
)


def executable_magic_kind(data: bytes, *, is_utf8_text: bool) -> str:
    """Return the loadable-executable kind for ``data``'s magic bytes, or ``""``.

    PE (``MZ``, 2 bytes) and other-version .pyc magics are too short /
    version-varying to be unambiguous on text, so they are judged only on
    content that is NOT valid UTF-8 — a genuine PE or .pyc body never is."""
    for magic, kind in _EXECUTABLE_MAGICS:
        if data.startswith(magic):
            return kind
    import importlib.util

    if data.startswith(importlib.util.MAGIC_NUMBER):
        # The current interpreter's .pyc magic (the .pyc the host could actually
        # import) is 4 exact bytes — unambiguous, so judged on any content.
        return "compiled Python bytecode (.pyc)"
    if not is_utf8_text:
        if data[:2] == b"MZ":
            return "PE/DOS executable"
        # Deliberately NO looser .pyc heuristic here: "bytes 2-3 == CRLF" also
        # matches legacy-encoded text files (false hard-block). A .pyc compiled
        # by a DIFFERENT Python version cannot be imported by this host at all,
        # so it falls to the descriptor path — disclosed residual, not a gap.
    return ""


class SkillBinaryPayload(RuntimeError):
    """Raised for loadable-executable skill payloads (judged by content magic
    bytes); other binary payloads — non-UTF-8 files and WebAssembly admitted by
    magic — become ``{path,size,mime_from_name,sha256}`` descriptors."""

    def __init__(self, relpath: str, size_bytes: int, kind: str = "") -> None:
        super().__init__(
            f"Skill file {relpath!r} is a loadable executable ({kind or 'magic bytes'}, "
            f"{size_bytes} bytes); review hard-blocks native code in the skill surface."
        )
        self.relpath = relpath
        self.size_bytes = size_bytes
        self.kind = kind


def binary_file_descriptor(relpath: str, data: bytes, *, filename: str = "") -> Dict[str, Any]:
    """Typed descriptor for a binary (non-UTF-8 or WebAssembly), non-executable payload file: the review
    pack carries ``{path,size,mime_from_name,sha256}`` instead of raw bytes."""
    import mimetypes

    return {
        "path": relpath,
        "size": len(data),
        # Named honestly: guessed from the FILENAME, not sniffed from bytes — a
        # hostile blob named cert.png would present as image/png; the reviewer
        # must not read this as a content attestation (size/sha256 do not lie).
        "mime_from_name": mimetypes.guess_type(filename or relpath)[0] or "application/octet-stream",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


_SINGLE_CONTENT = (
    "Review the skill package whose manifest and payload are included above, using the "
    "Skill Review Checklist. Return ONLY the JSON array described in the output contract."
)

_RETRIEVING_CONTENT = (
    "Review the skill package — its manifest and its frozen payload, pasted above in full "
    "or named above as exact files to read — using the Skill Review Checklist. Return ONLY "
    "the JSON array described in the output contract."
)

_SESSION_RETRIEVAL = (
    "Read the governance and host-contract sources in {repository}. Read "
    "`BIBLE.md` in full. Read the `docs/ARCHITECTURE.md` and `docs/DEVELOPMENT.md` "
    "book entrypoints in full, then every physical chapter in each entrypoint's "
    "`Chapters` membership list in full, in the order you choose. Resolve each chapter link "
    "relative to its entrypoint; a legacy monolith without `Chapters` is already "
    "the complete book. Cite physical file paths and file-local lines, never "
    "line numbers in a composed book. Reading an entrypoint alone does not "
    "cover its chapters; report any missing or unread chapter explicitly. Read "
    "the `Skill Review Checklist` section of `docs/CHECKLISTS.md`; then read "
    "`docs/CREATING_SKILLS.md`, `ouroboros/contracts/plugin_api.py`, and "
    "`ouroboros/extension_ui_validation.py` in full. Treat those source reads as "
    "the governance and host-contract context for this review.\n\n"
)

# A native episode's own workspace IS the frozen payload and the installed repository is
# its ``system_repo`` root; a delegated session keeps the repository as its root and reads
# the frozen copy by absolute path (a writable seat checks its private copy of it).
_REPOSITORY = {
    "native": ('`root="system_repo"` (the installed repository; your own workspace root, '
               '`active_workspace`, is the frozen skill payload)'),
    "session": "your session root, the installed repository",
}
_PAYLOAD_ADDRESS = {
    "native": ('your workspace root (`root="active_workspace"`, the default of read_file, '
               'list_files and search_code)'),
    "session": "the absolute folder {view}",
}
_PAYLOAD_INLINE = (
    "## Skill payload — exact frozen copy\n\n"
    "The skill files below are the payload bound to content hash {content_hash}, pasted in "
    "full: judge them, never the mutable skill_dir path. The same files are readable at "
    "{address}; a binary file is pasted only as its {{path,size,mime_from_name,sha256}} "
    "descriptor (mime guessed from the FILENAME) and may be inspected there — say in the "
    "finding which you did.\n\n"
)
_PAYLOAD_SOURCES = (
    "## Skill payload — exact frozen copy, read it\n\n"
    "The payload bound to content hash {content_hash} is not pasted: {reason}. Read its files "
    "at {address}, never the mutable skill_dir path. The index under `Skill files` below (pasted, or "
    "one exact stored source when even it exceeds the initial inline sizing hint) names every file; "
    "each text file is a required source, and a binary file is only its descriptor unless you "
    "inspect it there — say in the finding which you did.\n\n"
)
# The required-manifest identity of this surface: a change lapses recorded replay.
SKILL_REQUIRED_SOURCES_POLICY = "skill_frozen_payload_v1"
_INDEX_SOURCE_ID = "skill-payload-index"


def _paged_index_pointer(kind: str, source: Dict[str, Any], index: str, content_hash: str) -> str:
    """The `Skill files` section when even the index exceeds the initial inline sizing hint:
    the index is stored as one exact source, read in ranges like a paged commit diff."""
    address = (f'read it in ranges with `read_file(root="artifact_store", path="{source["path"]}", '
               'start_line=A, max_lines=N)`' if kind == "native" else
               f"read the project-relative path `{source['session_relative_path']}` (inside this "
               "repository root, git-ignored) in ranges with your own file tools")
    return (f"Frozen payload index (content hash {content_hash}) — stored, not pasted: with it this first "
            f"send would exceed the initial inline sizing hint too. It is complete — {len(index):,} chars, sha256 "
            f"`{source.get('sha256', 'unavailable')}` — and names every file of the frozen copy with its "
            f"size and digests: {address}, until you have seen all of it.")


def skill_review_session_contract_hash() -> str:
    """Identity of the retrieving Skill Review serialization (native and session).

    Free replay must lapse when the retrieving assignment, its payload delivery
    and required-source manifest, or the agent-session prompt wrapper changes.
    Kept separate from the historical API prompt hash so packet-only panels
    retain their exact identity.
    """
    try:
        import hashlib
        import inspect

        from ouroboros.review_execution import AgentSessionReviewExecutor

        getter = AgentSessionReviewExecutor.session_prompt.fget
        if getter is None:
            return ""
        parts = (
            _SINGLE_CONTENT, _RETRIEVING_CONTENT, _SESSION_RETRIEVAL, _PAYLOAD_INLINE, _PAYLOAD_SOURCES,
            json.dumps([_REPOSITORY, _PAYLOAD_ADDRESS, SKILL_REQUIRED_SOURCES_POLICY, _INDEX_SOURCE_ID],
                       sort_keys=True),
            *(inspect.getsource(fn) for fn in (
                run_skill_review_passes, _retrieving_seat, _retrieving_rows, _governance_rows,
                _payload_index, _paged_index_pointer, _seat_first_send)),
            inspect.getsource(AgentSessionReviewExecutor._output_contract),
            inspect.getsource(getter),
        )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    except Exception:
        return ""  # unknown contract never matches (fail-open toward paying)


def _skill_review_retry_key(
    *,
    skill_name: str,
    wave_id: str,
    content_hash: str,
    contract_fingerprint: str,
    rebuttal_sha256: str,
    pack: str,
    chunk_index: int,
    chunk_count: int,
) -> str:
    """Identity of one physical Skill Review wave/chunk.

    The lifecycle owner may explicitly resume this logical wave from its
    recorded roster. Distinct logical waves/chunks never borrow actors.
    """
    skill = str(skill_name or "")
    wave = str(wave_id or "")
    if not skill or not wave:
        return ""  # legacy callers retain full content-addressed identity
    pack_digest = hashlib.sha256(
        str(pack).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    payload = {
        "skill": skill,
        "wave": wave,
        "content_hash": str(content_hash or ""),
        "contract_fingerprint": str(contract_fingerprint or ""),
        "rebuttal_sha256": str(rebuttal_sha256 or ""),
        "chunk_index": int(chunk_index),
        "chunk_count": int(chunk_count),
        "pack_sha256": pack_digest,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return f"skill_review:{digest}"


def _reserve_skill_wave(ctx, drive_root, skill, file_packs, models, row_plan, attribution,
                        content_hash, contract_fp, rebuttal_sha, packet_slots=None):
    """Write the complete chunk/operation roster before the first paid dispatch.

    The first chunk reserves every seat; a later chunk only the packet seats that
    receive it (``packet_slots``; ``None`` = every seat), because a retrieving seat
    reviews the whole payload once, in the first pass."""
    from ouroboros.observability import new_call_id
    from ouroboros.review_dispatch import slot_id_for_row
    from ouroboros.skill_review_runner import review_job_state_path
    from ouroboros.utils import update_json_locked

    wave_id = str(attribution.get("review_wave_id") or "")
    if not wave_id or ctx is None:
        return None  # Legacy pass-only callers have no lifecycle/paid wave.
    ids = list((row_plan or {}).get("slot_ids") or [])
    ids += [slot_id_for_row(i + 1) for i in range(len(ids), len(models))]
    resume = getattr(ctx, "_skill_review_resume", None)
    if resume:
        return resume
    wave = {"wave_id": wave_id, "binding": getattr(ctx, "_skill_review_wave_binding", {}),
            "panel": dict(getattr(ctx, "_skill_review_panel", {}) or {}),
            "additional": [sid for sid, extra in zip(ids, (row_plan or {}).get("additional") or []) if extra],
            "chunks": [{"retry_key": _skill_review_retry_key(
                skill_name=skill.name, wave_id=wave_id, content_hash=content_hash,
                contract_fingerprint=contract_fp, rebuttal_sha256=rebuttal_sha,
                pack=pack, chunk_index=i, chunk_count=len(file_packs)),
                "operations": {slot: new_call_id(f"skill_review_{slot}") for slot in ids
                               if i == 0 or packet_slots is None or slot in packet_slots}}
                for i, pack in enumerate(file_packs)]}
    job_id = str(getattr(ctx, "_skill_review_lifecycle_job_id", "") or "")
    if job_id:
        def reserve(current):
            if current.get("job_id") != job_id or current.get("status") != "running":
                raise RuntimeError("review was not persisted because its lifecycle no longer owns this wave")
            return {**current, "review_wave": wave}
        update_json_locked(review_job_state_path(drive_root, skill.name), reserve,
                           strict_existing_dict=True)
    return wave


def _chunk_checkpoint(drive_root, skill_name: str, job_id: str, wave_id: str, index: int, retry_key: str):
    """Bind each seat's invocation token into its reserved wave chunk BEFORE the POST.

    The token lands under the lifecycle job's lock, only while that job still runs
    this wave and the seat's reserved operation is the one being sent; the same
    token again is idempotent, a different one is refused. A refusal stops the
    POST (``checkpoint_pending_invocation``), so a resume finds every token of
    every seat that could have been sent (#1582).
    """
    from ouroboros.skill_review_runner import review_job_state_path
    from ouroboros.utils import update_json_locked

    def record(*, surface: str, slot_id: str, operation_id: str, invocation_id: str) -> None:
        def bind(current):
            wave = current.get("review_wave") if isinstance(current.get("review_wave"), dict) else {}
            chunks = list(wave.get("chunks") or [])
            chunk = dict(chunks[index]) if index < len(chunks) and isinstance(chunks[index], dict) else {}
            bound = dict(chunk.get("invocations") or {})
            if (current.get("job_id") != job_id or current.get("status") != "running"
                    or wave.get("wave_id") != wave_id or surface != "skill_review"
                    or chunk.get("retry_key") != retry_key
                    or (chunk.get("operations") or {}).get(slot_id) != operation_id
                    or bound.get(slot_id) not in (None, "", invocation_id)):
                raise RuntimeError("the running Skill Review wave no longer owns this seat's invocation")
            chunks[index] = {**chunk, "invocations": {**bound, slot_id: invocation_id}}
            return {**current, "review_wave": {**wave, "chunks": chunks}}
        update_json_locked(review_job_state_path(drive_root, skill_name), bind, strict_existing_dict=True)
    return record


def panel_delivery_classes(row_plan: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    """``(packet seat ids, packet api models, native api models)`` of a panel. Only a packet
    seat reads host-built packs, so only it makes the payload split into packet chunks; a
    native api seat reads the whole frozen payload once."""
    from ouroboros.review_dispatch import slot_id_for_row
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.reviewer_slot_config import row_plan_retrieves

    models, routes = list(row_plan.get("models") or []), list(row_plan.get("routes") or [])
    slot_ids = list(row_plan.get("slot_ids") or [])
    seats, packet_api, native_api = [], [], []
    for i, model in enumerate(models):
        retrieves = row_plan_retrieves(row_plan, i)
        if not retrieves:
            seats.append(str(slot_ids[i] if i < len(slot_ids) and slot_ids[i] else slot_id_for_row(i + 1)))
        if i < len(routes) and routes[i] is ReviewRouteKind.API_CHAT:
            (native_api if retrieves else packet_api).append(model)
    return seats, packet_api, native_api


def _loader_inventory(skill: Any) -> Dict[str, Any]:
    return {"manifest_entry": skill.manifest.entry, "manifest_scripts": skill.manifest.scripts}


def frozen_payload_source(drive_root: Any, skill: Any, content_hash: str) -> Dict[str, Any]:
    """The wave's read-only frozen payload: the loader inventory kept at loader hash
    ``content_hash`` — the files that hash and the packs cover, without the dependency
    env and caches both leave out — named by the skill folder and that hash.

    A native episode reads it in place and a read-only session by absolute path, so
    seats sent on either side of an edit of the live skill_dir review the same bytes;
    a writable seat checks its own copy of the whole folder (``skill_checking_source``).
    A payload that moved past the reviewed hash before its view was kept yields
    ``{"unavailable": ...}``.
    """
    from functools import partial

    from ouroboros.review_checking import bound_directory_source
    from ouroboros.skill_loader import compute_content_hash
    from ouroboros.subagent_worktrees import _copy_payload_inventory

    inventory = _loader_inventory(skill)
    return bound_directory_source(skill.skill_dir, data_root=drive_root, expected_hash=content_hash,
                                  content_hash_of=lambda root: compute_content_hash(root, **inventory),
                                  copy=partial(_copy_payload_inventory, preserve_links=False, **inventory))


def skill_checking_source(drive_root: Any, skill: Any, row_plan: Dict[str, Any],
                          content_hash: str) -> Dict[str, Any] | None:
    """A writable seat checks a copy of the whole skill folder — its dependencies, links,
    modes and empty folders — whose loader hash is ``content_hash``; None when every seat
    keeps the read-only inspection boundary."""
    from types import SimpleNamespace

    from ouroboros.review_checking import bound_directory_source, writable_seat
    from ouroboros.skill_loader import compute_content_hash

    routes = list(row_plan.get("routes") or [])
    accesses = row_plan.get("session_accesses") or [""] * len(routes)
    if not any(writable_seat(SimpleNamespace(route=route, session_access=access))
               for route, access in zip(routes, accesses)):
        return None
    return bound_directory_source(skill.skill_dir, data_root=drive_root, expected_hash=content_hash,
                                  content_hash_of=lambda root: compute_content_hash(root, **_loader_inventory(skill)))


def _governance_rows(repo_root: str) -> List[Dict[str, Any]]:
    """The governance and host-contract sources a retrieving seat must read in full
    (``_SESSION_RETRIEVAL``), as required rows of the installed repository, so the
    reading is observable; coverage stays diagnostic (BIBLE P3)."""
    from ouroboros.reference_books import BOOK_ENTRYPOINTS, book_source_sizes
    from ouroboros.tools.scope_required_sources import source_text_identity

    root, paths = pathlib.Path(repo_root), ["BIBLE.md"]
    for book_id, entrypoint in BOOK_ENTRYPOINTS.items():
        # The entrypoint and its declared chapters in order; only the entrypoint is
        # parsed. An unreadable book leaves its entrypoint row, which shows the gap.
        paths.extend(book_source_sizes(root, book_id) or [entrypoint])
    paths += ["docs/CREATING_SKILLS.md", "ouroboros/contracts/plugin_api.py", "ouroboros/extension_ui_validation.py"]
    rows = []
    for path in paths:
        row = {"root": "system_repo", "path": path, "disposition": "governance"}
        try:
            rows.append({**row, **source_text_identity((root / path).read_bytes())})
        except (OSError, UnicodeDecodeError):
            rows.append({**row, "coverage_basis": "source_unavailable",
                         "reason": "the installed repository carries no readable text at this path"})
    return rows


def _payload_index(entries: List[Dict[str, Any]], content_hash: str) -> str:
    """The `Skill files` section when the payload is read, not pasted: every file of
    the frozen copy with its size and digests, a binary file as its descriptor."""
    lines = [f"Frozen payload index (content hash {content_hash}) — path: bytes, sha256, chars"]
    for entry in entries:
        binary = entry.get("binary")
        lines.append(
            f"- {entry['path']}: binary descriptor — {binary['size']:,} bytes, sha256 "
            f"{binary['sha256']}, mime_from_name {binary['mime_from_name']}" if binary else
            f"- {entry['path']}: {entry['size']:,} bytes, sha256 {entry['sha256']}, "
            f"{entry['complete_chars']:,} chars")
    return "\n".join(lines)


def _retrieving_rows(entries: List[Dict[str, Any]], governance: List[Dict[str, Any]], *,
                     kind: str, view: str, data_root: Any, inline: bool) -> List[Dict[str, Any]]:
    """One retrieving seat's required-source manifest: each text file of the frozen copy
    (credited ``delivered_inline`` when pasted whole) and the governance rows. A native
    row addresses the episode's workspace; a session row names the absolute file its
    harness reads, under the runtime data root that holds the copy."""
    rows = []
    for entry in entries:
        if entry.get("binary"):
            continue  # a descriptor is not text a reader delivers
        row = {"root": "active_workspace", "path": entry["path"], "disposition": "skill_payload",
               **{key: entry[key] for key in ("source_revision", "complete_sha256", "complete_chars", "range_basis")},
               "coverage_basis": "delivered_inline" if inline else "candidate_blob"}
        if kind == "session":
            file = pathlib.Path(view) / entry["path"]
            try:
                logical = file.relative_to(pathlib.Path(data_root)).as_posix()
            except ValueError:
                logical = file.as_posix()
            row.update(root="runtime_data", path=logical, file=str(file))
        rows.append(row)
    return rows + governance


def _seat_first_send(kind: str, *, task: str, view: str, slot: Dict[str, Any], contract: str,
                     ref: Dict[str, Any]) -> Tuple[int, int]:
    """``(measured, ceiling)`` of one retrieving seat's first send, as it is dispatched: a
    native episode by its own opening (work order, manifest reference and tool schemas)
    against the initial inline share of its layout hint, never a send limit; a session by
    the host-submitted prompt against the conservative session inline ceiling — never the
    vendor's own context."""
    from types import SimpleNamespace

    from ouroboros.tools.review_brief_coupling import SESSION_INLINE_DIFF_CEILING_CHARS
    from ouroboros.tools.review_multi_model import TRIAD_ROLE_HINT, _review_output_budget

    if kind == "session":
        from ouroboros.review_session_preparation import render_review_session_prompt

        prompt = render_review_session_prompt(
            SimpleNamespace(surface="skill_review", policy={"output_contract": contract}),
            SimpleNamespace(role_hint=TRIAD_ROLE_HINT, slot_id=slot["slot_id"], session_access=slot["access"]), task)
        return len(prompt), SESSION_INLINE_DIFF_CEILING_CHARS
    from ouroboros.review_native_episode import (
        native_episode_transcript_bound,
        native_first_send_chars,
        native_landing_at,
    )

    budget = _review_output_budget()
    bound = native_episode_transcript_bound(SimpleNamespace(max_tokens=budget, policy={}), SimpleNamespace(
        model=slot["model"], max_tokens=budget, use_local=slot["use_local"], slot_id=slot["slot_id"],
        session_profile=slot["profile"]))
    measured = native_first_send_chars(view, surface="skill_review", role_hint=TRIAD_ROLE_HINT,
                                       slot_id=slot["slot_id"], session_task=task, output_contract=contract,
                                       task_id="skill_review", required_sources_ref=ref)
    return measured, native_landing_at(bound)


def _retrieving_seat(kind: str, slot: Dict[str, Any], *, source: Dict[str, Any], data_root: Any,
                     content_hash: str, entries: List[Dict[str, Any]], governance: List[Dict[str, Any]],
                     payload_pack: str, tail_for: Callable[[str], str], page: Callable[[str, bytes], Dict[str, Any]],
                     contract: str) -> Tuple[str, Dict[str, Any], str]:
    """``(task, policy, session_root)`` of one retrieving seat.

    The whole payload is pasted only when the seat's measured first send — the
    instructions, the pasted payload, its manifest reference and the schemas — stays
    under its inline share; otherwise the seat gets the index of the frozen copy and reads
    the files itself, however large the payload or one of its files. An index over that
    share too is stored as one exact source (``page``, the commit diff's paging)
    and read in ranges like the files. Either way the manifest names every file and the
    governance it is owed (``native_required_sources``).
    """
    from ouroboros.tools.scope_required_sources import required_sources_ref

    view = str(source["view"])
    address = _PAYLOAD_ADDRESS[kind].format(view=view)
    index = _payload_index(entries, content_hash)

    def assemble(inline: bool, reason: str = "", stored: Optional[Dict[str, Any]] = None
                 ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        payload = (_PAYLOAD_INLINE.format(content_hash=content_hash, address=address) if inline else
                   _PAYLOAD_SOURCES.format(content_hash=content_hash, address=address, reason=reason))
        pack = payload_pack if inline else _paged_index_pointer(kind, stored, index, content_hash) if stored else index
        task = (_SESSION_RETRIEVAL.format(repository=_REPOSITORY[kind]) + payload
                + tail_for(pack) + f"\n\n## Review assignment\n\n{_RETRIEVING_CONTENT}")
        rows = _retrieving_rows(entries, governance, kind=kind, view=view, data_root=data_root, inline=inline)
        rows += [{**stored["required_row"], "disposition": "skill_payload_index"}] if stored else []
        return task, rows, {**required_sources_ref(rows), "policy": SKILL_REQUIRED_SOURCES_POLICY,
                            "content_hash": content_hash}

    def measure(task: str, ref: Dict[str, Any]) -> Tuple[int, int]:
        measured, ceiling = _seat_first_send(kind, task=task, view=view, slot=slot, contract=contract, ref=ref)
        delivery.update(first_send_chars=measured, first_send_ceiling=ceiling)
        return measured, ceiling

    task, rows, ref = assemble(True)
    delivery: Dict[str, Any] = {"form": "inline", "frozen_view": view, "content_hash": content_hash}
    stored: Dict[str, Any] = {}
    try:
        measured, ceiling = measure(task, ref)
        delivery["inline_first_send_chars"] = measured
        reason = "" if measured < ceiling else (
            f"with it this first send would carry {measured:,} chars, over this seat's "
            f"{ceiling:,}-char initial inline sizing hint")
        if reason:
            task, rows, ref = assemble(False, reason)
            delivery["form"] = "sources"
            delivery["index_first_send_chars"] = measure(task, ref)[0]
            if delivery["index_first_send_chars"] >= ceiling:
                stored = page(kind, index.encode("utf-8"))
                if stored.get("path"):
                    task, rows, ref = assemble(False, reason, stored)
                    delivery.update(index="paged", index_source={k: stored[k] for k in ("root", "path", "size", "sha256")})
                    measure(task, ref)
                else:  # an unreachable store is disclosed; the complete index stays pasted
                    delivery.update(index_paging="unavailable", index_paging_reason=str(stored.get("error") or "no_source_root"))
                    stored = {}
    except Exception as exc:  # an unmeasured seat is sent the index; size never refuses the seat
        reason = f"this seat's first send could not be measured ({type(exc).__name__})"
        delivery["measurement_error"] = f"{type(exc).__name__}: {exc}"
        if delivery["form"] == "inline":
            task, rows, ref = assemble(False, reason)
            delivery["form"] = "sources"
    policy = {"output_contract": contract, "native_required_sources": rows,
              "native_required_sources_ref": ref, "skill_payload_delivery": delivery}
    if stored and kind == "native":
        policy["native_data_root"] = stored["data_root"]  # where the stored index resolves, as for a paged diff
    return task, policy, view if kind == "native" else ""


def retained_beside_preflight(outcome: Any, preflight_outcome: Any) -> None:
    """A wave collected while the current preflight fails keeps its paid findings beside
    the preflight failure and stays pending: collection never grants execution (#1582)."""
    if preflight_outcome is None:
        return
    from ouroboros.skill_review_status import STATUS_PENDING

    outcome.status = STATUS_PENDING
    outcome.error = f"{preflight_outcome.error}; the resumed wave's paid findings were collected and retained"
    outcome.findings = [*preflight_outcome.findings, *outcome.findings]


def _seat_plan(row_plan: Dict[str, Any], indexes: List[int], count: int) -> Dict[str, Any]:
    """The row plan of the seats one pass dispatches: every aligned per-row vector
    narrowed to ``indexes`` in order; a value that is not per-row stays as it is."""
    return {key: ([value[i] for i in indexes] if isinstance(value, (list, tuple)) and len(value) == count
                  else value) for key, value in row_plan.items()}


def _responded_slots(parsed: Any) -> set:
    return {record.slot_id for record in parsed.actor_records if record.status == "responded" and record.slot_id and not record.additional}


def run_skill_review_passes(
    ctx: Any,
    drive_root: Any,
    skill: Any,
    *,
    evidence: Dict[str, Any],
    file_packs: List[str],
    models: List[str],
    row_plan: Dict[str, Any] | None = None,
    session_root: str = "",
    usage_attribution: Dict[str, str] | None = None,
    review_contract_fingerprint: str = "",
    rebuttal_sha256: str = "",
    build_prompt: Callable[..., Tuple[str, int, Dict[str, Any]]],
    run_review: Callable[..., str],
    checking_source: Dict[str, Any] | None = None,
) -> Tuple[str, Dict[str, Any], str, str]:
    """Return ``(prompt, advisory_evidence, result_json_text, infra_error)``. A non-empty
    ``infra_error`` means a pass failed and the caller should fail closed (pending). ``evidence``
    carries the prompt-building inputs: ``manifest_dump``, ``content_hash``, ``history``,
    ``review_rebuttal``, ``required_items``."""
    from ouroboros.config import adaptive_quorum
    from ouroboros.review_dispatch import slot_id_for_row
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.reviewer_slot_config import row_plan_retrieves
    from ouroboros.triad_review import parse_model_review_results

    manifest_dump = evidence["manifest_dump"]
    content_hash = evidence["content_hash"]
    history = evidence["history"]
    review_rebuttal = evidence["review_rebuttal"]
    required_items = evidence["required_items"]
    matrix_contract = (
        "Return ONLY a JSON array with at least one PASS or FAIL object for EVERY "
        "expected item. Empty arrays and NO_FINDINGS are invalid. Expected items: "
        f"{json.dumps(list(required_items))}. Each object needs item, verdict, severity, "
        "and a concrete reason; emit no prose outside the array."
    )

    attribution = dict(usage_attribution or {})
    resuming = bool(getattr(ctx, "_skill_review_resume", None))
    if resuming:
        from ouroboros.skill_review_collect import collect_skill_review_wave

        result = collect_skill_review_wave(ctx, drive_root, file_packs, required_items=required_items)
        return "", {"status": "not_rerun", "reason": "collection of the recorded wave"}, json.dumps(result), ""

    def prompt_for(pack: str) -> Tuple[str, int, Dict[str, Any]]:
        return build_prompt(
            ctx, drive_root, skill,
            manifest_dump=manifest_dump, content_hash=content_hash,
            file_pack=pack, history=history, review_rebuttal=review_rebuttal,
        )

    plan, count = dict(row_plan or {}), len(models)
    routes = list(plan.get("routes") or [])
    routes += [ReviewRouteKind.API_CHAT] * max(0, count - len(routes))
    retrieving = [i for i in range(count) if row_plan and row_plan_retrieves({**plan, "routes": routes}, i)]
    slot_ids = list(plan.get("slot_ids") or [])
    slot_ids += [slot_id_for_row(i + 1) for i in range(len(slot_ids), count)]
    if retrieving:
        from types import SimpleNamespace

        from ouroboros.provider_models import review_model_uses_local
        from ouroboros.review_checking import writable_seat
        from ouroboros.skill_review_packs import frozen_payload_files

        accesses = list(plan.get("session_accesses") or []) + [""] * count
        writable = {i for i in retrieving
                    if writable_seat(SimpleNamespace(route=routes[i], session_access=accesses[i]))}
        source = frozen_payload_source(drive_root, skill, content_hash)
        if writable and checking_source is None:
            checking_source = skill_checking_source(drive_root, skill, {**plan, "routes": routes}, content_hash)
        if not (source or {}).get("view"):
            return "", {}, "", ("Skill Review frozen payload unavailable: "
                                f"{(source or {}).get('unavailable') or 'no retained copy'}")
        # A writer whose whole-folder copy was not kept is refused alone at its own dispatch
        # (``review_checking.session_checking``); its unsent assignment names the read-only copy.
        writable = writable if (checking_source or {}).get("view") else set()
        try:
            entries = frozen_payload_files(pathlib.Path(source["view"]), **_loader_inventory(skill))
        except (OSError, RuntimeError) as exc:
            return "", {}, "", f"Skill Review frozen payload unreadable: {exc}"
        from ouroboros.tools.review_admission import source_root_for
        from ouroboros.tools.review_brief_coupling import BriefInputs, _store_review_source
        from ouroboros.tools.review_multi_model import review_request_task_id

        governance = _governance_rows(session_root)
        tails: Dict[str, str] = {}

        def tail_for(pack: str) -> str:
            """The variable tail of the built prompt around one `Skill files` section."""
            if pack not in tails:
                text, stable, _ = prompt_for(pack)
                tails[pack] = text[max(0, min(stable, len(text))):]
            return tails[pack]

        def page(kind: str, data: bytes) -> Dict[str, Any]:
            """Store exact bytes where this seat's own reader resolves them (a paged diff's store)."""
            return _store_review_source(pathlib.Path(session_root), BriefInputs(
                delegated=kind == "session", task_id=review_request_task_id(ctx),
                source_root=source_root_for(ctx, {})), data, _INDEX_SOURCE_ID)

        tasks, policies, roots = [""] * count, [None] * count, [""] * count
        profiles = list(plan.get("session_profiles") or []) + [""] * count
        local = list(plan.get("use_local") or []) + [None] * count
        for i in retrieving:
            kind = "session" if routes[i] is ReviewRouteKind.AGENT_SESSION else "native"
            slot = {"slot_id": slot_ids[i], "model": models[i], "access": accesses[i] or "readonly",
                    "profile": profiles[i] or "",
                    "use_local": review_model_uses_local(models[i]) if local[i] is None else bool(local[i])}
            # Both views hold the files ``entries`` names at the same loader hash; a writable
            # seat's rows and instructions name the whole-folder view its checking copy is made of.
            tasks[i], policies[i], roots[i] = _retrieving_seat(
                kind, slot, source=checking_source if i in writable else source, data_root=drive_root,
                content_hash=content_hash, entries=entries, governance=governance,
                payload_pack="\n\n".join(file_packs), tail_for=tail_for, page=page, contract=matrix_contract)
        plan.update(routes=routes, session_tasks=tasks, session_policies=policies, session_roots=roots)
    packet_slots = {slot_ids[i] for i in range(count) if i not in retrieving}

    try:
        wave = _reserve_skill_wave(ctx, drive_root, skill, file_packs, models, row_plan,
                                   attribution, content_hash, review_contract_fingerprint, rebuttal_sha256,
                                   packet_slots=packet_slots if retrieving else None)
    except (OSError, ValueError, RuntimeError) as exc:
        return "", {}, "", f"Skill Review roster unavailable: {exc}"

    def _run(
        content: str,
        prompt: str,
        stable_prefix_len: int,
        *,
        pack: str,
        chunk_index: int,
        chunk_count: int,
    ) -> str:
        retry_key = _skill_review_retry_key(
            skill_name=str(attribution.get("review_skill") or getattr(skill, "name", "") or ""),
            wave_id=str(attribution.get("review_wave_id") or ""),
            content_hash=str(content_hash or ""),
            contract_fingerprint=review_contract_fingerprint,
            rebuttal_sha256=rebuttal_sha256,
            pack=pack,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
        )
        # A retrieving seat answers once, for the whole payload, in the first pass.
        seats = list(range(count)) if chunk_index == 0 else [i for i in range(count) if i not in retrieving]
        delivery = {"retry_key": retry_key} if retry_key else {}
        if row_plan:
            seat_plan = plan if len(seats) == count else _seat_plan(plan, seats, count)
            delivery.update({
                "routes": seat_plan.get("routes") or [],
                "row_plan": seat_plan,
                "session_root": session_root,
                "session_policy": {"output_contract": matrix_contract},
                "surface": "skill_review",
                "usage_attribution": attribution,
                **({"checking_source": checking_source} if checking_source is not None else {}),
            })
        seat_models = [models[i] for i in seats]
        def deliver():
            from ouroboros.skill_review_output import skill_result_panel
            text = run_review(ctx, content=content, prompt=prompt, models=seat_models,
                              stable_prefix_len=stable_prefix_len, **delivery)
            try:
                result = json.loads(text)
            except (TypeError, ValueError):
                return text  # the caller reports a non-JSON answer as such
            return json.dumps(skill_result_panel(result, delivery.get("row_plan") or {})) if isinstance(result, dict) else text
        if wave is None:
            return deliver()
        chunk = wave["chunks"][chunk_index]
        if chunk["retry_key"] != retry_key:
            raise RuntimeError("Skill Review chunk identity changed before reconciliation")
        names = ("_review_reserved_operations", "_review_frozen_rows", "_review_reconcile_only",
                 "_review_pending_invocation_checkpoint")
        prior = {name: getattr(ctx, name, None) for name in names}
        ctx._review_reserved_operations = {"skill_review": chunk["operations"]}
        ctx._review_reconcile_only, ctx._review_pending_invocation_checkpoint = False, None
        if str(getattr(ctx, "_skill_review_lifecycle_job_id", "") or ""):
            ctx._review_pending_invocation_checkpoint = _chunk_checkpoint(
                drive_root, skill.name, str(ctx._skill_review_lifecycle_job_id), str(wave.get("wave_id") or ""),
                chunk_index, retry_key)
        try:
            return deliver()
        finally:
            for name, value in prior.items():
                setattr(ctx, name, value)

    if len(file_packs) == 1:
        prompt, stable_prefix_len, advisory_evidence = prompt_for(file_packs[0])
        try:
            result_json_text = _run(
                _SINGLE_CONTENT,
                prompt,
                stable_prefix_len,
                pack=file_packs[0],
                chunk_index=0,
                chunk_count=1,
            )
        except Exception as exc:  # pragma: no cover — transport failure path
            return prompt, advisory_evidence, "", f"{type(exc).__name__}: {exc}"
        return prompt, advisory_evidence, result_json_text, ""

    # Over-budget skill: review each chunk in a separate pass and merge the per-model
    # records. ``run_review`` returns a JSON OBJECT {"model_count", "results":[...]} (not a
    # bare array), so we union the chunks' ``results`` into ONE such object — the shape the
    # downstream ``parse_model_review_results`` expects (a bare list would crash it).
    prompt = ""
    advisory_evidence: Dict[str, Any] = {}
    merged_results: List[Any] = []
    retrieved: set = set()  # retrieving seats that answered for the whole payload
    total = len(file_packs)
    for idx, pack in enumerate(file_packs):
        chunk_prompt, chunk_stable_len, adv = prompt_for(pack)
        prompt = chunk_prompt
        if idx == 0:
            advisory_evidence = adv
        content = (
            f"This skill is oversized, so its payload is split into {total} parts for "
            f"review; this is PART {idx + 1} of {total}. Review ONLY the files shown in "
            "this part against the Skill Review Checklist — other parts are reviewed "
            "separately, so do NOT flag files absent from this part as missing. Return "
            "ONLY the JSON array described in the output contract."
        )
        try:
            chunk_text = _run(
                content,
                chunk_prompt,
                chunk_stable_len,
                pack=pack,
                chunk_index=idx,
                chunk_count=total,
            )
            chunk_json = json.loads(chunk_text)
        except Exception as exc:  # pragma: no cover — transport failure path
            return prompt, advisory_evidence, "", f"chunk {idx + 1}/{total}: {type(exc).__name__}: {exc}"
        if isinstance(chunk_json, dict) and "error" in chunk_json:
            return prompt, advisory_evidence, "", f"chunk {idx + 1}/{total} service error: {chunk_json['error']}"
        if not isinstance(chunk_json, dict):
            return prompt, advisory_evidence, "", f"chunk {idx + 1}/{total}: non-object review response"
        # Fail CLOSED unless THIS chunk reached quorum of PARSEABLE reviewers — validated
        # with the SAME parser/required-item contract the single-pass gate uses, so a chunk
        # of malformed/non-JSON actor text cannot pass as "responsive" while the global
        # quorum is satisfied by other chunks (which would leave a portion of the oversized
        # skill under-reviewed — a trust-gate hole). A retrieving seat's first-pass answer
        # covered every chunk's files, so it counts in each. adaptive_quorum matches the
        # single-pass gate (1 reviewer => degraded-but-allowed).
        advisers = [sid for sid, extra in zip(slot_ids, plan.get("additional") or []) if extra]
        parsed = parse_model_review_results(chunk_json, required_items=required_items, additional=advisers)
        if idx == 0:
            retrieved = _responded_slots(parsed) & {slot_ids[i] for i in retrieving}
        responsive = len(parsed.responsive_models) + (len(retrieved) if idx else 0)
        required = adaptive_quorum(len(models) - len(advisers))
        if responsive < required:
            return (
                prompt, advisory_evidence, "",
                f"chunk {idx + 1}/{total}: only {responsive}/{required} reviewers parsed",
            )
        merged_results.extend(chunk_json.get("results") or [])
    return (
        prompt,
        advisory_evidence,
        json.dumps({"results": merged_results}, ensure_ascii=False),
        "",
    )
