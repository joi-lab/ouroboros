"""Model-source onboarding uses the same atomic transaction fixtures as API setup."""

import json

import pytest

from ouroboros.settings_setup_contract import ONBOARDING_COMPLETED_KEY
from tests.test_onboarding_complete_endpoint import (
    LIVE_SNAPSHOT, WIZARD_PAYLOAD, _profile, _profile_account,
    onboarding as onboarding,  # explicit fixture re-export, not another settings writer
)


def test_invalid_manual_reviewer_draft_refuses_the_whole_onboarding_write(onboarding):
    response = onboarding.client.post("/api/onboarding/complete", json={
        **WIZARD_PAYLOAD, "OUROBOROS_REVIEWER_SLOTS": '{"triad":[],"scope":[]}',
    })
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_reviewer_slots"
    assert not onboarding.settings_path.exists()


def test_api_only_preview_includes_effective_reviewer_assignments(onboarding):
    response = onboarding.client.post("/api/onboarding/subagents/preview", json=WIZARD_PAYLOAD)
    assert response.status_code == 200, response.text
    from ouroboros.reviewer_slot_config import parse_reviewer_slots
    slots = parse_reviewer_slots(response.json()["reviewer_slots"])
    assert slots.triad and slots.scope and slots.deep_review
    assert not onboarding.settings_path.exists()


def test_codex_only_preview_and_finish_share_models_agents_and_atomic_settings(onboarding):
    """One managed account supplies real model defaults and agent review, without API keys."""
    model = "claudexor::codex=provider-default"
    onboarding.calls["snapshot_payload"] = {
        **LIVE_SNAPSHOT,
        "harnesses": [LIVE_SNAPSHOT["harnesses"][1]],
        "profiles": {"harnessAccounts": [_profile_account("codex", "shared")],
                     "profiles": [_profile("codex", "shared")]},
        "model_catalog": [{"value": model, "is_default": True, "input_modalities": ["text", "image"],
                           "credential_profile_id": "shared", "max_context_window": 872000}],
    }
    draft = {"subscriptionsConnected": True, "OUROBOROS_MODEL": "", "TOTAL_BUDGET": 25.0}
    preview = onboarding.client.post("/api/onboarding/subagents/preview", json=draft)
    assert preview.status_code == 200, preview.text
    proposed = preview.json()
    assert proposed["model_settings"]["OUROBOROS_MODEL"] == model
    assert json.loads(proposed["reviewer_slots"])["deep_review"]
    assert not onboarding.settings_path.exists()
    completed = onboarding.client.post("/api/onboarding/complete", json={
        **draft, **proposed["model_settings"],
        "OUROBOROS_REVIEWER_SLOTS": proposed["reviewer_slots"],
        "OUROBOROS_MODEL_ACCOUNTS": {"main": "shared", "light": ""},
        "OUROBOROS_MODEL_CONTEXT_WINDOWS": {"main": 1000000},
    })
    assert completed.status_code == 200, completed.text
    saved = onboarding.saved()
    assert saved["OUROBOROS_MODEL"] == saved["OUROBOROS_MODEL_LIGHT"] == model
    assert not saved["OPENAI_API_KEY"] and not saved["OPENROUTER_API_KEY"]
    assert json.loads(saved["OUROBOROS_MODEL_ACCOUNTS"])["main"] == "shared"
    assert json.loads(saved["OUROBOROS_MODEL_CONTEXT_WINDOWS"])["main"] == 1000000
    assert saved[ONBOARDING_COMPLETED_KEY]
    assert onboarding.calls["supervisor"] == 1
    assert json.loads(saved["OUROBOROS_REVIEWER_SLOTS"]) == json.loads(proposed["reviewer_slots"])


def test_codex_only_finish_computes_omitted_quick_path_models(onboarding):
    onboarding.calls["snapshot_payload"] = {
        **LIVE_SNAPSHOT,
        "model_catalog": [{"value": "claudexor::codex=default", "is_default": True,
                           "input_modalities": ["text", "image"]}],
    }
    response = onboarding.client.post("/api/onboarding/complete", json={"subscriptionsConnected": True})
    assert response.status_code == 200, response.text
    assert onboarding.saved()["OUROBOROS_MODEL"] == "claudexor::codex=default"


def test_finish_preserves_visible_empty_inheritance_and_shipped_value(onboarding):
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS

    onboarding.calls["snapshot_payload"] = {
        **LIVE_SNAPSHOT,
        "model_catalog": [{"value": "claudexor::codex=default", "is_default": True}],
    }
    light = SETTINGS_DEFAULTS["OUROBOROS_MODEL_LIGHT"]
    response = onboarding.client.post("/api/onboarding/complete", json={
        "subscriptionsConnected": True, "OUROBOROS_MODEL": "",
        "OUROBOROS_MODEL_LIGHT": light, "OUROBOROS_MODEL_VISION": "",
        "OUROBOROS_MODEL_FALLBACKS": "",
    })
    assert response.status_code == 200, response.text
    saved = onboarding.saved()
    assert saved["OUROBOROS_MODEL_LIGHT"] == light
    assert saved["OUROBOROS_MODEL_VISION"] == ""
    assert saved["OUROBOROS_MODEL_FALLBACKS"] == ""


@pytest.mark.serial
def test_explicit_preset_recovery_previews_main_reviews_then_saves_visible_draft(onboarding):
    """A broken CLI inventory does not replace a working Main with keyless API defaults."""
    from ouroboros.subscription_install_presets import PRESET_MARKER_KEY

    model = "claudexor::codex=chosen-model"
    onboarding.calls["snapshot_payload"] = RuntimeError("model inventory unavailable")
    draft = {
        "subscriptionsConnected": True, "skipSubscriptionPresets": True,
        "OUROBOROS_MODEL": model, "OUROBOROS_MODEL_LIGHT": "",
        "OUROBOROS_MODEL_FALLBACKS": "", "TOTAL_BUDGET": 25,
        "OUROBOROS_MODEL_ACCOUNTS": {"main": "chosen-account"},
        "OUROBOROS_MODEL_PROCESSING_PREFERENCES": {"main": "standard"},
    }
    preview = onboarding.client.post("/api/onboarding/subagents/preview", json=draft)
    assert preview.status_code == 200, preview.text
    assert onboarding.calls["snapshot"] == 0
    assert not onboarding.settings_path.exists()
    proposed = preview.json()
    slots = json.loads(proposed["reviewer_slots"])
    rows = [*slots["triad"], *slots["scope"], slots["advisory"], slots["deep_review"]]
    assert len(slots["triad"]) == 3 and len(slots["scope"]) == 1
    assert all(row["route"] == {
        "kind": "api_chat", "target_id": model, "profile_id": "chosen-account",
    } for row in rows)
    assert all(row["processing_preference"] == "standard" for row in rows)

    # A later owner edit to the displayed proposal is saved as shown, not recomputed.
    slots["triad"][0]["route"]["target_id"] = "claudexor::codex=owner-refinement"
    visible_slots = json.dumps(slots)
    completed = onboarding.client.post("/api/onboarding/complete", json={
        **draft, "OUROBOROS_SUBAGENTS": proposed["available_subagents"],
        "OUROBOROS_REVIEWER_SLOTS": visible_slots,
    })
    assert completed.status_code == 200, completed.text
    assert onboarding.calls["snapshot"] == 0
    saved = onboarding.saved()
    assert json.loads(saved["OUROBOROS_REVIEWER_SLOTS"]) == slots
    assert not saved.get(PRESET_MARKER_KEY)
    assert saved[ONBOARDING_COMPLETED_KEY]
    assert onboarding.calls["supervisor"] == 1


def test_main_review_recovery_replaces_references_without_changing_actor_draft():
    from ouroboros.subscription_install_presets import preview_main_reviewer_slots

    settings = {
        "OUROBOROS_MODEL": "claudexor::source=main-model",
        "OUROBOROS_SUBAGENTS": {"enabled": True, "items": [{
            "subagent_id": "original-actor", "recommended_use": "Original task actor",
            "route": {"kind": "api_model", "target_id": "claudexor::source=old", "credential_profile_id": ""},
            "effort": "xhigh",
        }]},
        "OUROBOROS_REVIEWER_SLOTS": json.dumps({
            "triad": [{"slot_id": "my-review", "subagent_id": "original-actor", "effort": "high"}],
            "scope": [{"slot_id": "my-scope", "subagent_id": "original-actor"}],
            "advisory": {"enabled": False, "subagent_id": "original-actor"},
        }),
    }
    before = dict(settings)
    raw, roster = preview_main_reviewer_slots(settings)
    slots = json.loads(raw)
    assert settings == before
    from ouroboros.reviewer_slot_config import parse_reviewer_slots, roster_env_override
    with roster_env_override(roster, environ=settings):
        resolved = parse_reviewer_slots(raw)
    assert resolved.triad[0].retrieves
    assert resolved.scope[0].effort == "xhigh"
    assert json.loads(roster)["items"][0] == settings["OUROBOROS_SUBAGENTS"]["items"][0]
    assert slots["triad"][0]["slot_id"] == "my-review"
    assert slots["triad"][0]["effort"] == "high"
    assert slots["advisory"]["enabled"] is False
    for row in [*resolved.triad, *resolved.scope, resolved.advisory, resolved.deep_review]:
        assert row.target_id == settings["OUROBOROS_MODEL"]


@pytest.mark.serial
@pytest.mark.parametrize("model", ["", "openai/no-key-model"])
def test_main_review_recovery_does_not_invent_main_access(onboarding, model):
    response = onboarding.client.post("/api/onboarding/subagents/preview", json={
        "subscriptionsConnected": True, "skipSubscriptionPresets": True,
        "OUROBOROS_MODEL": model,
    })
    assert response.status_code == 400, response.text
    assert not onboarding.settings_path.exists()
    assert onboarding.calls["snapshot"] == 0


def test_main_review_recovery_materializes_optional_inherited_reviewers():
    from ouroboros.subscription_install_presets import preview_main_reviewer_slots

    model = "claudexor::codex=chosen-main"
    raw, _roster = preview_main_reviewer_slots({
        "OUROBOROS_MODEL": model,
        "OUROBOROS_SUBAGENTS": {"enabled": True, "items": []},
        "OUROBOROS_REVIEWER_SLOTS": json.dumps({
            "triad": [{"slot_id": "review", "route": {"kind": "api_chat", "target_id": "old"}}],
            "scope": [{"slot_id": "scope", "route": {"kind": "api_chat", "target_id": "old"}}],
            "advisory": None, "deep_review": None,
        }),
    })
    slots = json.loads(raw)
    assert slots["advisory"]["enabled"] is True
    assert slots["advisory"]["route"]["target_id"] == slots["deep_review"]["route"]["target_id"] == model


@pytest.mark.serial
def test_recovery_endpoint_preserves_visible_panel_and_inherited_inspection(onboarding):
    from ouroboros.reviewer_slot_config import parse_reviewer_slots, roster_env_override
    from ouroboros.subscription_install_presets import compile_install_preset
    from tests.test_subscription_install_presets import _discoveries

    preset = compile_install_preset(_discoveries("claude", "codex", "cursor", "agy"), settings={
        "OPENAI_API_KEY": "fixture-no-network", "OUROBOROS_MODEL": "openai::main",
        "OUROBOROS_MODEL_LIGHT": "openai::scout",
    })
    assert preset.ok
    roster = json.loads(preset.available_subagents)
    panel = json.loads(preset.reviewer_slots)
    panel["triad"][0]["effort"] = "xhigh"
    panel["advisory"]["enabled"] = False
    settings = {
        "subscriptionsConnected": True, "skipSubscriptionPresets": True,
        "OUROBOROS_MODEL": "claudexor::codex=main",
        "OUROBOROS_MODEL_ACCOUNTS": {"main": "account"},
        "OUROBOROS_MODEL_PROCESSING_PREFERENCES": {"main": "standard"},
        "OUROBOROS_SUBAGENTS": roster, "OUROBOROS_REVIEWER_SLOTS": json.dumps(panel),
    }
    with roster_env_override(json.dumps(roster), environ=settings):
        original = parse_reviewer_slots(json.dumps(panel))
    response = onboarding.client.post("/api/onboarding/subagents/preview", json=settings)
    assert response.status_code == 200, response.text
    result = response.json()
    # #1334: retrieving triad rows become direct Main rows saved as native delivery;
    # no roster actor is minted for them.
    assert result["available_subagents"]["items"] == roster["items"]
    with roster_env_override(json.dumps(result["available_subagents"]), environ=settings):
        resolved = parse_reviewer_slots(result["reviewer_slots"])
    before = [*original.triad, *original.scope, original.advisory, original.deep_review]
    after = [*resolved.triad, *resolved.scope, resolved.advisory, resolved.deep_review]
    assert [(r.slot_id, r.effort) for r in after] == [(r.slot_id, r.effort) for r in before]
    assert resolved.advisory.enabled is False
    assert all(r.retrieves and r.native_retrieval and not r.subagent_id for r in resolved.triad)
    assert all(r.target_id == settings["OUROBOROS_MODEL"] and r.profile_id == "account"
               and r.processing_preference == "standard" for r in after)
    assert not onboarding.settings_path.exists() and onboarding.calls["snapshot"] == 0


@pytest.mark.serial
def test_recovery_endpoint_keeps_authored_counts_ids_effort_and_disabled_advisory(onboarding):
    panel = {"triad": [{"slot_id": "own-triad", "effort": "xhigh",
                        "route": {"kind": "api_chat", "target_id": "claudexor::codex=old"}}],
             "scope": [{"slot_id": "own-scope", "effort": "high",
                        "route": {"kind": "api_chat", "target_id": "claudexor::codex=old"}}],
             "advisory": {"enabled": False, "effort": "high"}}
    response = onboarding.client.post("/api/onboarding/subagents/preview", json={
        "subscriptionsConnected": True, "skipSubscriptionPresets": True,
        "OUROBOROS_MODEL": "claudexor::codex=main", "OUROBOROS_REVIEWER_SLOTS": json.dumps(panel),
    })
    assert response.status_code == 200, response.text
    result = json.loads(response.json()["reviewer_slots"])
    assert [(r["slot_id"], r["effort"]) for r in result["triad"]] == [("own-triad", "xhigh")]
    assert [(r["slot_id"], r["effort"]) for r in result["scope"]] == [("own-scope", "high")]
    assert result["advisory"]["enabled"] is False and result["advisory"]["effort"] == "high"


def test_main_review_recovery_never_proposes_an_owner_disabled_roster_row():
    """A switched-off row is not a reviewer seat, and the recovery needs no
    roster actor at all (#1334): a retrieving triad row becomes a direct Main
    row saved with ``delivery: native``. The owner's disabled row is preserved
    untouched and nothing is minted."""
    from ouroboros.subscription_install_presets import preview_main_reviewer_slots

    main = "claudexor::source=main-model"
    disabled_match = {
        "subagent_id": "paused-main", "recommended_use": "Paused reviewer seat",
        "route": {"kind": "api_model", "target_id": main, "credential_profile_id": ""},
        "enabled": False,
    }
    raw, roster_raw = preview_main_reviewer_slots({
        "OUROBOROS_MODEL": main,
        "OUROBOROS_SUBAGENTS": {"enabled": True, "items": [disabled_match]},
        "OUROBOROS_REVIEWER_SLOTS": json.dumps({
            "triad": [{"slot_id": "t1", "route": {"kind": "agent_session", "target_id": "codex"}}],
            "scope": [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "old"}}],
        }),
    })
    roster = json.loads(roster_raw)
    row = json.loads(raw)["triad"][0]
    assert "subagent_id" not in row
    assert row["route"] == {"kind": "api_chat", "target_id": main} and row["delivery"] == "native"
    assert roster["items"] == [disabled_match]

    # The compiled draft survives the reviewer-slot parser it must be saved through.
    from ouroboros.reviewer_slot_config import parse_reviewer_slots, roster_env_override

    with roster_env_override(roster_raw, environ={}):
        triad = parse_reviewer_slots(raw).triad[0]
    assert triad.native_retrieval and triad.retrieves and not triad.subagent_id
