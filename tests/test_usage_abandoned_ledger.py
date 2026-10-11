"""Late receipts retain their exact attempt across validation and compaction."""

from __future__ import annotations

import hashlib
import json

import pytest

from ouroboros import usage_compaction as compaction
from ouroboros import usage_ledger as ledger
from ouroboros._usage_rows import _summary


def _row(attempt_id, state, **facts):
    return {
        "kind": "attempt", "attempt_id": attempt_id, "state": state,
        "ts": "2000-01-01T00:00:00+00:00", "model": "test-model",
        "provider": "test", "task_id": "child", "root_task_id": "root",
        "parent_task_id": "root", "category": "task", "source": "test",
        "reservation_upper_bound_usd": 1.25, "cost_usd": None,
        "cost_final": False, **facts,
    }


def _chain(attempt_id, final):
    rows = [_row(attempt_id, "reserved"), _row(attempt_id, "dispatched")]
    if final in {"unresolved", "abandoned"}:
        rows.append(_row(attempt_id, "unresolved", reason="response lost"))
    if final == "abandoned":
        rows.append(_row(attempt_id, "settled", settle_reason="abandoned"))
    elif final == "settled":
        rows.append(_row(attempt_id, "settled", cost_usd=0.5, cost_final=True))
    elif final == "released":
        rows.append(_row(attempt_id, "released", reason="before_dispatch_failed:not_started"))
    return rows


def _numbered(rows, start=1):
    return [{**row, "seq": seq} for seq, row in enumerate(rows, start)]


def _encoded(rows):
    return b"".join((json.dumps(row, sort_keys=True) + "\n").encode("utf-8") for row in rows)


def _persist(root, rows):
    path = root / ledger.LEDGER_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_encoded(rows))
    return path


@pytest.mark.parametrize("prior", ["unresolved", "abandoned"])
@pytest.mark.parametrize("cost", [0.0, 0.7, None])
def test_one_late_receipt_has_identical_full_and_incremental_validation(prior, cost):
    before = _numbered(_chain("late", prior))
    actual = _numbered([_row("late", "settled", settle_reason="late_receipt",
                            cost_usd=cost, cost_final=cost is not None,
                            prompt_tokens=20, completion_tokens=5)], len(before) + 1)
    ledger._validate_records(before + actual)
    states, late_ids = {}, set()
    ledger._validate_records(before, states=states, late_receipt_ids=late_ids)
    assert late_ids == {"late"}
    ledger._validate_records(actual, start_seq=len(before) + 1,
                             states=states, late_receipt_ids=late_ids)
    assert states == {"late": "settled"}
    assert not late_ids
    duplicate = _numbered(actual, len(before) + 2)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(before + actual + duplicate)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(duplicate, start_seq=len(before) + 2,
                                 states=states, late_receipt_ids=late_ids)


def test_incremental_resume_keeps_then_consumes_abandonment_fact(tmp_path):
    prefix = _numbered(_chain("late", "unresolved"))
    with ledger._locked(tmp_path):
        path = _persist(tmp_path, prefix)
        resume = ledger._ledger_resume_state(tmp_path, prefix)
        abandoned = ledger._append_rows_locked(tmp_path, prefix, [
            _row("late", "settled", settle_reason="abandoned")])
        delta, abandoned_resume = ledger._read_new_records_locked(tmp_path, resume)
        assert delta == abandoned
        assert abandoned_resume.states == {"late": "settled"}
        assert abandoned_resume.late_receipt_ids == {"late"}
        # A cold rebuild must retain the same permission as incremental validation.
        rebuilt = ledger._ledger_resume_state(tmp_path, prefix + abandoned)
        assert rebuilt == abandoned_resume
        actual = ledger._append_rows_locked(tmp_path, prefix + abandoned, [
            _row("late", "settled", settle_reason="late_receipt", cost_usd=.4, cost_final=True)])
        delta, final_resume = ledger._read_new_records_locked(tmp_path, rebuilt)
        assert delta == actual
        assert not final_resume.late_receipt_ids
        assert abandoned_resume.late_receipt_ids == {"late"}  # resume input is not mutated
        before_bytes = path.read_bytes()
        with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
            ledger._append_rows_locked(tmp_path, prefix + abandoned + actual, [
                _row("late", "settled", settle_reason="late_receipt", cost_usd=.9, cost_final=True)])
        assert path.read_bytes() == before_bytes


@pytest.mark.parametrize("prior", ["unresolved", "abandoned", "settled", "released"])
def test_untyped_settlement_cannot_change_a_terminal_attempt(prior):
    rows = _numbered(_chain("terminal", prior))
    update = _numbered([_row("terminal", "settled", cost_usd=.8, cost_final=True)], len(rows) + 1)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(rows + update)


@pytest.mark.parametrize("prior", ["settled", "released"])
def test_late_marker_cannot_change_an_ordinary_terminal(prior):
    rows = _numbered(_chain("terminal", prior))
    update = _numbered([_row("terminal", "settled", settle_reason="late_receipt",
                            cost_usd=.8, cost_final=True)], len(rows) + 1)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(rows + update)


@pytest.mark.parametrize("facts", [
    {"cost_usd": 0.0}, {"cost_usd": 1.25}, {"cost_final": True},
    {"cost_final": None}, {"state": "unresolved"}, {"kind": "subscription_session"},
])
def test_abandoned_marker_cannot_claim_a_price_or_another_kind(facts):
    base = _numbered(_chain("abandoned", "dispatched"))
    invalid = _row("abandoned", "settled", settle_reason="abandoned")
    invalid.update(facts)
    assert not ledger.is_abandoned_settlement(invalid)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="invalid abandoned settlement"):
        ledger._validate_records(base + _numbered([invalid], len(base) + 1))


@pytest.mark.parametrize("prior", ["unresolved", "abandoned"])
def test_positive_never_started_receipt_can_release_once(prior):
    before = _numbered(_chain("late", prior))
    released = _numbered([_row("late", "released", reason="before_dispatch_failed:not_started")], len(before) + 1)
    ledger._validate_records(before + released)
    states, late_ids = {}, set()
    ledger._validate_records(before, states=states, late_receipt_ids=late_ids)
    ledger._validate_records(released, start_seq=len(before) + 1,
                             states=states, late_receipt_ids=late_ids)
    assert states == {"late": "released"}
    assert not late_ids
    untyped = [{**released[0], "reason": "cancelled"}]
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(before + untyped)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(before + released + _numbered(released, len(before) + 2))


def test_legacy_unresolved_cannot_be_retyped_into_a_correctable_attempt():
    legacy = _numbered([_row("legacy", "unresolved", kind="legacy_call")])
    update = _numbered([_row("legacy", "settled", settle_reason="late_receipt",
                            cost_usd=.3, cost_final=True)], 2)
    with pytest.raises(ledger.UsageLedgerCorrupt, match="changed after terminal"):
        ledger._validate_records(legacy + update)


def test_compaction_retains_correctable_chains_until_the_real_receipt(tmp_path):
    raw_rows = [row for i in range(24) for row in _chain(f"closed-{i}", "settled")]
    raw_rows += _chain("unresolved", "unresolved") + _chain("abandoned", "abandoned")
    before = _numbered(raw_rows)
    path = _persist(tmp_path, before)
    before_bytes = path.read_bytes()
    before_summary = _summary(list(ledger._final_rows(before).values()))
    with ledger._locked(tmp_path) as heartbeat:
        receipt = compaction.compact_usage_ledger_locked(tmp_path, heartbeat=heartbeat)
        assert receipt is not None
        compacted = ledger._read_records_locked(tmp_path)
        assert (tmp_path / receipt["archive_rel"]).read_bytes() == before_bytes
        for attempt_id in ("unresolved", "abandoned"):
            retained = [row for row in compacted if row["attempt_id"] == attempt_id]
            original = [row for row in before if row["attempt_id"] == attempt_id]
            assert [{k:v for k,v in row.items() if k not in {"seq", "pre_compaction_seq"}}
                    for row in retained] == [{k:v for k,v in row.items() if k != "seq"}
                                            for row in original]
        assert _summary(list(ledger._final_rows(compacted).values())) == before_summary
        correction = ledger._append_rows_locked(tmp_path, compacted, [
            _row("abandoned", "settled", settle_reason="late_receipt", cost_usd=.6, cost_final=True)])
        assert not ledger.is_abandoned_settlement(correction[0])
        receipt2 = compaction.compact_usage_ledger_locked(tmp_path, heartbeat=heartbeat)
        assert receipt2 is not None
        after = ledger._read_records_locked(tmp_path)
        assert "abandoned" not in ledger._final_rows(after)
        assert "unresolved" in ledger._final_rows(after)
    assert {"abandoned", "unresolved"} <= compaction.archived_attempt_ids(tmp_path)


def test_previously_folded_unknown_groups_remain_valid_without_attempt_recreation(tmp_path):
    archived = _numbered(_chain("historical-unknown", "unresolved"))
    original_bytes = _encoded(archived)
    archive_rel = "archive/usage_ledger/historical.jsonl"
    segment = tmp_path / archive_rel
    segment.parent.mkdir(parents=True)
    segment.write_bytes(original_bytes)
    header = {"kind":"usage_baseline", "attempt_id":"old-baseline", "state":"settled",
              "seq":1, "baseline_id":"old-baseline", "compaction_epoch":1,
              "archive_rel":archive_rel, "source_sha256":hashlib.sha256(original_bytes).hexdigest(),
              "source_size_bytes":len(original_bytes), "source_row_count":3,
              "source_first_seq":1, "source_last_seq":3, "folded_row_count":3,
              "folded_attempt_count":1, "group_count":1, "retained_row_count":0}
    group = _row("old-baseline-group", "unresolved", kind="usage_baseline_group",
                 baseline_id="old-baseline", folded_attempt_count=1,
                 reservation_upper_bound_usd="1.25", seq=2)
    rows = [header, group] + _numbered(_chain("ordinary", "settled"), 3)
    ledger._validate_records(rows)
    path = _persist(tmp_path, rows)
    before = _summary(list(ledger._final_rows(rows).values()))
    with ledger._locked(tmp_path) as heartbeat:
        assert compaction.compact_usage_ledger_locked(tmp_path, heartbeat=heartbeat)
        after = ledger._read_records_locked(tmp_path)
    assert _summary(list(ledger._final_rows(after).values())) == before
    assert "historical-unknown" not in ledger._final_rows(after)
    assert segment.read_bytes() == original_bytes
    assert "historical-unknown" in compaction.archived_attempt_ids(tmp_path)
    assert path.exists()
