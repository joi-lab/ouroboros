"""Guard (post-v6.37.0): a live task converted to a project via the UI
("Turn into project", api_project_from_task) must hold its new project's one-writer
lease. The lease reads RUNNING[tid].task['project_id'], NOT the durable bindings, so
the convert path must update RUNNING too (SSOT helper shared with the in-task
ensure_project_scope path). Without it a concurrent same-project task could be
assigned — two writers per project."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _redirect_queue_snapshot(tmp_path, monkeypatch):
    """Every handler test here calls the real ``api_project_from_task``, which now
    persists the queue snapshot after its in-memory lease mark. Redirect
    ``supervisor.queue``'s snapshot writer to a throwaway temp path so no test can write
    (or silently swallow a failed write to) the live ``state/queue_snapshot.json`` under
    the real data root. A test that asserts on the snapshot re-points it explicitly."""
    import supervisor.queue as queue

    snap = tmp_path / "_queue_isolation" / "queue_snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snap)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)


def _request(tmp_path, body):
    async def _json():
        return body

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(drive_root=tmp_path)),
        json=_json,
    )


def test_mark_task_project_running_and_pending():
    from ouroboros.project_lease import (
        candidate_is_leasable,
        mark_task_project,
        running_project_ids,
    )

    running = {"t1": {"task": {"id": "t1", "project_id": ""}}}
    pending = [{"id": "t2", "project_id": ""}]  # PENDING holds bare task dicts
    assert running_project_ids(running.values()) == set()           # no lane yet

    # RUNNING task -> occupies the lane immediately
    assert mark_task_project(running, pending, "t1", "proj-x") is True
    assert running["t1"]["task"]["project_id"] == "proj-x"
    assert running_project_ids(running.values()) == {"proj-x"}
    assert candidate_is_leasable({"id": "z", "project_id": "proj-x"}, {"proj-x"}) is False

    # PENDING task -> its own dict is scoped, so when assigned it will carry the lane
    # (assign_tasks reads the candidate's project_id and copies it into RUNNING)
    assert mark_task_project(running, pending, "t2", "proj-y") is True
    assert pending[0]["project_id"] == "proj-y"

    # no-op safety: not-present task, blank pid
    assert mark_task_project(running, pending, "missing", "proj-z") is False
    assert mark_task_project(running, pending, "t1", "") is False
    assert mark_task_project(running, None, "t1", "proj-x") is True   # pending=None tolerated


def test_ui_conversion_marks_running_task_as_lease_holder(tmp_path, monkeypatch):
    from ouroboros.gateway.projects import api_project_from_task
    from ouroboros.project_lease import candidate_is_leasable, running_project_ids
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")

    # a still-RUNNING main-chat task with no project scope yet
    monkeypatch.setitem(workers.RUNNING, "tlive", {"task": {"id": "tlive", "project_id": ""}})
    assert running_project_ids(workers.RUNNING.values()) == set()

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tlive", "id": "task-tlive", "objective_hint": "build it"},
    )))
    pid = json.loads(resp.body.decode("utf-8"))["project"]["id"]

    # the live task now carries the project_id in RUNNING -> holds the lane
    assert workers.RUNNING["tlive"]["task"]["project_id"] == pid
    assert pid in running_project_ids(workers.RUNNING.values())
    # so a concurrent same-project task is held out of assignment (one writer)
    assert candidate_is_leasable({"id": "other", "project_id": pid}, running_project_ids(workers.RUNNING.values())) is False


def test_ui_conversion_scopes_a_pending_task(tmp_path, monkeypatch):
    """codex finding: a task converted while still PENDING (not yet RUNNING) must get
    its project_id on the PENDING dict too — assign_tasks reads the candidate's own
    project_id and copies it into RUNNING, so without this the queued task starts
    unscoped and never holds its lane."""
    from ouroboros.gateway.projects import api_project_from_task
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")

    pending_task = {"id": "tq", "project_id": "", "type": "task"}
    monkeypatch.setattr(workers, "PENDING", [pending_task])

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tq", "id": "task-tq", "objective_hint": "queued work"},
    )))
    pid = json.loads(resp.body.decode("utf-8"))["project"]["id"]

    # the PENDING task dict is now scoped -> it will occupy the lane once assigned
    assert pending_task["project_id"] == pid
    assert workers.PENDING[0]["project_id"] == pid


def test_ui_conversion_persists_pending_scope_across_restart(tmp_path, monkeypatch):
    """scope finding: marking a PENDING converted task only in memory is not enough.
    restore_pending_from_snapshot rebuilds PENDING from state/queue_snapshot.json on
    restart, and assignment reads task['project_id'] from THERE (never the durable
    bindings). So the convert path must persist the snapshot right after the in-memory
    mark — otherwise a restart in the window (the snapshot is only rewritten on the next
    queue event) restores the task UNSCOPED and it never holds its lane."""
    from ouroboros.gateway.projects import api_project_from_task
    import supervisor.queue as queue
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    snap = tmp_path / "state" / "queue_snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)

    pending_task = {"id": "tq", "project_id": "", "type": "task", "chat_id": 9}
    pending_list = [pending_task]
    # Point BOTH the gateway's view (workers.*) and the snapshot writer (queue.*) at the
    # SAME live list, as init_queue_refs does in production.
    monkeypatch.setattr(workers, "PENDING", pending_list)
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(queue, "PENDING", pending_list)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snap)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "QUEUE_SEQ_COUNTER_REF", {"value": 0})

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tq", "id": "task-tq", "objective_hint": "queued work"},
    )))
    pid = json.loads(resp.body.decode("utf-8"))["project"]["id"]

    # the mark reached the persisted snapshot, not just the in-memory list
    assert snap.exists()
    saved = json.loads(snap.read_text(encoding="utf-8"))
    assert saved["pending"][0]["task"]["project_id"] == pid

    # simulate a restart: empty live queue, restore from the snapshot -> STILL scoped
    pending_list.clear()
    assert queue.restore_pending_from_snapshot() == 1
    assert pending_list[0]["project_id"] == pid


def test_mark_task_project_is_fill_only_over_a_different_project():
    """B4=A: the durable binding is the one truth about a task's project, so by default
    this in-memory copy may FILL an empty value or repeat the same one, and an ordinary
    caller never moves a task from one project to another (that is how a second, empty
    project got a lane). The single exception is a conversion that already holds the
    binding it is about to write, which moves the lane onto that binding."""
    from ouroboros.project_lease import mark_task_project

    running = {"t1": {"task": {"id": "t1", "project_id": "token-atlas"}}}
    pending = [{"id": "t2", "project_id": "token-atlas"}]

    assert mark_task_project(running, pending, "t1", "token-observatory") is False
    assert running["t1"]["task"]["project_id"] == "token-atlas"
    assert mark_task_project(running, pending, "t2", "token-observatory") is False
    assert pending[0]["project_id"] == "token-atlas"
    # Same value stays the idempotent commit point both convert paths rely on.
    assert mark_task_project(running, pending, "t1", "token-atlas") is True
    # The ONE exception: a conversion that owns the durable binding for that project
    # moves the in-memory copy onto it, because the copy follows the truth.
    assert mark_task_project(running, pending, "t1", "token-observatory", authority="binding") is True
    assert running["t1"]["task"]["project_id"] == "token-observatory"


def test_ui_conversion_of_a_bound_task_adopts_it_and_still_refuses_another_room(
    tmp_path, monkeypatch,
):
    """B4=A + R14: converting a task that already belongs to a project creates no
    second project and marks no new lane.

    The ONE-CLICK act (the id the browser derives for this task, or none) is now
    ANSWERED with that project instead of refused: after a sibling conversion binds
    a card, its button can still be on screen for a moment on every surface with no
    reload affordance, and an error toast on a card that is already correctly bound
    is a lie. An EXPLICIT different id is a caller naming another room, and it keeps
    the refusal that NAMES the project (id + display name) in one human sentence."""
    import json

    from ouroboros.gateway.projects import api_project_from_task
    from ouroboros.projects_registry import bind_task_to_project, create_project, list_projects
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    create_project(tmp_path, "token-atlas", name="Token Atlas")
    bind_task_to_project(tmp_path, "tbound", "token-atlas", origin={"absent": "system"})
    monkeypatch.setitem(workers.RUNNING, "tbound", {"task": {"id": "tbound", "project_id": "token-atlas"}})

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tbound", "id": "task-tbound", "objective_hint": "build it"},
    )))
    body = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 200 and body["adopted"] is True
    assert body["project"]["id"] == "token-atlas"
    assert [p["id"] for p in list_projects(tmp_path)] == ["token-atlas"]
    assert workers.RUNNING["tbound"]["task"]["project_id"] == "token-atlas"

    explicit = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tbound", "id": "token-observatory", "objective_hint": "build it"},
    )))
    refusal = json.loads(explicit.body.decode("utf-8"))

    assert explicit.status_code == 409
    assert "Token Atlas" in refusal["error"] and "token-atlas" in refusal["error"]
    assert "open it there or start a new task" in refusal["error"]
    assert [p["id"] for p in list_projects(tmp_path)] == ["token-atlas"]


def test_ui_conversion_with_an_unreadable_bindings_store_proceeds_and_discloses(
    tmp_path, monkeypatch, caplog,
):
    """Proportionality (D6-6): an unreadable store is not a measured incident. The
    conversion runs as it would for an unbound task and says once that it could not
    read the bindings; only a READABLE binding elsewhere refuses."""
    import json
    import logging

    from ouroboros.gateway.projects import api_project_from_task
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "project_task_bindings.json").write_text("{ not json", encoding="utf-8")
    monkeypatch.setitem(workers.RUNNING, "tbroken", {"task": {"id": "tbroken", "project_id": ""}})

    with caplog.at_level(logging.WARNING):
        resp = asyncio.run(api_project_from_task(_request(
            tmp_path, {"task_id": "tbroken", "id": "task-tbroken", "objective_hint": "build it"},
        )))

    assert resp.status_code == 200
    assert json.loads(resp.body.decode("utf-8"))["project"]["id"] == "task-tbroken"
    assert "project_binding_unreadable" in caplog.text


def test_ui_conversion_of_a_scoped_but_unbound_task_moves_its_lane(tmp_path, monkeypatch):
    """A bare-workspace promote stamps a DERIVED proj_<hash> on the row without a
    durable binding and keeps the originating chat, so the Main card still offers
    "Turn into project". Fill-only alone left that conversion half-done: the durable
    bind landed while RUNNING/PENDING kept the derived id and the snapshot was
    skipped, so the new project's one-writer lane stayed free."""
    import json

    from ouroboros.gateway.projects import api_project_from_task
    from ouroboros.project_lease import candidate_is_leasable, running_project_ids
    from ouroboros.projects_registry import project_binding_for_task
    import supervisor.queue as queue
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    snap = tmp_path / "state" / "queue_snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    derived = "proj_deadbeef1234"
    running = {"tws": {"task": {"id": "tws", "project_id": derived}}}
    pending = [{"id": "tpend", "project_id": derived, "type": "task", "chat_id": 5}]
    for mod in (workers, queue):
        monkeypatch.setattr(mod, "RUNNING", running)
        monkeypatch.setattr(mod, "PENDING", pending)
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snap)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "QUEUE_SEQ_COUNTER_REF", {"value": 0})

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tws", "id": "task-tws", "objective_hint": "workspace work"},
    )))
    pid = json.loads(resp.body.decode("utf-8"))["project"]["id"]

    assert resp.status_code == 200
    assert (project_binding_for_task(tmp_path, "tws") or {}).get("project_id") == pid
    assert running["tws"]["task"]["project_id"] == pid       # the lane followed the binding
    leased = running_project_ids(running.values())
    assert leased == {pid}
    assert candidate_is_leasable({"id": "other", "project_id": pid}, leased) is False
    assert snap.exists()                                      # the mark reached the snapshot

    resp_pending = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tpend", "id": "task-tpend", "objective_hint": "queued work"},
    )))
    assert resp_pending.status_code == 200
    assert pending[0]["project_id"] == "task-tpend"
    saved = json.loads(snap.read_text(encoding="utf-8"))
    assert saved["pending"][0]["task"]["project_id"] == "task-tpend"


def test_ui_conversion_adopts_a_binding_that_lands_during_the_naming_await(tmp_path, monkeypatch):
    """Scope review round 1: the durable binding was read BEFORE the naming step, and
    that step can await a model call for seconds. A running task that scoped itself in
    that window came back to a created project row, a moved lane and a durable bind
    that then raised - binding B, lane A, orphan row A, the exact split state P4
    removes. The authority is re-read at the side-effect boundary, so no row is
    created; the one-click act then ANSWERS with the project the task now belongs to
    and the coined name is discarded."""
    import json

    from ouroboros.gateway.projects import api_project_from_task
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        list_projects,
        project_binding_for_task,
    )
    import supervisor.message_bus as message_bus
    import supervisor.workers as workers

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setitem(workers.RUNNING, "trace", {"task": {"id": "trace", "project_id": ""}})
    broadcasts = []
    monkeypatch.setattr(
        message_bus,
        "get_bridge",
        lambda: SimpleNamespace(broadcast=lambda payload: broadcasts.append(payload)),
    )

    async def _namer_binds_meanwhile(*args, **kwargs):
        create_project(tmp_path, "token-observatory", name="Token Observatory")
        bind_task_to_project(tmp_path, "trace", "token-observatory", origin={"absent": "system"})
        return "Coined by the model"

    monkeypatch.setattr("ouroboros.project_naming.llm_project_name_async", _namer_binds_meanwhile)

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "trace", "id": "task-trace", "objective_hint": "build it"},
    )))
    body = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 200 and body["adopted"] is True
    assert body["project"]["id"] == "token-observatory"
    # The claim lands before create_project, so the requested row never exists: only
    # the project the task actually belongs to, and the one broadcast names it.
    assert [p["id"] for p in list_projects(tmp_path)] == ["token-observatory"]
    assert [row["project_id"] for row in broadcasts] == ["token-observatory"]
    assert workers.RUNNING["trace"]["task"]["project_id"] == "token-observatory"
    assert (project_binding_for_task(tmp_path, "trace") or {}).get("project_id") == "token-observatory"
    # The coined name was discarded with the create branch, so nothing recorded a
    # naming decision for a project that was never made.
    assert _events(tmp_path, "project_named") == []


def test_ui_conversion_restores_the_lane_when_the_durable_bind_is_refused(tmp_path, monkeypatch):
    """The window between that re-read and the immutable bind is microseconds, but its
    consequence is the same split state, because the lane mark anticipates a bind that
    is then refused. The mark goes back to the value it had, nothing is broadcast, and
    the answer names the project the durable binding actually holds.

    The empty project row created just before that bind is a DISCLOSED residual, not an
    oversight: the registry has no primitive that removes a row. Its delete lifecycle
    tombstones the row and reserves the id permanently, so a later create with that id
    raises forever, and the in-task path derives that id from the display name the owner
    asked for. An inert row the owner can delete is the smaller harm."""
    import json

    import ouroboros.projects_registry as registry
    import supervisor.message_bus as message_bus
    import supervisor.queue as queue
    import supervisor.workers as workers
    from ouroboros.gateway.projects import api_project_from_task

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    snap = tmp_path / "state" / "queue_snapshot.json"
    snap.parent.mkdir(parents=True, exist_ok=True)
    running = {"tlate": {"task": {"id": "tlate", "project_id": ""}}}
    for mod in (workers, queue):
        monkeypatch.setattr(mod, "RUNNING", running)
        monkeypatch.setattr(mod, "PENDING", [])
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", snap)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "QUEUE_SEQ_COUNTER_REF", {"value": 0})

    real_bind = registry.bind_task_to_project

    def _bind_loses_the_race(drive_root, task_id, project_id, chat_id=None, *, origin):
        registry.create_project(drive_root, "token-observatory", name="Token Observatory")
        real_bind(drive_root, task_id, "token-observatory", origin={"absent": "system"})
        raise ValueError(
            f"task {task_id!r} is already bound to project 'token-observatory'; "
            "project binding is immutable"
        )

    monkeypatch.setattr(registry, "bind_task_to_project", _bind_loses_the_race)
    broadcasts = []
    monkeypatch.setattr(
        message_bus,
        "get_bridge",
        lambda: SimpleNamespace(broadcast=lambda payload: broadcasts.append(payload)),
    )

    resp = asyncio.run(api_project_from_task(_request(
        tmp_path, {"task_id": "tlate", "id": "task-tlate", "name": "Late", "objective_hint": "x"},
    )))
    body = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 409
    assert "Token Observatory" in body["error"] and "token-observatory" in body["error"]
    assert running["tlate"]["task"]["project_id"] == ""
    assert broadcasts == []
    # The residual: the requested row survives, holding no task and no binding, while
    # the task itself belongs to the project the answer names.
    assert sorted(p["id"] for p in registry.list_projects(tmp_path)) == [
        "task-tlate", "token-observatory",
    ]
    assert (registry.project_binding_for_task(tmp_path, "tlate") or {}).get(
        "project_id") == "token-observatory"
    assert "task-tlate" not in {
        str(row.get("project_id") or "") for row in registry.project_task_bindings(tmp_path).values()
    }


# --- one owner message, one Project (origin-keyed adopt + sibling claim) ------
#
# The measured incident: one Main message spawned a direct turn AND the root that
# turn promoted. Both carried the same ingress origin, both were separately
# convertible because every guard was keyed by task_id, and converting each card
# minted its own Project for one piece of work.

_OWNER_TEXT = "Publish and merge the seven pull requests"


def _origin_ref(client_message_id: str = "msg-owner", text: str = _OWNER_TEXT, chat_id: int = 1) -> dict:
    from ouroboros.project_dialogue import build_owner_message_ref

    return build_owner_message_ref(
        chat_id=chat_id, client_message_id=client_message_id,
        ts="2026-09-14T12:15:20+00:00", text=text,
    )


def _seed_origin_task(tmp_path, task_id: str, ref: dict, title: str = "Ship the queue") -> None:
    """Persist the ingress-captured origin the convert path reads by value. An empty
    title leaves the naming step to the (patchable) model call."""
    from ouroboros.task_results import write_task_result

    write_task_result(
        tmp_path, task_id, "running",
        origin_message_ref=ref, origin_message_text=_OWNER_TEXT, **({"title": title} if title else {}),
    )


def _live_queue(monkeypatch, tmp_path, running: dict, pending: list) -> None:
    import supervisor.queue as queue
    import supervisor.workers as workers

    for mod in (workers, queue):
        monkeypatch.setattr(mod, "RUNNING", running)
        monkeypatch.setattr(mod, "PENDING", pending)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "QUEUE_SEQ_COUNTER_REF", {"value": 0})


def _root(task_id: str, ref: dict, project_id: str = "") -> dict:
    return {"id": task_id, "project_id": project_id, "type": "task",
            "delegation_role": "root", "chat_id": 1, "origin_message_ref": dict(ref)}


def _convert(tmp_path, task_id: str, **body):
    payload = {"task_id": task_id, "id": f"task-{task_id}", "objective_hint": _OWNER_TEXT}
    payload.update(body)
    from ouroboros.gateway.projects import api_project_from_task

    return asyncio.run(api_project_from_task(_request(tmp_path, payload)))


def _events(tmp_path, kind: str) -> list:
    path = tmp_path / "logs" / "events.jsonl"
    if not path.exists():
        return []
    return [row for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
            if (row := json.loads(line)).get("type") == kind]


@pytest.fixture
def _direct_turns():
    """The in-process registry of live direct turns is a module singleton."""
    from supervisor.active_activity import get_direct_activity_registry

    registry = get_direct_activity_registry()
    registry.clear()
    try:
        yield registry
    finally:
        registry.clear()


def test_converting_the_turn_claims_the_root_it_promoted(tmp_path, monkeypatch, _direct_turns):
    """The incident, in order: the owner converts the live DIRECT TURN while the
    root it promoted is still running in Main. One Project is created, and the
    root joins it instead of keeping a convert button that mints a second one."""
    from ouroboros.projects_registry import list_projects, project_binding_for_task

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    _seed_origin_task(tmp_path, "t-turn", ref)
    _seed_origin_task(tmp_path, "t-root", ref)
    running = {"t-root": {"task": _root("t-root", ref)}}
    _live_queue(monkeypatch, tmp_path, running, [])
    _direct_turns.register("t-turn", 1, origin_message_ref=ref)

    resp = _convert(tmp_path, "t-turn")
    pid = json.loads(resp.body.decode("utf-8"))["project"]["id"]

    assert resp.status_code == 200
    assert [p["id"] for p in list_projects(tmp_path)] == [pid]
    assert (project_binding_for_task(tmp_path, "t-turn") or {}).get("project_id") == pid
    assert (project_binding_for_task(tmp_path, "t-root") or {}).get("project_id") == pid
    assert running["t-root"]["task"]["project_id"] == pid   # the lane followed the bind
    [row] = _events(tmp_path, "project_origin_siblings_bound")
    assert row["task_id"] == "t-turn" and row["project_id"] == pid
    assert row["bound"] == ["t-root"] and row["skipped"] == []
    # A project that really WAS created still records how it was named.
    [named] = _events(tmp_path, "project_named")
    assert named["task_id"] == "t-turn" and named["reason"] == "explicit_task_title"

    # The root's card may still be showing its stale button (a mid-air /api/state
    # refresh, the Telegram mini app, a phone). Clicking it ADOPTS the project the
    # work already has and answers with that row - never a second project, never an
    # error toast on a card that is already correctly bound.
    again = _convert(tmp_path, "t-root")
    body = json.loads(again.body.decode("utf-8"))
    assert again.status_code == 200 and body["adopted"] is True
    assert body["project"]["id"] == pid
    assert [p["id"] for p in list_projects(tmp_path)] == [pid]


def test_one_origin_with_three_live_roots_converts_to_one_project_in_any_order(
    tmp_path, monkeypatch, _direct_turns,
):
    """Swarm shape: one owner message, several live roots. Whichever card the owner
    clicks first creates the room; the others join it, in any order."""
    from ouroboros.projects_registry import list_projects, project_task_bindings

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    for tid in ("r1", "r2", "r3"):
        _seed_origin_task(tmp_path, tid, ref)
    running = {tid: {"task": _root(tid, ref)} for tid in ("r1", "r2")}
    pending = [_root("r3", ref)]
    _live_queue(monkeypatch, tmp_path, running, pending)

    first = _convert(tmp_path, "r2")
    pid = json.loads(first.body.decode("utf-8"))["project"]["id"]
    second = _convert(tmp_path, "r1")
    third = _convert(tmp_path, "r3")

    assert first.status_code == second.status_code == third.status_code == 200
    assert json.loads(second.body.decode("utf-8"))["adopted"] is True
    assert json.loads(third.body.decode("utf-8"))["adopted"] is True
    assert [p["id"] for p in list_projects(tmp_path)] == [pid]
    bindings = project_task_bindings(tmp_path)
    assert {tid: row["project_id"] for tid, row in bindings.items()} == {
        "r1": pid, "r2": pid, "r3": pid,
    }
    assert pending[0]["project_id"] == pid   # a PENDING sibling is scoped too


def test_an_explicitly_named_project_is_adopted_by_the_turn_that_asked_for_it(
    tmp_path, monkeypatch, _direct_turns,
):
    """P13: an explicit project_name/route_to_project is the model's choice and the
    origin lookup never overrides it - it ADOPTS it. The turn whose message created
    that room joins it, instead of minting a second one beside it."""
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        list_projects,
        project_binding_for_task,
    )

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    _seed_origin_task(tmp_path, "t-turn", ref)
    _seed_origin_task(tmp_path, "t-root", ref)
    # The promote chose a named project and bound the root to it, carrying the same
    # ingress origin onto the binding (supervisor/worker_promotion.py).
    other = create_project(tmp_path, "other-room", name="Other Room")
    bind_task_to_project(tmp_path, "t-root", "other-room", other["chat_id"],
                         origin={"ref": ref, "text": _OWNER_TEXT})
    running = {"t-root": {"task": _root("t-root", ref, project_id="other-room")}}
    _live_queue(monkeypatch, tmp_path, running, [])
    _direct_turns.register("t-turn", 1, origin_message_ref=ref)

    resp = _convert(tmp_path, "t-turn")
    body = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 200 and body["adopted"] is True
    assert body["project"]["id"] == "other-room"
    assert [p["id"] for p in list_projects(tmp_path)] == ["other-room"]
    assert (project_binding_for_task(tmp_path, "t-turn") or {}).get("project_id") == "other-room"
    assert _events(tmp_path, "project_named") == []      # adoption names nothing
    assert _events(tmp_path, "project_origin_siblings_bound") == []


def test_a_sibling_bound_elsewhere_is_skipped_and_disclosed(tmp_path, monkeypatch, _direct_turns):
    """A live sibling whose OWN binding names another project (a pre-origin row, or
    one bound through a path that carried no ref) keeps it: the binding is immutable,
    so the claim skips it, restores its lane and says so durably - never a 4xx for
    the owner, never a silent divergence."""
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        list_projects,
        project_binding_for_task,
    )

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    _seed_origin_task(tmp_path, "t-turn", ref)
    _seed_origin_task(tmp_path, "t-root", ref)
    legacy = create_project(tmp_path, "legacy-room", name="Legacy Room")
    bind_task_to_project(tmp_path, "t-root", "legacy-room", legacy["chat_id"],
                         origin={"absent": "producer_missing_ref"})
    running = {"t-root": {"task": _root("t-root", ref, project_id="legacy-room")}}
    _live_queue(monkeypatch, tmp_path, running, [])

    resp = _convert(tmp_path, "t-turn")
    pid = json.loads(resp.body.decode("utf-8"))["project"]["id"]

    assert resp.status_code == 200 and pid != "legacy-room"
    assert sorted(p["id"] for p in list_projects(tmp_path)) == sorted([pid, "legacy-room"])
    assert (project_binding_for_task(tmp_path, "t-root") or {}).get("project_id") == "legacy-room"
    assert running["t-root"]["task"]["project_id"] == "legacy-room"   # lane restored
    [row] = _events(tmp_path, "project_origin_siblings_bound")
    assert row["bound"] == [] and [item["task_id"] for item in row["skipped"]] == ["t-root"]


def test_a_sibling_that_binds_during_the_naming_await_is_adopted_not_duplicated(
    tmp_path, monkeypatch, _direct_turns,
):
    """Naming can await a model call for seconds, and a second card of the same
    message (another tab, the desktop shell and the mini app at once) can claim the
    origin inside that window. The authority is re-read after naming, so the second
    conversion adopts instead of minting the duplicate."""
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        list_projects,
    )

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    _seed_origin_task(tmp_path, "t-turn", ref, title="")
    _live_queue(monkeypatch, tmp_path, {}, [])

    async def _sibling_converts_meanwhile(*_args, **_kwargs):
        room = create_project(tmp_path, "sibling-room", name="Sibling Room")
        bind_task_to_project(tmp_path, "t-root", "sibling-room", room["chat_id"],
                             origin={"ref": ref, "text": _OWNER_TEXT})
        return "Coined by the model"

    monkeypatch.setattr("ouroboros.project_naming.llm_project_name_async", _sibling_converts_meanwhile)

    resp = _convert(tmp_path, "t-turn", objective_hint="")
    body = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 200 and body["adopted"] is True
    assert body["project"]["id"] == "sibling-room"
    assert [p["id"] for p in list_projects(tmp_path)] == ["sibling-room"]


def test_a_conversion_whose_origin_lands_late_still_adopts_the_message_project(
    tmp_path, monkeypatch, _direct_turns,
):
    """The ingress record can persist only AFTER the click starts - the window the
    convert path already documents for the naming await. Resolving the origin once,
    before the authority reads, then answering "this work has no project" is exactly
    how the second Project got minted while a sibling of the SAME message was already
    bound to the first. The re-read runs inside the claim, ahead of both reads."""
    import ouroboros.gateway.projects as gateway
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        list_projects,
        project_binding_for_task,
    )

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    _seed_origin_task(tmp_path, "t-turn", ref)
    room = create_project(tmp_path, "the-work", name="The Work")
    bind_task_to_project(tmp_path, "t-root", "the-work", room["chat_id"],
                         origin={"ref": ref, "text": _OWNER_TEXT})
    _live_queue(monkeypatch, tmp_path, {}, [])
    real_origin, calls = gateway._owner_task_origin, []

    def _lands_late(drive_root, task_id):
        calls.append(task_id)
        if len(calls) == 1:
            return {"absent": "post_hoc_unresolved"}
        return real_origin(drive_root, task_id)

    monkeypatch.setattr(gateway, "_owner_task_origin", _lands_late)

    resp = _convert(tmp_path, "t-turn")
    body = json.loads(resp.body.decode("utf-8"))

    assert resp.status_code == 200 and body["adopted"] is True
    assert body["project"]["id"] == "the-work"
    assert [p["id"] for p in list_projects(tmp_path)] == ["the-work"]
    assert (project_binding_for_task(tmp_path, "t-turn") or {}).get("project_id") == "the-work"
    assert _events(tmp_path, "project_named") == []


def test_two_sibling_cards_converted_at_once_still_yield_one_project(
    tmp_path, monkeypatch, _direct_turns,
):
    """The owner has the same work on two surfaces (a second tab, the desktop shell
    and the mini app) and clicks both cards inside one naming window. Naming runs
    OUTSIDE the claim lock because it can await a model for seconds, so both
    conversions reach the decision together; the lock makes the second one adopt what
    the first created instead of minting the duplicate."""
    import threading

    from ouroboros.projects_registry import list_projects, project_task_bindings

    (tmp_path / "logs").mkdir()
    ref = _origin_ref()
    for tid in ("r1", "r2"):
        _seed_origin_task(tmp_path, tid, ref, title="")
    running = {tid: {"task": _root(tid, ref)} for tid in ("r1", "r2")}
    _live_queue(monkeypatch, tmp_path, running, [])
    barrier = threading.Barrier(2, timeout=10)

    async def _both_finish_naming_together(*_args, **_kwargs):
        barrier.wait()
        return "Coined by the model"

    monkeypatch.setattr("ouroboros.project_naming.llm_project_name_async", _both_finish_naming_together)
    answers: dict = {}

    def _convert_in_thread(task_id):
        answers[task_id] = _convert(tmp_path, task_id, objective_hint="")

    threads = [threading.Thread(target=_convert_in_thread, args=(tid,)) for tid in ("r1", "r2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert {tid: resp.status_code for tid, resp in answers.items()} == {"r1": 200, "r2": 200}
    projects = [p["id"] for p in list_projects(tmp_path)]
    assert len(projects) == 1, projects
    bound = {tid: row["project_id"] for tid, row in project_task_bindings(tmp_path).items()}
    assert bound == {"r1": projects[0], "r2": projects[0]}
    # Exactly one of them created the room; the other was told it was adopted.
    adopted = [tid for tid, resp in answers.items()
               if json.loads(resp.body.decode("utf-8")).get("adopted")]
    assert len(adopted) == 1
