"""Phase D (review-custody sprint): the advisory lane's route-aware pack and
honest overflow classification.

Item 7 (owner-accepted A): the advisory used to inline ~830KB of governance
docs on BOTH delivery routes while its only size gate was the 1.6M char
constant — far above any real route window — so oversize prompts died
downstream as a false "harness crashed / Retry" classification. Now:

* api route: admission consults the REAL route window from the reviewer-window
  SSOT (``reviewer_window.resolve_reviewer_window``), not the 1.6M constant
  (which survives only as an emergency sanity ceiling);
* agent_session route: governance BODIES are replaced by resolvable pointers
  plus mandatory-read instructions (the plan-review agent_session precedent) —
  the session reads the docs itself;
* a dispatched failure matching the ``context_budget`` overflow SSOT becomes
  the typed non-blocking ``ADVISORY_SKIPPED: context_window_exceeded`` outcome;
  every other failure keeps the ``ADVISORY_ERROR`` shape.

Offline fixtures throughout: the window resolver and the transports are faked.
"""

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

import ouroboros.tools.claude_advisory_review as advisory
from ouroboros.reviewer_window import ReviewerWindow


_ADVISORY_ITEMS = json.dumps([
    {"item": "correctness", "verdict": "PASS", "severity": "advisory",
     "reason": "checked end to end"},
])


def _ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir(exist_ok=True)
    drive.mkdir(exist_ok=True)
    return ToolContext(repo_dir=repo, drive_root=drive)


def _write_governance_docs(repo):
    """Governance docs with one distinctive body marker each, written as the LF
    bytes the tracked docs are pinned to (`.gitattributes`): the reference-book
    reader delivers exact source bytes, so a platform-newline write would not
    measure the same text a newline-translating read expects."""
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    for rel, text in (
        ("BIBLE.md", "# BIBLE\nBIBLE-BODY-MARKER-7Q\n"),
        ("docs/CHECKLISTS.md", "## Repo Commit Checklist\nCHECKLIST-BODY-MARKER-7Q\n"),
        ("docs/DEVELOPMENT.md", "# DEV\nDEVELOPMENT-BODY-MARKER-7Q\n"),
        ("docs/DESIGN.md", "# DESIGN\nDESIGN-BODY-MARKER-7Q\n"),
        ("docs/ARCHITECTURE.md", "# ARCH\nARCHITECTURE-BODY-MARKER-7Q\n"),
    ):
        (repo / rel).write_bytes(text.encode("utf-8"))


_DOC_MARKERS = (
    "BIBLE-BODY-MARKER-7Q",
    "CHECKLIST-BODY-MARKER-7Q",
    "DEVELOPMENT-BODY-MARKER-7Q",
    "DESIGN-BODY-MARKER-7Q",
    "ARCHITECTURE-BODY-MARKER-7Q",
)


@pytest.fixture()
def api_env(monkeypatch):
    # Native (routed) advisory delivery: credentials follow the routed model.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)


def _fake_window(monkeypatch, tokens: int):
    monkeypatch.setattr(
        "ouroboros.reviewer_window.resolve_reviewer_window",
        lambda model, **kw: ReviewerWindow(
            window_tokens=tokens, status="confirmed", model=str(model)),
    )


def _no_dispatch(monkeypatch):
    def _boom(*args, **kwargs):  # pragma: no cover - failure signal only
        raise AssertionError("provider dispatch must not happen")
    monkeypatch.setattr(advisory, "_run_advisory_native", _boom)


def _stub_run_readonly(monkeypatch, **overrides):
    """Stub the NATIVE episode runner with the shared rehydrated result shape."""
    result = SimpleNamespace(
        success=True, result_text=_ADVISORY_ITEMS, session_id="sess-1",
        cost_usd=0.0, usage={}, error="", stderr_tail="",
    )
    for key, value in overrides.items():
        setattr(result, key, value)
    monkeypatch.setattr(
        advisory, "_run_advisory_native",
        lambda prompt, repo_dir, ctx_, slot, model, **_: (result, model),
    )
    return result


# ---------------------------------------------------------------------------
# 1. api admission consults the REAL route window, not the 1.6M constant
# ---------------------------------------------------------------------------


def test_api_admission_small_window_skips_before_dispatch(tmp_path, monkeypatch, api_env):
    """A small evidenced window skips the advisory BEFORE any dispatch even
    though the prompt is far below the 1.6M char constant — the constant is no
    longer the admission gate."""
    _fake_window(monkeypatch, 1_000)
    _no_dispatch(monkeypatch)
    ctx = _ctx(tmp_path)
    items, raw, model, chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED:")
    assert "does not fit the api route window" in raw
    # The reason names the window and the measured size.
    assert "1,000-token window" in raw
    assert f"{chars:,} chars" in raw
    assert chars < advisory._ADVISORY_PROMPT_MAX_CHARS  # constant did not decide
    assert model == advisory._advisory_default_model()
    # The pre-dispatch window skip stamps the meta snapshot like every skip.
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    assert meta.get("status") == "skipped"
    assert meta.get("skip_reason") == "route_window_exceeded"


def test_api_admission_big_window_proceeds(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(monkeypatch)
    ctx = _ctx(tmp_path)
    items, raw, model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert not raw.startswith("⚠️ ADVISORY_SKIPPED"), raw
    assert not raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert [i["item"] for i in items] == ["correctness"]
    assert model == advisory._advisory_default_model()


def test_api_window_skip_is_the_existing_typed_skip_status(tmp_path, monkeypatch, api_env):
    """The window skip rides the EXISTING non-blocking skip path: the handler
    persists a 'skipped' run for the snapshot, never an error."""
    _fake_window(monkeypatch, 1_000)
    _no_dispatch(monkeypatch)
    # Out-of-scope deterministic gate (P9 release metadata) — stubbed exactly as
    # the existing handler-path tests stub it (test_git_review_pipeline.py).
    monkeypatch.setattr(advisory, "_release_metadata_preflight", lambda *a, **kw: None)
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    for cmd in (["git", "init", "-q"],
                ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello\nchanged\n", encoding="utf-8", newline="\n")
    ctx = _ctx(tmp_path)
    payload = json.loads(advisory._handle_advisory_pre_review(
        ctx, commit_message="m", skip_tests=True,
    ))
    assert payload["status"] == "skipped"
    assert "does not fit the api route window" in payload["message"]


# ---------------------------------------------------------------------------
# 2. the brief's governance tiers: the activated rules in full, the map by
#    navigation (governance_context — one SSOT for every review surface)
# ---------------------------------------------------------------------------


def test_the_brief_tiers_the_governance_corpus_instead_of_inlining_it(tmp_path):
    """One delivery form, because both routes retrieve: the constitution rides
    the brief in full (tier 1) while the reference books and the documents this
    change does not activate are NAMED for reading, with the exact read
    instruction and their disposition in the manifest."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _write_governance_docs(repo)
    facts: dict = {}
    prompt = advisory._build_advisory_prompt(
        repo, "commit msg",
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "file-a",
                        "governance_facts": facts},
    )
    assert "BIBLE-BODY-MARKER-7Q" in prompt          # tier 1, in full
    for marker in ("DEVELOPMENT-BODY-MARKER-7Q", "DESIGN-BODY-MARKER-7Q",
                   "ARCHITECTURE-BODY-MARKER-7Q"):
        assert marker not in prompt                  # named, never inlined here
    assert "Governance navigation (read on demand)" in prompt
    assert 'read_file(root="system_repo"' in prompt
    assert "## Governance delivery (manifest)" in prompt
    rows = {row["path"]: row["disposition"] for row in facts["governance_manifest"]}
    assert rows["BIBLE.md"] == "inline"
    assert rows["docs/DESIGN.md"] == rows["docs/ARCHITECTURE.md"] == "navigation"
    # The non-governance sections are unchanged.
    assert "DIFF-SENTINEL" in prompt
    assert "commit msg" in prompt
    assert "file-a" in prompt


def test_delegated_route_dispatches_the_tiered_brief(tmp_path, monkeypatch):
    """_run_claude_advisory on the agent_session route hands the delegated
    session the same tiered brief: no reference-book bodies, and the session
    reads what the navigation names with its own tools."""
    # ABI-10: the delegated advisory is configured through the structured slots.
    monkeypatch.setenv("OUROBOROS_REVIEWER_SLOTS", json.dumps({
        "triad": [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "openai/x"}}],
        "scope": [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/y"}}],
        "advisory": {"enabled": True,
                     "route": {"kind": "agent_session", "target_id": "claude"}},
    }))
    ctx = _ctx(tmp_path)
    _write_governance_docs(ctx.repo_dir)
    captured = {}

    def _capture(prompt, repo_dir, ctx_):
        captured["prompt"] = prompt
        return SimpleNamespace(
            success=True, result_text=_ADVISORY_ITEMS, session_id="run-1",
            cost_usd=0.0, usage={}, error="", stderr_tail="",
        ), "fake-session-model"

    monkeypatch.setattr(advisory, "_run_advisory_delegated", _capture)
    items, raw, model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert not raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert [i["item"] for i in items] == ["correctness"]
    assert model == "fake-session-model"
    prompt = captured["prompt"]
    for marker in ("DEVELOPMENT-BODY-MARKER-7Q", "DESIGN-BODY-MARKER-7Q",
                   "ARCHITECTURE-BODY-MARKER-7Q"):
        assert marker not in prompt
    assert "BIBLE-BODY-MARKER-7Q" in prompt
    assert "Governance navigation (read on demand)" in prompt


# ---------------------------------------------------------------------------
# 3. post-dispatch overflow classification (context_budget SSOT)
# ---------------------------------------------------------------------------


def test_api_overflow_failure_becomes_typed_skip(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch,
        success=False,
        result_text="",
        error="API Error: prompt is too long: 251078 tokens > 200000 maximum",
    )
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: context_window_exceeded"), raw
    assert "native route" in raw
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    assert meta.get("status") == "skipped"
    assert meta.get("skip_reason") == "context_window_exceeded"


def test_delegated_overflow_failure_becomes_typed_skip(tmp_path, monkeypatch):
    # ABI-10: the delegated advisory is configured through the structured slots.
    monkeypatch.setenv("OUROBOROS_REVIEWER_SLOTS", json.dumps({
        "triad": [{"slot_id": "t1", "route": {"kind": "api_chat", "target_id": "openai/x"}}],
        "scope": [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "openai/y"}}],
        "advisory": {"enabled": True,
                     "route": {"kind": "agent_session", "target_id": "claude"}},
    }))

    def _failed(prompt, repo_dir, ctx_):
        return SimpleNamespace(
            success=False, result_text="(no output)", session_id="",
            cost_usd=0.0, usage={},
            error="ReviewSessionError: Prompt is too long for the selected route",
            stderr_tail="",
        ), ""

    monkeypatch.setattr(advisory, "_run_advisory_delegated", _failed)
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: context_window_exceeded"), raw
    assert "agent_session route" in raw


def test_raised_overflow_exception_becomes_typed_skip(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)

    def _raise(*a, **k):
        raise RuntimeError("provider rejected: context_length_exceeded")

    monkeypatch.setattr(advisory, "_run_advisory_native", _raise)
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: context_window_exceeded"), raw


def test_generic_failure_stays_advisory_error(tmp_path, monkeypatch, api_env):
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch,
        success=False,
        result_text="",
        error="transport reset by peer",
    )
    ctx = _ctx(tmp_path)
    items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_ERROR"), raw
    assert "context_window_exceeded" not in raw


def test_output_limit_rejection_is_not_reclassified(tmp_path, monkeypatch, api_env):
    """The SSOT's output-size precedence holds: an output/body-limit rejection
    is NOT a window overflow and keeps the error shape."""
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch,
        success=False,
        result_text="",
        error="max_tokens 65536 exceeds the maximum allowed for this model",
    )
    ctx = _ctx(tmp_path)
    _items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert raw.startswith("⚠️ ADVISORY_ERROR"), raw


# ---------------------------------------------------------------------------
# 4. the native episode's own bound end: typed, disclosed, money-bearing
# ---------------------------------------------------------------------------


_NATIVE_BOUND_FACTS = {
    "native_rounds": 7, "native_tool_calls": 11, "native_transcript_chars": 887_000,
    "native_transcript_bound": 900_000, "native_transcript_refused_chars": 912_345,
    "native_end_reason": "transcript_bound", "delivery": "native_tool_rounds",
    "prompt_tokens": 480_000, "completion_tokens": 3_000, "cost": 1.25,
}


def test_native_bound_end_is_a_typed_skip_carrying_the_episodes_numbers(tmp_path, monkeypatch, api_env):
    """``native_transcript_cap_exceeded`` is keyed on the STRUCTURED code, is
    NOT the provider window vocabulary, and reaches the caller as the typed
    non-blocking skip with the episode's bound/refused/rounds facts — never
    the generic ADVISORY_ERROR that reads as a crashed harness."""
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch, success=False, result_text="(no output)",
        error="ReviewRouteUnavailable: native review episode transcript (912345 chars) "
              "exceeded its bound (900000) before a final answer; the episode fails closed",
        failure_code="native_transcript_cap_exceeded", usage=dict(_NATIVE_BOUND_FACTS),
    )
    ctx = _ctx(tmp_path)
    items, raw, model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED: native_transcript_bound_exceeded"), raw
    assert "7 paid round(s)" in raw and "912,345 chars" in raw and "900,000-char bound" in raw
    assert "OUROBOROS_REVIEW_NATIVE_MAX_TRANSCRIPT_CHARS" in raw
    assert "context_window_exceeded" not in raw and "ADVISORY_ERROR" not in raw
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    assert meta.get("status") == "skipped"
    assert meta.get("skip_reason") == "native_transcript_bound_exceeded"
    # The paid rounds' custody survives on the meta (D-06d: never an empty {}).
    assert meta.get("usage", {}).get("native_rounds") == 7
    assert meta.get("usage", {}).get("cost") == 1.25
    assert model == advisory._advisory_default_model()


def _fake_native_executor(monkeypatch, *, raise_exc=None, facts=None):
    """Replace the native episode with a scripted executor; returns the log."""
    import ouroboros.llm as llm_mod
    import ouroboros.review_native_episode as native_episode

    seen = {}

    class _Executor:
        def __init__(self, assignment, *, llm=None):
            seen["assignment"] = assignment
            self._facts = dict(facts or {})

        def execute(self):
            if raise_exc is not None:
                raise raise_exc
            from ouroboros.review_execution import ReviewAttemptResult
            return ReviewAttemptResult(message={}, usage=dict(self._facts), raw_text=_ADVISORY_ITEMS)

        def failure_custody(self):
            return dict(self._facts)

    monkeypatch.setattr(native_episode, "NativeToolRoundReviewExecutor", _Executor)
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: object())
    return seen


def test_native_episode_failure_keeps_custody_and_the_typed_code(tmp_path, monkeypatch):
    """``_run_advisory_native`` no longer collapses an episode exception to
    ``usage={}``: the failure result carries ``failure_custody()`` and the
    exception's structured code; ``cost_usd`` stays the ledger's 0.0."""
    from ouroboros.review_execution import ReviewRouteUnavailable

    _fake_native_executor(
        monkeypatch, facts=_NATIVE_BOUND_FACTS,
        raise_exc=ReviewRouteUnavailable("exceeded its bound", code="native_transcript_cap_exceeded"),
    )
    ctx = _ctx(tmp_path)
    slot = SimpleNamespace(effort="low", subagent_id="")
    result, model = advisory._run_advisory_native("prompt", ctx.repo_dir, ctx, slot, "openai/adv")
    assert result.success is False and model == "openai/adv"
    assert result.failure_code == "native_transcript_cap_exceeded"
    assert {key: result.usage[key] for key in _NATIVE_BOUND_FACTS} == _NATIVE_BOUND_FACTS
    assert result.usage["operation_state"] == "settled"
    assert result.usage["physical_attempt_state"] == "settled"
    assert result.cost_usd == 0.0
    assert result.error.startswith("ReviewRouteUnavailable: exceeded its bound")


def test_uncoded_native_failure_stays_an_error_but_keeps_the_paid_rounds(tmp_path, monkeypatch, api_env):
    """A transport failure with no structured code keeps the ADVISORY_ERROR
    shape (no text sniffing), yet the rounds it paid for stay on the meta."""
    _fake_window(monkeypatch, 1_000_000)
    _stub_run_readonly(
        monkeypatch, success=False, result_text="(no output)",
        error="RuntimeError: transport reset by peer", failure_code="",
        usage={"native_rounds": 2, "native_transcript_bound": 900_000, "cost": 0.4},
    )
    ctx = _ctx(tmp_path)
    _items, raw, _model, _chars = advisory._run_claude_advisory(
        ctx.repo_dir, "msg", ctx, options={"include_repo_diff": False},
    )
    assert raw.startswith("⚠️ ADVISORY_ERROR"), raw
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    assert meta.get("status") == "error"
    assert meta.get("usage", {}).get("native_rounds") == 2


# ---------------------------------------------------------------------------
# 5. the window-derived transcript bound IS applied on the advisory's native episode
# ---------------------------------------------------------------------------


def test_native_advisory_episode_bound_is_derived_from_the_advisory_models_window(tmp_path, monkeypatch):
    """Pin for the default advisory delivery (an api_chat row = the native
    episode): the episode's send bound is ``review_native_transcript_bound``
    of the ADVISORY's own routed model, and the delivered usage discloses it.
    The pre-dispatch window gate runs on the same branch (``_predispatch_size_skip``
    returns None only for the delegated route)."""
    import ouroboros.llm as llm_mod
    import ouroboros.review_native_episode as native_episode

    bound_calls = []

    def _bound(model_id, *, output_reserve, use_local=None, mandatory_read_chars=0,
               model_role="", credential_profile_id=None):
        bound_calls.append((model_id, output_reserve))
        return 60_000  # above the first send; the floor below it is its own typed end

    monkeypatch.setattr(native_episode, "review_native_transcript_bound", _bound)

    class _Chat:
        def chat(self, **kwargs):
            return {"content": _ADVISORY_ITEMS}, {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0}

    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: _Chat())
    ctx = _ctx(tmp_path)
    (ctx.repo_dir / "a.txt").write_text("x\n", encoding="utf-8", newline="\n")
    slot = SimpleNamespace(effort="low", subagent_id="")
    result, model = advisory._run_advisory_native(
        "Review the worktree.", ctx.repo_dir, ctx, slot, "openai/adv-window")
    assert result.success is True, result.error
    assert bound_calls and bound_calls[0][0] == "openai/adv-window"
    assert result.usage["native_transcript_bound"] == 60_000
    assert result.usage["delivery"] == "native_tool_rounds"
    # The pre-dispatch window gate is the native branch's, not the delegated one's.
    _fake_window(monkeypatch, 1_000)
    assert advisory._predispatch_size_skip(ctx, True, "openai/adv-window", "p" * 40_000, False) is None
    native_skip = advisory._predispatch_size_skip(ctx, False, "openai/adv-window", "p" * 40_000, False)
    assert native_skip is not None and "does not fit the api route window" in native_skip[1]


# ---------------------------------------------------------------------------
# 6. the MANDATORY READ budget: the measured touched bodies, the bound the
#    episode applies, the typed multiwindow code
# ---------------------------------------------------------------------------


class _CapturingChat:
    """Answers at once; keeps every messages payload it was sent and the lane it rode."""

    def __init__(self):
        self.messages = []
        self.lanes = []

    def chat(self, **kwargs):
        self.messages.append(copy.deepcopy(kwargs["messages"]))
        self.lanes.append(kwargs.get("use_local"))
        return {"content": _ADVISORY_ITEMS}, {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0}


def _sent_task(chat):
    return [m for m in chat.messages[0] if m.get("role") == "user"][0]["content"]


def test_native_prompt_names_the_touched_reading_and_the_bound_the_episode_applies(tmp_path, monkeypatch):
    """The declared reading is the touched bodies the manifest names (the
    governance corpus is delivered, not read), and the prompt's MANDATORY READ
    budget names it beside THAT episode's bound — the number the episode
    applies — while the facts carry the declaration."""
    import ouroboros.llm as llm_mod
    from ouroboros.review_native_episode import native_landing_at

    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_TRANSCRIPT_CHARS", "200000")
    _fake_window(monkeypatch, 1_000_000)
    chat = _CapturingChat()
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: chat)
    ctx = _ctx(tmp_path)
    _write_governance_docs(ctx.repo_dir)
    (ctx.repo_dir / "touched.py").write_text("x = 1\n" * 20_000, encoding="utf-8", newline="\n")
    prompt = advisory._build_advisory_prompt(
        ctx.repo_dir, "commit msg", resolved_paths=["touched.py"],
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "M touched.py"},
    )
    corpus = advisory._mandatory_read_corpus_chars(ctx.repo_dir, ["touched.py"])
    assert corpus == (ctx.repo_dir / "touched.py").stat().st_size
    slot = SimpleNamespace(effort="low", subagent_id="")
    result, _model = advisory._run_advisory_native(
        prompt, ctx.repo_dir, ctx, slot, "openai/adv", mandatory_read_corpus_chars=corpus)
    assert result.success is True, result.error
    usage = result.usage
    need = len(prompt) + corpus
    assert usage["native_mandatory_read_chars"] == need
    bound = usage["native_transcript_bound"]
    assert bound == 200_000
    assert usage["native_mandatory_read_disclosure"] == "native_multiple_windows_required"
    task = _sent_task(chat)
    assert prompt in task and "## MANDATORY READ budget" in task
    assert f"hold {corpus:,} chars" in task and f"needs {need:,} transcript chars" in task
    assert f"bound is {bound:,} chars" in task and f"landing notice at {native_landing_at(bound):,} chars" in task
    assert "native_multiple_windows_required" in task
    assert "native_mandatory_read_exceeds_bound" not in task
    # Undeclared (the corpus argument left at 0): the prompt and the facts are untouched.
    chat.messages.clear()
    result, _model = advisory._run_advisory_native(prompt, ctx.repo_dir, ctx, slot, "openai/adv")
    assert "native_mandatory_read_chars" not in result.usage and result.usage["native_transcript_bound"] == 200_000
    assert "MANDATORY READ budget" not in _sent_task(chat)


def test_native_prompt_and_facts_carry_the_typed_code_when_the_reading_does_not_fit(tmp_path, monkeypatch, api_env):
    """Does-not-fit branch, end to end through _run_claude_advisory: a 200K
    window carries ≈446K chars and the touched body alone is over 500K, so the
    bound stays at the window's capacity and BOTH the prompt's MANDATORY READ
    budget and the advisory meta's usage carry the typed multiwindow code with
    the measured reading and the bound — never a silent full-read
    contradiction. The governance manifest rides the same meta."""
    import ouroboros.llm as llm_mod
    from ouroboros.review_native_episode import native_mandatory_read_bound

    _fake_window(monkeypatch, 200_000)
    chat = _CapturingChat()
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: chat)
    ctx = _ctx(tmp_path)
    _write_governance_docs(ctx.repo_dir)
    _git(ctx.repo_dir, "init", "-q")
    _git(ctx.repo_dir, "config", "user.email", "t@t")
    _git(ctx.repo_dir, "config", "user.name", "t")
    # The defect's shape: a one-line change inside a module whose BODY dwarfs
    # the diff, so only the required reading is large.
    body = "const line = 1;\n" * 40_000
    (ctx.repo_dir / "big.js").write_text(body, encoding="utf-8", newline="\n")
    _git(ctx.repo_dir, "add", "-A")
    _git(ctx.repo_dir, "commit", "-qm", "base")
    (ctx.repo_dir / "big.js").write_text(body + "const tail = 2;\n", encoding="utf-8", newline="\n")
    _git(ctx.repo_dir, "add", "-A")
    corpus = advisory._mandatory_read_corpus_chars(ctx.repo_dir, ["big.js"])
    assert corpus > 500_000
    items, raw, _model, _chars = advisory._run_claude_advisory(ctx.repo_dir, "msg", ctx)
    assert not raw.startswith("⚠️ ADVISORY"), raw
    assert [i["item"] for i in items] == ["correctness"]
    meta = dict(getattr(ctx, "_last_claude_advisory_meta", {}) or {})
    usage = meta["usage"]
    assert usage["native_mandatory_read_disclosure"] == "native_multiple_windows_required"
    bound = usage["native_transcript_bound"]
    assert 400_000 <= bound <= 460_000 < native_mandatory_read_bound(usage["native_mandatory_read_chars"])
    task = _sent_task(chat)
    assert "MANDATORY_READ_DISCLOSURE: native_multiple_windows_required" in task
    assert f"hold {corpus:,} chars" in task and f"bound is {bound:,} chars" in task
    # No body is inlined: the reading stays the reviewer's own.
    assert "const line = 1;\nconst line = 1;" not in task
    # The disclosure is a reading ORDER across the episode's successive working
    # views, never "this cannot be honoured": the episode continues past one view.
    assert "cannot be honoured" not in task
    assert "CONTINUES across successive working views" in task
    assert "Mark as unverified only what you did not actually read" in task
    # The durable prompt facts disclose what governance the brief delivered.
    tiers = {row["path"]: row["disposition"] for row in meta["governance_manifest"]}
    assert tiers["BIBLE.md"] == "inline" and tiers["docs/ARCHITECTURE.md"] == "navigation"


def test_local_advisory_model_previews_the_bound_on_its_own_local_window(tmp_path, monkeypatch):
    """The preview slot rides the dispatch builder (``reviewer_slots``: ``use_local``
    off the resolved route), so a LOCAL advisory model previews and dispatches on
    ITS window. Built with the default ``use_local=False`` the preview resolved the
    remote/unknown route instead: a local model with a smaller window had its bound
    lifted past the real local capacity and the typed shortfall never disclosed."""
    import ouroboros.llm as llm_mod
    import ouroboros.review_native_episode as native_episode
    import ouroboros.reviewer_window as reviewer_window
    from ouroboros.provider_models import provider_for_model
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_native_episode import native_mandatory_read_bound

    model = "qwen3-32b (local)"
    assert provider_for_model(model) == "local"
    resolved, seen = [], {}
    _real_executor = native_episode.NativeToolRoundReviewExecutor

    class _RecordingExecutor(_real_executor):
        def __init__(self, assignment, **kwargs):
            seen["slot"] = assignment.slot
            super().__init__(assignment, **kwargs)

    monkeypatch.setattr(native_episode, "NativeToolRoundReviewExecutor", _RecordingExecutor)

    def _window(model_id, *, use_local=None, **_kwargs):
        resolved.append(use_local)
        # The local lane's real window is small; the remote/unknown route would size at 1M.
        return ReviewerWindow(window_tokens=200_000 if use_local else 1_000_000,
                              status="confirmed", model=str(model_id))

    monkeypatch.setattr(reviewer_window, "resolve_reviewer_window", _window)
    chat = _CapturingChat()
    monkeypatch.setattr(llm_mod, "LLMClient", lambda *a, **k: chat)
    ctx = _ctx(tmp_path)
    _write_governance_docs(ctx.repo_dir)
    (ctx.repo_dir / "big.js").write_text("const line = 1;\n" * 40_000, encoding="utf-8", newline="\n")
    prompt = advisory._build_advisory_prompt(
        ctx.repo_dir, "commit msg", resolved_paths=["big.js"],
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "M big.js",
                        # The share is sized on the SAME lane the episode sends on.
                        "reviewer_model": model, "reviewer_use_local": True},
    )
    corpus = advisory._mandatory_read_corpus_chars(ctx.repo_dir, ["big.js"])
    assert corpus > 500_000
    slot = SimpleNamespace(effort="low", subagent_id="")
    result, _model = advisory._run_advisory_native(
        prompt, ctx.repo_dir, ctx, slot, model, mandatory_read_corpus_chars=corpus)
    assert result.success is True, result.error
    assert resolved and all(lane is True for lane in resolved)  # preview AND episode: the local route
    assert chat.lanes == [True]  # the provider call rode the same lane
    usage = result.usage
    bound = usage["native_transcript_bound"]
    assert 400_000 <= bound <= 460_000 < native_mandatory_read_bound(usage["native_mandatory_read_chars"])
    assert usage["native_mandatory_read_disclosure"] == "native_multiple_windows_required"
    task = _sent_task(chat)
    assert "MANDATORY_READ_DISCLOSURE: native_multiple_windows_required" in task
    assert f"bound is {bound:,} chars" in task
    # The slot the assignment carried is the dispatch builder's: local route, api_chat, advisory identity.
    rslot = seen["slot"]
    assert (rslot.slot_id, rslot.model, rslot.use_local, rslot.route, rslot.effort, rslot.subagent_id) == (
        "advisory_slot_1", model, True, ReviewRouteKind.API_CHAT, "low", "")


def test_declared_mandatory_reading_lifts_the_bound_past_the_ceiling_up_to_the_window(monkeypatch):
    """The episode-side arithmetic behind both branches (pinned here beside its
    advisory caller: tests/test_native_tool_round_executor.py sits at the band
    cap). A declared mandatory reading is a FLOOR (P13): it lifts the bound
    past the owner ceiling to where the reading lands one result cap before
    the landing notice — never past what the window carries; short of that,
    the typed shortfall code names it. An undeclared episode is unchanged."""
    import ouroboros.review_native_episode as native_episode
    import ouroboros.reviewer_window as reviewer_window
    from ouroboros.reviewer_window import REVIEWER_FULL_WINDOW

    monkeypatch.setenv("OUROBOROS_REVIEW_NATIVE_MAX_TRANSCRIPT_CHARS", "200000")
    windows = {"openai/big": REVIEWER_FULL_WINDOW, "openai/small": 200_000}
    monkeypatch.setattr(reviewer_window, "reviewer_context_window",
                        lambda model_id, **_: windows[model_id])
    need = 300_000
    required = native_episode.native_mandatory_read_bound(need)
    assert required == 525_000  # ceil((300K + the 120K result cap) / 0.8)
    assert native_episode.native_landing_at(required) >= need + native_episode._EPISODE_TOOL_RESULT_CHAR_CAP
    # Undeclared: the owner ceiling, exactly as before.
    assert native_episode.review_native_transcript_bound("openai/big", output_reserve=16_000) == 200_000
    big = native_episode.review_native_transcript_bound(
        "openai/big", output_reserve=16_000, mandatory_read_chars=need)
    assert big == 200_000
    assert native_episode.native_mandatory_read_disclosure(big, need) == "native_multiple_windows_required"
    # The 200K window carries ≈446K chars: the floor is capped there and typed.
    small = native_episode.review_native_transcript_bound(
        "openai/small", output_reserve=16_000, mandatory_read_chars=need)
    assert small == 200_000 and small < required
    assert native_episode.native_mandatory_read_disclosure(small, need) == "native_multiple_windows_required"
    assert native_episode.native_mandatory_read_disclosure(small, 0) == ""
    # The facts helper the episode folds into its custody: declared chars, plus the code only when short.
    request = SimpleNamespace(policy={"native_mandatory_read_chars": need})
    assert native_episode.native_mandatory_read_facts(request, big) == {
        "native_mandatory_read_chars": need,
        "native_mandatory_read_disclosure": "native_multiple_windows_required",
    }
    assert native_episode.native_mandatory_read_facts(request, small) == {
        "native_mandatory_read_chars": need,
        "native_mandatory_read_disclosure": "native_multiple_windows_required"}
    assert native_episode.native_mandatory_read_facts(SimpleNamespace(policy={}), big) == {}

# 5. the shared span-only release-carrier cut on the advisory's live-tree pair
#    (owner decision, F3 Q4 = A: the triad's cut with the same disclosure)
# ---------------------------------------------------------------------------


_UV_LOCK = (
    'version = 1\n\n[[package]]\nname = "ouroboros"\nversion = "{v}"\n'
    'source = {{ editable = "." }}\n\n[[package]]\nname = "httpx"\nversion = "0.27.0"\n'
)


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True).stdout


def _carrier_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8", newline="\n")
    (repo / "uv.lock").write_text(_UV_LOCK.format(v="1.0.0"), encoding="utf-8", newline="\n")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "ouroboros"\nversion = "1.0.0"\n', encoding="utf-8", newline="\n")
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _manifest_rows(manifest: str) -> dict:
    """The manifest's ``- <path> — <size> — <disposition>`` rows as a mapping.

    The PACK EXCLUSION NOTE's own reason lines are indented, so they never
    parse as rows."""
    rows = {}
    for line in manifest.splitlines():
        if line.startswith("- ") and line.count(" — ") == 2:
            path, size, disposition = line[2:].split(" — ")
            rows[path] = (size, disposition)
    return rows


def test_advisory_manifest_cuts_span_only_carriers_on_the_live_tree_pair(tmp_path):
    """The advisory reviews the LIVE tree, so its pair is HEAD vs the working
    tree the manifest measures — staged or not. A span-only carrier's row reads
    carrier-cut and the shared PACK EXCLUSION NOTE states why; every other
    touched path is an ordinary row. No body is inlined on either side, the
    governance pointers are untouched, and the note precedes the diff."""
    repo = _carrier_repo(tmp_path)
    (repo / "VERSION").write_text("1.0.1\n", encoding="utf-8", newline="\n")
    (repo / "uv.lock").write_text(_UV_LOCK.format(v="1.0.1"), encoding="utf-8", newline="\n")
    _git(repo, "add", "VERSION", "uv.lock")  # staged, span-only
    (repo / "pyproject.toml").write_text(  # UNSTAGED, and outside its span
        '[project]\nname = "ouroboros"\nversion = "1.0.1"\ndependencies = ["httpx"]\n',
        encoding="utf-8", newline="\n")
    (repo / "app.py").write_bytes(b"x = 2\n")
    porcelain = _git(repo, "status", "--porcelain")

    prompt = advisory._build_advisory_prompt(
        repo, "release: 1.0.1",
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": porcelain},
    )
    manifest = prompt[prompt.index("## Touched files"):prompt.index("## Staged diff")]
    rows = _manifest_rows(manifest)

    assert set(rows) == {"VERSION", "uv.lock", "pyproject.toml", "app.py"}
    assert rows["VERSION"][1] == rows["uv.lock"][1] == "carrier-cut"
    assert rows["app.py"] == ("6 bytes", "modified")
    assert rows["pyproject.toml"][1] == "modified"
    # No file bodies anywhere: neither a cut carrier's nor an ordinary path's.
    assert "editable" not in prompt and "x = 2" not in prompt and "httpx" not in prompt
    assert manifest.count("PACK EXCLUSION NOTE") == 1
    assert "VERSION_CARRIER_SPANS" in manifest and "version_carrier_desyncs" in manifest
    assert "byte-identical" not in manifest  # no prefix-dedup class on a retrieving brief
    assert "Governance navigation (read on demand)" in prompt
    assert prompt.index("PACK EXCLUSION NOTE") < prompt.index("## Staged diff")
    # A cut carrier is excluded from the declared reading too: the manifest row
    # tells the reviewer not to read it, so the budget must not require it.
    touched = ["VERSION", "uv.lock", "pyproject.toml", "app.py"]
    assert advisory._mandatory_read_corpus_chars(repo, touched) == sum(
        (repo / rel).stat().st_size for rel in ("pyproject.toml", "app.py"))


def test_advisory_manifest_keeps_a_carrier_whose_worktree_edit_leaves_its_span(tmp_path):
    """The live-tree pair is the truth: a carrier staged span-only but then
    edited outside its span in the working tree is an ordinary modified row;
    without VERSION in the change the release-bump mechanism is not engaged at
    all and nothing is cut."""
    from ouroboros.tools import preflight_review_prompt as prompt_mod

    repo = _carrier_repo(tmp_path)
    (repo / "VERSION").write_text("1.0.1\n", encoding="utf-8", newline="\n")
    (repo / "uv.lock").write_text(_UV_LOCK.format(v="1.0.1"), encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    (repo / "uv.lock").write_text(
        _UV_LOCK.format(v="1.0.1").replace("0.27.0", "0.28.0"), encoding="utf-8", newline="\n")

    manifest = prompt_mod._advisory_touched_manifest(
        repo, None, _git(repo, "status", "--porcelain"))
    rows = _manifest_rows(manifest)
    assert rows["VERSION"][1] == "carrier-cut" and rows["uv.lock"][1] == "modified"
    assert "0.28.0" not in manifest

    _git(repo, "reset", "-q", "HEAD", "VERSION")
    (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8", newline="\n")
    manifest = prompt_mod._advisory_touched_manifest(
        repo, None, _git(repo, "status", "--porcelain"))
    assert "carrier-cut" not in manifest and "PACK EXCLUSION NOTE" not in manifest
    assert _manifest_rows(manifest)["uv.lock"][1] == "modified"
