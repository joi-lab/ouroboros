"""Housekeeping on the two cadences a running server already has (owner 3A: a server that stays
up for weeks retires its leftovers too). The age-only retirers of small planes run on every 300 s
reconcile pass, task trees and service logs behind the startup recovery gate; the working copies'
retirers (worktrees, delegated snapshots, review checkouts) run on every 600 s custody pass with
the supervisor's settlement probe; the memory-journal observation stays once per generation."""
from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros import server_maintenance as sm
from ouroboros.startup_migrations import prepare_startup_state
from ouroboros.task_results import write_task_result

_OLD = time.time() - 30 * 86400  # past the default 7-day GC retention


def _aged(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (_OLD, _OLD))
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    prepare_startup_state(data)
    monkeypatch.setattr(sm, "DATA_DIR", data)
    monkeypatch.setattr(sm, "_STARTUP_RECOVERY_GAPS", [False])
    return data


def _reconcile_pass(monkeypatch):
    """One real reconcile pass, its unrelated steps idle."""
    monkeypatch.setattr(sm, "_periodic_zombie_reconcile", lambda **_kw: None)
    monkeypatch.setattr(sm, "_run_drive_custody_pass", lambda *_a: None)
    monkeypatch.setattr(sm, "_tick_maintenance_child", lambda: None)
    monkeypatch.setattr("supervisor.workspace_preparation.recover_interrupted_preparations", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.review_operation.retire_dead_review_pointers_softly", lambda *a, **k: None)
    monkeypatch.setattr("ouroboros.late_review_marks.replay_owed_late_reviews", lambda *a, **k: None)
    sm._run_periodic_reconcile_sweep([0.0], latch=SimpleNamespace(release=lambda: None))


def test_the_age_only_retirers_run_again_on_a_later_pass_and_trees_wait_out_a_recovery_gap(root, monkeypatch):
    from ouroboros import memory_journal_compaction, subagent_worktrees

    observations = []
    observe = memory_journal_compaction.compact_memory_journal_snapshots
    monkeypatch.setattr(memory_journal_compaction, "compact_memory_journal_snapshots",
                        lambda drive: observations.append(drive) or observe(drive))
    for name in ("prune_orphans", "prune_execution_snapshots"):
        monkeypatch.setattr(subagent_worktrees, name, lambda *a, **k: pytest.fail("working copies on a reconcile pass"))
    write_task_result(root, "settled", "completed", result="done", ts="2000-01-01T00:00:00+00:00")
    tree = _aged(root / "task_trees" / "settled" / "blackboard.jsonl").parent
    service = _aged(root / "services" / "settled" / "service.log")
    first = [_aged(root / "uploads" / "screenshots" / "first.png"),
             _aged(root / "state" / "code_intel" / "stale-root" / "inventory.json", "{}"),
             _aged(root / "state" / "extension_reconcile" / "failed" / "skill.json", "{}"),
             _aged(root / "state" / "delegate_supervision" / "settled.json", "{}")]

    sm._startup_prune_sweeps(preserve_task_sources=True, recovery_report={"errors": ["ownership unknown"]})
    assert observations == [root] and all(path.exists() for path in first), "boot observes and prunes nothing"
    _reconcile_pass(monkeypatch)
    assert not any(path.exists() for path in first)
    assert tree.exists() and service.exists(), "a startup recovery gap keeps task trees and service logs"

    later = [_aged(root / "uploads" / "screenshots" / "later.png"),
             _aged(root / "state" / "delegate_supervision" / "settled.json", "{}")]
    _reconcile_pass(monkeypatch)
    assert not any(path.exists() for path in later), "the second pass of the same process retires again"
    assert tree.exists() and service.exists()
    assert observations == [root], "the journal observation is boot's, never a pass's"

    sm._startup_prune_sweeps()  # the next generation, its recovery complete
    _reconcile_pass(monkeypatch)
    assert not tree.exists() and not service.exists()
    assert observations == [root, root]


def test_a_generation_closed_between_two_reconcile_prunes_starts_no_further_one(root, monkeypatch):
    from ouroboros import code_intelligence, delegate_state_sweep, skill_uninstall_state

    closed, calls = threading.Event(), []

    def first(_root):
        calls.append("delegate")
        closed.set()
        return {}

    monkeypatch.setattr(delegate_state_sweep, "sweep_settled_delegate_state", first)
    monkeypatch.setattr(skill_uninstall_state, "sweep_uninstalled_skill_state", lambda _root: calls.append("skills") or {})
    monkeypatch.setattr(code_intelligence, "prune_stale_code_intel_roots", lambda _root: calls.append("caches") or {})
    sm._run_reconcile_prunes(closed)
    assert calls == ["delegate"], "the generation closed: no further retirer starts"
    sm._run_reconcile_prunes(threading.Event())
    assert calls == ["delegate", "delegate", "skills", "caches"], "an open generation runs every step"


@pytest.fixture
def custody_pass(root, monkeypatch):
    """The 600 s custody pass with its reaping and delegated-run steps idle."""
    from ouroboros import claudexor_daemon, process_custody, process_scope, terminal_projection

    monkeypatch.setattr(terminal_projection, "reconcile_terminal_projections", lambda *_a: None)
    monkeypatch.setattr(claudexor_daemon, "get_owned_daemon", lambda: SimpleNamespace(
        clear_start_failure_latch=lambda *, cleared_by: False))
    monkeypatch.setattr(process_custody, "reap_orphaned_processes", lambda *a, **k: [])
    monkeypatch.setattr(process_scope, "settle_task_scopes", lambda *a, **k: None)
    monkeypatch.setattr(sm, "_installed_skill_names", lambda: None)
    monkeypatch.setattr(sm, "_reconcile_delegated_runs", lambda *a, **k: None)
    monkeypatch.setattr(sm, "_cursor_refresh_settled_terminals", lambda *a, **k: None)
    monkeypatch.setattr(sm, "_prune_review_checkouts", lambda stop_event=None: None)

    def run(stop_event=None):
        latch = threading.Lock()
        latch.acquire()
        sm._run_periodic_custody_sweep(stop_event, latch)
        assert not latch.locked()
    return run


def test_every_custody_pass_prunes_the_working_copies_with_the_supervisor_probe(custody_pass, monkeypatch):
    from ouroboros import delegate_custody, subagent_worktrees
    import supervisor.queue as queue

    def probe(_task_id):
        return False

    calls = []
    monkeypatch.setattr(queue, "task_settlement_liveness", probe)
    monkeypatch.setattr(subagent_worktrees, "prune_orphans",
                        lambda **kw: calls.append(("worktrees", kw)) or {"removed": 0, "kept": 1})
    monkeypatch.setattr(subagent_worktrees, "prune_execution_snapshots",
                        lambda open_ids: calls.append(("snapshots", open_ids)) or {"removed": [], "kept": []})
    monkeypatch.setattr(delegate_custody, "custody_log_unreadable", lambda _root: False)
    monkeypatch.setattr(delegate_custody, "open_snapshot_ids", lambda _root: {"snapshot-open"})

    custody_pass()
    custody_pass()
    assert calls == [("worktrees", {"live": probe}), ("snapshots", {"snapshot-open"})] * 2

    calls.clear()
    closed = threading.Event()
    closed.set()
    custody_pass(closed)
    assert calls == [], "a closed generation retires nothing"
