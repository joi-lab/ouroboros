"""The anchored edit compiler behind every authored ``knowledge_write(mode="edit")``.

Automatic Light nominations no longer write knowledge; the acting mind revises a note
by one exact ``old_str`` replacement through this compiler (``write_knowledge_note``),
so an edit is atomic and an anchor is unique and never overlaps another.
"""

import pytest

from ouroboros import knowledge as k


def test_list_compiler_is_atomic_and_overlap_is_never_a_unique_anchor():
    with pytest.raises(ValueError, match="exactly once"):
        k.compile_anchored_edits("aaa", [("aa", "b")], "old_text")
    with pytest.raises(ValueError, match="must not overlap"):
        k.compile_anchored_edits("one two three", [("one two", "1 2"), ("two three", "2 3")])
    with pytest.raises(ValueError, match="edit 2"):
        k.compile_anchored_edits("one two", [("one", "1"), ("absent", "x")])
    # Adjacent spans are distinct; every anchor is found in the ORIGINAL text.
    assert k.compile_anchored_edits("one two", [("two", "one"), ("one ", "")]) == "one"
