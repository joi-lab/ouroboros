"""The owner's single start budget, ordered forms and protected commitments."""

import pytest

from ouroboros import context_budget as cb, memory_floor as mf, memory_view as mv
from tests import _memory_view_synthetic as syn
from tests import _memory_inventory_shared as shared


def tokens(snapshot, level=mv.FULL_VIEW):
    return mf.view_tokens(mv.render_story(snapshot, level)) + mf.view_tokens(mv.render_room(snapshot, level))


def frame(bound, physical=10**9):
    return {"margin": bound, "physical": physical, "budget": None}


def test_role_budget_is_one_frame_and_reply_is_charged_once():
    for role, facts, levels in (("integrator", {}, 2), ("consciousness", {"direct_chat": True}, 2),
                                ("integrator", {"direct_chat": True}, 1), ("child", {}, 1)):
        actual = mf.floor_allowances(window_tokens=200_000, output_reserve_tokens=65_536,
            non_memory_tokens=10_000, work_room_levels=cb.start_room_levels(role, **facts))
        assert actual["margin"] == 200_000 - 65_536 - 10_000 - levels * 25_000
        assert actual["physical"] == 200_000 - 65_536 - 10_000
    # Input-only limits use total-equivalent for physical arithmetic and the stated I for B/8.
    actual = mf.floor_allowances(window_tokens=265_536, boundary_tokens=200_000,
                                output_reserve_tokens=65_536, non_memory_tokens=0)
    assert actual["margin"] == 175_000 and actual["physical"] == 200_000
    unknown = mf.floor_allowances(window_tokens=None, output_reserve_tokens=65_536, non_memory_tokens=10)
    assert unknown == {"margin": None, "physical": None, "budget": None}


def test_order_conversation_before_my_pages_and_oldest_reply_first():
    replies = [syn.spoken(1, "ouroboros", 5_000), syn.spoken(2, "ouroboros", 50_000)]
    snapshot = syn.snapshot(story=syn.pages(2, 12_000), room_facts=syn.room("1", lane1=replies))
    assert mf.LADDER == ("F1", "F1b", "F6r", "F6", "F6a", "F3", "F2", "F4", "F7p", "F7", "F5")
    assert mf.fit_memory_view(snapshot, frame(tokens(snapshot))) == mv.FULL_VIEW
    level = mf.fit_memory_view(snapshot, frame(tokens(snapshot) - 100))
    assert level.addressed == (("F2", (replies[0]["address"],)),)
    assert replies[0]["line"] not in mv.render_room(snapshot, level)
    assert replies[1]["line"] in mv.render_room(snapshot, level)
    assert mv.render_story(snapshot, level) == mv.render_story(snapshot)
    owner_target = mf.fit_memory_view(snapshot, {"margin": 10**9, "physical": 10**9, "budget": 0})
    assert "F2" in dict(owner_target.steps) and "F5" in dict(owner_target.steps)


def test_governing_unanswered_and_uncovered_owner_rows_have_distinct_protection():
    rows = [{**syn.spoken(i, "human", 5_000), "owner_side": True, "immune": i == 2,
             "carried": i == 0} for i in range(3)]
    snapshot = syn.snapshot(room_facts=syn.room("1", lane1=rows))
    level = mf.fit_memory_view(snapshot, frame(0))
    assert dict(level.addressed)["F7"] == (rows[0]["address"],)
    text = mv.render_room(snapshot, level)
    assert rows[0]["line"] not in text and all(r["line"] in text for r in rows[1:])
    physical = mf.fit_memory_view(snapshot, frame(0, 0))
    assert dict(physical.addressed)["F7"] == tuple(r["address"] for r in rows[:2])
    assert rows[2]["line"] in mv.render_room(snapshot, physical)
    _, text, _ = mf.render_view_for_mode(snapshot, mode="max", owner_mode="max", window_tokens=80_000,
        known_window=True, output_reserve=65_536, ratio=1, non_memory_tokens=10_000)
    assert "Start room is smaller" in text


def test_pages_go_before_uncovered_owner_rows_and_the_oldest_rows_go_only_when_pages_are_exhausted():
    rows = [{**syn.spoken(i, "human", 5_000), "owner_side": True} for i in range(2)]
    story = syn.pages(2, 12_000)
    snapshot = syn.snapshot(story=story, room_facts=syn.room("1", lane1=rows))
    pages_cut = tokens(snapshot, mv.FloorLevel((("F5", tuple(p["id"] for p in story)),)))
    level = mf.fit_memory_view(snapshot, frame(0, pages_cut))
    assert dict(level.addressed) == {"F5": tuple(p["id"] for p in story)}
    assert all(r["line"] in mv.render_room(snapshot, level) for r in rows)
    oldest = mf.fit_memory_view(snapshot, frame(0, pages_cut - 1))
    assert dict(oldest.addressed)["F7"] == (rows[0]["address"],) and rows[1]["line"] in mv.render_room(snapshot, oldest)
    both = mf.fit_memory_view(snapshot, frame(0, 0))
    assert dict(both.addressed)["F7"] == tuple(r["address"] for r in rows)


@pytest.mark.parametrize("lane1, owners", [
    ([{**syn.spoken(i, "human", 8_000), "owner_side": True} for i in range(3)], True),
    ([syn.spoken(i, "ouroboros", 1_000) for i in range(400)], False),
])
def test_the_start_room_notice_names_owner_words_only_when_they_keep_the_view_above_it(lane1, owners):
    snapshot = syn.snapshot(room_facts=syn.room("1", lane1=lane1))
    _, text, facts = mf.render_view_for_mode(snapshot, mode="max", owner_mode="max", window_tokens=200_000,
        known_window=True, output_reserve=65_536, ratio=1, non_memory_tokens=104_464)
    floor = facts["floor"]
    assert (floor["allowance_tokens"], floor["physical_allowance_tokens"]) == (5_000, 30_000)
    assert "Start room is smaller than requested" in text
    assert ("uncovered owner words remain in view" in text) is owners
    assert ("shortest forms" in text) is not owners
    assert (dict(floor["steps"]).get("F2") == 400) is not owners and "F7" not in dict(floor["steps"])


def test_other_rooms_have_range_directory_and_bounded_aggregate():
    rooms = [syn.live_room(i, notes=1, words=3, word_chars=2_000) for i in range(164)]
    snapshot = syn.snapshot(live=rooms)
    ident = rooms[0]["room_id"]
    full = mv.render_room(snapshot)
    ranged = mv.render_room(snapshot, mv.FloorLevel((("F6r", (ident,)),)))
    directory = mv.render_room(snapshot, mv.FloorLevel((("F6", (ident,)),)))
    aggregate = mv.render_room(snapshot, mv.FloorLevel((("F6a", tuple(r["room_id"] for r in rooms)),)))
    assert len(aggregate) < len(directory) < len(ranged) < len(full)
    assert "from='" in ranged and rooms[0]["words"][0]["line"] not in ranged
    assert mv._directory_room(rooms[0]) in directory
    assert "164 open rooms" in aggregate and "memory_read(room_id='*', start=0)" in aggregate
    assert len(aggregate) < 700


def test_giveback_restores_small_earlier_elements_but_never_kept_choices():
    replies = [syn.spoken(1, "ouroboros", 1_000), syn.spoken(2, "ouroboros", 20_000)]
    snapshot = syn.snapshot(room_facts=syn.room("1", lane1=replies))
    bound = tokens(snapshot) - 2_000
    level = mf.fit_memory_view(snapshot, frame(bound))
    assert dict(level.addressed)["F2"] == (replies[1]["address"],)
    kept = mv.FloorLevel((("F2", (replies[0]["address"],)),))
    later = mf.fit_memory_view(snapshot, frame(10**9), kept=kept)
    assert later == kept
    facts = mf.view_facts(snapshot, level, window_tokens=200_000, mode="max", allowances={})
    assert mf.kept_level(snapshot, facts) == level  # counts alone cannot reconstruct a give-back choice


def test_capture_protects_governing_words_and_does_not_recapture_for_fitting(tmp_path):
    shared.world(tmp_path)
    task = {"id": "turn0001", "chat_id": 1, "type": "task", "_is_direct_chat": True,
            "governing_owner_words": [{"text": "next please", "ref": "chat 1 / m1"}]}
    spec = mv.view_spec_for_task(task, tmp_path)
    snapshot = mv.capture_memory_view(tmp_path, task, spec)
    assert snapshot.active and snapshot.spec.work_room_levels == 1
    assert any(i.get("owner_side") and i.get("immune") for i in snapshot.room["lane1"])
    assert mv.snapshot_from_json(mv.snapshot_json(snapshot)) == snapshot
    before = mv.snapshot_json(snapshot)
    mf.fit_memory_view(snapshot, frame(0, 0))
    assert mv.snapshot_json(snapshot) == before


@pytest.mark.parametrize("governing, immune", [
    ([{"text": "да"}], {"m5"}),
    ([{"text": "всегда проверяй логи"}], {"m4"}),
    ([{"text": "да", "ref": "chat 1 / m3"}], {"m3"}),
    ([{"source": "initial_user"}, {"text": "да"}], {"m5"}),
])
def test_governing_words_protect_rows_by_identity_never_by_substring(tmp_path, governing, immune):
    shared.world(tmp_path)
    words = {"m3": "когда это будет готово?", "m4": "всегда проверяй логи", "m5": "да"}
    shared.append(tmp_path / "logs" / "chat.jsonl", *[shared.msg(f"2026-09-04T00:0{i}:00+00:00", text, client_message_id=key)
                                                      for i, (key, text) in enumerate(words.items())],
                  shared.msg("2026-09-04T00:09:00+00:00", "answered", direction="out", task_id="t4"))
    task = {"id": "turn0002", "chat_id": 1, "type": "task", "_is_direct_chat": True, "governing_owner_words": governing}
    lane1 = mv.capture_memory_view(tmp_path, task, mv.view_spec_for_task(task, tmp_path)).room["lane1"]
    assert {key for key in words for item in lane1 if item["ref"] == f"chat 1 / {key}" and item["immune"]} == immune


@pytest.mark.parametrize("names, expected", [(None, ""), (("memory_read",), ""),
    (("enable_tools",), "reachable through enable_tools"), (("list_available_tools",), "my parent task can read")])
def test_floor_receipt_names_reachable_sources_and_never_promises_lossless_recall(names, expected):
    note = mf.floor_note(mv.FloorLevel((("F5", ("p",)),)), window_tokens=200_000, mode="nano", tool_names=names)
    assert expected in note and "Nothing is lost" not in note and "brings it back" not in note
    assert "memory_read reads each" in note
    assert ("chronicle_write" in note) == (names is None or "enable_tools" in names)
