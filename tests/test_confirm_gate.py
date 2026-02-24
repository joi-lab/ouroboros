"""Tests for confirm-gate sensitive action approvals."""

from __future__ import annotations

import pathlib
import tempfile

from ouroboros.confirm_gate import (
    approve_request,
    guard_tool_call,
    pending_requests,
    request_id_from_text,
)


def _tmp_drive() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp())


def test_non_sensitive_call_passes():
    drive = _tmp_drive()
    msg = guard_tool_call(drive, "repo_read", {"path": "README.md"}, task_id="t1")
    assert msg is None


def test_sensitive_call_requires_approval_then_consumes():
    drive = _tmp_drive()

    first = guard_tool_call(
        drive,
        "repo_commit_push",
        {"commit_message": "test publish"},
        task_id="t2",
    )
    assert first is not None
    assert "CONFIRM_REQUIRED" in first
    req_id = request_id_from_text(first)
    assert req_id.startswith("cfm-")

    ok, approve_msg = approve_request(drive, req_id, approver_id=123)
    assert ok
    assert "Approved" in approve_msg

    second = guard_tool_call(
        drive,
        "repo_commit_push",
        {"commit_message": "test publish"},
        task_id="t2",
    )
    assert second is None

    third = guard_tool_call(
        drive,
        "repo_commit_push",
        {"commit_message": "test publish"},
        task_id="t2",
    )
    assert third is not None
    assert "CONFIRM_REQUIRED" in third


def test_toggle_evolution_requires_approval_only_for_enable():
    drive = _tmp_drive()

    off_msg = guard_tool_call(
        drive,
        "toggle_evolution",
        {"enabled": False},
        task_id="t3",
    )
    assert off_msg is None

    on_msg = guard_tool_call(
        drive,
        "toggle_evolution",
        {"enabled": True},
        task_id="t3",
    )
    assert on_msg is not None
    assert "CONFIRM_REQUIRED" in on_msg


def test_sensitive_shell_detection_creates_pending_request():
    drive = _tmp_drive()
    msg = guard_tool_call(
        drive,
        "run_shell",
        {"cmd": ["bash", "-lc", "echo $OPENAI_API_KEY"]},
        task_id="t4",
    )
    assert msg is not None
    assert "CONFIRM_REQUIRED" in msg

    pend = pending_requests(drive)
    assert len(pend) >= 1
    assert any(str(r.get("category")) == "credentials_or_access" for r in pend)
