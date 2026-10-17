"""A live no-dispatch size refusal is distinct from the same retained pending record."""
from types import SimpleNamespace

import pytest

from ouroboros.tools.registry import ToolEntry, ToolRegistry


@pytest.mark.parametrize("historical", [False, True])
def test_oversized_skill_result_reaches_the_tool_consumer_without_retyping_history(tmp_path, monkeypatch, historical):
    from ouroboros import skill_review, skill_review_packs
    from ouroboros.skill_review_runner import _outcome_payload
    from ouroboros.tools import skill_exec
    from tests._skill_review_shared import _build_skill, _make_ctx
    from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    skills = _build_skill(tmp_path)
    (skills / "weather/scripts/table.py").write_text("ROWS = " + repr("x" * 4000) + "\n")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills))
    monkeypatch.setattr(skill_review_packs, "_skill_pack_token_budget", lambda: 300)
    set_review_pool(monkeypatch, pool_roster(pool_seat("review-1", "openai/gpt-5.5")))
    monkeypatch.setattr("ouroboros.tools.review._handle_multi_model_review",
                        lambda *a, **k: pytest.fail("oversized review dispatched"))
    outcome = skill_review.review_skill(_make_ctx(tmp_path), "weather", persist=False)
    assert outcome.status == "pending" and "budget" in outcome.error
    if historical:
        outcome = skill_review.SkillReviewOutcome(skill_name="weather", status="pending",
                                                  content_hash=outcome.content_hash, error=outcome.error)
    payload = _outcome_payload(outcome, deps_status="not_run", deps_error="", extension_action=None,
                               extension_reason=None, job_data={"job_id": "fixture-job"})
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path / "consumer-data")
    binding = SimpleNamespace(state_drive_root=registry._ctx.drive_root)
    monkeypatch.setattr(skill_exec, "_skill_tool_preflight", lambda *a: "")
    monkeypatch.setattr("ouroboros.skill_review_runner.run_skill_review_lifecycle_blocking", lambda *a, **k: payload)
    monkeypatch.setattr("ouroboros.skill_review._count_attempts_for_content", lambda *a: 1)
    monkeypatch.setattr("ouroboros.skill_review._load_accepted_rebuttals", lambda *a: [])
    registry.register(ToolEntry("skill_consumer", {
        "name": "skill_consumer", "description": "test consumer",
        "parameters": {"type": "object", "properties": {}}},
        lambda ctx: skill_exec._handle_review_skill(ctx, skill="weather", _resolved_binding=binding)))
    result = registry.execute_result("skill_consumer", {})
    assert "fixture-job" in result.text
    assert result.code == ("OK" if historical else "LEGACY_BLOCKED")
    assert result.status == ("ok" if historical else "blocked")


@pytest.mark.parametrize("arm,expected", [("over_budget", "LEGACY_BLOCKED"), ("binary", ""), ("unreadable", "")])
def test_payload_refusal_marker_preserves_each_arm(monkeypatch, arm, expected):
    """Relocating refusals into one return must not reclassify binary/unreadable inputs."""
    from ouroboros import skill_review

    raised = {
        "over_budget": skill_review._SkillFileOverBudget("scripts/table.py", 5000, 300),
        "binary": skill_review._SkillBinaryPayload("lib/native.so", 1024, "ELF"),
        "unreadable": skill_review._SkillFileUnreadable("scripts/x.py", PermissionError("denied")),
    }[arm]

    def refuse(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(skill_review, "_build_skill_file_packs", refuse)
    skill = SimpleNamespace(name="fixture", skill_dir="/nonexistent",
                            manifest=SimpleNamespace(entry="", scripts=[]))
    packs, same_skill, refusal = skill_review._skill_payload_packs(
        skill, None, "content-hash", split=True, packet_seats=["review-1"])
    assert packs == [] and same_skill is skill
    assert refusal.status == "pending" and refusal.content_hash == "content-hash" and refusal.error
    assert refusal.dispatch_refusal_code == expected
