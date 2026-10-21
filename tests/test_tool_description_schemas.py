"""Representative calls retain the executable contracts of the edited descriptions.

Six of the seven remain; the seventh tool, task_acceptance_review, left with the review fold.
"""

import pytest

from ouroboros.tools.registry import ToolRegistry


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
