"""Packet assembly on the REAL chaptered tree, with no model call.

The unit cutover can be green while an assembled packet is still wrong: a prompt
builder that received a membership page produces a packet that looks complete
and carries none of the book. The constitutional plan packet is the surface
where that would ship — it is the one review packet that inlines a whole
reference book — and it is asserted against the actual chapters in this
repository rather than a synthetic corpus, because the synthetic corpora are
exactly what hid the regression.

No provider is contacted: the prompt builders are pure assembly.
"""

import pathlib

import pytest

from ouroboros.reference_books import BOOK_ENTRYPOINTS, load_reference_book

REPO = pathlib.Path(__file__).resolve().parents[1]


def _chapter_tails() -> dict[str, str]:
    """The last 200 chars of every chapter: a needle no other file carries."""
    tails = {}
    for book_id in BOOK_ENTRYPOINTS:
        for chapter in load_reference_book(REPO, book_id).chapters:
            tails[chapter.source_path] = chapter.text[-200:]
    return tails


def test_a_constitutional_plan_packet_inlines_the_chapters_not_the_membership():
    from ouroboros.tools.plan_packet import PlanPacketError, build_plan_review_system_prompt
    from ouroboros.tools.plan_review_runtime import _governance_text

    architecture = _governance_text(REPO, "docs/ARCHITECTURE.md")
    bible = _governance_text(REPO, "BIBLE.md")
    prompt = build_plan_review_system_prompt(
        checklist_section="## Plan Review Checklist\n\nx",
        constitutional=True,
        bible_text=bible,
        architecture_text=architecture,
        cycle_index=1,
        enforcement="blocking",
    )
    assert "inline, in full, for a self-modification" in prompt
    for path, tail in _chapter_tails().items():
        if path.startswith("docs/architecture/"):
            assert prompt.count(tail) == 1, path

    # The claim and the bytes are one fact: a constitutional packet that has
    # only the membership page must not be assemblable at all.
    with pytest.raises(PlanPacketError):
        build_plan_review_system_prompt(
            checklist_section="x", constitutional=True, bible_text=bible,
            architecture_text="", cycle_index=1, enforcement="blocking",
        )


def test_a_non_constitutional_plan_packet_points_at_chapters_by_path():
    from ouroboros.tools.plan_packet import build_plan_review_system_prompt
    from ouroboros.tools.plan_review_runtime import _architecture_navigation, _governance_text

    architecture = _governance_text(REPO, "docs/ARCHITECTURE.md")
    prompt = build_plan_review_system_prompt(
        checklist_section="x", constitutional=False, bible_text=None, cycle_index=1,
        enforcement="advisory",
        architecture_nav_map=_architecture_navigation(REPO, architecture),
        bible_nav_map="## BIBLE.md (navigation map)\n\n- P0 — lines 1-9",
    )
    assert "Source: `docs/architecture/06-agent-core.md`" in prompt
    for path, tail in _chapter_tails().items():
        assert tail not in prompt, f"a pointer view must not inline {path}"
