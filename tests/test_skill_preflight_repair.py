"""#335: the typed preflight_failed fact behind the skill card Repair action.

A deterministic preflight FAIL persists as review_status=pending; the UI used
to offer Review/Re-review, which deterministically fails the same way. The
gate now carries ``preflight_failed`` when the caller has the persisted
findings — never fabricated when it cannot know — plus the D11 companion
``preflight_failed_stale`` for a recorded failure whose findings no longer
describe the current payload bytes (Re-review stays primary; Repair is
additionally offered based on the last recorded preflight).
"""

from __future__ import annotations

import json
import types

from ouroboros.skill_review_status import preflight_failed, skill_review_gate

_PREFLIGHT_FAIL = {
    "item": "skill_preflight",
    "verdict": "FAIL",
    "severity": "critical",
    "reason": '{"manifest": [{"item": "manifest_entry_exists", "ok": false}], "ok": false}',
    "model": "deterministic_preflight",
}


def test_preflight_predicate_matches_the_persisted_finding_shape():
    assert preflight_failed([_PREFLIGHT_FAIL]) is True
    assert preflight_failed([{"item": "skill_preflight", "verdict": "PASS"}]) is False
    assert preflight_failed([{"item": "other", "verdict": "FAIL"}]) is False
    # The model marker alone is enough (mirror of the pending aggregation).
    assert preflight_failed([{"verdict": "FAIL", "model": "deterministic_preflight"}]) is True
    assert preflight_failed([]) is False
    assert preflight_failed(None) is False
    assert preflight_failed(["not-a-dict"]) is False


def test_gate_carries_the_fact_only_when_findings_are_known():
    with_fact = skill_review_gate("pending", findings=[_PREFLIGHT_FAIL])
    assert with_fact["preflight_failed"] is True
    assert with_fact["executable_review"] is False

    # A STALE review's persisted failure belongs to the previous payload
    # bytes: the owner may have fixed it by hand, so the FRESH fact stays
    # False — the recorded failure surfaces as preflight_failed_stale (D11)
    # and the card keeps Re-review primary with Repair offered from the menu.
    stale = skill_review_gate("pending", stale=True, findings=[_PREFLIGHT_FAIL])
    assert stale["preflight_failed"] is False

    benign = skill_review_gate("pending", findings=[])
    assert benign["preflight_failed"] is False

    # Absence is a fact: a status-only caller must not fabricate False —
    # older/leaner producers simply do not emit the key.
    status_only = skill_review_gate("pending")
    assert "preflight_failed" not in status_only


def test_gate_shape_is_otherwise_unchanged():
    base = skill_review_gate("pending")
    extended = skill_review_gate("pending", findings=[])
    assert set(extended) - set(base) == {"preflight_failed", "preflight_failed_stale"}
    for key in base:
        assert base[key] == extended[key]


def test_stale_companion_key_matrix():
    """D11 matrix: the stale companion is present exactly with the fresh fact,
    True only for a recorded FAIL whose findings are stale."""
    # Absent when the caller did not pass findings (could not know).
    assert "preflight_failed_stale" not in skill_review_gate("pending")
    # Fresh clean: both facts honestly False.
    fresh_clean = skill_review_gate("clean", findings=[])
    assert fresh_clean["preflight_failed"] is False
    assert fresh_clean["preflight_failed_stale"] is False
    # Stale clean: nothing recorded to surface.
    stale_clean = skill_review_gate("clean", stale=True, findings=[])
    assert stale_clean["preflight_failed"] is False
    assert stale_clean["preflight_failed_stale"] is False
    # Stale + recorded FAIL: the stale companion carries the fact.
    stale_fail = skill_review_gate("pending", stale=True, findings=[_PREFLIGHT_FAIL])
    assert stale_fail["preflight_failed"] is False
    assert stale_fail["preflight_failed_stale"] is True
    # Fresh FAIL keeps the fresh fact only (existing behavior unchanged).
    fresh_fail = skill_review_gate("pending", findings=[_PREFLIGHT_FAIL])
    assert fresh_fail["preflight_failed"] is True
    assert fresh_fail["preflight_failed_stale"] is False


def test_owner_attestation_never_bypasses_a_failed_preflight(monkeypatch, tmp_path):
    """#335 acceptance: Skip review NEVER bypasses the deterministic preflight
    — a FAIL outcome is returned verbatim (pending, carrying the finding) and
    no CLEAN verdict or marker is produced."""
    import ouroboros.skill_owner_attestation as soa
    import ouroboros.skill_review as sr

    failed = sr.SkillReviewOutcome(
        skill_name="s", status=sr.STATUS_PENDING, content_hash="hash",
        findings=[_PREFLIGHT_FAIL],
    )
    monkeypatch.setattr(sr, "_run_deterministic_preflight", lambda *a, **k: failed)
    ctx = types.SimpleNamespace(drive_root=str(tmp_path))
    out = soa.run_owner_attestation(ctx, tmp_path, types.SimpleNamespace(name="s"), "hash")
    assert out is failed
    assert out.status == sr.STATUS_PENDING
    assert preflight_failed(out.findings) is True


def _patch_failing_preflight(monkeypatch):
    import ouroboros.tools.skill_preflight as sp

    monkeypatch.setattr(
        sp, "_handle_skill_preflight",
        lambda ctx, **kwargs: json.dumps({
            "ok": False,
            "manifest": [{"item": "manifest_entry_exists", "ok": False,
                          "detail": "missing or escaping entry: plugin.py"}],
        }),
    )


def test_failed_attestation_persists_the_fail_and_the_gate_turns_fresh(monkeypatch, tmp_path):
    """D9(1)/D10 regression cycle: a failed attestation with NO recorded review
    persists the preflight FAIL as a normal review result, so the gate carries
    a FRESH ``preflight_failed`` — the card then offers Repair primary and
    hides the Skip review dead end (no extra Re-review click needed)."""
    import ouroboros.skill_owner_attestation as soa
    from ouroboros.skill_loader import load_review_state

    _patch_failing_preflight(monkeypatch)
    ctx = types.SimpleNamespace(drive_root=str(tmp_path))

    out = soa.run_owner_attestation(ctx, tmp_path, types.SimpleNamespace(name="s"), "hash-b")
    assert out.status == "pending"
    assert preflight_failed(out.findings) is True

    persisted = load_review_state(tmp_path, "s")
    assert persisted.content_hash == "hash-b"
    assert preflight_failed(persisted.findings) is True
    gate = skill_review_gate(
        persisted.status, stale=persisted.is_stale_for("hash-b"),
        findings=persisted.findings,
    )
    assert gate["preflight_failed"] is True
    assert gate["preflight_failed_stale"] is False
    assert gate["executable_review"] is False


def test_failed_attestation_supersedes_stale_but_never_a_fresh_verdict(monkeypatch, tmp_path):
    """RO12 guard: a stale recorded CLEAN is knowingly superseded by the fresh
    FAIL at the current hash; a FRESH valid verdict is never overwritten."""
    import ouroboros.skill_owner_attestation as soa
    from ouroboros.skill_loader import SkillReviewState, load_review_state, save_review_state

    _patch_failing_preflight(monkeypatch)
    ctx = types.SimpleNamespace(drive_root=str(tmp_path))
    pass_finding = {
        "item": "bug_hunting", "verdict": "PASS", "severity": "info",
        "reason": "reviewed", "model": "m",
    }

    # Stale CLEAN (recorded at hash-a, payload now hashes hash-b) -> overwritten.
    save_review_state(tmp_path, "stale_skill", SkillReviewState(
        status="clean", content_hash="hash-a", findings=[pass_finding],
    ))
    out = soa.run_owner_attestation(
        ctx, tmp_path, types.SimpleNamespace(name="stale_skill"), "hash-b")
    assert out.status == "pending"
    persisted = load_review_state(tmp_path, "stale_skill")
    assert persisted.content_hash == "hash-b"
    assert preflight_failed(persisted.findings) is True

    # Fresh valid verdict at the CURRENT hash -> untouched; the failure only
    # surfaces through the endpoint outcome (409), state as-is.
    save_review_state(tmp_path, "fresh_skill", SkillReviewState(
        status="clean", content_hash="hash-b", findings=[pass_finding],
    ))
    out = soa.run_owner_attestation(
        ctx, tmp_path, types.SimpleNamespace(name="fresh_skill"), "hash-b")
    assert out.status == "pending"
    assert preflight_failed(out.findings) is True
    persisted = load_review_state(tmp_path, "fresh_skill")
    assert persisted.status == "clean"
    assert persisted.content_hash == "hash-b"
    assert preflight_failed(persisted.findings) is False
