"""Tests for ouroboros/tools/git_rollback.py: the preview, and the confirmed call as a
``rollback_request`` the supervisor answers through the UI Rollback door (P4-S)."""

import queue
import subprocess

import pytest

from ouroboros.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path):
    """Minimal ToolContext pointing at a temporary git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    (repo / "f.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return ToolContext(repo_dir=str(repo), drive_root=str(tmp_path / "data"))


def test_rollback_empty_target(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    result = _rollback_to_target(ctx, target="", confirm=True)
    assert "ROLLBACK_ERROR" in result
    assert "required" in result


def test_rollback_preview_valid_sha(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ctx.repo_dir, capture_output=True, text=True,
    ).stdout.strip()
    result = _rollback_to_target(ctx, target=sha, confirm=False)
    assert "Will reset" in result
    assert sha[:8] in result
    assert "confirm=true" in result


def test_rollback_preview_invalid_target(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    result = _rollback_to_target(ctx, target="nonexistent_tag_xyz", confirm=False)
    assert "ROLLBACK_ERROR" in result
    assert "Cannot resolve" in result


def test_rollback_confirm_emits_the_request_and_answers_requested(ctx):
    """A confirmed call resets nothing in the worker: one ``rollback_request`` event, bound to
    the resolved SHA, reaches the supervisor; the answer says the request was taken."""
    from ouroboros.tools.git_rollback import _rollback_to_target
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ctx.repo_dir, capture_output=True, text=True).stdout.strip()
    ctx.event_queue = queue.Queue()
    ctx.task_id = "task-1"
    result = _rollback_to_target(ctx, target="HEAD", confirm=True)
    assert "requested" in result and sha[:8] in result and "ROLLBACK_ERROR" not in result
    event = ctx.event_queue.get_nowait()
    assert event["type"] == "rollback_request" and event["target"] == "HEAD" and event["target_sha"] == sha
    assert event["reason"] == "agent_rollback_tool" and event["task_id"] == "task-1" and event["ts"]
    assert ctx.event_queue.empty() and getattr(ctx, "pending_restart_reason", None) is None
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ctx.repo_dir, capture_output=True, text=True).stdout == ""


def test_rollback_confirm_failure(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    ctx.event_queue = queue.Queue()
    result = _rollback_to_target(ctx, target="badref", confirm=True)
    assert "ROLLBACK_ERROR" in result and "Cannot resolve" in result
    assert ctx.event_queue.empty()


def test_rollback_confirm_without_a_supervisor_is_an_error(ctx):
    """No event queue (no supervisor): the tool refuses instead of resetting on its own."""
    from ouroboros.tools.git_rollback import _rollback_to_target
    ctx.event_queue = None
    result = _rollback_to_target(ctx, target="HEAD", confirm=True)
    assert "ROLLBACK_ERROR" in result and "UI Rollback" in result


def test_get_tools_exports():
    from ouroboros.tools.git_rollback import get_tools
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "vcs_rollback"
    schema = tools[0].schema
    assert "target" in schema["parameters"]["properties"]
    assert "confirm" in schema["parameters"]["properties"]


def test_rollback_in_core_tool_names():
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "vcs_rollback" in CORE_TOOL_NAMES


def test_rollback_in_initial_tool_schemas():
    """rollback_to_target must be visible in the runtime initial_tool_schemas path."""
    import pathlib, tempfile
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import initial_tool_schemas
    with tempfile.TemporaryDirectory() as d:
        reg = ToolRegistry(repo_dir=pathlib.Path(d), drive_root=pathlib.Path(d))
        schemas = initial_tool_schemas(reg)
        names = {s["function"]["name"] for s in schemas}
        assert "vcs_rollback" in names
