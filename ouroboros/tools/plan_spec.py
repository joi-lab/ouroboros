"""The ONE structural path fact of ``plan_task`` and the spec reads of earlier plan
records — pure functions only (no I/O, no ToolContext).

``resolve_constitutional`` decides whether declared paths change Ouroboros's own
body, with the locator normalisation ``ouroboros.tools.plan_evidence`` shares; the
planning organ's body predicate (``plan_ask.plan_is_body``) composes it. ``spec_hash``
and ``spec_delta`` read the specs of earlier-schema (v1/v2) plan-review records for
the read-only views and the reflection; nothing writes a spec any more.

Design principle (BIBLE P5, owner-approved 2026-08-15): the HOST checks ONE
structural path fact and never interprets the domain — a plan for a code change, a
slide deck, a literature review, a GUI flow or a trip plan works identically, and a
plan with ZERO file paths is first-class.
"""

from __future__ import annotations

from hashlib import sha256
import json
import pathlib
import re
from typing import Any, Iterable, Mapping, Optional

from ouroboros.tool_access import path_is_relative_to


_SPEC_STRING_LISTS = (
    "in_scope", "non_goals", "invariants", "affected_paths", "affected_resources", "evidence",
)
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_FILE_SCHEME = "file://"
_TASK_LOCATOR_PREFIX = "task:"


# ------------------------------------------------------ earlier-schema spec reads


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def spec_hash(spec: Mapping[str, Any]) -> str:
    """sha256 over canonical JSON (sorted keys, compact separators): stable under key order/whitespace."""
    return _canonical_hash(dict(spec))


def _by_id(items: Any) -> dict[str, dict]:
    return {
        str(item["id"]): dict(item)
        for item in (items or []) if isinstance(item, Mapping) and item.get("id")
    }


def _text_by_id(spec: Mapping[str, Any]) -> dict[str, str]:
    """id → identifying text for every id-bearing element (invariants positional)."""
    out = {f"invariant_{i}": t for i, t in enumerate(spec.get("invariants") or [], start=1)}
    for key, text_key in (("acceptance_claims", "claim"), ("decisions", "choice"), ("deferred", "what")):
        for item in spec.get(key) or []:
            if isinstance(item, Mapping) and item.get("id"):
                out[str(item["id"])] = str(item.get(text_key) or "")
    return out


def _renumbered(prev: Mapping[str, Any], spec: Mapping[str, Any]) -> list[dict]:
    """Same identifying text under a DIFFERENT id between cycles → ``[{from, to, text}]``.
    Ids are positional/minted, so an agent dropping or reordering one element shifts the
    later ones; the reviewer must re-target ``breaks`` against the CURRENT ids (B-02)."""
    before, after = _text_by_id(prev), _text_by_id(spec)
    after_by_text: dict[str, list[str]] = {}
    for fid, text in after.items():
        after_by_text.setdefault(text, []).append(fid)
    out: list[dict] = []
    for old_id, text in before.items():
        if not text or after.get(old_id) == text:
            continue
        for new_id in after_by_text.get(text, []):
            if new_id != old_id and before.get(new_id) != text:
                out.append({"from": old_id, "to": new_id, "text": text})
                break
    return out


def spec_delta(prev_spec: Mapping[str, Any] | None, spec: Mapping[str, Any]) -> dict:
    """What changed between two normalized specs (wave ≥2 packets).

    Id-bearing collections (acceptance_claims, decisions, deferred) diff by id
    (added / removed / changed content); plain string lists diff by text; the
    goal is a changed flag; ``renumbered`` lists elements whose text moved to a
    different id (positional ids shift when an earlier element is dropped or
    reordered); ``declared_keys_changed`` reports a spec whose set of declared keys
    itself moved (a spec stored before ``affected_paths`` existed declares no such
    key at all). ``prev_spec=None`` means everything is added.
    """
    prev = dict(prev_spec or {})
    ids: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}
    for key in ("acceptance_claims", "decisions", "deferred"):
        before, after = _by_id(prev.get(key)), _by_id(spec.get(key))
        ids["added"].extend(sorted(set(after) - set(before)))
        ids["removed"].extend(sorted(set(before) - set(after)))
        ids["changed"].extend(sorted(k for k in set(before) & set(after) if before[k] != after[k]))
    lists: dict[str, dict[str, list[str]]] = {}
    for key in _SPEC_STRING_LISTS:
        before_l, after_l = list(prev.get(key) or []), list(spec.get(key) or [])
        lists[key] = {
            "added": [t for t in after_l if t not in before_l],
            "removed": [t for t in before_l if t not in after_l],
        }
    goal_changed = str(prev.get("goal") or "") != str(spec.get("goal") or "")
    # A key that appears or disappears between two normalized specs is a change even when both
    # sides read as empty: `affected_paths` ABSENT (stored before the field existed) and
    # `affected_paths: []` ("this work changes no files") are different claims, and the spec
    # hash already says so — the delta the reviewer reads must not say they are the same.
    declared_keys_changed = prev_spec is not None and (
        {key for key in prev if key != "normalization_omissions"}
        != {key for key in spec if key != "normalization_omissions"}
    )
    changed = goal_changed or declared_keys_changed or any(ids.values()) or any(
        entry["added"] or entry["removed"] for entry in lists.values()
    )
    return {
        "changed": changed,
        "goal_changed": goal_changed,
        "declared_keys_changed": declared_keys_changed,
        "ids": ids,
        "lists": lists,
        "renumbered": _renumbered(prev, spec),
        "prev_hash": spec_hash(prev) if prev_spec is not None else "",
        "hash": spec_hash(spec),
    }


# ------------------------------------------------------------------ constitutional


def _is_url(locator: str) -> bool:
    return bool(_URL_RE.match(locator)) and not locator.startswith(_FILE_SCHEME)


def _is_path_locator(locator: str) -> bool:
    return bool(locator) and not _is_url(locator) and not locator.startswith((_TASK_LOCATOR_PREFIX, "chat:"))

def _resolve_locator_path(locator: str, root: pathlib.Path) -> tuple[Optional[pathlib.Path], str]:
    """Relative → under ``root``; absolute (or ``file://`` absolute) as-is. Returns
    ``(path, "")`` or ``(None, reason)`` — ``symlink_loop`` (RuntimeError from resolve) or
    ``unsupported_locator`` (NUL bytes, over-long components, …). Never raises."""
    if locator.startswith(_FILE_SCHEME):
        locator = locator[len(_FILE_SCHEME):]
    try:
        candidate = pathlib.Path(locator)
        return (candidate if candidate.is_absolute() else root / candidate).resolve(strict=False), ""
    except RuntimeError:
        return None, "symlink_loop"
    except (OSError, ValueError):
        return None, "unsupported_locator"


def _under(path: pathlib.Path, root: pathlib.Path) -> bool:
    """``tool_access.path_is_relative_to`` guarded against symlink loops on re-resolution."""
    try:
        return path_is_relative_to(path, root)
    except RuntimeError:
        return False


def resolve_constitutional(
    *,
    active_root: str | pathlib.Path,
    system_repo_root: str | pathlib.Path,
    affected_paths: Iterable[str],
    evidence: Iterable[str],
    payload_roots: Iterable[str | pathlib.Path] = (),
) -> tuple[bool, str]:
    """ONE structural fact: will this plan CHANGE Ouroboros's own body?

    True iff a declared ``affected_paths`` locator (relative resolved against
    ``active_root``, absolute or ``file://`` as-is) resolves to or under the
    system repository — whether or not the file exists yet, because creating
    ``ouroboros/new_module.py`` IS self-modification. ``affected_resources`` is
    never read here: treating every non-URL string in it as a path is what turned
    a Russian sentence about a project into "a file under the repo" and billed the
    whole constitution to a deck plan (owner decisions 8=A/9=A/16=A).
    ``evidence`` is what to LOOK AT — reading a repository file is not changing it,
    so an evidence locator never decides on its own (16=A); when the work will also
    change that file it is in ``affected_paths`` and decides there. System-repo
    evidence reads are named in the note so the author can see why the constitutional
    pack was not bought (P1). The active binding alone does NOT decide (owner
    decision D29: a plan bound to the system repo that declares no system path is
    not constitutional). Skill-payload paths under ``payload_roots`` are exempt
    (data plane, as today). URLs and ``task:``/``chat:`` locators never make it
    true. Returns ``(constitutional, note)`` — the note names the deciding
    locator for disclosure.
    """
    from ouroboros.tools.plan_evidence import _split_selector
    system = pathlib.Path(system_repo_root).resolve(strict=False)
    active = pathlib.Path(active_root).resolve(strict=False)
    # Payload roots are supplied by the FROZEN skill-payload predicate
    # (``plan_review_runtime.plan_payload_roots`` -> ``resolve_skill_payload_target``),
    # never by the plan text, so a caller cannot dress an arbitrary system path as a
    # payload. They intentionally survive even when they resolve INSIDE the system
    # repo: a skill task whose active workspace IS the repo still touches only
    # data-plane payload files (frozen behaviour, task 7881ad77902d4d25). A reviewer
    # proposal to drop system-repo-nested payload roots (S-B05) was REJECTED for that
    # reason; the residual it names requires control of the payload predicate itself.
    payloads = [pathlib.Path(p).resolve(strict=False) for p in payload_roots]

    def _system_locators(locators: Iterable[str]):
        """Lazily yield the declared locators that are PATHS resolving under the system repo,
        verbatim — the first one is enough to decide, so nothing further is resolved."""
        for raw in locators or []:
            locator = str(raw or "").strip()
            if not _is_path_locator(locator):
                continue
            resolved, _reason = _resolve_locator_path(_split_selector(locator)[0], active)
            if resolved is None or any(_under(resolved, payload) for payload in payloads):
                continue
            if resolved == system or _under(resolved, system):
                yield locator

    for target in _system_locators(affected_paths):
        # The locator is quoted VERBATIM (never ``repr``): the note is disclosure a
        # reviewer copies back, and ``repr`` doubles every backslash of a Windows path.
        return True, (
            f"constitutional: affected_paths locator '{target}' resolves under the "
            "Ouroboros system repository (structural fact)"
        )
    reads: list[str] = []
    for read in _system_locators(evidence):
        reads.append(read)
        if len(reads) >= 5:  # the note names them; a sixth adds nothing a reviewer acts on
            break
    if reads:
        listed = ", ".join(f"'{item}'" for item in reads)
        return False, (
            "not constitutional: the system-repo locators declared are EVIDENCE reads "
            f"({listed}), and reading a repository file is not changing it — list a file "
            "under affected_paths if the work will change it"
        )
    return False, (
        "not constitutional: no declared affected_paths locator resolves under the "
        "Ouroboros system repository"
    )
