"""Selected context facts name their sources without adding advice or another allowance."""
from dataclasses import replace

from ouroboros import capability_evidence as ce
from ouroboros.context_window import record_observation, resolve_window
from ouroboros.context_view_facts import window_fact, wake_facts
from ouroboros import memory_view as mv
from tests.test_memory_view_lever import plan
from tests._memory_view_synthetic import live_room


def test_source_changes_on_numbered_refusal_and_later_acceptance(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "canonical_evidence_root", lambda: tmp_path)
    evidence = ce.CapabilityEvidence(200000, "confirmed", "metadata", "route", window_kind="input")
    record = resolve_window(evidence, 65536)
    p, _ = plan()
    p = replace(p, window_record=record, window_tokens=record.total_equivalent)
    assert "200,000 input: metadata" in window_fact(p, 65536)
    assert resolve_window(p, 8192).usable == 200000  # Nano changes R, never an input-only boundary.
    record_observation(tmp_path, "route", refused=128000)
    assert "128,000 total: refusal" in window_fact(p, 65536)
    record_observation(tmp_path, "route", accepted=210000)
    assert "210,000 input: accepted" in window_fact(p, 65536)
    other = replace(p, route_fp="other", window_record=resolve_window(replace(evidence, route_fp="other"), 65536))
    assert "metadata" in window_fact(other, 65536) and "accepted" not in window_fact(other, 65536)


def test_wake_counts_captured_open_rooms_and_other_roles_do_not():
    p, snap = plan()
    assert wake_facts(p, "max") == ""
    wake = replace(snap, spec=replace(snap.spec, role="consciousness"), live_rooms=(live_room(1),))
    p = replace(p, core=replace(p.core, memory_view_json=mv.snapshot_json(wake)))
    facts = wake_facts(p, "max")
    assert "unsealed rooms 2" in facts and "characters" in facts and "%" in facts
    assert "drafts awaiting a decision 0" in facts
