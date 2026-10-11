"""GATE ROUND-7 (v6.98.0 phase A) — closure-verification regression tests.

GR7-1  ingress gaps of the GR6-1 live-ownership predicate: (a) the worker-side
       queue-snapshot twin fails OPEN toward liveness — a missing, unreadable,
       or STALE snapshot cannot prove a dead worker, so the agent cancel tool
       mints the intent instead of answering "Nothing to cancel" over a
       burning worker; (b) the HTTP cascade endpoint's 404 pre-check consults
       the same live-ownership predicate — a settled-but-LIVE root proceeds to
       mint + custody, a settled-AND-dead tree keeps the 404 envelope;
GR7-2  verbatim preservation order in the settled-capture kill lane: the
       settled short-circuit runs ABOVE every mutating step (child copy-back,
       artifact finalize, memory export) — the stored terminal row survives
       BYTE-IDENTICAL, and a split-drive child answer never replaces the
       canonical settled answer;
GR7-3  project-deletion wind-down only RE-CHECKS quiescence — it never re-runs
       the cancel pass over a purely settled-lingering set (each re-mint
       delivered a duplicate owner summary); a stuck/new non-settled task
       still gets re-cancelled;
GR7-4  stable delivery identity for disclosure-bearing single-task messages:
       the mutable unreconciled-runs note rides the TEXT but never the id, so
       a replay whose rebuilt note shrank dedups to one delivery;
GR7-5  a quarantined malformed intent row emits its typed disclosure EVENT
       once per row content (in-process memo); the log.error stays per sweep.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
import types

import pytest

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros import cancel_intents as ci
from ouroboros.task_results import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    load_task_result,
    task_result_path,
    write_task_result,
)
from ouroboros.utils import utc_now_iso


class _CaptureQueue:
    def __init__(self):
        self.events = []

    def put(self, evt):
        self.events.append(evt)


@pytest.fixture()
def qenv(tmp_path, monkeypatch):
    import supervisor.queue as q
    from supervisor import task_lifecycle, workers

    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "PENDING", [])
    monkeypatch.setattr(q, "RUNNING", {}, raising=False)
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(workers, "respawn_worker", lambda wid: None, raising=False)
    monkeypatch.setattr(q, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(task_lifecycle, "CANCELLED_ROOT_FENCES", {}, raising=False)
    monkeypatch.setattr(task_lifecycle, "_ACTIVE_CASCADE_FENCES", {}, raising=False)
    return types.SimpleNamespace(q=q, tl=task_lifecycle, workers=workers, drive=tmp_path)


def _install_live_worker(qenv, monkeypatch, task_id, *, wid=0):
    """A worker double whose LIVE process dies only through kill_pid_tree."""
    state = {"alive": True}
    proc = types.SimpleNamespace(
        pid=4242,
        is_alive=lambda: state["alive"],
        terminate=lambda: state.__setitem__("alive", False),
        join=lambda timeout=None: None,
    )
    qenv.workers.WORKERS[wid] = types.SimpleNamespace(
        wid=wid, proc=proc, busy_task_id=task_id, reaping=False,
    )
    monkeypatch.setattr(
        "ouroboros.platform_layer.kill_pid_tree",
        lambda *a, **kw: state.__setitem__("alive", False),
    )
    return state


def _write_snapshot(drive, *, ts=None, running=(), pending=()):
    snap = pathlib.Path(drive) / "state" / "queue_snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    payload = {"running": list(running), "pending": list(pending)}
    if ts is not None:
        payload["ts"] = ts
    snap.write_text(json.dumps(payload), encoding="utf-8")
    return snap


def _tool_ctx(drive, parent="parent-7"):
    return types.SimpleNamespace(
        task_depth=0, pending_events=[], event_queue=_CaptureQueue(),
        drive_root=drive, task_id=parent,
        task_metadata={"root_task_id": parent},
        is_direct_chat=False, is_workspace_mode=lambda: False,
    )


# --------------------------------------------------------------------------
# GR7-1a — the worker-side twin fails OPEN toward liveness
# --------------------------------------------------------------------------


def test_gr7_1a_twin_fails_open_on_missing_invalid_and_stale_snapshots(tmp_path):
    from ouroboros.task_status import task_has_live_queue_ownership

    # MISSING snapshot: cannot prove a dead worker — assume live.
    assert task_has_live_queue_ownership(tmp_path, "t7") is True

    # UNREADABLE/invalid snapshot: same.
    snap = _write_snapshot(tmp_path, ts=utc_now_iso())
    snap.write_text("{not json", encoding="utf-8")
    assert task_has_live_queue_ownership(tmp_path, "t7") is True

    # STALE snapshot (older than the freshness bound): same, even though the
    # task is absent from it.
    _write_snapshot(tmp_path, ts="2020-01-01T00:00:00+00:00")
    assert task_has_live_queue_ownership(tmp_path, "t7") is True

    # A snapshot WITHOUT a ts cannot prove freshness either.
    _write_snapshot(tmp_path)
    assert task_has_live_queue_ownership(tmp_path, "t7") is True

    # Only a FRESH snapshot that positively lacks a RUNNING row answers False.
    _write_snapshot(tmp_path, ts=utc_now_iso())
    assert task_has_live_queue_ownership(tmp_path, "t7") is False
    _write_snapshot(
        tmp_path, ts=utc_now_iso(),
        running=[{"id": "t7", "task": {"id": "t7"}}],
    )
    assert task_has_live_queue_ownership(tmp_path, "t7") is True


def test_gr7_1a_cancel_tool_mints_the_intent_on_missing_and_stale_snapshots(
    tmp_path, monkeypatch,
):
    """The probe: a settled result + a MISSING/STALE snapshot used to answer
    "Nothing to cancel" with NO intent while the worker kept burning."""
    from ouroboros.tools.join_ledger import _cancel_task

    monkeypatch.setattr(
        "ouroboros.tools.control._emit_control_event", lambda *_a, **_k: "live",
    )

    # MISSING snapshot shape.
    write_task_result(tmp_path, "burn-a", STATUS_COMPLETED, result="done")
    reply = _cancel_task(_tool_ctx(tmp_path), "burn-a", reason="stop spending")
    assert "Cancel requested" in reply, reply
    assert ci.active_intent(tmp_path, "burn-a") is not None, (
        "GR7-1a: the intent IS minted when the snapshot cannot prove a dead worker"
    )

    # STALE snapshot shape.
    write_task_result(tmp_path, "burn-b", STATUS_COMPLETED, result="done")
    _write_snapshot(tmp_path, ts="2020-01-01T00:00:00+00:00")
    reply = _cancel_task(_tool_ctx(tmp_path), "burn-b", reason="stop spending")
    assert "Cancel requested" in reply, reply
    assert ci.active_intent(tmp_path, "burn-b") is not None


# --------------------------------------------------------------------------
# GR7-1b — the HTTP cascade 404 pre-check consults live ownership
# --------------------------------------------------------------------------


def _client(tmp_path):
    from ouroboros.gateway.tasks import api_task_cancel

    app = Starlette(routes=[
        Route("/api/tasks/{task_id}/cancel", api_task_cancel, methods=["POST"]),
    ])
    app.state.drive_root = tmp_path
    return TestClient(app)


def test_gr7_1b_cascade_proceeds_for_a_settled_but_live_root(qenv, monkeypatch):
    """A settled root still holding its RUNNING row (worker burning post-task
    cognition) used to 404 BEFORE the intent mint. It must mint + run custody."""
    import ouroboros.gateway.tasks as tasks_mod

    write_task_result(qenv.drive, "root7", STATUS_COMPLETED, chat_id=3,
                      result="settled while the worker burns")
    qenv.q.RUNNING["root7"] = {"task": {"id": "root7", "chat_id": 3,
                                        "root_task_id": "root7"}}
    seen: dict = {}

    def _capture_teardown(task_id):
        seen.update(ci.active_intent(qenv.drive, task_id) or {})
        return True

    monkeypatch.setattr(tasks_mod, "_run_cascade_cancel", _capture_teardown)
    with _client(qenv.drive) as client:
        response = client.post("/api/tasks/root7/cancel", json={"cascade": True})

    assert response.status_code == 200, response.text
    assert seen.get("scope") == "cascade", (
        "GR7-1b: the settled-but-live root proceeds to mint + custody"
    )
    assert seen.get("source") == "http_cascade"


def test_gr7_1b_settled_and_dead_root_keeps_the_404_envelope(qenv):
    write_task_result(qenv.drive, "dead7", STATUS_COMPLETED, chat_id=3,
                      result="settled, worker long gone")
    with _client(qenv.drive) as client:
        response = client.post("/api/tasks/dead7/cancel", json={"cascade": True})
    assert response.status_code == 404
    assert response.json()["error"] == "task not found or not active"
    assert ci.active_intent(qenv.drive, "dead7") is None, (
        "no intent is minted for a genuinely inactive tree"
    )


# --------------------------------------------------------------------------
# GR7-2 — verbatim preservation on the settled-capture kill lane
# --------------------------------------------------------------------------


def test_gr7_2_shared_drive_settled_row_survives_byte_identical(qenv, monkeypatch):
    """The probe: a shared-drive task's settled row gained
    ``headless_child_drive_root`` + a ``memory_export.json`` artifact from the
    pre-short-circuit child-copy/finalize steps."""
    from supervisor import workers

    task_id = "shared7"
    queue = _CaptureQueue()
    monkeypatch.setattr(workers, "get_event_q", lambda: queue, raising=False)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *a, **kw: None)
    state = _install_live_worker(qenv, monkeypatch, task_id)
    qenv.q.RUNNING[task_id] = {
        "task": {"id": task_id, "chat_id": 5, "drive_root": str(qenv.drive)},
        "worker_id": 0,
    }
    write_task_result(qenv.drive, task_id, STATUS_COMPLETED, chat_id=5,
                      result="the settled shared-drive answer")
    row_path = task_result_path(qenv.drive, task_id)
    before = row_path.read_bytes()
    ci.request_cancel(qenv.drive, task_id, reason="stop", allow_settled_target=True)

    assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_ALREADY_SETTLED
    assert not state["alive"]
    assert row_path.read_bytes() == before, (
        "GR7-2: the settled terminal row survives the kill BYTE-IDENTICAL"
    )
    stored = load_task_result(qenv.drive, task_id)
    assert "headless_child_drive_root" not in stored
    assert not any(
        "memory_export" in str(a.get("name") or "")
        for a in (stored.get("artifacts") or [])
    )


def test_gr7_2_split_drive_child_answer_never_replaces_the_settled_canonical(
    qenv, monkeypatch,
):
    """The SPLIT-DRIVE probe: a child drive holding a DIFFERING answer used to
    replace the canonical settled answer through the pre-short-circuit
    copy-back — a completion-wins violation inside the kill path."""
    from supervisor import workers

    task_id = "split7"
    queue = _CaptureQueue()
    monkeypatch.setattr(workers, "get_event_q", lambda: queue, raising=False)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *a, **kw: None)
    state = _install_live_worker(qenv, monkeypatch, task_id)
    child_drive = qenv.drive / "child-drive"
    child_drive.mkdir()
    qenv.q.RUNNING[task_id] = {
        "task": {"id": task_id, "chat_id": 5,
                 "child_drive_root": str(child_drive)},
        "worker_id": 0,
    }
    from ouroboros.headless import prepare_terminal_task_files

    write_task_result(child_drive, task_id, STATUS_COMPLETED, chat_id=5,
                      result="the canonical settled answer")
    # Complete the real adoption before testing a stale replica against CURRENT.
    assert not prepare_terminal_task_files(qenv.drive, qenv.q.RUNNING[task_id]["task"])["error"]
    write_task_result(child_drive, task_id, STATUS_COMPLETED,
                      result="a DIFFERING child answer")
    row_path = task_result_path(qenv.drive, task_id)
    before = row_path.read_bytes()
    ci.request_cancel(qenv.drive, task_id, reason="stop", allow_settled_target=True)

    assert qenv.tl.cancel_task_custody(task_id) == qenv.tl.CANCEL_ALREADY_SETTLED
    assert not state["alive"]
    stored = load_task_result(qenv.drive, task_id)
    assert stored["result"] == "the canonical settled answer", (
        "GR7-2: the kill is about the process, never the result"
    )
    assert row_path.read_bytes() == before


# --------------------------------------------------------------------------
# GR7-3 — wind-down re-checks quiescence without re-minting
# --------------------------------------------------------------------------


def _patch_project_registry(monkeypatch):
    import ouroboros.projects_registry as pr

    done: dict = {"complete": [], "fail": []}
    monkeypatch.setattr(pr, "project_task_bindings", lambda root: {})
    monkeypatch.setattr(
        pr, "complete_project_deletion",
        lambda root, pid: done["complete"].append(str(pid)),
    )
    monkeypatch.setattr(
        pr, "fail_project_deletion",
        lambda root, pid, detail: done["fail"].append((str(pid), str(detail))),
    )
    return done


def _record_mints(monkeypatch):
    mints: list = []
    real_mint = ci.request_cancel

    def _recording_mint(drive, task_id, **kw):
        mints.append(str(task_id))
        return real_mint(drive, task_id, **kw)

    monkeypatch.setattr(ci, "request_cancel", _recording_mint)
    return mints


def test_gr7_3_settled_lingering_root_gets_exactly_one_summary(qenv, monkeypatch):
    """The probe: each 0.5s wind-down round re-entered the full cancel pass,
    minting a fresh request_id → fresh cascade delivery id → up to 21
    duplicate owner summaries. The wind-down must only RE-CHECK."""
    from supervisor import queue_transitions as qt
    from supervisor import workers

    done = _patch_project_registry(monkeypatch)
    queue = _CaptureQueue()
    monkeypatch.setattr(workers, "get_event_q", lambda: queue, raising=False)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *a, **kw: None)

    root_id = "w7-root"
    qenv.q.RUNNING[root_id] = {
        "task": {"id": root_id, "chat_id": 8, "project_id": "p7",
                 "root_task_id": root_id},
        "worker_id": 0,
    }
    write_task_result(qenv.drive, root_id, STATUS_COMPLETED, chat_id=8,
                      result="done; finalizer still owns the row")
    mints = _record_mints(monkeypatch)

    sleeps: list = []

    def _linger_three_rounds(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            qenv.q.RUNNING.pop(root_id, None)

    monkeypatch.setattr(time, "sleep", _linger_three_rounds)

    qt.run_project_deletion(qenv.drive, "p7", 8)

    assert done["complete"] == ["p7"] and not done["fail"]
    assert len(sleeps) >= 3, "the wind-down actually deferred"
    assert mints.count(root_id) == 1, (
        "GR7-3: the settled-lingering root is minted ONCE across the wind-down"
    )
    summaries = [
        e for e in queue.events
        if e.get("type") == "send_message"
        and str(e.get("delivery_id") or "").startswith("cascade:")
    ]
    assert len(summaries) == 1, (
        f"exactly ONE cascade summary, got {len(summaries)}"
    )


def test_gr7_3_stuck_nonsettled_task_still_gets_recancelled(qenv, monkeypatch):
    """A non-settled task appearing in the remaining set re-enters the cancel
    pass (targeted at the roots covering it), while the settled-lingering
    root is never re-minted."""
    from supervisor import queue_transitions as qt
    from supervisor import workers

    done = _patch_project_registry(monkeypatch)
    queue = _CaptureQueue()
    monkeypatch.setattr(workers, "get_event_q", lambda: queue, raising=False)
    monkeypatch.setattr(qenv.q, "_emit_cancel_task_done", lambda *a, **kw: None)

    root_id = "w7b-root"
    qenv.q.RUNNING[root_id] = {
        "task": {"id": root_id, "chat_id": 8, "project_id": "p7b",
                 "root_task_id": root_id},
        "worker_id": 0,
    }
    write_task_result(qenv.drive, root_id, STATUS_COMPLETED, chat_id=8,
                      result="done; winding down")
    mints = _record_mints(monkeypatch)

    sleeps: list = []

    def _inject_then_release(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            # A NEW non-settled task appears mid-wind-down.
            qenv.q.PENDING.append(
                {"id": "w7b-new", "chat_id": 8, "project_id": "p7b"},
            )
        if len(sleeps) >= 2:
            qenv.q.RUNNING.pop(root_id, None)

    monkeypatch.setattr(time, "sleep", _inject_then_release)

    qt.run_project_deletion(qenv.drive, "p7b", 8)

    assert done["complete"] == ["p7b"] and not done["fail"]
    assert load_task_result(qenv.drive, "w7b-new")["status"] == STATUS_CANCELLED, (
        "GR7-3: the stuck/new non-settled task is still re-cancelled"
    )
    assert mints.count(root_id) == 1, (
        "the settled-lingering root is never re-minted by the re-entered pass"
    )
    assert mints.count("w7b-new") >= 1


# --------------------------------------------------------------------------
# GR7-4 — stable delivery identity: the note rides the text, never the id
# --------------------------------------------------------------------------


def test_gr7_4_completed_delivery_dedups_across_a_changed_note(tmp_path):
    """The probe: ["run-a"] → id …X; after reconcile, [] → id …Y — both owed.
    The id must digest the CORE answer only."""
    from supervisor import terminal_delivery as td

    stored = {"status": "completed", "result": "the answer"}
    task = {"id": "sid7", "chat_id": 4}
    first = td.build_completed_result_event(
        tmp_path, task, "sid7", stored, unreconciled_runs=["run-a"],
    )
    replay = td.build_completed_result_event(
        tmp_path, task, "sid7", stored, unreconciled_runs=[],
    )
    assert first["text"] == replay["text"] == "the answer", "the answer is the model's words"
    assert "run-a" in first["terminal_custody_notice"] and "terminal_custody_notice" not in replay, (
        "the disclosure rides the custody row (#1006)"
    )
    assert first["delivery_id"] == replay["delivery_id"], (
        "GR7-4: identity comes from the stable core, never the mutable note"
    )
    # End to end: owed → sent → replay with the shrunk note dedups to nothing.
    assert td.register_pending_delivery(tmp_path, first) is True
    assert td.register_delivery(tmp_path, first["delivery_id"]) is True
    queue = _CaptureQueue()
    assert td.deliver_completed_result(
        tmp_path, task, "sid7", stored, event_queue=queue, unreconciled_runs=[],
    ) is False
    assert queue.events == []
    assert td.pending_deliveries(tmp_path) == [], "one delivery, nothing re-owed"


def test_gr7_4_salvage_and_status_note_ids_are_stable(tmp_path):
    from supervisor import terminal_delivery as td

    # Unreviewed-salvage single-task message.
    row = {"id": "sv7", "chat_id": 4}
    first = td.build_unreviewed_salvage_event(
        tmp_path, row, "sv7", outcome="cancelled",
        salvaged_text="partial work", settled_status="cancelled",
        unreconciled_runs=["run-a"],
    )
    replay = td.build_unreviewed_salvage_event(
        tmp_path, row, "sv7", outcome="cancelled",
        salvaged_text="partial work", settled_status="cancelled",
        unreconciled_runs=[],
    )
    assert "DELEGATED RUNS NOT RECONCILED" in first["text"]
    assert "DELEGATED RUNS NOT RECONCILED" not in replay["text"]
    assert first["delivery_id"] == replay["delivery_id"]

    # Failed/rejected disclosure-only message on the miss lane.
    queue = _CaptureQueue()
    miss_row = {"task_id": "fs7", "chat_id": 4, "result": "died"}
    assert td.deliver_miss_lane_outcome(
        tmp_path, tmp_path, miss_row, "fs7", "failed",
        event_queue=queue, unreconciled_runs=["run-a", "run-b"],
    ) is True
    assert td.deliver_miss_lane_outcome(
        tmp_path, tmp_path, miss_row, "fs7", "failed",
        event_queue=queue, unreconciled_runs=["run-a"],
    ) is True
    ids = {
        e["delivery_id"] for e in queue.events if e.get("type") == "send_message"
    }
    assert len(ids) == 1, "one identity across replays with a shrinking note"
    assert len(td.pending_deliveries(tmp_path)) == 1, (
        "GR7-4: the replay dedups to ONE owed row, never a second owed message"
    )


def test_gr7_4_different_tasks_and_statuses_keep_distinct_identities(tmp_path):
    """The stable identity must not over-dedupe: a different task or a
    different settled status is a different message."""
    from supervisor import terminal_delivery as td

    queue = _CaptureQueue()
    row_a = {"task_id": "da7", "chat_id": 4, "result": "x"}
    td.deliver_miss_lane_outcome(
        tmp_path, tmp_path, row_a, "da7", "failed",
        event_queue=queue, unreconciled_runs=["run-a"],
    )
    td.deliver_miss_lane_outcome(
        tmp_path, tmp_path, row_a, "da7", "rejected_duplicate",
        event_queue=queue, unreconciled_runs=["run-a"],
    )
    row_b = {"task_id": "db7", "chat_id": 4, "result": "x"}
    td.deliver_miss_lane_outcome(
        tmp_path, tmp_path, row_b, "db7", "failed",
        event_queue=queue, unreconciled_runs=["run-a"],
    )
    ids = [e["delivery_id"] for e in queue.events if e.get("type") == "send_message"]
    assert len(ids) == len(set(ids)) == 3


# --------------------------------------------------------------------------
# GR7-5 — quarantine disclosure: typed event once per row, log every sweep
# --------------------------------------------------------------------------


def test_gr7_5_quarantined_intent_row_event_fires_once_per_row(tmp_path, caplog):
    path = tmp_path / "state" / "cancel_intents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "intents": {"bad7": "not-a-dict"},
    }), encoding="utf-8")

    def _forensic_rows():
        trail = tmp_path / "logs" / "supervisor.jsonl"
        if not trail.exists():
            return []
        return [
            json.loads(line)
            for line in trail.read_text(encoding="utf-8").splitlines()
            if line.strip() and '"active_intents_row"' in line
        ]

    with caplog.at_level(logging.ERROR, logger="ouroboros.cancel_intents"):
        for _sweep in range(3):
            assert ci.active_intents(tmp_path, disclose_corruption=True) == {}
    assert len(_forensic_rows()) == 1, (
        "GR7-5: the typed EVENT is emitted once per quarantined row, not per sweep"
    )
    per_sweep = [r for r in caplog.records if "malformed row" in r.message]
    assert len(per_sweep) == 3, "the log.error stays per sweep"

    # A DIFFERENT malformed row content is a new fact and re-announces once.
    path.write_text(json.dumps({
        "schema_version": 1,
        "intents": {"bad7": ["still", "not", "a", "dict"]},
    }), encoding="utf-8")
    for _sweep in range(2):
        ci.active_intents(tmp_path, disclose_corruption=True)
    assert len(_forensic_rows()) == 2
