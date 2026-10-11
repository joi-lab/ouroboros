"""The grant rules behind the reference-book chapter budgets, proven on throwaway trees.

``tests/test_reference_book_budgets.py`` applies these helpers to the real chapters in the
official-CI ``size_ratchet`` lane. Here each rule fires on the case it blocks and stays quiet on the
case it allows, without the repository's own chapters or history, so the default lanes run it.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from tests.test_reference_book_budgets import (
    BUDGETS_MODULE,
    GRANTS_TREE,
    base_facts,
    budget_faults,
    grant_layout_faults,
    growth_base,
    measures_one_change,
    growth_faults,
    read_grants,
)

CHAPTER = "docs/development/02-example.md"
OTHER = "docs/architecture/06-other.md"
GRANT = "development/02-example/2026-10-02-example.grant"
REASON = "The chapter gains the paragraph this change describes.\n"


def _write(root: pathlib.Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


# --- the limit: base number plus the chapter's grants ---------------------------------------------


def test_a_grant_raises_the_limit_by_exactly_its_bytes(tmp_path):
    assert budget_faults({CHAPTER: 1000}, {CHAPTER: 1000}, {}) == []
    assert len(budget_faults({CHAPTER: 1001}, {CHAPTER: 1000}, {})) == 1

    _write(tmp_path, GRANT, "500\n" + REASON)
    grants = read_grants(tmp_path)
    assert grants == {CHAPTER: [(GRANT, 500)]}
    assert budget_faults({CHAPTER: 1500}, {CHAPTER: 1000}, grants) == []
    (fault,) = budget_faults({CHAPTER: 1501}, {CHAPTER: 1000}, grants)
    assert fault.startswith(f"{CHAPTER}: 1501 bytes exceeds its budget 1500 (base 1000 + 1 grant file(s) totalling 500)")
    assert f"add {GRANTS_TREE}/development/02-example/<YYYY-MM-DD>-<slug>.grant holding this change's NET" in fault
    assert "replace the description you touched instead of appending" in fault


def test_two_grants_add_up(tmp_path):
    _write(tmp_path, GRANT, "500\n" + REASON)
    _write(tmp_path, "development/02-example/2026-10-03-second.grant", "40\n" + REASON)
    grants = read_grants(tmp_path)
    assert budget_faults({CHAPTER: 1540}, {CHAPTER: 1000}, grants) == []
    (fault,) = budget_faults({CHAPTER: 1541}, {CHAPTER: 1000}, grants)
    assert "exceeds its budget 1540 (base 1000 + 2 grant file(s) totalling 540)" in fault


def test_a_grant_under_another_chapter_does_not_help(tmp_path):
    _write(tmp_path, "architecture/06-other/2026-10-02-elsewhere.grant", "500\n" + REASON)
    grants = read_grants(tmp_path)
    numbers = {CHAPTER: 1000, OTHER: 1000}
    assert budget_faults({CHAPTER: 1000, OTHER: 1500}, numbers, grants) == []
    (fault,) = budget_faults({CHAPTER: 1001, OTHER: 1000}, numbers, grants)
    assert fault.startswith(f"{CHAPTER}: 1001 bytes exceeds its budget 1000 (base 1000 + 0 grant file(s)")


def test_a_chapter_without_a_base_and_a_base_without_a_chapter_are_both_named():
    assert budget_faults({CHAPTER: 10}, {}, {CHAPTER: [(GRANT, 500)]}) == [
        f"{CHAPTER}: add a byte budget for the new chapter"
    ]
    assert budget_faults({CHAPTER: 10}, {CHAPTER: 10, OTHER: 10}, {}) == [
        f"budgets for chapters that no longer exist: ['{OTHER}']"
    ]


# --- the layout: nothing under the grant tree but a dot-file is silently ignored -------------------

PLACE = "sits at <book>/<chapter-stem>/<name>.grant"
INTEGER = "a positive integer and nothing else"
NO_REASON = "the reason, at least one non-blank line"
GOOD = "500\n" + REASON


def test_a_tree_of_well_formed_grants_or_no_tree_at_all_has_no_faults(tmp_path):
    assert grant_layout_faults(tmp_path / "absent", [CHAPTER]) == []
    assert read_grants(tmp_path / "absent") == {}
    _write(tmp_path, GRANT, "500\n\n" + REASON + "A second reason line.\n")
    _write(tmp_path, "architecture/06-other/free name.grant", "7\n" + REASON)
    assert grant_layout_faults(tmp_path, [CHAPTER, OTHER]) == []


def test_the_desktops_own_dot_files_are_not_grants_and_not_faults(tmp_path):
    _write(tmp_path, GRANT, GOOD)
    for stray in (".DS_Store", "development/.DS_Store", "development/02-example/.2026-10-02-example.grant"):
        _write(tmp_path, stray, "999\n" + REASON)
    assert grant_layout_faults(tmp_path, [CHAPTER]) == []
    assert read_grants(tmp_path) == {CHAPTER: [(GRANT, 500)]}, "a dot-file grants nothing either"


def test_a_crlf_checkout_reads_the_same_grant(tmp_path):
    _write(tmp_path, GRANT, "500\r\n" + REASON.replace("\n", "\r\n"))
    assert grant_layout_faults(tmp_path, [CHAPTER]) == []
    assert read_grants(tmp_path) == {CHAPTER: [(GRANT, 500)]}


@pytest.mark.parametrize(
    ("rel", "content", "rule"),
    [
        pytest.param("development/02-example/notes.txt", GOOD, PLACE, id="wrong-extension"),
        pytest.param("README", GOOD, PLACE, id="depth-root"),
        pytest.param("development/loose.grant", GOOD, PLACE, id="depth-shallow"),
        pytest.param("development/02-example/more/deep.grant", GOOD, PLACE, id="depth-deep"),
        pytest.param("guides/02-example/a.grant", GOOD, "'guides' is not a reference book", id="unknown-book"),
        pytest.param(
            "development/99-gone/a.grant", GOOD, "docs/development/99-gone.md is not a current chapter", id="unknown-chapter"
        ),
        pytest.param(GRANT, "0\n" + REASON, INTEGER, id="zero"),
        pytest.param(GRANT, "-500\n" + REASON, INTEGER, id="negative"),
        pytest.param(GRANT, "+500\n" + REASON, INTEGER, id="plus-sign"),
        pytest.param(GRANT, "12.5\n" + REASON, INTEGER, id="non-integer"),
        pytest.param(GRANT, "500 bytes\n" + REASON, INTEGER, id="integer-with-words"),
        pytest.param(GRANT, REASON + "500\n", INTEGER, id="reason-first"),
        pytest.param(GRANT, "", INTEGER, id="empty-file"),
        pytest.param(GRANT, "500\n", NO_REASON, id="no-reason"),
        pytest.param(GRANT, "500\n\n  \n\t\n", NO_REASON, id="blank-reason"),
    ],
)
def test_a_file_that_is_not_a_usable_grant_is_named_with_its_rule(tmp_path, rel, content, rule):
    _write(tmp_path, rel, content)
    (fault,) = grant_layout_faults(tmp_path, [CHAPTER])
    assert fault.startswith(f"{rel}: ") and rule in fault
    assert CHAPTER not in read_grants(tmp_path), "a file the layout check rejects must grant nothing"


# --- the growth rule: one change against its own base ----------------------------------------------


def _growth(tip, *, tip_grants=(), base_grants=(), tip_number=1000, base=1000, base_number=1000):
    return growth_faults(
        {CHAPTER: base, OTHER: 1000},
        {CHAPTER: tip, OTHER: 1000},
        base_grants,
        {CHAPTER: list(tip_grants)},
        {CHAPTER: base_number, OTHER: 1000},
        {CHAPTER: tip_number, OTHER: 1000},
    )


def test_growth_covered_by_a_grant_the_change_adds_passes():
    assert _growth(1500, tip_grants=[(GRANT, 500)]) == []


def test_growth_beyond_the_added_grant_names_the_uncovered_bytes():
    (fault,) = _growth(1500, tip_grants=[(GRANT, 481)])
    assert fault.startswith(
        f"{CHAPTER}: grew 500 bytes in this change, 481 covered (481 by grant files it adds, 0 by a raised base number), "
        "19 uncovered"
    )
    assert f"add {GRANTS_TREE}/development/02-example/<YYYY-MM-DD>-<slug>.grant for the uncovered bytes" in fault
    (fault,) = _growth(1001)
    assert "grew 1 bytes in this change, 0 covered" in fault and "1 uncovered" in fault


def test_growth_covered_by_a_raised_base_number_passes():
    assert _growth(1500, tip_number=1500) == []
    assert _growth(1500, tip_number=1300, tip_grants=[(GRANT, 200)]) == []
    (fault,) = _growth(1500, tip_number=1300)
    assert "300 covered (0 by grant files it adds, 300 by a raised base number), 200 uncovered" in fault


def test_a_chapter_that_shrank_or_did_not_change_needs_nothing():
    assert _growth(900) == []
    assert _growth(900, tip_number=950) == []  # a fold that lowers the base number is not negative cover
    assert _growth(1000) == []


def test_a_lowered_base_number_does_not_eat_the_grant_the_change_adds():
    assert _growth(1500, tip_number=900, tip_grants=[(GRANT, 500)]) == []


def test_a_chapter_new_in_the_change_is_bound_by_its_base_number_alone():
    assert growth_faults({}, {CHAPTER: 5000}, (), {}, {}, {CHAPTER: 5000}) == []
    # Not measured from zero either: a number the base already carried for the path is no raise.
    assert growth_faults({}, {CHAPTER: 5000}, (), {}, {CHAPTER: 5000}, {CHAPTER: 5000}) == []


def test_a_grant_that_existed_at_the_base_does_not_count_as_added():
    (fault,) = _growth(1500, tip_grants=[(GRANT, 500)], base_grants=[GRANT])
    assert "grew 500 bytes in this change, 0 covered" in fault and "500 uncovered" in fault
    newer = "development/02-example/2026-10-03-newer.grant"
    assert _growth(1500, tip_grants=[(GRANT, 500), (newer, 500)], base_grants=[GRANT]) == []


def test_a_grant_added_under_another_chapter_does_not_cover_growth():
    faults = growth_faults(
        {CHAPTER: 1000, OTHER: 1000},
        {CHAPTER: 1500, OTHER: 1000},
        (),
        {OTHER: [("architecture/06-other/2026-10-02-elsewhere.grant", 500)]},
        {CHAPTER: 1000, OTHER: 1000},
        {CHAPTER: 1000, OTHER: 1000},
    )
    assert len(faults) == 1 and faults[0].startswith(f"{CHAPTER}: grew 500 bytes")


# --- the base facts: read from the base commit, never from the checkout ----------------------------


def _git(repo: pathlib.Path, *args: str) -> str:
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false")
    done = subprocess.run(
        ["git", *identity, "-c", "core.autocrlf=false", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commit(repo: pathlib.Path, files: dict[str, str]) -> str:
    for rel, content in files.items():
        _write(repo, rel, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return _git(repo, "rev-parse", "HEAD")


BUDGETS_SOURCE = (
    '"""A base revision of the budgets module."""\n'
    "OTHER_CONSTANT = 3\n"
    "CHAPTER_BYTE_BUDGETS: dict[str, int] = {\n"
    "    # 900 -> 1000: a raise note\n"
    f'    "{CHAPTER}": 1000,\n'
    "}\n"
)


@pytest.mark.serial
def test_base_facts_come_from_the_base_commit_in_bytes(tmp_path):
    _git(tmp_path, "init", "-q")
    text = "# Example\n\ncaf\u00e9 na\u00efve\n"  # two-byte characters: a size in characters would be short
    first = _commit(tmp_path, {CHAPTER: text, "docs/guide/outside-the-books.md": "x\n"})
    assert base_facts(tmp_path, first) == ({CHAPTER: len(text.encode("utf-8"))}, set(), {})
    assert len(text.encode("utf-8")) > len(text)

    second = _commit(tmp_path, {GRANTS_TREE + "/" + GRANT: "500\n" + REASON, BUDGETS_MODULE: BUDGETS_SOURCE})
    # The checkout moves on; the base commit's facts must not.
    _write(tmp_path, CHAPTER, text + "appended after the base\n")
    _write(tmp_path, GRANTS_TREE + "/development/02-example/uncommitted.grant", "9\n" + REASON)
    assert base_facts(tmp_path, second) == ({CHAPTER: len(text.encode("utf-8"))}, {GRANT}, {CHAPTER: 1000})
    assert base_facts(tmp_path, first) == ({CHAPTER: len(text.encode("utf-8"))}, set(), {})


@pytest.mark.serial
def test_growth_base_is_the_event_base_and_nothing_else(tmp_path):
    _git(tmp_path, "init", "-q")
    first = _commit(tmp_path, {CHAPTER: "one\n"})
    _commit(tmp_path, {CHAPTER: "one\ntwo\n"})
    _commit(tmp_path, {CHAPTER: "one\ntwo\nthree\n"})
    assert growth_base(tmp_path, first) == first
    assert growth_base(tmp_path, f" {first[:12]} ") == first
    # No event base (a local or manual run), all zeros (a tag or new-branch push) and a commit that
    # is not in the repository all leave the rule unapplied; HEAD's parent is never substituted.
    for absent in (None, "", "   ", "0" * 40, "f" * 40):
        assert growth_base(tmp_path, absent) == "", absent


@pytest.mark.parametrize(("environ", "applies"), [
    ({}, True),  # a local run with an explicit base
    ({"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/7/merge"}, True),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ouroboros"}, True),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/ouroboros-stable"}, False),
    ({"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}, False),
])
def test_the_growth_rule_measures_one_change_not_a_release_range(environ, applies):
    assert measures_one_change(environ) is applies
