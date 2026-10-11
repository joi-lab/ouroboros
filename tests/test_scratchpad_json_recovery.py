"""Scratchpad JSON recovery preserves source memory, typed outcomes and paid usage."""
import copy
import json
from types import SimpleNamespace

import pytest

from ouroboros import consolidator as c
from ouroboros.post_task_synthesis import _run_scratchpad_consolidation
from ouroboros.utils import extract_trailing_json_object
from tests import test_consolidator_context_fit as fit_helpers
from tests.test_consolidator_context_fit import _LLM
from tests.test_memory_maintenance_visibility import _events, _scratchpad


fit = fit_helpers.fit
FINAL = {"knowledge_entries": [], "compressed_block": "Actual consolidated memory."}
JSON = json.dumps(FINAL)
EXAMPLE = json.dumps({"knowledge_entries": [], "compressed_block": "Quoted prompt example."})
USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
         "cost": 0.02, "provider": "openrouter", "resolved_model": "light/fixture"}
CASES = [
    ("pure", JSON, "replaced", ""),
    ("fenced", "```json\n" + JSON + "\n```", "replaced", ""),
    ("prose_tail", "Here is the consolidated memory.\n" + JSON, "replaced", ""),
    ("example_then_tail", "Example: " + EXAMPLE + "\nActual result:\n" + JSON,
     "replaced", ""),
    ("prose_fenced_tail", "Here is the result.\n```json\n" + JSON + "\n```",
     "replaced", ""),
    ("empty", "", "call_failed", "empty_summary"),
    ("invalid", "No JSON result.", "failed", "scratchpad_consolidation_failed"),
    ("malformed", '{"compressed_block":', "failed", "scratchpad_consolidation_failed"),
    ("array", "[]", "failed", "scratchpad_consolidation_failed"),
    ("wrong_block_type", '{"knowledge_entries":[],"compressed_block":7}',
     "failed", "scratchpad_consolidation_failed"),
    ("missing_block", '{"knowledge_entries":[]}', "empty_block", ""),
    ("empty_block", '{"knowledge_entries":[],"compressed_block":"   "}', "empty_block", ""),
    ("prose_after_object", "Example: " + JSON + "\nThis is only an example.",
     "failed", "scratchpad_consolidation_failed"),
    ("duplicate_tail", 'Result:\n{"compressed_block":"first","compressed_block":"second"}',
     "failed", "scratchpad_consolidation_failed"),
]


def _memory_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "memory").rglob("*") if p.is_file()}


def _run_case(tmp_path, label, raw, *, stage=False, monkeypatch=None):
    memory = _scratchpad(tmp_path)
    memory.load_identity()  # Normal startup precedes the write-preservation snapshot.
    knowledge = tmp_path / "memory" / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "untouched.md").write_text("# Existing note\nKeep unchanged.\n", encoding="utf-8")
    before = copy.deepcopy(memory.load_scratchpad_blocks())
    files_before = _memory_bytes(tmp_path)
    llm = _LLM(effect=lambda client, prompt: ({"content": raw}, dict(USAGE)))
    accounted = []
    if stage:
        import supervisor.state as state
        monkeypatch.setattr(state, "update_budget_from_usage", lambda usage: accounted.append(copy.deepcopy(usage)))
        env = SimpleNamespace(drive_root=tmp_path, drive_path=lambda path: tmp_path / path)
        outcome = _run_scratchpad_consolidation(env, memory, llm)
        assert len(accounted) == 1
        usage = accounted[0]
    else:
        usage = c.consolidate_scratchpad(memory, knowledge, llm)
        outcome = None
    after = memory.load_scratchpad_blocks()
    files_after = _memory_bytes(tmp_path)
    [event] = _events(tmp_path, "scratchpad_consolidation")
    journal = [json.loads(row) for row in memory.journal_path().read_text(encoding="utf-8").splitlines()]
    consolidated = [row for row in journal if row.get("type") == "blocks_consolidated"]
    assert len(llm.calls) == 1, "formatting never causes another synthetic model call"
    assert usage["prompt_tokens"] == 10 and usage["completion_tokens"] == 5 and usage["cost"] == 0.02
    assert event["accounted_upper_bound_usd"] == 0.02
    assert event["knowledge_writes"] == {"ok": 0, "failed": 0}
    assert (knowledge / "untouched.md").read_bytes() == files_before["memory/knowledge/untouched.md"]
    if event["outcome"] == "replaced":
        assert after[0]["content"] == FINAL["compressed_block"]
        assert after[1:] == before[2:]
        assert len(consolidated) == 1 and consolidated[0]["source_blocks"] == before[:2]
        assert after[0]["metadata"]["source_ref"]["entry_id"] == event["source_entry_id"]
    else:
        assert after == before and files_after == files_before
        assert not consolidated and event["source_entry_id"] == ""
    parsed = extract_trailing_json_object(raw)[1]
    return {"usage": usage, "event": event, "stage_outcome": outcome,
            "accounting_calls": len(accounted), "canonical_extractor_result": parsed}


@pytest.mark.parametrize("label,raw,outcome,error", CASES, ids=[row[0] for row in CASES])
def test_scratchpad_reply_preserves_memory_and_usage(tmp_path, fit, label, raw, outcome, error):
    record = _run_case(tmp_path, label, raw)
    assert record["event"]["outcome"] == outcome
    errors = record["usage"].get("_consolidation_errors") or []
    assert [row["kind"] for row in errors] == ([error] if error else [])
    assert record["event"]["last_error_kind"] == (error or None)
    if label in {"pure", "fenced", "prose_tail", "example_then_tail", "prose_fenced_tail"}:
        assert record["canonical_extractor_result"] == FINAL
    elif label in {"prose_after_object", "duplicate_tail"}:
        assert record["canonical_extractor_result"] is None


@pytest.mark.parametrize("label,raw,expected", [
    ("stage_pure", JSON, ""),
    ("stage_prose", "Result:\n" + JSON, ""),
    ("stage_empty", "", "empty_summary"),
    ("stage_invalid", "No JSON result.", "scratchpad_consolidation_failed"),
])
def test_actual_post_task_accounting(tmp_path, fit, monkeypatch, label, raw, expected):
    record = _run_case(tmp_path, label, raw, stage=True, monkeypatch=monkeypatch)
    assert record["stage_outcome"] == expected and record["accounting_calls"] == 1
