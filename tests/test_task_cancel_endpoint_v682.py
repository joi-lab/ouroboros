"""HTTP contract for /api/tasks/{id}/cancel with the optional cascade body (v6.82 P5).

Both the plain (no-body / empty-body) request and {"cascade": true} record the
durable intent FIRST — no liveness pre-probe gates it — and drive it through
the canonical ``drive_cancel_intent_scope`` before answering: 200 with the
pre-v6.82 single-task envelope (plus "cascade": true) once the teardown
settled, the legacy 404 when there is nothing to cancel, and 202
``cancel_state: "pending"`` while the recorded intent stays open for the
cancel-intent watchdog. 503 means nothing was recorded.
"""

from __future__ import annotations

import pytest

import json

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.tasks import api_task_cancel


def _isolate_queue(monkeypatch, tmp_path, tasks):
    from supervisor import queue
    from supervisor import workers

    pending = [dict(task) for task in tasks]
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", pending)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    return queue, pending


def _client(tmp_path):
    app = Starlette(routes=[
        Route("/api/tasks/{task_id}/cancel", api_task_cancel, methods=["POST"]),
    ])
    app.state.drive_root = tmp_path
    return TestClient(app)


def test_no_body_cancel_stays_synchronous_single_task(tmp_path, monkeypatch):
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result

    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root"},
            {"id": "child", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root"},
        ],
    )
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel")
    assert response.status_code == 200
    # Envelope byte-identical to the pre-v6.82 contract: exactly ok + task_id.
    assert response.json() == {"ok": True, "task_id": "root"}
    # Default cancel preserves live children (headless compat, v6.64 contract).
    assert [task["id"] for task in pending] == ["child"]
    assert load_task_result(tmp_path, "root")["status"] == STATUS_CANCELLED


def test_empty_json_body_matches_no_body_path(tmp_path, monkeypatch):
    """The CLI posts {} — it must keep the synchronous single-task behavior."""
    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root"},
            {"id": "child", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root"},
        ],
    )
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel", json={})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "task_id": "root"}
    assert [task["id"] for task in pending] == ["child"]


def test_cascade_acknowledges_and_cancels_whole_live_subtree(tmp_path, monkeypatch):
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result

    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root", "depth": 0},
            {"id": "child", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root", "depth": 1},
            {"id": "grandchild", "chat_id": 0, "root_task_id": "root", "parent_task_id": "child", "depth": 2},
            {"id": "other", "chat_id": 0, "root_task_id": "other", "depth": 0},
        ],
    )
    cascade_calls: list[str] = []
    real = queue.cancel_task_by_id

    def _spy(task_id):
        cascade_calls.append(task_id)
        return real(task_id)

    monkeypatch.setattr(queue, "cancel_task_by_id", _spy)
    with _client(tmp_path) as client:
        # TestClient runs the response's BackgroundTask before returning.
        response = client.post("/api/tasks/root/cancel", json={"cascade": True})
    assert response.status_code == 200
    # Same envelope keys as the plain path, plus the cascade echo.
    assert response.json() == {"ok": True, "task_id": "root", "cascade": True}
    assert cascade_calls == ["root"]
    assert [task["id"] for task in pending] == ["other"]
    for task_id in ("root", "child", "grandchild"):
        assert load_task_result(tmp_path, task_id)["status"] == STATUS_CANCELLED


def test_cancel_404_contract_both_modes(tmp_path, monkeypatch):
    _isolate_queue(monkeypatch, tmp_path, [])
    with _client(tmp_path) as client:
        plain = client.post("/api/tasks/ghost/cancel")
        cascade = client.post("/api/tasks/ghost/cancel", json={"cascade": True})
    assert plain.status_code == 404
    assert cascade.status_code == 404
    assert cascade.json()["error"] == "task not found or not active"


def test_intent_write_failure_refuses_both_cancel_modes(tmp_path, monkeypatch):
    """AR2-1 (owner 1=A, fail-closed like the agent tool lane): a failed durable
    cancel-intent write REFUSES the cancel with a typed 503 — custody is never
    invoked on an unfenced teardown and nothing is torn down."""
    queue, pending = _isolate_queue(
        monkeypatch, tmp_path,
        [{"id": "root", "chat_id": 0, "root_task_id": "root"}],
    )
    custody_calls: list = []
    monkeypatch.setattr(
        queue, "cancel_task_custody",
        lambda tid, **_kw: custody_calls.append(tid) or queue.CANCEL_CANCELLED,
    )
    monkeypatch.setattr(
        queue, "cancel_task_by_id",
        lambda tid, **_kw: custody_calls.append(tid) or True,
    )
    monkeypatch.setattr(
        "ouroboros.cancel_intents.request_cancel",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("intent store io")),
    )
    with _client(tmp_path) as client:
        plain = client.post("/api/tasks/root/cancel")
        cascade = client.post("/api/tasks/root/cancel", json={"cascade": True})
    for response in (plain, cascade):
        assert response.status_code == 503
        assert response.json()["reason_code"] == "cancel_intent_write_failed"
    assert custody_calls == [], "no teardown without the durable intent"
    assert [task["id"] for task in pending] == ["root"]


def test_cascade_repeat_after_full_cancel_is_404(tmp_path, monkeypatch):
    _isolate_queue(
        monkeypatch,
        tmp_path,
        [{"id": "root", "chat_id": 0, "root_task_id": "root"}],
    )
    with _client(tmp_path) as client:
        first = client.post("/api/tasks/root/cancel", json={"cascade": True})
        repeat = client.post("/api/tasks/root/cancel", json={"cascade": True})
    assert first.status_code == 200
    # Nothing live anymore: repeats fall back to the standard inactive contract
    # (the UI treats it as the completion-wins no-op).
    assert repeat.status_code == 404


def test_cascade_on_cancel_requested_latch_finalizes(tmp_path, monkeypatch):
    """A repeat while the result sits in the cancel_requested latch stays ok:
    the per-task cancel's finalize-on-miss branch still has honest work to do."""
    from ouroboros.task_results import (
        STATUS_CANCELLED,
        STATUS_CANCEL_REQUESTED,
        load_task_result,
        write_task_result,
    )

    _isolate_queue(monkeypatch, tmp_path, [])
    write_task_result(tmp_path, "latch", STATUS_CANCEL_REQUESTED, result="tearing down")
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/latch/cancel", json={"cascade": True})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "task_id": "latch", "cascade": True}
    assert load_task_result(tmp_path, "latch")["status"] == STATUS_CANCELLED


def test_a_failing_teardown_is_reported_and_recorded_never_swallowed(tmp_path, monkeypatch):
    queue, _pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [{"id": "root", "chat_id": 0, "root_task_id": "root"}],
    )

    def _boom(task_id):
        raise RuntimeError("teardown exploded")

    monkeypatch.setattr(queue, "cancel_task_by_id", _boom)
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel", json={"cascade": True})
    # The cascade is awaited, so a teardown that explodes is REPORTED (never an
    # ok:true cancellation that did not happen): the intent was recorded first,
    # so the answer is the typed pending Stop the watchdog finishes, and the
    # failure is also a durable owner-visible incident. The server does not crash.
    assert response.status_code == 202
    assert response.json() == {
        "ok": True, "task_id": "root", "cancel_state": "pending", "stop_policy": "immediate",
        "request_id": response.json()["request_id"], "reason_code": "teardown_unconfirmed",
        "cascade": True,
    }
    from ouroboros.cancel_intents import active_intent

    assert (active_intent(tmp_path, "root") or {}).get("request_id") == response.json()["request_id"]
    log_path = tmp_path / "logs" / "supervisor.jsonl"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    errors = [row for row in rows if row.get("type") == "task_cancel_cascade_error"]
    assert errors and errors[0]["task_id"] == "root"
    assert "teardown exploded" in errors[0]["error"]


def test_running_task_counts_as_live_for_cascade_precheck(tmp_path, monkeypatch):
    queue, _pending = _isolate_queue(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        queue,
        "RUNNING",
        {"root": {"task": {"id": "root", "root_task_id": "root"}}},
    )
    seen: list[str] = []
    monkeypatch.setattr(queue, "cancel_task_by_id", lambda task_id: seen.append(task_id) or True)
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel", json={"cascade": True})
    assert response.status_code == 200
    assert seen == ["root"]


def test_non_boolean_cascade_is_a_client_error_not_a_subtree_cancel(tmp_path, monkeypatch):
    """`{"cascade": "false"}` must never select the destructive path — a non-empty
    string is truthy in Python, so the flag is parsed STRICTLY and a non-boolean
    value is a 400 rather than a silent single-task (or worse, subtree) cancel."""
    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root"},
            {"id": "child", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root"},
        ],
    )
    with _client(tmp_path) as client:
        for bad in ("false", "true", 1, 0):
            response = client.post("/api/tasks/root/cancel", json={"cascade": bad})
            assert response.status_code == 400, (bad, response.text)
            assert "cascade must be a boolean" in response.text
    # Nothing was cancelled by any of the rejected requests.
    assert [task["id"] for task in pending] == ["root", "child"]


def test_malformed_or_non_object_body_is_rejected(tmp_path, monkeypatch):
    """A body that is PRESENT but unparseable (or not a JSON object) must be a 400,
    never a silent fallback to the single-task cancel: a client that meant to
    cascade would otherwise get its root cancelled and its descendants left live."""
    queue, pending = _isolate_queue(
        monkeypatch, tmp_path, [{"id": "root", "chat_id": 0, "root_task_id": "root"}],
    )
    with _client(tmp_path) as client:
        for payload in (b"{not json", b"[]", b"true", b'"cascade"'):
            response = client.post(
                "/api/tasks/root/cancel", content=payload,
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 400, (payload, response.text)
        # Nothing was cancelled by a rejected request.
        assert [task["id"] for task in pending] == ["root"]
        # ...and an ABSENT body still takes the legacy single-task path unchanged.
        assert client.post("/api/tasks/root/cancel").status_code == 200


def test_cancelable_marker_is_lineage_gated_at_emission(tmp_path, monkeypatch):
    """The supervisor stamps `cancelable` ONLY on lineage-resolved non-subagent
    roots, and copies the RUNNING row's authoritative lineage onto the frame: a
    subagent narration must never carry the marker (a lineage-less replay of a
    marked child row could mint a root-shaped card with a live Cancel), while a
    TIMEOUT-RETRY root — whose root_task_id names the original task — must."""
    import types

    from supervisor.events import _handle_send_message

    sent = []
    running = {
        "retry-2": {"task": {
            "id": "retry-2", "root_task_id": "orig-1", "delegation_role": "root",
            "original_task_id": "orig-1", "timeout_retry_from": "orig-1",
        }},
        "child-1": {"task": {
            "id": "child-1", "root_task_id": "root-9", "parent_task_id": "root-9",
            "delegation_role": "subagent",
        }},
    }
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING=running,
        send_with_budget=lambda *a, **k: sent.append(k),
        append_jsonl=lambda *a, **k: None,
    )
    for task_id in ("retry-2", "child-1"):
        _handle_send_message({
            "chat_id": 1, "task_id": task_id, "text": "working",
            "is_progress": True, "format": "markdown",
        }, ctx)

    retry_meta = sent[0]["progress_meta"]
    child_meta = sent[1]["progress_meta"]
    assert retry_meta.get("cancelable") is True
    assert retry_meta.get("root_task_id") == "orig-1", "authoritative lineage rides the frame"
    assert child_meta.get("cancelable") is not True, "a subagent frame never carries the marker"
    assert child_meta.get("parent_task_id") == "root-9"
    assert child_meta.get("delegation_role") == "subagent"


@pytest.fixture(autouse=True)
def _isolate_cancellation_globals_endpoint(monkeypatch, tmp_path):
    """Same module-global rule as tests/test_cancel_cascade_v664.py: these tests
    drive the real cascade path, so the process-global fence registries must be
    reset around each one or a later test enqueuing a task with a common id is
    refused with root_cancelled."""
    import supervisor.queue as queue
    import supervisor.task_lifecycle as tl

    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(tl, "CANCELLED_ROOT_FENCES", {}, raising=False)


def test_cascade_answers_only_after_the_teardown_and_reports_an_unsettled_tree(tmp_path, monkeypatch):
    """The cascade runs before the answer: the response is sent after the
    teardown, so a tree that is still live when the teardown gives up is never
    acknowledged as cancelled — it is the typed pending Stop whose recorded
    intent the watchdog keeps driving."""
    import supervisor.queue as queue

    order: list[str] = []
    # GR3-11a: the endpoint consults liveness through the supervisor.queue
    # re-export (the single public import surface); "root" has no durable row,
    # so a cascade is recorded only while something of it is live.
    monkeypatch.setattr("supervisor.queue.task_subtree_is_live", lambda tid: True)
    monkeypatch.setattr(
        queue, "cancel_task_by_id",
        lambda task_id, **_kw: (order.append("teardown"), True)[1],
    )
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel", json={"cascade": True})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "task_id": "root", "cascade": True}
    assert order == ["teardown"], "the teardown ran before the answer"

    monkeypatch.setattr(queue, "cancel_task_by_id", lambda task_id, **_kw: False)
    with _client(tmp_path) as client:
        unsettled = client.post("/api/tasks/root/cancel", json={"cascade": True})
    assert unsettled.status_code == 202
    assert unsettled.json()["cancel_state"] == "pending"
    assert unsettled.json()["reason_code"] == "teardown_unconfirmed"
    assert unsettled.json()["cascade"] is True


def test_single_task_cancel_reports_an_unconfirmed_teardown_as_pending_not_404(tmp_path, monkeypatch):
    """A worker that refuses to die is neither cancelled nor absent. The legacy
    boolean collapsed both into 404 — telling the caller the task is gone while it
    keeps running. A refused teardown whose recorded intent stays open is the
    typed pending Stop (202), and 404 stays for a task that genuinely is not there."""
    import supervisor.queue as q

    monkeypatch.setattr(q, "cancel_task_custody", lambda tid, **_kw: q.CANCEL_FAILED)
    with _client(tmp_path) as client:
        refused = client.post("/api/tasks/root/cancel")
    assert refused.status_code == 202
    assert refused.json()["cancel_state"] == "pending"
    assert "cascade" not in refused.json()

    monkeypatch.setattr(q, "cancel_task_custody", lambda tid, **_kw: q.CANCEL_NOT_FOUND)
    with _client(tmp_path) as client:
        missing = client.post("/api/tasks/root/cancel")
    assert missing.status_code == 404

    # ...and an already-settled task keeps the legacy INACTIVE answer (404), which
    # its own dedicated test pins explicitly.
    monkeypatch.setattr(q, "cancel_task_custody", lambda tid, **_kw: q.CANCEL_CANCELLED)
    with _client(tmp_path) as client:
        cancelled = client.post("/api/tasks/root/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"ok": True, "task_id": "root"}


def test_plain_cancel_of_an_already_finished_task_keeps_the_legacy_404(tmp_path, monkeypatch):
    """The typed outcome must not widen the legacy envelope: a task that already
    settled on its own is INACTIVE, which the plain path has always answered 404."""
    import supervisor.queue as q

    monkeypatch.setattr(q, "cancel_task_custody", lambda tid, **_kw: q.CANCEL_ALREADY_SETTLED)
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel")
    assert response.status_code == 404


def test_cascade_ingress_mints_the_cascade_scope_itself(tmp_path, monkeypatch):
    """GR2-1a: the HTTP cascade endpoint mints its durable intent WITH
    ``scope=cascade`` at the ingress — even when the supervisor's own
    ``mark_intent_scope`` stamp never runs (the crash-before-stamp window), a
    watchdog replay of the intent re-runs a CASCADE, never a single cancel."""
    queue, _pending = _isolate_queue(monkeypatch, tmp_path, [
        {"id": "root", "chat_id": 0, "root_task_id": "root"},
        {"id": "kid", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root"},
    ])
    # The supervisor-side second line of defense is DEAD in this shape.
    monkeypatch.setattr("ouroboros.cancel_intents.mark_intent_scope",
                        lambda *_a, **_kw: False)
    seen: dict = {}

    def _capture_teardown(task_id, **_kw):
        from ouroboros.cancel_intents import active_intent

        seen.update(active_intent(tmp_path, task_id) or {})
        return True

    monkeypatch.setattr(queue, "cancel_task_by_id", _capture_teardown)
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel", json={"cascade": True})

    assert response.status_code == 200
    assert seen.get("scope") == "cascade", (
        "the ingress-minted intent must already carry the cascade scope"
    )
    assert seen.get("source") == "http_cascade"


def test_plain_stop_now_executes_an_existing_durable_cascade(tmp_path, monkeypatch):
    """The raw body is only an ingress request; durable scope is authoritative.

    Omitting ``cascade=true`` while hardening a graceful cascade must execute
    the existing cascade immediately, never replay it as a narrower single
    cancellation.
    """
    import supervisor.queue as queue

    from ouroboros import cancel_intents as ci

    _isolate_queue(monkeypatch, tmp_path, [
        {"id": "root", "chat_id": 0, "root_task_id": "root"},
        {
            "id": "kid",
            "chat_id": 0,
            "root_task_id": "root",
            "parent_task_id": "root",
        },
    ])
    graceful = ci.request_cancel(
        tmp_path,
        "root",
        reason="finish before stopping the tree",
        source="http_cascade",
        scope=ci.SCOPE_CASCADE,
        requested_stop_policy=ci.STOP_POLICY_FINALIZE,
    )
    cascades: list[str] = []
    monkeypatch.setattr(
        queue,
        "cancel_task_by_id",
        lambda task_id: cascades.append(task_id) or True,
    )
    monkeypatch.setattr(
        queue,
        "cancel_task_custody",
        lambda *_args, **_kwargs: pytest.fail(
            "durable cascade scope must not enter single-task custody"
        ),
    )

    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "task_id": "root", "cascade": True}
    assert cascades == ["root"]
    hardened = ci.active_intent(tmp_path, "root")
    assert hardened is not None
    assert hardened["request_id"] == graceful["request_id"]
    assert hardened["scope"] == ci.SCOPE_CASCADE
    assert ci.stop_policy(hardened) == ci.STOP_POLICY_IMMEDIATE
    assert set(ci.active_intents(tmp_path)) == {"root"}


def test_cascade_over_a_settled_root_leaves_a_replayable_intent_on_crash(tmp_path, monkeypatch):
    """GR2-1b: a cascade over an ALREADY-SETTLED root with live descendants
    mints a durable cascade coordination intent — after a simulated crash
    mid-sweep (teardown reports unsettled) that intent survives as the
    watchdog's replay trigger for the descendants, and the answer says so."""
    from ouroboros.cancel_intents import active_intent
    from ouroboros.task_results import write_task_result

    queue, _pending = _isolate_queue(monkeypatch, tmp_path, [
        {"id": "kid", "chat_id": 7, "root_task_id": "root", "parent_task_id": "root"},
    ])
    write_task_result(tmp_path, "root", "failed", result="root died on budget")
    write_task_result(tmp_path, "kid", "scheduled")
    # Crash mid-sweep: the teardown never settles the tree.
    monkeypatch.setattr(queue, "cancel_task_by_id", lambda task_id, **_kw: False)

    with _client(tmp_path) as client:
        response = client.post("/api/tasks/root/cancel", json={"cascade": True})

    assert response.status_code == 202, "an unsettled tree is never acknowledged as cancelled"
    assert response.json()["cancel_state"] == "pending"
    row = active_intent(tmp_path, "root")
    assert row is not None and row["scope"] == "cascade", (
        "the settled-root cascade intent must survive the crash as the "
        "watchdog's replay trigger for the live descendants"
    )
    assert response.json()["request_id"] == row["request_id"]


# ---------------------------------------------------------------------------
# Intent first (no liveness pre-probe); a recorded, unconfirmed Stop is pending
# ---------------------------------------------------------------------------


def _supervisor_rows(tmp_path, kind):
    path = tmp_path / "logs" / "supervisor.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == kind]


@pytest.mark.parametrize("cascade", [False, True])
def test_unknown_id_is_404_with_no_task_row_and_no_open_intent(tmp_path, monkeypatch, cascade):
    """An id that names no task. The single Stop records its intent and
    custody's miss lane settles it ``not_found`` in the same call; a cascade has
    no recorded tree to stop and mints nothing. Neither invents a task row,
    leaves an intent open or runs a cascade over a phantom tree."""
    from ouroboros.cancel_intents import active_intents
    from ouroboros.task_results import load_task_result

    _isolate_queue(monkeypatch, tmp_path, [])
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/ghost-id/cancel", json={"cascade": cascade})
    assert response.status_code == 404
    assert response.json()["error"] == "task not found or not active"
    assert load_task_result(tmp_path, "ghost-id") is None
    assert active_intents(tmp_path) == {}
    events = [(row.get("event"), row.get("outcome")) for row in _supervisor_rows(tmp_path, "cancel_intent")]
    if cascade:
        assert events == []
        assert _supervisor_rows(tmp_path, "task_cancel_subtree_snapshot") == []
    else:
        assert ("requested", None) in events and ("settled", "not_found") in events, events


@pytest.mark.parametrize("cascade", [False, True])
def test_finished_and_dead_task_is_404_and_mints_nothing(tmp_path, monkeypatch, cascade):
    """Completion wins: a settled task with no live owner left mints no intent
    (no false "Cancelling…" on a finished card) and keeps its stored answer."""
    from ouroboros.cancel_intents import active_intents
    from ouroboros.task_results import load_task_result, write_task_result

    _isolate_queue(monkeypatch, tmp_path, [])
    write_task_result(tmp_path, "done-1", "completed", result="the finished answer")
    before = load_task_result(tmp_path, "done-1")
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/done-1/cancel", json={"cascade": cascade})
    assert response.status_code == 404
    assert active_intents(tmp_path) == {}
    assert [row for row in _supervisor_rows(tmp_path, "cancel_intent") if row.get("event") == "requested"] == []
    assert load_task_result(tmp_path, "done-1") == before


@pytest.mark.parametrize("cascade", [False, True])
def test_persisted_but_never_admitted_task_is_cancelled_by_stop(tmp_path, monkeypatch, cascade):
    """A child stuck at ``requested`` that never reached the queue (the
    admission stall the agent's cancel_task already settles): Stop records the
    intent and custody's miss lane settles it cancelled. A liveness pre-probe
    used to answer 404 here and leave it requested."""
    from ouroboros.cancel_intents import active_intents
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result

    _isolate_queue(monkeypatch, tmp_path, [])
    write_task_result(tmp_path, "stuck-kid", "requested", parent_task_id="coord",
                      root_task_id="coord", delegation_role="subagent")
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/stuck-kid/cancel", json={"cascade": cascade})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "task_id": "stuck-kid", **({"cascade": True} if cascade else {})}
    assert load_task_result(tmp_path, "stuck-kid")["status"] == STATUS_CANCELLED
    assert active_intents(tmp_path) == {}


def test_settled_task_with_a_live_worker_is_still_killed_and_keeps_its_answer(tmp_path, monkeypatch):
    """Stored completion can precede the worker's exit (post-task cognition still
    spends). Once the intent request finds the target settled, live ownership
    qualifies it: the worker is killed, the stored answer survives, and
    completion wins the envelope (404)."""
    import types

    from supervisor import workers

    from ouroboros.cancel_intents import active_intents
    from ouroboros.task_results import load_task_result, write_task_result

    queue, _pending = _isolate_queue(monkeypatch, tmp_path, [])
    monkeypatch.setattr(workers, "respawn_worker", lambda wid: None, raising=False)
    monkeypatch.setattr(workers, "get_event_q", lambda *, create=True: types.SimpleNamespace(put=lambda evt: None),
                        raising=False)
    monkeypatch.setattr(queue, "_emit_cancel_task_done", lambda *_a, **_kw: None)
    alive = {"value": True}
    proc = types.SimpleNamespace(pid=4242, is_alive=lambda: alive["value"],
                                 terminate=lambda: alive.update(value=False), join=lambda timeout=None: None)
    workers.WORKERS[0] = types.SimpleNamespace(wid=0, proc=proc, busy_task_id="burning", reaping=False)
    monkeypatch.setattr("ouroboros.platform_layer.kill_pid_tree", lambda *_a, **_kw: alive.update(value=False))
    queue.RUNNING["burning"] = {"task": {"id": "burning", "chat_id": 0, "root_task_id": "burning"}, "worker_id": 0}
    write_task_result(tmp_path, "burning", "completed", chat_id=0, result="the stored answer")
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/burning/cancel")
    assert response.status_code == 404, response.text
    assert alive["value"] is False, "the still-spending worker is killed"
    stored = load_task_result(tmp_path, "burning")
    assert (stored["status"], stored["result"]) == ("completed", "the stored answer")
    assert active_intents(tmp_path) == {}


def test_stop_records_its_intent_before_custody_even_when_admission_races_the_click(tmp_path, monkeypatch):
    """The single Stop used to ask liveness first, mint only for a live answer and
    call custody anyway: a task admitted between the probe and custody was torn
    down with no durable intent. No probe gates the intent now — even when every
    liveness observation says "not live", custody starts from a recorded,
    watchdog-replayable intent."""
    from ouroboros.cancel_intents import active_intent
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result

    queue, pending = _isolate_queue(monkeypatch, tmp_path, [{"id": "late", "chat_id": 1, "root_task_id": "late"}])
    # Observed before scheduler admission: both liveness readers say "not live".
    monkeypatch.setattr(queue, "task_has_live_ownership", lambda *_a, **_kw: False)
    monkeypatch.setattr(queue, "task_subtree_is_live", lambda *_a, **_kw: False)
    write_task_result(tmp_path, "late", "scheduled")
    intents_at_custody = []
    real_custody = queue.cancel_task_custody

    def _custody(task_id, **kwargs):
        intents_at_custody.append(active_intent(tmp_path, task_id))
        return real_custody(task_id, **kwargs)

    monkeypatch.setattr(queue, "cancel_task_custody", _custody)
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/late/cancel")
    assert response.status_code == 200, response.text
    assert intents_at_custody and (intents_at_custody[0] or {}).get("source") == "http_single", intents_at_custody
    assert load_task_result(tmp_path, "late")["status"] == STATUS_CANCELLED
    assert pending == []


@pytest.mark.parametrize("cascade", [False, True])
def test_unconfirmed_teardown_answers_pending_and_the_watchdog_settles_it(tmp_path, monkeypatch, cascade):
    """The intent is recorded but this request cannot confirm the teardown (a
    busy store or a refused custody leaves it open with nothing captured): 202
    pending with the intent's request id — never 503 for a Stop that is recorded
    — and the cancel-intent watchdog finishes it once custody can."""
    import time

    from supervisor.task_lifecycle import sweep_cancel_intents

    from ouroboros.cancel_intents import active_intent, active_intents
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result, write_task_result

    queue, pending = _isolate_queue(monkeypatch, tmp_path, [{"id": "busy-1", "chat_id": 0, "root_task_id": "busy-1"}])
    write_task_result(tmp_path, "busy-1", "scheduled")
    store = {"busy": True}
    real_custody = queue.cancel_task_custody

    def _custody(task_id, **kwargs):
        if store["busy"]:
            return queue.CANCEL_FAILED  # nothing captured, the intent stays open
        return real_custody(task_id, **kwargs)

    monkeypatch.setattr(queue, "cancel_task_custody", _custody)
    with _client(tmp_path) as client:
        response = client.post("/api/tasks/busy-1/cancel", json={"cascade": cascade})
    intent = active_intent(tmp_path, "busy-1")
    assert response.status_code == 202, response.text
    assert response.json() == {
        "ok": True, "task_id": "busy-1", "cancel_state": "pending", "stop_policy": "immediate",
        "request_id": intent["request_id"], "reason_code": "teardown_unconfirmed",
        **({"cascade": True} if cascade else {}),
    }
    assert [task["id"] for task in pending] == ["busy-1"], "nothing was torn down yet"

    store["busy"] = False
    assert sweep_cancel_intents(now=time.time() + 60) == {"busy-1": queue.CANCEL_CANCELLED}
    assert load_task_result(tmp_path, "busy-1")["status"] == STATUS_CANCELLED
    assert active_intents(tmp_path) == {}
    assert pending == []


def test_stop_action_replay_joins_the_pending_intent_and_other_arguments_are_409(tmp_path, monkeypatch):
    """The exact retry of one Stop action joins the recorded intent (same request
    id, still pending); the same stop_action_id with different arguments is
    refused 409 and changes nothing."""
    from ouroboros.task_results import write_task_result

    queue, pending = _isolate_queue(monkeypatch, tmp_path, [{"id": "act-1", "chat_id": 0, "root_task_id": "act-1"}])
    write_task_result(tmp_path, "act-1", "scheduled")
    monkeypatch.setattr(queue, "cancel_task_custody", lambda task_id, **_kw: queue.CANCEL_FAILED)
    with _client(tmp_path) as client:
        first = client.post("/api/tasks/act-1/cancel", json={"stop_action_id": "stop-a"})
        replay = client.post("/api/tasks/act-1/cancel", json={"stop_action_id": "stop-a"})
        conflict = client.post("/api/tasks/act-1/cancel", json={"stop_action_id": "stop-a", "cascade": True})
    assert (first.status_code, replay.status_code) == (202, 202)
    assert replay.json()["request_id"] == first.json()["request_id"]
    assert conflict.status_code == 409
    assert conflict.json()["reason_code"] == "stop_action_conflict"
    assert [task["id"] for task in pending] == ["act-1"]
