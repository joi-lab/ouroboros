"""One-time, locked disk migration; later changes are independent."""
import json

import pytest


@pytest.fixture
def install(tmp_path, monkeypatch):
    from ouroboros import config
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for key in config.SETTINGS_DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    return config


@pytest.mark.parametrize("old", ["off", "light", "full"])
def test_existing_install_copies_once_persisted_and_audited(install, old):
    cfg = install
    cfg.SETTINGS_PATH.write_text(json.dumps({"OUROBOROS_SAFETY_MODE": old}))
    first = cfg.load_settings()
    assert first["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == old
    saved = json.loads(cfg.SETTINGS_PATH.read_text())
    assert saved["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == old
    saved["OUROBOROS_SAFETY_MODE"] = "full" if old != "full" else "off"
    cfg.SETTINGS_PATH.write_text(json.dumps(saved))
    assert cfg.load_settings()["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == old
    events = [json.loads(line) for line in (cfg.DATA_DIR / "logs/events.jsonl").read_text().splitlines()]
    assert len([e for e in events if e["type"] == "safety_settings_migration"]) == 1
    assert events[0]["actor"] == "migration"


def test_new_file_full_and_no_migration_and_malformed_preserved(install):
    cfg = install
    settings = cfg.load_settings()
    assert settings["OUROBOROS_SAFETY_MODE"] == settings["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == "full"
    assert not cfg.SETTINGS_PATH.exists()
    cfg.SETTINGS_PATH.write_text("{broken")
    cfg.load_settings()
    assert cfg.SETTINGS_PATH.read_text() == "{broken"


def test_explicit_new_key_is_not_overwritten_and_is_disk_authored(install, monkeypatch):
    cfg = install
    cfg.SETTINGS_PATH.write_text(json.dumps({"OUROBOROS_SAFETY_MODE": "off", "OUROBOROS_CONSCIOUSNESS_SAFETY_MODE": "full"}))
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_SAFETY_MODE", "off")
    assert cfg.load_settings()["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == "full"
    assert not (cfg.DATA_DIR / "logs/events.jsonl").exists()


def test_fresh_desktop_second_start_keeps_consciousness_full(install):
    cfg = install
    cfg.save_settings({"OUROBOROS_SAFETY_MODE": "light"}, onboarding_safety_default=True)
    for _ in range(2):
        settings = cfg.load_settings()
        assert settings["OUROBOROS_SAFETY_MODE"] == "light"
        assert settings["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == "full"
    assert not (cfg.DATA_DIR / "logs/events.jsonl").exists()


def test_fresh_web_generic_defaults_end_full_full(install):
    cfg = install
    cfg.save_settings({"OUROBOROS_SAFETY_MODE": "full", "OUROBOROS_CONSCIOUSNESS_SAFETY_MODE": "full"})
    settings = cfg.load_settings()
    assert settings["OUROBOROS_SAFETY_MODE"] == settings["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"] == "full"
