"""A delegated (agent_session) seat in a receptive review_change wave, left on mail before
its first launch (live incident on 740a649ec, task b54e4bffb8744066).

G1 records a seat the author left for an addressed message before its first launch as
``pending_dispatch`` ("preparing its first launch, which still sends"). That seat still
sends: its delegated start writes START_REQUESTED, then checkpoints its invocation onto
the attempt row, which required ``in_flight`` and so refused it ("reserved commit-review
operation is not in flight"): the paid seat never answered. Only the Claudexor gateway and
the API seat's model are scripted; the wave, attempt rows, custody and checkpoint are real.
"""
from __future__ import annotations

import json
import threading

import pytest

from ouroboros import delegate_custody
from ouroboros.owner_mailbox import write_owner_message
from ouroboros.review_state import load_state
from tests._review_session_route_shared import FakeGateway, _owned_gateway_uses_each_test_transport  # noqa: F401
from tests._review_session_route_shared import _terminal_detail, fake_route as _fake_route
from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool
from tests.test_receptive_commit_review import TASK, _git, _join, _notices, _parent, _produce, _read_mail, until
from tests.test_receptive_commit_review import author as _author

author, fake_route = _author, _fake_route
pytestmark = pytest.mark.serial
SESSION_ANSWER = "SESSION-SEAT-ANSWER"


@pytest.fixture
def session_wave(author, fake_route, monkeypatch):
    """review_change over [API seat m1, delegated seat 'second'], the delegated seat held
    before its START_REQUESTED until ``release`` is set."""
    from ouroboros.tools.scope_review_contract import SCOPE_REQUIRED_ITEMS

    from ouroboros import config
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools import review
    from ouroboros.triad_review import REVIEW_TWO_PART_OBJECT_CONTRACT

    a = author
    set_review_pool(monkeypatch, pool_roster(pool_seat("first", "test/m1", delivery="native"),
                                             pool_seat("second", "fake-review=fake-small", kind="agent_session")))
    routes = [ReviewRouteKind.API_CHAT, ReviewRouteKind.AGENT_SESSION]
    plan = {"models": ["m1", "fake-review=fake-small"], "routes": routes, "efforts": ["high"] * 2,
            "slot_ids": ["first", "second"], "use_local": [False] * 2, "subagent_ids": ["", ""],
            "session_targets": ["", "fake-review=fake-small"], "session_profiles": ["", ""],
            "retrieves": [True, True], "parts": [("change", "coupling")] * 2,
            "session_tasks": ["Review this candidate."] * 2,
            "session_policies": [{"output_contract": REVIEW_TWO_PART_OBJECT_CONTRACT}] * 2,
            "brief_shas": ["brief-sha"] * 2}

    def prepared(ctx, commit_message, review_rebuttal="", repo_dir=None, goal="", scope="", subject=None):
        from ouroboros.tools.review_helpers import review_enforcement_blocks
        root = subject.review_root if subject is not None else ctx.repo_dir
        return ({"blocking_review": review_enforcement_blocks(config.get_review_enforcement()),
                 "prompt": "Review this candidate.", "models": list(plan["models"]), "stable_prefix_len": 0,
                 "routes": list(routes), "session_task": "", "target_repo": root,
                 "row_plan": json.loads(json.dumps(plan, default=str)) | {"routes": list(routes)},
                 "layer": "core", "brief_texts": {"brief-sha": "Review this candidate."}}, None, False)
    monkeypatch.setattr(review, "_prepare_unified_review", prepared)
    answer = {"change": [], "change_clean": True, "coupling": [
        {"item": item, "verdict": "PASS", "severity": "advisory", "reason": SESSION_ANSWER}
        for item in sorted(SCOPE_REQUIRED_ITEMS)]}
    FakeGateway.detail = _terminal_detail(json.dumps(answer), conformance="passed")
    a.preparing, a.release = threading.Event(), threading.Event()
    real = delegate_custody.record_start_requested

    def held(*args, **kwargs):
        a.preparing.set()
        assert a.release.wait(60), "the delegated seat was never released"
        return real(*args, **kwargs)
    monkeypatch.setattr(delegate_custody, "record_start_requested", held)
    _git(a.repo, "add", "change.py")
    yield a
    a.release.set()


def _review_change(a, outcome):
    from ouroboros.tools.review_change import _handle_review_change

    def run():
        with _parent(a):
            outcome["result"] = _handle_review_change(a.ctx, root="system_repo", subject="index",
                                                      goal="Raise the value.", scope="change.py only")
    return _produce(a, run)


def _session_row(a):
    rows = [row for row in load_state(a.data).attempts[-1].triad_raw_results if row.get("slot_id") == "second"]
    assert len(rows) == 1, rows
    return rows[0]


def _posts():
    return [body for gateway in FakeGateway.instances for body in gateway.start_requests]


def _payload(text):
    return json.loads(text[text.index("\n{") + 1:] if not text.startswith("{") else text)


@pytest.mark.parametrize("left_on_mail", [True, False])
def test_a_delegated_seat_left_before_its_launch_still_checkpoints_and_answers(session_wave, left_on_mail):
    """Left on mail before its launch, the delegated seat still starts once (its checkpoint moves
    the row to in flight) and its answer is collected at $0 by the same unchanged call; without
    the mail the wave settles with that answer, as before."""
    a, outcome = session_wave, {}
    thread = _review_change(a, outcome)
    assert a.reviewer.sent["m1"].wait(60) and a.preparing.wait(60)
    if left_on_mail:
        write_owner_message(a.data, "Is the review done?", TASK)
        _join(thread)
        assert outcome["result"].startswith("An addressed message reached you while the reviewers ran"), outcome
        assert _session_row(a)["operation_state"] == "pending_dispatch"  # left before its first launch
    a.release.set()
    a.reviewer.release()
    if left_on_mail:
        until(lambda: _notices(a, "change"), timeout=60)
        _read_mail(a)
        outcome = {}
        _join(_review_change(a, outcome))  # the same unchanged call collects the settled wave
    else:
        _join(thread)
    collected = _payload(outcome["result"])
    assert collected["state"] == "settled", outcome["result"]
    assert len(_posts()) == 1, "the delegated seat started exactly once and was never sent again"
    row = _session_row(a)
    assert "not in flight" not in str(row.get("raw_text") or "") + str(row.get("error") or ""), row
    assert SESSION_ANSWER in str(row.get("raw_text") or ""), row


@pytest.mark.parametrize("detach", ["addressed_message", "pause_preparation", "not_live"])
def test_only_a_seat_left_on_mail_moves_to_in_flight_at_its_start(tmp_path, monkeypatch, detach):
    """The checkpoint takes a pending_dispatch row only when this process's live seat was
    detached by an addressed message before its first launch (it still sends); Pause's
    preparation cannot send after Resume, and a row with no live seat stays refused, as before."""
    from types import SimpleNamespace

    from ouroboros import review_custody
    from ouroboros.review_pause import ADDRESSED_EVENT
    from ouroboros.review_state import CommitAttemptRecord, checkpoint_pending_review_invocation, update_state

    live = {"addressed_message": {"author_left_for": ADDRESSED_EVENT, "pause_before_launch": False},
            "pause_preparation": {"author_left_for": "", "pause_before_launch": True}}.get(detach)
    monkeypatch.setattr(review_custody, "_ACTIVE", {} if live is None else {
        "attempt-key": SimpleNamespace(operation_id="op-1", **live)})
    row = {"slot_id": "second", "operation_id": "op-1", "operation_state": "pending_dispatch",
           "late_result_pending": True}
    update_state(tmp_path, lambda state: state.record_attempt(CommitAttemptRecord(
        ts="2026-10-16T00:00:00+00:00", commit_message="c", status="reviewing", task_id=TASK, root_task_id=TASK,
        tool_name="review_change", attempt=1, repo_key="repo", paid=True, review_retry_key="key",
        triad_raw_results=[row])))

    def checkpoint():
        checkpoint_pending_review_invocation(
            tmp_path, repo_key="repo", tool_name="review_change", task_id=TASK, attempt=1, review_retry_key="key",
            surface="multi_model_review", slot_id="second", operation_id="op-1", invocation_id="inv-1")
    if detach != "addressed_message":
        with pytest.raises(ValueError, match="not in flight"):
            checkpoint()
        return
    checkpoint()
    [saved] = load_state(tmp_path).attempts[-1].triad_raw_results
    assert (saved["operation_state"], saved["pending_invocation_id"], saved["late_result_pending"]) == (
        "in_flight", "inv-1", True)
