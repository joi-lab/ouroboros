"""The acceptance packet's substrate_execution section: facts, never gates.

Charter D8 (owner 2026-08-28, N6=A + «работа уже сделана — пофиг каким
способом»): the packet carries host-attested substrate FACTS from durable
custody rows — visibility for the reviewer, with zero typed rules tying the
substrate to the verdict. These tests pin the section's shape and its honesty
rules (unreadable custody never reads as proven-empty; non-harness tasks stay
noise-free)."""

from types import SimpleNamespace

from ouroboros import delegate_custody as custody
from ouroboros.delegate_evidence import acceptance_substrate_facts


def _ctx(tmp_path, **extra):
    base = {
        "task_id": "sub-1",
        "drive_root": tmp_path,
        "budget_drive_root": str(tmp_path),
        "task_metadata": {},
        "_nanny_route_dispatched": True,
    }
    base.update(extra)
    return SimpleNamespace(**base)


def _emit_started(drive, run_id, task_id):
    assert custody.emit(drive, custody.STARTED, {
        "run_id": run_id, "task_id": task_id, "route": "codex",
        "project_id": "", "selected_subagent_id": "session-a",
    })


def test_non_harness_task_gets_no_section(tmp_path):
    ctx = _ctx(tmp_path, _nanny_route_dispatched=False)
    assert acceptance_substrate_facts(ctx, "sub-1") == {}


def test_harness_task_carries_counts_and_substrate(tmp_path):
    ctx = _ctx(tmp_path)
    _emit_started(tmp_path, "run-1", "sub-1")
    assert custody.emit(tmp_path, custody.SETTLED, {
        "run_id": "run-1", "task_id": "sub-1", "state": "succeeded",
        "spend_disclosed": True, "cost_usd": 0.0,
    })
    out = acceptance_substrate_facts(ctx, "sub-1")
    assert out["actual_substrate"] == "harness_used"
    assert out["delegated_runs_started"] == 1
    assert out["delegated_runs_settled"] == 1
    assert out["delegated_runs_succeeded"] == 1
    assert out["delegated_runs_failed"] == 0


def test_start_blocked_attempt_is_visible_without_any_run(tmp_path):
    # Plan D8: durable start-blocked facts ride the packet — an ATTEMPT is
    # evidence of obedience even when nothing started.
    from ouroboros.delegate_evidence import record_start_blocked

    ctx = _ctx(tmp_path)
    record_start_blocked(ctx, "sub-1", "route_disabled")
    out = acceptance_substrate_facts(ctx, "sub-1")
    assert out["actual_substrate"] == "native_only"
    assert out["delegated_runs_started"] == 0
    assert out["delegate_start_attempted"] is True


def test_unreadable_custody_never_reads_as_proven_empty(tmp_path, monkeypatch):
    from ouroboros import delegate_evidence

    ctx = _ctx(tmp_path)
    monkeypatch.setattr(
        delegate_evidence, "task_execution_evidence",
        lambda _root, _tid: {"evidence_read_failed": True},
    )
    out = acceptance_substrate_facts(ctx, "sub-1")
    assert out["evidence_read_failed"] is True
    assert "actual_substrate" not in out
    assert "delegated_runs_started" not in out


def test_configured_session_zero_run_facts_ride_the_section(tmp_path):
    ctx = _ctx(
        tmp_path,
        _configured_actor_bootstrap={
            "zero_run_receipt_recorded": True,
            "zero_run_decision": "incomplete",
            "zero_run_basis": "route down at start",
            "route_available": False,
        },
    )
    out = acceptance_substrate_facts(ctx, "sub-1")
    assert out["configured_session"] is True
    assert out["zero_run_decision"] == "incomplete"
    assert out["zero_run_basis"] == "route down at start"
    assert out["route_available"] is False


def test_the_section_is_facts_only_no_acceptance_consumer_gates_on_it():
    # Zero typed policy consumers: nothing routes, rejects, retries or scores
    # on substrate_execution — the reviewer prompt is the only reader.
    import pathlib
    import re

    repo = pathlib.Path(__file__).parents[1]
    hits = []
    for root in ("ouroboros", "supervisor"):
        for path in (repo / root).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "substrate_execution" in text and path.name not in {"review_evidence.py", "delegate_evidence.py"}:
                hits.append(path.name)
    assert hits == [], f"unexpected typed consumers of substrate_execution: {hits}"
    source = (repo / "ouroboros" / "review_evidence.py").read_text(encoding="utf-8")
    # The section is written into the packet and never compared/branched on.
    assert not re.search(r"substrate_execution\"?\]\s*(==|!=|in |not in )", source)
