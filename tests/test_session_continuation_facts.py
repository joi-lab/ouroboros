"""Route facts of a delegated session's next turn, stated flat and neutrally.

A run paused on a question keeps its session: every ``waiting_on_user`` payload —
inline, spilled and the re-wait of a question the model already saw — carries
``continuation: same_session`` plus a cost note; a codex-shaped ``input_required``
terminal carries the honest opposite, ``continuation: new_physical_run``. No payload
names a question a contribution, no contract field opts anything in, and nothing
parses a participant's labels: participation rules travel in the parent's own
objective/constraints or prompt.
"""

from __future__ import annotations

import json
import queue as stdqueue

from ouroboros.delegate_interactions import SAME_SESSION_CONTINUATION, _waiting_on_user_payload

_EXPECTED = {
    "continuation": "same_session",
    "continuation_note": ("the answer resumes THIS session (delegate_answer, free_text "
                          "included); each resumed turn is a paid/quota round"),
}


def _pending_row(iid="int-1", question="Which port should the server use?"):
    return {
        "interactionId": iid, "runId": "run-1", "attemptId": "a01", "harnessId": "claude",
        "sourceTool": "AskUserQuestion",
        "questions": [{"id": "q1", "question": question, "header": "Port",
                       "options": [{"label": "8080", "description": "the default"}],
                       "multi_select": False}],
        "requestedAt": "2026-08-11T10:00:00Z", "timeoutAt": "2026-08-11T10:15:00Z",
    }


def _wait_ctx(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path,
                      task_constraint=TaskConstraint(mode="local_readonly_subagent"))
    ctx.task_id = "t-nanny"
    ctx.event_queue = stdqueue.Queue()
    return ctx


def test_the_waiting_payload_carries_the_flat_continuation_inline(tmp_path):
    from ouroboros.gateways.claudexor import pending_interactions

    pending = pending_interactions({"pendingInteractions": [_pending_row()]})
    out = json.loads(_waiting_on_user_payload(_wait_ctx(tmp_path), "run-1", "running", 5, pending))

    assert out["status"] == "waiting_on_user"
    assert "interactions_delivery" not in out  # inline branch
    assert {key: out[key] for key in _EXPECTED} == _EXPECTED
    assert SAME_SESSION_CONTINUATION == _EXPECTED
    assert "contribution" not in json.dumps(out)


def test_the_spilled_waiting_payload_keeps_the_continuation(tmp_path):
    """The bounded/spilled preview sheds question rows and the advances ride-along,
    never the route fact."""
    from ouroboros.gateways.claudexor import pending_interactions
    from ouroboros.loop_tool_execution import _truncate_tool_result
    from ouroboros.tool_capabilities import tool_result_limit

    rows = [_pending_row(iid=f"int-{i}", question="Q" * 4_000) for i in range(6)]
    pending = pending_interactions({"pendingInteractions": rows})
    raw = _waiting_on_user_payload(_wait_ctx(tmp_path), "run-1", "running", 5, pending)

    assert len(raw) <= tool_result_limit("delegate_wait")
    assert _truncate_tool_result(raw, "delegate_wait", {}) == raw
    out = json.loads(raw)
    assert out["status"] == "waiting_on_user"
    assert out["interactions_delivery"]["complete"] is False  # the spilled branch
    assert out["interactions_omitted"] >= 0 and len(out["pending_interactions"]) <= len(rows)
    assert {key: out[key] for key in _EXPECTED} == _EXPECTED


def test_an_input_required_terminal_names_a_new_physical_run():
    from ouroboros.subagents import DelegatedRunShape
    from ouroboros.tools.delegate_terminal_evidence import _terminal_payload

    shape = DelegatedRunShape(access="readonly", mode="ask", isolation="", delegated=False)
    asked = {"lastSeq": 9, "summary": {
        "state": "failed",
        "outcomeFacts": {"reason": "input_required", "required_inputs": ["which database?"]},
    }}
    payload = _terminal_payload("run-1", asked, shape)
    assert "input_required_note" in payload
    assert payload["continuation"] == "new_physical_run"

    finished = _terminal_payload("run-2", {"lastSeq": 3, "summary": {
        "state": "succeeded", "outcomeFacts": {"reason": "completed"}}}, shape)
    assert "input_required_note" not in finished and "continuation" not in finished


def test_no_contract_field_or_host_section_opts_a_participant_in():
    """The opt-in lives in objective/constraints or the prompt, never in a frozen
    contract value or a host-authored instruction section (owner: no fixed topology)."""
    from ouroboros.contracts.task_contract import normalize_answer_protocol
    from ouroboros.delegate_start_instructions import HOST_INSTRUCTIONS

    assert normalize_answer_protocol("contribution_turn") == ""  # not a protocol value
    assert normalize_answer_protocol("final_answer_line") == "final_answer_line"
    assert "CONTRIBUTION TURN" not in HOST_INSTRUCTIONS
