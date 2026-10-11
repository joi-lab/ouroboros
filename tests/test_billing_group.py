"""Owner Batch4 (2A): whole-work money across Continue, through the REAL ledger.

The original root P, its Continue successor S and S's helper T share ONE
billing group under P's original cap. Every reservation — P's own late review
included — is checked against the same locked snapshot; legacy rows of P join
by P's root id without a rewrite; compaction, the tree-accounting cache the
pacing reads, the review wave and the Resume grant's strict read see the
group; a late charge establishes an overrun it did not prevent.
"""

from __future__ import annotations

import json

import pytest

from ouroboros import usage_accounting as ua


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "1000")
    (root / "state").mkdir(parents=True)
    ua._reset_task_cache_splits()
    return root


def _scope(root, task_id, root_task_id, *, group="", group_limit=None, root_limit=None, **extra):
    return ua.UsageScope(drive_root=root, task_id=task_id, root_task_id=root_task_id, source="test",
                         root_limit_usd=root_limit, billing_group_id=group, billing_group_limit_usd=group_limit,
                         **extra)


def _spend(root, scope, cost, *, bound=None, final=True):
    with ua.usage_scope(scope):
        reservation = ua.reserve_attempt(ua.AttemptRequest(
            model="openai/gpt-5.2", provider="openai", reservation_usd=cost if bound is None else bound))
        ua.mark_dispatched(reservation)
        if cost is not None:
            ua.settle_attempt(reservation, {"prompt_tokens": 1, "completion_tokens": 1},
                              cost_usd=cost, cost_final=final)
        return reservation


P = dict(group="P", group_limit=20.0, root_limit=20.0)
S = dict(group="P", group_limit=20.0)          # a Continue's successor: no fresh cap of its own
T = dict(group="P", group_limit=20.0)          # the successor's helper


def test_every_member_sees_every_other_including_the_original_roots_late_review(data_root):
    _spend(data_root, _scope(data_root, "P", "P", **P), 8.0)
    _spend(data_root, _scope(data_root, "S", "S", **S), 10.0)
    # P's late review ($4) would reach $22: refused under P's OWN scope.
    with pytest.raises(ua.BudgetExceeded, match="whole-work budget exhausted for group P"):
        _spend(data_root, _scope(data_root, "P-review", "P", category="review", **P), 4.0)
    # S's helper T cannot spend the same remainder either.
    _spend(data_root, _scope(data_root, "T", "S", parent_task_id="S", **T), 1.5)
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "S", "S", **S), 1.0)
    projection = ua.usage_projection(data_root, billing_group_id="P")
    assert projection["accounted_usd"] == pytest.approx(19.5) and projection["limit_usd"] == 20.0
    # Genuine ids stay genuine: each row names its own task and root.
    rows = [json.loads(line) for line in (data_root / ua.LEDGER_REL).read_text().splitlines()]
    reserved = {(r["task_id"], r["root_task_id"], r["billing_group_id"]) for r in rows if r["state"] == "reserved"}
    assert {("P", "P", "P"), ("S", "S", "P"), ("T", "S", "P")} <= reserved
    assert ua.usage_projection(data_root, root_task_id="S")["accounted_usd"] == pytest.approx(11.5)


def test_legacy_rows_of_the_original_root_join_the_group_without_a_rewrite(data_root):
    root = ua._drive_root(data_root)
    with ua._locked(root):
        records = ua._read_records_locked(root)
        legacy = {"kind": "attempt", "attempt_id": "legacy-1", "model": "m", "provider": "openai",
                  "task_id": "P", "root_task_id": "P", "parent_task_id": "", "category": "task", "source": "old",
                  "reservation_upper_bound_usd": 15.0, "pricing_known": True, "root_limit_usd": 20.0}
        # Written before the group field existed: no ``billing_group_id`` anywhere.
        ua._append_rows_locked(root, records, [{**legacy, "state": "reserved"}, {**legacy, "state": "dispatched"},
                                               {**legacy, "state": "settled", "cost_usd": 15.0, "cost_final": True}])
    before = (data_root / ua.LEDGER_REL).read_text()
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "S", "S", **S), 6.0)
    assert (data_root / ua.LEDGER_REL).read_text().startswith(before), "history is appended to, never rewritten"
    from ouroboros.usage_admission import original_group_limit

    assert original_group_limit(data_root, "P") == {"limit_usd": 20.0, "source": "ledger_first_row"}


def test_a_late_charge_establishes_an_overrun_it_did_not_prevent(data_root):
    """An unknown-priced reservation is admitted while the known accounting fits
    (the existing policy, unchanged). Its late settlement past the cap is
    recorded as an overrun and bars the next admission."""
    _spend(data_root, _scope(data_root, "P", "P", **P), 18.0)
    with ua.usage_scope(_scope(data_root, "S", "S", **S)):
        unknown = ua.reserve_attempt(ua.AttemptRequest(model="m", provider="openai", force_unknown_reservation=True))
        ua.mark_dispatched(unknown)
        ua.settle_attempt(unknown, {"prompt_tokens": 1, "completion_tokens": 1}, cost_usd=5.0, cost_final=True)
    projection = ua.usage_projection(data_root, billing_group_id="P")
    assert projection["accounted_usd"] == pytest.approx(23.0)
    assert projection["accounted_usd"] > projection["limit_usd"] == 20.0
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "P-post", "P", **P), 0.01)


def test_compaction_keeps_the_group_and_the_pacing_cache_reads_it(data_root, monkeypatch):
    import time

    from ouroboros import usage_compaction
    from ouroboros.loop_budget import _loop_tree_accounting
    from ouroboros.usage_compaction import compact_usage_ledger_locked

    for _ in range(16):
        _spend(data_root, _scope(data_root, "P", "P", **P), 0.5)
    for _ in range(10):
        _spend(data_root, _scope(data_root, "S", "S", **S), 0.5)
    monkeypatch.setattr(usage_compaction, "_fold_clock", lambda: time.time() + 1_000_000)
    before = ua.usage_projection(data_root, billing_group_id="P")["accounted_usd"]
    with ua._locked(ua._drive_root(data_root)) as lock:
        receipt = compact_usage_ledger_locked(data_root, heartbeat=lock)
    assert receipt is not None, "the pass folded the settled rows"
    from ouroboros.usage_admission import original_group_limit
    assert original_group_limit(data_root, "P")["limit_usd"] == 20.0
    baselines = [json.loads(line) for line in (data_root / ua.LEDGER_REL).read_text().splitlines()]
    assert all(row["billing_group_limit_usd"] == 20.0 for row in baselines
               if row.get("kind") == "usage_baseline_group" and row.get("billing_group_id") == "P")
    assert ua.usage_projection(data_root, billing_group_id="P")["accounted_usd"] == pytest.approx(before)
    with ua.usage_scope(_scope(data_root, "S", "S", **S)):
        tree = _loop_tree_accounting(refresh=True, max_age_sec=0.0, strict=True)
    assert tree["accounted_usd"] == pytest.approx(13.0) and tree["root_limit_usd"] == 20.0
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "S", "S", **S), 8.0)


def test_the_review_wave_binds_on_the_group_when_it_is_tighter(data_root):
    _spend(data_root, _scope(data_root, "P", "P", **P), 17.0)
    with ua.usage_scope(_scope(data_root, "S", "S", **S)):
        wave = ua.review_wave_admission(data_root, root_task_id="S", models=["openai/gpt-5.2"],
                                        prompt_chars=4000, max_completion_tokens=1000)
    assert wave["binding_axis"] == "group" and wave["remaining_usd"] == pytest.approx(3.0)
    assert wave["billing_group_id"] == "P"


def test_task_start_fields_for_the_successor_its_helper_and_an_unreadable_root(data_root):
    from ouroboros.task_results import write_task_result
    from ouroboros.usage_admission import UNAVAILABLE_GROUP_PREFIX, task_billing_fields

    successor = {"id": "S", "metadata": {"continuation": {"billing_group_id": "P", "billing_group_limit_usd": 20.0}}}
    assert task_billing_fields(successor, "S", 5.0, data_root) == {
        "root_limit_usd": 5.0, "billing_group_id": "P", "billing_group_limit_usd": 20.0,
        "billing_group_limit_source": None, "billing_group_limit_revision": None}
    write_task_result(data_root, "S", "running", metadata=successor["metadata"])
    helper = {"id": "T", "root_task_id": "S", "parent_task_id": "S"}
    assert task_billing_fields(helper, "S", 5.0, data_root)["billing_group_id"] == "P"
    plain = {"id": "R"}
    assert task_billing_fields(plain, "R", 5.0, data_root) == {
        "root_limit_usd": 5.0, "billing_group_id": "R", "billing_group_limit_usd": 5.0}
    (data_root / "task_results" / "S.json").write_text("{not json", encoding="utf-8")
    fields = task_billing_fields(helper, "S", 5.0, data_root)
    assert fields["billing_group_id"].startswith(UNAVAILABLE_GROUP_PREFIX)
    with pytest.raises(ua.BudgetExceeded, match="billing group authority unavailable"):
        _spend(data_root, _scope(data_root, "T", "S", group=fields["billing_group_id"],
                                 group_limit=fields["billing_group_limit_usd"]), 0.01)


def test_the_continue_admission_carries_the_original_cap_and_the_resume_grant_reads_the_group(tmp_path, monkeypatch):
    from tests._budget_pause_exact_helpers import _install_queue
    from tests.test_owner_continue import NONCE, _interrupted
    from ouroboros.usage_admission import task_accounting_key

    _queue, _state, workers = _install_queue(tmp_path, monkeypatch)
    monkeypatch.setenv("TOTAL_BUDGET", "1000")
    _spend(tmp_path, _scope(tmp_path, "pred-1", "pred-1", group="pred-1", group_limit=20.0, root_limit=20.0), 3.0)
    _interrupted(tmp_path)
    from supervisor.continuation_admission import admit_continuation

    ack = admit_continuation("pred-1", action_nonce=NONCE)
    row = next(task for task in workers.PENDING if task["id"] == ack["successor_task_id"])
    carried = row["metadata"]["continuation"]
    assert carried["billing_group_id"] == "pred-1" and carried["billing_group_limit_usd"] == 20.0
    assert carried["billing_group_limit_source"] == "ledger_first_row"
    assert task_accounting_key(tmp_path, row, row["id"]) == "group:pred-1"


def test_concurrent_siblings_cannot_each_spend_the_same_remainder(data_root):
    """S and its helper T race for the group's last $8 with $6 bounds each: the
    one locked read/check/append admits exactly one, whichever wins."""
    import threading

    _spend(data_root, _scope(data_root, "P", "P", **P), 12.0)
    barrier = threading.Barrier(2)
    outcomes: dict = {}

    def race(task_id, root_task_id, extra):
        scope = _scope(data_root, task_id, root_task_id, **extra, **S)
        barrier.wait()
        try:
            with ua.usage_scope(scope):
                ua.reserve_attempt(ua.AttemptRequest(model="openai/gpt-5.2", provider="openai",
                                                     reservation_usd=6.0))
            outcomes[task_id] = "admitted"
        except ua.BudgetExceeded:
            outcomes[task_id] = "refused"

    threads = [threading.Thread(target=race, args=("S", "S", {})),
               threading.Thread(target=race, args=("T", "S", {"parent_task_id": "S"}))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(outcomes.values()) == ["admitted", "refused"], outcomes
    assert ua.usage_projection(data_root, billing_group_id="P")["accounted_usd"] <= 20.0 + 1e-9


def test_late_session_uses_start_custody_after_root_loss_not_callers_group(data_root):
    from ouroboros.task_results import write_task_result, task_result_path
    from ouroboros.delegate_custody import record_start_requested

    binding = {"billing_group_id": "P", "billing_group_limit_usd": 20.0,
               "billing_group_limit_source": "initial_task_admission", "billing_group_limit_revision": "cap-1"}
    write_task_result(data_root, "S", "running", billing_group=binding)
    assert record_start_requested(data_root, task_id="S", root_task_id="S", invocation_id="late-start",
                                  request={"prompt": "do work"}, idempotency_key="late-start")
    task_result_path(data_root, "S").unlink()
    with ua.usage_scope(_scope(data_root, "unrelated", "unrelated", group="unrelated", group_limit=999.0)):
        ua.record_subscription_session("late-session", drive_root=data_root, route="test", model="m",
                                       task_id="S", root_task_id="S", spend_usd=19.5)
    assert ua.usage_projection(data_root, billing_group_id="P")["accounted_usd"] == pytest.approx(19.5)
    assert ua.usage_projection(data_root, root_task_id="S")["accounted_usd"] == pytest.approx(19.5)
    assert ua.usage_projection(data_root, billing_group_id="unrelated")["accounted_usd"] == 0.0
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "P", "P", **P), 1.0)


def test_sibling_reservations_share_one_atomic_remainder(data_root):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _spend(data_root, _scope(data_root, "P", "P", **P), 19.0)
    ready = threading.Barrier(2)
    def claim(tid):
        with ua.usage_scope(_scope(data_root, tid, tid, **S)):
            ready.wait(timeout=3)
            try:
                ua.reserve_attempt(ua.AttemptRequest(model="m", provider="test", reservation_usd=0.75))
                return "reserved"
            except ua.BudgetExceeded:
                return "refused"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["S", "T"])) == ["refused", "reserved"]
    assert ua.usage_projection(data_root, billing_group_id="P")["accounted_usd"] == pytest.approx(19.75)


def test_root_cap_and_group_cap_are_both_required(data_root):
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "S", "S", group="P", group_limit=20.0, root_limit=2.0), 3.0)
    _spend(data_root, _scope(data_root, "P", "P", **P), 19.0)
    with pytest.raises(ua.BudgetExceeded):
        _spend(data_root, _scope(data_root, "S", "S", group="P", group_limit=20.0, root_limit=5.0), 2.0)


def test_explicit_other_root_cannot_inherit_the_callers_billing_group(data_root):
    from ouroboros.task_results import write_task_result

    write_task_result(data_root, "S", "running", billing_group={
        "billing_group_id": "P", "billing_group_limit_usd": 20.0,
        "billing_group_limit_source": "initial_task_admission", "billing_group_limit_revision": "r1"})
    with ua.usage_scope(_scope(data_root, "foreign", "foreign", group="foreign", group_limit=0.01)):
        reservation = ua.reserve_attempt(ua.AttemptRequest(task_id="helper", root_task_id="S",
                                          model="m", provider="test", reservation_usd=1.0))
    with ua._locked(data_root):
        row = ua._final_rows(ua._read_records_locked(data_root))[reservation.attempt_id]
    assert row["root_task_id"] == "S" and row["task_id"] == "helper"
    assert row["billing_group_id"] == "P"
    assert ua.usage_projection(data_root, billing_group_id="P")["accounted_usd"] == pytest.approx(1.0)
    assert ua.usage_projection(data_root, billing_group_id="foreign")["accounted_usd"] == 0.0
