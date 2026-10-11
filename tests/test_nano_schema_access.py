"""A compact schema view must not become a new tool permission system."""
from __future__ import annotations

import pytest

from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.tool_capabilities import schema_selection_tools_for_context
from ouroboros.tools.registry import ToolContext, ToolRegistry


@pytest.mark.parametrize("mode", ["max", "low", "nano"])
@pytest.mark.parametrize("profile", ["local_readonly_subagent", "acting_subagent"])
def test_nano_child_can_select_only_its_existing_capabilities(tmp_path, monkeypatch, mode, profile):
    repo, data, workspace = (tmp_path / name for name in ("repo", "data", "workspace"))
    for path in (repo, data, workspace):
        path.mkdir()
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    ctx = ToolContext(repo_dir=repo, drive_root=data, workspace_root=workspace,
                      workspace_mode="external", task_id="nano-schema", active_context_mode=mode,
                      task_constraint=TaskConstraint(mode=profile, surface="external_workspace", write_root=str(workspace)))
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry.set_context(ctx)
    names = set(registry.available_tools())
    assert ("enable_tools" in names) is (mode == "nano")
    assert ("compact_context" in names) is (mode == "nano")
    assert ("run_command" in names) is (profile == "acting_subagent")
    assert "commit_reviewed" not in names
    if mode == "nano":
        result = registry.execute_result("enable_tools", {"tools": "read_file,commit_reviewed"})
        assert result.status == "ok", result.text
        assert registry.get_schema_by_name("commit_reviewed") is None
        assert set(registry.available_tools()) == names
    ctx.task_contract = {"disabled_tools": ["enable_tools"]}
    assert registry.get_schema_by_name("enable_tools") is None
    assert registry.execute_result("enable_tools", {"tools": "read_file"}).status == "blocked"


def test_cold_schema_selection_uses_existing_mode_reader(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr("ouroboros.config.get_context_mode", lambda: "nano")
    assert schema_selection_tools_for_context(SimpleNamespace(active_context_mode="")) == {
        "list_available_tools", "enable_tools", "compact_context",
    }
