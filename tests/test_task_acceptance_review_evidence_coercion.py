"""Regression coverage for ibl-329e1741d5f9.

task_acceptance_review crashed with `dictionary update sequence element #0
has length 1; 2 is required` when `evidence` arrived as something other than
a dict — most plausibly a JSON-encoded string, which some providers emit for
object-typed tool arguments instead of a nested object. `dict(some_str)`
iterates the string char-by-char, and each single character is not a
length-2 (key, value) pair, hence that exact error text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ouroboros import review_substrate as rs


def _ctx(tmp_path):
    return SimpleNamespace(
        task_id="t",
        drive_root=tmp_path,
        task_metadata={"root_task_id": "root", "parent_task_id": "root"},
        task_contract={},
    )


def _stub_review(monkeypatch):
    """Stub the review round-trip and CAPTURE what reaches the evidence builder.

    Returns (handler, captured); after a call, ``captured["agent_evidence"]``
    is the dict the handler actually forwarded — the only place the coercion
    is observable, since the returned tool text does not echo the evidence.
    """
    from ouroboros.tools.review import _handle_task_acceptance_review

    result = rs.ReviewRunResult(
        request={"surface": "task_acceptance"},
        actors=[
            {"slot_id": "s1", "signal": "PASS", "parsed": {"outcome_tier": "solved"}},
        ],
        parsed_findings=[], aggregate_signal="PASS",
    )
    captured: dict = {}

    def _build(ctx, **kwargs):
        captured.update(kwargs)
        return {"claim": "x"}

    # One configured packet row: a child reviews with it (no selection needed).
    monkeypatch.setattr(rs, "triad_delivery_slots", lambda **k: [rs.ReviewSlot(slot_id="s1", model="m")])
    monkeypatch.setattr(rs, "run_review_request", lambda *a, **k: result)
    monkeypatch.setattr(
        "ouroboros.review_evidence.build_task_acceptance_evidence", _build,
    )
    return _handle_task_acceptance_review, captured


def test_evidence_as_json_string_is_parsed_not_crashed(monkeypatch, tmp_path):
    handler, captured = _stub_review(monkeypatch)
    ctx = _ctx(tmp_path)
    # A JSON-encoded object passed as a string, the way a misbehaving
    # provider might double-encode an object-typed argument.
    out = handler(ctx, claim="done", goal="g", evidence=json.dumps({"tests": "green"}))
    assert '"outcome_tier": "solved"' in out or "solved" in out
    # The decoded object — not the string, not an empty dict — is what the
    # reviewer's evidence packet is built from.
    forwarded = captured["agent_evidence"]
    assert forwarded["tests"] == "green"
    assert "raw_evidence" not in forwarded


def test_evidence_as_plain_string_falls_back_to_raw_wrapper(monkeypatch, tmp_path):
    handler, captured = _stub_review(monkeypatch)
    ctx = _ctx(tmp_path)
    # Not JSON at all — must not raise; gets wrapped rather than dropped.
    out = handler(ctx, claim="done", goal="g", evidence="not json and not a dict")
    assert "solved" in out
    assert captured["agent_evidence"]["raw_evidence"] == "not json and not a dict"


def test_evidence_as_list_is_wrapped_not_dropped(monkeypatch, tmp_path):
    handler, captured = _stub_review(monkeypatch)
    ctx = _ctx(tmp_path)
    # A non-dict, non-string type (e.g. a list) must also degrade, not raise —
    # and must stay VISIBLE to the reviewer, symmetric with the string branch.
    out = handler(ctx, claim="done", goal="g", evidence=["a", "b"])
    assert "solved" in out
    assert captured["agent_evidence"]["raw_evidence"] == "['a', 'b']"


def test_oversized_non_dict_evidence_is_disclosed_not_silently_cut(monkeypatch, tmp_path):
    """A large non-dict payload is bounded with an explicit OMISSION NOTE,
    never hard-sliced: the reviewer sees the prefix AND that it was cut."""
    handler, captured = _stub_review(monkeypatch)
    ctx = _ctx(tmp_path)
    handler(ctx, claim="done", goal="g", evidence=list(range(3000)))
    forwarded = captured["agent_evidence"]["raw_evidence"]
    assert forwarded.startswith("[0, 1, 2")
    assert "⚠️ OMISSION NOTE" in forwarded
    assert "original length" in forwarded


def test_absent_evidence_stays_empty(monkeypatch, tmp_path):
    """Only a genuinely absent evidence argument yields an empty packet —
    the wrapper must not invent a raw_evidence entry for None."""
    handler, captured = _stub_review(monkeypatch)
    ctx = _ctx(tmp_path)
    handler(ctx, claim="done", goal="g")
    assert "raw_evidence" not in captured["agent_evidence"]
