"""Every dispatch guard refuses its target case and permits its working neighbor."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from ouroboros import place_guard as guard
from ouroboros.places import Place, NoteEntry
from ouroboros.tools.registry_guards import _capability_resource_guard_result
from tests.test_tool_effects import published_tool as _published_tool

published_tool = _published_tool


@pytest.fixture
def places(monkeypatch, tmp_path):
    rows = [Place("quiet", "connector", "service", "account", "C1", "Quiet", "silent",
                  notes=(NoteEntry("Owner instruction", "owner"), NoteEntry("My note", "ouroboros"))),
            Place("active", "connector", "service", "account", "C2", "Active")]
    monkeypatch.setattr(guard.places, "places_of_connector", lambda root, connector: rows if connector == "connector" else [])
    monkeypatch.setattr(guard.places, "load_places", lambda root: rows)

    def resolve(root, *, connector, targets, conversation="", thread=""):
        values = [v for values in targets.values() for v in values]
        match = next((p for p in rows if p.conversation_id in values), None)
        return match, match is not None

    monkeypatch.setattr(guard.places, "place_for_target", resolve)
    monkeypatch.setattr(guard.conversation_lease, "live_author", lambda *a, **k: "")
    ctx = SimpleNamespace(drive_root=tmp_path, task_id="caller", task_metadata={})
    return rows, ctx


def test_advises_extension_and_mcp_both_directions(published_tool, places):
    _, ctx = places
    ctx.task_metadata = {"initiator": "consciousness", "consciousness_autonomy": "observe"}
    for effect in ("write", "message", None):
        name, tool = published_tool(effect)
        assert "Advises" in guard.dispatch_refusal(ctx, name, {}, ext_tool=tool)
    name, tool = published_tool("read")
    assert guard.dispatch_refusal(ctx, name, {}, ext_tool=tool) == ""
    assert "Advises" in guard.dispatch_refusal(ctx, "mcp_demo__read", {}, is_mcp=True)
    ctx.task_metadata["consciousness_autonomy"] = "act"
    assert guard.dispatch_refusal(ctx, "mcp_demo__read", {}, is_mcp=True) == ""
    assert guard.dispatch_refusal(ctx, "read_file", {}) == ""


@pytest.mark.parametrize("metadata", [{}, {"task_type": "task"}, {"schedule_id": "s"},
    {"initiator": "consciousness", "consciousness_autonomy": "full", "wake_cause": "focus"}])
@pytest.mark.parametrize("state", ["silent", "paused"])
def test_place_state_from_every_origin_and_active_sibling(published_tool, places, metadata, state):
    rows, ctx = places
    rows[0] = replace(rows[0], state=state)
    ctx.task_metadata = metadata
    for effect in ("message", "write"):
        name, tool = published_tool(effect, target="room", conversation="room")
        refusal = guard.dispatch_refusal(ctx, name, {"room": "C1"}, ext_tool=tool)
        assert refusal.startswith("⚠️ RESOURCE_CONSTRAINT_BLOCKED:")
        assert state.title() in refusal and "Consciousness › Places" in refusal
        assert "configure_presence" in refusal
        assert guard.dispatch_refusal(ctx, name, {"room": "C2"}, ext_tool=tool) == ""
    name, tool = published_tool("read", target="room")
    assert guard.dispatch_refusal(ctx, name, {"room": "C1"}, ext_tool=tool) == ""


@pytest.mark.parametrize("args", [{}, {"room": "unknown"}])
def test_unattributable_write_explains_connector_wide_hold(published_tool, places, args):
    rows, ctx = places
    name, tool = published_tool("write", target="room")
    assert "cannot be attributed" in guard.dispatch_refusal(ctx, name, args, ext_tool=tool)
    rows[0] = replace(rows[0], state="active")
    assert guard.dispatch_refusal(ctx, name, args, ext_tool=tool) == ""


def test_multi_recipient_send_cannot_hide_silent_place(published_tool, places):
    _, ctx = places
    name, tool = published_tool("message", target=["to", "cc", "bcc"])
    for args in ({"to": ["C2", "C1"]}, {"to": "C2", "bcc": "C1"}, {"to": "C2", "cc": "C1"}):
        assert "Quiet is Silent" in guard.dispatch_refusal(ctx, name, args, ext_tool=tool)
    assert guard.dispatch_refusal(ctx, name, {"to": ["C2"]}, ext_tool=tool) == ""


def test_one_author_names_task_and_allows_current_or_idle(published_tool, places, monkeypatch):
    _, ctx = places
    name, tool = published_tool("message", target="room", conversation="room", thread="thread")
    authors = {"service:account:C2:T": "other"}
    monkeypatch.setattr(guard.conversation_lease, "live_author", lambda key, **kw: authors.get(key, ""))
    assert "tell_task(task_id='other'" in guard.dispatch_refusal(ctx, name, {"room": "C2", "thread": "T"}, ext_tool=tool)
    assert guard.dispatch_refusal(ctx, name, {"room": "C2", "thread": "else"}, ext_tool=tool) == ""
    ctx.task_id = "other"
    assert guard.dispatch_refusal(ctx, name, {"room": "C2", "thread": "T"}, ext_tool=tool) == ""


def test_supervisor_facts_keep_every_place_and_note_author(published_tool, places):
    _, ctx = places
    name, tool = published_tool("message", target=["to", "cc"])
    facts = guard.call_place_facts(ctx, name, {"to": "C2", "cc": "C1"}, ext_tool=tool)
    assert facts["declared"] and facts["effect"] == "message"
    assert {row["place_id"] for row in facts["places"]} == {"active", "quiet"}
    reply = guard.call_place_facts(ctx, "place_reply", {"place_id": "quiet", "text": "hi"})
    assert reply["place_state"] == "silent" and reply["effect"] == "message"
    assert [note["author"] for note in reply["notes"]] == ["owner", "ouroboros"]
    reply = guard.call_place_facts(ctx, "place_reply", {"place_id": "quiet", "text": "hi",
                                                       "account_id": "A", "conversation_id": "C3", "thread_id": "T"})
    assert reply["conversation_key"] == "service:A:C3:T"
    assert reply["targets"] == {"account_id": ["A"], "conversation_id": ["C3"], "thread_id": ["T"]}


def test_actual_registry_callsite_blocks_and_allows(published_tool, places):
    _, ctx = places
    name, tool = published_tool("message", target="room")
    result = _capability_resource_guard_result(ctx, name, {"room": "C1"}, ext_tool=tool)
    assert result.code == "RESOURCE_CONSTRAINT_BLOCKED" and result.status == "blocked"
    assert _capability_resource_guard_result(ctx, name, {"room": "C2"}, ext_tool=tool) is None


@pytest.mark.parametrize("declares_place", [True, False])
def test_unreadable_place_store_holds_only_connector_writes(published_tool, places, monkeypatch, declares_place):
    """A store that needs repair never blocks a skill that connects no place."""
    from ouroboros import extension_registry_state as registry
    from ouroboros.places import PlaceError

    _, ctx = places

    def broken(root, connector):
        raise PlaceError("places_unreadable", "places.json", "Place records need repair")

    monkeypatch.setattr(guard.places, "places_of_connector", broken)
    rows = [{"provides": "place", "skill": "connector", "id": "place"}] if declares_place else []
    monkeypatch.setattr(registry, "live_contribution_projection", lambda: rows)
    name, tool = published_tool("write", target="channel")
    refusal = guard.dispatch_refusal(ctx, name, {"channel": "C1"}, ext_tool=tool)
    if declares_place:
        assert "need repair" in refusal and "Consciousness › Places" in refusal
    else:
        assert refusal == ""
    name, tool = published_tool("read", target="channel")
    assert guard.dispatch_refusal(ctx, name, {"channel": "C1"}, ext_tool=tool) == ""
    assert guard.call_place_facts(ctx, name, {"channel": "C1"}, ext_tool=tool).get("places_unreadable", "") == (
        "Place records need repair" if declares_place else "")
