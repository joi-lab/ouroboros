"""Helpers keep the selected predecessor brief; only lineage roots read predecessor history.

A Continue root's provider copy materializes the named predecessor's selected
account (``historical_review_context``). A shared or declared helper receives
the pure brief and its exact reader instead; the context assembler must not
re-expand that brief into history. Root identity is the existing
``resolve_task_lineage(...).is_root_task`` decision, never a role string alone,
and a helper's own current review authority still reaches it.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.test_review_decision_notes import NOTE, first_plan, harness as _harness, notes_for
from tests.test_review_view_continue import successor
from tests.test_review_view_integration import _apply, _capture

harness = _harness


def _continued(harness):
    ctx = first_plan(harness)
    sub = harness.install({})
    _apply(ctx, _capture(ctx), note=NOTE, review_notes=notes_for(ctx))
    env, root = successor(ctx)
    return ctx, sub, env, root


def _helper(root, selection, task_id="helper-child"):
    from ouroboros.tools.control_scheduling import _build_child_subagent_contract

    parent = {**deepcopy(root["task_contract"]), "input_sources": selection}
    contract = _build_child_subagent_contract({"tid": task_id, "parent_contract": parent})
    return {"id": task_id, "type": "task", "text": "check the carried brief", "delegation_role": "subagent",
            "parent_task_id": root["id"], "root_task_id": root["id"], "input_sources": selection,
            "configured_subagent": {"route": {"kind": "api_model"}}, "task_contract": contract}


def _historical(runtime):
    nodes = [runtime.get("predecessor_authority"), (runtime.get("task_contract") or {}).get("predecessor_authority")]
    return [node["historical_review_context"] for node in nodes
            if isinstance(node, dict) and "historical_review_context" in node]


@pytest.mark.parametrize("selection", ["shared", "declared"])
def test_helpers_keep_the_brief_and_reader_while_their_own_review_authority_stays(harness, selection):
    from ouroboros.context import _task_authority_projection
    from ouroboros.task_results import load_task_result
    from ouroboros.tools.control import _get_task_result
    from ouroboros.tools.registry import ToolContext
    from tests.test_plan_task_tool import _call

    ctx, sub, env, root = _continued(harness)
    assert NOTE in str(_task_authority_projection(env, root)) and _historical(_task_authority_projection(env, root))
    child = _helper(root, selection)
    canonical = deepcopy(child)
    before = {tid: load_task_result(ctx.drive_root, tid) for tid in (ctx.task_id, root["id"])}
    runtime = _task_authority_projection(env, child)
    assert NOTE not in json.dumps(runtime, ensure_ascii=False) and not _historical(runtime)
    assert "previous_review_context" not in json.dumps(runtime)
    brief = child["task_contract"]["predecessor_authority"]
    assert runtime["task_contract"]["predecessor_authority"] == brief
    assert brief["source"]["task_id"] == ctx.task_id and brief["source"]["arguments"]["include_authority"]
    assert child == canonical and before == {tid: load_task_result(ctx.drive_root, tid) for tid in before}
    # The brief's reader resolves the full predecessor result for the helper itself.
    reader_ctx = ToolContext(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root, task_id=child["id"],
                             task_contract=child["task_contract"], task_metadata=dict(child))
    payload = json.loads(_get_task_result(reader_ctx, **brief["source"]["arguments"]))
    assert payload["status"] == "available" and payload["source"]["task_id"] == ctx.task_id
    assert payload["authority"]["task_contract"] and payload["authority"]["status"] == "failed"
    # A helper's OWN current plan authority is still projected for it.
    own = {**child, "id": "helper-own-plan"}
    _call(harness.make_ctx(task_id=own["id"]), goal="The helper's own plan.", affected_paths=[],
          plan="A free planning-organ revision of the helper's own work.")
    own_runtime = _task_authority_projection(env, own)
    assert own_runtime["plan_review_authority"]["source"]["arguments"]["task_id"] == own["id"]
    assert NOTE not in json.dumps(own_runtime, ensure_ascii=False) and not _historical(own_runtime)


@pytest.mark.parametrize("selection", ["shared", "declared"])
def test_assembled_helper_context_carries_the_brief_not_the_history(harness, tmp_path, selection):
    from ouroboros.context import build_llm_messages
    from tests.test_doc_context import _make_env_and_memory

    ctx, _, _, root = _continued(harness)
    env, memory = _make_env_and_memory(tmp_path / "assembly")
    child = {**_helper(root, selection), "budget_drive_root": str(ctx.drive_root)}
    messages, _ = build_llm_messages(env=env, memory=memory, task=child)
    rendered = json.dumps(messages, ensure_ascii=False)
    assert NOTE not in rendered and "historical_review_context" not in rendered
    assert child["task_contract"]["predecessor_authority"]["authority_sha256"] in rendered
    assert '"reader": "get_task_result"' in rendered or "get_task_result" in rendered
    root_messages, _ = build_llm_messages(env=env, memory=memory, task={**root, "budget_drive_root": str(ctx.drive_root)})
    root_rendered = json.dumps(root_messages, ensure_ascii=False)
    assert NOTE in root_rendered and "historical_review_context" in root_rendered


@pytest.mark.parametrize("shape,enriched", [
    ({"delegation_role": "root"}, True),
    ({"delegation_role": "", "root_task_id": ""}, True),
    ({"delegation_role": "root", "root_task_id": "first-attempt", "original_task_id": "first-attempt",
      "timeout_retry_from": "first-attempt"}, True),
    ({"delegation_role": "root", "root_task_id": "first-attempt", "original_task_id": "first-attempt",
      "timeout_retry_from": "another-attempt"}, False),
    ({"delegation_role": "", "parent_task_id": "some-parent"}, False),
    ({"delegation_role": "subagent", "root_task_id": None}, False),
], ids=["root", "blank-role-root", "agreeing-retry-root", "disagreeing-retry", "blank-role-child", "subagent"])
@pytest.mark.parametrize("edge", ["top-level", "contract-only"])
def test_only_lineage_roots_read_named_predecessor_history(harness, shape, enriched, edge):
    from ouroboros.context import _task_authority_projection

    ctx, _, env, root = _continued(harness)
    task = deepcopy(root)
    for key, value in shape.items():
        if value is None:
            task.pop(key, None)
        else:
            task[key] = value
    if edge == "contract-only":
        task.pop("predecessor_authority")
        assert task["task_contract"]["predecessor_authority"]["source"]["task_id"] == ctx.task_id
    runtime = _task_authority_projection(env, task)
    historical = _historical(runtime)
    assert bool(historical) == enriched and (NOTE in json.dumps(runtime, ensure_ascii=False)) == enriched
    if enriched:
        (history,) = historical
        assert history["task_id"] == ctx.task_id and history["authored_account"]["authorship"] == "predecessor_actor"
        assert not history["source_gaps"]
        args = history["source_reads"][0]["read"]["arguments"]
        assert Path(args["path"]).is_file() and args["root"] == "artifact_store"
