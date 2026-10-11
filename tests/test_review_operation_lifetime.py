"""An already-paid review operation owns its wait, controls and lifetime past its author.

Real consumers only: the substrate runner and custody seam dispatch the panel,
the real ``model_waitable`` wrapper parks a reviewer on a quota refusal inside
the operation's own ``TaskModelWait``, and the gateway/supervisor seams decide
and publish exactly as the Web/Host ingress would.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ouroboros import config, model_wait, review_operation
from ouroboros.model_wait import model_waitable
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from ouroboros.task_results import load_task_result, write_task_result

MODEL = "claudexor::codex=exact-model"
OTHER_MODEL = "claudexor::codex=replacement-model"
TASK = "author-root"


def until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("expected operation state did not arrive")


def _quota_error(role):
    from ouroboros.llm_claudexor import ClaudexorModelError

    error = ClaudexorModelError({"code": "subscription_window_exhausted", "message": "quota window", "context": {}},
                                model_role=role)
    error.physical_attempt_capture = SimpleNamespace(state="released", attempt_id="", provider_status_code=None)
    return error


class QuotaModel:
    """One quota refusal per panel, then a PASS; catalog availability gated by ``ready``."""

    def __init__(self):
        self.ready = threading.Event()
        self.calls = []
        self._refused = set()
        self._lock = threading.Lock()

    @model_waitable
    def chat(self, messages, model, model_role="", **kwargs):
        subject = json.dumps(messages)
        with self._lock:
            self.calls.append({"model": model, "role": model_role, "subject": subject})
            first = subject not in self._refused
            self._refused.add(subject)
        if first:
            raise _quota_error(model_role)
        return ({"content": json.dumps({"verdict": "PASS", "findings": [], "summary": f"{model} passes"})},
                {"prompt_tokens": 3, "completion_tokens": 2})

    def claudexor_model_sources(self):
        return {"sources": [{"id": "codex", "credentialHarness": "fixture-harness"}]}

    def claudexor_model_catalog(self, source, account=None, *, requested_model=None):
        models = [{"id": requested_model}] if self.ready.is_set() else []
        return {"source": source, "credentialProfileId": account or "account-a", "models": models}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.005)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_START_SEC", 0.005)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_MAX_SEC", 0.01)
    root = tmp_path / "data"
    root.mkdir()
    write_task_result(root, TASK, "running", chat_id=7)
    ctx = SimpleNamespace(task_id=TASK, task_attempt=1, drive_root=root, budget_drive_root=root,
                          task_metadata={}, pending_events=[], event_queue=None)
    model = QuotaModel()
    yield SimpleNamespace(root=root, ctx=ctx, model=model, events=queue.Queue())
    model.ready.set()
    until(lambda: not [op for op in review_operation._LIVE.values() if op.task_id == TASK], timeout=15)


def _request(subject="complete answer", retry_key="acceptance-op-one", *, drain=True):
    return ReviewRequest(surface="task_acceptance", task_id=TASK, goal="goal", subject=subject,
                         evidence={"requirement": "exact"}, retry_key=retry_key,
                         drain_deadline=time.monotonic() if drain else None)


def _slot(slot_id="one"):
    return ReviewSlot(slot_id=slot_id, model=MODEL, effort="high", timeout_sec=30)


def _rows(root, state="waiting"):
    waits = (load_task_result(root, TASK) or {}).get("model_waits") or {}
    return [row for row in waits.values() if row.get("state") == state and row.get("review_operation")]


def _settled_rows(root):
    """Waiting rows past their last waiting publication.

    A parked reviewer publishes its row, then republishes it at the next revision
    once its credential-harness lookup lands; a copy taken earlier is a stale
    event its live operation has already superseded.
    """
    return [row for row in _rows(root) if row.get("credential_harness")]


def _parent(f, **task):
    return model_wait.task_model_wait_scope(task={"id": TASK, "chat_id": 7, "_attempt": 1, **task},
                                            drive_root=f.root, event_queue=f.events, worker_slot_held=False)


def test_reviewer_wait_outlives_the_author_scope_and_closes_only_after_settlement(env):
    f = env
    with _parent(f) as parent:
        first = run_review_request(_request(), slots=[_slot()], drive_root=f.root, usage_ctx=f.ctx, llm=f.model)
        row = until(lambda: _rows(f.root))[0]
    assert parent.closed, "the author's own scope ended normally"
    block = row["review_operation"]
    assert block["owner_id"] != parent.owner_id and row["model_wait_owner_id"] == block["owner_id"]
    assert block["controller"]["pid"] == os.getpid() and block["slot_id"] == "one" and block["chat_id"] == 7
    operation = review_operation._LIVE[block["owner_id"]]
    time.sleep(0.15)
    assert _rows(f.root) and not operation.closed, "the author's close is not the panel's"
    # The checkpoint retained the exact operation BEFORE the first send.
    entry = load_task_result(f.root, TASK)["review_operations"][block["owner_id"]]
    assert entry["state"] == "dispatched" and entry["retained_at" if "retained_at" in entry else "recorded_at"]
    assert entry["controller"] == block["controller"] and block["controller"]["birth"]
    from ouroboros.artifacts import read_actor_source_bytes

    source = json.loads(read_actor_source_bytes(f.root, TASK, entry["source_ref"]))
    assert source["request"]["subject"] == "complete answer" and source["slot_roster"][0]["model"] == MODEL
    assert source["operations"]["one"] == first.actors[0]["operation_id"], "reserved id is the dispatched id"
    f.model.ready.set()
    until(lambda: operation.closed)
    resolved = [r for r in (load_task_result(f.root, TASK)["model_waits"] or {}).values()
                if r["wait_id"] == row["wait_id"]][0]
    assert resolved["state"] == "resolved" and resolved["resolution"] == "resource_available"
    # Physical close leaves canonical publication duty outstanding; the mailbox is not consumption.
    until(lambda: load_task_result(f.root, TASK)["review_operations"][block["owner_id"]]["state"] == "unpublished")
    from ouroboros.review_dispatch import collect_task_acceptance_run

    collected = collect_task_acceptance_run(json.loads(json.dumps(dataclasses.asdict(first))),
                                            drive_root=f.root, usage_ctx=f.ctx)
    assert collected.actors[0]["parsed"]["verdict"] == "PASS"
    assert collected.collection["facts"] == {"one": "collected"}
    assert len(f.model.calls) == 2, "one refused attempt and one answer; collection bought nothing"


def test_author_execution_scope_is_stripped_while_operation_clocks_bound_the_drain(env, monkeypatch):
    f = env
    timer = threading.Timer(0.4, f.model.ready.set)
    timer_started = False
    catalog = f.model.claudexor_model_catalog

    def observe_wait(*args, **kwargs):
        nonlocal timer_started
        response = catalog(*args, **kwargs)
        if not timer_started:
            # Start the recovery window after quota_enter, not during panel setup.
            assert response["models"] == []
            timer.start()
            timer_started = True
        return response

    monkeypatch.setattr(f.model, "claudexor_model_catalog", observe_wait)
    try:
        with _parent(f) as parent:
            # An author tool-call execution deadline far shorter than the reviewer's quota wait.
            with model_wait.execution_deadline_scope(model_wait.monotonic_now() + 0.05):
                result = run_review_request(_request(drain=False), slots=[_slot()], drive_root=f.root,
                                            usage_ctx=f.ctx, llm=f.model)
            assert parent.paused_seconds() == 0.0, "the reviewer's quota wait is the operation's clock"
    finally:
        timer.cancel()
        if timer_started:
            timer.join()
    actor = result.actors[0]
    assert actor["status"] == "ok" and actor["parsed"]["verdict"] == "PASS", actor
    events = [f.events.get_nowait() for _ in range(f.events.qsize())]
    resolved = [e for e in events if e.get("state") == "resolved" and e.get("review_operation")]
    assert resolved and resolved[-1]["resolution"] == "resource_available"
    assert resolved[-1]["quota_clock"]["elapsed_sec"] >= 0.2, "the union clock is the operation's own"


def test_explicit_calendar_deadline_still_bounds_the_reviewer_wait(env):
    f = env
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=0.4)).isoformat()
    with _parent(f, deadline_at=deadline):
        result = run_review_request(_request(drain=False), slots=[_slot()], drive_root=f.root,
                                    usage_ctx=f.ctx, llm=f.model)
    assert result.actors[0]["status"] == "error"
    waits = load_task_result(f.root, TASK)["model_waits"].values()
    assert [row["resolution"] for row in waits] == ["deadline"]
    assert len(f.model.calls) == 1, "an interrupted wait never re-sends"


@pytest.mark.parametrize("stop", ["cancel", "settled_cancel", "panic"])
def test_stop_panic_and_settled_cancellation_bind_the_operation(env, stop):
    f = env
    with _parent(f) as parent:
        run_review_request(_request(), slots=[_slot()], drive_root=f.root, usage_ctx=f.ctx, llm=f.model)
        row = until(lambda: _rows(f.root))[0]
    operation = review_operation._LIVE[row["review_operation"]["owner_id"]]
    assert parent.closed and operation.control() is None
    if stop == "cancel":
        from ouroboros.cancel_intents import request_cancel

        request_cancel(f.root, TASK, reason="owner stop", source="test")
    elif stop == "settled_cancel":
        write_task_result(f.root, TASK, "cancelled")
    else:
        (f.root / "state").mkdir(exist_ok=True)
        (f.root / "state" / "panic_stop.flag").write_text("panic", encoding="utf-8")
    until(lambda: operation.closed)
    expected = "panic" if stop == "panic" else "cancelled"
    assert operation.control() == expected
    resolved = [r for r in load_task_result(f.root, TASK)["model_waits"].values() if r["wait_id"] == row["wait_id"]]
    assert resolved[0]["resolution"] == expected and len(f.model.calls) == 1
    if stop == "settled_cancel":
        # Latched: a later read still refuses even if the settled status were rewritten.
        write_task_result(f.root, TASK, "completed")
        assert operation.control() == "cancelled"


def _decision(row, request_id, **action):
    body = {"request_id": request_id, "decision_id": f"model_wait:{TASK}:{row['wait_id']}",
            "revision": row["revision"], "action": "retry"}
    body.update(action)
    return body


def _supervisor_ctx(root, forwarded):
    from ouroboros.utils import append_jsonl

    return SimpleNamespace(RUNNING={}, DRIVE_ROOT=root, append_jsonl=append_jsonl,
                           bridge=SimpleNamespace(push_log=forwarded.append))


def test_owner_controls_reach_the_exact_live_operation_after_author_end(env, monkeypatch):
    from ouroboros.gateway import task_model_wait as gateway
    from supervisor import queue as task_queue
    from supervisor.task_model_wait import handle_task_model_wait

    f = env
    monkeypatch.setattr(task_queue, "RUNNING", {})
    with _parent(f) as parent:
        run_review_request(_request("answer A", "wave-a"), slots=[_slot()], drive_root=f.root,
                           usage_ctx=f.ctx, llm=f.model)
        run_review_request(_request("answer B", "wave-b"), slots=[_slot()], drive_root=f.root,
                           usage_ctx=f.ctx, llm=f.model)
        until(lambda: len(_settled_rows(f.root)) == 2)
    write_task_result(f.root, TASK, "completed", result="answer B")
    rows = {row["review_operation"]["retry_key"]: row for row in _settled_rows(f.root)}
    op_a = review_operation._LIVE[rows["wave-a"]["review_operation"]["owner_id"]]
    op_b = review_operation._LIVE[rows["wave-b"]["review_operation"]["owner_id"]]
    # The supervisor publishes a live operation's waiting row although the author ended.
    forwarded = []
    sup = _supervisor_ctx(f.root, forwarded)
    event = {"type": "task_model_wait", "task_id": TASK, **rows["wave-a"]}
    handle_task_model_wait(event, sup)
    assert len(forwarded) == 1 and forwarded[0]["chat_id"] == 7
    # A stale revision is refused; the exact one is accepted by the live operation only.
    stale = gateway._decide(f.root, {**_decision(rows["wave-a"], "stale"), "revision": rows["wave-a"]["revision"] + 5})
    assert stale.status_code == 409 and json.loads(stale.body)["reason_code"] == "stale_model_wait"
    switch = _decision(rows["wave-a"], "switch-a", action="switch", model=OTHER_MODEL,
                       credential_profile_id="", use_local=False, persist_role=False)
    assert gateway._decide(f.root, switch).status_code == 202
    until(lambda: op_a.closed)
    assert op_a.wait.overrides["reviewer:one"]["model"] == OTHER_MODEL
    assert "reviewer:one" not in op_b.wait.overrides and "reviewer:one" not in parent.overrides, \
        "a decision for one panel never re-routes a sibling panel or the author"
    assert [c["model"] for c in f.model.calls if "answer A" in c["subject"]] == [MODEL, OTHER_MODEL]
    # A decision after the controller closed is never a false 202.
    after = gateway._decide(f.root, _decision(rows["wave-a"], "late", action="retry"))
    assert after.status_code == 409
    handle_task_model_wait(event, sup)
    assert len(forwarded) == 1, "a closed operation's waiting row cannot be revived"
    f.model.ready.set()
    until(lambda: op_b.closed)
    assert [c["model"] for c in f.model.calls if "answer B" in c["subject"]] == [MODEL, MODEL]


def test_a_waiting_row_superseded_by_its_live_operation_is_not_forwarded(env):
    """The first visible waiting row is not the settled one, and its copy stays refused."""
    from supervisor.task_model_wait import handle_task_model_wait

    f = env
    parked, gate = threading.Event(), threading.Event()
    lookup = f.model.claudexor_model_sources

    def held_lookup():
        parked.set()
        gate.wait(10)
        return lookup()

    f.model.claudexor_model_sources = held_lookup
    try:
        with _parent(f):
            run_review_request(_request(), slots=[_slot()], drive_root=f.root, usage_ctx=f.ctx, llm=f.model)
            assert parked.wait(10), "the reviewer published its first waiting row, then looked up its harness"
        early = _rows(f.root)[0]
    finally:
        gate.set()
    assert not early["credential_harness"]
    settled = until(lambda: _settled_rows(f.root))[0]
    assert settled["wait_id"] == early["wait_id"] and settled["revision"] > early["revision"]
    write_task_result(f.root, TASK, "completed")
    forwarded = []
    sup = _supervisor_ctx(f.root, forwarded)
    handle_task_model_wait({"type": "task_model_wait", "task_id": TASK, **early}, sup)
    assert forwarded == [], "a revision the live operation already republished is never forwarded"
    handle_task_model_wait({"type": "task_model_wait", "task_id": TASK, **settled}, sup)
    assert len(forwarded) == 1 and forwarded[0]["revision"] == settled["revision"] and forwarded[0]["chat_id"] == 7


# A REAL review operation in another process: the author scope ends, the reviewer
# parks on a quota refusal inside its operation, and the process stays alive after
# the operation closes until the test closes its stdin. Its first line names the
# process actually executing it (on Windows a venv ``python.exe`` is a launcher
# whose CHILD is the interpreter, so ``Popen.pid`` is not that process).
_REMOTE_OPERATION = r"""
import json, os, sys, threading, time
from types import SimpleNamespace
root, session, task, surface = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
from ouroboros import config, model_wait, process_custody, review_operation
from ouroboros.llm_claudexor import ClaudexorModelError
from ouroboros.model_wait import model_waitable
from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
process_custody.adopt_session_id(session)
config.CLAUDEXOR_MODEL_POLL_INTERVAL_SEC = 0.01
config.NETWORK_WAIT_BACKOFF_START_SEC = 0.01
config.NETWORK_WAIT_BACKOFF_MAX_SEC = 0.02

class Model:
    def __init__(self):
        self.calls, self.refused = [], set()

    @model_waitable
    def chat(self, messages, model, model_role="", **kwargs):
        self.calls.append(model)
        subject = json.dumps(messages)
        if subject not in self.refused:
            self.refused.add(subject)
            error = ClaudexorModelError({"code": "subscription_window_exhausted", "message": "quota",
                                         "context": {}}, model_role=model_role)
            error.physical_attempt_capture = SimpleNamespace(state="released", attempt_id="",
                                                             provider_status_code=None)
            raise error
        return {"content": json.dumps({"verdict": "PASS", "findings": [], "summary": "passes"})}, {}

    def claudexor_model_sources(self):
        return {"sources": [{"id": "codex", "credentialHarness": "fixture"}]}

    def claudexor_model_catalog(self, source, account=None, *, requested_model=None):
        return {"source": source, "credentialProfileId": account or "a", "models": []}

model = Model()
ctx = SimpleNamespace(task_id=task, task_attempt=1, drive_root=root, budget_drive_root=root, task_metadata={},
                      pending_events=[], event_queue=None)
with model_wait.task_model_wait_scope(task={"id": task, "chat_id": 7, "_attempt": 1}, drive_root=root,
                                      event_queue=None, worker_slot_held=False):
    run_review_request(ReviewRequest(surface=surface, task_id=task, goal="goal", subject="answer",
                                     evidence={"requirement": "exact"}, retry_key="remote-wave",
                                     drain_deadline=time.monotonic()),
                       slots=[ReviewSlot(slot_id="one", model="claudexor::codex=exact-model", timeout_sec=60)],
                       drive_root=root, usage_ctx=ctx, llm=model)
print(json.dumps({"author_scope": "closed", "pid": os.getpid(), "ppid": os.getppid(), "executable": sys.executable,
                  "base_executable": getattr(sys, "_base_executable", "")}), flush=True)
while any(op.task_id == task for op in list(review_operation._LIVE.values())):
    time.sleep(0.01)
print(json.dumps({"operation": "closed", "calls": model.calls}), flush=True)
sys.stdin.read()
"""


def _lines(stream):
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in stream], daemon=True)
    reader.start()
    return lines, reader


def _exited(pid, birth, within):
    """Whether the process ``pid`` born at ``birth`` is gone (or was never known) within ``within`` seconds."""
    from ouroboros.platform_layer import pid_is_alive, process_start_time

    deadline = time.monotonic() + within
    while pid and birth and pid_is_alive(pid):
        observed_birth = process_start_time(pid)
        if observed_birth and observed_birth != birth:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.mark.parametrize("alive,observed_birth,expected", [
    (True, "", False),
    (True, "original", False),
    (True, "replacement", True),
    (False, "", True),
])
def test_remote_exit_observation_does_not_turn_unknown_birth_into_death(monkeypatch, alive, observed_birth, expected):
    from ouroboros import platform_layer

    monkeypatch.setattr(platform_layer, "pid_is_alive", lambda _pid: alive)
    monkeypatch.setattr(platform_layer, "process_start_time", lambda _pid: observed_birth)
    assert _exited(123, "original", 0) is expected


def _stop_remote(child, reader, executing):
    """EOF on stdin ends the operation's interpreter (also behind a launcher); reap both, then the reader.

    A stuck child is killed as its own PID tree, never ``killpg``: it shares the
    test runner's process group. The interpreter is awaited by its own pid and
    birth, since a launcher's exit alone does not prove it gone.
    """
    from ouroboros.platform_layer import force_kill_pid, kill_pid_tree, process_start_time

    child.stdin.close()
    try:
        child.wait(timeout=30)
    except subprocess.TimeoutExpired:
        kill_pid_tree(child.pid)
        child.kill()
        child.wait()
    pid, birth = executing.get("pid"), executing.get("birth")
    exited = _exited(pid, birth, 10)
    if not exited and pid and birth and process_start_time(pid) == birth:
        force_kill_pid(pid)
        _exited(pid, birth, 10)
    reader.join(10)
    return {"launcher_returncode": child.returncode, "interpreter_exited": exited, "reader_running": reader.is_alive()}


@pytest.mark.parametrize("observed_birth,should_kill", [("", False), ("replacement", False), ("original", True)])
def test_remote_cleanup_requires_positive_birth_match(monkeypatch, observed_birth, should_kill):
    from types import SimpleNamespace
    from ouroboros import platform_layer

    killed = []
    joined = []
    child = SimpleNamespace(stdin=SimpleNamespace(close=lambda: None), wait=lambda timeout: None, returncode=0)
    reader = SimpleNamespace(join=joined.append, is_alive=lambda: False)
    monkeypatch.setitem(globals(), "_exited", lambda *_args: False)
    monkeypatch.setattr(platform_layer, "process_start_time", lambda _pid: observed_birth)
    monkeypatch.setattr(platform_layer, "force_kill_pid", killed.append)
    result = _stop_remote(child, reader, {"pid": 123, "birth": "original"})
    assert killed == ([123] if should_kill else [])
    assert joined == [10]
    assert result["interpreter_exited"] is False  # Emergency cleanup never launders a failed teardown.


def _forge(root, wait_id, row, pointer=None, *, remove=False):
    """Write (or ``remove``) one forged wait row and, when given, its own open pointer, in one locked write."""
    from ouroboros.task_results import task_result_path
    from ouroboros.utils import update_json_locked

    owner_id = row["review_operation"]["owner_id"]

    def change(current):
        waits = {key: value for key, value in current["model_waits"].items() if key != wait_id}
        pointers = {key: value for key, value in current["review_operations"].items()
                    if pointer is None or key != owner_id}
        if not remove:
            waits[wait_id] = row
            pointers.update({owner_id: pointer} if pointer is not None else {})
        return {**current, "model_waits": waits, "review_operations": pointers}

    update_json_locked(task_result_path(root, TASK), change, strict_existing_dict=True)


@pytest.mark.serial
@pytest.mark.parametrize("surface", ["task_acceptance", "plan_review"])
def test_a_real_remote_operation_consumes_its_exact_decision_and_a_closed_one_is_never_told_202(
        env, monkeypatch, record_property, capsys, surface):
    from ouroboros.gateway import task_model_wait as gateway
    from ouroboros.owner_mailbox import KIND_MODEL_WAIT, drain_owner_entries
    from ouroboros.platform_layer import collect_descendant_pids, process_start_time
    from ouroboros.process_custody import current_custody_session_id
    from supervisor import queue as task_queue
    from supervisor.task_model_wait import handle_task_model_wait

    f = env
    monkeypatch.setattr(task_queue, "RUNNING", {})
    child = subprocess.Popen([sys.executable, "-c", _REMOTE_OPERATION, str(f.root), current_custody_session_id(), TASK,
                              surface],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=os.getcwd())
    out, reader = _lines(child.stdout)
    executing = {}
    topology = {"launched": {"pid": child.pid, "birth": process_start_time(child.pid), "argv0": sys.executable},
                "executing": executing}
    try:
        banner = json.loads(out.get(timeout=60))
        assert banner.pop("author_scope") == "closed"
        executing.update(banner, birth=process_start_time(banner["pid"]))
        # The controller is the process executing the operation: the one we launched or its
        # descendant, with the birth measured here and this server generation's custody session.
        assert executing["pid"] == child.pid or executing["pid"] in collect_descendant_pids(child.pid), topology
        assert executing["birth"], topology
        # The live row once the operation published its credential harness (its last
        # revision while it waits for the owner).
        row = until(lambda: [r for r in _rows(f.root) if r.get("credential_harness")], timeout=30)[0]
        write_task_result(f.root, TASK, "completed", result="answer")  # the author ended
        block = row["review_operation"]
        assert block["controller"] == {"pid": executing["pid"], "birth": executing["birth"],
                                       "session": current_custody_session_id()}, topology
        # Its open pointer is part of the proof; only task acceptance also retains a checkpoint source.
        pointer = load_task_result(f.root, TASK)["review_operations"][block["owner_id"]]
        assert pointer["state"] == "dispatched" and pointer["controller"] == block["controller"]
        assert ("source_ref" in pointer) is (surface == "task_acceptance")
        # The supervisor publishes the live operation's waiting row although the author ended.
        forwarded = []
        handle_task_model_wait({"type": "task_model_wait", "task_id": TASK, **row}, _supervisor_ctx(f.root, forwarded))
        assert len(forwarded) == 1 and forwarded[0]["chat_id"] == 7
        stale = gateway._decide(f.root, {**_decision(row, "stale"), "revision": row["revision"] + 5})
        assert stale.status_code == 409 and json.loads(stale.body)["reason_code"] == "stale_model_wait"
        # Rows naming the same process but another birth (a reused pid) or another server
        # generation are refused, and a refused claim leaves no pending action behind. A
        # row alone disagrees with its operation's pointer; a forged row with its own
        # matching pointer passes that check and meets the birth and session checks.
        before = load_task_result(f.root, TASK)
        for wait_id, controller, state in (
                ("reused-pid", {**block["controller"], "birth": "another process"}, "dead"),
                ("other-generation", {**block["controller"], "session": "previous-server"}, "alive")):
            assert review_operation.controller_state(controller) == state
            for owner_id in (block["owner_id"], f"forged-{wait_id}"):
                forged = {**row, "wait_id": wait_id, "model_wait_owner_id": owner_id,
                          "review_operation": {**block, "owner_id": owner_id, "controller": controller}}
                own_pointer = None if owner_id == block["owner_id"] else {**pointer, "controller": controller}
                _forge(f.root, wait_id, forged, own_pointer)
                try:
                    stored = load_task_result(f.root, TASK)
                    opened = review_operation._open_pointer(stored, stored["model_waits"][wait_id])
                    assert (opened is not None) is (own_pointer is not None)
                    refused = gateway._decide(f.root, _decision(forged, f"forged-{wait_id}"))
                    assert refused.status_code == 409 and json.loads(refused.body)["reason_code"] == "task_not_live"
                    assert "pending_action" not in load_task_result(f.root, TASK)["model_waits"][wait_id]
                finally:
                    _forge(f.root, wait_id, forged, own_pointer, remove=True)
        restored = load_task_result(f.root, TASK)
        assert restored["model_waits"].keys() == before["model_waits"].keys()
        assert restored["review_operations"] == before["review_operations"]
        switch = _decision(row, "switch-remote", action="switch", model=OTHER_MODEL,
                           credential_profile_id="", use_local=False, persist_role=False)
        response = gateway._decide(f.root, switch)
        assert response.status_code == 202, response.body
        closed = json.loads(out.get(timeout=60))
        assert closed == {"operation": "closed", "calls": [MODEL, OTHER_MODEL]}, "the remote operation applied it"
        assert drain_owner_entries(f.root, TASK, set(), kinds={KIND_MODEL_WAIT}) == [], \
            "the decision reached the operation through its canonical row, not the author's mailbox"
        resolved = load_task_result(f.root, TASK)["model_waits"][row["wait_id"]]
        assert resolved["state"] == "resolved" and resolved["applied_request_id"] == "switch-remote"
        # Physical close retains unpaid canonical-publication duty; a liveness-only pointer is removed.
        until(lambda: (load_task_result(f.root, TASK)["review_operations"].get(block["owner_id"]) or {}).get(
            "state", "removed") == ("unpublished" if surface == "task_acceptance" else "removed"))
        # The worker process is still alive; its operation is gone. A waiting row naming
        # that exact live controller is never answered 202, nor published as live.
        assert child.poll() is None
        lingering = {**row, "wait_id": "after-close"}
        model_wait.mutate_wait(f.root, TASK, "after-close", lambda _previous: lingering)
        late = gateway._decide(f.root, _decision(lingering, "after-close"))
        assert late.status_code == 409 and json.loads(late.body)["reason_code"] == "task_not_live"
        forwarded.clear()
        handle_task_model_wait({"type": "task_model_wait", "task_id": TASK, **lingering},
                               _supervisor_ctx(f.root, forwarded))
        assert forwarded == []
        # Nor does a live author turn adopt it: the author's mailbox is not the operation's.
        task_queue.RUNNING[TASK] = {"task": {"id": TASK, "chat_id": 7, "_attempt": 1}, "attempt": 1}
        adopted = gateway._decide(f.root, _decision(lingering, "author-live"))
        assert adopted.status_code == 409 and json.loads(adopted.body)["reason_code"] == "task_not_live"
        assert drain_owner_entries(f.root, TASK, set(), kinds={KIND_MODEL_WAIT}) == []
    finally:
        topology["teardown"] = _stop_remote(child, reader, executing)
        # The actual process topology, readable in the CI log even when the test passes.
        record_property("remote_controller_topology", json.dumps(topology))
        with capsys.disabled():
            print(f"\nREMOTE_CONTROLLER_TOPOLOGY {surface} {json.dumps(topology)}", flush=True)
    assert topology["teardown"]["interpreter_exited"] and not topology["teardown"]["reader_running"], topology
    # A dead controller is refused as well.
    dead = gateway._decide(f.root, _decision({**row, "wait_id": "after-close"}, "after-death"))
    assert dead.status_code == 409


def test_rejoining_a_live_panel_binds_its_operation_and_retains_no_phantom_checkpoint(env):
    f = env
    with _parent(f):
        run_review_request(_request(), slots=[_slot()], drive_root=f.root, usage_ctx=f.ctx, llm=f.model)
        row = until(lambda: _rows(f.root))[0]
        operation = review_operation._LIVE[row["review_operation"]["owner_id"]]
        seen = []
        timer = threading.Timer(0.3, f.model.ready.set)
        timer.start()
        # The same panel again while its worker is still parked: a pure relay.
        original = review_operation.review_operation_scope

        def spy(**kwargs):
            scope = original(**kwargs)
            binding = scope.__enter__()
            seen.append(binding.operation)
            return _Entered(scope, binding)

        review_operation.review_operation_scope, restore = spy, original
        try:
            import ouroboros.review_custody as custody
            custody.review_operation_scope = spy
            again = run_review_request(_request(drain=False), slots=[_slot()], drive_root=f.root,
                                       usage_ctx=f.ctx, llm=f.model)
        finally:
            review_operation.review_operation_scope = restore
            custody.review_operation_scope = restore
    assert seen == [operation], "the rejoining caller binds the live operation, not a new one"
    assert again.actors[0]["parsed"]["verdict"] == "PASS" and len(f.model.calls) == 2
    until(lambda: operation.closed)
    assert list(load_task_result(f.root, TASK)["review_operations"]) == [operation.owner_id]


class _Entered:
    def __init__(self, scope, binding):
        self.scope, self.binding = scope, binding

    def __enter__(self):
        return self.binding

    def __exit__(self, *exc):
        return self.scope.__exit__(*exc)


def test_a_checkpoint_pointer_that_did_not_land_refuses_the_send(env, monkeypatch):
    f = env
    # The write "succeeds" but the stored row does not hold this operation's pointer.
    monkeypatch.setattr(review_operation, "_update_operations", lambda *_a, **_k: {"review_operations": {}})
    with _parent(f):
        result = run_review_request(_request(), slots=[_slot()], drive_root=f.root, usage_ctx=f.ctx, llm=f.model)
    assert f.model.calls == [] and result.actors[0]["operation_state"] == "not_dispatched"
    assert "pointer did not land" in result.actors[0]["error"]


def test_missing_checkpoint_refuses_the_panel_before_any_send(env, monkeypatch):
    f = env

    def unavailable(*_args, **_kwargs):
        raise OSError("artifact store unavailable")

    monkeypatch.setattr("ouroboros.artifacts.store_actor_source_bytes", unavailable)
    with _parent(f):
        result = run_review_request(_request(), slots=[_slot(), _slot("two")], drive_root=f.root,
                                    usage_ctx=f.ctx, llm=f.model)
    assert f.model.calls == []
    assert {actor["operation_state"] for actor in result.actors} == {"not_dispatched"}
    assert all(review_operation.CHECKPOINT_REFUSAL in actor["error"] for actor in result.actors)
    assert not [op for op in review_operation._LIVE.values() if op.task_id == TASK]


def test_a_panel_refused_at_zero_cost_keeps_its_own_refusal_and_owes_no_checkpoint(env, monkeypatch):
    f = env
    monkeypatch.setattr("ouroboros.artifacts.store_actor_source_bytes",
                        lambda *_a, **_k: pytest.fail("a panel that can send nothing needs no checkpoint"))
    request = _request(drain=False)
    request.evidence = {"requirement": "exact", "__immutable_core_overflow__": {"reason": "owner text too large"}}
    with _parent(f):
        result = run_review_request(request, slots=[_slot()], drive_root=f.root, usage_ctx=f.ctx, llm=f.model)
    assert f.model.calls == [] and "review_operations" not in load_task_result(f.root, TASK)
    assert result.actors[0]["operation_state"] == "not_dispatched"
    assert "degraded_core_overflow" in result.actors[0]["error"]
