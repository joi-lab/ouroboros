"""A delegated run that did not succeed says WHY on its own settlement row.

``delegate_run_settled`` used to record only ``state: failed``: the engine's failure
object was never read at settlement, so every instrument that reads the custody log saw
a death without a cause. The row now carries what was asked for, the ENGINE's own code
("" when it gave none — never a host placeholder) and the words it reported, as opaque
facts. A succeeded row is unchanged, and the existing readers of the row tolerate the
new keys.
"""

from __future__ import annotations

import json

import pytest

from tests._delegated_transport_shared import (  # noqa: F401  (autouse fixture applies on import)
    _LiveRunStub,
    _owned_gateway_uses_each_test_transport,
)

WORDS = "Selected model is at capacity. Please try a different model."
# ``outcome_reason`` is the engine's TYPED terminal reason (``wall_clock_exceeded`` =
# maxSeconds expiry), the one fact the finite leaf continuation gate reads (#1196).
CAUSE_KEYS = {"requested_model", "failure_code", "reported_cause", "outcome_reason"}


def _settle(root, run_id, summary, *, model="gpt-6-astra"):
    """Start and settle one terminal run through the real custody writers; return its
    durable SETTLED row."""
    import ouroboros.delegate_custody as dc

    entry = dc.RunCustody(run_id=run_id, task_id="t-cause", route_id="codex", model=model,
                          project_id="p", project_owned=False, ledger_root=str(root))
    assert dc.record_started(root, entry, shape={"access": "readonly", "mode": "ask"})
    assert dc.settle_run(root, _LiveRunStub(), entry, {"summary": {"spendUsd": 0.0, **summary}})["settled"]
    dc._CUSTODY.clear()
    rows = [json.loads(line) for line in
            (root / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    mine = [r for r in rows if r.get("type") == dc.SETTLED and r.get("run_id") == run_id]
    assert len(mine) == 1
    return mine[0]


def test_a_failed_settlement_row_carries_its_cause_and_a_succeeded_row_is_unchanged(tmp_path):
    import ouroboros.delegate_custody as dc
    from ouroboros.delegate_evidence import task_execution_evidence

    failed = _settle(tmp_path, "run-dead", {"state": "failed", "failure": {
        "phase": "harness", "category": "harness_error", "code": None, "safeMessage": WORDS}})
    assert failed["state"] == "failed"
    # The engine gave no code: the row says so with "", never with a host-derived placeholder.
    assert {key: failed[key] for key in CAUSE_KEYS} == {
        "requested_model": "gpt-6-astra", "failure_code": "", "reported_cause": WORDS,
        "outcome_reason": ""}
    # The requested model never poses as the observed one (the evidence reader lists `model`).
    assert failed["model"] == ""
    # Other direction: a succeeded settlement has exactly the keys it had before.
    succeeded = _settle(tmp_path, "run-ok", {"state": "succeeded"})
    assert succeeded["state"] == "succeeded" and not CAUSE_KEYS & set(succeeded)
    assert set(failed) - set(succeeded) == CAUSE_KEYS and set(succeeded) <= set(failed)
    # The row's existing readers take the new keys in their stride: custody replay still
    # closes both runs on their typed state, and the task's execution evidence still
    # counts one failure and one success without reading the words.
    replayed = dc.replay(tmp_path)
    assert {run: (c.settled, c.terminal_state, c.model) for run, c in replayed.items()} == {
        "run-dead": (True, "failed", "gpt-6-astra"), "run-ok": (True, "succeeded", "gpt-6-astra")}
    evidence = task_execution_evidence(tmp_path, "t-cause")
    assert WORDS not in json.dumps(evidence, ensure_ascii=False, default=str)


@pytest.mark.parametrize("state,failure,expected", [
    # A typed engine code rides verbatim, beside the words.
    ("failed", {"code": "subscription_window_exhausted", "safeMessage": "window spent",
                "resetsAt": "2030-01-01T00:00:00Z"},
     {"failure_code": "subscription_window_exhausted", "reported_cause": "window spent"}),
    # Every non-succeeded terminal state carries the keys, empty when the engine said nothing.
    ("interrupted", None, {"failure_code": "", "reported_cause": ""}),
    ("cancelled", "prose, not an object", {"failure_code": "", "reported_cause": ""}),
])
def test_every_non_succeeded_state_carries_the_keys_with_honest_absence(tmp_path, state, failure, expected):
    row = _settle(tmp_path, f"run-{state}", {"state": state, "failure": failure}, model="")
    assert {key: row[key] for key in CAUSE_KEYS} == {"requested_model": "", "outcome_reason": "", **expected}
    # Other direction: the same summary on a succeeded run adds nothing.
    assert not CAUSE_KEYS & set(_settle(tmp_path, f"ok-{state}", {"state": "succeeded", "failure": failure}))
