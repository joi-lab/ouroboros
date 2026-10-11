"""What a routing verb may continue, and what stamps a project's last-result pointer.

A room once offered exactly ONE predecessor candidate (the project's last-result
pointer, which a CHILD had overwritten), the model named the interrupted root
itself and was refused `AUTHORITY_SOURCE_UNAVAILABLE`, and the retry without a
predecessor minted a duplicate root. Later a coordinator task named the settled
roots of five other projects, each to be continued inside its own project, and
was refused seven times because the door compared the predecessor's project with
the CALLER's room. The list is a HINT; the door is a predicate on the result
itself: settled and readable - never where the caller sits, where the work lands
or whether it is a root's or a helper's; those facts are disclosed in the receipt.
"""

from __future__ import annotations

import types

import pytest


def _room_ctx(tmp_path, metadata, *, project_id: str = "racer"):
    """A project room's routing ctx: the host facts of this turn plus the room."""
    return types.SimpleNamespace(
        task_metadata=metadata, drive_root=tmp_path, budget_drive_root=str(tmp_path),
        project_id=project_id, current_chat_id=7, pending_events=[], event_queue=None,
    )


def _host_ctx(tmp_path, *, pending=None, running=None):
    return types.SimpleNamespace(
        DRIVE_ROOT=tmp_path, PENDING=list(pending or []), RUNNING=dict(running or {}),
    )


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """`_durable_project_of_request` reads the registry through config.DATA_DIR."""
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)


class _RecordingQueue:
    """A supervisor that would accept the event, so an emission cannot hide."""

    def __init__(self):
        self.events = []

    def put_nowait(self, event):
        self.events.append(event)


def _door(ctx, task_id, evt=None):
    from ouroboros.tools.control_routing import _attach_predecessor_authority_from_metadata

    return _attach_predecessor_authority_from_metadata(ctx, evt if evt is not None else {}, task_id)


def _confirm(monkeypatch, effective_project_id: str = ""):
    """The admission receipt names where the task actually landed."""
    monkeypatch.setattr(
        "ouroboros.tools.control_events._wait_for_promotion_admission",
        lambda *_a, **_k: {"status": "scheduled", "effective_project_id": effective_project_id},
    )


_TOWER_POINTER = {
    "kind": "task_result", "task_id": "tower-root", "human_label": "another room's work",
    "tool": "get_task_result", "arguments": {"task_id": "tower-root", "include_authority": True},
}


def _tower(tmp_path):
    """A second project with one settled root, exactly the shape the coordinator named."""
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    create_project(tmp_path, "tower", name="Tower")
    write_task_result(tmp_path, "tower-root", "completed", project_id="tower",
                      objective="another room's work", ts="2026-08-10T00:00:01Z")


def test_a_root_the_room_manifest_lists_is_still_addressable(tmp_path):
    """I29 positive path (owner batch 3, answer 6b=A): the narrowing removed
    CHILDREN from the window, and a listed owner root stays promotable."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the room's own finished work", ts="2026-08-10T00:00:01Z")

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-1", {"project_id": "racer"},
    )
    listed = [row["task_id"] for row in metadata["project_routing_manifest"]["final_results"]]
    assert listed == ["racer-root"]

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-root", evt) == ""
    assert evt["predecessor_task_id"] == "racer-root"
    assert evt["predecessor_authority_source"]["tool"] == "get_task_result"


def test_a_listed_row_is_accepted_only_with_the_pointer_the_host_issued_for_it(tmp_path):
    """A row the host showed carries its own host-issued authority source, and only
    that one is accepted: rebuilding a source for a shown row would make a tampered
    manifest row indistinguishable from a host-built one."""
    import copy

    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the root", ts="2026-08-10T00:00:01Z")
    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-2", {"project_id": "racer"},
    )
    tampered = copy.deepcopy(metadata)
    tampered["project_last_task_result"]["authority_source"] = {"kind": "invented"}
    for row in tampered["project_routing_manifest"]["final_results"]:
        row["authority_source"] = {"kind": "invented"}

    evt: dict = {}
    assert "no readable authority source" in _door(_room_ctx(tmp_path, tampered), "racer-root", evt)
    assert evt == {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-root") == ""  # the host's own row passes


def test_a_room_root_older_than_the_list_is_addressable_all_the_same(tmp_path, monkeypatch):
    """The 16-row cap is a HINT window, never the door: the night's root was a
    finished root of this very project, and only the cap hid it."""
    import server
    from ouroboros import runtime_limits
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    monkeypatch.setattr(runtime_limits, "ROUTING_MANIFEST_RESULT_ROWS", 2)
    write_task_result(tmp_path, "racer-old", "completed", project_id="racer",
                      objective="the interrupted work", ts="2026-08-10T00:00:01Z")
    for index in range(2):
        write_task_result(tmp_path, f"racer-new{index}", "completed", project_id="racer",
                          objective="newer work", ts=f"2026-08-11T00:00:0{index}Z")

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-2", {"project_id": "racer"},
    )
    manifest = metadata["project_routing_manifest"]
    assert [row["task_id"] for row in manifest["final_results"]] == ["racer-new1", "racer-new0"]
    assert manifest["omissions"]["final_results"] == 1

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-old", evt) == ""
    assert evt["predecessor_task_id"] == "racer-old"
    assert evt["predecessor_authority_source"] == {
        "kind": "task_result", "task_id": "racer-old", "human_label": "the interrupted work",
        "tool": "get_task_result",
        "arguments": {"task_id": "racer-old", "include_authority": True},
    }


def test_a_helpers_result_is_continued_with_its_root_named_and_never_offered(tmp_path, monkeypatch):
    """A pointer stamped by a child before only roots stamped it still names a child:
    the host offers the ROOT (the hint stays roots-only, owner decision 6b=A) and heals
    the pointer, while a helper's result the model names on purpose is continued -
    the receipt says whose helper it was and where its root is."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task
    from ouroboros.tools.project_journal import record_project_last_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the root", ts="2026-08-10T00:00:01Z")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer",
                      objective="helper work", parent_task_id="racer-root",
                      root_task_id="racer-root", delegation_role="subagent",
                      ts="2026-08-10T00:00:02Z")
    record_project_last_result("racer", "racer-child", tmp_path)

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-3", {"project_id": "racer"},
    )
    assert metadata["project_last_task_result"]["task_id"] == "racer-root"
    assert [row["task_id"] for row in
            metadata["project_routing_manifest"]["final_results"]] == ["racer-root"]

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-child", evt) == ""
    assert evt["predecessor_task_id"] == "racer-child"
    assert evt["predecessor_facts"] == {"project_id": "racer", "helper": True,
                                        "root_task_id": "racer-root", "parent_task_id": "racer-root"}
    root_evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "racer-root", root_evt) == ""
    assert root_evt["predecessor_facts"]["helper"] is False  # a root is nobody's helper

    _confirm(monkeypatch, effective_project_id="racer")
    ctx = _room_ctx(tmp_path, metadata)
    out = _promote_chat_to_task(ctx, "Continue the helper's work", workspace="none",
                                predecessor_task_id="racer-child")
    assert out.startswith("OK: task"), out
    assert "Note: predecessor racer-child is a delegated helper's result; its root is racer-root." in out
    assert "belongs to" not in out  # same project: nothing else to disclose


def test_a_helper_predecessor_is_named_on_the_route_verb_and_without_a_root_id(tmp_path, monkeypatch):
    """The helper note rides both verbs and names what the helper's row knows: its root,
    else its parent, else that the root is unknown - it never goes silent on a helper
    whose row carries only the subagent role."""
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task, _route_to_project

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer", objective="helper work",
                      parent_task_id="racer-root", root_task_id="racer-root", delegation_role="subagent")
    write_task_result(tmp_path, "racer-nested", "completed", project_id="racer", objective="nested helper",
                      parent_task_id="racer-child", delegation_role="subagent")
    write_task_result(tmp_path, "racer-orphan", "completed", project_id="racer", objective="role only",
                      delegation_role="subagent")
    _confirm(monkeypatch, effective_project_id="racer")

    routed = _room_ctx(tmp_path, {}, project_id="racer")
    out = _route_to_project(routed, "racer", "continue the helper's work", predecessor_task_id="racer-child")
    assert out.startswith("✉️ Routed to project 'Racer' (racer)"), out
    assert "Note: predecessor racer-child is a delegated helper's result; its root is racer-root." in out

    nested = _room_ctx(tmp_path, {}, project_id="racer")
    out = _promote_chat_to_task(nested, "Continue the nested helper", workspace="none", predecessor_task_id="racer-nested")
    assert "Note: predecessor racer-nested is a delegated helper's result; its parent is racer-child." in out

    orphan = _room_ctx(tmp_path, {}, project_id="racer")
    out = _promote_chat_to_task(orphan, "Continue the role-only helper", workspace="none", predecessor_task_id="racer-orphan")
    assert "Note: predecessor racer-orphan is a delegated helper's result; its root is unknown." in out


def test_a_failed_root_is_a_settled_predecessor(tmp_path):
    """Settled means completed, failed or cancelled: the coordinator's first refused
    predecessor had failed at its absolute ceiling and was still the work to continue."""
    from ouroboros.task_results import write_task_result

    write_task_result(tmp_path, "tower-failed", "failed", project_id="tower",
                      objective="ran out of ceiling", reason_code="absolute_ceiling")
    evt: dict = {}
    assert _door(_room_ctx(tmp_path, {}, project_id="racer"), "tower-failed", evt) == ""
    assert evt["predecessor_authority_source"]["arguments"] == {"task_id": "tower-failed", "include_authority": True}

def test_another_projects_root_is_continued_from_this_room_and_the_hint_stays_room_local(tmp_path):
    """The room's manifest lists only its own roots - a hint - while the door judges the
    root itself: another project's settled root is continued from here with a pointer
    rebuilt from the durable result, never from a shown row."""
    import server
    from ouroboros.projects_registry import create_project

    project = create_project(tmp_path, "racer", name="Racer")
    _tower(tmp_path)

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-4", {"project_id": "racer"},
    )
    assert metadata["project_routing_manifest"]["final_results"] == []

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, metadata), "tower-root", evt) == ""
    assert evt["predecessor_task_id"] == "tower-root"
    assert evt["predecessor_authority_source"] == _TOWER_POINTER
    assert evt["predecessor_facts"] == {"project_id": "tower", "helper": False, "root_task_id": "", "parent_task_id": ""}


def test_a_pooled_task_continues_another_rooms_root_into_that_room_in_one_hop(tmp_path, monkeypatch):
    """The coordinator shape: a pooled task carries no host manifest at all (its metadata is
    the client surface and its own contract), sits in one room, and names a settled root
    of another project as the predecessor of work it sends INTO that project. Both verbs
    schedule it in one hop, the event carries the rebuilt pointer and nothing else new,
    and a landing in the predecessor's own project has nothing to disclose."""
    from ouroboros.projects_registry import create_project
    from ouroboros.tools.control_routing import _promote_chat_to_task, _route_to_project

    create_project(tmp_path, "coord", name="Coordination")
    _tower(tmp_path)
    metadata = {"client_surface": {"channel": "web"}, "task_contract": {"objective": "coordinate"}}
    _confirm(monkeypatch, effective_project_id="tower")

    promoted = _room_ctx(tmp_path, metadata, project_id="coord")
    out = _promote_chat_to_task(promoted, "Continue the tower work", project_id="tower",
                                workspace="none", predecessor_task_id="tower-root")
    assert out.startswith("OK: task"), out
    assert "belongs to" not in out
    [evt] = promoted.pending_events
    assert evt["project_id"] == "tower"
    assert evt["predecessor_task_id"] == "tower-root"
    assert evt["predecessor_authority_source"] == _TOWER_POINTER
    assert "predecessor_facts" not in evt

    routed = _room_ctx(tmp_path, metadata, project_id="coord")
    out = _route_to_project(routed, "tower", "continue the tower work", predecessor_task_id="tower-root")
    assert out.startswith("✉️ Routed to project 'Tower' (tower)"), out
    assert "belongs to" not in out
    [evt] = routed.pending_events
    assert evt["predecessor_task_id"] == "tower-root"
    assert evt["predecessor_authority_source"] == _TOWER_POINTER
    assert "predecessor_facts" not in evt


def test_a_landing_outside_the_predecessors_project_is_disclosed_once(tmp_path, monkeypatch):
    """A free choice, said in the receipt like the second-project note: a continuation
    landing in another project names the predecessor's own project, on both verbs; one
    landing at home carries no such sentence, so the note cannot fire unconditionally."""
    from ouroboros.projects_registry import create_project
    from ouroboros.tools.control_routing import _promote_chat_to_task, _route_to_project

    create_project(tmp_path, "racer", name="Racer")
    _tower(tmp_path)
    note = "Note: predecessor tower-root belongs to project 'tower'; this continuation runs in project 'racer' (your choice)."

    _confirm(monkeypatch, effective_project_id="racer")
    ctx = _room_ctx(tmp_path, {}, project_id="racer")
    out = _promote_chat_to_task(ctx, "Continue elsewhere", project_id="racer", workspace="none",
                                predecessor_task_id="tower-root")
    assert out.startswith("OK: task") and note in out, out

    routed = _room_ctx(tmp_path, {}, project_id="racer")
    out = _route_to_project(routed, "racer", "continue elsewhere", predecessor_task_id="tower-root")
    assert out.startswith("✉️ Routed to project 'Racer' (racer)") and note in out, out

    _confirm(monkeypatch, effective_project_id="tower")
    home = _room_ctx(tmp_path, {}, project_id="racer")
    out = _promote_chat_to_task(home, "Continue at home", project_id="tower", workspace="none",
                                predecessor_task_id="tower-root")
    assert out.startswith("OK: task") and "belongs to" not in out, out


def test_a_main_root_is_continued_from_a_room_and_its_home_is_named(tmp_path, monkeypatch):
    """A project-less (Main) root was reachable only through the Main lane's list; the door
    judges the root, so a room names it too, and the receipt says it comes from Main."""
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "main-root", "completed", project_id="", objective="main work")

    evt: dict = {}
    assert _door(_room_ctx(tmp_path, {}, project_id="racer"), "main-root", evt) == ""
    assert evt["predecessor_task_id"] == "main-root" and evt["predecessor_facts"]["project_id"] == ""

    _confirm(monkeypatch, effective_project_id="racer")
    ctx = _room_ctx(tmp_path, {}, project_id="racer")
    out = _promote_chat_to_task(ctx, "Continue the main work here", project_id="racer",
                                workspace="none", predecessor_task_id="main-root")
    assert "predecessor main-root belongs to the main chat; this continuation runs in project 'racer'" in out


def test_a_public_conversation_continues_a_settled_root_and_still_lands_without_a_project(tmp_path, monkeypatch):
    """Presence holds the routing verb under its own ceiling: the door judges the root,
    while the promote still strips project, workspace and source - the ceiling is the
    caller's, never the predecessor's - and the receipt names where the predecessor lives."""
    from ouroboros.tools.control_routing import _promote_chat_to_task

    _tower(tmp_path)
    _confirm(monkeypatch, effective_project_id="")
    ctx = _room_ctx(tmp_path, {"presence": {"binding_id": "b" * 32}}, project_id="")
    out = _promote_chat_to_task(ctx, "Continue the tower work", project_id="tower",
                                workspace="none", predecessor_task_id="tower-root")
    assert out.startswith("OK: task"), out
    [evt] = ctx.pending_events
    assert evt["project_id"] == "" and evt["presence"] == {"binding_id": "b" * 32}
    assert evt["predecessor_task_id"] == "tower-root"
    assert "predecessor tower-root belongs to project 'tower'; this continuation runs in the main chat" in out


def test_a_live_root_is_steer_territory_not_a_predecessor(tmp_path):
    """Accepting a PENDING/RUNNING root would mint the second root the night
    produced; the room still SEES it, with the status that says `steer_task`."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-live", "running", project_id="racer",
                      objective="work in flight", ts="2026-08-10T00:00:01Z")
    running = {"racer-live": {"task": {"id": "racer-live", "project_id": "racer",
                                       "title": "Live work", "objective": "work in flight"},
                              "started_at": "2026-08-10T00:00:01Z"}}

    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path, running=running), int(project["chat_id"]), "room-5",
        {"project_id": "racer"},
    )
    manifest = metadata["project_routing_manifest"]
    assert [row["task_id"] for row in manifest["active_roots"]] == ["racer-live"]
    assert manifest["active_roots"][0]["status"] == "running"

    evt: dict = {}
    refusal = _door(_room_ctx(tmp_path, metadata), "racer-live", evt)
    assert "steer_task" in refusal and "running" in refusal
    assert evt == {}


def test_an_unreadable_predecessor_still_answers_authority_source_unavailable(tmp_path):
    """A collected result is the one case the predicate cannot rescue, and the
    promote says so in its own refusal instead of emitting anything."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from ouroboros.tools.control_routing import _promote_chat_to_task

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-gone", "completed", project_id="racer",
                      objective="collected work", ts="2026-08-10T00:00:01Z")
    metadata = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-6", {"project_id": "racer"},
    )
    (tmp_path / "task_results" / "racer-gone.json").unlink()

    evt: dict = {}
    refusal = _door(_room_ctx(tmp_path, metadata), "racer-gone", evt)
    assert refusal == "the selected predecessor task result is missing or unreadable"
    assert evt == {}

    ctx = _room_ctx(tmp_path, metadata)
    ctx.event_queue = _RecordingQueue()
    out = _promote_chat_to_task(ctx, "Continue the interrupted work", workspace="none",
                                predecessor_task_id="racer-gone")
    assert out.startswith("\u26a0\ufe0f AUTHORITY_SOURCE_UNAVAILABLE (promote_chat_to_task):")
    assert "missing or unreadable" in out
    # Nothing was emitted, so no id was reserved and no second root can follow.
    assert ctx.event_queue.events == [] and ctx.pending_events == []
    assert not (tmp_path / "task_results" / "racer-gone.json").exists()


def test_outside_a_room_an_unlisted_settled_root_is_continued_too(tmp_path):
    """With no room there is no project to compare against, and none is needed: a Main
    turn names any settled root, listed (the host's own pointer) or not (a pointer
    rebuilt from the durable result); a missing id stays the one case no predicate
    can rescue, and it still emits nothing."""
    import server
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer",
                      objective="the room's work", ts="2026-08-10T00:00:01Z")

    metadata = server._decision_turn_metadata(_host_ctx(tmp_path), 1, "main-1", {})
    main_ctx = _room_ctx(tmp_path, {"client_message_id": "main-1"}, project_id="")
    evt: dict = {}
    assert _door(main_ctx, "racer-root", evt) == ""
    assert evt["predecessor_task_id"] == "racer-root"
    assert evt["predecessor_authority_source"]["arguments"] == {"task_id": "racer-root", "include_authority": True}

    listed_ctx = _room_ctx(tmp_path, metadata, project_id="")
    assert _door(listed_ctx, "racer-root") == ""

    gone: dict = {}
    assert _door(main_ctx, "never-existed", gone) == "the selected predecessor task result is missing or unreadable"
    assert gone == {}


def test_only_a_root_finalization_moves_the_projects_pointer(tmp_path):
    """The pointer answers "continue from here" for the ROOM; a child that
    finalizes later must not move it onto work no owner ever addressed."""
    from ouroboros.projects_registry import create_project, get_project
    from ouroboros.tools.project_journal import record_task_finalization

    create_project(tmp_path, "racer", name="Racer")
    record_task_finalization(
        "racer", {"id": "racer-root", "root_task_id": "racer-root"},
        objective="the root", kind="task", exec_status="completed", drive_root=tmp_path,
    )
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"

    record_task_finalization(
        "racer", {"id": "racer-child", "parent_task_id": "racer-root",
                  "root_task_id": "racer-root", "delegation_role": "subagent"},
        objective="helper work", kind="task", exec_status="completed", drive_root=tmp_path,
    )
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"

    # The door calls a row a child by its parent OR by its role; so does the stamp.
    record_task_finalization(
        "racer", {"id": "racer-role-only", "root_task_id": "racer-root", "delegation_role": "subagent"},
        objective="helper with no parent field", kind="task", exec_status="completed", drive_root=tmp_path,
    )
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"


def test_the_self_heal_scan_never_offers_or_stamps_a_child(tmp_path):
    """The lookup's fallback scan is the pointer's SECOND writer: with no pointer
    yet and a child as the project's newest result, it answers with the newest ROOT
    and stamps that, never the child the hint does not offer."""
    import os

    import server
    from ouroboros.projects_registry import create_project, get_project
    from ouroboros.task_results import task_results_dir, write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer", result="root answer")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer", result="helper answer",
                      parent_task_id="racer-root", root_task_id="racer-root", delegation_role="subagent")
    results = task_results_dir(tmp_path, create=False)
    os.utime(results / "racer-root.json", (1_000, 1_000))
    os.utime(results / "racer-child.json", (2_000, 2_000))  # the child is the newest file
    assert not get_project(tmp_path, "racer").get("last_task_result_id")

    row = server._latest_project_task_result(_host_ctx(tmp_path), "racer")
    assert row["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"


def test_a_pointer_a_child_stamped_before_the_rule_heals_onto_the_root(tmp_path):
    """A pointer written before "only a root stamps it" may name a child. That is
    provably wrong (not a copy-back in flight), so the lookup answers with the root
    and repairs the pointer; a pointer naming a ROOT is served as it is."""
    import server
    from ouroboros.projects_registry import create_project, get_project, update_project
    from ouroboros.task_results import write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer", result="root answer")
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer", result="helper answer",
                      parent_task_id="racer-root", root_task_id="racer-root", delegation_role="subagent")
    update_project(tmp_path, "racer", last_task_result_id="racer-child")

    assert server._latest_project_task_result(_host_ctx(tmp_path), "racer")["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"

    # The quiet direction: a root pointer is one direct fetch and is left alone.
    write_task_result(tmp_path, "racer-root-2", "completed", project_id="racer", result="later root")
    update_project(tmp_path, "racer", last_task_result_id="racer-root")
    assert server._latest_project_task_result(_host_ctx(tmp_path), "racer")["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"


def test_a_pending_promote_is_neither_the_last_result_nor_a_predecessor(tmp_path):
    """An emitted stub carries the project's id and is the newest file, but it is an
    admission still pending, not a result: the lookup answers with the root and never
    stamps the stub, and the door says what it is instead of "steer this live root"."""
    import os

    import server
    from ouroboros.projects_registry import create_project, get_project
    from ouroboros.task_results import task_results_dir, write_task_result

    create_project(tmp_path, "racer", name="Racer")
    write_task_result(tmp_path, "racer-root", "completed", project_id="racer", result="root answer")
    write_task_result(tmp_path, "racer-promote", "requested", project_id="racer", promotion_admission={
        "status": "emitted", "routing_token": "tok-1", "emitted_at": "2026-09-21T10:00:00Z"})
    results = task_results_dir(tmp_path, create=False)
    os.utime(results / "racer-root.json", (1_000, 1_000))
    os.utime(results / "racer-promote.json", (2_000, 2_000))

    assert server._latest_project_task_result(_host_ctx(tmp_path), "racer")["task_id"] == "racer-root"
    assert get_project(tmp_path, "racer")["last_task_result_id"] == "racer-root"

    refusal = _door(_room_ctx(tmp_path, {}), "racer-promote")
    assert "admission is still pending" in refusal and "steer_task" not in refusal
    # The quiet direction: a root that really is live keeps its own sentence.
    write_task_result(tmp_path, "racer-live", "running", project_id="racer")
    assert "steer_task" in _door(_room_ctx(tmp_path, {}), "racer-live")


def test_the_room_manifest_carries_cancel_facts_and_an_honest_omission_count(tmp_path):
    import server
    from ouroboros.cancel_intents import request_cancel
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    write_task_result(
        tmp_path, "racer-cancelled", "cancelled", project_id="racer",
        objective="stopped work", ts="2026-08-10T00:00:01Z",
        cancel_origin={"source": "owner", "reason": "owner stopped it",
                       "requested_at": "2026-08-10T00:00:00Z"},
    )
    write_task_result(tmp_path, "racer-child", "completed", project_id="racer",
                      objective="helper", parent_task_id="racer-cancelled",
                      root_task_id="racer-cancelled", delegation_role="subagent",
                      ts="2026-08-10T00:00:02Z")
    running = {"racer-live": {"task": {"id": "racer-live", "project_id": "racer",
                                       "title": "Live", "objective": "in flight"},
                              "started_at": "2026-08-10T00:00:03Z"},
               # Another project's live root is none of this room's business.
               "other-live": {"task": {"id": "other-live", "project_id": "other",
                                       "title": "Elsewhere", "objective": "not this room"},
                              "started_at": "2026-08-10T00:00:04Z"}}
    request_cancel(tmp_path, "racer-live", source="owner", reason="stop it")

    manifest = server._decision_turn_metadata(
        _host_ctx(tmp_path, running=running), int(project["chat_id"]), "room-7",
        {"project_id": "racer"},
    )["project_routing_manifest"]

    [row] = manifest["final_results"]
    assert row["task_id"] == "racer-cancelled"
    assert row["cancel_origin"] == {"source": "owner", "reason": "owner stopped it",
                                    "requested_at": "2026-08-10T00:00:00Z"}
    [live] = manifest["active_roots"]
    assert live["task_id"] == "racer-live" and live["cancel_state"] == "pending"
    assert manifest["omissions"]["children"] == 1
    assert manifest["omissions"]["final_results"] == 0
    assert manifest["omissions"]["active_roots"] == 0

    # The live list is bounded too, and says how many of this room's roots it left out.
    crowd = {f"racer-live-{index:02d}": {"task": {"id": f"racer-live-{index:02d}", "project_id": "racer"},
                                          "started_at": "2026-08-10T00:00:05Z"} for index in range(43)}
    crowded = server._decision_turn_metadata(
        _host_ctx(tmp_path, running=crowd), int(project["chat_id"]), "room-8", {"project_id": "racer"},
    )["project_routing_manifest"]
    assert len(crowded["active_roots"]) == 40 and crowded["omissions"]["active_roots"] == 3


def test_the_manifest_row_cap_is_one_runtime_limit(tmp_path, monkeypatch):
    """Both lanes read the same bound; no call-site literal decides the window."""
    import server
    from ouroboros import runtime_limits
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result

    project = create_project(tmp_path, "racer", name="Racer")
    for index in range(3):
        write_task_result(tmp_path, f"root{index}", "completed", project_id="racer",
                          objective="work", ts=f"2026-08-10T00:00:0{index}Z")
    monkeypatch.setattr(runtime_limits, "ROUTING_MANIFEST_RESULT_ROWS", 1)

    room = server._decision_turn_metadata(
        _host_ctx(tmp_path), int(project["chat_id"]), "room-8", {"project_id": "racer"},
    )["project_routing_manifest"]
    main = server._main_routing_manifest(_host_ctx(tmp_path))

    assert len(room["final_results"]) == 1 and room["omissions"]["final_results"] == 2
    assert len(main["final_results"]) == 1 and main["omissions"]["final_results"] == 2
