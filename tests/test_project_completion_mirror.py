"""Main mirrors a Project root's final answer as an ordinary Ouroboros message.

The completion row keeps its plain ``text`` byte-identical (Telegram, the
model's own Main context and older clients read only that) and carries ONE
additive typed key, ``completion_answer``, only for a model-authored final
answer of a settled task. These tests pin the gate in both directions, the
unchanged text, and the key's survival live -> durable row -> history replay.
"""

from __future__ import annotations

import asyncio
import json
import types
from types import SimpleNamespace

import pytest

import supervisor.message_bus as message_bus
from ouroboros.projects_registry import MIRRORED_ANSWER_MAX_CHARS, mirrored_answer

ANSWER = "**Done: PR #7 merged.**\n\n## What changed\n\n- one\n- two\n"


def _result(**overrides):
    row = {"result": ANSWER, "terminal_origin": "model_final"}
    row.update(overrides)
    return row


@pytest.mark.parametrize("phase", ["done", "warn", "error", "cancelled"])
def test_model_final_answer_is_mirrored_whole_for_every_terminal_phase(phase):
    # The mind's own words ride the row uncut, whatever word the host folded:
    # a Failed task that explains itself is still Ouroboros speaking.
    assert mirrored_answer(_result(), phase) == {"completion_answer": ANSWER.strip()}


@pytest.mark.parametrize("result, phase", [
    (_result(terminal_origin="host_salvage"), "error"),   # preserved bytes are not a final answer
    (_result(terminal_origin="host_notice"), "error"),    # the host's own notice is not his voice
    (_result(terminal_origin=""), "done"),                # unknown authorship is never assumed
    (_result(result="   "), "done"),                      # nothing to say
    (_result(result={"answer": "x"}), "done"),            # a malformed result is never repr()-ed into his voice
    (_result(), "working"),                               # the outbox freezes the first send
    (_result(), ""),
    (_result(result="x" * (MIRRORED_ANSWER_MAX_CHARS + 1)), "done"),  # over the ceiling: pointer, never a cut
    (None, "done"),
])
def test_everything_else_keeps_the_row_a_pointer(result, phase):
    assert mirrored_answer(result, phase) == {}


def test_the_ceiling_is_inclusive_and_nothing_is_ever_cut():
    exact = "y" * MIRRORED_ANSWER_MAX_CHARS
    assert mirrored_answer(_result(result=exact), "done") == {"completion_answer": exact}


def _enqueue_row(tmp_path, monkeypatch, result):
    from ouroboros.project_dialogue import enqueue_project_completion_summary
    from ouroboros.projects_registry import bind_task_to_project, create_project

    project = create_project(tmp_path, "launch", name="Launch")
    bind_task_to_project(tmp_path, "root-project", project["id"], project["chat_id"],
                         origin={"absent": "system"})
    queued = []
    monkeypatch.setattr("supervisor.terminal_delivery.enqueue_terminal_delivery",
                        lambda _root, event, **_kw: queued.append(dict(event)) or True)
    ctx = types.SimpleNamespace(DRIVE_ROOT=tmp_path)
    root = {"id": "root-project", "project_id": "launch", "title": "Ship release",
            "chat_id": project["chat_id"]}
    row = {"task_id": "root-project", "status": "completed", "project_id": "launch",
           "title": "Ship release", **result}
    done = {"status": "completed", "outcome_axes": {"execution": {"status": "ok"}}}
    assert enqueue_project_completion_summary(
        ctx.DRIVE_ROOT, {"status": "completed"}, "root-project", root, row, done) is True
    return queued[0]


def test_the_row_carries_the_answer_and_its_text_does_not_change(tmp_path, monkeypatch):
    mirrored = _enqueue_row(tmp_path / "a", monkeypatch, _result())
    pointer = _enqueue_row(tmp_path / "b", monkeypatch, _result(terminal_origin="host_salvage"))

    assert mirrored["progress_meta"]["completion_answer"] == ANSWER.strip()
    assert mirrored["role"] == "system"
    assert mirrored["system_type"] == "project_completion_summary"
    # Telegram, the model's own context and older clients read `text` alone:
    # the answer never leaks into it, and the pointer sentence stays.
    assert mirrored["text"] == "Launch › Ship release · Done\nOpen the Project for details."
    assert "PR #7" not in mirrored["text"]
    # Without a model-authored answer the key is ABSENT, not empty.
    assert "completion_answer" not in pointer["progress_meta"]


def test_the_answer_survives_live_frame_durable_row_and_history_replay(monkeypatch, tmp_path):
    bridge = message_bus.LocalChatBridge({})
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "get_bridge", lambda: bridge)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"session_id": "s", "owner_id": 7})
    monkeypatch.setattr(message_bus, "_advance_project_visible_revision", lambda _chat_id: None)
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_k: None)

    message_bus.send_with_budget(
        1, "Launch › Ship release · Done\nOpen the Project for details.",
        task_id="root-project", role="system", system_type="project_completion_summary",
        progress_meta={"project_id": "launch", "project_name": "Launch",
                       "target_label": "Launch › Ship release", "status": "completed",
                       "completion_answer": ANSWER,
                       "terminal_time": {"v": 1, "source": "executor_terminal", "occurred_at": "2026-09-24T18:18:27Z"}},
    )

    live = next(frame for frame in frames if frame.get("type") == "chat")
    assert live["terminal_time"]["occurred_at"] == "2026-09-24T18:18:27Z"
    assert live["completion_answer"] == ANSWER
    assert live["role"] == "system"   # the wire voice is unchanged: one ending, one ring

    durable = [json.loads(line) for line in
               (tmp_path / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    stored = next(row for row in durable if row.get("type") == "project_completion_summary")
    assert stored["completion_answer"] == ANSWER
    assert stored["text"] == "Launch › Ship release · Done\nOpen the Project for details."

    from ouroboros.gateway.history import make_chat_history_endpoint

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"chat_id": "1", "limit": "20"})))
    replayed = next(row for row in json.loads(response.body.decode("utf-8"))["messages"]
                    if row.get("system_type") == "project_completion_summary")
    # Markdown in the answer is NOT normalised away: the read-side plain
    # normalisation of lifecycle rows applies to `text` only.
    assert replayed["completion_answer"] == ANSWER
    assert replayed["text"] == stored["text"]
    assert replayed["terminal_time"] == stored["terminal_time"] == live["terminal_time"]


def test_a_row_without_the_key_replays_without_it(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    (logs / "chat.jsonl").write_text(json.dumps({
        "ts": "2026-08-21T00:00:03Z", "direction": "system", "chat_id": 1,
        "type": "project_completion_summary", "task_id": "root-project", "project_id": "launch",
        "project_name": "Launch", "target_label": "Launch › Ship release", "status": "completed",
        "text": "Launch › Ship release · Completed",
    }) + "\n", encoding="utf-8")

    from ouroboros.gateway.history import make_chat_history_endpoint

    endpoint = make_chat_history_endpoint(tmp_path)
    response = asyncio.run(endpoint(SimpleNamespace(query_params={"chat_id": "1"})))
    row = json.loads(response.body.decode("utf-8"))["messages"][0]
    assert "completion_answer" not in row
