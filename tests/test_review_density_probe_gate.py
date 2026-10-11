"""The triad pack's cold-start density rung (owner decision 2026-09-05, answer 1 = A).

The commit gate's triad pack gets the SAME cold-start tokenizer-density rung
(``capability_evidence.cold_start_density_probe``) at the pre-dispatch admission
seam (``review_admission``). Scope review delivers by retrieval and assembles no
pack, so it has no size refusal to probe before:

- triad fit: the rung runs before the degradation ladder, on every overflowing
  cold slot, once per slot, and never without a ctx (no drive root to record a
  witness on);
- a fresh exact-model witness -> no probe, the existing refusal path unchanged;
- a probe the paid ledger refuses -> typed disclosure (review event), the
  existing refusal path, no crash;
- a pack that fits -> no probe (never on every commit).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from ouroboros import capability_evidence as ce
from ouroboros.capability_evidence import DENSITY_PROBE_MAX_TOKENS
from ouroboros.tools import review_admission as admission
from ouroboros.tools.review_helpers import DENSITY_PROBE_SAMPLE_CHARS
from ouroboros.usage_accounting import BudgetExceeded


@pytest.fixture
def drive(tmp_path, monkeypatch):
    root = tmp_path / "drive"
    (root / "state").mkdir(parents=True)
    # The cap is computed through review_drive_root(None) -> config.DATA_DIR;
    # the probe records on ctx.drive_root: one root for both, as in production.
    monkeypatch.setattr("ouroboros.config.DATA_DIR", root)
    ce._DENSITY_MEMO.clear()
    return root


def _probe_chat(calls: list, density: float = 0.9):
    def chat(llm, **kwargs):
        calls.append(kwargs)
        chars = sum(len(m["content"]) for m in kwargs["messages"])
        return {"content": "OK"}, {"prompt_tokens": int(chars / 4 * density), "cost": 0.0}
    return chat


# --- triad fit -----------------------------------------------------------------

def _triad_env(monkeypatch, prefix_tokens: int):
    import ouroboros.tools.review as review

    monkeypatch.setattr(review, "reviewer_context_window", lambda model: 1_000_000)
    monkeypatch.setattr(review, "run_cmd", lambda *a, **kw: "")
    monkeypatch.setattr(review, "_REVIEW_PROMPT_TEMPLATE_STABLE", "{preamble}")
    monkeypatch.setattr(review, "_REVIEW_PROMPT_TEMPLATE_DYNAMIC",
                        "{current_files_section}\n{diff_text}\n{changed_files}")

    def assemble(files_section, staged_diff):
        stable = review._REVIEW_PROMPT_TEMPLATE_STABLE.format(preamble="g" * (prefix_tokens * 4))
        dynamic = review._REVIEW_PROMPT_TEMPLATE_DYNAMIC.format(
            current_files_section=files_section, diff_text=staged_diff, changed_files="a.py")
        return stable + "\n" + dynamic, len(stable) + 1

    return assemble


def test_triad_cold_store_probes_the_overflowed_slot_once_and_fits(tmp_path, drive, monkeypatch):
    # 600K estimated tokens of irreducible prefix: above the cold cap of a 1M
    # slot (900,000 / 1.65 = 545,454), below its measured cap (745,000).
    assemble = _triad_env(monkeypatch, 600_000)
    model = "openai/gpt-5.6-terra"
    progress: list = []
    ctx = SimpleNamespace(drive_root=drive, task_id="triad-test", emit_progress_fn=progress.append,
                          pending_events=[])
    calls: list = []
    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=_probe_chat(calls)):
        prompt, _stable, overflow = admission.fit_triad_prompt(
            [model], assemble, "full snapshot of a.py", "+x", "a.py", tmp_path, ctx=ctx)

    assert overflow == "", overflow
    assert "full snapshot of a.py" in prompt, "no degradation rung was needed after the witness"
    assert [c["call_type"] for c in calls] == [admission.DENSITY_PROBE_CALL_TYPE]
    assert calls[0]["model"] == model and calls[0]["max_tokens"] == DENSITY_PROBE_MAX_TOKENS
    assert len(calls[0]["messages"][1]["content"]) == DENSITY_PROBE_SAMPLE_CHARS
    assert ce.resolve_review_token_density(drive, model)[1] == "measured"
    events = [e for e in ctx.pending_events if e.get("type") == admission.DENSITY_PROBE_EVENT]
    assert len(events) == 1 and events[0]["surface"] == "triad_review"


def test_triad_warm_store_never_probes(tmp_path, drive, monkeypatch):
    assemble = _triad_env(monkeypatch, 600_000)
    model = "openai/gpt-5.6-terra"
    ce.record_token_density(drive, model, prompt_chars=400_000, prompt_tokens=90_000)
    calls: list = []
    ctx = SimpleNamespace(drive_root=drive, task_id="t", emit_progress_fn=lambda _t: None, pending_events=[])
    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=_probe_chat(calls)):
        _prompt, _stable, overflow = admission.fit_triad_prompt(
            [model], assemble, "full snapshot of a.py", "+x", "a.py", tmp_path, ctx=ctx)
    assert overflow == "" and calls == []


def test_triad_budget_refused_probe_keeps_the_typed_fit_terminal(tmp_path, drive, monkeypatch):
    assemble = _triad_env(monkeypatch, 800_000)  # above even the measured cap
    model = "openai/gpt-5.6-terra"
    ctx = SimpleNamespace(drive_root=drive, task_id="t", emit_progress_fn=lambda _t: None, pending_events=[])

    def refused(llm, **kwargs):
        raise BudgetExceeded("global budget exhausted")

    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=refused):
        _prompt, _stable, overflow = admission.fit_triad_prompt(
            [model], assemble, "full snapshot of a.py", "+x", "a.py", tmp_path, ctx=ctx)
    assert "REVIEW_BLOCKED" in overflow
    events = [e for e in ctx.pending_events if e.get("type") == admission.DENSITY_PROBE_EVENT]
    assert len(events) == 1 and events[0]["outcome"] == "budget_refused"


def test_triad_without_a_ctx_never_sends(tmp_path, drive, monkeypatch):
    """A bare fit-check (no ctx, no drive root to record a witness on) is the
    pre-rung behaviour byte for byte: no send, the cold cap, the typed block."""
    assemble = _triad_env(monkeypatch, 600_000)
    calls: list = []
    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=_probe_chat(calls)):
        _prompt, _stable, overflow = admission.fit_triad_prompt(
            ["openai/gpt-5.6-terra"], assemble, "full snapshot of a.py", "+x", "a.py", tmp_path)
    assert "REVIEW_BLOCKED" in overflow and calls == []


def test_triad_probes_every_overflowing_cold_slot_and_recomputes_the_quorum_cap(tmp_path, drive, monkeypatch):
    """Three DISTINCT cold models (the shipped panel shape): the rung probes
    every overflowing slot — not the first witness only — so the quorum cap
    (the quorum-th largest slot cap) moves and the prompt fits undegraded."""
    import ouroboros.tools.review as review
    from ouroboros.tools.review_synthesis import quorum_input_token_limit

    assemble = _triad_env(monkeypatch, 600_000)
    models = ["openai/gpt-5.6-terra", "anthropic/claude-fable-5.1", "x-ai/grok-4.6"]

    def caps() -> dict:
        return {m: review.calibrated_input_token_limit(
            m, context_window=1_000_000, output_reserve=50_000, tokenizer_margin=50_000,
            drive_root=drive) for m in models}

    cold_caps = caps()
    assert quorum_input_token_limit(models, cold_caps) < 600_000
    ctx = SimpleNamespace(drive_root=drive, task_id="triad-3", emit_progress_fn=lambda _t: None,
                          pending_events=[])
    calls: list = []
    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=_probe_chat(calls)):
        prompt, _stable, overflow = admission.fit_triad_prompt(
            models, assemble, "full snapshot of a.py", "+x", "a.py", tmp_path, ctx=ctx)

    assert overflow == "", overflow
    assert "full snapshot of a.py" in prompt, "no degradation rung after the witnesses"
    assert [c["model"] for c in calls] == models, "one probe per overflowing cold slot"
    assert all(ce.resolve_review_token_density(drive, m)[1] == "measured" for m in models)
    measured_caps = caps()
    assert all(measured_caps[m] > cold_caps[m] for m in models)
    assert quorum_input_token_limit(models, measured_caps) >= 600_000
    # Why a first-witness short-circuit was wrong: with only the first slot
    # measured the quorum cap is still the cold one and the pack still overflows.
    assert quorum_input_token_limit(models, {**cold_caps, models[0]: measured_caps[models[0]]}) < 600_000
    events = [e for e in ctx.pending_events if e.get("type") == admission.DENSITY_PROBE_EVENT]
    assert sorted(e["model"] for e in events) == sorted(models)


def test_triad_probes_only_the_cold_overflowing_slots(tmp_path, drive, monkeypatch):
    assemble = _triad_env(monkeypatch, 600_000)
    warm, cold_a, cold_b = "openai/gpt-5.6-terra", "anthropic/claude-fable-5.1", "x-ai/grok-4.6"
    ce.record_token_density(drive, warm, prompt_chars=400_000, prompt_tokens=90_000)
    ctx = SimpleNamespace(drive_root=drive, task_id="t", emit_progress_fn=lambda _t: None, pending_events=[])
    calls: list = []
    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=_probe_chat(calls)):
        _prompt, _stable, overflow = admission.fit_triad_prompt(
            [warm, cold_a, cold_b], assemble, "full snapshot of a.py", "+x", "a.py", tmp_path, ctx=ctx)
    assert overflow == ""
    assert [c["model"] for c in calls] == [cold_a, cold_b], "a warm slot never spends a probe"


# --- one physical attempt --------------------------------------------------------

class _CountingTransport:
    """A fake transport on the REAL physical-attempt rail: ``chat`` dispatches
    through ``execute_physical_attempt`` and redials once on a transient
    failure, exactly like the fallback ladder's body-error reroute."""

    def __init__(self, drive, *, transient_first: bool) -> None:
        self.drive, self.transient_first, self.sends = drive, transient_first, []

    def chat(self, **kwargs):
        from ouroboros.usage_accounting import AttemptRequest, execute_physical_attempt

        request = AttemptRequest(model=kwargs["model"], provider="openrouter", reservation_usd=0.0,
                                 drive_root=self.drive, task_id="probe-rail")

        def physical():
            self.sends.append(kwargs["model"])
            if self.transient_first and len(self.sends) == 1:
                raise RuntimeError("provider body error 429")
            chars = sum(len(m["content"]) for m in kwargs["messages"])
            return {"choices": [{"message": {"content": "OK"}}],
                    "usage": {"prompt_tokens": int(chars / 4 * 0.9), "completion_tokens": 1}}

        try:
            response = execute_physical_attempt(request, physical)
        except RuntimeError:
            response = execute_physical_attempt(request, physical)  # the ladder's one redial
        return response["choices"][0]["message"], dict(response["usage"])


def _probe(drive, transport, model="openai/gpt-5.6-terra", progress=None) -> str:
    return ce.cold_start_density_probe(
        drive, transport, (progress if progress is not None else []).append, model, "z" * 80_000,
        task_id="t", call_type=admission.DENSITY_PROBE_CALL_TYPE, source="commit_gate_cold_start_probe")


def test_probe_is_exactly_one_physical_attempt_even_when_the_transport_would_redial(drive):
    from ouroboros.usage_accounting import PhysicalAttemptLimitExceeded, _claim_physical_dispatch

    transport = _CountingTransport(drive, transient_first=True)
    progress: list = []
    assert _probe(drive, transport, progress=progress) == "failed"
    assert transport.sends == ["openai/gpt-5.6-terra"], "the redial never reached the provider"
    assert ce.resolve_review_token_density(drive, "openai/gpt-5.6-terra")[1] == "cold_conservative"
    assert any("Density probe failed (PhysicalAttemptLimitExceeded)" in p for p in progress)
    _claim_physical_dispatch()  # the rail is released with the probe: no limit leaks out
    with pytest.raises(PhysicalAttemptLimitExceeded):
        transport.transient_first = False
        # Outside the probe the same transport redials freely; inside it cannot.
        from ouroboros.usage_accounting import physical_attempt_limit
        with physical_attempt_limit(1):
            _claim_physical_dispatch()
            _claim_physical_dispatch()


def test_probe_one_successful_physical_attempt_records_the_witness(drive):
    transport = _CountingTransport(drive, transient_first=False)
    assert _probe(drive, transport) == "measured"
    assert transport.sends == ["openai/gpt-5.6-terra"]
    assert ce.resolve_review_token_density(drive, "openai/gpt-5.6-terra")[1] == "measured"


# --- the gate's client seam and the shared rung -----------------------------------

def test_gate_uses_the_review_surface_client_seam_and_types_a_constructor_failure(tmp_path, drive, monkeypatch):
    import ouroboros.tools.review as review

    seam = object()
    monkeypatch.setattr(review, "LLMClient", lambda: seam)
    ctx = SimpleNamespace(drive_root=drive, task_id="t", emit_progress_fn=lambda _t: None, pending_events=[])
    seen: list = []

    def chat(llm, **kwargs):
        seen.append(llm)
        return {"content": "OK"}, {"prompt_tokens": 18_000}

    with mock.patch("ouroboros.llm_observability.chat_observed", side_effect=chat):
        assert admission.density_probe_before_size_refusal(ctx, "m/one", "z" * 80_000, surface="triad_review") == "measured"
    assert seen == [seam], "the probe's client comes from review.LLMClient"

    def broken():
        raise RuntimeError("no provider credentials")

    monkeypatch.setattr(review, "LLMClient", broken)
    ctx = SimpleNamespace(drive_root=drive, task_id="t", emit_progress_fn=lambda _t: None, pending_events=[])
    assert admission.density_probe_before_size_refusal(ctx, "m/two", "z" * 80_000, surface="scope_review") == "failed"
    events = [e for e in ctx.pending_events if e.get("type") == admission.DENSITY_PROBE_EVENT]
    assert len(events) == 1 and events[0]["outcome"] == "failed"
    assert "RuntimeError: no provider credentials" in events[0]["reason"]
