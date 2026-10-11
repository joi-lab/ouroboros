"""A ``task:<id>`` evidence item is bounded ONCE, by the resolver.

sha256/bytes describe the whole task-result projection, the attached head is cut at
``EVIDENCE_PER_ITEM_BYTES`` with the cut named, and a selector reads the original.
Before, an inner 6,000-character display preview ran first: two results that differed
only past the preview shared one evidence identity (so one plan fingerprint), and a
tail selector read the omission marker instead of the result.
"""
from __future__ import annotations

from ouroboros.task_results import STATUS_COMPLETED, write_task_result
from ouroboros.tools.plan_evidence import EVIDENCE_PER_ITEM_BYTES, resolve_evidence, task_evidence_reader


def _resolve(root, locator):
    return resolve_evidence([locator], active_root=root, allowed_roots=[root],
                            resolve_task=task_evidence_reader(root))


def test_results_differing_only_in_the_tail_have_distinct_identities_and_a_named_cut(tmp_path):
    body = "x" * 50_000
    write_task_result(tmp_path, "task-a", STATUS_COMPLETED, result=body + "ALPHA")
    write_task_result(tmp_path, "task-b", STATUS_COMPLETED, result=body + "BRAVO")

    a, b = _resolve(tmp_path, "task:task-a"), _resolve(tmp_path, "task:task-b")

    [row_a], [row_b] = a["attached"], b["attached"]
    assert row_a["sha256"] != row_b["sha256"]
    assert row_a["bytes"] > EVIDENCE_PER_ITEM_BYTES >= row_a["attached_bytes"]
    assert [o["reason"] for o in a["omissions"]] == [f"truncated_to_{EVIDENCE_PER_ITEM_BYTES}"]
    assert "OMISSION NOTE" not in row_a["text"]


def test_a_tail_selector_reads_the_original_end_not_an_omission_marker(tmp_path):
    write_task_result(tmp_path, "task-c", STATUS_COMPLETED, result="x" * 50_000 + "BRAVO")

    out = _resolve(tmp_path, "task:task-c::tail=40")

    [row] = out["attached"]
    assert row["selector"]["kind"] == "tail"
    assert "BRAVO" in row["text"] and "OMISSION NOTE" not in row["text"]
    assert out["omissions"] == []


def test_a_short_result_is_attached_whole_with_no_omission(tmp_path):
    write_task_result(tmp_path, "task-d", STATUS_COMPLETED, result="a short finding")

    out = _resolve(tmp_path, "task:task-d")

    [row] = out["attached"]
    assert "a short finding" in row["text"]
    assert row["bytes"] == row["attached_bytes"] and out["omissions"] == []


def test_the_head_cut_never_splits_a_multibyte_character(tmp_path):
    write_task_result(tmp_path, "task-e", STATUS_COMPLETED, result="я" * 30_000)

    [row] = _resolve(tmp_path, "task:task-e")["attached"]

    assert len(row["text"].encode("utf-8")) <= EVIDENCE_PER_ITEM_BYTES
    assert row["text"].endswith("я")


def test_a_host_notice_rides_in_the_bounded_head_of_a_long_result(tmp_path):
    write_task_result(tmp_path, "task-f", STATUS_COMPLETED, result="x" * 50_000,
                      terminal_host_notice="First source limitation.")

    [row] = _resolve(tmp_path, "task:task-f")["attached"]

    assert '"terminal_host_notice": "First source limitation."' in row["text"]
    assert row["attached_bytes"] <= EVIDENCE_PER_ITEM_BYTES < row["bytes"]
