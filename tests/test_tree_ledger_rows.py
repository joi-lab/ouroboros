"""Blackboard rows name the task they came from, so a row is addressable
(forward_to_worker / peek_task take the id as written)."""
from __future__ import annotations

from ouroboros.task_tree_ledger import _format_tree_ledger_row


def test_a_row_carries_the_role_and_the_full_task_id():
    row = {"ts": "2026-10-03T10:00:00Z", "kind": "note", "role": "critic",
           "task_id": "0123456789abcdef", "text": "objection"}
    line = _format_tree_ledger_row(row)
    assert "(critic 0123456789abcdef)" in line
    assert "(critic)" not in line


def test_a_row_without_a_role_shows_the_whole_id_not_an_eight_char_prefix():
    row = {"ts": "2026-10-03T10:00:00Z", "kind": "note", "task_id": "0123456789abcdef", "text": "objection"}
    line = _format_tree_ledger_row(row)
    assert "(0123456789abcdef)" in line
    assert "(01234567)" not in line
