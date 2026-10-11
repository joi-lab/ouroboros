"""The ONE decision about which governance documents a reviewer receives in full.

Owner decision (2026-09-17, batch 2 question 1 = A): every review surface —
triad, scope, advisory, deep self-review — asks this module which rules it must
carry inline and which it reaches through a navigation map. Three tiers:

1. **Rules, always inline.** The surface's applicable ``docs/CHECKLISTS.md``
   section (supplied by the caller, which owns its own section name),
   ``BIBLE.md`` whole and ``docs/CHECKLISTS_ARCHIVE.md`` whole. This tier is
   byte-stable across commits, so the caller puts it in its cache-marked prefix.
2. **Rules by change class, within a budget share.** ``docs/DESIGN.md`` when the
   change touches ``web/``; the review-and-commit protocol chapter always; and
   every DEVELOPMENT chapter whose text mentions a touched file NAME. Bounded by
   :data:`~ouroboros.runtime_limits.REVIEW_GOVERNANCE_INLINE_SHARE` of the
   reviewer's usable window, largest-relevance-first.
3. **Map, never whole.** ``docs/ARCHITECTURE.md`` arrives as book navigation for
   every delivery. A PACKET reviewer (a row with no tools of its own)
   additionally receives the top-level sections whose text mentions a touched
   file name, from the same budget; a RETRIEVING row gets the navigation and
   reads what it needs with its own ``read_file``.

Every document that is not inlined is NAMED in the returned manifest and in the
navigation text, never silently dropped (BIBLE P1), and the manifest is the
disclosure record the durable review evidence carries.

Choosing which reference chapter to inline from an exact file-name mention is
context ASSEMBLY, not behaviour selection: no verdict, routing decision or
reviewer judgment depends on the match, and the reviewer may read anything else
it wants. No LLM call; one tree plus one touched-path list gives one result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ouroboros.runtime_limits import REVIEW_GOVERNANCE_INLINE_SHARE
from ouroboros.utils import estimate_tokens

BIBLE_PATH = "BIBLE.md"
CHECKLISTS_PATH = "docs/CHECKLISTS.md"
CHECKLISTS_ARCHIVE_PATH = "docs/CHECKLISTS_ARCHIVE.md"
DESIGN_PATH = "docs/DESIGN.md"
DEVELOPMENT_BOOK_ID = "development"
ARCHITECTURE_BOOK_ID = "architecture"
# Always relevant: it is the protocol the reviewer is executing right now.
REVIEW_PROTOCOL_CHAPTER = "docs/development/05-review-and-commit-protocol.md"
# The one change class that decides a whole document: DESIGN governs web/ work.
DESIGN_CHANGE_CLASS_PREFIX = "web/"

PACKET_DELIVERY = "packet"
RETRIEVING_DELIVERY = "retrieving"
_CARRIED_REASON = "carried by this surface's own delivery"


@dataclass(frozen=True)
class GovernanceContext:
    """What one reviewer receives, and the record of what it did not receive.

    ``stable_inline``/``selected_inline`` render the same sections for the
    caller's two prompt regions: tier 1 belongs in the cache-marked prefix, tier
    2/3 in the change-relative tail. ``inline_whole_documents`` maps a path to
    the exact text inlined here, so a caller that also packs touched files
    suppresses the duplicate instead of sending one document twice."""

    inline_sections: list = field(default_factory=list)   # [(title, text)] in prompt order
    navigation: str = ""
    manifest: list = field(default_factory=list)          # [{path, tier, disposition, chars, reason}]
    tokens_estimate: int = 0
    stable_inline: str = ""
    selected_inline: str = ""
    inline_whole_documents: dict = field(default_factory=dict)


def _normalize(path: Any) -> str:
    return str(path or "").replace("\\", "/").lstrip("./")


def _touched_names(touched_paths: Optional[Iterable[Any]]) -> tuple[tuple[str, ...], ...]:
    """One name group per touched file: its repo-relative path and its basename.

    Directory prefixes are deliberately absent — ``web/`` or ``ouroboros/``
    would match nearly every chapter and so select nothing."""
    groups: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for raw in touched_paths or ():
        rel = _normalize(raw)
        if not rel or rel.endswith("/") or rel in seen:
            continue
        seen.add(rel)
        base = rel.rsplit("/", 1)[-1]
        groups.append((rel,) if base == rel else (rel, base))
    return tuple(groups)


def _mentions(text: str, groups: Iterable[tuple[str, ...]]) -> tuple[int, int]:
    """``(touched files mentioned, mentions)`` of those files in ``text``.

    A name matches only as its own token: ``git.py`` inside ``review_git.py`` is
    not a mention, while ``ouroboros/git.py`` is one (a path separator may
    precede a basename). The largest per-spelling count wins, so a full path
    beside its own basename is one mention, not two."""
    files = mentions = 0
    for group in groups:
        found = max(
            len(re.findall(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text))
            for name in group)
        if found:
            files += 1
            mentions += found
    return files, mentions


def _carried_reasons(already_inline: Any) -> dict:
    """``{path: reason}`` for the documents the CALLER's delivery already carries.

    A plain iterable of paths takes the default wording; a mapping lets the
    caller state the existing inline location, such as a constitutional head.
    A pointer or an instruction to read later is not inline delivery."""
    if isinstance(already_inline, Mapping):
        return {_normalize(path): str(reason or _CARRIED_REASON) or _CARRIED_REASON
                for path, reason in already_inline.items()}
    return {_normalize(path): _CARRIED_REASON for path in already_inline or ()}


def _row(path: str, tier: int, disposition: str, chars: int, reason: str) -> dict:
    return {"path": path, "tier": tier, "disposition": disposition,
            "chars": int(chars), "reason": reason}


def _render(title: str, text: str) -> str:
    return f"## {title}\n\n{text}"


def _join(parts: Iterable[str]) -> str:
    return "\n\n".join(part for part in parts if str(part or "").strip())


def _load_book(repo_dir: Path, book_id: str):
    """``(book, "")`` or ``(None, reason)`` — an unreadable book is disclosed,
    never rendered as a delivered one."""
    from ouroboros.reference_books import load_reference_book

    try:
        return load_reference_book(repo_dir, book_id), ""
    except (OSError, ValueError, KeyError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _book_sources(book) -> tuple:
    """A chaptered book's chapters; a legacy revision has none to select from."""
    return () if book is None or book.legacy else tuple(book.chapters)


def _top_sections(source) -> tuple:
    """The chapter's own top-level sections, heading plus complete subtree.

    A chapter keeps its relocated body at the heading level that body already
    had, so the monolith's ``##`` entries sit at ``###`` in a split chapter and
    at ``##`` in one authored after the split: the shallowest level below the
    chapter's H1 selects the same sections in both shapes. A chapter with no
    subsections contributes none and stays addressable through the navigation."""
    levels = [heading.level for heading in source.headings if heading.level > 1]
    if not levels:
        return ()
    top = min(levels)
    return tuple(heading for heading in source.headings if heading.level == top)


def _book_navigation(repo_dir: Path, book_id: str, book, load_doc, *, instructions: bool = True) -> str:
    from ouroboros.context_layout import book_navigation, generate_doc_nav_map
    from ouroboros.reference_books import BOOK_ENTRYPOINTS

    entrypoint = BOOK_ENTRYPOINTS[book_id]
    if book is not None:
        return book_navigation(book, instructions=instructions)
    # No book: map whatever the entrypoint reader returns, so the document stays
    # named and addressable even when its membership cannot be assembled.
    text = load_doc(repo_dir, entrypoint, on_missing="explicit")
    return generate_doc_nav_map(
        text, title=entrypoint.rsplit("/", 1)[-1], rel_path=entrypoint, instructions=instructions)


def governance_context(
    repo_dir: Any,
    *,
    surface: str,
    touched_paths: Optional[Iterable[Any]] = None,
    usable_window_tokens: int = 0,
    delivery: str = PACKET_DELIVERY,
    checklist_section_text: str = "",
    already_inline: Any = (),
) -> GovernanceContext:
    """Tier the governance corpus for ONE reviewer of ONE change.

    ``surface`` labels the review surface in the disclosure record.
    ``checklist_section_text`` is the tier-1 section that surface already
    loaded; a surface supplying none has ``docs/CHECKLISTS.md`` recorded as read
    on demand, never as an inlined section of zero characters.
    ``already_inline`` names what the caller's OWN delivery carries in full
    (:func:`_carried_reasons` states the mechanism)."""
    from ouroboros.tools.review_helpers import load_governance_doc

    root = Path(repo_dir)
    names = _touched_names(touched_paths)
    carried = _carried_reasons(already_inline)
    touched = {_normalize(path) for path in (touched_paths or ())}
    rows: list[dict] = []
    tier1: list[tuple[str, str]] = []
    selected: list[tuple[str, str]] = []
    inline_texts: dict[str, str] = {}

    # --- tier 1: rules, always inline ------------------------------------
    checklist_text = str(checklist_section_text or "")
    if checklist_text.strip():
        rows.append(_row(CHECKLISTS_PATH, 1, "inline", len(checklist_text),
                         f"applicable {surface} checklist section, supplied by the surface"))
    else:
        rows.append(_row(CHECKLISTS_PATH, 1, "navigation", 0,
                         "this surface supplies no checklist section"))
    for path in (BIBLE_PATH, CHECKLISTS_ARCHIVE_PATH):
        if path in carried:
            already = load_governance_doc(root, path, on_missing="silent")
            rows.append(_row(path, 1, "inline", len(already), carried[path]))
            continue
        text = load_governance_doc(root, path, on_missing="explicit")
        tier1.append((path, text))
        inline_texts[path] = text
        rows.append(_row(path, 1, "inline", len(text), "tier 1: every reviewer carries it"))

    # --- the change-class budget: ONE share for tier 2 and tier 3 together,
    # rules by change class drawing first, architecture sections from the rest.
    budget = max(0, int(int(usable_window_tokens or 0) * REVIEW_GOVERNANCE_INLINE_SHARE))
    remaining = budget

    def _admit(path: str, title: str, text: str, tier: int, reason: str) -> bool:
        nonlocal remaining
        cost = estimate_tokens(_render(title, text))
        if cost > remaining:
            rows.append(_row(path, tier, "navigation", len(text),
                             f"{reason}; inline share exhausted "
                             f"({cost} tokens needed, {remaining} of {budget} left)"))
            return False
        remaining -= cost
        selected.append((title, text))
        if "#" not in path:
            inline_texts[path] = text
        rows.append(_row(path, tier, "inline", len(text), reason))
        return True

    # --- tier 2: rules by change class ------------------------------------
    dev_book, dev_problem = _load_book(root, DEVELOPMENT_BOOK_ID)
    dev_chapters = _book_sources(dev_book)
    rows.append(_row("docs/DEVELOPMENT.md", 2, "navigation", 0,
                     "book navigation; its chapters are selected by change class" if dev_chapters
                     else dev_problem or "no chaptered membership: navigation only"))

    candidates: list[tuple[tuple, str, str, str]] = []  # (order, path, title, reason)
    for chapter in dev_chapters:
        path = chapter.source_path
        files, mentions = _mentions(chapter.text, names)
        if path == REVIEW_PROTOCOL_CHAPTER:
            candidates.append(((0, 0, path), path, path, "the review protocol this reviewer executes"))
        elif files:
            candidates.append(((2, -files * 1000 - mentions, path), path, path,
                               f"mentions {files} touched file(s), {mentions} time(s)"))
        else:
            rows.append(_row(path, 2, "navigation", len(chapter.text),
                             "no touched file mentioned"))

    design_touched = any(path.startswith(DESIGN_CHANGE_CLASS_PREFIX) for path in touched)
    design_text = ""
    if design_touched:
        design_text = load_governance_doc(root, DESIGN_PATH, on_missing="explicit")
        candidates.append(((1, 0, DESIGN_PATH), DESIGN_PATH, DESIGN_PATH,
                           f"a touched path is under {DESIGN_CHANGE_CLASS_PREFIX}"))
    else:
        rows.append(_row(DESIGN_PATH, 2, "navigation", 0,
                         f"no touched path under {DESIGN_CHANGE_CLASS_PREFIX}"))

    chapter_text = {chapter.source_path: chapter.text for chapter in dev_chapters}
    chapter_text[DESIGN_PATH] = design_text
    for order, path, title, reason in sorted(candidates):
        _admit(path, title, chapter_text.get(path, ""), 2, reason)

    # --- tier 3: the map, never whole -------------------------------------
    arch_book, arch_problem = _load_book(root, ARCHITECTURE_BOOK_ID)
    arch_chapters = _book_sources(arch_book)
    packet = str(delivery or PACKET_DELIVERY) == PACKET_DELIVERY
    arch_reason = "the map is delivered as navigation, never whole; " + (
        "the sections that name a touched file are selected below" if packet
        else "this reviewer reads sections on demand with its own tools")
    rows.append(_row("docs/ARCHITECTURE.md", 3, "navigation", 0, arch_reason if arch_chapters
                     else arch_problem or "no chaptered membership: navigation only"))
    sections: list[tuple[tuple, str, str, str]] = []
    for chapter in arch_chapters:
        rows.append(_row(chapter.source_path, 3, "navigation", len(chapter.text),
                         "the map is never inlined whole; chapter navigation above"))
        if not packet or not names:
            continue
        for heading in _top_sections(chapter):
            body = chapter.text_at(heading.section)
            files, mentions = _mentions(body, names)
            if not files:
                continue
            path = f"{chapter.source_path}#{heading.title}"
            sections.append(((-files * 1000 - mentions, chapter.source_path, heading.span.start_byte),
                             path, path, f"mentions {files} touched file(s), {mentions} time(s)"))
            chapter_text[path] = body
    for order, path, title, reason in sorted(sections):
        _admit(path, title, chapter_text.get(path, ""), 3, reason)

    # --- navigation -------------------------------------------------------
    checklist_pointer = ("the section that applies to this review is inlined above."
                         if checklist_text.strip() else
                         "NO section of it is inlined for this review.")
    pointers = [f"- `{CHECKLISTS_PATH}` — the complete checklist book; {checklist_pointer}"]
    if not design_touched:
        pointers.append(f"- `{DESIGN_PATH}` — the design system; not inlined because no "
                        f"touched path is under `{DESIGN_CHANGE_CLASS_PREFIX}`.")
    navigation = _join([
        "## Governance navigation (index of sources not inlined)" if packet else
        "## Governance navigation (read on demand)",
        ("The supplied rules and selected sections are the governance evidence this packet "
         "delivers. This row has no repository tools: the map below identifies sources and "
         "ranges that were NOT delivered, not additional evidence. State any resulting "
         "uncertainty; do not claim to have read the named sources." if packet else
        "The rules above are the ones this change activates. Everything below is complete "
        "on disk and untruncated: read any range with "
        '`read_file(root="system_repo", path=..., start_line=A, max_lines=N)`. Ranges are '
        "inclusive and a parent heading includes its descendant group. A document named here "
        "and not inlined was NOT dropped — it is one read away, and the review's governance "
        "manifest records the disposition of each one."),
        "\n".join(pointers),
        _book_navigation(root, DEVELOPMENT_BOOK_ID, dev_book, load_governance_doc, instructions=not packet),
        _book_navigation(root, ARCHITECTURE_BOOK_ID, arch_book, load_governance_doc, instructions=not packet),
    ])

    stable_inline = _join(_render(title, text) for title, text in tier1)
    selected_inline = _join(_render(title, text) for title, text in selected)
    return GovernanceContext(
        inline_sections=tier1 + selected,
        navigation=navigation,
        manifest=rows,
        tokens_estimate=estimate_tokens(_join([stable_inline, selected_inline, navigation])),
        stable_inline=stable_inline,
        selected_inline=selected_inline,
        inline_whole_documents=inline_texts,
    )
