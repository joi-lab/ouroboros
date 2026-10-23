"""Own-work sealing, terminal links and the ON/OFF candidate boundary."""
from types import SimpleNamespace

import pytest
from ouroboros import memory_view as mv, memory_fallback as mf, chat_chain
from ouroboros.chronicle_store import ChronicleStore
from ouroboros.memory_sealing import failed_work_units
from ouroboros.task_results import write_task_result
from ouroboros.tools.chronicle import _memory_read
from tests.test_chronicle_tools import chat, ctx_for, ts, addr, sha, write
from tests._memory_inventory_shared import MIND
from tests.test_memory_fallback import _Light, _consciousness, light_route


def _own_rows(root, status="failed"):
    store = ChronicleStore(root)
    store.ensure_activated()
    rows = chat(root, [
        {"chat_id": 1, "direction": "out", "ts": ts(1), "text": "OWN ONE", "task_id": "failed01"},
        {"chat_id": 1, "direction": "out", "ts": ts(2), "text": "FOREIGN CHILD", "task_id": "child001", "parent_task_id": "failed01", "root_task_id": "failed01"},
        {"chat_id": 1, "direction": "out", "ts": ts(3), "text": "OWN TWO", "task_id": "failed01"},
        {"chat_id": 1, "direction": "out", "ts": ts(4), "text": "FOREIGN ROOT", "task_id": "foreign1"},
    ])
    write_task_result(root, "failed01", status, result="Own work result")
    return store, rows


@pytest.mark.parametrize("status", ["failed", "cancelled", "completed", "running"])
def test_candidates_only_cover_finished_unsealed_failures_from_their_own_task(tmp_path, status):
    store, rows = _own_rows(tmp_path, status)
    units = failed_work_units(tmp_path, store)
    assert bool(units) is (status in {"failed", "cancelled"})
    sealed = store.publish_page(room_id="1", text="I already sealed my work.",
        covers={"rows": [sha(rows[0])], "stream_span": [0, 0]}, author={**MIND, "task_id": "failed01"})
    assert sealed.ok
    assert not failed_work_units(tmp_path, store)  # no re-folding a mind-authored task, even with later open rows


@pytest.mark.parametrize("accepted", [True, False])
def test_candidate_is_nonacting_until_adopted_and_rebuild_preserves_decision(tmp_path, monkeypatch, accepted):
    light_route(monkeypatch)
    _consciousness(monkeypatch, True)
    store, rows = _own_rows(tmp_path)
    llm = _Light()
    run = mf.run_fallback_draft(SimpleNamespace(), {"id": "next0001"}, llm, tmp_path / "logs", {})
    assert run.outcome == "published", run
    candidate = store.pending_candidates()[0]
    assert candidate["metadata"]["candidate"] and candidate["status"] == "candidate"
    assert candidate["covers"]["rows"] == [sha(rows[0]), sha(rows[2])]
    assert "OWN ONE" in llm.prompts[0] and "OWN TWO" in llm.prompts[0]
    assert "FOREIGN CHILD" not in llm.prompts[0] and "FOREIGN ROOT" not in llm.prompts[0]
    assert not store.sealed_row_refs("1") and not store.pages_of_room("1")
    assert not failed_work_units(tmp_path, store)  # the same pending rows are not paid for twice
    task = {"id": "wake0001", "usage_category": "consciousness", "chat_id": 1}
    snap = mv.capture_memory_view(tmp_path, task, mv.view_spec_for_task(task, tmp_path))
    assert "Drafts awaiting a decision: 1" in mv.render_room(snap)
    assert candidate["text"] not in mv.render_story(snap)
    ctx = ctx_for(tmp_path)
    assert candidate["id"] in _memory_read(ctx, room_id="candidates")
    assert not store.publish_part(room_id="1", text="Cannot fold pending", member_ids=[candidate["id"]],
                                  author=MIND, expected_sequence=store.room_head("1")).ok
    result = write(ctx, kind="decision", target_id=candidate["id"], accepted=accepted, reason="Read the draft and its sources")
    assert result["ok"], result
    store.index_path.unlink()
    assert not store.pending_candidates()
    assert bool(store.sealed_row_refs("1")) is accepted
    assert bool(store.pages_of_room("1")) is accepted
    assert sha(rows[1]) not in store.sealed_row_refs("1")


def test_candidate_adoption_checks_intervening_sealing_and_ordinary_drafts_still_act(tmp_path):
    store, rows = _own_rows(tmp_path)
    covers = {"rows": [sha(rows[0])], "stream_span": [0, 0]}
    candidate = store.publish_page(room_id="1", text="Pending", covers=covers,
        author={"kind": "helper", "route": "Light"}, metadata={"candidate": True}).record
    acting = store.publish_page(room_id="1", text="Ordinary signed draft", covers=covers,
        author={"kind": "helper", "route": "child"})
    assert acting.ok and store.sealed_row_refs("1") == {sha(rows[0])}
    assert store.decide(candidate["id"], True, MIND, "Adopt").reason == "already_sealed"
    assert store.decide(acting.record["id"], False, MIND, "Withdraw").ok
    assert store.decide(candidate["id"], True, MIND, "Now free").ok


@pytest.mark.parametrize("role", ["root", "child", "nanny"])
def test_end_page_links_only_own_later_terminal_rows_and_to_leaves_no_holes(tmp_path, role):
    ctx, store = ctx_for(tmp_path, "writer01", task_metadata={"delegation_role": "subagent"} if role == "child" else
        {"configured_subagent": {"route": {"kind": "agent_session"}}} if role == "nanny" else {}), ChronicleStore(tmp_path)
    store.ensure_activated()
    rows = chat(tmp_path, [{"chat_id": 1, "direction": "out", "ts": ts(i), "text": f"work {i}",
                           "task_id": "writer01"} for i in range(1, 5)])
    first = write(ctx, kind="page", text="Middle rows", covers={"from": addr(rows[1]), "to": addr(rows[2])})
    last = write(ctx, kind="page", text="I finished; I did not reread external sources.", covers={"to": addr(rows[-1])})
    assert first["ok"] and last["ok"]
    assert store.sealed_row_refs("1") == {sha(row) for row in rows}
    page = store.get(last["node_id"])
    assert page["author"]["focus"]["role"] == role
    before = set(page["covers"]["rows"])
    terminal = chat(tmp_path, [
        {"chat_id": 1, "direction": "out", "ts": ts(20), "text": "Final", "task_id": "writer01", "terminal_origin": "model_final"},
        {"chat_id": 1, "direction": "system", "ts": ts(21), "text": "Done", "task_id": "writer01", "type": "task_summary", "status": "completed"},
        {"chat_id": 1, "direction": "out", "ts": ts(22), "text": "Child final", "task_id": "child001", "root_task_id": "writer01", "terminal_origin": "model_final"},
    ])
    document = _memory_read(ctx, node_id=page["id"])
    assert "Linked task final report of task writer01" in document and "Linked host summary of task writer01" in document
    assert "child001" not in document and "not page coverage" in document
    assert set(store.get(page["id"])["covers"]["rows"]) == before
    assert not {sha(row) for row in terminal} & store.sealed_row_refs("1")


class _CountedReads:
    """One module as memory_sealing sees it, counting the rows its readers return."""

    def __init__(self, module, *readers):
        self._module, self.rows = module, 0
        for name in readers:
            setattr(self, name, self._counted(getattr(module, name)))

    def _counted(self, read):
        def counted(*args, **kwargs):
            rows = list(read(*args, **kwargs))
            self.rows += len(rows)
            return rows
        return counted

    def __getattr__(self, name):
        return getattr(self._module, name)


@pytest.mark.parametrize("writers,foreign", [(2, 30), (6, 120)])
def test_story_links_terminal_rows_once_per_capture_without_reading_room_history(tmp_path, monkeypatch, writers, foreign):
    from ouroboros import dialogue_store, memory_sealing

    ChronicleStore(tmp_path).ensure_activated()
    ids, pages, expected = [f"writer{i:02d}" for i in range(writers)], {}, {}
    for i, writer in enumerate(ids):
        work = chat(tmp_path, [{"chat_id": 1, "direction": "out", "ts": ts(10 + i), "text": f"work {i}", "task_id": writer}])[0]
        page = write(ctx_for(tmp_path, writer), kind="page", text=f"Page of {writer}", covers={"from": addr(work), "to": addr(work)})
        assert page["ok"], page
        pages[writer] = page["node_id"]
    chat(tmp_path, [{"chat_id": 1, "direction": "out", "ts": ts(100 + n), "text": f"foreign {n}", "task_id": "foreign1"}
                    for n in range(foreign)])
    chat(tmp_path, [{"chat_id": 1, "direction": "out", "ts": ts(400), "text": "Foreign final", "task_id": "foreign1",
                     "terminal_origin": "model_final"}])
    for i, writer in enumerate(ids):
        final, summary = chat(tmp_path, [
            {"chat_id": 1, "direction": "out", "ts": ts(500 + 2 * i), "text": "Final", "task_id": writer, "terminal_origin": "model_final"},
            {"chat_id": 1, "direction": "system", "ts": ts(501 + 2 * i), "text": "Done", "task_id": writer, "type": "task_summary",
             "status": "completed"}])
        expected[writer] = [
            f"- Linked task final report of task {writer} (status not recorded); not page coverage; "
            f"memory_read(room_id='1', rows=true, from='{addr(final)}', to='{addr(final)}')",
            f"- Linked host summary of task {writer} (completed); not page coverage; "
            f"memory_read(room_id='1', rows=true, from='{addr(summary)}', to='{addr(summary)}')"]
    chain = _CountedReads(chat_chain, "iter_room_rows", "iter_rows", "_addressed")
    dialogue = _CountedReads(dialogue_store, "task_rows", "iter_rows")
    monkeypatch.setattr(memory_sealing, "chat_chain", chain)
    monkeypatch.setattr(memory_sealing, "dialogue_store", dialogue, raising=False)
    task = {"id": "next0001", "chat_id": 1}
    story = mv.render_story(mv.capture_memory_view(tmp_path, task, mv.view_spec_for_task(task, tmp_path)))
    for writer in ids:
        block = story.split(f" · page {pages[writer]}\n", 1)[1].split("\n### ", 1)[0]
        assert [line for line in block.splitlines() if line.startswith("- Linked")] == expected[writer]
    assert "foreign1" not in story
    # The capture reads the writers' own rows once; the room's foreign history is never visited.
    assert chain.rows == 2 * writers
    assert 2 * writers <= dialogue.rows <= 3 * writers
