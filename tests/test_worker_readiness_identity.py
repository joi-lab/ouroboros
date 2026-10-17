"""Worker readiness identity: which process a ``worker_ready`` row names (owner, 14 Oct 21:09 #1).

What is pinned, on the fake slots and the real seam of tests/test_worker_readiness.py (no child is
ever forked here):

* births are compared only when both the parent and the child measured one, else the pid decides
  and a parent without a birth adopts the child's; mismatching births never open the slot;
* a stale row of a reused pid never names a slot whose birth was measured; without a parent birth
  the newest row for the pid wins (an earlier holder wrote its row before it died);
* the seam (the spawn's readiness thread) ledgers a birthless slot itself, never a measured one
  (tests/test_worker_demand.py pins that the supervisor loop ledgers nothing);
* the idle probe's answer follows the same rule.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ouroboros.utils import append_jsonl
from tests.test_worker_readiness import _READY_ROW, _booting_slot, _FakeProc  # the seam's fake slots
from tests.test_worker_readiness import pool, seam  # noqa: F401 -- the real seam fixtures


@pytest.fixture
def unmeasured_parent(monkeypatch):
    """The parent's own birth read fails (its ledger write raises): the slot keeps no birth."""
    from ouroboros import process_custody

    attempted = []

    def fail(_root, *, pid, **_kwargs):
        attempted.append(pid)
        raise OSError("ps unavailable")

    monkeypatch.setattr(process_custody, "record_process", fail)
    return attempted


@pytest.mark.parametrize("parent,child", [
    ("", "child-birth"), ("test-birth", ""), ("", ""), ("test-birth", "test-birth"),
], ids=["absent_parent_birth", "absent_child_birth", "both_absent", "matching"])
def test_readiness_compares_births_only_when_both_sides_measured_one(
        pool, seam, unmeasured_parent, parent, child):  # noqa: F811
    slot = _booting_slot(pool, 0, 5001)
    slot.process_birth = parent
    append_jsonl(seam.events, {**_READY_ROW, "process_birth": child})

    seam.run({0: slot}, seam.cursor, 1)

    assert seam.killed == [] and seam.respawned == []
    assert pool.lifecycle.worker_ready(slot), "the child's own row opened the slot"
    assert slot.process_birth == (parent or child), "a parent without a birth adopts the child's own"
    assert slot.ready_identity == (5001, parent or child)
    assert unmeasured_parent == ([] if parent else [5001]), "only a birthless slot is ledgered, by its seam"


def test_mismatching_births_never_open_the_slot(pool, seam, unmeasured_parent):  # noqa: F811
    slot = _booting_slot(pool, 0, 5001)
    append_jsonl(seam.events, {**_READY_ROW, "process_birth": "another-process"})

    seam.run({0: slot}, seam.cursor, 1)

    assert not pool.lifecycle.worker_ready(slot) and slot.reaping and slot.process_birth == "test-birth"
    assert seam.killed == [5001] and seam.respawned == [(0, {"ready_attempt": 2})]


@pytest.mark.parametrize("order", ["stale_first", "own_first"])
def test_a_stale_row_of_a_reused_pid_never_names_a_slot_whose_birth_was_measured(pool, seam, order):  # noqa: F811
    slot = _booting_slot(pool, 0, 5001)
    stale = {**_READY_ROW, "process_birth": "earlier-holder"}
    for row in ([stale, _READY_ROW] if order == "stale_first" else [_READY_ROW, stale]):
        append_jsonl(seam.events, row)

    seam.run({0: slot}, seam.cursor, 1)

    assert pool.lifecycle.worker_ready(slot) and slot.ready_identity == (5001, "test-birth")
    assert seam.killed == []


def test_pid_reuse_without_a_parent_birth_adopts_the_newest_row_for_the_pid(
        pool, seam, unmeasured_parent):  # noqa: F811
    """An earlier holder of a reused pid died, and so wrote its row, before this child received it."""
    slot = _booting_slot(pool, 0, 5001)
    slot.process_birth = ""
    append_jsonl(seam.events, {**_READY_ROW, "process_birth": "earlier-holder"})
    append_jsonl(seam.events, {**_READY_ROW, "process_birth": "child-birth"})

    seam.run({0: slot}, seam.cursor, 1)

    assert slot.process_birth == "child-birth" and slot.ready_identity == (5001, "child-birth")
    assert pool.lifecycle.worker_ready(slot)


@pytest.mark.parametrize("slot_birth,event_pid,event_birth,confirmed", [
    ("test-birth", 5001, "test-birth", True),
    ("test-birth", 5001, "", True),
    ("", 5001, "", True),
    ("test-birth", 5001, "another-process", False),
    ("test-birth", 4999, "test-birth", False),
], ids=["matching", "absent_child_birth", "both_absent", "mismatching", "foreign_pid"])
def test_the_idle_probe_answer_compares_births_only_when_both_are_known(
        pool, slot_birth, event_pid, event_birth, confirmed):  # noqa: F811
    lifecycle, workers = pool.lifecycle, pool.workers
    slot = workers.Worker(wid=0, proc=_FakeProc(5001), in_q=MagicMock(), busy_task_id=None,
                          process_birth=slot_birth, ready_identity=(5001, slot_birth), quiescence_token="probe")
    workers.WORKERS[0] = slot

    lifecycle.handle_worker_quiescent(
        {"worker_id": 0, "pid": event_pid, "process_birth": event_birth, "token": "probe"}, workers)

    assert slot.quiescence_confirmed is confirmed
