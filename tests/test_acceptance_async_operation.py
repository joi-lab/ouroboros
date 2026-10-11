"""One frozen acceptance operation survives owner input and free collection."""

import dataclasses
import json
import threading
import time
from types import SimpleNamespace

import pytest

from ouroboros.loop_acceptance_review import acceptance_run_pending
from ouroboros.review_dispatch import collect_task_acceptance_run
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request


@pytest.mark.parametrize("cold", [False, True])
def test_released_acceptance_collects_exact_producer_without_another_send(tmp_path, monkeypatch, cold):
    from ouroboros.review_custody import _ACTIVE, _ACTIVE_LOCK, _attempt_key
    from ouroboros.owner_mailbox import OwnerMailboxPeek

    entered, release, settled = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original_settle = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original_settle(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)

    class HeldModel:
        def chat(self, **kwargs):
            calls.append(kwargs)
            entered.set()
            assert release.wait(10), "fixture did not release its model"
            return {"content": json.dumps({"verdict": "FAIL", "summary": "Keep this actual criticism",
                                           "findings": []})}, {"prompt_tokens": 5, "completion_tokens": 2}

    ctx = SimpleNamespace(task_id="acceptance-root", task_attempt=1, drive_root=tmp_path,
                          budget_drive_root=tmp_path, task_metadata={}, pending_events=[], event_queue=None)
    request = ReviewRequest(surface="task_acceptance", task_id=ctx.task_id, goal="original goal",
                            subject="complete original result", evidence={"requirement": "exact original"},
                            retry_key="acceptance-subject-one", drain_deadline=time.monotonic())
    slot = ReviewSlot(slot_id="one", model="model/original", effort="high", timeout_sec=20)
    try:
        first = run_review_request(request, slots=[slot], drive_root=tmp_path, usage_ctx=ctx, llm=HeldModel())
        assert entered.wait(5)
        assert acceptance_run_pending(first)
        frozen = json.loads(json.dumps(dataclasses.asdict(first)))
        assert frozen["slot_roster"][0]["model"] == "model/original"
        assert frozen["request"]["subject"] == "complete original result"
        # Main remains independent of the frozen worker input.
        ctx.messages = [{"role": "user", "content": "How is it going?"}]
        ctx._owner_directives = [{"content": "How is it going?"}]
        still_running = collect_task_acceptance_run(frozen, drive_root=tmp_path, usage_ctx=ctx)
        assert acceptance_run_pending(still_running)
        assert len(calls) == 1
        release.set()
        assert settled.wait(5)
        assert OwnerMailboxPeek().pending(tmp_path, ctx.task_id, set(), 1)
        with _ACTIVE_LOCK:
            assert _attempt_key(request, slot) not in _ACTIVE
        if cold:
            ctx = SimpleNamespace(task_id=ctx.task_id, task_attempt=1, drive_root=tmp_path,
                                  budget_drive_root=tmp_path, task_metadata={}, pending_events=[], event_queue=None)
        result = collect_task_acceptance_run(frozen, drive_root=tmp_path, usage_ctx=ctx)
        assert not acceptance_run_pending(result)
        assert result.actors[0]["parsed"]["verdict"] == "FAIL"
        assert result.actors[0]["operation_id"] == first.actors[0]["operation_id"]
        assert result.request["subject"] == "complete original result"
        assert result.request["evidence"] == {"requirement": "exact original"}
        assert len(calls) == 1
    finally:
        release.set()
        assert settled.wait(5)


def test_missing_recorded_roster_does_not_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.review_substrate.run_review_request",
                        lambda *a, **k: pytest.fail("missing source bought another review"))
    request = dataclasses.asdict(ReviewRequest(surface="task_acceptance", goal="g", retry_key="subject"))
    with pytest.raises(ValueError, match="roster is unavailable"):
        collect_task_acceptance_run({"request": request}, drive_root=tmp_path, usage_ctx=SimpleNamespace())


def test_review_park_is_not_a_question_and_preserves_operation(tmp_path):
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.owner_wait import wait_after_tools
    from tests.test_owner_wait import context

    ctx, captured = context(tmp_path), []
    ctx._owner_wait_requested = ""
    ctx._task_acceptance_pending = "binding-original"
    ctx.owner_wait_callback = lambda owner, checkpoint: captured.append(checkpoint)
    trace = {"review_runs": [{"binding_hash": "binding-original", "request": {"subject": "full result"}}]}
    wait_after_tools(ctx, [], trace, {}, 3, [], set(), review_binding="binding-original")
    assert len(captured) == 1
    assert captured[0]["quiz_id"] == ""
    assert captured[0]["reason"] == "review"
    source = json.loads(read_actor_source_bytes(tmp_path, ctx.task_id, captured[0]["source_ref"]))
    assert source["acceptance"]["_task_acceptance_pending"] == "binding-original"
    assert source["trace"] == trace


def test_unknown_custody_is_not_an_active_wait():
    assert not acceptance_run_pending({"actors": [{"operation_state": "custody_lost", "late_result_pending": True}]})
    assert acceptance_run_pending({"actors": [{"operation_state": "pending_dispatch"}]})


def _host_run(**overrides):
    run = {"authority": "host_root", "request": {"surface": "task_acceptance", "retry_key": "subject"},
           "slot_roster": [{"slot_id": "one", "model": "m", "route": "api_chat"}],
           "actors": [{"operation_state": "pending_dispatch"}]}
    run.update(overrides)
    return run


def test_a_settled_acceptance_run_is_never_collected_twice(tmp_path, monkeypatch):
    """``acceptance_run_pending`` is the whole idempotency guard: a settled,
    custody-lost or agent-tool run is never re-read, so no marker field exists."""
    from ouroboros import review_dispatch

    monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run",
                        lambda *a, **k: pytest.fail("a run that is not pending was collected again"))
    trace = {"review_runs": [
        _host_run(actors=[{"operation_state": "settled", "parsed": {"verdict": "PASS"}}]),
        _host_run(actors=[{"operation_state": "custody_lost", "late_result_pending": True}]),
        _host_run(authority="agent_tool"),
        _host_run(slot_roster=[]),
        _host_run(request=None),
    ]}
    advanced = review_dispatch.reconcile_pending_acceptance_runs(
        trace, drive_root=tmp_path, usage_ctx=SimpleNamespace(),
    )
    assert advanced == 0


def test_an_uncollectable_pending_run_is_left_alone_and_never_raises(tmp_path, monkeypatch):
    """Fail-soft like ``plan_review_collect.collect_before_gate``: the pass
    continues, the run stays pending, and nothing dispatches."""
    from ouroboros import review_dispatch

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request",
                        lambda *a, **k: pytest.fail("a stranded run bought another review"))
    for error in (ValueError("recorded acceptance roster is unavailable"),
                  KeyError("request"), OSError("custody store unavailable"), TimeoutError("slow")):
        def raising(*_a, _error=error, **_k):
            raise _error

        monkeypatch.setattr(review_dispatch, "collect_task_acceptance_run", raising)
        pending = _host_run()
        advanced = review_dispatch.reconcile_pending_acceptance_runs(
            {"review_runs": [pending]}, drive_root=tmp_path, usage_ctx=SimpleNamespace(),
        )
        assert advanced == 0 and acceptance_run_pending(pending)


def _mailbox_rows(root, task_id):
    from ouroboros.owner_mailbox import _mailbox_path

    path = _mailbox_path(root, task_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_settlement_wake_carries_the_reviewers_own_verdicts(tmp_path):
    """A review is advice for its author (owner D4=A): the wake IS the advice,
    not a pointer to a collect verb the model reaches only by not moving on."""
    from ouroboros.acceptance_settlement import announce_acceptance_settlement

    announce_acceptance_settlement(
        SimpleNamespace(drive_root=tmp_path),
        SimpleNamespace(retry_key="acceptance-subject-one", task_id="root"),
        {"slots": {"one": "ok", "two": ""}, "total": 2,
         "verdicts": {"one": {"verdict": "PASS", "note": "Budget section is complete."}}},
    )
    rows = _mailbox_rows(tmp_path, "root")
    assert len(rows) == 1 and rows[0]["provenance"] == "system"
    text = rows[0]["text"]
    assert "acceptance-subject-one" in text and "1 of 2 reviewer slot(s)" in text
    assert "advice for you, not a signature" in text
    assert "- one: PASS — Budget section is complete." in text and "- two: pending" in text
    assert "keep control" not in text


class _SlotModel:
    """One chat per roster slot, each held behind its own event."""

    def __init__(self, gates, verdicts):
        self.gates, self.verdicts, self.calls = gates, verdicts, []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        model = str(kwargs.get("model") or "")
        assert self.gates[model].wait(10), f"fixture did not release {model}"
        return ({"content": json.dumps({"verdict": self.verdicts[model], "findings": [],
                                        "summary": f"{model} says {self.verdicts[model]}"})},
                {"prompt_tokens": 5, "completion_tokens": 2})


def _released_wave(tmp_path, ctx, *, slots, model, min_successful_slots=1, task_id="root",
                   retry_key="acceptance-subject-one"):
    request = ReviewRequest(surface="task_acceptance", task_id=task_id, goal="goal",
                            subject="complete result", evidence={"requirement": "exact"},
                            policy={"min_successful_slots": min_successful_slots},
                            retry_key=retry_key, drain_deadline=time.monotonic())
    return run_review_request(request, slots=slots, drive_root=tmp_path, usage_ctx=ctx, llm=model)


def test_the_quorum_wake_carries_the_reviewers_verdicts_before_the_last_slot_settles(tmp_path, monkeypatch):
    """Fable roast round 1 / owner D4=A: a two-slot wave with quorum 1 wakes Main
    when the first reviewer answers, then again when the straggler settles; the
    straggler's wake lists every slot."""
    settled = threading.Condition()
    count = {"n": 0}
    original_settle = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original_settle(*args, **kwargs)
        finally:
            with settled:
                count["n"] += 1
                settled.notify_all()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)
    gates = {"model/a": threading.Event(), "model/b": threading.Event()}
    model = _SlotModel(gates, {"model/a": "PASS", "model/b": "FAIL"})
    ctx = SimpleNamespace(task_id="root", task_attempt=1, drive_root=tmp_path, budget_drive_root=tmp_path,
                          task_metadata={}, pending_events=[], event_queue=None)
    slots = [ReviewSlot(slot_id="a", model="model/a", effort="high", timeout_sec=20),
             ReviewSlot(slot_id="b", model="model/b", effort="high", timeout_sec=20)]
    try:
        first = _released_wave(tmp_path, ctx, slots=slots, model=model)
        assert acceptance_run_pending(first) and _mailbox_rows(tmp_path, "root") == []
        gates["model/a"].set()
        with settled:
            assert settled.wait_for(lambda: count["n"] >= 1, timeout=10)
        rows = _mailbox_rows(tmp_path, "root")
        assert len(rows) == 1, rows
        assert "1 of 2 reviewer slot(s)" in rows[0]["text"]
        assert "- a: PASS — model/a says PASS" in rows[0]["text"] and "- b: pending" in rows[0]["text"]
        gates["model/b"].set()
        with settled:
            assert settled.wait_for(lambda: count["n"] >= 2, timeout=10)
        rows = _mailbox_rows(tmp_path, "root")
        assert len(rows) == 2, rows
        assert "2 of 2 reviewer slot(s)" in rows[1]["text"]
        assert "- b: FAIL — model/b says FAIL" in rows[1]["text"]
        assert len(model.calls) == 2, "the wakes bought nothing"
    finally:
        for gate in gates.values():
            gate.set()
        with settled:
            settled.wait_for(lambda: count["n"] >= 2, timeout=10)


def _terminal_ctx(tmp_path, *, task_id, retry_key="acceptance-subject-one"):
    """A worker context whose task already ended, with the wave's run in its trace."""
    import queue

    from ouroboros.task_results import write_task_result

    write_task_result(tmp_path, task_id, "completed", chat_id=1, result="The delivered answer.")
    return SimpleNamespace(task_id=task_id, task_attempt=1, drive_root=tmp_path, budget_drive_root=tmp_path,
                           task_metadata={}, pending_events=[], event_queue=queue.Queue(),
                           _execution_trace={"review_runs": []}, _retry_key=retry_key)


def test_a_panel_that_settles_after_the_task_ended_is_attached_and_announced_once(tmp_path, monkeypatch):
    """Owner fork 2=A + 3=A: one System row in the task's room, whatever the
    verdict, placed inside the card's Reviews group; the projection reads the
    collected verdicts and carries the same sentence the row does; the panel
    that settled before the terminal carries none; nothing wakes a model."""
    from ouroboros.task_results import load_task_result

    settled = threading.Event()
    original_settle = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original_settle(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)
    gates = {"model/a": threading.Event()}
    model = _SlotModel(gates, {"model/a": "PASS"})
    ctx = _terminal_ctx(tmp_path, task_id="late-root")
    slot = ReviewSlot(slot_id="a", model="model/a", effort="high", timeout_sec=20)
    try:
        first = _released_wave(tmp_path, ctx, slots=[slot], model=model, task_id="late-root")
        assert acceptance_run_pending(first)
        run = {**json.loads(json.dumps(dataclasses.asdict(first))), "authority": "host_root",
               "panel_id": "panel_1", "binding_hash": "binding-one", "candidate_hash": "c1",
               "superseded_by_revision": True}
        ctx._execution_trace["review_runs"].append(run)
        # A sibling panel of the same task that settled while the task was still
        # alive: it is republished beside the late one and must stay unstamped.
        ctx._execution_trace["review_runs"].append(_host_run(
            request={"surface": "task_acceptance", "retry_key": "settled-in-time"},
            panel_id="panel_in_time", aggregate_signal="PASS",
            actors=[{"operation_state": "settled", "parsed": {"verdict": "PASS"}}]))
        gates["model/a"].set()
        assert settled.wait(10)
        events = []
        while not any(e.get("system_type") == "acceptance_late_settlement" for e in events):
            events.append(ctx.event_queue.get(timeout=10))
    finally:
        gates["model/a"].set()
        assert settled.wait(10)
    assert _mailbox_rows(tmp_path, "late-root") == [], "a terminal task has nobody to wake"
    rows = [e for e in events if e.get("system_type") == "acceptance_late_settlement"]
    # Exactly one chat row; the other queue entries are the typed operation facts
    # every settlement emits, never a second message and never a model turn.
    assert len(rows) == 1 and [e for e in events if e.get("type") == "send_message"] == rows, events
    event = rows[0]
    assert event["type"] == "send_message" and event["role"] == "system"
    assert event["chat_id"] == 1 and event["task_id"] == "late-root"
    # Version first, then the verdict. The trace recorded a rewrite, but a
    # supersession is not delivery proof: with no send receipt the version is unknown.
    assert event["text"].startswith("On the reviewed version of this answer (whether it was the delivered one "
                                    "is unknown), reviewers later passed it.")
    # The owner row names the reviewer by model and says the verdict in words;
    # the slot id and the token stay in the typed evidence (#1369).
    assert "- model/a (requested): passed it — model/a says PASS" in event["text"]
    assert "- a: PASS" not in event["text"]
    assert event["delivery_id"] == "acceptance-late:acceptance-subject-one"
    assert {key: event["progress_meta"][key] for key in ("card_row", "card_row_id")} == {
        "card_row": "reviews", "card_row_id": "acceptance-late:acceptance-subject-one"}
    assert not acceptance_run_pending(run) and run["actors"][0]["parsed"]["verdict"] == "PASS"
    stored = load_task_result(tmp_path, "late-root")
    assert stored["status"] == "completed", "the supplement never moves a terminal status"
    panels = stored["review_projection"]["panels"]
    actor = panels[0]["actors"][0]
    assert actor["transport_status"] == "success" and actor["parse_status"] == "valid"
    # The panel of THIS wave carries the host's sentence verbatim; the sibling
    # that settled in time carries no settlement at all.
    assert len(panels) == 2 and panels[1]["panel_id"] == "panel_in_time"
    late = panels[0]["late_settlement"]
    assert {key: late[key] for key in ("note", "reviewed_revision", "settled_after_terminal")} == {
        "note": event["text"], "reviewed_revision": "unknown", "settled_after_terminal": True}
    assert late["reviewed_superseded"] is True, "the recorded rewrite stays its own fact"
    # Neutral evidence: when it settled, which exact subject was read, and what the host
    # can prove it emitted (nothing here: no delivery record, so unknown, never inferred).
    assert late["settled_at"] and late["reviewed_subject"]["retry_key"] == "acceptance-subject-one"
    assert late["emitted_answer"]["state"] == "unknown" and late["reviewed_is_emitted"] is None
    assert [row["slot_id"] for row in late["reviewer_outputs"]] == ["a"]
    pointer = event["progress_meta"]["late_evidence"]
    assert pointer["settled_at"] == late["settled_at"] and pointer["panel_id"] == "panel_1"
    assert pointer["source_ref"] == panels[0]["applied_source_ref"] and pointer["reviewed_is_emitted"] is None
    assert "late_settlement" not in panels[1]
    assert len(model.calls) == 1, "collection is free"
    # A second settlement of the same wave finds nothing to reconcile, announces
    # nothing and stamps nothing new.
    from ouroboros.acceptance_settlement import attach_late_acceptance_settlement

    before = json.dumps(stored["review_projection"], sort_keys=True)
    assert attach_late_acceptance_settlement(
        ctx, SimpleNamespace(retry_key="acceptance-subject-one", task_id="late-root"),
        {"slots": {"a": "ok"}, "total": 1}, result=stored) is False
    assert ctx.event_queue.empty()
    assert json.dumps(load_task_result(tmp_path, "late-root")["review_projection"], sort_keys=True) == before
    # The settled verdict reaches the learning log exactly once: one bounded row from the
    # announced settlement, none from the replay that announced nothing.
    rows = [json.loads(line) for line in (tmp_path / "logs" / "task_reflections.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]
    late_rows = [row for row in rows if row.get("type") == "acceptance_late_settlement"]
    assert len(late_rows) == 1 and late_rows[0]["task_id"] == "late-root"
    assert late_rows[0]["supplement_id"] == "acceptance-late:acceptance-subject-one"
    assert "reviewers later passed it" in late_rows[0]["reflection"]


def test_the_late_row_never_reports_a_reviewer_whose_outcome_is_unknown_as_answered():
    """Only PASS and FAIL are verdicts. Anything else is a panel that reached no
    quorum, and a slot whose physical outcome the host does not know is named as
    unknown rather than read as silence."""
    from ouroboros.acceptance_settlement import _late_settlement_text

    wave = {"slots": {"a": "ok", "b": "ok", "c": ""},
            "verdicts": {"a": {"verdict": "PASS"}, "b": {"verdict": "DEGRADED"}}}
    incident = {"aggregate_signal": "DEGRADED", "actors": [
        {"slot_id": "a", "operation_state": "settled", "parsed": {"verdict": "PASS"}},
        {"slot_id": "b", "operation_state": "settled", "parsed": {"verdict": "DEGRADED"}},
        {"slot_id": "c", "operation_state": "custody_lost", "late_result_pending": True}]}
    text = _late_settlement_text(incident, wave)
    unknown_version = "On the reviewed version of this answer (whether it was the delivered one is unknown), "
    assert text.startswith(unknown_version + "reviewers later returned no settled verdict — 1 reviewer's outcome "
                           "is still unknown.")
    assert "no quorum" not in text
    # Owner wording (#1369): a settled PASS, a reviewer's own DEGRADED and an
    # unanswered seat read as words; with no roster identity the reviewer is unknown.
    assert "- unknown reviewer (seat 1): passed it" in text
    assert "- unknown reviewer (seat 2): inconclusive" in text
    assert "- unknown reviewer (seat 3): still awaited" in text
    assert "PASS" not in text.split("\n", 1)[1] and "DEGRADED" not in text and "pending" not in text
    assert "— 2 reviewers' outcomes are still unknown." in _late_settlement_text(
        {**incident, "actors": [incident["actors"][2], {"operation_state": "pending_dispatch"}]}, wave)
    answered = {**incident, "actors": incident["actors"][:2]}
    assert _late_settlement_text(answered, wave).startswith(
        unknown_version + "reviewers later returned no settled verdict.")
    assert _late_settlement_text({**answered, "aggregate_signal": "PASS"}, wave).startswith(
        unknown_version + "reviewers later passed it.")
    assert _late_settlement_text(
        {**answered, "aggregate_signal": "FAIL", "superseded_by_revision": True}, wave).startswith(
        unknown_version + "reviewers later rejected it."), "a recorded rewrite proves no delivered bytes"
    assert _late_settlement_text({**answered, "aggregate_signal": "FAIL"}, wave,
                                 {"reviewed_revision": "different"}).startswith(
        "On a version of this answer other than the one delivered, reviewers later rejected it.")
    assert _late_settlement_text({**answered, "aggregate_signal": "PASS"}, wave,
                                 {"reviewed_revision": "delivered"}).startswith(
        "On the delivered version of this answer, reviewers later passed it.")


def test_a_terminal_task_gets_one_row_at_completion_not_at_quorum(tmp_path, monkeypatch):
    """Fable review round 2: a quorum settlement on an already-terminal task must
    not announce a half-settled wave (the straggler's verdict would never reach
    the chat, deduped behind the same delivery id). Nothing is reconciled until
    every slot answered, so the one row carries every reviewer."""
    settled = threading.Condition()
    count = {"n": 0}
    original_settle = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original_settle(*args, **kwargs)
        finally:
            with settled:
                count["n"] += 1
                settled.notify_all()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)
    gates = {"model/a": threading.Event(), "model/b": threading.Event()}
    model = _SlotModel(gates, {"model/a": "PASS", "model/b": "PASS"})
    ctx = _terminal_ctx(tmp_path, task_id="late-two")
    slots = [ReviewSlot(slot_id="a", model="model/a", effort="high", timeout_sec=20),
             ReviewSlot(slot_id="b", model="model/b", effort="high", timeout_sec=20)]
    try:
        first = _released_wave(tmp_path, ctx, slots=slots, model=model, task_id="late-two")
        run = {**json.loads(json.dumps(dataclasses.asdict(first))), "authority": "host_root",
               "panel_id": "panel_1", "binding_hash": "binding-one", "candidate_hash": "c1"}
        ctx._execution_trace["review_runs"].append(run)
        gates["model/a"].set()
        with settled:
            assert settled.wait_for(lambda: count["n"] >= 1, timeout=10)
        # The late row is enqueued inside the settle (before the wrapper counts), so this is deterministic.
        assert not [e for e in list(ctx.event_queue.queue) if e.get("system_type") == "acceptance_late_settlement"]
        gates["model/b"].set()
        with settled:
            assert settled.wait_for(lambda: count["n"] >= 2, timeout=10)
        events = []
        while not any(e.get("system_type") == "acceptance_late_settlement" for e in events):
            events.append(ctx.event_queue.get(timeout=10))
    finally:
        for gate in gates.values():
            gate.set()
        with settled:
            settled.wait_for(lambda: count["n"] >= 2, timeout=10)
    rows = [e for e in events if e.get("system_type") == "acceptance_late_settlement"]
    assert len(rows) == 1
    assert "- model/a (requested): passed it" in rows[0]["text"] and "- model/b (requested): passed it" in rows[0]["text"]
    assert "still awaited" not in rows[0]["text"] and _mailbox_rows(tmp_path, "late-two") == []


def test_two_late_panels_of_one_task_each_announce_their_own_row(tmp_path, monkeypatch):
    """Astra review round 4: a settlement reconciles only its own wave. Collecting
    every pending panel would let the first callback swallow the sibling's verdicts
    and leave that panel's own callback with nothing to announce."""
    settled = threading.Condition()
    count = {"n": 0}
    original_settle = __import__("ouroboros.review_custody", fromlist=["_settle_review_attempt"])._settle_review_attempt

    def settle(*args, **kwargs):
        try:
            return original_settle(*args, **kwargs)
        finally:
            with settled:
                count["n"] += 1
                settled.notify_all()

    monkeypatch.setattr("ouroboros.review_custody._settle_review_attempt", settle)
    # Both settlements are PUBLISHED (custody's lock block ran, the settled attempt
    # is collectible) before EITHER announcement runs, so the two announcements
    # race exactly as two panels settling together would; a broken barrier fails
    # the test instead of degrading it to a serial schedule.
    barrier = threading.Barrier(2, timeout=10)
    broken = []
    from ouroboros import acceptance_settlement as leaf
    original_announce = leaf.announce_acceptance_settlement

    def announce(*args, **kwargs):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            broken.append(True)
        return original_announce(*args, **kwargs)

    monkeypatch.setattr(leaf, "announce_acceptance_settlement", announce)
    gates = {"model/a": threading.Event(), "model/b": threading.Event()}
    model = _SlotModel(gates, {"model/a": "PASS", "model/b": "FAIL"})
    ctx = _terminal_ctx(tmp_path, task_id="late-pair")
    try:
        for key, slot_model in (("wave-one", "model/a"), ("wave-two", "model/b")):
            slot = ReviewSlot(slot_id=key, model=slot_model, effort="high", timeout_sec=20)
            first = _released_wave(tmp_path, ctx, slots=[slot], model=model, task_id="late-pair", retry_key=key)
            ctx._execution_trace["review_runs"].append(
                {**json.loads(json.dumps(dataclasses.asdict(first))), "authority": "host_root",
                 "panel_id": f"panel_{key}", "binding_hash": f"binding-{key}", "candidate_hash": f"c-{key}"})
        gates["model/a"].set()
        gates["model/b"].set()
        with settled:
            assert settled.wait_for(lambda: count["n"] >= 2, timeout=15)
        events = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and sum(1 for e in events if e.get("system_type") == "acceptance_late_settlement") < 2:
            try:
                events.append(ctx.event_queue.get(timeout=2))
            except Exception:
                break
    finally:
        for gate in gates.values():
            gate.set()
        with settled:
            settled.wait_for(lambda: count["n"] >= 2, timeout=10)
    rows = [e for e in events if e.get("system_type") == "acceptance_late_settlement"]
    assert not broken, "both announcements must have raced through the barrier"
    assert sorted(r["delivery_id"] for r in rows) == ["acceptance-late:wave-one", "acceptance-late:wave-two"], events
    assert any("passed" in r["text"] for r in rows) and any("rejected" in r["text"] for r in rows)


def test_an_owner_followup_sets_the_running_panel_aside(tmp_path):
    """Fable review round 4: after the owner changes the requirements, the answer
    Main writes for them is not a delivery under the panel that reviewed the old
    ones — the latch clears, so the ordinary path decides while the old panel's
    verdicts still arrive as advice."""
    from ouroboros import loop
    from tests.test_delivery_forced_finalization import _forced_test_context

    _loop, registry, _ctx, trace = _forced_test_context(tmp_path)
    registry._ctx._task_acceptance_pending = "binding-one"
    trace["review_decision"] = {}
    loop._supersede_task_acceptance_for_owner_followup(registry._ctx, trace)
    assert registry._ctx._task_acceptance_pending == ""
    assert trace["acceptance_decision"]["reason"] == "owner_followup"


def test_a_late_settlement_for_another_task_is_never_published(tmp_path):
    """The worker may already be rebound: a wave whose retry key is not in the
    live trace returns False and writes nothing anywhere."""
    from ouroboros.acceptance_settlement import attach_late_acceptance_settlement
    from ouroboros.task_results import load_task_result

    ctx = _terminal_ctx(tmp_path, task_id="other-root")
    ctx._execution_trace["review_runs"].append(_host_run(request={"surface": "task_acceptance", "retry_key": "another"}))
    before = json.dumps(load_task_result(tmp_path, "other-root"), sort_keys=True)
    assert attach_late_acceptance_settlement(
        ctx, SimpleNamespace(retry_key="acceptance-subject-one", task_id="other-root"),
        {"slots": {"a": "ok"}, "total": 1}, result={"chat_id": 1}) is False
    assert ctx.event_queue.empty() and _mailbox_rows(tmp_path, "other-root") == []
    assert json.dumps(load_task_result(tmp_path, "other-root"), sort_keys=True) == before


def test_every_acceptance_wake_reoffers_the_changed_answer_selector(tmp_path, monkeypatch):
    """A replacement candidate inherits ``control_episode_seen``; the contract for
    the NEW candidate must still be shown, while identical bytes are not repeated."""
    from ouroboros.loop_acceptance_review import wait_for_acceptance_feedback
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    monkeypatch.setattr("ouroboros.owner_wait.wait_after_tools", lambda *_a, **_k: None)
    registry._ctx._task_acceptance_pending = "binding-one"
    blocks = lambda: str(ctx.messages).count("[DELIVERY_FINALIZATION_CONTROL]")  # noqa: E731

    first = loop._replace_delivery_candidate(registry, ctx, trace, "First complete answer.", control="candidate")
    assert first.control_episode_seen is False
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert first.control_episode_seen is True and blocks() == 1
    # The same candidate renders identical bytes: a second wake adds no noise.
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert blocks() == 1
    second = loop._replace_delivery_candidate(registry, ctx, trace, "Second complete answer.", control="candidate")
    assert second.control_episode_seen is True, "the inherited flag is what hid the contract"
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    assert blocks() == 2 and second.content_sha256[:12] in str(ctx.messages)


def test_acceptance_wakes_never_spend_a_counter_to_discard_the_answer(tmp_path, monkeypatch):
    """Repeated wakes and interim replies retain the answer until an explicit selection."""
    from ouroboros.loop_acceptance_review import wait_for_acceptance_feedback
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    monkeypatch.setattr("ouroboros.owner_wait.wait_after_tools", lambda *_a, **_k: None)
    registry._ctx._task_acceptance_pending = "binding-one"
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "Complete answer.", control="candidate")
    wait_for_acceptance_feedback(registry, ctx, trace, [], set())
    for interim in ("Still waiting.", "The critic is working.", "No new review result yet."):
        wait_for_acceptance_feedback(registry, ctx, trace, [], set())
        status, text = loop._resolve_delivery_control(interim, registry, ctx, trace)
        assert (status, text) == ("retry", "Complete answer.")
        assert registry._ctx._delivery_candidate.full_text == "Complete answer."
        assert registry._ctx._delivery_control_required
        assert not registry._ctx._delivery_candidate.degraded
        assert not getattr(registry._ctx, "_completion_selected", None)
    from tests.test_delivery_forced_finalization import _select_completion
    _select_completion(registry, ctx, trace, answer_sha256=candidate.content_sha256)
    assert registry._ctx._completion_selected["action"] == "finish"
    assert registry._ctx._delivery_candidate.full_text == "Complete answer."


def test_the_rearmed_contract_never_rewrites_an_already_sent_row(tmp_path):
    """Issue #906: merging into a sent row discards the conversation cache. Every
    wake re-arms, so the control block must take the execution slot and append."""
    import copy as _copy

    from ouroboros.transcript_prefix import observe_send
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    loop._replace_delivery_candidate(registry, ctx, trace, "Complete answer.", control="candidate")
    ctx.messages.append({"role": "user", "content": "An owner follow-up that already went out."})
    observe_send(registry._ctx, ctx.messages, round_idx=1)
    sent = _copy.deepcopy(ctx.messages[-1])

    loop._arm_delivery_control(registry, ctx, trace)

    assert sent in ctx.messages, "an already-sent message was rewritten"
    control = [row for row in ctx.messages
               if "[DELIVERY_FINALIZATION_CONTROL]" in str(row.get("content") or "")]
    assert len(control) == 1 and control[0] is not ctx.messages[ctx.messages.index(sent)]
    # Only the acceptance wake's repeated re-offer is deduplicated. Every other
    # caller arms because something changed, so it always appends.
    loop._arm_delivery_control(registry, ctx, trace)
    assert str(ctx.messages).count("[DELIVERY_FINALIZATION_CONTROL]") == 2


@pytest.mark.parametrize("choice", ["wait", "finish"])
def test_pending_review_rides_beside_the_verb_and_is_recorded_on_every_answer(tmp_path, choice):
    """WP-7: the optional wait/finish choice is a sibling of ``acceptance_subject``,
    never an extra key that invalidates the body, and every control answer records
    it (an answer without the key means wait)."""
    from tests.test_delivery_control_lineage import _start_control_episode
    from ouroboros.loop_delivery import completion_observation, consume_completion_request
    from ouroboros.tools.control_runtime import stage_completion_request

    loop, registry, ctx, trace, candidate = _start_control_episode(tmp_path)
    loop._arm_delivery_control(registry, ctx, trace)
    registry._ctx._completion_observation = completion_observation(registry._ctx, trace)
    assert json.loads(stage_completion_request(registry._ctx, {
        "action": "finish", "answer_sha256": candidate.content_sha256, "pending_review": choice,
    }))["status"] == "completion_requested"
    assert consume_completion_request(registry, ctx, trace)
    assert registry._ctx._delivery_candidate.full_text == candidate.full_text
    assert registry._ctx._acceptance_pending_review_choice == choice
    loop._arm_delivery_control(registry, ctx, trace)
    registry._ctx._completion_observation = completion_observation(registry._ctx, trace)
    assert json.loads(stage_completion_request(registry._ctx, {
        "action": "finish", "answer_sha256": candidate.content_sha256,
    }))["status"] == "completion_requested"
    assert consume_completion_request(registry, ctx, trace)
    assert registry._ctx._acceptance_pending_review_choice == "wait"


@pytest.mark.parametrize("content", ["", [{"type": "thinking", "thinking": "reasoning only"}], "invalid control"])
def test_held_prose_keeps_the_sent_control_prefix(tmp_path, content):
    import copy
    from ouroboros.transcript_prefix import observe_send
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    ctx.messages.insert(0, {"role": "system", "content": "Complete the task."})
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "Complete retained answer.", control="candidate")
    loop._arm_delivery_control(registry, ctx, trace)
    observe_send(registry._ctx, ctx.messages, round_idx=1)
    sent = copy.deepcopy(ctx.messages)

    status, text = loop._resolve_delivery_control(content, registry, ctx, trace)

    assert (status, text) == ("retry", "Complete retained answer.")
    assert ctx.messages[:len(sent)] == sent
    assert "No completion selection was made" in ctx.messages[-1]["content"]
    assert ctx.messages[-1]["role"] == "user"
    assert observe_send(registry._ctx, ctx.messages, round_idx=2) is None
    assert candidate.full_text == "Complete retained answer."
