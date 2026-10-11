"""The facts reach the mind before its last word (chat-voice C2, perception).

A forced rail hands its ONE model call the limitations it will record beside the
answer as typed ``key=value`` facts, never the owner-facing ``⚠️`` notice; and a
plan-review hold the mind was told about that the owner's hurry then released
reaches it exactly once. Every other gate transition already arrives by its own
route (the mind's own ``plan_task`` result, the settled-wave task message, the
forced prompt), so it buys no round.

New module rather than ``tests/test_owner_hurry_s3.py`` (933 lines, would cross the
1000-line target) or ``tests/test_delivery_forced_finalization.py`` (a frozen giant).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# The plan-review engine is imported BEFORE the s3 helpers on purpose: importing
# ``tests.test_owner_hurry_s3`` (its gateway imports) ahead of the engine leaves a
# later ``tests/test_owner_hurry_custody.py`` collection in the same process reading
# a wave with an empty roster as resumable (a pre-existing import-order hazard,
# reproducible on the base with ``test_owner_hurry_s3 -> test_owner_hurry_custody``
# in one process); this module sorts before both, so it keeps that order safe.
import ouroboros.tools.plan_review  # noqa: F401  (import order, see above)
from tests._delivery_candidate_shared import write_child, write_confirmed_disposition_fixture
from tests.test_delivery_forced_finalization import _forced_test_context
from tests.test_owner_hurry_s3 import _acceptance_ctx, _plan_state

OWNER_NOTICE = (
    "\n\n⚠️ Plan review is still open (DEGRADED); work proceeded under the owner-selected "
    "advisory enforcement."
)


def _held(ctx, *, content="answer"):
    import ouroboros.loop as loop_mod

    trace = {"reasoning_notes": []}
    messages: list = []
    held = loop_mod._enforce_swarm_actions(content, messages, SimpleNamespace(_ctx=ctx), trace, lambda _m: None)
    return held, messages, trace


@pytest.fixture
def open_blocking_install(monkeypatch):
    import ouroboros.loop as loop_mod
    import ouroboros.task_results as tr

    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "blocking")
    monkeypatch.setattr(tr, "load_plan_review_state", lambda _root, _tid: _plan_state("open"))


def test_an_open_advisory_gate_costs_no_extra_round_on_first_sight(tmp_path, monkeypatch):
    """Q4=A: no reminder before every final. The marker is not seeded by an observation."""
    import ouroboros.loop as loop_mod
    import ouroboros.task_results as tr

    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "advisory")
    monkeypatch.setattr(tr, "load_plan_review_state", lambda _root, _tid: _plan_state("open"))
    ctx = _acceptance_ctx(tmp_path, latched=False)
    ctx.task_metadata = {"force_plan": True}
    held, messages, trace = _held(ctx)
    assert held is False and messages == []
    assert trace["force_plan_decision"]["allow"] is True
    assert not hasattr(ctx, "_plan_gate_told_identity")


def test_a_hold_released_by_owner_hurry_reaches_the_mind_once(tmp_path, open_blocking_install):
    """Fires only on a change the mind cannot see otherwise, one shot, typed not rendered."""
    ctx = _acceptance_ctx(tmp_path, latched=False)
    ctx.task_metadata = {"force_plan": True}
    held, messages, _trace = _held(ctx)
    assert held is True and messages[-1]["content"].startswith("[PLAN_REVIEW_HOLD]")
    told = ctx._plan_gate_told_identity
    assert told

    ctx._owner_hurry_latch = _acceptance_ctx(tmp_path, latched=True)._owner_hurry_latch
    held, messages, trace = _held(ctx, content="the draft")
    assert held is True and [m["role"] for m in messages] == ["assistant", "user"]
    released = messages[-1]["content"]
    assert released.startswith("[PLAN_REVIEW_RELEASED]")
    for fact in ("plan_review_open=true", "owner_hurry_local_advisory=true", "configured_enforcement=blocking"):
        assert fact in released
    assert "⚠️" not in released and "advisory enforcement" not in released.split("\n")[1]
    assert ctx._plan_gate_told_identity != told and ctx._plan_gate_release_told is True
    assert trace["reasoning_notes"] == ["Released plan-review gate reported before final response."]

    held, messages, _trace = _held(ctx)
    assert held is False and messages == []


def test_a_gate_the_mind_closed_itself_buys_no_round(tmp_path, monkeypatch, open_blocking_install):
    """The latch never bills the mind for its own act: a closed wave, or an author
    stop, after a delivered hold appends nothing."""
    import ouroboros.task_results as tr

    projection = {"status": "open", "allow": False, "closed": False, "outcome": "REVIEW_REQUIRED"}
    monkeypatch.setattr(tr, "plan_review_gate_projection", lambda *_a, **_k: dict(projection))
    ctx = _acceptance_ctx(tmp_path, latched=False)
    ctx.task_metadata = {"force_plan": True}
    assert _held(ctx)[0] is True

    ctx._owner_hurry_latch = _acceptance_ctx(tmp_path, latched=True)._owner_hurry_latch
    projection = {"status": "closed", "allow": True, "closed": True, "outcome": "GREEN"}
    held, messages, _trace = _held(ctx)
    assert held is False and messages == []

    projection = {"status": "author_stopped", "allow": True, "closed": False, "outcome": "REVIEW_REQUIRED"}
    held, messages, _trace = _held(ctx)
    assert held is False and messages == []
    assert not getattr(ctx, "_plan_gate_release_told", False)


ADVISORY_OPEN = {
    "required": True, "self_opened": True, "enforcement": "advisory", "status": "advisory_open",
    "allow": True, "closed": False, "outcome": "DEGRADED", "reviewer_slots_degraded": True,
    "custody_pending": True, "review_late_result_pending": True, "cycles_paid": 1,
}


def _round_limit_prompt(tmp_path, monkeypatch, decision, *, deferred_child):
    if deferred_child:
        write_child(tmp_path)
        write_confirmed_disposition_fixture(tmp_path, disposition="deferred", rationale="defer it")
    loop, _registry, limit_ctx, _trace = _forced_test_context(tmp_path)
    captured = []
    monkeypatch.setattr(loop, "call_llm_with_retry", lambda _llm, messages, *_a, **_k: (
        captured.append([dict(m) for m in messages]) or {"content": "Forced answer."}, 0.0))
    monkeypatch.setattr(loop, "_force_plan_decision", lambda *_a, **_k: dict(decision))
    monkeypatch.setattr(loop, "_force_plan_disclosure",
                        lambda *_a, **_k: OWNER_NOTICE if not decision.get("closed") else "")
    text, usage, _returned = loop._handle_round_limit(limit_ctx)
    assert text == "Forced answer." and len(captured) == 1
    last = next(m for m in reversed(captured[0]) if m["role"] == "user")
    content = last["content"]
    if isinstance(content, list):
        content = "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
    return str(content), usage


def test_the_forced_prompt_carries_typed_facts_not_the_owner_notice(tmp_path, monkeypatch):
    prompt, usage = _round_limit_prompt(tmp_path / "facts", monkeypatch, ADVISORY_OPEN, deferred_child=True)
    assert "[TASK_STATE_FACTS]" in prompt
    for fact in ("plan_review_open=true", "plan_review_enforcement=advisory", "cycles_paid=1",
                 "children_deferred=1: child1 [completed] sha256="):
        assert fact in prompt
    assert "⚠️ Plan review is still open" not in prompt and "⚠️ DEFERRED CHILD RESULTS" not in prompt
    if "[ACCEPTANCE_SUBJECT_OBSERVATION]" in prompt:
        assert prompt.index("[ACCEPTANCE_SUBJECT_OBSERVATION]") > prompt.index("[TASK_STATE_FACTS]")
    # The owner-facing bytes did not move into or out of the prompt.
    assert "⚠️ Plan review is still open (DEGRADED)" in usage["terminal_host_notice"]
    assert "⚠️ DEFERRED CHILD RESULTS: child1" in usage["terminal_host_notice"]

    # Nothing to state: a closed review and no child add no block at all.
    quiet, usage = _round_limit_prompt(tmp_path / "quiet", monkeypatch, {
        **ADVISORY_OPEN, "status": "closed", "closed": True, "outcome": "GREEN"}, deferred_child=False)
    assert "[TASK_STATE_FACTS]" not in quiet and "plan_review_open" not in quiet
    assert "terminal_host_notice" not in usage


def test_the_facts_block_is_data_from_typed_fields_only():
    from ouroboros.loop_forced_finalization import _plan_gate_facts

    assert _plan_gate_facts({}) == "" and _plan_gate_facts({"required": True, "closed": True}) == ""
    line = _plan_gate_facts({**ADVISORY_OPEN, "owner_hurry_local_advisory": True,
                             "configured_enforcement": "blocking", "decision_authority": "cyber_pro"})
    assert line == (
        "plan_review_open=true plan_review_outcome=DEGRADED plan_review_enforcement=advisory "
        "decision_authority=cyber_pro owner_hurry_local_advisory=true configured_enforcement=blocking "
        "reviewer_slots_degraded=true custody_pending=true review_late_result_pending=true cycles_paid=1"
    )
    assert "⚠️" not in line and "proceeded" not in line


def test_the_narrowed_latch_reads_nothing_but_the_registry_context(tmp_path):
    """The two attributes live on ``tools._ctx`` (loop-local, per worker): a resumed
    task re-derives them and spends no round. The identity is typed fields only."""
    from ouroboros.loop_forced_finalization import _plan_gate_identity

    same = _plan_gate_identity({**ADVISORY_OPEN})
    assert same == _plan_gate_identity({**ADVISORY_OPEN, "fingerprint": "different-wave"})
    assert same != _plan_gate_identity({**ADVISORY_OPEN, "custody_pending": False})
    assert _plan_gate_identity({}) == "|" * 10
