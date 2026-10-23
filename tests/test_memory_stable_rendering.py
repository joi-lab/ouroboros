"""Source revisions and selected forms, rather than start allowances, determine memory bytes."""
import dataclasses
import json
from types import SimpleNamespace

from ouroboros import context_fit as cf, memory_view as mv, memory_floor as mf
from tests.test_memory_view_lever import plan


def test_two_production_starts_keep_story_and_authored_note_bytes(monkeypatch):
    original, snap = plan()
    note = {"id": "note-one", "date": "2026-10-22", "role": "root", "text": "A durable thought.\nIts reason."}
    snap = dataclasses.replace(snap, room={**snap.room, "notes": [note]})
    core = dataclasses.replace(original.core, memory_view_json=mv.snapshot_json(snap))
    monkeypatch.setattr(cf, "_route_calibration_ratio", lambda *_a, **_kw: 1.0)
    def start(window, source=core):
        return cf.build_context_fit_plan(None, source, {"id": "story001", "type": "task"}, preferred_mode="max",
            route_resolver=lambda *_a, **_kw: ({"model": "test", "provider": "test"},
                SimpleNamespace(route_fp="stable-story", status="confirmed", stale=False, window_tokens=window)))
    small, large = start(200_000), start(1_000_000)
    blocks = lambda p: json.loads(p.max_projection.system_content_json)
    assert blocks(small) == blocks(large)
    assert "A durable thought." in json.dumps(blocks(small))
    changed = dataclasses.replace(snap, room={**snap.room, "notes": [{**note, "text": "A corrected thought."}]})
    revised = start(200_000, dataclasses.replace(core, memory_view_json=mv.snapshot_json(changed)))
    assert blocks(revised) != blocks(small)
    assert blocks(revised)[:-1] == blocks(small)[:-1]  # only the authored room note changed


def test_selecting_a_later_entry_does_not_reorder_or_rewrite_the_earlier_story():
    original, snap = plan()
    whole = mv.render_story(snap)
    selected = mf.select_view(original, "max", ["F5:page-001"])
    level = mf.kept_level(snap, selected.max_projection.memory_facts)
    short = mv.render_story(snap, level)
    assert whole.index("page-000") < whole.index("page-001")
    assert short.index("page-000") < short.index("page-001")
    assert "Page 0." in short and "Page 1." not in short
    prefix = whole[:whole.index("page-001")].rpartition("\n###")[0]
    assert short.startswith(prefix)
    revised = dataclasses.replace(snap, story=({**snap.story[0], "text": "Revised source"}, snap.story[1]))
    assert mv.render_story(revised, level) != short
    assert "Revised source" in mv.render_story(revised, level)
