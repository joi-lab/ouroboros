"""Finite delegated-leaf continuation after a CONFIRMED wall-clock expiry (#1196).

Static authoring note: these tests were WRITTEN against the candidate but NOT
RUN by their author (no runtime imports were permitted in that lane); the
parent's isolated harness is the first execution.

``delegate_start(continue_from=<run_id>)`` is admitted only over this task's
own SETTLED run that the engine cancelled with reason ``wall_clock_exceeded``,
after its result was read and its patch explicitly disposed, on the same
executor and workspace authority. It is not recovery: every other ending
refuses typed, nothing is replayed, and no session state is transferred.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ouroboros import delegate_continuation as continuation, delegate_custody as custody
from ouroboros.delegate_registration_policy import (
    CAP_BASIS_DEADLINE_DERIVED,
    CAP_BASIS_LIFETIME_DERIVED,
    CAP_BASIS_OPERATION_WINDOW,
    CAP_BASIS_REQUESTED,
    CAP_BASIS_REQUESTED_CLAMPED_DEADLINE,
    CAP_BASIS_REQUESTED_CLAMPED_LIFETIME,
    CAP_BASIS_REQUESTED_CLAMPED_SCHEMA,
    FINITE_LEAF_CAP_BASES,
)
from tests._delegated_transport_shared import (  # noqa: F401 -- autouse transport fixture
    _nanny_ctx,
    _owned_gateway_uses_each_test_transport,
    _started_request,
)

ROUTE = "some-route"
CAUSE = continuation.CONTINUATION_CAUSE
# The binding facts an admissible predecessor RECORDS. Each is a separate seed
# argument so a test can withhold exactly one and name the refusal it earns.
ACTOR = "actor-1"
CFG = "cfg-fingerprint-1"
AUTH = "authority-fingerprint-1"
WORK_ORDER = "work-order-fingerprint-1"


def _seed(tmp_path, run_id, *, task_id="t-a", state="cancelled", reason=CAUSE, settled=True,
          actor=ACTOR, route=ROUTE, access="readonly", mode="ask", isolation="", snapshot_id="",
          target_root="", max_seconds=90, continuation_of="", cap_basis=CAP_BASIS_REQUESTED,
          config_fingerprint=CFG, authority_fingerprint=AUTH, work_order_fingerprint=WORK_ORDER,
          output="consumed"):
    """Durable rows for one prior run; the memo is cleared so lookups REPLAY them.

    The defaults describe an ADMISSIBLE predecessor: a finite leaf cap the nanny
    asked for, recorded executor/configuration/task-authority/work-order bindings,
    and a terminal detail staged in full and read to EOF. ``output`` selects how
    much of the result story exists: ``none`` (nothing retained), ``staged`` (full
    content staged, never read) or ``consumed`` (staged and acknowledged).
    """
    entry = custody.RunCustody(
        run_id=run_id, task_id=task_id, route_id=route, model="m", selected_subagent_id=actor,
        snapshot_id=snapshot_id, target_root=target_root, root_task_id=task_id,
        continuation_of=continuation_of, config_fingerprint=config_fingerprint,
        authority_fingerprint=authority_fingerprint, work_order_fingerprint=work_order_fingerprint,
    )
    assert custody.record_started(tmp_path, entry, shape={
        "access": access, "mode": mode, "isolation": isolation, "delegated": bool(isolation),
        "root": "/r", "max_seconds": max_seconds, "max_seconds_basis": cap_basis})
    if settled:
        row = {"run_id": run_id, "task_id": task_id, "route": route, "state": state}
        if reason is not None:
            row["outcome_reason"] = reason
        assert custody.emit(tmp_path, custody.SETTLED, row)
    if output in ("staged", "consumed"):
        assert custody.emit(tmp_path, custody.OUTPUT_SPILLED, {
            "run_id": run_id, "task_id": task_id, "artifact": f"delegated_runs/{run_id}.json",
            "sha256": f"sha-{run_id}", "bytes": 3, "staged": True, "full_content": True})
    if output == "consumed":
        assert custody.emit(tmp_path, custody.OUTPUT_CONSUMED, {
            "run_id": run_id, "task_id": task_id, "sha256": f"sha-{run_id}"})
    custody._CUSTODY.clear()
    return entry


def _gate(tmp_path, run_id, *, task_id="t-a", actor=ACTOR, route=ROUTE, access="readonly", mode="ask",
          isolation="", target_root="", config_fingerprint=CFG, authority_fingerprint=AUTH,
          canonical=""):
    ctx = SimpleNamespace(task_id=task_id)
    return continuation.bind_continuation(
        ctx, tmp_path, run_id, actor={"selected_subagent_id": actor,
                                      "config_fingerprint": config_fingerprint,
                                      "authority_fingerprint": authority_fingerprint},
        route=SimpleNamespace(route_id=route),
        authority=SimpleNamespace(access=access, mode=mode, isolation=isolation),
        target_root=target_root, canonical_work_order_fingerprint=canonical)


# --------------------------------------------------------------------------- custody facts

def test_settlement_rows_replay_the_typed_cause_and_the_continuation_lineage(tmp_path):
    _seed(tmp_path, "run-a", continuation_of="run-0")
    replayed = custody.replay(tmp_path)["run-a"]
    assert replayed.settled and replayed.terminal_state == "cancelled"
    assert replayed.terminal_reason == CAUSE and replayed.continuation_of == "run-0"
    # A settlement that predates the field replays as an UNRECORDED cause, never as one.
    _seed(tmp_path, "run-legacy", reason=None)
    assert custody.replay(tmp_path)["run-legacy"].terminal_reason == ""


def test_settle_run_records_the_engines_typed_outcome_reason(tmp_path, monkeypatch):
    import ouroboros.usage_accounting as accounting

    monkeypatch.setattr(accounting, "record_subscription_session", lambda *_a, **_k: None)
    custody._CUSTODY.pop("run-s", None)
    row = custody.RunCustody(run_id="run-s", task_id="t-a", route_id=ROUTE, model="m")
    assert custody.record_started(tmp_path, row)
    assert custody.settle_run(tmp_path, None, row, {"summary": {
        "state": "cancelled", "spendUsd": 0, "spendEstimated": False,
        "outcomeFacts": {"lifecycle": "cancelled", "reason": CAUSE},
    }})["settled"]
    settled = [r for r in custody.custody_rows(tmp_path) if r.get("type") == custody.SETTLED and r.get("run_id") == "run-s"]
    assert settled[-1]["outcome_reason"] == CAUSE and settled[-1]["state"] == "cancelled"
    custody._CUSTODY.clear()
    assert custody.replay(tmp_path)["run-s"].terminal_reason == CAUSE
    # A succeeded run keeps its byte-identical settlement row (no failure facts at all).
    ok = custody.RunCustody(run_id="run-ok", task_id="t-a", route_id=ROUTE, model="m")
    assert custody.record_started(tmp_path, ok)
    assert custody.settle_run(tmp_path, None, ok, {"summary": {"state": "succeeded", "spendUsd": 0,
                                                                 "spendEstimated": False}})["settled"]
    row_ok = [r for r in custody.custody_rows(tmp_path) if r.get("type") == custody.SETTLED and r.get("run_id") == "run-ok"][-1]
    assert "outcome_reason" not in row_ok and "failure_code" not in row_ok


# --------------------------------------------------------------------------- the gate

def test_gate_admits_only_this_tasks_own_settled_wall_clock_cancelled_run(tmp_path):
    assert _gate(tmp_path, "run-none")[1] == continuation.REFUSAL_SOURCE_UNKNOWN
    _seed(tmp_path, "run-theirs", task_id="t-other")
    assert _gate(tmp_path, "run-theirs")[1] == continuation.REFUSAL_SOURCE_NOT_OWNED
    _seed(tmp_path, "run-live", settled=False)
    facts, code, detail = _gate(tmp_path, "run-live")
    assert code == continuation.REFUSAL_SOURCE_NOT_TERMINAL and "second writer" in detail
    _seed(tmp_path, "run-legacy", reason=None)
    assert _gate(tmp_path, "run-legacy")[1] == continuation.REFUSAL_CAUSE_UNRECORDED
    for state, reason in (("cancelled", "user_cancelled"), ("cancelled", "host_cancelled"),
                          ("cancelled", "owner_task_gone"), ("failed", "harness_failed"),
                          ("interrupted", "crash_interrupted")):
        _seed(tmp_path, f"run-{reason}", state=state, reason=reason)
        facts, code, detail = _gate(tmp_path, f"run-{reason}")
        assert code == continuation.REFUSAL_CAUSE_NOT_WALL_CLOCK and reason in detail
    _seed(tmp_path, "run-ok")
    facts, code, _detail = _gate(tmp_path, "run-ok")
    assert code == "" and facts["continuation_of"] == "run-ok" and facts["cause"] == CAUSE
    assert facts["prior_max_seconds"] == 90 and facts["state_transfer"] == "none"
    assert facts["prior_patch_disposition"] == "not_applicable"


def test_gate_requires_a_read_result_and_an_explicit_disposition(tmp_path):
    # Staged full output never read to EOF: continuing it is a blind resend.
    _seed(tmp_path, "run-unread", output="none")
    assert custody.emit(tmp_path, custody.OUTPUT_SPILLED, {
        "run_id": "run-unread", "task_id": "t-a", "artifact": "delegated_runs/run-unread.json",
        "sha256": "s", "bytes": 3, "staged": True, "full_content": True})
    custody._CUSTODY.clear()
    assert _gate(tmp_path, "run-unread")[1] == continuation.REFUSAL_RESULT_UNREAD
    assert custody.emit(tmp_path, custody.OUTPUT_CONSUMED, {"run_id": "run-unread", "task_id": "t-a", "sha256": "s"})
    custody._CUSTODY.clear()
    assert _gate(tmp_path, "run-unread")[1] == ""
    # A snapshot run's captured patch must be explicitly applied or rejected first.
    _seed(tmp_path, "run-snap", snapshot_id="snap-1", access="workspace_write", mode="agent", isolation="live",
          target_root="/target")
    facts, code, _d = _gate(tmp_path, "run-snap", access="workspace_write", mode="agent", isolation="live",
                            target_root="/target")
    assert code == continuation.REFUSAL_PATCH_UNDISPOSED
    assert custody.emit(tmp_path, custody.PATCH_APPLY_STARTED, {"run_id": "run-snap", "task_id": "t-a",
                                                                "snapshot_id": "snap-1", "apply_idempotency_key": "k"})
    custody._CUSTODY.clear()
    assert _gate(tmp_path, "run-snap", access="workspace_write", mode="agent", isolation="live",
                 target_root="/target")[1] == continuation.REFUSAL_APPLY_AMBIGUOUS
    assert custody.emit(tmp_path, custody.PATCH_DISPOSED, {"run_id": "run-snap", "task_id": "t-a",
                                                           "snapshot_id": "snap-1", "disposition": "rejected"})
    custody._CUSTODY.clear()
    facts, code, _d = _gate(tmp_path, "run-snap", access="workspace_write", mode="agent", isolation="live",
                            target_root="/target")
    assert code == "" and facts["prior_patch_disposition"] == "rejected" and facts["prior_target_root"] == "/target"
    assert "REJECTED" in continuation.continuation_instruction(facts)


def test_gate_keeps_the_same_executor_and_workspace_authority(tmp_path):
    _seed(tmp_path, "run-actor", actor="actor-a")
    assert _gate(tmp_path, "run-actor", actor="actor-b")[1] == continuation.REFUSAL_EXECUTOR_MISMATCH
    assert _gate(tmp_path, "run-actor", actor="actor-a")[1] == ""
    # An UNRECORDED side never "does not contradict" — it refuses, on either side.
    assert _gate(tmp_path, "run-actor", actor="")[1] == continuation.REFUSAL_EXECUTOR_MISMATCH
    _seed(tmp_path, "run-noactor", actor="")
    assert _gate(tmp_path, "run-noactor", actor="actor-a")[1] == continuation.REFUSAL_EXECUTOR_MISMATCH
    _seed(tmp_path, "run-route", route="other-route")
    assert _gate(tmp_path, "run-route")[1] == continuation.REFUSAL_EXECUTOR_MISMATCH
    _seed(tmp_path, "run-shape", access="workspace_write", mode="agent", isolation="live", target_root="/t")
    assert _gate(tmp_path, "run-shape")[1] == continuation.REFUSAL_AUTHORITY_MISMATCH
    assert _gate(tmp_path, "run-shape", access="workspace_write", mode="agent", isolation="live",
                 target_root="/elsewhere")[1] == continuation.REFUSAL_TARGET_MISMATCH
    # A mutating run WITHOUT a snapshot (nothing to dispose) passes on the same target.
    facts, code, _d = _gate(tmp_path, "run-shape", access="workspace_write", mode="agent", isolation="live",
                            target_root="/t")
    assert code == "" and facts["prior_target_root"] == "/t" and facts["prior_patch_disposition"] == "not_applicable"
    # ...and its host block says the tree ALREADY holds its in-place work: a run that
    # captured no patch because it wrote directly is not a read-only run.
    assert facts["prior_access"] == "workspace_write"
    in_place = continuation.continuation_instruction(facts)
    assert "wrote DIRECTLY into the authority target" in in_place and "already contains" in in_place
    assert "read-only" not in in_place
    readonly_facts, code, _d = _gate(tmp_path, "run-actor", actor="actor-a")
    assert code == "" and readonly_facts["prior_access"] == "readonly"
    assert "(a read-only run)" in continuation.continuation_instruction(readonly_facts)


def test_gate_admits_only_a_finite_leaf_cap_the_nanny_asked_for(tmp_path):
    """A cap this task's own deadline or lifetime derived (or narrowed) expiring IS that
    bound, so it is not a leaf expiry a continuation may follow; a row predating the
    field is UNKNOWN, never assumed. Only what the nanny asked for is admitted."""
    # A legacy row with no recorded basis is unknown, not a leaf cap.
    _seed(tmp_path, "run-nobasis", cap_basis="")
    facts, code, detail = _gate(tmp_path, "run-nobasis")
    assert code == continuation.REFUSAL_CAP_BASIS_UNKNOWN and "no basis" in detail
    for basis in (CAP_BASIS_DEADLINE_DERIVED, CAP_BASIS_LIFETIME_DERIVED,
                  CAP_BASIS_OPERATION_WINDOW, CAP_BASIS_REQUESTED_CLAMPED_DEADLINE,
                  CAP_BASIS_REQUESTED_CLAMPED_LIFETIME):
        assert basis not in FINITE_LEAF_CAP_BASES
        _seed(tmp_path, f"run-{basis}", cap_basis=basis)
        facts, code, detail = _gate(tmp_path, f"run-{basis}")
        assert code == continuation.REFUSAL_CAP_NOT_FINITE_LEAF and basis in detail
    # An absent number is not a finite bound either, whatever the basis claims.
    _seed(tmp_path, "run-zerocap", max_seconds=0)
    assert _gate(tmp_path, "run-zerocap")[1] == continuation.REFUSAL_CAP_NOT_FINITE_LEAF
    # The two bases the nanny genuinely asked for are admitted, and disclosed as facts.
    for basis in (CAP_BASIS_REQUESTED, CAP_BASIS_REQUESTED_CLAMPED_SCHEMA):
        _seed(tmp_path, f"run-ok-{basis}", cap_basis=basis)
        facts, code, _detail = _gate(tmp_path, f"run-ok-{basis}")
        assert code == "" and facts["prior_cap_basis"] == basis and facts["prior_max_seconds"] == 90


def test_gate_requires_each_recorded_binding_fact_positively(tmp_path):
    """Configuration, task authority and the bound work order are each checked
    POSITIVELY: recorded on the prior run, present on this start, and equal. An
    unrecorded side on EITHER half refuses; it never passes for lack of a
    contradiction, and the authority cannot be re-derived."""
    _seed(tmp_path, "run-cfg")
    assert _gate(tmp_path, "run-cfg", config_fingerprint="other")[1] == continuation.REFUSAL_CONFIG_MISMATCH
    assert _gate(tmp_path, "run-cfg", config_fingerprint="")[1] == continuation.REFUSAL_CONFIG_MISMATCH
    _seed(tmp_path, "run-nocfg", config_fingerprint="")
    assert _gate(tmp_path, "run-nocfg")[1] == continuation.REFUSAL_CONFIG_MISMATCH

    _seed(tmp_path, "run-auth")
    assert _gate(tmp_path, "run-auth",
                 authority_fingerprint="other")[1] == continuation.REFUSAL_TASK_AUTHORITY_MISMATCH
    assert _gate(tmp_path, "run-auth",
                 authority_fingerprint="")[1] == continuation.REFUSAL_TASK_AUTHORITY_MISMATCH
    _seed(tmp_path, "run-noauth", authority_fingerprint="")
    assert _gate(tmp_path, "run-noauth")[1] == continuation.REFUSAL_TASK_AUTHORITY_MISMATCH

    # A run whose STARTED row binds no work order is not followed at all.
    _seed(tmp_path, "run-nowo", work_order_fingerprint="")
    assert _gate(tmp_path, "run-nowo")[1] == continuation.REFUSAL_WORK_ORDER_UNBOUND
    # A configured session's canonical brief must be the one the prior run was bound to;
    # an absent canonical brief is not a mismatch (an ordinary start carries none).
    _seed(tmp_path, "run-wo")
    assert _gate(tmp_path, "run-wo",
                 canonical="a-different-brief")[1] == continuation.REFUSAL_WORK_ORDER_MISMATCH
    facts, code, _detail = _gate(tmp_path, "run-wo", canonical=WORK_ORDER)
    assert code == "" and facts["canonical_work_order_fingerprint"] == WORK_ORDER
    assert facts["prior_work_order_fingerprint"] == WORK_ORDER
    assert facts["prior_config_fingerprint"] == CFG and facts["prior_authority_fingerprint"] == AUTH


def test_gate_refuses_a_result_that_was_never_retained_or_only_partly_staged(tmp_path):
    """Continuing work nobody retained or read is a blind resend: a run with no staged
    detail, and one whose staging does not POSITIVELY claim full content, each refuse."""
    _seed(tmp_path, "run-noout", output="none")
    facts, code, detail = _gate(tmp_path, "run-noout")
    assert code == continuation.REFUSAL_RESULT_UNRETAINED and "nothing of its result" in detail.lower()
    _seed(tmp_path, "run-partial", output="none")
    assert custody.emit(tmp_path, custody.OUTPUT_SPILLED, {
        "run_id": "run-partial", "task_id": "t-a", "artifact": "delegated_runs/run-partial.json",
        "sha256": "p", "bytes": 3, "staged": True, "full_content": False})
    custody._CUSTODY.clear()
    assert _gate(tmp_path, "run-partial")[1] == continuation.REFUSAL_RESULT_INCOMPLETE
    # A staged-in-full result read to EOF is the admissible shape.
    _seed(tmp_path, "run-read", output="consumed")
    facts, code, _detail = _gate(tmp_path, "run-read")
    assert code == "" and facts["prior_output"]["staged_output_consumed"] is True


def test_requested_cap_is_clamped_by_the_deadline_and_the_remaining_lifetime(tmp_path, monkeypatch):
    """The producer side of the cap basis: ``bounded_max_seconds`` is narrow-only and
    RECORDS which bound decided the number, so a deadline/lifetime narrowing can never
    later be read as a finite leaf cap the nanny asked for."""
    import time
    from datetime import datetime, timedelta, timezone

    from ouroboros import config
    from ouroboros.tools.delegate import bounded_max_seconds

    def _ctx(*, deadline_in=None, started_ago=100.0):
        meta = {}
        if deadline_in is not None:
            meta["deadline_at"] = (datetime.now(timezone.utc)
                                   + timedelta(seconds=deadline_in)).isoformat()
        return SimpleNamespace(task_id="t-a", task_metadata=meta,
                               task_started_at=time.time() - started_ago,
                               _budget_paused_sec=0.0, budget_pause_resume=None)

    # No deadline and no finite lifetime: an explicit ask is exactly what was asked.
    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: None)
    plain = bounded_max_seconds(_ctx(), 120)
    assert (plain.seconds, plain.basis) == (120, CAP_BASIS_REQUESTED)
    # Omitting the ask derives from the operation window — never a leaf cap.
    assert bounded_max_seconds(_ctx(), None).basis == CAP_BASIS_OPERATION_WINDOW
    # A finite lifetime with 200s left narrows a larger ask and NAMES the lifetime.
    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 300.0)
    clamped = bounded_max_seconds(_ctx(), 1000)
    assert clamped.basis == CAP_BASIS_REQUESTED_CLAMPED_LIFETIME
    assert 150 <= clamped.seconds <= 200
    # An ask that already fits inside the remaining lifetime is untouched.
    assert bounded_max_seconds(_ctx(), 30).basis == CAP_BASIS_REQUESTED
    # A nearer deadline decides instead, and is named instead.
    near = bounded_max_seconds(_ctx(deadline_in=50), 1000)
    assert near.basis == CAP_BASIS_REQUESTED_CLAMPED_DEADLINE and 40 <= near.seconds <= 50
    # Omitting the ask under a finite lifetime derives from it — still not a leaf cap.
    assert bounded_max_seconds(_ctx(), None).basis == CAP_BASIS_LIFETIME_DERIVED
    # A spent lifetime is a typed definite no-run, not a zero-second cap.
    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 10.0)
    spent = bounded_max_seconds(_ctx(started_ago=100.0), 60)
    assert spent.refusal_code == "task_lifetime_exhausted" and spent.seconds == 0
    # Every basis this producer can record is either a leaf cap or explicitly not one.
    assert FINITE_LEAF_CAP_BASES == frozenset({CAP_BASIS_REQUESTED, CAP_BASIS_REQUESTED_CLAMPED_SCHEMA})


def test_instruction_block_names_the_predecessor_and_transfers_no_state():
    text = continuation.continuation_instruction({
        "continuation_of": "run-x", "cause": CAUSE, "prior_max_seconds": 120,
        "prior_patch_disposition": "applied"})
    assert "CONTINUATION OF RUN run-x" in text and "120s wall-clock cap" in text and CAUSE in text
    assert "APPLIED" in text and "NOTHING of its session state is transferred" in text
    assert "never re-apply" in text


# --------------------------------------------------------------------------- delegate_start wiring

def test_continue_from_conflicts_are_refused_before_the_daemon(tmp_path):
    from ouroboros.delegate_shared import delegate_payload
    from ouroboros.tools import delegate

    ctx = _nanny_ctx(tmp_path)
    clash = delegate_payload(delegate._delegate_start(ctx, "finish it", continue_from="run-x", retry_of="tok"))
    assert clash["status"] == "refused" and clash["reason"] == "continuation_selector_conflict"
    assert clash["definitely_unrun"] is True
    payload_run = delegate_payload(delegate._delegate_start(
        ctx, "finish it", continue_from="run-x", root="skill_payload", bucket="external", skill_name="s"))
    assert payload_run["reason"] == "continuation_resource_conflict"
    # An empty prompt is still the first refusal, ahead of every continuation check.
    assert delegate_payload(delegate._delegate_start(ctx, "   ", continue_from="run-x"))["reason"] == "empty_prompt"


def test_continue_from_binds_the_started_run_to_its_settled_predecessor(tmp_path, monkeypatch):
    """End to end through the stubbed transport: the gate passes for this nanny's own
    wall-clock-cancelled run on the same route/actor/authority, the host block rides
    the instructions, the STARTED row carries the lineage and the payload states it."""
    from ouroboros.subagent_work_order import start_binding_fingerprints
    from tests._delegated_transport_shared import _delegating_ctx

    # The predecessor's bindings are the ones this start genuinely DERIVES (the
    # transport actor's configuration plus the production task-authority and
    # work-order fingerprints), so the gate is exercised against real equality
    # rather than against a hand-invented matching pair.
    work_order, authority = start_binding_fingerprints(
        _delegating_ctx(tmp_path, acting=False, task_id="t-nanny-read"), "edit the README")
    _seed(tmp_path, "run-prev", task_id="t-nanny-read", actor="transport-fixture", access="readonly",
          mode="ask", isolation="", config_fingerprint="transport-fixture-v1",
          authority_fingerprint=authority, work_order_fingerprint=work_order)
    request, payload = _started_request(tmp_path, acting=False, monkeypatch=monkeypatch,
                                        start_kwargs={"continue_from": "run-prev"})
    assert "CONTINUATION OF RUN run-prev" in request["instructions"]
    assert payload["continuation"]["continuation_of"] == "run-prev"
    assert payload["continuation"]["cause"] == CAUSE and payload["continuation"]["state_transfer"] == "none"
    custody._CUSTODY.clear()
    assert custody.replay(tmp_path)["run-read"].continuation_of == "run-prev"


def test_continue_from_over_an_unknown_run_is_a_typed_definite_no_run(tmp_path, monkeypatch):
    request, payload = _started_request(tmp_path, acting=False, monkeypatch=monkeypatch, expect="refused",
                                        start_kwargs={"continue_from": "run-missing"})
    assert request is None
    assert payload["reason"] == continuation.REFUSAL_SOURCE_UNKNOWN and payload["definitely_unrun"] is True
    assert payload["continue_from"] == "run-missing"


def test_no_resume_causes_are_untouched_by_the_continuation_seam():
    """The continuation is not crash recovery: the recovery veto list keeps every cause."""
    from ouroboros.delegate_recovery import NO_RESUME_CAUSES

    assert NO_RESUME_CAUSES == (
        "owner_restart", "panic", "external_signal", "worker_signal",
        "deadline", "timeout", "explicit_cancellation", "abrupt_whole_app_loss",
    )
    assert json.dumps(sorted(NO_RESUME_CAUSES))  # a tuple of plain strings, nothing hidden
