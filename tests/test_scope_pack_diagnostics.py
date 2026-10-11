"""Scope preparation diagnostics survive the move from packets to retrieval."""

from __future__ import annotations

from dataclasses import asdict
import json
import queue
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ouroboros import utils
from ouroboros.tools import review_admission as admission
from ouroboros.tools import scope_review as sr
from ouroboros.tools import scope_review_session as session
from supervisor.log_addressing import make_server_log_sink


pytestmark = pytest.mark.serial
_TASK_ADDRESS = {"chat_id": 42, "parent_task_id": "scope-parent", "root_task_id": "scope-root"}


@pytest.fixture
def scope_env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "CHECKLISTS.md").write_text(
        "## Intent / Scope Review Checklist\n\nplaceholder\n", encoding="utf-8")
    (repo / "docs" / "DEVELOPMENT.md").write_text("development\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("constitution\n", encoding="utf-8")
    (repo / ".gitignore").write_text(".review-drive/\n", encoding="utf-8")
    (repo / "example.py").write_text("value = 1\n", encoding="utf-8")
    for args in (("init",), ("add", "."), ("commit", "-m", "initial")):
        subprocess.run(
            ["git", "-c", "user.email=test@example.test", "-c", "user.name=Test", *args],
            cwd=repo, check=True, capture_output=True,
        )
    (repo / "example.py").write_text("value = 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    drive = tmp_path / "drive"
    ctx = SimpleNamespace(
        repo_dir=repo, drive_root=drive, task_id="scope-task",
        drive_logs=lambda: drive / "logs", pending_events=[],
    )
    monkeypatch.setattr(session, "scope_first_send_bound", lambda _brief: 900_000)
    dispatch = Mock(side_effect=AssertionError("preparation must not dispatch a reviewer"))
    monkeypatch.setattr(sr, "_call_scope_llm", dispatch)
    forwarded = []
    bridge = SimpleNamespace(push_log=forwarded.append)
    monkeypatch.setattr(utils, "_log_sink", make_server_log_sink(
        bridge, drive, running={"scope-task": {"task": dict(_TASK_ADDRESS)}},
    ))
    token = sr._SCOPE_CONTEXT_MANIFEST.set({})
    yield ctx, forwarded, dispatch
    sr._SCOPE_CONTEXT_MANIFEST.reset(token)
    dispatch.assert_not_called()


def _events(ctx):
    path = ctx.drive_root / "logs" / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.parametrize("route", ["api_chat", "agent_session"])
@pytest.mark.parametrize("model_override", [False, True])
def test_context_failure_persists_and_forwards_one_row(scope_env, monkeypatch, route, model_override):
    ctx, forwarded, _ = scope_env
    # Exercise the actual builder's missing-checklist refusal and direct caller.
    monkeypatch.setattr(session, "load_checklist_section", lambda _section: "")
    waiter = SimpleNamespace(overrides={"reviewer:scope-slot": {
        "model": "test/final", "model_account_override": "profile-final", "use_local": False,
    }} if model_override else {})
    monkeypatch.setattr("ouroboros.model_wait.current_model_wait", lambda: waiter)

    final = sr.run_scope_review(ctx, "Update value", scope_model="test/model", slot_id="scope-slot", route=route)

    expected_model = "test/final" if model_override and route == "api_chat" else "test/model"
    assert final.blocked and final.status == "error"
    assert final.failure_phase == "context" and final.failure_code == "context_unavailable"
    assert final.model_id == expected_model and "could not be loaded" in final.block_message
    event, = _events(ctx)
    assert forwarded == [{**event, **_TASK_ADDRESS}]
    assert not _TASK_ADDRESS.keys() & event.keys(), "addressing must not mutate the durable row"
    assert event["ts"]
    assert {key: value for key, value in event.items() if key != "ts"} == {
        "type": "scope_review_preparation_failed", "task_id": "scope-task",
        "slot_id": "scope-slot", "model": expected_model, "status": "error",
        "failure_phase": "context", "failure_code": "context_unavailable",
        "reason": (
            "Intent / Scope Review Checklist could not be loaded from docs/CHECKLISTS.md — "
            "scope review cannot run without its checklist (fail-closed)."
        ),
    }
    assert ctx.pending_events == []


@pytest.mark.parametrize("failure", ["false", "append_error", "missing_logs", "sink_error"])
def test_diagnostic_failure_preserves_exact_preparation_result(scope_env, monkeypatch, failure):
    ctx, _, _ = scope_env
    monkeypatch.setattr(session, "load_checklist_section", lambda _section: "")
    with monkeypatch.context() as baseline:
        baseline.setattr(sr, "append_jsonl", lambda *_args: False)
        _, expected = admission.prepare_scope_review(ctx, "Update value", scope_model="test/model")
    calls = []

    def broken_append(*args):
        calls.append(args)
        if failure == "append_error":
            raise OSError("log unavailable")
        return False

    def broken_sink(event):
        calls.append(event)
        raise RuntimeError("forwarder unavailable")

    if failure in {"false", "append_error"}:
        monkeypatch.setattr(sr, "append_jsonl", broken_append)
    elif failure == "missing_logs":
        del ctx.drive_logs
    else:
        monkeypatch.setattr(utils, "_log_sink", broken_sink)
    prepared, final = admission.prepare_scope_review(ctx, "Update value", scope_model="test/model")

    assert prepared is None and asdict(final) == asdict(expected)
    assert len(calls) == (0 if failure == "missing_logs" else 1)
    assert len(_events(ctx)) == (1 if failure == "sink_error" else 0)


@pytest.mark.parametrize("route", ["api_chat", "agent_session"])
@pytest.mark.parametrize("delivery", ["inline", "paged", "retrieved_by_reviewer"])
def test_successful_retrieval_preparation_emits_no_failure(scope_env, monkeypatch, route, delivery):
    from ouroboros.tools import review_binary_context

    ctx, forwarded, _ = scope_env
    if delivery == "paged":
        # Use the real source store and reader addresses, with a small test ceiling.
        monkeypatch.setattr(session, "scope_first_send_bound", lambda _brief: 1)
        monkeypatch.setattr(session, "SESSION_INLINE_DIFF_CEILING_CHARS", 1)
    elif delivery == "retrieved_by_reviewer":
        monkeypatch.setattr(review_binary_context, "capture_staged_diff", Mock(
            side_effect=review_binary_context.StagedDiffUnavailable("fixture diff unavailable")))
    prepared, final = admission.prepare_scope_review(
        ctx, "Update value", scope_model="test/model", slot_id="scope-slot", route=route,
        subagent_id="fixture-actor",
    )

    assert final is None
    assert prepared["context_manifest"]["diff_delivery"] == delivery, prepared["context_manifest"]
    assert _events(ctx) == forwarded == []
    if delivery == "paged":
        from ouroboros.artifacts import read_actor_source_bytes

        source = prepared["context_manifest"]["diff_source"]
        assert read_actor_source_bytes(ctx.drive_root, ctx.task_id, source).decode("utf-8") == (
            review_binary_context.capture_staged_diff(ctx.repo_dir))


def test_model_control_error_propagates_before_diagnostic(scope_env, monkeypatch):
    from ouroboros.llm_claudexor import ClaudexorModelError

    ctx, forwarded, _ = scope_env
    error = ClaudexorModelError({"code": "model_outcome_unknown", "message": "original custody"})
    monkeypatch.setattr(session, "build_scope_session_task", Mock(side_effect=error))
    with pytest.raises(ClaudexorModelError) as raised:
        admission.prepare_scope_review(ctx, "Update value", scope_model="test/model")
    assert raised.value is error and _events(ctx) == forwarded == []


def test_worker_log_envelope_forwards_without_a_new_dispatch_kind(scope_env, monkeypatch):
    from supervisor import events
    from supervisor.worker_process import WORKER_LOG_SINK_SUPPRESSED_TYPES

    ctx, forwarded, _ = scope_env
    monkeypatch.setattr(session, "load_checklist_section", lambda _section: "")
    outgoing = queue.Queue()
    monkeypatch.setattr(utils, "_log_sink", lambda row: utils.emit_log_event(outgoing, row))
    sr.run_scope_review(ctx, "Update value", scope_model="test/model")
    durable, = _events(ctx)
    assert durable["type"] not in WORKER_LOG_SINK_SUPPRESSED_TYPES
    envelope = outgoing.get_nowait()
    assert outgoing.empty() and envelope == {"type": "log_event", "data": durable}
    events.dispatch_event(envelope, SimpleNamespace(
        DRIVE_ROOT=ctx.drive_root, RUNNING={ctx.task_id: {"task": dict(_TASK_ADDRESS)}},
        bridge=SimpleNamespace(push_log=forwarded.append), append_jsonl=utils.append_jsonl,
    ))
    assert forwarded == [{**durable, **_TASK_ADDRESS}] and _events(ctx) == [durable]


def test_parallel_preparation_failure_stays_row_local(scope_env, monkeypatch):
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools import parallel_review

    ctx, forwarded, _ = scope_env
    slots = [SimpleNamespace(
        slot_id=name, model="test/model", route=route, effort="", session_target="",
        session_profile="", subagent_id="fixture-actor",
    ) for name, route in [("scope-native", ReviewRouteKind.API_CHAT), ("scope-session", ReviewRouteKind.AGENT_SESSION)]]
    monkeypatch.setattr(parallel_review, "scope_reviewer_slots", lambda: slots)
    original = session.build_scope_session_task

    def build(repo, brief):
        if brief.slot_id == "scope-native":
            raise RuntimeError("fixture context unavailable")
        return original(repo, brief)

    monkeypatch.setattr(session, "build_scope_session_task", build)
    rows = parallel_review._prepare_scope_rows(
        ctx, "Update value", goal="", scope="", review_rebuttal="", history_snapshot=[], scope_history=[],
    )
    assert rows[0]["prepared"] is None and rows[0]["final"].failure_code == "context_unavailable"
    assert rows[1]["prepared"] and rows[1]["final"] is None
    event, = _events(ctx)
    assert forwarded == [{**event, **_TASK_ADDRESS}] and event["slot_id"] == "scope-native"
    assert not {"blocked", "verdict", "block_message", "context_manifest"} & event.keys()
