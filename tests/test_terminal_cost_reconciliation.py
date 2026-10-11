"""Selection is a no-write proof, never an ownership or accounting authority."""
import contextlib
import json
import logging
import time
from collections import Counter
from types import SimpleNamespace

import pytest

from ouroboros import server_maintenance as maintenance
from ouroboros import task_results
from ouroboros import terminal_cost_reconciliation as reconciliation
from ouroboros import usage_accounting as usage
from supervisor import events_task_done as done
from supervisor import queue


@pytest.fixture
def env(tmp_path, monkeypatch):
    live = set()
    monkeypatch.setattr(reconciliation, "_EQUAL_PROJECTIONS", {})
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "task_has_live_ownership", lambda task_id: task_id in live)
    return SimpleNamespace(root=tmp_path, live=live)


def task(env, tid="root", **fields):
    return task_results.write_task_result(env.root, tid, "cancelled", result="kept answer", **fields)


def attempt(env, tid="root", *, logical=None, cost=0.4, final=True, provider="openai", non_task=False):
    # ``system:*`` probes/one-shots dispatch under the explicit non-task scope
    # their real producers bind (``update_letter``, ``llm_probe``): no task
    # control owner, so no task-result Pause/sleep admission read.
    scope = (usage.usage_scope(usage.UsageScope(drive_root=env.root, task_id=tid, root_task_id=logical or tid,
                                                non_task_operation=True))
             if non_task else contextlib.nullcontext())
    with scope:
        reservation = usage.reserve_attempt(usage.AttemptRequest(
            model="test", provider=provider, drive_root=env.root, task_id=tid,
            root_task_id=logical or tid, reservation_usd=1.0, global_limit_usd=100.0))
        usage.mark_dispatched(reservation)
    if final:
        usage.settle_attempt(reservation, {"prompt_tokens": 7}, cost_usd=cost, cost_final=True)
    return reservation


def key(env, tid="root"):
    return str(env.root.resolve()), tid


def observe(monkeypatch):
    counts = Counter()
    # Count BOTH import sites: the probe and the original refresh's reread.
    read, project, owned = task_results.load_task_result, done._authoritative_terminal_cost, queue.task_has_live_ownership

    def load(root, tid, **kw):
        counts["reads"] += 1
        return read(root, tid, **kw)

    def projection(*args, **kw):
        counts["projections"] += 1
        return project(*args, **kw)

    def ownership(tid):
        counts["ownership"] += 1
        return owned(tid)

    monkeypatch.setattr(task_results, "load_task_result", load)
    monkeypatch.setattr(done, "load_task_result", load)
    monkeypatch.setattr(done, "_authoritative_terminal_cost", projection)
    monkeypatch.setattr(queue, "task_has_live_ownership", ownership)
    return counts


def warm(env):
    maintenance._reconcile_abandoned_usage(env.root)  # write; not evidence of equality
    maintenance._reconcile_abandoned_usage(env.root)  # independent confirmation
    assert key(env) in reconciliation._EQUAL_PROJECTIONS


def test_quiet_history_reads_once_then_skips_bodies_projection_and_ownership(env, monkeypatch):
    for tid in ("a", "b", "c"):
        task(env, tid)
        attempt(env, tid)
        done._refresh_terminal_task_cost(env.root, tid)
    before = {p: p.read_bytes() for folder in ("task_results", "state", "logs")
              for p in (env.root / folder).glob("*.json*")}
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts == {"reads": 3, "projections": 3}
    counts.clear()
    for _ in range(2):
        maintenance._reconcile_abandoned_usage(env.root)
    assert counts == {}
    assert {p: p.read_bytes() for p in before} == before


def test_a_write_is_not_memoized_until_a_later_equal_read(env, monkeypatch):
    task(env)
    attempt(env)
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    assert key(env) not in reconciliation._EQUAL_PROJECTIONS
    assert counts == {"reads": 2, "projections": 2, "ownership": 1}
    counts.clear()
    maintenance._reconcile_abandoned_usage(env.root)
    assert key(env) in reconciliation._EQUAL_PROJECTIONS
    assert counts == {"reads": 1, "projections": 1}
    counts.clear()
    maintenance._reconcile_abandoned_usage(env.root)
    assert not counts


@pytest.mark.parametrize("failure", ["false", "raise"])
def test_failed_or_ambiguous_refresh_is_not_an_equal_proof(env, monkeypatch, failure):
    task(env)
    attempt(env)
    refresh = done._refresh_terminal_task_cost
    calls = []

    def failed(*args, **kwargs):
        calls.append(args[1])
        if failure == "raise":
            raise OSError("unwritable result")
        return False

    monkeypatch.setattr(done, "_refresh_terminal_task_cost", failed)
    for _ in range(2):
        maintenance._reconcile_abandoned_usage(env.root)
        assert not reconciliation._EQUAL_PROJECTIONS
    assert calls == ["root", "root"]
    monkeypatch.setattr(done, "_refresh_terminal_task_cost", refresh)
    warm(env)
    assert task_results.load_task_result(env.root, "root")["accounted_upper_bound_usd"] == 0.4


def test_unrelated_bucket_does_not_invalidate_but_logical_root_bucket_does(env, monkeypatch):
    task(env, root_task_id="old", parent_task_id="", delegation_role="root",
         original_task_id="old", timeout_retry_from="old")
    attempt(env, logical="old")
    warm(env)
    # The physical root is 'root'; its monetary subtree is 'old', not 'root'.
    assert reconciliation._EQUAL_PROJECTIONS[key(env)].scopes == (True, "old")
    counts = observe(monkeypatch)
    attempt(env, "unrelated")  # missing result isn't memoized
    # The measured seam is maintenance: the attempt's own launch admission
    # reads its task's pause row before dispatch, which is not a projection.
    counts.clear()
    maintenance._reconcile_abandoned_usage(env.root)
    assert not counts
    attempt(env, "child", logical="old", cost=0.6)
    counts.clear()
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts["projections"] == 2 and counts["ownership"] == 1
    row = task_results.load_task_result(env.root, "root")
    assert row["accounted_upper_bound_usd"] == 0.4
    assert row["accounted_upper_bound_usd_with_children"] == 1.0


def test_result_lineage_change_invalidates_without_ledger_change(env):
    task(env)
    attempt(env)
    attempt(env, "old-child", logical="old", cost=0.6)
    warm(env)
    ledger = (env.root / usage.LEDGER_REL).read_bytes()
    task(env, root_task_id="old", parent_task_id="", delegation_role="root",
         original_task_id="old", timeout_retry_from="old")
    maintenance._reconcile_abandoned_usage(env.root)
    row = task_results.load_task_result(env.root, "root")
    assert row["accounted_upper_bound_usd_with_children"] == 0.6
    assert (env.root / usage.LEDGER_REL).read_bytes() == ledger


def test_detached_basis_detects_in_place_changes_and_absent_bucket_integrity(env, monkeypatch):
    task(env)
    attempt(env, "child", logical="root")  # root's own bucket is absent
    warm(env)
    breakdown = usage.usage_breakdown(env.root)
    monkeypatch.setattr(usage, "usage_breakdown", lambda *_a, **_kw: breakdown)
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    assert not counts
    # Mutating a nested basis must not mutate the saved comparison witness.
    breakdown["by_root"]["root"]["prompt_cache_ttls"]["fixture"] = 1
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts["projections"] == 1  # no written field changed, a new EQUAL proof
    assert reconciliation._EQUAL_PROJECTIONS[key(env)].buckets[2][1] is not breakdown["by_root"]["root"]
    breakdown["integrity_degraded"] = True
    maintenance._reconcile_abandoned_usage(env.root)
    assert key(env) not in reconciliation._EQUAL_PROJECTIONS
    row = task_results.load_task_result(env.root, "root")
    assert row["ledger_integrity_degraded"] and not row["cost_final"]


@pytest.mark.parametrize("price", ["unresolved", "estimated", "unknown_component"])
def test_equal_nonfinal_projection_is_reused_until_inputs_change(env, monkeypatch, price):
    task(env)
    reservation = attempt(env, final=False)
    if price == "estimated":
        usage.settle_attempt(reservation, cost_usd=1.0, cost_final=False)
    elif price == "unknown_component":
        usage.settle_attempt(reservation, cost_usd=1.0, cost_final=True)
        usage.record_subscription_session("unknown", drive_root=env.root, route="subscription",
                                          task_id="root", root_task_id="root")
    warm(env)
    before = task_results.load_task_result(env.root, "root")
    assert before["accounted_upper_bound_usd"] == 1.0 and not before["cost_final"]
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts == {}, "equality reuses an observation without declaring its cost final"
    if price != "unresolved":
        # Settled estimates are immutable; a genuinely new receipt changes the
        # full bucket even if the amount of the prior estimate remains equal.
        attempt(env, cost=0.0)
    else:
        usage.settle_attempt(reservation, cost_usd=1.0, cost_final=True)
    counts.clear()
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts["projections"] >= 1
    after = task_results.load_task_result(env.root, "root")
    assert after["accounted_upper_bound_usd"] == 1.0
    if price == "unresolved":
        assert after["cost_final"]
        assert after["cost_presentation"] != before["cost_presentation"]
    else:
        assert not after["cost_final"]


def test_equal_unknown_amount_still_has_no_memo(env):
    task(env)
    reservation = usage.reserve_attempt(usage.AttemptRequest(
        model="test", provider="opaque", force_unknown_reservation=True,
        drive_root=env.root, task_id="root", root_task_id="root", global_limit_usd=100.0))
    usage.mark_dispatched(reservation)
    usage.settle_attempt(reservation, cost_usd=None, cost_final=False)
    for _ in range(3):
        maintenance._reconcile_abandoned_usage(env.root)
        assert key(env) not in reconciliation._EQUAL_PROJECTIONS
    row = task_results.load_task_result(env.root, "root")
    assert row["accounted_upper_bound_usd"] is None and not row["cost_final"]


@pytest.mark.parametrize("change", ["deleted", "malformed", "schema", "postwork", "replacement"])
def test_result_stamp_invalidates_and_no_negative_cache_survives_repair(env, monkeypatch, change):
    task(env)
    attempt(env)
    warm(env)
    path = task_results.task_result_path(env.root, "root", create=False)
    good = path.read_bytes()
    if change == "deleted":
        path.unlink()
    elif change == "malformed":
        path.write_bytes(b'{broken')
    elif change == "schema":
        path.write_text('{"_schema_version":99999,"task_id":"root","status":"cancelled"}', encoding="utf-8")
    elif change == "postwork":
        task(env, root_phase_checkpoint={"post_task_synthesis": "running"})
    else:
        replacement = path.with_suffix(".tmp")
        replacement.write_bytes(good)
        replacement.replace(path)
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    if change == "replacement":
        assert counts == {"reads": 1, "projections": 1}
    else:
        assert key(env) not in reconciliation._EQUAL_PROJECTIONS
        path.write_bytes(good)
        maintenance._reconcile_abandoned_usage(env.root)
    assert key(env) in reconciliation._EQUAL_PROJECTIONS


def test_concurrent_result_replacement_cannot_mint_equal_memo(env, monkeypatch):
    task(env)
    attempt(env)
    done._refresh_terminal_task_cost(env.root, "root")
    original = task_results.load_task_result

    def concurrent(root, tid, **kw):
        row = original(root, tid, **kw)
        task(env, root_phase_checkpoint={"post_task_synthesis": "running"})
        return row

    monkeypatch.setattr(task_results, "load_task_result", concurrent)
    maintenance._reconcile_abandoned_usage(env.root)
    assert not reconciliation._EQUAL_PROJECTIONS


def test_unavailable_breakdown_invalidates_old_proof_without_per_owner_fallback(env, monkeypatch):
    task(env)
    attempt(env)
    warm(env)
    breakdown = usage.usage_breakdown
    calls = []

    def unavailable(*_a, **kw):
        calls.append(kw)
        raise OSError("ledger unavailable")

    monkeypatch.setattr(usage, "usage_breakdown", unavailable)
    maintenance._reconcile_abandoned_usage(env.root)
    assert calls == [{}] and not reconciliation._EQUAL_PROJECTIONS
    monkeypatch.setattr(usage, "usage_breakdown", breakdown)
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts == {"reads": 1, "projections": 1}


def test_foreign_live_owner_never_imports_or_reads_foreign_money(env, monkeypatch):
    foreign = env.root / "foreign"
    task(env, budget_drive_root=str(foreign))
    attempt(env)
    env.live.add("root")
    imported = usage.ensure_legacy_imported
    calls = []

    def checked(root):
        calls.append(root)
        assert root != foreign, "foreign accounting must follow eligibility"
        return imported(root)

    monkeypatch.setattr(usage, "ensure_legacy_imported", checked)
    maintenance._reconcile_abandoned_usage(env.root)
    assert calls == [env.root]
    assert not foreign.exists() and not reconciliation._EQUAL_PROJECTIONS
    assert "accounted_upper_bound_usd" not in task_results.load_task_result(env.root, "root")


def test_authority_symlink_rebinding_without_result_change_invalidates_hit(env):
    alias = env.root.parent / f"{env.root.name}-alias"
    try:
        alias.symlink_to(env.root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    task(env, budget_drive_root=str(alias))
    attempt(env)
    warm(env)
    before = task_results.task_result_path(env.root, "root").read_bytes()
    other = SimpleNamespace(root=env.root / "other")
    attempt(other, cost=0.9)
    alias.unlink()
    alias.symlink_to(other.root, target_is_directory=True)
    maintenance._reconcile_abandoned_usage(env.root)
    row = task_results.load_task_result(env.root, "root")
    assert row["accounted_upper_bound_usd"] == 0.9
    assert key(env) not in reconciliation._EQUAL_PROJECTIONS
    assert before != task_results.task_result_path(env.root, "root").read_bytes()


def test_fresh_ownership_after_recovery_prevents_projection_write(env, monkeypatch):
    task(env)
    attempt(env, final=False)
    original = usage.terminalize_abandoned_attempt

    def recovered(*args, **kw):
        result = original(*args, **kw)
        env.live.add("root")
        return result

    monkeypatch.setattr(usage, "terminalize_abandoned_attempt", recovered)
    monkeypatch.setattr(done, "_refresh_terminal_task_cost", lambda *_a, **_kw: pytest.fail("owner revived"))
    maintenance._reconcile_abandoned_usage(env.root)
    assert "accounted_upper_bound_usd" not in task_results.load_task_result(env.root, "root")


def test_retained_remote_recovery_is_independent_of_prior_equal_proof(env, monkeypatch):
    from ouroboros import llm_claudexor

    task(env)
    attempt(env)
    warm(env)
    reservation = attempt(env, provider="claudexor", final=False)
    calls = []
    receipt = [None]

    def recover(root, row, **kw):
        calls.append(row["attempt_id"])
        return receipt[0]

    monkeypatch.setattr(llm_claudexor, "recover_model_attempt", recover)
    maintenance._reconcile_abandoned_usage(env.root)
    assert calls == [reservation.attempt_id]
    assert key(env) not in reconciliation._EQUAL_PROJECTIONS
    # Remote administrative settlement remains recoverable on an unchanged
    # ledger: a retained receipt can arrive without any local accounting append.
    usage.terminalize_abandoned_attempt(reservation, reason="owner_task_terminal")
    maintenance._reconcile_abandoned_usage(env.root)
    maintenance._reconcile_abandoned_usage(env.root)  # separately confirm the rewritten fields
    assert key(env) in reconciliation._EQUAL_PROJECTIONS
    counts = observe(monkeypatch)
    maintenance._reconcile_abandoned_usage(env.root)
    assert counts["projections"] == 0
    receipt[0] = ("settled", {"prompt_tokens": 2}, 0.2, True)
    maintenance._reconcile_abandoned_usage(env.root)
    assert calls == [reservation.attempt_id] * 5
    assert task_results.load_task_result(env.root, "root")["accounted_upper_bound_usd"] == 0.6


def test_memo_is_root_scoped_and_pruned_by_current_attribution(env):
    task(env)
    attempt(env)
    warm(env)
    other = SimpleNamespace(root=env.root / "other")
    task(other)
    attempt(other, cost=0.8)
    warm(other)
    assert len(reconciliation._EQUAL_PROJECTIONS) == 2
    reconciliation._refresh_costs(env.root, set(), {})
    assert key(env) not in reconciliation._EQUAL_PROJECTIONS
    assert key(other) in reconciliation._EQUAL_PROJECTIONS


@pytest.mark.parametrize("compact", [False, True])
def test_settled_system_scopes_own_no_result_but_their_real_root_still_does(env, monkeypatch, caplog, compact):
    from ouroboros import usage_compaction

    task(env)
    attempt(env)
    # Probe/test money settles under non-task ``system:*`` scopes; one such scope
    # still rolls up into a real root that no row of its own names.
    task(env, "owed")
    attempt(env, "system:update_letter", logical="owed", cost=0.6, non_task=True)
    for _ in range(20 if compact else 1):
        for scope in ("system:capability_probe", "system:provider_test", "system:update_letter"):
            attempt(env, scope, logical="owed" if scope == "system:update_letter" else None, cost=0.0,
                    non_task=True)
    task(env, "broken")
    attempt(env, "broken")
    broken = task_results.task_result_path(env.root, "broken")
    broken.write_bytes(b"{broken")
    if compact:
        monkeypatch.setattr(usage_compaction, "_fold_clock", lambda: time.time() + 1_000_000)
        with usage._locked(env.root) as heartbeat:
            assert usage_compaction.compact_usage_ledger_locked(env.root, heartbeat=heartbeat)
            rows = usage._read_records_locked_cached(env.root)
        grouped = {(row.get("task_id"), row.get("root_task_id")) for row in rows
                   if row.get("kind") == "usage_baseline_group"}
        assert {("system:capability_probe", "system:capability_probe"),
                ("system:update_letter", "owed")} <= grouped
    ledger = (env.root / usage.LEDGER_REL).read_bytes()
    caplog.set_level(logging.WARNING, logger=reconciliation.__name__)

    for _ in range(3):
        maintenance._reconcile_abandoned_usage(env.root)

    # A malformed real task keeps its diagnostic; non-task scopes add none.
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING] == [
        "Reconciled task cost refresh failed for broken"] * 3
    assert (env.root / usage.LEDGER_REL).read_bytes() == ledger
    assert sorted(path.name for path in (env.root / "task_results").iterdir()) == [
        "broken.json", "owed.json", "root.json"]
    assert broken.read_bytes() == b"{broken"
    events = (env.root / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(line)["task_id"] for line in events
                  if json.loads(line).get("type") == "task_cost_finalized") == ["owed", "root"]
    assert task_results.load_task_result(env.root, "root")["accounted_upper_bound_usd"] == 0.4
    assert task_results.load_task_result(env.root, "owed")["accounted_upper_bound_usd_with_children"] == 0.6
    assert sorted(reconciliation._EQUAL_PROJECTIONS) == [key(env, "owed"), key(env)]


@pytest.mark.parametrize("owner", ["running", "busy", "direct", "postwork", "retry", "malformed_retry", "review", "control_review"])
def test_real_ownership_predicate_still_fences_projection_and_recovery(env, monkeypatch, owner):
    from ouroboros import review_operation
    from supervisor import queue_transitions, workers

    monkeypatch.setattr(queue, "task_has_live_ownership", queue_transitions.task_has_live_ownership)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "QUEUE_MAX_RETRIES", 3)
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "direct_chat_turn", lambda tid: object() if owner == "direct" else None)
    monkeypatch.setattr(queue_transitions, "post_task_synthesis_in_flight", lambda *_: owner == "postwork")
    task(env)
    reservation = attempt(env, final=False)
    if owner == "running":
        queue.RUNNING["root"] = {"task": {"id": "root"}}
    if owner == "busy":
        workers.WORKERS[1] = SimpleNamespace(busy_task_id="root")
    if owner in {"retry", "malformed_retry"}:
        lineage = dict(root_task_id="root", parent_task_id="", delegation_role="root",
                       original_task_id="root", timeout_retry_from="root", supersedes_task_id="root")
        task(env, "retry", **lineage)
        queue.RUNNING["retry"] = {"task": {"id": "retry", **lineage}}
        # The malformed case is a live retry OUTSIDE the reciprocal chain.
        if owner == "retry":
            task(env, superseded_by="retry", retry_task_id="retry")
    if owner in {"review", "control_review"}:
        primary = {"state": review_operation.OPERATION_DISPATCHED, "controller": {"fixture": "alive"}, "source_ref": {"id": "source"}}
        monkeypatch.setattr(review_operation, "controller_state", lambda _: "alive")
        if owner == "review":
            task(env, **{review_operation.OPERATIONS_FIELD: {"operation": primary}})
        else:
            task(env, "subject", **{review_operation.OPERATIONS_FIELD: {"operation": primary}})
            link = {"control_only": True, "subject_task_id": "subject", "controller": primary["controller"], "source_ref": primary["source_ref"]}
            task(env, **{review_operation.OPERATIONS_FIELD: {"operation": link}})
    assert queue.task_has_live_ownership("root")
    ledger = (env.root / usage.LEDGER_REL).read_bytes()
    original = task_results.task_result_path(env.root, "root").read_bytes()
    maintenance._reconcile_abandoned_usage(env.root)
    assert (env.root / usage.LEDGER_REL).read_bytes() == ledger
    assert task_results.task_result_path(env.root, "root").read_bytes() == original
    usage.settle_attempt(reservation, cost_usd=0.4, cost_final=True)
    maintenance._reconcile_abandoned_usage(env.root)
    assert task_results.task_result_path(env.root, "root").read_bytes() == original


def test_a_live_review_link_changed_elsewhere_is_checked_before_new_money(env, monkeypatch):
    from ouroboros import review_operation
    from supervisor import queue_transitions, workers

    task(env)
    attempt(env)
    primary = {"state": review_operation.OPERATION_CLOSED, "controller": {"fixture": "alive"}, "source_ref": {"id": "s"}}
    task(env, "subject", **{review_operation.OPERATIONS_FIELD: {"operation": primary}})
    link = {"control_only": True, "subject_task_id": "subject", "controller": primary["controller"], "source_ref": primary["source_ref"]}
    task(env, **{review_operation.OPERATIONS_FIELD: {"operation": link}})
    warm(env)
    before = task_results.task_result_path(env.root, "root").read_bytes()
    monkeypatch.setattr(queue, "task_has_live_ownership", queue_transitions.task_has_live_ownership)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "direct_chat_turn", lambda _: None)
    monkeypatch.setattr(queue_transitions, "post_task_synthesis_in_flight", lambda *_: False)
    monkeypatch.setattr(review_operation, "controller_state", lambda _: "alive")
    task(env, "subject", **{review_operation.OPERATIONS_FIELD: {"operation": {**primary, "state": review_operation.OPERATION_DISPATCHED}}})
    # Equal no-write memo does not claim this owner is dead; a changed ledger
    # must invoke the exact control-link predicate despite unchanged owner file.
    maintenance._reconcile_abandoned_usage(env.root)
    attempt(env, cost=0.1)
    maintenance._reconcile_abandoned_usage(env.root)
    assert task_results.task_result_path(env.root, "root").read_bytes() == before
    assert key(env) not in reconciliation._EQUAL_PROJECTIONS


def test_canonical_probe_does_not_fallback_if_authority_moves_during_comparison(env, monkeypatch):
    alias = env.root.parent / f"{env.root.name}-alias"
    try:
        alias.symlink_to(env.root, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    task(env, budget_drive_root=str(alias))
    attempt(env)
    imported = usage.ensure_legacy_imported
    project = done._authoritative_terminal_cost
    foreign = env.root / "foreign"

    def imports(root):
        assert root.resolve() == env.root, "probe must not enter foreign accounting"
        return imported(root)

    def moved(*args, **kw):
        alias.unlink()
        alias.symlink_to(foreign, target_is_directory=True)
        return project(*args, **kw)

    monkeypatch.setattr(usage, "ensure_legacy_imported", imports)
    monkeypatch.setattr(done, "_authoritative_terminal_cost", moved)
    maintenance._reconcile_abandoned_usage(env.root)
    assert not foreign.exists() and not reconciliation._EQUAL_PROJECTIONS
    assert "accounted_upper_bound_usd" not in task_results.load_task_result(env.root, "root")
