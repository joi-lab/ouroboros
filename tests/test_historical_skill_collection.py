"""Public skill collection uses its paid session roster after Settings changes."""
import json
import threading

import pytest

from ouroboros import delegate_custody, skill_review
from ouroboros.observability import call_manifest_path, persist_call, read_call_payload
from ouroboros.review_execution import ReviewAttemptResult, ReviewRouteKind
from ouroboros.skill_loader import load_review_state
from ouroboros.tools.skill_exec import _handle_review_skill
from tests.test_skill_review_late_cas import late_skill  # noqa: F401


@pytest.fixture
def session_skill(late_skill, monkeypatch):  # noqa: F811
    h = late_skill
    h.rejoins = []
    started = threading.Event()
    h.delivery.update(routes=[ReviewRouteKind.AGENT_SESSION] * 2,
                      session_targets=["cursor=grok"] * 2, subagent_ids=["", "original-actor"])
    from ouroboros import review_custody

    timeout_actor = review_custody._late_or_timeout_actor

    def after_both_starts(*args, **kwargs):
        # These cases collect a fully admitted historical wave. Keep lifecycle
        # ownership until both fake transports checkpoint; slow fsync must not
        # turn the second seat into a different pre-admission refusal scenario.
        assert started.wait(10)
        return timeout_actor(*args, **kwargs)

    monkeypatch.setattr(review_custody, "_late_or_timeout_actor", after_both_starts)

    class Session:
        def __init__(self, assignment):
            self.assignment, self.cell = assignment, {}

        def restore_custody(self, cell):
            self.cell = cell

        def set_pending_invocation_checkpoint(self, checkpoint):
            self.checkpoint = checkpoint

        def prompt_payload(self):
            return {"messages": []}

        def prompt_chars(self):
            return 0

        def failure_custody(self):
            return dict(self.cell)

        def execute(self):
            a = self.assignment
            token, run = "inv-" + a.call_id, "run-" + a.call_id
            if self.cell:
                assert getattr(h.behavior, "session_remains_live", False), "a completed producer was re-executed"
                assert self.cell["pending_invocation_id"] == token
                assert not a.slot.session_access
                record = delegate_custody.invocation_record(a.custody_root, token)
                assert self._session_prompt == record["request"]["prompt"]
                from ouroboros.review_session_custody import review_recovery_facts

                review_recovery_facts(record, record["request"], None, prompt=self._session_prompt,
                    root=a.request.session_root, claimant_task_id=a.request.task_id,
                    claimant_surface="skill_review", claimant_slot_id=a.slot.slot_id, claimant_operation_id=a.call_id)
                h.rejoins.append(a)
                return ReviewAttemptResult(message={"content": h.text}, raw_text=h.text,
                    usage={"delegated_run_started": True, "delegated_run_id": run,
                           "physical_attempt_state": "settled", "provider": "claudexor"})
            assert delegate_custody.record_start_requested(
                a.custody_root, task_id=a.request.task_id, root_task_id="skill-root",
                surface="skill_review", slot_id=a.slot.slot_id, operation_id=a.call_id,
                invocation_id=token, max_seconds=540, route="cursor", idempotency_key=token,
                request={"prompt": a.request.session_task, "harnesses": ["cursor"],
                         "model": a.slot.model, "mode": "ask", "access": a.slot.session_access or "readonly",
                         "primaryHarness": "cursor", "authPreference": "subscription",
                         "scope": {"kind": "project", "root": a.request.session_root}})
            self.checkpoint(token)
            assert delegate_custody.record_started(a.custody_root, delegate_custody.RunCustody(
                run_id=run, task_id=a.request.task_id, root_task_id="skill-root", invocation_id=token,
                route_id="cursor", model=a.slot.model, ledger_root=str(a.custody_root)),
                shape={"surface": "skill_review", "slot_id": a.slot.slot_id, "max_seconds": 540})
            self.cell.update(pending_invocation_id=token, delegated_run_id=run, delegated_run_started=True)
            a.dispatch_stamp()
            h.calls.append(a)
            if len(h.calls) >= 2:
                started.set()
            # This test owns release; teardown also releases on an early failure.
            h.release.wait()
            if getattr(h.behavior, "session_remains_live", False):
                raise TimeoutError("review waiter ended while the original delegated run remains live")
            return ReviewAttemptResult(message={"content": h.text}, raw_text=h.text,
                usage={"delegated_run_started": True, "delegated_run_id": run,
                       "physical_attempt_state": "settled", "provider": "claudexor"})

    monkeypatch.setattr("ouroboros.review_substrate._review_route_executor", lambda a, **kw: Session(a))
    return h


def _finish_historical_wave(h):
    assert "pending" in _handle_review_skill(h.ctx(), skill="late").lower()
    h.release.set()
    assert h.settled.wait(10)
    original = h.history.read_bytes()
    old = json.loads(original.splitlines()[-1])
    # Records actually predating session_access have no field at all. Their
    # binding is byte-identical to the old readonly invocation.
    for operation in old["review_wave"]["chunks"][0]["operations"].values():
        manifest, prompt, _refs = read_call_payload(h.root, task_id="skill-task", call_id=operation + "_prompt")
        prompt["slot"].pop("session_access")
        persist_call(h.root, task_id="skill-task", call_id=operation + "_prompt",
                     call_type=manifest["call_type"], payload=prompt,
                     manifest={key: manifest[key] for key in ("surface", "slot_id", "model", "review_operation_binding")})
    return original, old


@pytest.mark.parametrize("change", ["full_default", "roster", "invalid_settings"])
def test_paid_session_wave_collects_original_roster_rights_and_tokens(session_skill, monkeypatch, change):
    h = session_skill
    original, old = _finish_historical_wave(h)
    tokens = old["review_wave"]["chunks"][0]["invocations"]
    original_requests = {slot: delegate_custody.invocation_record(h.root, token)["request"]
                         for slot, token in tokens.items()}
    assert all(body["access"] == "readonly" and body["mode"] == "ask" for body in original_requests.values())
    h.delivery["session_accesses"] = ["full", "full"]
    if change == "full_default":
        from ouroboros.reviewer_slot_config import commit_triad_delivery
        from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool

        set_review_pool(monkeypatch, pool_roster(pool_seat("configured", "cursor=grok", kind="agent_session")))
        config = commit_triad_delivery()
        assert config["session_accesses"] == ["full"]
        h.delivery["session_accesses"] = config["session_accesses"] * 2
    if change == "roster":
        h.delivery.update(models=["new/model"], routes=[ReviewRouteKind.API_CHAT], slot_ids=["new-seat"],
                          efforts=["high"], session_targets=[""], session_profiles=[""], subagent_ids=[""])
    if change == "invalid_settings":
        monkeypatch.setattr(skill_review, "reviewer_slot_config_error", lambda: "malformed new Settings")
    monkeypatch.setattr(skill_review, "_build_review_prompt_for_attempt",
                        lambda *a, **kw: pytest.fail("collection rebuilt a prompt"))
    answer = _handle_review_skill(h.ctx(), skill="late")
    state = load_review_state(h.root, "late")
    assert state.status == "clean", (state, answer)
    assert len(h.calls) == 2 and h.history.read_bytes().startswith(original)
    newest = json.loads(h.history.read_bytes().splitlines()[-1])
    assert newest["review_resume_of"] == old["wave_id"]
    assert newest["review_wave"] == old["review_wave"]
    assert newest["review_contract_fingerprint"] == old["review_contract_fingerprint"]
    assert not newest.get("paid")
    for actor in state.raw_actor_records:
        assert actor["raw_text"] == h.text
        assert actor["recovery_binding"]["pending_invocation_id"] == tokens[actor["slot_id"]]
    assert original_requests == {slot: delegate_custody.invocation_record(h.root, token)["request"]
                                 for slot, token in tokens.items()}
    assert not (h.root / "state/skills/late/enabled.json").exists()


def test_session_release_belongs_to_the_test_not_the_first_wave_clock(session_skill, monkeypatch):
    h = session_skill
    real_wait, observed = h.release.wait, []

    def expire_only_timed_waits(timeout=None):
        observed.append(timeout)
        # Deterministically model a seat deadline expiring before its owner
        # finishes first-wave publication; do not spend ten real seconds.
        return False if timeout is not None else real_wait()

    monkeypatch.setattr(h.release, "wait", expire_only_timed_waits)
    try:
        first = _handle_review_skill(h.ctx(), skill="late")
        original = h.history.read_bytes()
        h.release.set()
        assert h.settled.wait(10)
        answer = _handle_review_skill(h.ctx(), skill="late")
        state = load_review_state(h.root, "late")
        assert state.status == "clean", (state, first, answer)
        assert observed == [None, None]
        assert len(h.calls) == 2 and h.history.read_bytes().startswith(original)
        assert all(actor["raw_text"] == h.text for actor in state.raw_actor_records)
    finally:
        h.release.set()


@pytest.mark.parametrize("defect", ["missing_prompt", "foreign_operation", "invocation", "chunk_contract"])
def test_invalid_original_session_source_stays_pending_without_replacement(session_skill, defect):
    h = session_skill
    _original, old = _finish_historical_wave(h)
    chunk = old["review_wave"]["chunks"][0]
    if defect == "missing_prompt":
        call_manifest_path(h.root, "skill-task", chunk["operations"]["seat-a"] + "_prompt").unlink()
    elif defect == "foreign_operation":
        chunk["operations"]["seat-a"] = chunk["operations"]["seat-b"]
    elif defect == "invocation":
        chunk["invocations"]["seat-a"] = chunk["invocations"]["seat-b"]
    else:
        old["review_wave"]["binding"]["review_contract_fingerprint"] = "foreign-contract"
    if defect != "missing_prompt":
        h.history.write_text(json.dumps(old) + "\n")
    h.delivery["session_accesses"] = ["full", "full"]
    _handle_review_skill(h.ctx(), skill="late")
    state = load_review_state(h.root, "late")
    assert state.status == "pending"
    assert len(h.calls) == 2
    if defect in {"missing_prompt", "invocation"}:
        # Pending quorum results preserve paid evidence in lifecycle history;
        # they do not install a trust verdict containing only the healthy seat.
        actors = json.loads(h.history.read_bytes().splitlines()[-1])["raw_actor_records"]
        assert any(actor["raw_text"] == h.text for actor in actors)
        lost = next(actor for actor in actors if actor["operation_state"] == "custody_lost")
        assert lost["operation_id"] == chunk["operations"]["seat-a"]
        assert lost["pending_invocation_id"] == chunk["invocations"]["seat-a"]
        assert lost["late_result_pending"]


def test_completed_session_cas_keeps_legacy_recovery_without_checkpoint_token(session_skill):
    h = session_skill
    _original, old = _finish_historical_wave(h)
    old["review_wave"]["chunks"][0].pop("invocations")
    h.history.write_text(json.dumps(old) + "\n")
    h.delivery["session_accesses"] = ["full", "full"]
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "clean"
    assert len(h.calls) == 2


def test_unfinished_session_rejoins_original_raw_invocation_after_roster_change(session_skill, monkeypatch):
    h = session_skill
    h.behavior.session_remains_live = True
    _original, old = _finish_historical_wave(h)
    h.delivery.update(models=["new/model"], routes=[ReviewRouteKind.AGENT_SESSION],
                      session_accesses=["full"], slot_ids=["new-seat"])
    monkeypatch.setattr("ouroboros.review_custody._logical_timeout", lambda *_: 2)
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "clean"
    assert len(h.calls) == 2 and len(h.rejoins) == 2
    assert {a.call_id for a in h.rejoins} == set(old["review_wave"]["chunks"][0]["operations"].values())
    assert json.loads(h.history.read_bytes().splitlines()[-1])["review_wave"] == old["review_wave"]


def test_new_explicit_skill_wave_uses_new_session_rights_and_identity(session_skill, monkeypatch):
    h = session_skill
    _original, old = _finish_historical_wave(h)
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "clean"
    h.delivery["session_accesses"] = ["full", "workspace_write"]
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "2")
    monkeypatch.setattr("ouroboros.review_custody._logical_timeout", lambda *_: 2)
    _handle_review_skill(h.ctx(), skill="late")
    new = json.loads(h.history.read_bytes().splitlines()[-1])
    assert new["paid"] and not new.get("review_resume_of")
    assert new["wave_id"] != old["wave_id"]
    assert new["review_contract_fingerprint"] != old["review_contract_fingerprint"]
    assert len(h.calls) == 4
    assert {a.slot.session_access for a in h.calls[2:]} == {"full", "workspace_write"}
    assert set(new["review_wave"]["chunks"][0]["invocations"].values()).isdisjoint(
        old["review_wave"]["chunks"][0]["invocations"].values())


@pytest.fixture
def lost_start_skill(late_skill, monkeypatch, tmp_path):  # noqa: F811
    """Both seats ride the real delegated-session transport on a synthetic engine.
    While ``lost_start`` holds, seat-a's start POST has an unknown outcome, so its
    invocation stays pending without a run id and only its exact token may replay."""
    from ouroboros import claudexor_daemon
    from ouroboros.gateways import claudexor as gateway_module
    from tests._review_session_route_shared import FakeGateway, _terminal_detail

    h = late_skill
    FakeGateway.reset()
    FakeGateway.run_dir = tmp_path / "review-run"
    FakeGateway.detail = _terminal_detail(h.text, conformance="passed", model="fake-a")
    h.lost_start = True

    class Engine(FakeGateway):
        def start_run(self, request, *, idempotency_key=""):
            if h.lost_start and request.get("model") == "fake-a":
                self.start_requests.append(dict(request))
                self.start_keys.append(str(idempotency_key))
                raise gateway_module.ClaudexorUnavailable("daemon_unreachable", "POST outcome unknown")
            super().start_run(request, idempotency_key=idempotency_key)
            return {"runId": "run-" + str(idempotency_key), "runDir": "/tmp/fake-run"}

    monkeypatch.setattr(gateway_module, "ClaudexorGateway", Engine)
    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", lambda: Engine())
    delegate_custody._CUSTODY.clear()
    h.delivery.update(routes=[ReviewRouteKind.AGENT_SESSION] * 2, session_accesses=["readonly"] * 2,
                      session_targets=["fake-review=fake-a", "fake-review=fake-b"])
    monkeypatch.setattr("ouroboros.review_custody._logical_timeout", lambda *_: 5)
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "2")
    h.engine = Engine
    return h


def _posts(engine):
    return [(key, body) for g in engine.instances for key, body in zip(g.start_keys, g.start_requests)]


def test_lost_start_replay_is_free_and_the_next_panel_stays_admissible(lost_start_skill):
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles

    h, group = lost_start_skill, "task:skill-root:late"
    _handle_review_skill(h.ctx(), skill="late")
    original = json.loads(h.history.read_bytes().splitlines()[-1])
    token = original["review_wave"]["chunks"][0]["invocations"]["seat-a"]
    record = delegate_custody.invocation_record(h.root, token)
    assert record["state"] == "pending" and not record.get("run_id")
    assert original["paid"] and count_paid_skill_review_cycles(h.root, "late", group) == 1
    before = _posts(h.engine)
    h.lost_start = False
    _handle_review_skill(h.ctx(), skill="late")  # a new lifecycle collects the recorded wave
    resumed = json.loads(h.history.read_bytes().splitlines()[-1])
    replay = _posts(h.engine)[len(before):]
    # Only seat-a's recorded token is re-posted, with its raw recorded body.
    assert replay and {key for key, _body in replay} == {token}
    assert all(body == record["request"] for _key, body in replay)
    assert resumed["review_resume_of"] == resumed["wave_id"] == original["wave_id"]
    assert load_review_state(h.root, "late").status == "clean"
    assert not resumed.get("paid") and count_paid_skill_review_cycles(h.root, "late", group) == 1
    h.delivery["efforts"] = ["high"] * 2  # a changed contract buys the next genuine panel
    answer = _handle_review_skill(h.ctx(), skill="late")
    new = json.loads(h.history.read_bytes().splitlines()[-1])
    assert "REVIEW_CYCLES_EXHAUSTED" not in answer
    assert new["paid"] and not new.get("review_resume_of") and new["wave_id"] != original["wave_id"]
    assert count_paid_skill_review_cycles(h.root, "late", group) == 2
