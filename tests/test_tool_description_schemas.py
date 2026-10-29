"""Representative calls retain the executable contracts of the edited descriptions.

Six of the seven remain; the seventh tool, task_acceptance_review, left with the review fold.
query_data, delegate_start, delegate_wait and wait_tasks (#1801, #502) changed prose only: their
parameter structures are pinned with the prose removed, and the conditions a caller needs before
a call stay in the tool text that every profile and the custom codec deliver.
"""

import hashlib
import json

import pytest

from ouroboros.tools.registry import ToolRegistry


@pytest.mark.parametrize("constraint", [
    {}, {"mode": "local_readonly_subagent"},
    {"mode": "acting_subagent", "surface": "self_worktree", "root_task_id": "projection"},
], ids=["full", "readonly", "acting"])
def test_schedule_readonly_choice_survives_custom_projection(tmp_path, constraint):
    from ouroboros.openai_chat_custom import project_function_tools_to_openai_custom

    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.task_constraint = constraint
    schema = next(tool for tool in registry.schemas() if tool["function"]["name"] == "schedule_subagent")
    function = schema["function"]
    # Normal/pruned function schemas carry parameter prose; the custom codec
    # strips it and truncates the tool prose to fit its description bound.
    assert "Select readonly for read-only session execution" in function["parameters"]["properties"]["access"]["description"]
    catalog = project_function_tools_to_openai_custom([schema])
    description = catalog.wire_tools()[0]["custom"]["description"]
    assert "access='readonly' selects read-only session execution" in description
    assert "evidence-only private checking copy" in description
    assert catalog.schema_binding("schedule_subagent").schema() == function["parameters"]


@pytest.mark.serial
@pytest.mark.parametrize("name,arguments,invalid_field,invalid_value", [
    ("schedule_subagent", {"subagent_id": "actor-1", "objective": "Inspect interfaces",
                          "expected_output": "Findings", "role": "", "write_surface": "read_only"},
     "write_surface", "invented_surface"),
    ("plan_task", {"goal": "Repair interfaces", "plan": "Inspect and verify", "affected_paths": [],
                   "done_when": [{"id": "c1", "claim": "Interfaces remain compatible", "support": "run the suite"}],
                   "ask": "council", "message": "First look."},
     "ask", "everyone"),
    ("delegate_start", {"prompt": "New evidence", "subagent_id": "actor-1",
                        "continue_from": "settled-run", "continue_carrier": "packet"},
     "continue_carrier", "restart"),
    ("verify_and_record", {"contract_kind": "explicit_command", "check": ["python", "check.py"],
                           "expected_match": "bytes_equal", "artifact_paths": ["actual", "expected"]},
     "expected_match", "approximate"),
    ("start_task", {"objective": "Inspect supplied project", "title": "Supplied project inspection",
                              "workspace": "none", "context_requires_self_body_docs": False},
     "context_requires_self_body_docs", "false"),
    ("preflight_review", {"commit_message": "Repair interfaces", "deterministic_only": True,
                          "source": "index"},
     "source", "head"),
    ("query_data", {"op": "sql", "db": "money", "query": "SELECT 1", "max_rows": 5}, "db", "ledger"),
    ("query_data", {"op": "jsonl", "kind": "events_log", "tail": 20}, "max_rows", 0),
    ("delegate_wait", {"run_id": "run-1", "checkpoint_after_sec": 60, "checkpoint_reason": "Inspect the diff"},
     "checkpoint_after_sec", "soon"),
    ("wait_tasks", {"task_ids": ["child-1"], "mode": "any_terminal", "timeout_sec": 60}, "mode", "first_terminal"),
])
def test_edited_description_schemas_keep_representative_call_shapes(
        tmp_path, name, arguments, invalid_field, invalid_value):
    """Exercise the exported parameter contracts, including continuation aliases."""
    from jsonschema import Draft7Validator

    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    parameters = registry.get_schema_by_name(name)["function"]["parameters"]
    Draft7Validator.check_schema(parameters)
    validator = Draft7Validator(parameters)
    assert not list(validator.iter_errors(arguments))
    assert list(validator.iter_errors({**arguments, invalid_field: invalid_value}))


_PROFILES = {
    "full": {}, "readonly": {"mode": "local_readonly_subagent"},
    "acting": {"mode": "acting_subagent", "surface": "self_worktree", "root_task_id": "projection"},
}
_SQL_ONLY = "db, query, max_rows, snapshot and timeout_sec are op=sql only"
# Parameters with every string description removed (required, defaults, enums and bounds kept),
# as emitted before these prose-only edits. A deliberate parameter change updates its pin.
# delegate_start: + attachment_paths (string array) and attachment_import (reuse|new), C505;
# without those two the structure is the earlier c4263da7232644b1 / 1aa885fc9a0c37ff.
# query_data: + state_file "context_snapshot" (the current skill snapshot reader, #2333); before it 6ddb5645e6aecb12.
_STRUCTURE = {
    "query_data": {"full": "f790f964764d75b7", "readonly": "f790f964764d75b7", "acting": "f790f964764d75b7"},
    "delegate_start": {"full": "5e25716838978044", "readonly": "1326e91ef42b3cea", "acting": "1326e91ef42b3cea"},
    "delegate_wait": {"full": "60ab2c0f421da812", "readonly": "60ab2c0f421da812", "acting": "60ab2c0f421da812"},
    "wait_tasks": {"full": "c806a86311a0a965", "readonly": "c806a86311a0a965", "acting": "c806a86311a0a965"},
}


def _without_prose(value):
    if isinstance(value, dict):
        return {key: _without_prose(item) for key, item in value.items()
                if not (key == "description" and isinstance(item, str))}
    return [_without_prose(item) for item in value] if isinstance(value, list) else value


@pytest.mark.parametrize("profile", sorted(_PROFILES))
def test_prose_edits_keep_structure_and_deliver_conditions_in_every_profile(tmp_path, profile):
    from ouroboros.openai_chat_custom import project_function_tools_to_openai_custom

    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.task_constraint = _PROFILES[profile]
    schemas = {tool["function"]["name"]: tool for tool in registry.schemas()}
    for name in _STRUCTURE:
        function = schemas[name]["function"]
        structure = json.dumps(_without_prose(function["parameters"]), sort_keys=True, separators=(",", ":"))
        assert hashlib.sha256(structure.encode()).hexdigest()[:16] == _STRUCTURE[name][profile], name
        catalog = project_function_tools_to_openai_custom([schemas[name]])
        custom = catalog.wire_tools()[0]["custom"]["description"]
        # The whole tool text survives the codec's bound; only parameter prose and defaults are dropped.
        assert custom.startswith(" ".join(function["description"].split()) + "\n"), name
        assert catalog.schema_binding(name).schema() == function["parameters"]
        if name == "query_data":
            assert _SQL_ONLY in function["description"] and _SQL_ONLY in custom
    query = schemas["query_data"]["function"]
    assert query["parameters"]["required"] == ["op"]
    assert {key: value.get("default") for key, value in query["parameters"]["properties"].items()
            if key in {"db", "query", "max_rows", "snapshot", "timeout_sec", "tail"}} == {
        "db": "current", "query": None, "max_rows": 100, "snapshot": False, "timeout_sec": 1, "tail": 100}
    for field in ("db", "max_rows"):
        assert query["parameters"]["properties"][field]["description"].startswith("op=sql only")
    assert "accepts only the default current, as if omitted" in query["parameters"]["properties"]["db"]["description"]
    assert "accepts only the default 100" in query["parameters"]["properties"]["max_rows"]["description"]
