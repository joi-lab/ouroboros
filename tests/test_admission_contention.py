"""Real admission consumers: only unsent preparation retries, outside authority locks."""
import asyncio
import contextlib
import datetime
import json
import threading

import pytest

from ouroboros import _usage_wait, owner_pause, queue_observation, store, usage_accounting as ua
from ouroboros.model_wait import task_model_wait_scope
from ouroboros.task_results import write_task_result

pytestmark = pytest.mark.serial


@pytest.fixture
def root(tmp_path, monkeypatch):
    write_task_result(tmp_path, "root", "running", root_task_id="root")
    monkeypatch.setattr(ua, "_reservation_cost", lambda _request: 0.01)
    return tmp_path


def request(root):
    return ua.AttemptRequest(model="test", provider="test", task_id="root", root_task_id="root",
                             drive_root=root, reservation_usd=0.01)


def publish(root, *, generation="new", fences=()):
    (root / "state/queue_dispatch.json").write_text(json.dumps({
        "generation": generation, "budget_root_fences": list(fences), "members": {}}), encoding="utf-8")
    (root / "state/queue.alive").write_text(json.dumps({"generation": generation}), encoding="utf-8")


@contextlib.contextmanager
def database_reader(root, *, exclusive=False, money=False, acquire_wait_sec=0.0):
    """A real independent connection; release is controlled by the waiting consumer."""
    entered, release = threading.Event(), threading.Event()
    errors = []
    path = root / "state" / ("usage.sqlite" if money else "current.sqlite")
    def run():
        try:
            with contextlib.closing(store.connect(path, store.TIER_ENFORCED, wait_sec=acquire_wait_sec)) as conn:
                conn.execute("BEGIN EXCLUSIVE" if exclusive else "BEGIN")
                conn.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
                entered.set()
                if not release.wait(8):
                    raise AssertionError("consumer never released its database fixture")
                conn.execute("ROLLBACK")
        except BaseException as exc:
            errors.append(exc)
            entered.set()
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(3) and not errors
    try:
        yield lambda: (release.set(), thread.join(3))
    finally:
        release.set()
        thread.join(3)
        assert not thread.is_alive() and not errors


@pytest.mark.parametrize("phase", ["reserve", "dispatch"])
@pytest.mark.parametrize("control", ["open", "budget", "pause"])
def test_publication_replacement_retries_same_attempt_after_rollback(root, monkeypatch, phase, control):
    publish(root, generation="old")
    original = ua._check_dispatch_fences
    injected, sends, ids, waits = [], [], [], []
    stage = ["reserve"]

    def check(scope, drive, *, retry=True):
        if not retry and stage[0] == phase and not injected:
            injected.append(True)
            (root / "state/queue.alive").unlink()
        return original(scope, drive, retry=retry)

    def resume(_seconds):
        # Another money writer and Pause publisher can proceed while we wait.
        with ua._locked(root, timeout_sec=0) as view:
            rows = view.attempts()
            ids.extend(row["attempt_id"] for row in rows)
            assert all(row["state"] == "reserved" for row in rows)
        with owner_pause.launch_lock(root, "root", wait=False):
            pass
        waits.append(True)
        publish(root, fences=[{"root_task_id": "root", "status": "active", "fence_id": "budget"}]
                if control == "budget" else ())
        if control == "pause":
            owner_pause.install_fence(root, "root", request_id="pause-during-publication")

    def prepare(held):
        ids.append(held.attempt_id)
        stage[0] = "dispatch"

    monkeypatch.setattr(ua, "_check_dispatch_fences", check)
    monkeypatch.setattr(queue_observation, "_sleep", resume)
    with ua.physical_attempt_limit(1):
        if control == "open":
            assert ua.execute_physical_attempt(request(root), lambda: sends.append(True) or "answer",
                       before_dispatch=prepare, extractor=lambda _r: ({}, 0.1, True)) == "answer"
            assert ua._PHYSICAL_LIMIT.get().used == 1
        else:
            with pytest.raises(Exception, match="owner_pause|paused pending explicit resume"):
                ua.execute_physical_attempt(request(root), lambda: sends.append(True), before_dispatch=prepare)
    assert len(injected) == len(waits) == 1
    assert sends == ([True] if control == "open" else [])
    assert len(set(ids)) <= 1
    rows = ua.read_usage_records(root, final_only=True)
    assert len(rows) <= 1


@pytest.mark.parametrize("control", ["deadline", "cancelled"])
def test_real_control_poll_does_not_wait_for_legacy_sql_latch(root, monkeypatch, control):
    from ouroboros.llm_attempt import PhysicalDispatchInterrupted

    waits = []
    with task_model_wait_scope(task={"id": "root"}, drive_root=root, event_queue=None,
                               worker_slot_held=False) as owner:
        with database_reader(root, exclusive=True):
            def stop(_seconds):
                waits.append(True)
                if control == "deadline":
                    owner.task["deadline_at"] = (datetime.datetime.now(datetime.timezone.utc)
                                                  - datetime.timedelta(seconds=1)).isoformat()
                else:
                    (root / "state/cancel_intents.json").write_text(json.dumps({"schema_version": 1, "intents": {
                        "root": {"task_id": "root", "request_id": "ci-test", "state": "requested"}}}), encoding="utf-8")
            monkeypatch.setattr(queue_observation, "_sleep", stop)
            with pytest.raises(PhysicalDispatchInterrupted) as error:
                ua.execute_physical_attempt(request(root), lambda: pytest.fail("provider sent"))
            assert error.value.control_reason == control
    assert waits == [True]


def test_dispatch_commit_busy_rolls_back_before_waiting_on_root_lock(root, monkeypatch):
    original_commit = ua.usage_store.Txn.commit
    held = contextlib.ExitStack()
    waits, sends = [], []
    release = [None]

    def commit(txn, **kwargs):
        if kwargs.get("wait_sec") == 0 and release[0] is None:
            release[0] = held.enter_context(database_reader(root, money=True))
        return original_commit(txn, **kwargs)

    def wait(_seconds):
        with owner_pause.launch_lock(root, "root", wait=False):
            pass
        waits.append(True)
        release[0]()

    monkeypatch.setattr(ua.usage_store.Txn, "commit", commit)
    monkeypatch.setattr(queue_observation, "_sleep", wait)
    try:
        ua.execute_physical_attempt(request(root), lambda: sends.append(True) or "answer",
                                    extractor=lambda _r: ({}, 0.1, True))
    finally:
        held.close()
    assert waits == [True] and sends == [True]
    assert [row["state"] for row in ua.read_usage_records(root, final_only=True)] == ["settled"]


def test_async_final_submission_wait_yields_and_creates_task_on_its_loop(root, monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        original = owner_pause.submit_model
        submitted, callbacks, sends = [], [], []
        def submit(*args, **kwargs):
            submitted.append(True)
            if len(submitted) == 1:
                loop.call_soon(callbacks.append, "loop-progress")
                raise owner_pause.LaunchBusy("contention")
            assert callbacks == ["loop-progress"]
            return original(*args, **kwargs)
        async def send():
            assert asyncio.get_running_loop() is loop
            sends.append(True)
            return "answer"
        monkeypatch.setattr(owner_pause, "submit_model", submit)
        with ua.physical_attempt_limit(1):
            assert await ua.execute_physical_attempt_async(request(root), send,
                              extractor=lambda _r: ({}, 0.1, True)) == "answer"
            assert ua._PHYSICAL_LIMIT.get().used == 1
        assert len(submitted) == 2 and sends == [True]
    asyncio.run(run())


@pytest.mark.parametrize("error", [store.StoreBusy("resource", reason="uninspectable"),
                                   store.StoreLost("lost"), PermissionError("denied")])
def test_noncontention_never_enters_presend_retry(root, monkeypatch, error):
    monkeypatch.setattr(queue_observation, "_sleep", lambda _: pytest.fail("retry on permanent failure"))
    def fail():
        raise error
    with pytest.raises(type(error)) as caught:
        _usage_wait.retry_presend(fail, check=False)
    assert caught.value is error


def test_storebusy_from_the_provider_body_is_never_a_presend_retry(root):
    sends = []
    def send():
        sends.append(True)
        raise store.StoreBusy("an error after sender entry", reason="contention")
    with pytest.raises(store.StoreBusy):
        ua.execute_physical_attempt(request(root), send)
    assert sends == [True]
    assert ua.read_usage_records(root, final_only=True)[0]["state"] == "unresolved"


def test_unconfirmed_rollback_is_not_retried(root, monkeypatch):
    from ouroboros.usage_ledger import UsageLockUnavailable
    error = UsageLockUnavailable("commit outcome unavailable", reason="contention")
    error.rollback_confirmed = False
    monkeypatch.setattr(queue_observation, "_sleep", lambda _: pytest.fail("unconfirmed rollback retried"))
    with pytest.raises(UsageLockUnavailable):
        _usage_wait.retry_presend(lambda: (_ for _ in ()).throw(error), check=False)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("failure_at", ["stop", "historical", "pause_before", "pause_after"])
def test_real_review_controller_preserves_busy_probe_without_cancelling_paid_work(root, monkeypatch, strict, failure_at):
    from types import SimpleNamespace
    from ouroboros import acceptance_history, task_results
    from ouroboros.model_wait import TaskModelWait
    from ouroboros.review_operation import ReviewOperation

    purpose = {"schema_version": 1, "task_id": "root", "debt_id": "d", "source_ref": {"sha256": "original"},
               "delivery": {"id": "delivered"}, "accounting_root_task_id": "root", "automatic": False}
    write_task_result(root, "root", "running", acceptance_debt=purpose)
    parent = TaskModelWait(task={"id": "root"}, drive_root=root, event_queue=None, worker_slot_held=False)
    operation = ReviewOperation(parent=parent, request=SimpleNamespace(
        surface="task_acceptance", retry_key="same", policy={"historical_acceptance": purpose}),
        result_root=root, task_id="root")
    monkeypatch.setattr(acceptance_history, "read_acceptance_history", lambda *_a, **_k: {"task_contract": {}})
    monkeypatch.setattr(acceptance_history, "historical_review_controls",
                        lambda *_a, **_k: ["root_budget_fence"] if failure_at.startswith("pause_") else [])
    original, reads = task_results.load_task_result, []
    failed = [True]
    def load(*args, **kwargs):
        reads.append(True)
        if failed[0] and len(reads) == (1 if failure_at == "stop" else 3) and not failure_at.startswith("pause_"):
            raise store.StoreBusy("current fact contended", reason="contention")
        return original(*args, **kwargs)
    monkeypatch.setattr(task_results, "load_task_result", load)
    original_fence, fence_reads = owner_pause.read_fence, []
    def fence(*args, **kwargs):
        fence_reads.append(True)
        if failed[0] and len(fence_reads) == (1 if failure_at == "pause_before" else 2):
            raise store.StoreBusy("pause fact contended", reason="contention")
        return original_fence(*args, **kwargs)
    if failure_at.startswith("pause_"):
        monkeypatch.setattr(owner_pause, "read_fence", fence)
    before = operation._checked_at
    if strict:
        with pytest.raises(store.StoreBusy):
            operation.wait.control_reason(strict_current=True)
    else:
        assert operation.wait.control_reason() is None
        assert operation.control_state == "unknown"
    assert operation._checked_at == before and not operation._control
    failed[0] = False
    monkeypatch.setattr(acceptance_history, "historical_review_controls", lambda *_a, **_k: [])
    assert operation.wait.control_reason(strict_current=True) is None
    assert operation.control_state == "" and operation._checked_at > before


def test_q_signature_probe_keeps_unknown_without_waiting_for_the_database(root):
    from supervisor.task_ownership import TaskOwnershipRead
    from supervisor import queue
    reads = TaskOwnershipRead(root)
    reads.load("root")
    with database_reader(root, exclusive=True):
        with queue._queue_lock:
            assert reads.unchanged(["root"]) is False
    assert reads.unchanged(["root"]) is True


def test_resume_fence_uses_existing_hold_outside_the_root_lock(root, monkeypatch):
    from types import SimpleNamespace
    from ouroboros import budget_pause

    fence, _ = owner_pause.install_fence(root, "root", request_id="pause-before-resume")
    ctx = SimpleNamespace(task_id="root", drive_root=root, budget_drive_root=root, task_metadata={})
    usage, held = {}, []
    # Only diagnostic publication is stubbed; the real hold, controls and fence mutation run.
    monkeypatch.setattr(budget_pause, "_publish_hold", lambda *_a, **_k: None)
    with database_reader(root, exclusive=True) as release:
        def resume(_seconds):
            with owner_pause.launch_lock(root, "root", wait=False):
                held.append(True)
            release()
        monkeypatch.setattr(budget_pause.time, "sleep", resume)
        budget_pause._reopen_owner_fence(ctx, usage, fence_id=fence["fence_id"], grant_id="same-grant")
    assert held == [True] and "budget_pause_hold" not in usage
    current = owner_pause.read_fence(root, "root")
    assert current["fence_id"] == fence["fence_id"] and current["state"] == owner_pause.FENCE_RELEASED


def test_owner_pause_install_waits_before_acknowledgement_with_one_request_id(root, monkeypatch):
    waits = []
    with database_reader(root, exclusive=True) as release:
        def available(_seconds):
            with owner_pause.launch_lock(root, "root", wait=False):
                waits.append(True)
            release()
        monkeypatch.setattr(owner_pause.time, "sleep", available)
        fence, created = owner_pause.install_fence(root, "root", request_id="one-owner-action")
    assert waits == [True] and created
    repeated, created = owner_pause.install_fence(root, "root", request_id="one-owner-action")
    assert repeated["fence_id"] == fence["fence_id"] and not created
    assert owner_pause.read_fence(root, "root")["requests"] == {
        "one-owner-action": {"fence_id": fence["fence_id"], "generation": fence["generation"]}}


def test_pause_install_does_not_retry_an_unconfirmed_write(root, monkeypatch):
    from ouroboros import task_store
    error = store.StoreBusy("unknown write outcome", reason="contention")
    error.rollback_confirmed = False
    def unavailable(*_a, **_k):
        raise error
    monkeypatch.setattr(task_store, "patch_document", unavailable)
    monkeypatch.setattr(owner_pause.time, "sleep", lambda _: pytest.fail("uncertain Pause write replayed"))
    with pytest.raises(store.StoreBusy):
        owner_pause.install_fence(root, "root", request_id="uncertain")
    assert owner_pause.read_fence(root, "root") == {}
    with owner_pause.launch_lock(root, "root", wait=False):
        pass


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("bound", ["calendar", "logical", "ceiling"])
def test_known_stop_keeps_priority_over_expired_clocks(root, monkeypatch, strict, bound):
    from ouroboros import config, model_wait

    (root / "state/cancel_intents.json").write_text(json.dumps({"schema_version": 1, "intents": {
        "root": {"task_id": "root", "request_id": "stop-before-clock", "state": "requested"}}}), encoding="utf-8")
    with task_model_wait_scope(task={"id": "root"}, drive_root=root, event_queue=None,
                               worker_slot_held=False) as owner:
        if bound == "calendar":
            owner.task["deadline_at"] = "2000-01-01T00:00:00+00:00"
        elif bound == "ceiling":
            monkeypatch.setattr(config, "resolve_task_execution_timeout_sec", lambda _task: 0.0)
        with model_wait.execution_deadline_scope(0.0) if bound == "logical" else contextlib.nullcontext():
            assert owner.control_reason(strict_current=strict) == "cancelled"


@pytest.mark.parametrize("strict", [False, True])
def test_graceful_stop_keeps_deadline_and_does_not_become_immediate_cancel(root, strict):
    from ouroboros.cancel_intents import STOP_POLICY_FINALIZE

    (root / "state/cancel_intents.json").write_text(json.dumps({"schema_version": 1, "intents": {
        "root": {"task_id": "root", "request_id": "graceful", "state": "requested",
                 "stop_policy": STOP_POLICY_FINALIZE}}}), encoding="utf-8")
    with task_model_wait_scope(task={"id": "root", "deadline_at": "2000-01-01T00:00:00+00:00"},
                               drive_root=root, event_queue=None, worker_slot_held=False) as owner:
        assert owner.control_reason(strict_current=strict) == "deadline"


@pytest.mark.parametrize("strict", [False, True])
def test_busy_legacy_cancel_read_cannot_hide_an_expired_deadline(root, strict):
    with task_model_wait_scope(task={"id": "root", "deadline_at": "2000-01-01T00:00:00+00:00"},
                               drive_root=root, event_queue=None, worker_slot_held=False) as owner:
        with database_reader(root, exclusive=True):
            assert owner.control_reason(strict_current=strict) == "deadline"


def test_current_cancel_gap_waits_only_for_unsent_work(root):
    with task_model_wait_scope(task={"id": "root"}, drive_root=root, event_queue=None,
                               worker_slot_held=False) as owner:
        with database_reader(root, exclusive=True):
            with pytest.raises(store.StoreBusy):
                owner.control_reason(strict_current=True)
            assert owner.control_reason() is None


def test_custom_control_callback_keeps_its_original_place_after_calendar(root, monkeypatch):
    from ouroboros import config

    calls = []
    def control():
        calls.append(True)
        return "custom_control"
    with task_model_wait_scope(task={"id": "root", "deadline_at": "2000-01-01T00:00:00+00:00"},
                               drive_root=root, event_queue=None, worker_slot_held=False,
                               owner_control=control) as owner:
        assert owner.control_reason(strict_current=True) == "deadline" and not calls
        owner.task.pop("deadline_at")
        monkeypatch.setattr(config, "resolve_task_execution_timeout_sec", lambda _task: 0.0)
        assert owner.control_reason(strict_current=True) == "custom_control" and calls == [True]
