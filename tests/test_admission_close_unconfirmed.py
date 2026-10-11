"""Owner decision 2A: a reviewer-approved answer whose admission close the supervisor
never confirmed is ACCEPTED, and the card says so.

The vocabulary half: one reason token, one owner sentence in BOTH twins
(``project_dialogue.TASK_CAUSE_PHRASES`` / ``web/modules/log_events.js``), pinned by
``web/tests/fixtures/outcome_phase_parity.json`` on both sides of the boundary, and a
reducer that keeps an accepted decision out of BLOCKED. The behaviour half (the paid
fence wait is gone; the final seal reads the queue's typed answer) lives below it.
"""

from __future__ import annotations

import copy
import json
import pathlib
import queue as stdqueue
import threading
from types import SimpleNamespace

import pytest

from ouroboros import loop
from ouroboros.loop_acceptance import ACCEPTANCE_DECISION_REASONS
from ouroboros.outcomes import (
    ACCEPTANCE_ACCEPTED, ACCEPTANCE_FINALIZED_UNACCEPTED, OBJECTIVE_PASS, OUTCOME_TIER_BLOCKED,
    OUTCOME_TIER_SOLVED, _objective_axis,
)
from ouroboros.project_dialogue import TASK_CAUSE_PHRASES, _completion_verdict, outcome_phase
from tests.test_acceptance_async_loop import ANSWER, _terminal_record, call, full_loop as _full_loop, keep, select_completion  # noqa: F401
from tests.test_acceptance_fence_transport import _pooled_agent

full_loop = _full_loop  # noqa: F811 - shared real-loop fixture

NOTE = "admission_close_unconfirmed"
SENTENCE = "Reviewers approved this answer; the supervisor did not confirm that task admission was closed."


def _record(status: str, reason: str, *, enforcement: str = "blocking") -> dict:
    review = {"status": "pass", "outcome_tier": OUTCOME_TIER_SOLVED,
              "acceptance_decision": {"status": status, "reason": reason, "enforcement": enforcement}}
    return {"status": "completed", "reason_code": "final_message",
            "outcome_axes": {"execution": {"status": "ok"}, "review": review, "objective": _objective_axis(review)}}


def test_the_note_is_a_typed_accepted_reason_with_one_sentence_in_both_twins():
    assert NOTE in ACCEPTANCE_DECISION_REASONS
    assert TASK_CAUSE_PHRASES[NOTE] == SENTENCE
    twin = (pathlib.Path(__file__).resolve().parents[1] / "web" / "modules" / "log_events.js").read_text(encoding="utf-8")
    assert f'{NOTE}: "{SENTENCE}",' in twin, "the browser twin carries the byte-identical sentence"


def test_an_accepted_answer_with_the_note_is_done_and_never_blocked():
    """Both directions of the reducer: the note rides an ACCEPTED decision and keeps the
    objective a PASS at the reviewer's tier, while the honest unaccepted cells that a
    transport gap must NOT be laundered into still terminalize BLOCKED under blocking."""
    record = _record(ACCEPTANCE_ACCEPTED, NOTE)
    objective = record["outcome_axes"]["objective"]
    assert objective["status"] == OBJECTIVE_PASS and objective["outcome_tier"] == OUTCOME_TIER_SOLVED
    assert outcome_phase(record, {}) == "done" and outcome_phase({}, record) == "done"
    assert _completion_verdict(record, {}) == _completion_verdict({}, record) == SENTENCE
    for refusal in ("infra_failure", "review_degraded"):
        blocked = _record(ACCEPTANCE_FINALIZED_UNACCEPTED, refusal)["outcome_axes"]["objective"]
        assert blocked["outcome_tier"] == OUTCOME_TIER_BLOCKED and blocked["reason"] == refusal
    # A clean accepted decision keeps rendering nothing: the note is the only accepted cell that speaks here.
    assert _completion_verdict(_record(ACCEPTANCE_ACCEPTED, "clean_pass"), {}) == ""


# --- the behaviour half: no paid wait, the final seal reads the queue's typed answer -------


def _unavailable_rows(ctx):
    from ouroboros.task_pacing import acceptance_timing_events_path

    path = acceptance_timing_events_path(ctx)
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == "supervisor_ack_unavailable"]


def _gap(**_kwargs):
    raise TimeoutError("supervisor did not acknowledge acceptance fence")


def _silent_supervisor(f, *, begin=_gap, end=_gap):
    """The pooled seam raised its gap: no answer arrived within the wait."""
    f.ctx.begin_acceptance_fence, f.ctx.end_acceptance_fence, f.ctx.inspect_acceptance_fence = begin, end, _gap


def _nominate_then_keep(f, monkeypatch, *, nominate=True):
    """Main nominates (or simply writes) its complete answer, gets the verdict, then keeps it."""
    if nominate:  # the explicit nomination settles synchronously; a plain answer parks on its panel
        f.release.set()
        f.ctx.owner_wait_callback = None

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            if not nominate:
                return {"content": ANSWER}, 0.0
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "review")]}, 0.0
        assert f.model_step < 5, ("the fence bought model rounds", f.progress)
        return keep(f), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)


def test_blocking_never_acked_begin_and_seal_accepts_with_the_note(full_loop, monkeypatch):
    """Owner 2A: the reviewers approved, the supervisor never confirmed the admission close —
    not at the panel's begin, not at the final seal. The answer is ACCEPTED with the note,
    the rail is disclosed, no round was bought and the gate never became an owner follow-up."""
    f = full_loop
    _silent_supervisor(f)
    _nominate_then_keep(f, monkeypatch)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2 and len(f.review_sends) == 1
    assert (trace["acceptance_decision"]["status"], trace["acceptance_decision"]["reason"]) == ("accepted", NOTE)
    assert trace["review_decision"]["admission_fence_available"] is False
    assert trace["review_decision"]["admission_released"] is False
    assert "TASK ACCEPTANCE WAIT" not in str(f.model_inputs)
    assert trace["review_decision"]["eligibility"] != "pending_owner_followup"
    rows = _unavailable_rows(f.ctx)
    assert rows and {(row["op"], row["outcome"]) for row in rows} == {("begin", "unknown")}
    record = _terminal_record(trace)
    assert outcome_phase(record, {}) == "done" and _completion_verdict(record, {}) == SENTENCE
    assert record["outcome_axes"]["objective"]["outcome_tier"] == OUTCOME_TIER_SOLVED


def test_blocking_never_acked_begin_but_a_confirmed_final_seal_is_an_ordinary_clean_pass(full_loop, monkeypatch):
    """The natural second chance: the panel ran on the disclosed rail, the final seal's own
    begin+end were answered. No note — the admission close WAS confirmed."""
    f = full_loop
    ends: list = []

    def begin(**_kwargs):
        if not getattr(f.ctx, "_task_acceptance_reviewed", False):
            raise TimeoutError("no ack while the panel was owed")
        return {"token": "final-fence", "owner_message_generation": 0}

    def end(**kwargs):
        ends.append(dict(kwargs))
        return {"ok": True, "status": "sealed"}

    _silent_supervisor(f, begin=begin, end=end)
    _nominate_then_keep(f, monkeypatch)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2
    assert trace["acceptance_decision"]["reason"] == "clean_pass"
    assert ends == [{"token": "final-fence", "outcome": "terminal", "expected_generation": 0}]
    assert f.ctx._task_acceptance_sealed_fence_token == "final-fence"
    assert _completion_verdict(_terminal_record(trace), {}) == ""


def test_blocking_fail_verdict_is_never_laundered_by_the_note(full_loop, monkeypatch):
    """The live one-cycle shape with a silent supervisor: FAIL on the first draft, the rewrite
    is refused by the cap. Today's outcome stands — `review_cycles_exhausted`, Failed, BLOCKED —
    the answer still goes out (no bought round) and the note is nowhere near it."""
    f = full_loop
    f.reviewer_verdict = "FAIL"
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    _silent_supervisor(f)
    reauthored = ANSWER + " Budget: $12."

    def main(_llm, messages, *_a, **_kw):
        f.model_inputs.append(copy.deepcopy(messages))
        f.model_step += 1
        if f.model_step == 1:
            return {"content": "", "tool_calls": [call("task_acceptance_review", {"claim": ANSWER}, "first-review")]}, 0.0
        if f.model_step == 2:
            assert f.entered.wait(5) and not f.release.is_set()
            return select_completion(f, reauthored), 0.0
        assert f.model_step < 5, ("the fence bought model rounds", f.progress)
        return keep(f), 0.0

    monkeypatch.setattr(loop, "call_llm_with_retry", main)
    result, _usage, trace = f.run()
    assert result == reauthored and len(f.review_sends) == 1
    assert trace["acceptance_decision"]["reason"] == "review_cycles_exhausted"
    assert trace["review_decision"]["admission_released"] is False
    record = _terminal_record(trace)
    assert outcome_phase(record, {}) == "error"
    assert record["outcome_axes"]["objective"]["outcome_tier"] == OUTCOME_TIER_BLOCKED
    assert SENTENCE not in _completion_verdict(record, {})


def test_advisory_never_acked_delivers_with_the_loud_row_and_no_note(full_loop, monkeypatch):
    f = full_loop
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
    _silent_supervisor(f)
    _nominate_then_keep(f, monkeypatch)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2
    assert trace["acceptance_decision"]["reason"] == "clean_pass"  # advice on a soft install: no card note
    assert trace["review_decision"]["admission_fence_available"] is False
    assert trace["review_decision"]["admission_released"] is False
    assert _unavailable_rows(f.ctx), "the loud durable row is the soft install's record"


def test_a_healthy_supervisor_seals_as_before_without_a_row_or_a_note(full_loop, monkeypatch):
    f = full_loop
    begins: list = []
    ends: list = []
    _silent_supervisor(
        f,
        begin=lambda **_kw: begins.append(1) or {"token": f"fence-{len(begins)}", "owner_message_generation": 0},
        end=lambda **kw: ends.append(kw["outcome"]) or {"ok": True, "status": "sealed" if kw["outcome"] == "terminal" else "released"},
    )
    _nominate_then_keep(f, monkeypatch)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2
    assert trace["acceptance_decision"]["reason"] == "clean_pass"
    assert ends == ["revision", "terminal"] and f.ctx._task_acceptance_sealed_fence_token == "fence-2"
    assert trace["review_decision"]["admission_fence_available"] is True
    assert _unavailable_rows(f.ctx) == []


def test_a_seal_applied_after_the_wait_expired_is_read_as_the_workers_own_seal(full_loop, monkeypatch):
    """Main writes its answer, the panel passes and its end(terminal) loses the ack, but the
    supervisor applied it late: the final seal's own begin is refused because the root is
    already `sealed`. That is our seal, not a failure and not a gap — an ordinary clean pass,
    no note, no owner follow-up."""
    f = full_loop

    def begin(**_kwargs):
        if getattr(f.ctx, "_task_acceptance_reviewed", False):
            raise RuntimeError("sealed")  # the queue's typed refusal: already sealed for this root
        return {"token": "panel-fence", "owner_message_generation": 0}

    def end(**_kwargs):
        raise TimeoutError("ack lost; the supervisor sealed the row after the wait")

    _silent_supervisor(f, begin=begin, end=end)
    _nominate_then_keep(f, monkeypatch, nominate=False)
    result, _usage, trace = f.run()
    assert result == ANSWER and f.model_step == 2
    assert trace["acceptance_decision"]["reason"] == "clean_pass"
    assert trace["review_decision"].get("admission_released") is not False
    # (a silent `inspect` during the panel leaves its own gap row; the seal story is end -> begin)
    assert [(row["op"], row["outcome"], row["reason"]) for row in _unavailable_rows(f.ctx) if row["op"] != "inspect"] == [
        ("end", "unknown", "TimeoutError"), ("begin", "refused", "sealed")]


def _seal_context(tmp_path, owner_changed=False):
    ctx = SimpleNamespace(
        task_id="root-1", drive_root=tmp_path, task_metadata={"root_task_id": "root-1", "budget_drive_root": str(tmp_path)},
        _task_acceptance_fence_token=None, _task_acceptance_sealed_fence_token=None, _task_acceptance_fence_generation=None,
        _task_acceptance_queue_descendants=[], _task_acceptance_reviewed=True,
        _task_acceptance_fence_generation_mismatch=owner_changed, _owner_directives=[],
    )
    return SimpleNamespace(_ctx=ctx), SimpleNamespace(task_id="root-1")


@pytest.mark.parametrize("enforcement,decision,answer,owner_changed,delivered,reason", [
    ("blocking", "clean_pass", "unknown", False, True, NOTE),
    ("blocking", "clean_pass_obligations_closed", "unknown", False, True, NOTE),
    ("blocking", "clean_pass", "sealed", False, True, "clean_pass"),  # the worker's own earlier seal
    ("blocking", "clean_pass", "active", False, False, "owner_followup"),  # another holder: today's revision path
    ("blocking", "clean_pass", "unknown", True, False, "owner_followup"),  # a locally seen owner change is real
    ("advisory", "clean_pass", "unknown", False, True, "clean_pass"),  # soft install: the row, not the note
    ("blocking", "identical_acceptance_refused", "unknown", False, True, "identical_acceptance_refused"),
])
def test_the_final_seal_reads_the_queues_typed_answer(tmp_path, monkeypatch, enforcement, decision, answer, owner_changed, delivered, reason):
    from ouroboros.loop_delivery import _seal_admission_before_delivery

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", enforcement)
    tools, limit_ctx = _seal_context(tmp_path, owner_changed)
    tools._ctx.begin_acceptance_fence = _gap if answer == "unknown" else (lambda **_kw: (_ for _ in ()).throw(RuntimeError(answer)))
    status = ACCEPTANCE_ACCEPTED if decision.startswith("clean_pass") else ACCEPTANCE_FINALIZED_UNACCEPTED
    llm_trace = {"review_decision": {"eligibility": "eligible"}, "review_runs": [],
                 "acceptance_decision": {"status": status, "reason": decision, "source": "task_acceptance_review"}}
    assert _seal_admission_before_delivery(tools, limit_ctx, llm_trace) is delivered
    assert llm_trace["acceptance_decision"]["reason"] == reason
    assert llm_trace["acceptance_decision"]["status"] == (status if delivered else "revision_requested")
    if delivered and answer == "unknown":
        assert llm_trace["review_decision"]["admission_released"] is False


def test_owner_mail_that_arrived_during_the_silent_wait_is_not_delivered_over(tmp_path, monkeypatch):
    """The wait for a silent supervisor is long enough for the owner to write, and their mail is
    durable before any generation moves: a gap delivers only after the local mailbox was read once
    more. Quiet direction: the same gap with an empty mailbox delivers with the note (table above)."""
    from ouroboros.loop_delivery import _seal_admission_before_delivery
    from ouroboros.owner_mailbox import write_owner_message

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    tools, limit_ctx = _seal_context(tmp_path, False)
    tools._ctx.begin_acceptance_fence = _gap
    assert write_owner_message(tmp_path, "Use the blue variant instead", "root-1", msg_id="owner-blue")
    llm_trace = {"review_decision": {"eligibility": "eligible"}, "review_runs": [],
                 "acceptance_decision": {"status": ACCEPTANCE_ACCEPTED, "reason": "clean_pass", "source": "task_acceptance_review"}}
    assert _seal_admission_before_delivery(tools, limit_ctx, llm_trace) is False
    assert llm_trace["acceptance_decision"]["reason"] == "owner_followup"
    assert llm_trace["acceptance_decision"]["status"] == "revision_requested"


def test_a_locally_seen_owner_change_survives_a_silent_end(tmp_path):
    """`refused(generation_mismatch)` is a real owner follow-up even when the transport went
    silent: the local comparison decides the flag, not the missing ack. The quiet direction:
    an unchanged owner generation with the same silent end leaves the flag down."""
    from ouroboros.loop import _end_task_acceptance_fence

    def ctx(generation):
        return SimpleNamespace(
            task_metadata={"root_task_id": "root-1"}, task_id="root-1", drive_root=tmp_path,
            _task_acceptance_fence_token="token-1", _task_acceptance_sealed_fence_token=None,
            _task_acceptance_fence_generation=0, _task_acceptance_queue_descendants=[],
            _task_acceptance_owner_generation=0, owner_message_admission_lock=threading.RLock(),
            owner_message_admission_agent=SimpleNamespace(_owner_message_generation=generation),
            end_acceptance_fence=_gap,
        )

    changed, same = ctx(1), ctx(0)
    assert _end_task_acceptance_fence(changed, outcome="terminal").status == "unknown"
    assert changed._task_acceptance_fence_generation_mismatch is True
    assert _end_task_acceptance_fence(same, outcome="terminal").status == "unknown"
    assert same._task_acceptance_fence_generation_mismatch is False


@pytest.mark.parametrize("status,finished", [("unknown", True), ("refused", False)])
def test_an_advisory_author_finish_survives_a_silent_supervisor_but_not_a_refusal(tmp_path, monkeypatch, status, finished):
    from ouroboros import loop as loop_mod
    from ouroboros.loop_acceptance import FenceOutcome
    from ouroboros.loop_acceptance_review import _finish_advisory_author
    from ouroboros.loop_delivery import delivery_evidence_fingerprint
    from tests.test_acceptance_delivery import _acceptance_ctx

    ctx = _acceptance_ctx(tmp_path)
    ctx.tools._ctx._owner_directives = [{"source": "initial_user", "content": "goal"}]
    binding = ctx.review_binding["binding_hash"]
    ctx.llm_trace["review_runs"] = [{"authority": "host_root", "feedback_delivered": True,
                                     "binding_hash": binding, "aggregate_signal": "FAIL"}]
    ctx.llm_trace["review_decision"] = {}
    ctx.llm_trace["acceptance_decision"] = {
        "agent_disposition": "accepted", "agent_rationale": "done",
        "agent_finish_intent": {"review_binding_hash": binding, "tool_count": 0, "owner_directives": 1,
                                "evidence_fingerprint": delivery_evidence_fingerprint(ctx.tools._ctx, ctx.llm_trace)},
    }
    monkeypatch.setattr(loop_mod, "get_review_enforcement", lambda: "advisory")
    answer = FenceOutcome(status, "end", "TimeoutError" if status == "unknown" else "sealed", 0.8)
    monkeypatch.setattr(loop_mod, "_end_task_acceptance_fence", lambda *_a, **_k: answer)
    assert _finish_advisory_author(ctx) is True
    decision = ctx.llm_trace["acceptance_decision"]
    if finished:
        assert decision["reason"] == "author_finish" and ctx.tools._ctx._task_acceptance_reviewed is True
        assert ctx.llm_trace["review_decision"]["admission_released"] is False
    else:
        assert decision["reason"] == "owner_followup"


@pytest.mark.parametrize("op,ack,reason", [
    ("begin", {"ok": False, "status": "sealed", "error": "acceptance fence already sealed for root root-1"}, "sealed"),
    ("begin", {"ok": False, "status": "error", "error": "invalid acceptance fence event"}, "invalid acceptance fence event"),
    ("inspect", {"ok": True, "status": "released", "token": "t", "row_absent": True}, "released"),
])
def test_the_seam_refuses_with_the_rows_typed_status(tmp_path, op, ack, reason):
    """A refusal carries the row's typed state (`sealed`, `active`, `released`) as its reason;
    only a malformed request keeps the error text. The worker reads the state, never the prose."""
    agent = _pooled_agent(tmp_path, stdqueue.Queue())
    agent.fence_transition = lambda **_kwargs: ack
    with pytest.raises(RuntimeError) as refused:
        if op == "begin":
            agent._begin_acceptance_fence(root_task_id="root-1", task_id="root-1")
        else:
            agent._inspect_acceptance_fence(token="t")
    assert str(refused.value) == reason
