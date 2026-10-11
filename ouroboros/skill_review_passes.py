"""Skill-review pass runner (P5): one multi-model review pass normally, or — when an
over-budget skill is split into multiple budget-sized packs — a chunked pass per pack
whose per-model finding arrays are merged into one, so every byte is reviewed without
silent truncation and the existing quorum/aggregation produces one verdict.

Lives outside ``skill_review`` (module-size discipline). The prompt builder and the
multi-model review callable are INJECTED so this module never imports ``skill_review``
(no circular dependency). Every configured delivery row receives the SAME hardened,
host-built frozen chunk and output contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Tuple

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

_SESSION_RETRIEVAL = (
    "Use native read/search tools inside the source-repository session root. Read "
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
    "Some skill payload files may be binary / non-UTF-8 and appear in the pack only "
    "as {path,size,mime_from_name,sha256} descriptors instead of inlined content "
    "(mime_from_name is guessed from the FILENAME, not the bytes). You may "
    "inspect such suspicious or binary files with your own read/search tools when "
    "their path is reachable inside your session root; on the default install "
    "layout the skill payload lives OUTSIDE your session root, so expect to "
    "judge by the descriptor (size/mime_from_name/sha256) — and say which you did in the "
    "finding.\n\n"
)


def skill_review_session_contract_hash() -> str:
    """Identity of the route-specific Skill Review session serialization.

    Free replay must lapse when either the retrieval-sized assignment or the
    generic agent-session prompt wrapper changes.  Keep this separate from the
    historical API prompt hash so API-only panels retain their exact identity.
    """
    try:
        import hashlib
        import inspect

        from ouroboros.review_execution import AgentSessionReviewExecutor

        getter = AgentSessionReviewExecutor.session_prompt.fget
        if getter is None:
            return ""
        parts = (
            _SINGLE_CONTENT,
            _SESSION_RETRIEVAL,
            inspect.getsource(run_skill_review_passes),
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
                        content_hash, contract_fp, rebuttal_sha):
    """Write the complete chunk/operation roster before the first paid dispatch."""
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
            "chunks": [{"retry_key": _skill_review_retry_key(
                skill_name=skill.name, wave_id=wave_id, content_hash=content_hash,
                contract_fingerprint=contract_fp, rebuttal_sha256=rebuttal_sha,
                pack=pack, chunk_index=i, chunk_count=len(file_packs)),
                "operations": {slot: new_call_id(f"skill_review_{slot}") for slot in ids}}
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
) -> Tuple[str, Dict[str, Any], str, str]:
    """Return ``(prompt, advisory_evidence, result_json_text, infra_error)``. A non-empty
    ``infra_error`` means a pass failed and the caller should fail closed (pending). ``evidence``
    carries the prompt-building inputs: ``manifest_dump``, ``content_hash``, ``history``,
    ``review_rebuttal``, ``required_items``."""
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
    try:
        wave = _reserve_skill_wave(ctx, drive_root, skill, file_packs, models, row_plan,
                                   attribution, content_hash, review_contract_fingerprint, rebuttal_sha256)
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
        delivery = {"retry_key": retry_key} if retry_key else {}
        if row_plan:
            boundary = max(0, min(int(stable_prefix_len or 0), len(prompt)))
            delivery.update({
                "routes": row_plan.get("routes") or [],
                "row_plan": row_plan,
                "session_task": (
                    _SESSION_RETRIEVAL
                    + "Use the exact frozen skill evidence in this assignment as the payload "
                    "authority; do not replace it by rereading the mutable skill_dir path.\n\n"
                    f"{prompt[boundary:]}\n\n## Review assignment\n\n{content}"
                ),
                "session_root": session_root,
                "session_policy": {"output_contract": matrix_contract},
                "surface": "skill_review",
                "usage_attribution": attribution,
            })
        if wave is None:
            return run_review(ctx, content=content, prompt=prompt, models=models,
                              stable_prefix_len=stable_prefix_len, **delivery)
        chunk = wave["chunks"][chunk_index]
        if chunk["retry_key"] != retry_key:
            raise RuntimeError("Skill Review chunk identity changed before reconciliation")
        names = ("_review_reserved_operations", "_review_frozen_rows", "_review_reconcile_only")
        prior = {name: getattr(ctx, name, None) for name in names}
        ctx._review_reserved_operations = {"skill_review": chunk["operations"]}
        ctx._review_reconcile_only = bool(getattr(ctx, "_skill_review_resume", None))
        if ctx._review_reconcile_only:
            ctx._review_frozen_rows = {"skill_review": {
                slot: {"slot_id": slot, "operation_id": operation,
                       "operation_state": "in_flight", "late_result_pending": True}
                for slot, operation in chunk["operations"].items()}}
        try:
            return run_review(ctx, content=content, prompt=prompt, models=models,
                              stable_prefix_len=stable_prefix_len, **delivery)
        finally:
            for name, value in prior.items():
                setattr(ctx, name, value)

    if len(file_packs) == 1:
        prompt, stable_prefix_len, advisory_evidence = build_prompt(
            ctx, drive_root, skill,
            manifest_dump=manifest_dump, content_hash=content_hash,
            file_pack=file_packs[0], history=history, review_rebuttal=review_rebuttal,
        )
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
    total = len(file_packs)
    for idx, pack in enumerate(file_packs):
        chunk_prompt, chunk_stable_len, adv = build_prompt(
            ctx, drive_root, skill,
            manifest_dump=manifest_dump, content_hash=content_hash,
            file_pack=pack, history=history, review_rebuttal=review_rebuttal,
        )
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
        # skill under-reviewed — a trust-gate hole). adaptive_quorum matches the single-pass
        # gate (1 reviewer => degraded-but-allowed).
        from ouroboros.config import adaptive_quorum
        from ouroboros.triad_review import parse_model_review_results

        parsed = parse_model_review_results(chunk_json, required_items=required_items)
        required = adaptive_quorum(len(models))
        if len(parsed.responsive_models) < required:
            return (
                prompt, advisory_evidence, "",
                f"chunk {idx + 1}/{total}: only {len(parsed.responsive_models)}/{required} reviewers parsed",
            )
        merged_results.extend(chunk_json.get("results") or [])
    return (
        prompt,
        advisory_evidence,
        json.dumps({"results": merged_results}, ensure_ascii=False),
        "",
    )
