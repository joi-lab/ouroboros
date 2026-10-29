"""The lost-Max episode: one row, two Main lines through the terminal outbox, one Health line."""
from types import SimpleNamespace

import pytest

from ouroboros import memory_max as mm, store
from supervisor import schedule_notes, terminal_delivery as outbox


def _fact(cause, *, window=1_000_000, levels=2, story=300_000, room=60_000, allowance=320_000, room_id="1",
          reserve=65_536):
    work = -(-window // 8) * levels  # the work room: an eighth of the window per level
    return {"cause": cause, "window": window, "levels": levels, "room_id": room_id, "allowance": allowance,
            "floor_need": 84_464 + reserve + work, "schemas": 40_000, "zero_need": 124_464 + reserve + work,
            "need": 124_464 + reserve + work + story + room, "story": story, "unfolded": 120_000, "room": room,
            "reserve": reserve, "work": work, "folds": 12, "cuts": [],
            "parts": [["books", 170_000], ["knowledge, scratchpad and runtime facts", 60_000], ["core", 40_000],
                      ["assignment", 4_000]]}


def _plan(fact, *, role="integrator", owner="max"):
    facts = {"role": role, **({"max_room": fact} if fact else {})}
    return SimpleNamespace(preferred_mode=owner, projection=lambda _mode: SimpleNamespace(memory_facts=facts))


@pytest.fixture
def lines(monkeypatch):
    sent, state = [], {"delivered": set(), "owed": set()}

    def enqueue(_root, event, event_queue=None):
        sent.append(event)
        state["owed"].add(event["delivery_id"])
        return "queued"

    monkeypatch.setattr(outbox, "enqueue_terminal_delivery_outcome", enqueue)
    monkeypatch.setattr(outbox, "already_delivered", lambda _root, did, **_kw: did in state["delivered"])
    monkeypatch.setattr(outbox, "pending_deliveries", lambda _root, **_kw: [{"delivery_id": d} for d in state["owed"]])
    monkeypatch.setattr(schedule_notes, "owner_chat_id", lambda _root: 1)
    return sent, state


def _row(root):
    with store.open_store(root, write=False) as txn:
        return txn.get_row(mm.SET, mm.IDENTITY)


def _deliver(state, event):
    state["owed"].discard(event["delivery_id"])
    state["delivered"].add(event["delivery_id"])


def test_a_crowded_start_opens_once_and_later_starts_only_update(tmp_path, lines):
    sent, state = lines
    assert mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="root1") == "opened"
    assert len(sent) == 1 and sent[0]["system_type"] == mm.NOTICE_TYPE and sent[0]["role"] == "system"
    assert sent[0]["delivery_id"].endswith(":open") and sent[0]["chat_id"] == 1 and sent[0]["task_id"] == ""
    text = sent[0]["text"]
    assert "Max lost: task root1 started in Low with chapter maps" in text and "tool schemas 40,000" in text
    assert "may bring the books back" in text and "failure" not in text and "crowds the books out" not in text
    assert mm.observe_start(tmp_path, _plan(_fact("crowded", window=872_000, levels=1)), "low", task_id="t2") == "updated"
    row = _row(tmp_path)
    assert row["starts"] == 2 and row["window"] == 872_000 and row["levels"] == 2 and row["last"]["task"] == "t2"
    assert (row["unit"], row["reserve"]) == (109_000, 65_536)  # the smallest lost window's frame
    assert len(sent) == 1  # the open line is still owed by the outbox: no second send
    state["owed"].clear()  # lost before delivery: the next fresh start sends it again under the same id
    mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="t3")
    assert len(sent) == 2 and sent[1]["delivery_id"] == sent[0]["delivery_id"]
    _deliver(state, sent[1])
    mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="t4")
    assert _row(tmp_path)["notices"]["open"]["recorded"] is True and len(sent) == 2


@pytest.mark.parametrize("plan", [_plan(_fact("route")), _plan(_fact("held")), _plan(_fact("crowded"), role="child"),
                                  _plan(_fact("crowded"), role="nanny"), _plan(None), _plan(None, owner="low")])
def test_route_held_helpers_unknown_windows_and_owner_modes_open_nothing(tmp_path, lines, plan):
    assert mm.observe_start(tmp_path, plan, "low", task_id="t") == ""
    assert _row(tmp_path) is None and lines[0] == []


def _opened(tmp_path, lines, **kwargs):
    mm.observe_start(tmp_path, _plan(_fact("crowded", **kwargs)), "low", task_id="first")
    _deliver(lines[1], lines[0][0])


# Lost on 1,000,000 with two work-room levels and a 65,536 reply reserve: memory fits that frame up to 560,000
# (stories below + rooms 60,000).
@pytest.mark.parametrize("window,levels,story,reserve,closes", [
    (1_000_000, 2, 300_000, 65_536, True),
    (1_200_000, 2, 300_000, 65_536, True),    # the route's window was raised since: its need still fits the lost frame
    (1_200_000, 2, 520_000, 65_536, False),   # holds on the raised window, not in the lost one: no saw
    (1_000_000, 1, 300_000, 65_536, True),    # a Main turn closes a root's episode when the root's frame would hold
    (1_000_000, 1, 520_000, 65_536, False),   # fits Main's own eighth, not the root's two
    (1_000_000, 2, 520_000, 16_384, False),   # a smaller reply reserve elsewhere makes the lost route no roomier
])
def test_a_start_closes_only_when_its_need_fits_the_worst_lost_frame(tmp_path, lines, window, levels, story, reserve,
                                                                      closes):
    _opened(tmp_path, lines)
    fact = _fact("held", window=window, levels=levels, story=story, reserve=reserve)
    assert fact["need"] <= window  # each case is a start that held Max in its own frame
    event = mm.observe_start(tmp_path, _plan(fact), "max", task_id="back")
    assert (event == "closed") is closes and bool(_row(tmp_path).get("closed")) is closes
    assert mm.observe_start(tmp_path, _plan(_fact("route")), "low", task_id="route") == ""  # a route closes nothing


def test_a_main_only_installation_closes_in_its_own_eighth_and_the_close_line_retires_the_row(tmp_path, lines, monkeypatch):
    sent, state = lines
    spans = []
    monkeypatch.setattr(mm, "folds_by", lambda _root, since, until: spans.append((since, until)) or {"mind": 2, "helper": 1})
    _opened(tmp_path, lines, levels=1)
    assert mm.observe_start(tmp_path, _plan(_fact("held", levels=1, story=200_000)), "max", task_id="main") == "closed"
    close = sent[-1]
    assert close["delivery_id"].endswith(":close") and "Max holds again: task main started with whole books" in close["text"]
    assert "my story went from 300,000 to 200,000 tokens; parts and accounts written meanwhile: 2 by me, 1 by helpers" in close["text"]
    row = _row(tmp_path)
    assert spans == [(row["opened_at"], row["closed"]["at"])]  # the episode's own span, the same on every retry
    _deliver(state, close)
    mm.observe_start(tmp_path, _plan(_fact("held", levels=1)), "max", task_id="later")
    assert _row(tmp_path) is None  # the remover: the close line is in Main
    assert mm.observe_start(tmp_path, _plan(_fact("crowded", levels=1)), "low", task_id="again") == "opened"


def test_a_new_episode_hands_the_closed_ones_line_over_before_taking_its_row(tmp_path, lines, monkeypatch):
    sent, _state = lines
    _opened(tmp_path, lines)
    publish = mm._publish
    monkeypatch.setattr(mm, "_publish", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("lost before the send")))
    assert mm.observe_start(tmp_path, _plan(_fact("held")), "max", task_id="back") == ""  # closed, its line not sent
    old = _row(tmp_path)
    assert old["closed"] and not old["notices"]["close"]["recorded"] and len(sent) == 1
    monkeypatch.setattr(mm, "_publish", publish)
    assert mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="again") == "opened"
    assert [event["delivery_id"] for event in sent[1:]] == [f"memory_max:{old['id']}:close", f"memory_max:{_row(tmp_path)['id']}:open"]
    assert _row(tmp_path)["id"] != old["id"] and "Max holds again: task back" in sent[1]["text"]


@pytest.mark.parametrize("failure", ["raises", "unregistered"])
def test_a_closed_row_whose_lines_the_outbox_does_not_hold_stays_until_it_does(tmp_path, lines, monkeypatch, failure):
    sent, state = lines
    _opened(tmp_path, lines)
    publish, enqueue = mm._publish, outbox.enqueue_terminal_delivery_outcome
    monkeypatch.setattr(mm, "_publish", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("lost before the send")))
    mm.observe_start(tmp_path, _plan(_fact("held")), "max", task_id="back")
    old = _row(tmp_path)
    monkeypatch.setattr(mm, "_publish", publish)

    def failing(_root, event, event_queue=None):
        if failure == "raises":
            raise RuntimeError("store busy")
        sent.append(event)  # a live send whose owed row was not written
        return "queued_unregistered"

    monkeypatch.setattr(outbox, "enqueue_terminal_delivery_outcome", failing)
    assert mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="again") == ""
    assert _row(tmp_path)["id"] == old["id"] and _row(tmp_path)["closed"]  # the close line is still owed by the row
    monkeypatch.setattr(outbox, "enqueue_terminal_delivery_outcome", enqueue)
    assert mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="later") == "opened"
    close_id = f"memory_max:{old['id']}:close"
    assert close_id in state["owed"] and _row(tmp_path)["id"] != old["id"]


def test_a_child_execution_drive_observes_the_installations_canonical_root(tmp_path, lines):
    canonical, drive = tmp_path / "data", tmp_path / "child-drive"
    canonical.mkdir()
    drive.mkdir()
    child = SimpleNamespace(task_metadata={"budget_drive_root": str(canonical)}, drive_root=drive)
    assert mm.observe_task_start(child, _plan(_fact("crowded")), "low", task_id="t") == "opened"
    assert _row(canonical) is not None and _row(drive) is None
    root = SimpleNamespace(task_metadata={}, drive_root=drive)  # no budget root: the task's own drive
    assert mm.observe_task_start(root, _plan(_fact("crowded")), "low", task_id="t2") == "opened" and _row(drive)


@pytest.mark.parametrize("owner", ["low", "nano"])
def test_an_owner_who_leaves_max_closes_the_episode(tmp_path, lines, owner):
    _opened(tmp_path, lines)
    assert mm.observe_start(tmp_path, _plan(None, owner=owner), owner, task_id="chosen") == "closed"
    assert f"the owner selected {owner.capitalize()} (task chosen)" in lines[0][-1]["text"]
    assert mm.observe_start(tmp_path, _plan(None, owner=owner, role="child"), owner, task_id="c") == ""


def test_health_names_the_open_episode_for_integrating_readers_only(tmp_path, lines):
    assert mm.health_line(tmp_path) == ""
    _opened(tmp_path, lines)
    line = mm.health_line(tmp_path)
    assert line.startswith("WARNING: MAX LOST since ") and "(task first; 1 Low start)" in line
    assert "with no memory shown headroom 560,000" in line and "may bring the books back" in line
    assert all(mm.health_line(tmp_path, role) == "" for role in ("child", "nanny"))
    assert mm.health_line(tmp_path, "consciousness") == line
    mm.observe_start(tmp_path, _plan(_fact("held")), "max", task_id="back")
    assert mm.health_line(tmp_path) == ""


def test_the_health_block_carries_the_line_for_an_integrating_view_role(tmp_path, lines):
    from ouroboros.context_health import build_health_invariants

    env = SimpleNamespace(drive_root=tmp_path, repo_path=lambda rel: tmp_path / "repo" / rel,
                          drive_path=lambda rel: tmp_path / rel)
    _opened(tmp_path, lines)
    assert "MAX LOST" in build_health_invariants(env, view_role="integrator")
    assert "MAX LOST" not in build_health_invariants(env, view_role="child")
    assert "MAX LOST" not in build_health_invariants(env)


def test_an_unreadable_store_never_fails_the_start(tmp_path, lines, monkeypatch):
    monkeypatch.setattr(store, "open_store", lambda *_a, **_kw: (_ for _ in ()).throw(store.StoreBusy("busy")))
    assert mm.observe_start(tmp_path, _plan(_fact("crowded")), "low", task_id="t") == ""
    assert mm.health_line(tmp_path) == "" and lines[0] == []
