"""Fresh setup through preview, completion, runtime projection and deep admission."""
import asyncio
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest

from ouroboros import config
from ouroboros import reviewer_slot_config as rsc
from ouroboros.settings_defaults import OPENROUTER_DEFAULTS
from tests import test_onboarding_complete_endpoint as setup_fixture

onboarding = setup_fixture.onboarding

REAL_APPLY = config.apply_settings_to_env
KEY = "OUROBOROS_MODEL_DEEP_SELF_REVIEW"
MAIN = "openai-compatible::reachable-main"
PAYLOAD = {"OPENAI_COMPATIBLE_BASE_URL": "https://llm.example/v1",
           "OPENAI_COMPATIBLE_API_KEY": "test-only-key", "OUROBOROS_MODEL": MAIN,
           "OUROBOROS_RUNTIME_MODE": "advanced", "TOTAL_BUDGET": 25,
           "OUROBOROS_PER_TASK_COST_USD": 5}


@pytest.mark.parametrize("stored", [None, "openai/custom", OPENROUTER_DEFAULTS["deep_self_review"], ""])
def test_setup_preserves_raw_deep_choice_and_synthesizes_only_absence(onboarding, monkeypatch, stored):
    from ouroboros.deep_self_review import deep_review_route
    from ouroboros.gateway.settings import api_reviewer_slots
    from ouroboros.settings_integrity import task_settings_scope, task_settings_snapshot

    monkeypatch.delenv(KEY, raising=False)
    if stored is not None:
        onboarding.settings_path.write_text(json.dumps({KEY: stored}))
    preview = onboarding.client.post("/api/onboarding/subagents/preview", json=PAYLOAD)
    assert preview.status_code == 200, preview.text
    panel = json.loads(preview.json()["reviewer_slots"])
    expected = stored or MAIN
    assert panel["deep_review"]["route"]["target_id"] == expected
    completed = onboarding.client.post("/api/onboarding/complete", json=PAYLOAD)
    assert completed.status_code == 200, completed.text
    assert onboarding.calls["snapshot"] == 0  # no session daemon / provider dispatch
    loaded = config.load_settings()
    REAL_APPLY(loaded)
    assert config.get_deep_self_review_model() == expected
    assert rsc.deep_review_slot().target_id == expected
    endpoint = json.loads(asyncio.run(api_reviewer_slots(SimpleNamespace())).body)
    assert endpoint["deep_review"]["route"]["target_id"] == expected
    if stored is None:
        assert deep_review_route() == ("", MAIN)
    snapshot = task_settings_snapshot(loaded, dict(__import__("os").environ))
    # End this override before the global environment fixture restores the
    # pre-test state; pytest's later monkeypatch undo would reinsert REAL_APPLY's
    # test-only model value and poison the next availability test.
    with task_settings_scope(snapshot), mock.patch.dict(os.environ, {KEY: "different/new-task"}):
        assert config.get_deep_self_review_model() == expected


def test_runtime_raw_choice_unknown_panel_and_deep_inheritance(onboarding, monkeypatch):
    from ouroboros.deep_self_review import deep_review_route

    monkeypatch.delenv(KEY, raising=False)
    panel = {"triad": [{"slot_id": "t", "route": {"kind": "api_chat", "target_id": "saved/triad"}}],
             "scope": [{"slot_id": "s", "route": {"kind": "api_chat", "target_id": "saved/scope"}}]}
    raw = {**PAYLOAD, "OUROBOROS_REVIEWER_SLOTS": json.dumps(panel),
           "OUROBOROS_EFFORT_DEEP_SELF_REVIEW": "high", "OUROBOROS_PROCESSING_PREFERENCE": "economy",
           "OUROBOROS_MODEL_PROCESSING_PREFERENCES": json.dumps({"main": "fast", "deep_review": "standard"}),
           "OUROBOROS_MODEL_ACCOUNTS": json.dumps({"main": "main-only-pin"})}
    onboarding.settings_path.write_text(json.dumps(raw))
    loaded = config.load_settings()
    assert loaded[KEY] == ""  # unknown saved panel is never migrated
    REAL_APPLY(loaded)
    row = rsc.deep_review_slot()
    assert row.target_id == OPENROUTER_DEFAULTS["deep_self_review"]
    assert row.profile_id == "" and row.processing_preference == "standard"
    assert rsc.row_effort(row, "deep_self_review") == "high"
    panel["deep_review"] = {"route": {"kind": "api_chat", "target_id": MAIN}, "effort": "low",
                            "processing_preference": "economy"}
    with mock.patch.dict(os.environ, {rsc.REVIEWER_SLOTS_ENV: json.dumps(panel)}):
        row = rsc.deep_review_slot()
        assert deep_review_route(row) == ("", MAIN)
        assert row.processing_preference == "economy" and rsc.row_effort(row, "deep_self_review") == "low"


@pytest.mark.parametrize("other", [{"OPENROUTER_API_KEY": "test-key"}, {"OPENAI_API_KEY": "test-key"},
                                   {"ANTHROPIC_API_KEY": "test-key"}, {"USE_LOCAL_MAIN": True}, {}])
def test_defaults_outside_compatible_only_are_unchanged(other):
    from ouroboros.model_slots import get_deep_self_review_model
    from ouroboros.subscription_install_presets import preview_api_reviewer_slots

    settings = {"OUROBOROS_MODEL": "openai/gpt-5.6-sol", **other}
    assert get_deep_self_review_model(settings) == OPENROUTER_DEFAULTS["deep_self_review"]
    assert json.loads(preview_api_reviewer_slots(settings))["deep_review"]["route"]["target_id"] == OPENROUTER_DEFAULTS["deep_self_review"]
