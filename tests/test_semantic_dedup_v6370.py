"""v6.37.0 guards for the semantic-dedup primitive (C9.6) and its consumers
(C9.1/C9.2/C9.4/C9.5/C10.1/C10.2). The LLM is mocked everywhere — these assert the
structural contracts (fail-open, exact-id validation, fingerprint redirect, global
backlog routing, typed artifact field, digest injection), not model quality."""

from __future__ import annotations

from types import SimpleNamespace


# --------------------------------------------------------------------------- #
# C9.6 — the primitive: fail-open, exact-id validation, confidence gate
# --------------------------------------------------------------------------- #

def _fake_llm(monkeypatch, content):
    import ouroboros.config as cfg
    import ouroboros.llm as llm_mod

    class FakeClient:
        def chat(self, **kw):
            return ({"content": content}, {})

    monkeypatch.setattr(llm_mod, "LLMClient", FakeClient)
    monkeypatch.setattr(cfg, "get_light_model", lambda: "fake-model")


def test_primitive_fail_open_without_candidates_or_text():
    from ouroboros.semantic_dedup import find_semantic_duplicate_id

    assert find_semantic_duplicate_id("x", [], subject="s", call_type="t") is None
    assert find_semantic_duplicate_id("", [{"id": "a", "text": "y"}], subject="s", call_type="t") is None


def test_primitive_returns_exact_high_confidence_match(monkeypatch):
    _fake_llm(monkeypatch, '{"duplicate_id":"obl-1","confidence":"high","reason":"same"}')
    from ouroboros.semantic_dedup import find_semantic_duplicate_id

    got = find_semantic_duplicate_id(
        "new bug", [{"id": "obl-1", "text": "old bug"}], subject="s", call_type="t", drive_root=None
    )
    assert got == "obl-1"


def test_primitive_rejects_low_confidence(monkeypatch):
    _fake_llm(monkeypatch, '{"duplicate_id":"obl-1","confidence":"medium","reason":"maybe"}')
    from ouroboros.semantic_dedup import find_semantic_duplicate_id

    assert find_semantic_duplicate_id("n", [{"id": "obl-1", "text": "o"}], subject="s", call_type="t", drive_root=None) is None


def test_primitive_rejects_id_not_in_candidates(monkeypatch):
    _fake_llm(monkeypatch, '{"duplicate_id":"obl-999","confidence":"high","reason":"hallucinated"}')
    from ouroboros.semantic_dedup import find_semantic_duplicate_id

    assert find_semantic_duplicate_id("n", [{"id": "obl-1", "text": "o"}], subject="s", call_type="t", drive_root=None) is None


def test_primitive_fail_open_on_unparseable(monkeypatch):
    _fake_llm(monkeypatch, "not json at all")
    from ouroboros.semantic_dedup import find_semantic_duplicate_id

    assert find_semantic_duplicate_id("n", [{"id": "obl-1", "text": "o"}], subject="s", call_type="t", drive_root=None) is None


# --------------------------------------------------------------------------- #
# C9.2 — backlog: reworded restatement folds into the duplicate, not a new item
# --------------------------------------------------------------------------- #

def test_backlog_semantic_redirect_bumps_instead_of_new(monkeypatch, tmp_path):
    import ouroboros.improvement_backlog as ib
    import ouroboros.semantic_dedup as sd

    ib.append_backlog_items(tmp_path, [{
        "summary": "Flaky retry on transport 429", "category": "reliability", "source": "task",
    }])
    before = ib.load_backlog_items(tmp_path)
    assert len(before) == 1
    target_id = before[0]["id"]

    # The detector (lazily imported from semantic_dedup) says the reworded item is a dup.
    monkeypatch.setattr(sd, "find_semantic_duplicate_id", lambda *a, **k: target_id)
    ib.append_backlog_items(tmp_path, [{
        "summary": "Retries storm out on a 429 from the provider transport",
        "category": "reliability", "source": "task",
    }])
    after = ib.load_backlog_items(tmp_path)
    assert len(after) == 1  # folded in, no second ibl-*
    assert int(after[0]["count"]) == 2


def test_backlog_no_redirect_creates_new(monkeypatch, tmp_path):
    import ouroboros.improvement_backlog as ib
    import ouroboros.semantic_dedup as sd

    ib.append_backlog_items(tmp_path, [{"summary": "A", "category": "c", "source": "task"}])
    monkeypatch.setattr(sd, "find_semantic_duplicate_id", lambda *a, **k: None)
    ib.append_backlog_items(tmp_path, [{"summary": "Totally different B", "category": "c", "source": "task"}])
    assert len(ib.load_backlog_items(tmp_path)) == 2


# --------------------------------------------------------------------------- #
# C10.1 Fix A — merge_backlog_text + global routing
# --------------------------------------------------------------------------- #

def test_merge_backlog_text_fail_closed_on_unparseable(tmp_path):
    from ouroboros.improvement_backlog import merge_backlog_text, append_backlog_items, load_backlog_items

    append_backlog_items(tmp_path, [{"summary": "keep me", "category": "c", "source": "s"}])
    # Prose with no `### ibl-` item blocks -> fail-closed, backlog preserved.
    assert merge_backlog_text(tmp_path, "the model rambled but wrote no items") == -1
    assert len(load_backlog_items(tmp_path)) == 1


def test_project_scoped_backlog_write_routes_to_global_store(tmp_path, monkeypatch):
    import ouroboros.config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path, raising=False)
    from ouroboros.tools import knowledge as kn
    from ouroboros.improvement_backlog import backlog_path

    ctx = SimpleNamespace(
        drive_root=tmp_path, budget_drive_root="", project_id="proj-x", task_id="t1",
        drive_path=lambda rel: tmp_path / rel,
    )
    item_block = "# Improvement Backlog\n\n### ibl-z\n- summary: routed globally\n- category: c\n- source: s\n"
    msg = kn._knowledge_write(ctx, "improvement-backlog", item_block, mode="overwrite")
    assert "global backlog" in msg
    # Landed in the GLOBAL store, NOT projects/proj-x/knowledge.
    assert backlog_path(tmp_path).exists()
    assert "routed globally" in backlog_path(tmp_path).read_text(encoding="utf-8")
    assert not (tmp_path / "projects" / "proj-x" / "knowledge" / "improvement-backlog.md").exists()


# --------------------------------------------------------------------------- #
# C9.4 — consolidator validates topics through the single sanitizer
# --------------------------------------------------------------------------- #

def test_consolidator_skips_invalid_topic(tmp_path):
    from ouroboros.consolidator import _write_knowledge_entries

    kdir = tmp_path / "knowledge"
    _write_knowledge_entries(kdir, [
        {"topic": "valid-topic", "content": "ok"},
        {"topic": "has spaces!", "content": "useful name is preserved"},
        {"topic": "../evil", "content": "must not escape the shelf"},
    ])
    assert (kdir / "valid-topic.md").exists()
    assert "useful name is preserved" in (kdir / "has spaces!.md").read_text()
    assert not (tmp_path / "evil.md").exists()


# --------------------------------------------------------------------------- #
# C9.5 — outcomes recovery keys on the TYPED artifact_registered flag
# --------------------------------------------------------------------------- #

def test_outcomes_recovery_uses_typed_artifact_flag():
    from ouroboros.outcomes import _classify_tool_errors

    trace = {"tool_calls": [
        {"tool": "run_command", "is_error": True, "status": "artifact_output_error",
         "args": {"cmd": "build", "outputs": ["dist/app"]}, "result": "preview truncated, no marker here"},
        {"tool": "run_command", "is_error": False, "status": "ok",
         "args": {"cmd": "build", "outputs": ["dist/app"]}, "artifact_registered": True, "result": "ok"},
    ]}
    classified = _classify_tool_errors(trace)
    # The error is recovered via the typed flag despite the prose preview lacking the marker.
    assert not classified["unresolved"]
    assert classified["recovered"]


# --------------------------------------------------------------------------- #
# C9.3 — review_state obligation redirect (off-lock map applied under lock)
# --------------------------------------------------------------------------- #

def test_compute_obligation_redirects_empty_without_candidates(tmp_path):
    from ouroboros.review_state import AdvisoryReviewState, compute_obligation_semantic_redirects

    state = AdvisoryReviewState()
    findings = [{"verdict": "FAIL", "severity": "critical", "item": "bug_1", "reason": "boom"}]
    assert compute_obligation_semantic_redirects(state, findings, repo_key="r", drive_root=tmp_path) == {}


def test_obligation_redirect_folds_into_existing(monkeypatch):
    from ouroboros.review_state import AdvisoryReviewState, CommitAttemptRecord

    state = AdvisoryReviewState()
    a1 = CommitAttemptRecord(
        ts="2026-06-18T00:00:00Z", commit_message="m1", status="blocked", repo_key="r", blocked=True,
        critical_findings=[{"verdict": "FAIL", "severity": "critical", "item": "bug_login", "reason": "npe on submit"}],
    )
    state.record_attempt(a1)
    opens = state.get_open_obligations(repo_key="r")
    assert len(opens) == 1
    target = opens[0].obligation_id

    # A reworded restatement misses the exact fingerprint; the off-lock map redirects it.
    a2 = CommitAttemptRecord(
        ts="2026-06-18T00:01:00Z", commit_message="m2", status="blocked", repo_key="r", blocked=True,
        critical_findings=[{"verdict": "FAIL", "severity": "critical", "item": "bug_login", "reason": "null pointer when the form is submitted"}],
    )
    from ouroboros.review_state import _make_obligation_fingerprint
    fp = _make_obligation_fingerprint("bug_login", "null pointer when the form is submitted")
    state.record_attempt(a2, semantic_redirects={fp: target})
    assert len(state.get_open_obligations(repo_key="r")) == 1  # folded, not a new obligation


# --------------------------------------------------------------------------- #
# C10.2 Fix B — evolution prompt carries the backlog digest as context
# --------------------------------------------------------------------------- #

def test_evolution_task_text_injects_backlog_digest(monkeypatch, tmp_path):
    import supervisor.evolution_lifecycle as el
    import supervisor.queue as sq
    from ouroboros.improvement_backlog import append_backlog_items

    append_backlog_items(tmp_path, [{"summary": "ctx-only backlog item", "category": "c", "source": "s", "priority": "high"}])
    monkeypatch.setattr(el, "_read_evolution_campaign", lambda: {"status": "active", "id": "camp", "objective": "improve"})
    # build_evolution_task_text does `from supervisor import queue` locally, so the real
    # module attribute (not el.queue) is what it reads — isolate the drive root there.
    monkeypatch.setattr(sq, "DRIVE_ROOT", tmp_path, raising=False)

    text = el.build_evolution_task_text(1)
    assert "Improvement Backlog (context only" in text
    assert "ctx-only backlog item" in text
    assert "NOT a work order" in text
