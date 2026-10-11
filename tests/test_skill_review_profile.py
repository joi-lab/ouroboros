from __future__ import annotations

import hashlib
import json
import pathlib
from types import SimpleNamespace


# --- Aggregation semantics (official_hub downgrade) ---------------------------

def test_official_hub_profile_keeps_hard_trust_boundary_blockers():
    from ouroboros.skill_review_status import aggregate_skill_review_status
    findings = [{"item": "inject_chat_minimization", "verdict": "FAIL", "severity": "critical", "reason": "raw slash commands"}]
    assert aggregate_skill_review_status(findings, "extension") == "blockers"
    # Hard trust-boundary items still block under the official_hub profile.
    assert aggregate_skill_review_status(findings, "extension", review_profile="official_hub") == "blockers"


def test_official_hub_profile_downgrades_severity_driven_to_warnings():
    from ouroboros.skill_review_status import aggregate_skill_review_status
    findings = [{"item": "bug_hunting", "verdict": "FAIL", "severity": "critical", "reason": "runtime bug"}]
    # Without the profile, a severity-critical bug_hunting FAIL blocks.
    assert aggregate_skill_review_status(findings, "extension") == "blockers"
    # Hash-verified official hub payload downgrades hygiene/bug findings to warnings.
    assert aggregate_skill_review_status(findings, "extension", review_profile="official_hub") == "warnings"


# --- Profile eligibility (hash + full-file-set verification) ------------------

def _make_official_hub_skill(tmp_path, files):
    """Create a skill dir + sidecar; return (skill, catalog_files)."""
    skill_dir = tmp_path / "telegram-bridge"
    skill_dir.mkdir(parents=True)
    catalog_files = []
    for rel, content in files.items():
        p = skill_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        catalog_files.append({"path": rel, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    sidecar = {"source": "ouroboroshub", "slug": "telegram-bridge", "sanitized_name": "telegram-bridge", "files": catalog_files}
    (skill_dir / ".ouroboroshub.json").write_text(json.dumps(sidecar), encoding="utf-8")
    skill = SimpleNamespace(
        source="ouroboroshub",
        skill_dir=str(skill_dir),
        name="telegram-bridge",
        manifest=SimpleNamespace(entry="plugin.py", scripts=[]),
    )
    return skill, catalog_files


def _patch_catalog(monkeypatch, catalog_files):
    from ouroboros.marketplace import ouroboroshub
    monkeypatch.setattr(ouroboroshub, "info", lambda slug: SimpleNamespace(files=[dict(f) for f in catalog_files]))


def test_official_hub_profile_applies_when_payload_matches_catalog(tmp_path, monkeypatch):
    from ouroboros.skill_review import _official_hub_review_profile
    skill, catalog_files = _make_official_hub_skill(tmp_path, {"SKILL.md": b"---\nname: telegram-bridge\n---\n", "plugin.py": b"print('x')\n"})
    _patch_catalog(monkeypatch, catalog_files)
    assert _official_hub_review_profile(skill) == "official_hub"


def test_official_hub_profile_rejects_extra_runtime_file(tmp_path, monkeypatch):
    from ouroboros.skill_review import _official_hub_review_profile
    skill, catalog_files = _make_official_hub_skill(tmp_path, {"SKILL.md": b"x\n", "plugin.py": b"y\n"})
    _patch_catalog(monkeypatch, catalog_files)
    # A local runtime-reachable file not covered by the catalog drops the profile.
    (pathlib.Path(skill.skill_dir) / "evil.py").write_bytes(b"danger\n")
    assert _official_hub_review_profile(skill) == ""


def test_official_hub_profile_rejects_hash_mismatch(tmp_path, monkeypatch):
    from ouroboros.skill_review import _official_hub_review_profile
    skill, catalog_files = _make_official_hub_skill(tmp_path, {"SKILL.md": b"x\n", "plugin.py": b"y\n"})
    _patch_catalog(monkeypatch, catalog_files)
    # Tamper a payload file after the catalog hash was fixed.
    (pathlib.Path(skill.skill_dir) / "plugin.py").write_bytes(b"tampered\n")
    assert _official_hub_review_profile(skill) == ""


def test_official_hub_profile_rejects_sidecar_catalog_mismatch(tmp_path, monkeypatch):
    from ouroboros.skill_review import _official_hub_review_profile
    skill, catalog_files = _make_official_hub_skill(tmp_path, {"SKILL.md": b"x\n", "plugin.py": b"y\n"})
    # Live catalog advertises a different hash than the local sidecar.
    bogus = [dict(item) for item in catalog_files]
    bogus[0] = dict(bogus[0], sha256="0" * 64)
    _patch_catalog(monkeypatch, bogus)
    assert _official_hub_review_profile(skill) == ""


def test_load_review_state_drops_official_hub_when_sidecar_missing(tmp_path):
    # The official_hub downgrade must NOT be trusted on reload when the Hub
    # provenance sidecar is gone (payload moved/removed): the severity-driven
    # downgrade is dropped, so a critical bug_hunting FAIL goes back to blocking.
    import json as _json
    from ouroboros.skill_loader import load_review_state, skill_state_dir
    d = skill_state_dir(tmp_path, "demo"); d.mkdir(parents=True, exist_ok=True)
    (d / "review.json").write_text(_json.dumps({
        "status": "warnings",
        "content_hash": "h1",
        "review_profile": "official_hub",
        "findings": [{"item": "bug_hunting", "verdict": "FAIL", "severity": "critical", "model": "m"}],
    }))
    payload = tmp_path / "payload"; payload.mkdir()
    state = load_review_state(tmp_path, "demo", skill_type="script", skill_dir=payload)
    assert state.review_profile == "" and state.status == "blockers"
    (payload / ".ouroboroshub.json").write_text("{}", encoding="utf-8")
    state2 = load_review_state(tmp_path, "demo", skill_type="script", skill_dir=payload)
    assert state2.review_profile == "official_hub" and state2.status == "warnings"


def test_aggregate_returns_pending_for_deterministic_preflight_fail():
    from ouroboros.skill_review_status import aggregate_skill_review_status, STATUS_PENDING
    findings = [{"item": "skill_preflight", "verdict": "FAIL", "severity": "critical", "model": "deterministic_preflight"}]
    assert aggregate_skill_review_status(findings, "script") == STATUS_PENDING
    # Even hash-verified official_hub keeps a deterministic gate failure fail-closed.
    assert aggregate_skill_review_status(findings, "script", review_profile="official_hub") == STATUS_PENDING


def test_load_review_state_preserves_pending_for_preflight_fail(tmp_path):
    # Regression: a persisted preflight failure must reload as PENDING, not be
    # recomputed to advisory-overridable BLOCKERS by aggregate_skill_review_status.
    import json as _json
    from ouroboros.skill_loader import load_review_state, skill_state_dir
    from ouroboros.skill_review_status import STATUS_PENDING
    d = skill_state_dir(tmp_path, "demo"); d.mkdir(parents=True, exist_ok=True)
    (d / "review.json").write_text(_json.dumps({
        "status": "pending",
        "content_hash": "h1",
        "findings": [{"item": "skill_preflight", "verdict": "FAIL", "severity": "critical", "model": "deterministic_preflight"}],
    }))
    state = load_review_state(tmp_path, "demo", skill_type="script")
    assert state.status == STATUS_PENDING


def test_deterministic_preflight_persists_pending_and_is_non_executable(tmp_path, monkeypatch):
    # A deterministic preflight FAILURE is a structural fact, not an LLM verdict.
    # It must persist as PENDING so it is non-executable under EVERY enforcement
    # mode (advisory included) and in every readiness/execution caller, without
    # needing per-caller findings threading.
    import ouroboros.skill_review as sr
    import ouroboros.tools.skill_preflight as pf
    from ouroboros.skill_review_status import skill_review_gate
    monkeypatch.setattr(
        pf,
        "_handle_skill_preflight",
        lambda ctx, skill=None, _resolved_binding=None: json.dumps(
            {"ok": False, "error": "bad manifest schema"}
        ),
    )
    granted = []
    monkeypatch.setattr(sr, "auto_grant_if_enabled", lambda drive_root, skill: granted.append(skill))
    monkeypatch.setattr(sr, "save_review_state", lambda *a, **k: None)
    monkeypatch.setattr(sr, "_append_skill_review_history", lambda *a, **k: None)
    skill = SimpleNamespace(
        name="demo",
        review=None,
        manifest=SimpleNamespace(env_from_settings=["OPENROUTER_API_KEY"], permissions=[], subscribe_events=[]),
    )
    outcome = sr._run_deterministic_preflight(SimpleNamespace(), tmp_path, skill, "hash123", persist=True)
    assert outcome is not None and outcome.status == sr.STATUS_PENDING
    # Non-executable regardless of enforcement (no per-caller findings needed).
    assert skill_review_gate(outcome.status, enforcement="advisory")["executable_review"] is False
    assert skill_review_gate(outcome.status, enforcement="blocking")["executable_review"] is False
    assert granted == []  # auto_grant must NOT be called on a preflight failure
    assert outcome.requested_keys == ["OPENROUTER_API_KEY"]  # transparency preserved
