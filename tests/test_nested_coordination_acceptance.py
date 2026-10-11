"""Production-shaped deterministic acceptance for host-visible depth-three trees."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import task_tree_ledger
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.contracts.task_contract import build_task_contract
from ouroboros.headless import copy_child_task_result
from ouroboros.outcome_receipt_store import merge_verification_receipts
from ouroboros.outcomes import latest_unreconciled_failed_receipt
from ouroboros.loop import (
    _task_acceptance_eligible,
    _task_acceptance_subtree_snapshot,
)
from ouroboros.review_evidence import build_task_acceptance_evidence
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    load_task_result,
    write_task_result,
)
from ouroboros.tools import control
from ouroboros.tools.registry import ToolContext
from supervisor import (
    events,
    queue as queue_module,
    state as state_module,
    workers,
)


def _acceptance_binding(seed: str) -> dict[str, str]:
    from ouroboros.review_substrate import review_binding_hash

    components = {
        "candidate_hash": chr(ord(seed) + 1) * 64,
        "evidence_revision": chr(ord(seed) + 2) * 64,
        "fence_hash": chr(ord(seed) + 3) * 64,
    }
    return {**components, "binding_hash": review_binding_hash(**components)}


def test_root_acceptance_review_claims_share_one_atomic_exact_binding_wallet(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from ouroboros.task_results import (
        claim_task_acceptance_review_cycle,
        load_task_acceptance_review_state,
    )

    contract = build_task_contract({
        "budget_profile": {"max_improvement_passes": 1},
    })
    write_task_result(
        tmp_path, "root-wallet", STATUS_RUNNING, root_task_id="root-wallet",
        task_contract=contract,
    )
    first_binding = _acceptance_binding("1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda _index: claim_task_acceptance_review_cycle(
                tmp_path,
                "root-wallet",
                first_binding,
                claimed_by_task_id="root-wallet",
            ),
            range(2),
        ))
    assert sorted(row["status"] for row in outcomes) == ["claimed", "unknown"]
    state = load_task_acceptance_review_state(tmp_path, "root-wallet")
    assert list(state["claims_by_binding"]) == [first_binding["binding_hash"]]
    result_path = tmp_path / "task_results" / "root-wallet.json"
    before_duplicate = result_path.read_bytes()
    duplicate = claim_task_acceptance_review_cycle(
        tmp_path,
        "root-wallet",
        first_binding,
        claimed_by_task_id="root-wallet",
    )
    assert duplicate["status"] == "unknown"
    assert duplicate["reason"] == "binding_dispatch_already_claimed"
    assert result_path.read_bytes() == before_duplicate

    second = claim_task_acceptance_review_cycle(
        tmp_path,
        "root-wallet",
        _acceptance_binding("5"),
        claimed_by_task_id="root-wallet",
    )
    before_exhausted = result_path.read_bytes()
    exhausted = claim_task_acceptance_review_cycle(
        tmp_path,
        "root-wallet",
        _acceptance_binding("a"),
        claimed_by_task_id="root-wallet",
    )
    assert second["status"] == "claimed"
    assert second["cycles_paid"] == 2
    assert result_path.read_bytes() == before_exhausted
    assert exhausted == {
        "status": "unavailable",
        "reason": "review_cycles_exhausted",
        "binding_hash": _acceptance_binding("a")["binding_hash"],
        # A pre-A-material binding carries no paid identity: the receipt reports it
        # empty and the binding hash keeps being the claim key, as before.
        "paid_identity": "",
        "cycles_paid": 2,
        "max_cycles": 2,
        "remaining_cycles": 0,
    }


def test_acceptance_review_wallet_rejects_present_empty_or_tampered_authority(tmp_path):
    from ouroboros.task_results import (
        TASK_ACCEPTANCE_REVIEW_STATE_KEY,
        claim_task_acceptance_review_cycle,
        load_task_acceptance_review_state,
    )

    path = tmp_path / "task_results" / "root-invalid.json"
    write_task_result(
        tmp_path,
        "root-invalid",
        STATUS_RUNNING,
        root_task_id="root-invalid",
        task_contract=build_task_contract({}),
        **{TASK_ACCEPTANCE_REVIEW_STATE_KEY: {}},
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="TASK_ACCEPTANCE_REVIEW_STATE_INVALID"):
        load_task_acceptance_review_state(tmp_path, "root-invalid")
    with pytest.raises(ValueError, match="TASK_ACCEPTANCE_REVIEW_STATE_INVALID"):
        claim_task_acceptance_review_cycle(
            tmp_path,
            "root-invalid",
            _acceptance_binding("1"),
            claimed_by_task_id="root-invalid",
        )
    assert path.read_bytes() == before

    write_task_result(
        tmp_path,
        "root-tampered",
        STATUS_RUNNING,
        root_task_id="root-tampered",
        task_contract=build_task_contract({}),
    )
    tampered = _acceptance_binding("5")
    tampered["binding_hash"] = "f" * 64
    tampered_path = tmp_path / "task_results" / "root-tampered.json"
    before = tampered_path.read_bytes()
    with pytest.raises(ValueError, match="binding digest mismatch"):
        claim_task_acceptance_review_cycle(
            tmp_path,
            "root-tampered",
            tampered,
            claimed_by_task_id="root-tampered",
        )
    assert tampered_path.read_bytes() == before


def test_acceptance_review_wallet_cap_and_root_initialization_are_atomic(
    tmp_path, monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor

    import ouroboros.task_results as task_results

    with pytest.raises(ValueError, match="TASK_ACCEPTANCE_REVIEW_STATE_UNKNOWN"):
        task_results.claim_task_acceptance_review_cycle(
            tmp_path,
            "missing-root",
            _acceptance_binding("1"),
            claimed_by_task_id="child",
        )
    assert not (tmp_path / "task_results" / "missing-root.json").exists()

    with pytest.raises(ValueError, match="TASK_ACCEPTANCE_REVIEW_STATE_UNKNOWN"):
        task_results.claim_task_acceptance_review_cycle(
            tmp_path, "new-root", _acceptance_binding("1"),
            claimed_by_task_id="new-root",
        )
    assert load_task_result(tmp_path, "new-root") is None

    cap_contract = build_task_contract({
        "budget_profile": {"max_improvement_passes": 0},
    })
    write_task_result(
        tmp_path,
        "cap-root",
        STATUS_RUNNING,
        root_task_id="cap-root",
        task_contract=cap_contract,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda binding: task_results.claim_task_acceptance_review_cycle(
                tmp_path,
                "cap-root",
                binding,
                claimed_by_task_id="cap-root",
            ),
            (_acceptance_binding("1"), _acceptance_binding("5")),
        ))
    assert sorted(row["status"] for row in outcomes) == ["claimed", "unavailable"]

    write_task_result(
        tmp_path, "malformed-cap-root", STATUS_RUNNING,
        root_task_id="malformed-cap-root", task_contract=None,
    )
    with pytest.raises(ValueError, match="root contract is malformed"):
        task_results.claim_task_acceptance_review_cycle(
            tmp_path, "malformed-cap-root", _acceptance_binding("a"),
            claimed_by_task_id="child",
        )

    write_task_result(
        tmp_path, "racy-root", STATUS_RUNNING, root_task_id="racy-root",
        task_contract=build_task_contract({}),
    )
    original_update = task_results.update_json_locked

    def remove_before_locked_read(path, mutator, **kwargs):
        path.unlink()
        return original_update(path, mutator, **kwargs)

    monkeypatch.setattr(task_results, "update_json_locked", remove_before_locked_read)
    with pytest.raises(ValueError, match="TASK_ACCEPTANCE_REVIEW_STATE_UNKNOWN"):
        task_results.claim_task_acceptance_review_cycle(
            tmp_path,
            "racy-root",
            _acceptance_binding("a"),
            claimed_by_task_id="child",
        )
    assert not (tmp_path / "task_results" / "racy-root.json").exists()


def test_descendant_cannot_initialize_missing_root_review_authority(tmp_path, monkeypatch):
    from ouroboros.task_pacing import project_task_acceptance_review_capacity

    ctx = SimpleNamespace(
        task_id="child",
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_metadata={
            "root_task_id": "missing-root",
            "parent_task_id": "missing-root",
            "delegation_role": "subagent",
            "budget_drive_root": str(tmp_path),
        },
    )
    projection = project_task_acceptance_review_capacity(ctx)
    assert projection["state"] == "unknown"
    assert projection["claimed_cycles"] is None
    assert not (tmp_path / "task_results" / "missing-root.json").exists()


def test_root_review_capacity_uses_existing_explicit_pass_semantics(tmp_path, monkeypatch):
    from ouroboros.task_pacing import project_task_acceptance_review_capacity

    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "2")
    contract = build_task_contract({
        "budget_profile": {"max_improvement_passes": 4},
    })
    write_task_result(
        tmp_path,
        "root-cap",
        STATUS_RUNNING,
        root_task_id="root-cap",
        delegation_role="root",
        task_contract=contract,
    )
    ctx = SimpleNamespace(
        task_id="root-cap",
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_contract=contract,
        task_metadata={
            "root_task_id": "root-cap",
            "delegation_role": "root",
            "budget_drive_root": str(tmp_path),
            "task_contract": contract,
        },
    )
    projection = project_task_acceptance_review_capacity(ctx)
    assert projection["state"] == "available"
    assert projection["cap_cycles"] == 5
    assert projection["claimed_cycles"] == 0
    assert projection["remaining_cycles"] == 5


def test_depth3_control_plane_reaches_root_acceptance(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()

    settings = {
        "OUROBOROS_SUBAGENTS": json.dumps(
            {
                "enabled": True,
                "items": [
                    {
                        "subagent_id": "api-depth",
                        "name": "Depth actor",
                        "recommended_use": (
                            "Deterministic nested control-plane fixture."
                        ),
                        "route": {
                            "kind": "api_model",
                            "target_id": "openai/gpt-5.6-sol",
                        },
                        "effort": "high",
                    }
                ],
            }
        )
    }

    pending = []
    running = {}
    delivered = []
    emitted = []

    class WorkerQueue:
        def put(self, task):
            # Freeze what the worker really received. A shallow alias could make
            # later mutation hide an assignment/copy-back regression.
            delivered.append(copy.deepcopy(task))

    worker_map = {
        worker_id: SimpleNamespace(
            wid=worker_id,
            busy_task_id=None,
            reaping=False,
            in_q=WorkerQueue(),
        )
        for worker_id in (1, 2, 3)
    }

    class SupervisorContext:
        DRIVE_ROOT = tmp_path
        PENDING = pending
        RUNNING = running
        WORKERS = worker_map

        def load_state(self):
            return {}

        def enqueue_task(self, task):
            return queue_module.enqueue_task(task)

        def persist_queue_snapshot(self, reason=""):
            return queue_module.persist_queue_snapshot(reason=reason)

        def send_with_budget(self, *_args, **_kwargs):
            return None

    supervisor_context = SupervisorContext()

    class ImmediateEventQueue:
        def put_nowait(self, event):
            # Preserve the real schedule_subagent event construction. Only the
            # asynchronous transport is collapsed into this deterministic call.
            emitted.append(copy.deepcopy(event))
            events._handle_schedule_task(event, supervisor_context)

    event_queue = ImmediateEventQueue()

    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "3")
    monkeypatch.setenv("OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT", "6")
    monkeypatch.setattr(control, "load_settings", lambda: settings)
    monkeypatch.setattr(task_tree_ledger, "DATA_DIR", tmp_path)

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", running)
    monkeypatch.setattr(workers, "WORKERS", worker_map)
    monkeypatch.setattr(workers, "load_state", lambda: {})

    monkeypatch.setattr(
        state_module,
        "budget_remaining",
        lambda *_args, **_kwargs: 100.0,
    )
    for key, value in {"DRIVE_ROOT": tmp_path, "PENDING": pending, "RUNNING": running,
                       "QUEUE_SNAPSHOT_PATH": tmp_path / "state/queue_snapshot.json",
                       "QUEUE_SEQ_COUNTER_REF": {"value": 0}, "ADMISSION_RESERVATIONS": {},
                       "ACCEPTANCE_FENCES": {}}.items():
        monkeypatch.setattr(queue_module, key, value)
    monkeypatch.setattr(queue_module, "BUDGET_ROOT_FENCES", {})

    root_contract = build_task_contract(
        {
            "delegation_budget": {
                "depth_remaining": 3,
            }
        }
    )
    write_task_result(
        tmp_path,
        "root",
        STATUS_RUNNING,
        root_task_id="root",
        delegation_role="root",
        task_contract=root_contract,
    )

    def actor_context(
        task_id,
        depth,
        task_contract,
        drive_root,
        task=None,
    ):
        ctx = ToolContext(
            repo_dir=repo,
            drive_root=Path(drive_root),
            budget_drive_root=str(tmp_path),
        )
        ctx.task_id = task_id
        ctx.task_depth = depth
        ctx.event_queue = event_queue
        ctx.task_contract = task_contract

        if task is not None:
            ctx.task_constraint = normalize_task_constraint(
                task.get("task_constraint")
            )

        ctx.task_metadata = {
            "root_task_id": "root",
            "parent_task_id": str(
                (task or {}).get("parent_task_id") or ""
            ),
            "delegation_role": (
                "root" if depth == 0 else "subagent"
            ),
            "budget_drive_root": str(tmp_path),
            "task_contract": task_contract,
            "configured_subagent": copy.deepcopy(
                (task or {}).get("configured_subagent") or {}
            ),
        }
        return ctx

    def schedule_and_assign(
        parent_id,
        parent_depth,
        parent_contract,
        parent_drive,
        parent_task=None,
    ):
        previous_event_count = len(emitted)
        previous_delivery_count = len(delivered)

        result = control._schedule_task(
            actor_context(
                parent_id,
                parent_depth,
                parent_contract,
                parent_drive,
                parent_task,
            ),
            subagent_id="api-depth",
            objective=f"depth-{parent_depth + 1}",
            expected_output="typed handoff",
            memory_mode="empty",
        )

        assert "Subagent request queued" in result
        assert len(emitted) == previous_event_count + 1

        task_id = emitted[-1]["task_id"]
        admitted = load_task_result(tmp_path, task_id)

        assert admitted["status"] == STATUS_SCHEDULED
        assert (
            admitted["depth_provenance"]["achieved_depth"]
            is None
        )

        workers.assign_tasks()

        assert len(delivered) == previous_delivery_count + 1
        assigned = delivered[-1]
        assert assigned["id"] == task_id
        return assigned

    l1 = schedule_and_assign(
        "root",
        0,
        root_contract,
        tmp_path,
    )
    l2 = schedule_and_assign(
        l1["id"],
        1,
        l1["task_contract"],
        l1["drive_root"],
        l1,
    )
    l3 = schedule_and_assign(
        l2["id"],
        2,
        l2["task_contract"],
        l2["drive_root"],
        l2,
    )

    assert [task["depth"] for task in (l1, l2, l3)] == [1, 2, 3]
    assert [
        task["parent_task_id"] for task in (l1, l2, l3)
    ] == [
        "root",
        l1["id"],
        l2["id"],
    ]
    assert {
        task["root_task_id"] for task in (l1, l2, l3)
    } == {"root"}

    for depth, task in enumerate((l1, l2, l3), start=1):
        expected_provenance = {
            "requested_depth": 3,
            "permitted_depth": 3,
            "attempted_depth": depth,
            "achieved_depth": depth,
        }

        assert task["depth_provenance"] == expected_provenance
        assert (
            task["task_contract"]["delegation_budget"][
                "depth_provenance"
            ]
            == expected_provenance
        )
        assert (
            task["task_contract"]["delegation_budget"][
                "depth_remaining"
            ]
            == 3 - depth
        )

        canonical = load_task_result(tmp_path, task["id"])
        assert canonical["status"] == STATUS_RUNNING
        assert canonical["depth_provenance"] == expected_provenance
        assert (
            canonical["task_contract"]["delegation_budget"][
                "depth_provenance"
            ]
            == expected_provenance
        )

    assert (
        l3["task_contract"]["delegation_budget"]["may_delegate"]
        is False
    )

    def terminal_copy(task):
        write_task_result(
            Path(task["drive_root"]),
            task["id"],
            STATUS_COMPLETED,
            parent_task_id=task["parent_task_id"],
            root_task_id="root",
            delegation_role="subagent",
            task_contract=task["task_contract"],
            depth_provenance=task["depth_provenance"],
            result=f"done-{task['id']}",
        )

        assert copy_child_task_result(tmp_path, task) is not None

        canonical = load_task_result(tmp_path, task["id"])
        assert canonical["status"] == STATUS_COMPLETED
        assert canonical["depth_provenance"] == (
            canonical["task_contract"]["delegation_budget"][
                "depth_provenance"
            ]
        )

    terminal_copy(l3)

    root_context = actor_context(
        "root",
        0,
        root_contract,
        tmp_path,
    )
    quiescent, early_rows = _task_acceptance_subtree_snapshot(
        root_context,
        tmp_path,
        "root",
    )

    assert quiescent is False
    assert {
        row["task_id"]: row["status"]
        for row in early_rows
    } == {
        l1["id"]: STATUS_RUNNING,
        l2["id"]: STATUS_RUNNING,
        l3["id"]: STATUS_COMPLETED,
    }

    terminal_copy(l2)
    terminal_copy(l1)

    # The queue-owned acceptance fence is a separate liveness authority. Even
    # terminal task-result replicas cannot prove quiescence while the supervisor
    # still reports physical descendants as running.
    root_context._task_acceptance_queue_descendants = [
        {"task_id": task["id"], "status": STATUS_RUNNING}
        for task in (l1, l2, l3)
    ]
    quiescent, queue_live_rows = _task_acceptance_subtree_snapshot(
        root_context,
        tmp_path,
        "root",
    )
    assert quiescent is False
    assert sum(row.get("source") == "supervisor_queue" for row in queue_live_rows) == 3

    running.clear()
    for worker in worker_map.values():
        worker.busy_task_id = None
    root_context._task_acceptance_queue_descendants = []

    quiescent, terminal_rows = _task_acceptance_subtree_snapshot(
        root_context,
        tmp_path,
        "root",
    )

    assert quiescent is True
    assert {
        row["task_id"] for row in terminal_rows
    } == {
        l1["id"],
        l2["id"],
        l3["id"],
    }
    assert all(
        row["status"] == STATUS_COMPLETED
        for row in terminal_rows
    )
    assert all(
        len(row["child_result_sha256"]) == 64
        for row in terminal_rows
    )
    assert sorted(
        row["depth_provenance"]["achieved_depth"] for row in terminal_rows
    ) == [1, 2, 3]

    # Keep this test independent from the checkout's own dirty/staged diff.
    # The acceptance packet itself remains the real production builder.
    import ouroboros.review_evidence as review_evidence

    monkeypatch.setattr(
        review_evidence,
        "collect_turn_diff",
        lambda *_args, **_kwargs: "",
    )

    packet = build_task_acceptance_evidence(
        root_context,
        drive_root=tmp_path,
        task_id="root",
        canonical_subject="root synthesis",
        subtree_statuses=terminal_rows,
    )

    assert packet["terminal_subtree_statuses"] == terminal_rows
    assert (
        packet["__provenance__"]["terminal_subtree_statuses"]
        == "host_attested"
    )
    assert packet["depth_summary"] == {
        "requested_depth": 3,
        "permitted_depth": 3,
        "attempted_depth": 3,
        "achieved_depth": 3,
        "status": "achieved",
        "host_visible_only": True,
    }
    assert packet["__provenance__"]["depth_summary"] == "host_attested"

    assert _task_acceptance_eligible(
        "required",
        {},
        False,
        is_root_task=False,
    ) == (
        False,
        "skipped_child_advisory",
    )
    assert _task_acceptance_eligible(
        "required",
        {},
        False,
        is_root_task=True,
    )[0] is True


def test_depth3_waits_for_worker_slot_then_uses_active_cap_reservation(
    tmp_path, monkeypatch,
):
    delivered = []

    class WorkerQueue:
        def put(self, task):
            delivered.append(copy.deepcopy(task))

    # Root + two ancestors + unrelated work occupy a four-worker install.
    worker_map = {
        idx: SimpleNamespace(
            wid=idx,
            busy_task_id=task_id,
            reaping=False,
            in_q=WorkerQueue(),
        )
        for idx, task_id in enumerate(("root", "l1", "l2", "other"))
    }
    running = {
        "root": {"task": {
            "id": "root", "root_task_id": "root", "delegation_role": "root",
        }},
        "l1": {"task": {
            "id": "l1", "root_task_id": "root",
            "parent_task_id": "root", "delegation_role": "subagent",
        }},
        "l2": {"task": {
            "id": "l2", "root_task_id": "root",
            "parent_task_id": "l1", "delegation_role": "subagent",
        }},
        "other": {"task": {
            "id": "other", "root_task_id": "other", "delegation_role": "root",
        }},
    }
    provenance = {
        "requested_depth": 3,
        "permitted_depth": 3,
        "attempted_depth": 3,
        "achieved_depth": None,
    }
    contract = build_task_contract({"delegation_budget": {
        "depth_remaining": 0,
        "depth_provenance": provenance,
    }})
    l3 = {
        "id": "l3",
        "type": "task",
        "chat_id": 0,
        "description": "depth three",
        "objective": "depth three",
        "expected_output": "typed result",
        "depth": 3,
        "root_task_id": "root",
        "parent_task_id": "l2",
        "delegation_role": "subagent",
        "drive_root": str(tmp_path),
        "child_drive_root": str(tmp_path),
        "budget_drive_root": str(tmp_path),
        "task_contract": contract,
        "depth_provenance": provenance,
    }
    pending = [l3]
    write_task_result(
        tmp_path,
        "l3",
        STATUS_SCHEDULED,
        parent_task_id="l2",
        root_task_id="root",
        delegation_role="subagent",
        task_contract=contract,
        depth_provenance=provenance,
        result="Subagent accepted and scheduled.",
    )

    monkeypatch.setenv("OUROBOROS_MAX_WORKERS", "4")
    monkeypatch.setenv("OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT", "2")
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "3")
    monkeypatch.setattr(workers, "MAX_WORKERS", 4)
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", pending)
    monkeypatch.setattr(workers, "RUNNING", running)
    monkeypatch.setattr(workers, "WORKERS", worker_map)
    monkeypatch.setattr(workers, "load_state", lambda: {})
    monkeypatch.setattr(
        state_module, "budget_remaining", lambda *_args, **_kwargs: 100.0,
    )
    monkeypatch.setattr(queue_module, "PENDING", pending)
    monkeypatch.setattr(queue_module, "RUNNING", running)
    monkeypatch.setattr(queue_module, "BUDGET_ROOT_FENCES", {})
    monkeypatch.setattr(queue_module, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_module, "QUEUE_SNAPSHOT_PATH", tmp_path / "state/queue_snapshot.json")

    # Saturation preserves attempted-but-not-achieved evidence.
    workers.assign_tasks()
    assert [task["id"] for task in pending] == ["l3"]
    assert delivered == []
    scheduled = load_task_result(tmp_path, "l3")
    assert scheduled["status"] == STATUS_SCHEDULED
    assert scheduled["depth_provenance"]["achieved_depth"] is None
    assert queue_module._has_pending_descendant("root") is True
    assert queue_module._has_pending_descendant("l2") is True

    # Only an explicit unrelated-task settlement creates capacity; this test
    # does not promise progress for a permanently saturated ancestry-only pool.
    running.pop("other")
    worker_map[3].busy_task_id = None
    workers.assign_tasks()

    assert pending == []
    assert delivered[-1]["id"] == "l3"
    assert delivered[-1]["depth_provenance"]["achieved_depth"] == 3
    assigned = load_task_result(tmp_path, "l3")
    assert assigned["status"] == STATUS_RUNNING
    assert assigned["depth_provenance"]["achieved_depth"] == 3
    assert worker_map[3].busy_task_id == "l3"


def test_real_over_cap_refusal_reaches_root_acceptance_depth_summary(tmp_path, monkeypatch):
    import ouroboros.review_evidence as review_evidence

    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("OUROBOROS_MAX_SUBAGENT_DEPTH", "2")
    monkeypatch.setattr(
        control,
        "load_settings",
        lambda: {
            "OUROBOROS_SUBAGENTS": json.dumps({
                "enabled": True,
                "items": [{
                    "subagent_id": "api-depth",
                    "name": "Depth actor",
                    "recommended_use": "Typed depth refusal fixture.",
                    "route": {
                        "kind": "api_model",
                        "target_id": "openai/gpt-5.6-sol",
                    },
                    "effort": "high",
                }],
            }),
        },
    )
    root_contract = build_task_contract({
        "delegation_budget": {"depth_remaining": 3},
    })
    parent_provenance = {
        "requested_depth": 3,
        "permitted_depth": 2,
        "attempted_depth": 2,
        "achieved_depth": 2,
    }
    parent_contract = build_task_contract({
        "parent_task_id": "depth-one",
        "root_task_id": "root",
        "delegation_role": "subagent",
        "delegation_budget": {
            "may_delegate": False,
            "depth_remaining": 0,
            "depth_provenance": parent_provenance,
        },
    })
    write_task_result(
        tmp_path,
        "depth-one",
        STATUS_COMPLETED,
        parent_task_id="root",
        root_task_id="root",
        delegation_role="subagent",
        depth_provenance={
            **parent_provenance,
            "attempted_depth": 1,
            "achieved_depth": 1,
        },
        result="depth one complete",
    )
    write_task_result(
        tmp_path,
        "depth-two",
        STATUS_RUNNING,
        parent_task_id="depth-one",
        root_task_id="root",
        delegation_role="subagent",
        task_contract=parent_contract,
        depth_provenance=parent_provenance,
    )
    parent_ctx = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
    )
    parent_ctx.task_id = "depth-two"
    parent_ctx.task_depth = 2
    parent_ctx.task_contract = parent_contract
    parent_ctx.task_metadata = {
        "root_task_id": "root",
        "parent_task_id": "depth-one",
        "delegation_role": "subagent",
        "budget_drive_root": str(tmp_path),
        "task_contract": parent_contract,
    }

    refused = control._schedule_task(
        parent_ctx,
        subagent_id="api-depth",
        objective="attempt depth three",
        expected_output="typed refusal",
    )
    assert "subtask_depth_limit" in refused
    refused_id = refused.split("task_id=", 1)[1].split(";", 1)[0]
    assert load_task_result(tmp_path, refused_id)["status"] == STATUS_FAILED
    write_task_result(
        tmp_path,
        "depth-two",
        STATUS_COMPLETED,
        parent_task_id="depth-one",
        root_task_id="root",
        delegation_role="subagent",
        task_contract=parent_contract,
        depth_provenance=parent_provenance,
        result="depth two complete",
    )

    root_ctx = SimpleNamespace(
        task_id="root",
        drive_root=tmp_path,
        budget_drive_root=str(tmp_path),
        task_contract=root_contract,
        task_metadata={
            "root_task_id": "root",
            "budget_drive_root": str(tmp_path),
            "task_contract": root_contract,
        },
        _task_acceptance_queue_descendants=[],
    )
    quiescent, statuses = _task_acceptance_subtree_snapshot(
        root_ctx, tmp_path, "root",
    )
    assert quiescent is True
    assert {row["task_id"] for row in statuses} == {
        "depth-one", "depth-two", refused_id,
    }
    monkeypatch.setattr(review_evidence, "collect_turn_diff", lambda *_a, **_k: "")
    packet = build_task_acceptance_evidence(
        root_ctx,
        drive_root=tmp_path,
        task_id="root",
        canonical_subject="root depth reduction",
        subtree_statuses=statuses,
    )
    assert packet["depth_summary"] == {
        "requested_depth": 3,
        "permitted_depth": 2,
        "attempted_depth": 3,
        "achieved_depth": 2,
        "status": "capability_reduced",
        "host_visible_only": True,
    }


def test_split_root_receipts_reconcile_in_host_timestamp_order():
    old_pass = {
        "criterion_id": "claim_1", "status": "pass",
        "contract_kind": "explicit_command", "ts": "2026-01-01T00:00:01+00:00",
    }
    new_fail = {
        "criterion_id": "claim_1", "status": "fail",
        "contract_kind": "explicit_command", "ts": "2026-01-01T00:00:02+00:00",
    }
    merged = merge_verification_receipts([new_fail], [old_pass])
    assert merged == [old_pass, new_fail]
    assert latest_unreconciled_failed_receipt(merged) == new_fail

    old_fail = {**new_fail, "ts": "2026-01-01T00:00:01+00:00"}
    new_pass = {**old_pass, "ts": "2026-01-01T00:00:02+00:00"}
    merged = merge_verification_receipts([new_pass], [old_fail])
    assert merged == [old_fail, new_pass]
    assert latest_unreconciled_failed_receipt(merged) is None
