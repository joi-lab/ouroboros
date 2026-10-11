"""#1304: delegate_start's root offers its ordinary value, and every selector refusal is typed.

Driven through the real ToolRegistry (argument preparation, configured-session selector
check, payload binder, handler) so the normalization point is the one the model reaches.
"""
from __future__ import annotations

import json

import pytest

from tests.test_delegated_skill_payload import _payload_ctx, _seed_skill


def _registry(tmp_path, monkeypatch, ctx):
    import ouroboros.claudexor_daemon as daemon
    import ouroboros.safety as safety
    from ouroboros.tools.registry import ToolRegistry

    def _no_gateway():
        raise AssertionError("a refused or recorded start must not reach the daemon")

    monkeypatch.setattr(safety, "check_safety", lambda *a, **k: (True, ""))
    monkeypatch.setattr(daemon, "ensure_owned_gateway", _no_gateway)
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry.set_context(ctx)
    return registry


def _start_blocked_reasons(ctx):
    from ouroboros import delegate_custody as custody

    path = custody.event_log_path(custody.custody_root(ctx))
    if not path.exists():
        return []
    return [json.loads(line)["reason"] for line in path.read_text(encoding="utf-8").splitlines()
            if '"delegate_run_start_blocked"' in line]


def test_schema_offers_the_ordinary_member_as_its_default():
    from ouroboros.tools import delegate

    entry = next(e for e in delegate.get_tools() if e.name == "delegate_start")
    root = entry.schema["parameters"]["properties"]["root"]
    assert root["enum"] == ["active_workspace", "skill_payload"] and root["default"] == "active_workspace"


def test_neutral_root_reaches_the_ordinary_start_with_every_other_choice_intact(tmp_path, monkeypatch):
    import ouroboros.subagent_runtime as runtime
    import ouroboros.tools.delegate as delegate
    from ouroboros.tools.tool_result import ToolResult

    ctx = _payload_ctx(tmp_path, monkeypatch)
    seen = []

    def _record(_ctx, prompt, max_seconds=None, retry_of=None, **kwargs):
        kwargs.update(max_seconds=max_seconds, retry_of=retry_of)
        seen.append((prompt, kwargs, runtime._EXACT_START_SELECTION.get()))
        return ToolResult(status="ok", code="OK", text=json.dumps({"status": "started"}))

    monkeypatch.setattr(delegate, "_delegate_start", _record)
    out = _registry(tmp_path, monkeypatch, ctx).execute("delegate_start", {
        "subagent_id": "payload-session", "prompt": "review the tree", "root": "active_workspace",
        "bucket": "", "skill_name": "", "access": "readonly", "directory_strategy": "copy",
        "scope_paths": ["src"], "retry_of": "", "continue_from": "", "max_seconds": 600})
    assert json.loads(out)["status"] == "started", out
    prompt, kwargs, selection = seen[0]
    assert prompt == "review the tree" and not kwargs.get("root") and kwargs.get("_resolved_binding") is None
    assert (kwargs["directory_strategy"], kwargs["scope_paths"], kwargs["max_seconds"]) == ("copy", ["src"], 600)
    snapshot = json.dumps(selection["snapshot"], default=lambda value: getattr(value, "__dict__", str(value)))
    assert "payload-session" in snapshot and "readonly" in snapshot
    assert _start_blocked_reasons(ctx) == []


@pytest.mark.parametrize("selector, reason, blocked", [
    ({"root": "active_workspace", "bucket": "external", "skill_name": ""}, "payload_selector_incomplete", []),
    ({"root": "skill_payload", "bucket": "", "skill_name": ""}, "payload_selector_incomplete", []),
    ({"root": "skill_payload", "bucket": "external", "skill_name": "missing"},
     "payload_selector_unresolved", ["payload_selector_unresolved"]),
])
def test_selector_refusals_are_typed_name_the_repair_and_start_nothing(tmp_path, monkeypatch, selector, reason, blocked):
    ctx = _payload_ctx(tmp_path, monkeypatch)
    _seed_skill(tmp_path / "data")
    out = _registry(tmp_path, monkeypatch, ctx).execute("delegate_start", {
        "subagent_id": "payload-session", "prompt": "x", **selector})
    parsed = json.loads(out)
    assert parsed["status"] == "refused" and parsed["reason"] == reason, parsed
    assert "TOOL_ERROR" not in out and "root" in parsed["detail"]
    assert _start_blocked_reasons(ctx) == blocked
    if blocked:
        assert parsed["definitely_unrun"] is True


def test_configured_session_reads_the_neutral_root_as_no_selector(tmp_path, monkeypatch):
    ctx = _payload_ctx(tmp_path, monkeypatch)
    ctx._configured_actor_bootstrap = {"selected_subagent_id": "payload-session"}
    registry = _registry(tmp_path, monkeypatch, ctx)
    neutral = json.loads(registry.execute("delegate_start", {
        "prompt": "coordinate", "root": "active_workspace", "bucket": "", "skill_name": ""}))
    assert neutral["reason"] == "configured_work_order_unavailable", neutral
    selected = json.loads(registry.execute("delegate_start", {"prompt": "coordinate", "root": "skill_payload"}))
    assert selected["reason"] == "configured_actor_resource_mismatch", selected
    assert _start_blocked_reasons(ctx) == ["configured_work_order_unavailable", "configured_actor_resource_mismatch"]
