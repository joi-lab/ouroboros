"""The acceptance-fence TRANSPORT: an answer that has not arrived is a gap.

Direct turns run inside the supervisor process and apply the fence in-process
(no event, no ack file, no wait). Pooled workers keep event + ack, made
idempotent: one token per logical begin, one ``req`` per request, the ack file
is ``<token>.<req>.json``, one re-send, then a typed outcome. The supervisor
re-adopts an ``active`` row of the SAME task with token rebinding, never a
``sealed`` one; a missing row answers ``released`` + ``row_absent`` and the
worker treats only ``sealed`` as a seal.

The pooled tests run the REAL worker seam, the REAL ack writer and the REAL
queue transition with controlled supervisor delivery, loss and suspension.
"""

from __future__ import annotations

import json
import queue as stdqueue
import time
from types import SimpleNamespace

import pytest

from tests.test_acceptance_fence import _isolated_queue

WAIT_SEC = 0.4


def _pooled_agent(tmp_path, events, task_id="root-1"):
    from ouroboros.agent import Env, OuroborosAgent

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    agent = object.__new__(OuroborosAgent)
    agent.env = Env(repo_dir=repo, drive_root=tmp_path)
    agent._current_task_metadata = {}
    agent._current_task_id = task_id
    agent._event_queue = events
    return agent


class _Supervisor:
    """Deliver real fence events at controlled points, independent of scheduling.

    ``drop_events``: the first N events are never applied (a loop that never got
    to them). ``lose_acks``: the first N transitions ARE applied but their ack is
    lost. ``paused``: events queue up until ``resume()`` (a stalled loop).
    """

    def __init__(self, events, drive_root, *, drop_events=0, lose_acks=0, paused=False):
        self.events, self.drive_root = events, drive_root
        self.drop_events, self.lose_acks = drop_events, lose_acks
        self.seen: list = []
        self.paused = paused
        self._put = events.put
        events.put = self._deliver
        if not paused:
            self._drain()  # requests may predate this supervisor

    def resume(self):
        self.paused = False
        self._drain()

    def _deliver(self, evt):
        self._put(evt)
        if not self.paused:
            self._drain()

    def _drain(self):
        from supervisor.events_worker_reports import _handle_acceptance_fence

        while True:
            try:
                evt = self.events.get_nowait()
            except stdqueue.Empty:
                return
            self.seen.append(dict(evt))
            if self.drop_events > 0:
                self.drop_events -= 1
                continue
            _handle_acceptance_fence(evt, SimpleNamespace(DRIVE_ROOT=self.drive_root))
            if self.lose_acks > 0:
                self.lose_acks -= 1
                (self.drive_root / "state" / "acceptance_fence_acks" / f"{evt['token']}.{evt['req']}.json").unlink()

    def drained(self, count):
        return len(self.seen) >= count

    def stop(self):
        if self.events.put == self._deliver:
            self.resume()
            self.events.put = self._put


@pytest.fixture
def short_wait(monkeypatch):
    from ouroboros import runtime_limits

    monkeypatch.setattr(runtime_limits, "get_acceptance_fence_ack_wait_sec", lambda: WAIT_SEC)


def _ack_names(tmp_path):
    ack_dir = tmp_path / "state" / "acceptance_fence_acks"
    return sorted(path.name for path in ack_dir.glob("*.json")) if ack_dir.is_dir() else []


# --- supervisor: idempotent begin, real status, missing row --------------------------------


def test_begin_with_lost_token_readopts_existing_live_fence(monkeypatch, tmp_path):
    """The ack timed out AFTER the supervisor activated the fence: the same task's
    next begin re-adopts the row and REBINDS it, so the dead attempt's token dies."""
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    begun = queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-1", task_id="root-1")
    assert begun["status"] == "active"
    queue_mod.ACCEPTANCE_FENCES["root-1"]["owner_message_generation"] = 2

    readopted = queue_mod.transition_acceptance_fence(
        action="begin", token="b" * 32, root_task_id="root-1", task_id="root-1")
    assert (readopted["ok"], readopted["status"], readopted["token"]) == (True, "active", "b" * 32)
    assert readopted["owner_message_generation"] == 2  # the row survives, only its token moves
    assert queue_mod.ACCEPTANCE_FENCES["root-1"]["token"] == "b" * 32
    assert len(queue_mod.ACCEPTANCE_FENCES) == 1

    # A late event of the dead attempt finds no row: harmless, and it tears nothing down.
    late = queue_mod.transition_acceptance_fence(action="end", token="a" * 32, outcome="revision")
    assert (late["status"], late.get("row_absent")) == ("released", True)
    assert queue_mod.ACCEPTANCE_FENCES["root-1"]["status"] == "active"
    sealed = queue_mod.transition_acceptance_fence(
        action="end", token="b" * 32, outcome="terminal", expected_generation=2)
    assert sealed["status"] == "sealed"


def test_sealed_fence_is_never_readopted(monkeypatch, tmp_path):
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-2", task_id="root-2")
    assert queue_mod.transition_acceptance_fence(
        action="end", token="a" * 32, outcome="terminal")["status"] == "sealed"

    rejected = queue_mod.transition_acceptance_fence(
        action="begin", token="c" * 32, root_task_id="root-2", task_id="root-2")
    assert rejected["ok"] is False
    assert rejected["status"] == "sealed"  # the typed refusal carries the row's real status
    assert "already sealed" in rejected["error"]
    row = queue_mod.ACCEPTANCE_FENCES["root-2"]
    assert (row["status"], row["token"]) == ("sealed", "a" * 32)


def test_begin_by_another_task_is_refused_and_no_dead_owner_is_collected(monkeypatch, tmp_path):
    """Re-adoption is for the SAME task only. The owner here has no RUNNING/PENDING
    row at all — exactly how a LIVE direct root looks to the queue — and its fence
    is still not collected for a different requester."""
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-3", task_id="root-3")

    foreign = queue_mod.transition_acceptance_fence(
        action="begin", token="b" * 32, root_task_id="root-3", task_id="someone-else")
    assert foreign["ok"] is False
    assert queue_mod.ACCEPTANCE_FENCES["root-3"]["token"] == "a" * 32
    assert queue_mod.ACCEPTANCE_FENCES["root-3"]["task_id"] == "root-3"


def test_same_token_begin_answers_the_rows_real_status(monkeypatch, tmp_path):
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    first = queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-4", task_id="root-4")
    again = queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-4", task_id="root-4")
    assert (first["status"], again["status"]) == ("active", "active")
    queue_mod.transition_acceptance_fence(action="end", token="a" * 32, outcome="terminal")
    resent = queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-4", task_id="root-4")
    assert resent["status"] == "sealed"


@pytest.mark.parametrize("action,extra", [("inspect", {}), ("end", {"outcome": "revision"}), ("end", {"outcome": "terminal"})])
def test_missing_row_answers_released_row_absent(monkeypatch, tmp_path, action, extra):
    """Idempotent after ``task_done`` cleared the fence; a present row never says ``row_absent``."""
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    queue_mod.transition_acceptance_fence(
        action="begin", token="a" * 32, root_task_id="root-5", task_id="root-5")
    present = queue_mod.transition_acceptance_fence(action="inspect", token="a" * 32)
    assert present["status"] == "active" and "row_absent" not in present

    assert queue_mod.clear_acceptance_fence_for_root("root-5") is True
    absent = queue_mod.transition_acceptance_fence(action=action, token="a" * 32, **extra)
    assert (absent["ok"], absent["status"], absent["row_absent"]) == (True, "released", True)
    assert queue_mod.ACCEPTANCE_FENCES == {}


# --- ack identity ---------------------------------------------------------------------------


def test_ack_file_is_keyed_by_token_and_request(monkeypatch, tmp_path):
    from supervisor import queue
    from supervisor.events_worker_reports import _handle_acceptance_fence

    applied: list = []
    monkeypatch.setattr(
        queue, "transition_acceptance_fence",
        lambda **kwargs: applied.append(kwargs) or {"ok": True, "status": "active"})
    token, req = "a" * 32, "b" * 32
    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path)
    _handle_acceptance_fence({"token": token, "req": req, "action": "begin", "root_task_id": "r", "task_id": "r"}, ctx)
    assert _ack_names(tmp_path) == [f"{token}.{req}.json"]

    # The token validator covers ``req``: a request id that could leave the ack
    # directory, or none at all, is refused before any transition is applied.
    for bad in ("../escape", "", "z" * 8, "c" * 65):
        _handle_acceptance_fence({"token": token, "req": bad, "action": "inspect"}, ctx)
    assert len(applied) == 1
    assert _ack_names(tmp_path) == [f"{token}.{req}.json"]


def test_acceptance_ack_waiter_ignores_stale_operation_ack(monkeypatch, tmp_path, short_wait):
    """begin/inspect/end share one fence token, so an ack belongs to ONE request. A late
    ack of another request is never consumed as this request's receipt."""
    _isolated_queue(monkeypatch, tmp_path)
    token = "a" * 32
    ack_dir = tmp_path / "state" / "acceptance_fence_acks"
    ack_dir.mkdir(parents=True)
    stale = [ack_dir / f"{token}.json", ack_dir / f"{token}.{'d' * 32}.json"]
    for path in stale:
        path.write_text(json.dumps({"ok": True, "status": "active", "token": token}), encoding="utf-8")

    events: stdqueue.Queue = stdqueue.Queue()
    agent = _pooled_agent(tmp_path, events)
    with pytest.raises(TimeoutError):
        agent._inspect_acceptance_fence(token=token)  # nobody answers THIS request
    assert all(path.exists() for path in stale)

    supervisor = _Supervisor(events, tmp_path)
    try:
        own = agent._end_acceptance_fence(token=token, outcome="revision")
    finally:
        supervisor.stop()
    assert (own["status"], own["row_absent"]) == ("released", True)  # its OWN answer, not a stale ``active``
    # Its own ack is consumed; the stale ones — and the late answer to the timed-out
    # inspect the supervisor has meanwhile caught up with — are read by nobody.
    late_inspect = f"{token}.{supervisor.seen[0]['req']}.json"
    assert supervisor.seen[0]["action"] == "inspect" and supervisor.seen[-1]["action"] == "end"
    assert _ack_names(tmp_path) == sorted([path.name for path in stale] + [late_inspect])


def test_late_ack_of_a_previous_request_is_read_by_nobody(monkeypatch, tmp_path, short_wait):
    """evidence/h8 ``repro_stale_ack``: a timed-out inspect used to poison the NEXT
    operation on the same token — its late ``active`` ack was read as the end's answer."""
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    events: stdqueue.Queue = stdqueue.Queue()
    agent = _pooled_agent(tmp_path, events)
    supervisor = _Supervisor(events, tmp_path)
    try:
        token = agent._begin_acceptance_fence(root_task_id="root-1", task_id="root-1")["token"]
        supervisor.stop()

        supervisor = _Supervisor(events, tmp_path, paused=True)
        with pytest.raises(TimeoutError):
            agent._inspect_acceptance_fence(token=token)
        supervisor.resume()  # the loop catches up and answers the inspect LATE
        assert supervisor.drained(1)  # a read is sent once, never re-sent
        late = _ack_names(tmp_path)
        assert late and all(name.startswith(f"{token}.") for name in late)

        ended = agent._end_acceptance_fence(token=token, outcome="terminal", expected_generation=0)
        assert ended["status"] == "sealed"  # the end's own answer, not the late ``active``
        assert queue_mod.ACCEPTANCE_FENCES["root-1"]["status"] == "sealed"
        assert _ack_names(tmp_path) == late  # the late acks were read by nobody
    finally:
        supervisor.stop()


# --- pooled: one re-send, then a typed outcome ---------------------------------------------


def test_pooled_request_is_resent_once_with_the_same_identity(monkeypatch, tmp_path, short_wait):
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    events: stdqueue.Queue = stdqueue.Queue()
    agent = _pooled_agent(tmp_path, events)
    supervisor = _Supervisor(events, tmp_path, drop_events=1)
    try:
        ack = agent._begin_acceptance_fence(root_task_id="root-1", task_id="root-1")
    finally:
        supervisor.stop()
    assert ack["status"] == "active"
    assert len(supervisor.seen) == 2
    first, second = supervisor.seen
    assert (first["token"], first["req"]) == (second["token"], second["req"]) == (ack["token"], first["req"])
    assert queue_mod.ACCEPTANCE_FENCES["root-1"]["token"] == ack["token"]

    # A transition never answered: exactly ONE re-send, then a TimeoutError — no loop, no long wait.
    silent: stdqueue.Queue = stdqueue.Queue()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _pooled_agent(tmp_path, silent)._end_acceptance_fence(token=ack["token"], outcome="revision")
    assert time.monotonic() - started < WAIT_SEC * 2 + 2.0
    sent = [silent.get_nowait() for _ in range(silent.qsize())]
    assert len(sent) == 2 and sent[0]["req"] == sent[1]["req"]

    # A READ (inspect is asked many times a turn; its loss is harmless) is never re-sent:
    # one wait, one event — a stalled supervisor cannot multiply into minutes of host blocking.
    quiet: stdqueue.Queue = stdqueue.Queue()
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _pooled_agent(tmp_path, quiet)._inspect_acceptance_fence(token=ack["token"])
    assert time.monotonic() - started < WAIT_SEC + 2.0
    assert quiet.qsize() == 1


def test_every_request_carries_a_fresh_req_and_one_token_per_logical_begin(monkeypatch, tmp_path, short_wait):
    _isolated_queue(monkeypatch, tmp_path)
    events: stdqueue.Queue = stdqueue.Queue()
    agent = _pooled_agent(tmp_path, events)
    supervisor = _Supervisor(events, tmp_path)
    try:
        token = agent._begin_acceptance_fence(root_task_id="root-1", task_id="root-1")["token"]
        agent._inspect_acceptance_fence(token=token)
        agent._end_acceptance_fence(token=token, outcome="revision")
    finally:
        supervisor.stop()
    assert [evt["action"] for evt in supervisor.seen] == ["begin", "inspect", "end"]
    assert {evt["token"] for evt in supervisor.seen} == {token}
    assert len({evt["req"] for evt in supervisor.seen}) == 3
    assert _ack_names(tmp_path) == []  # every waiter consumed exactly its own ack


def test_ack_wait_is_a_named_runtime_limit():
    from ouroboros import runtime_limits

    assert runtime_limits.get_acceptance_fence_ack_wait_sec() == 10.0


# --- direct turns: no event, no ack file, no wait ------------------------------------------


def test_direct_turn_uses_no_event_and_no_ack_file(monkeypatch, tmp_path, short_wait):
    queue_mod, _pending = _isolated_queue(monkeypatch, tmp_path)
    events: stdqueue.Queue = stdqueue.Queue()
    direct = _pooled_agent(tmp_path, events, task_id="direct-1")
    direct.fence_transition = queue_mod.transition_acceptance_fence  # what ``_get_chat_agent`` injects
    def forbidden_send(*_args, **_kwargs):
        pytest.fail("a direct fence transition must not send or poll an ACK")

    monkeypatch.setattr(direct, "_send_fence_event", forbidden_send)
    token = direct._begin_acceptance_fence(root_task_id="direct-1", task_id="direct-1")["token"]
    assert queue_mod.ACCEPTANCE_FENCES["direct-1"]["status"] == "active"
    assert direct._inspect_acceptance_fence(token=token)["status"] == "active"
    assert direct._end_acceptance_fence(token=token, outcome="terminal", expected_generation=0)["status"] == "sealed"
    assert events.empty() and _ack_names(tmp_path) == []

    # The other direction: a pooled worker still travels by event + ack.
    pooled_events: stdqueue.Queue = stdqueue.Queue()
    supervisor = _Supervisor(pooled_events, tmp_path)
    try:
        _pooled_agent(tmp_path, pooled_events, task_id="root-9")._begin_acceptance_fence(
            root_task_id="root-9", task_id="root-9")
    finally:
        supervisor.stop()
    assert [evt["type"] for evt in supervisor.seen] == ["acceptance_fence"]


def test_only_the_direct_chat_agent_is_built_with_the_in_process_transition(monkeypatch, tmp_path):
    from ouroboros import agent as agent_module
    from supervisor import queue, workers

    built = SimpleNamespace()
    monkeypatch.setattr(agent_module, "make_agent", lambda **_kwargs: built)
    assert workers._get_chat_agent() is built
    assert built.fence_transition is queue.transition_acceptance_fence
    assert getattr(agent_module.OuroborosAgent, "fence_transition", None) is None  # pooled default
