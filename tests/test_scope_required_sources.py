"""The change-relative required-source manifest of the scope reviewer.

What the producer owes, what identity each row carries (the same one a
``read_file`` receipt stamps), and how the chain reaches the episode's coverage
fold, the scope result and the panel reducer.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros.runtime_mode_policy import GIT_OPS_FAMILY_PATHS, SAFETY_CRITICAL_PATHS
from ouroboros.tools.scope_required_sources import (
    RANGE_BASIS,
    REQUIRED_SOURCE_ROOT,
    SCOPE_REQUIRED_SOURCES_POLICY,
    coverage_state,
    render_required_sources,
    render_touched_manifest,
    required_sources_ref,
    scope_required_sources,
    staged_touched_paths,
    staged_tree_identity,
    touched_manifest,
    uncovered_sources,
)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@ouroboros"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    return repo


def _write(repo, rel, text):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def _commit(repo, message="c"):
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_only_protected_contract_and_prompt_paths_are_owed_in_full(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "ouroboros/safety.py", "SAFETY = 1\n")
    _write(repo, "prompts/SYSTEM.md", "system prompt\n")
    _write(repo, "ouroboros/contracts/tool_abi.py", "ABI = 1\n")
    _write(repo, "ouroboros/tools/scope_review.py", "ordinary = 1\n")
    _write(repo, "web/modules/chat.js", "export const a = 1;\n")
    _commit(repo)

    rows = scope_required_sources(repo, [
        ("M", "ouroboros/safety.py"), ("M", "prompts/SYSTEM.md"),
        ("A", "ouroboros/contracts/tool_abi.py"),
        ("M", "ouroboros/tools/scope_review.py"), ("M", "web/modules/chat.js"),
    ])
    owed = {row["path"]: row["disposition"] for row in rows}
    assert owed == {
        "ouroboros/safety.py": "modified",
        "prompts/SYSTEM.md": "modified",
        "ouroboros/contracts/tool_abi.py": "added",
        # The contract package declares its cross-language twin.
        "web/modules/api_types.js": "twin",
    }
    # A merely-touched ordinary file is a POINTER, never a required source: its
    # complete change evidence is the inlined diff.
    assert "ouroboros/tools/scope_review.py" not in owed
    assert "web/modules/chat.js" not in owed


def test_row_identity_matches_the_read_file_receipt_it_will_be_folded_against(tmp_path):
    repo = _repo(tmp_path)
    # CRLF on disk: source_revision names the BYTES, the char ranges the
    # universal-newline text the reader delivers.
    (repo / "prompts").mkdir()
    (repo / "prompts" / "SYSTEM.md").write_bytes(b"first\r\nsecond\r\n")
    _commit(repo)

    row = scope_required_sources(repo, [("M", "prompts/SYSTEM.md")])[0]
    raw = (repo / "prompts" / "SYSTEM.md").read_bytes()
    text = raw.decode().replace("\r\n", "\n")
    assert row["root"] == REQUIRED_SOURCE_ROOT
    assert row["source_revision"] == hashlib.sha256(raw).hexdigest()
    assert row["complete_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert row["complete_chars"] == len(text) == 13
    assert row["range_basis"] == RANGE_BASIS
    assert row["coverage_basis"] == "candidate_blob"


def test_a_touched_family_member_owes_the_whole_declared_family(tmp_path):
    repo = _repo(tmp_path)
    for rel in sorted(GIT_OPS_FAMILY_PATHS):
        _write(repo, rel, f"# {rel}\n")
    _commit(repo)

    rows = scope_required_sources(repo, [("M", "supervisor/git_ops_reset.py")])
    owed = {row["path"]: row["disposition"] for row in rows}
    assert set(owed) == set(GIT_OPS_FAMILY_PATHS)
    assert owed["supervisor/git_ops_reset.py"] == "modified"
    assert owed["supervisor/git_ops.py"] == "family"


def test_the_tool_dispatch_family_is_read_from_the_protection_constant(tmp_path):
    repo = _repo(tmp_path)
    family = {p for p in SAFETY_CRITICAL_PATHS if p.startswith("ouroboros/tools/")}
    for rel in sorted(family):
        _write(repo, rel, f"# {rel}\n")
    _commit(repo)

    rows = scope_required_sources(repo, [("M", "ouroboros/tools/registry.py")])
    assert {row["path"] for row in rows} == family


def test_api_types_touched_owes_its_host_contract_twin(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "web/modules/api_types.js", "export const V = 1;\n")
    _write(repo, "ouroboros/gateway/contracts.py", "V = 1\n")
    _commit(repo)

    rows = scope_required_sources(repo, [("M", "web/modules/api_types.js")])
    owed = {row["path"]: row["disposition"] for row in rows}
    # One contract, two languages: both sides are owed in full.
    assert owed == {
        "web/modules/api_types.js": "modified",
        "ouroboros/gateway/contracts.py": "twin",
    }


def test_a_deleted_required_source_keeps_its_preimage_obligation(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "prompts/SYSTEM.md", "system prompt\n")
    _commit(repo)
    (repo / "prompts" / "SYSTEM.md").unlink()

    rows = scope_required_sources(repo, [("D", "prompts/SYSTEM.md")])
    assert rows == [{
        "root": REQUIRED_SOURCE_ROOT, "path": "prompts/SYSTEM.md",
        "disposition": "deleted", "coverage_basis": "preimage_unavailable",
        "preimage": "HEAD:prompts/SYSTEM.md",
        "reason": rows[0]["reason"],
    }]
    assert "not delivered" in rows[0]["reason"]


def test_an_unreadable_required_source_is_typed_rather_than_dropped(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "ouroboros/safety.py", "SAFETY = 1\n")
    _commit(repo)
    # A family member the candidate tree does not carry at all.
    rows = scope_required_sources(repo, [("M", "supervisor/git_ops.py")])
    bases = {row["path"]: row["coverage_basis"] for row in rows}
    assert set(bases.values()) == {"source_unavailable"}


def test_the_candidate_tree_binds_every_row_when_the_caller_supplies_it(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "prompts/SYSTEM.md", "system prompt\n")
    _commit(repo)

    rows = scope_required_sources(repo, [("M", "prompts/SYSTEM.md")], staged_tree_sha="d" * 40)
    assert rows[0]["candidate_tree"] == "d" * 40


def test_touched_paths_come_from_the_staged_index_or_the_managed_subject(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "keep.py", "a = 1\n")
    _write(repo, "gone.py", "b = 2\n")
    _commit(repo)
    _write(repo, "keep.py", "a = 2\n")
    (repo / "gone.py").unlink()
    _write(repo, "added.py", "c = 3\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

    assert sorted(staged_touched_paths(repo)) == [
        ("A", "added.py"), ("D", "gone.py"), ("M", "keep.py"),
    ]
    assert len(staged_tree_identity(repo)) == 40

    subject = SimpleNamespace(name_status=(("M", "resolved.py"),),
                              conflict_paths=("anchor.py",), staged_tree="e" * 40)
    assert staged_touched_paths(repo, subject) == [("M", "resolved.py"), ("M", "anchor.py")]
    assert staged_tree_identity(repo, subject) == "e" * 40


def test_a_subject_without_a_declared_path_set_falls_back_to_the_index(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "keep.py", "a = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    assert staged_touched_paths(repo, object()) == [("A", "keep.py")]


def test_a_rename_owes_the_preimage_path_too(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "prompts/SYSTEM.md", "x" * 200 + "\n")
    _commit(repo)
    subprocess.run(["git", "mv", "prompts/SYSTEM.md", "prompts/SYSTEM2.md"], cwd=repo, check=True)
    pairs = {path: status for status, path in staged_touched_paths(repo)}
    assert pairs == {"prompts/SYSTEM2.md": "R", "prompts/SYSTEM.md": "D"}

    owed = {row["path"]: row["disposition"] for row in scope_required_sources(repo)}
    assert owed["prompts/SYSTEM2.md"] == "modified"
    assert owed["prompts/SYSTEM.md"] == "deleted"


def test_the_manifest_ref_is_the_manifest_identity(tmp_path):
    rows = [{"path": "prompts/SYSTEM.md", "complete_chars": 3}]
    ref = required_sources_ref(rows, staged_tree_sha="a" * 40)
    assert ref["policy"] == SCOPE_REQUIRED_SOURCES_POLICY
    assert ref["staged_tree_sha"] == "a" * 40
    assert ref["required_source_count"] == 1
    assert ref["sha256"] == hashlib.sha256(json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    assert required_sources_ref([])["required_source_count"] == 0


def test_the_brief_states_the_manifest_is_a_minimum(tmp_path):
    text = render_required_sources([
        {"path": "prompts/SYSTEM.md", "disposition": "modified", "complete_chars": 1234},
        {"path": "prompts/OLD.md", "disposition": "deleted", "coverage_basis": "preimage_unavailable"},
    ])
    assert "MINIMUM, not a sufficiency claim" in text
    assert "prompts/SYSTEM.md (modified, 1,234 chars)" in text
    assert "prompts/OLD.md (deleted, preimage_unavailable)" in text
    assert "no source is owed in full" in render_required_sources([])


@pytest.mark.parametrize("raw,extent", [(b"a = 1\n", "6 bytes"), (b"a = 1\r\n", "7 bytes")])
def test_the_touched_manifest_carries_dispositions_and_candidate_sizes(tmp_path, raw, extent):
    repo = _repo(tmp_path)
    (repo / "keep.py").write_bytes(raw)
    rows = touched_manifest(repo, [("M", "keep.py"), ("D", "gone.py"), ("M", "keep.py")])
    assert rows == [
        {"path": "gone.py", "disposition": "deleted", "extent": "not in the candidate tree"},
        {"path": "keep.py", "disposition": "modified", "extent": extent},
    ]
    rendered = render_touched_manifest(rows)
    assert "complete change evidence is the staged diff" in rendered
    assert f"- keep.py (modified, {extent})" in rendered
    assert render_touched_manifest([]) == ""


@pytest.mark.parametrize("fact,expected", [
    ({"status": "complete"}, "complete"),
    ({"status": "complete", "reason": "declared_empty"}, "declared_empty"),
    ({"status": "incomplete"}, "incomplete"),
    ({"status": "unobserved", "reason": "required_source_manifest_missing"}, "unobserved"),
    (None, "unobserved"),
    ("not a fact", "unobserved"),
])
def test_the_four_coverage_states(fact, expected):
    assert coverage_state(fact) == expected


def test_uncovered_sources_names_only_the_rows_that_fell_short():
    fact = {"status": "incomplete", "sources": [
        {"path": "a.py", "status": "complete"},
        {"path": "b.py", "status": "incomplete"},
        {"path": "c.py", "status": "unobserved"},
    ]}
    assert uncovered_sources(fact) == ["b.py", "c.py"]
    assert uncovered_sources(None) == []


# ---------------------------------------------------------------------------
# The chain: producer -> request policy -> coverage -> result -> reducer
# ---------------------------------------------------------------------------

from tests.test_review_session_scope_wiring import _scope_ctx, _scope_matrix_rows  # noqa: E402

def _staged_protected_repo(tmp_path):
    import subprocess

    repo = tmp_path / "candidate"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@ouroboros"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "prompts").mkdir()
    (repo / "prompts" / "SYSTEM.md").write_text("runtime system prompt\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    (repo / "prompts" / "SYSTEM.md").write_text("runtime system prompt, amended\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    return repo


@pytest.mark.parametrize("delivery", ["native", "session"])
def test_the_required_source_manifest_reaches_both_retrieving_deliveries(
    tmp_path, monkeypatch, delivery
):
    """The producer's rows travel to the reviewer three ways: the exact
    identities in the request policy (folded by whoever observes the reads),
    the manifest identity, and the human list in the brief itself."""
    import ouroboros.tools.scope_review as scope_mod
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.scope_required_sources import SCOPE_REQUIRED_SOURCES_POLICY

    repo = _staged_protected_repo(tmp_path)
    captured = {}

    def _capture(request, *, slots, drive_root, llm, usage_ctx=None):
        captured["policy"] = dict(request.policy)
        captured["task"] = request.session_task
        captured["messages"] = list(request.messages)
        return SimpleNamespace(actors=[{
            "slot_id": slots[0].slot_id, "model": slots[0].model, "status": "ok",
            "raw_text": json.dumps(_scope_matrix_rows()),
            "usage": {"native_read_coverage": {"status": "complete", "sources": []}},
            "prompt_ref": {}, "response_ref": {},
        }])

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", _capture)
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path / "data")
    (tmp_path / "data").mkdir(exist_ok=True)
    result = scope_mod.run_scope_review(
        ctx, "amend the system prompt", slot_id="scope_slot_1",
        scope_model="fixture/model",
        route=ReviewRouteKind.AGENT_SESSION if delivery == "session" else ReviewRouteKind.API_CHAT,
    )

    rows = captured["policy"]["native_required_sources"]
    assert [row["path"] for row in rows] == ["prompts/SYSTEM.md"]
    assert rows[0]["complete_chars"] == len("runtime system prompt, amended\n")
    assert rows[0]["range_basis"] == "unicode_text_universal_newlines"
    ref = captured["policy"]["native_required_sources_ref"]
    assert ref["policy"] == SCOPE_REQUIRED_SOURCES_POLICY and ref["required_source_count"] == 1
    # No packet is ever rendered for a scope row, and the brief names the source.
    assert captured["messages"] == []
    assert "prompts/SYSTEM.md (modified" in captured["task"]
    assert "MINIMUM, not a sufficiency claim" in captured["task"]
    assert result.status == "responded"
    assert result.coverage == "complete"
    assert result.coverage_manifest_ref == ref


@pytest.mark.parametrize("fact,expected", [
    ({"status": "complete", "sources": []}, "complete"),
    ({"status": "complete", "reason": "declared_empty", "sources": []}, "declared_empty"),
    ({"status": "incomplete", "sources": [{"path": "prompts/SYSTEM.md", "status": "incomplete"}]},
     "incomplete"),
    (None, "unobserved"),
])
def test_the_scope_result_carries_the_observed_coverage_state(tmp_path, monkeypatch, fact, expected):
    """One reader for every delivery: whoever observed the reads reports the
    fact, and an absent fact is `unobserved` rather than a guess."""
    import ouroboros.tools.scope_review as scope_mod

    usage = {"native_read_coverage": fact} if fact is not None else {}
    monkeypatch.setattr(scope_mod, "_call_scope_llm",
                        lambda *_a, **_k: (json.dumps(_scope_matrix_rows()), usage, ""))
    result = scope_mod.run_scope_review(
        _scope_ctx(tmp_path), "coverage state", scope_model="fixture", slot_id="scope_slot_1")
    assert result.coverage == expected
    if fact is not None:
        assert result.context_manifest["native_read_coverage"] == fact


def _reduce(tmp_path, monkeypatch, rows, *, enforcement="blocking"):
    """Run the panel reducer over prepared per-row results."""
    from ouroboros import config as cfg
    from ouroboros.tools import parallel_review

    monkeypatch.setattr(cfg, "get_review_enforcement", lambda: enforcement)
    monkeypatch.setattr(parallel_review, "_scope_enforcement", lambda: enforcement)
    slots = [SimpleNamespace(model=f"m{i}", slot_id=f"scope_slot_{i + 1}", route=None,
                             effort="", session_target="", session_profile="", retrieves=True)
             for i in range(len(rows))]
    scope_rows = [{"slot": slot, "prepared": {"brief": 1}, "final": None}
                  for slot in slots]
    monkeypatch.setattr(parallel_review, "run_scope_review",
                        lambda _ctx, _msg, **kwargs: rows[kwargs["slot_id"]])
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, task_id="reduce",
                          pending_events=[])
    return parallel_review._run_scope(
        ctx, "reduce", scope_rows, True, goal="", scope="", review_rebuttal="",
        history_snapshot=[], scope_history={}), ctx


def _row(status="responded", coverage="complete", uncovered=()):
    from ouroboros.tools.scope_review import ScopeReviewResult

    manifest = {"native_read_coverage": {
        "status": "incomplete" if coverage == "incomplete" else "complete",
        "sources": [{"path": path, "status": "incomplete" if coverage == "incomplete" else "complete"}
                    for path in uncovered],
    }} if coverage != "unobserved" else {}
    return ScopeReviewResult(blocked=False, status=status, coverage=coverage,
                             model_id="m", context_manifest=manifest)


def test_unobserved_coverage_counts_toward_the_quorum(tmp_path, monkeypatch):
    """A delivery whose reads the host cannot see keeps the P3 exception: its
    verdict counts, and the provenance limit is disclosure, not a shortfall."""
    rows = {"scope_slot_1": _row(coverage="unobserved"),
            "scope_slot_2": _row(coverage="declared_empty")}
    result, _ctx = _reduce(tmp_path, monkeypatch, rows)
    assert result.blocked is False and result.status == "responded"
    assert result.context_manifest["scope_responded_count"] == 2
    assert result.context_manifest["scope_coverage_incomplete_count"] == 0


@pytest.mark.parametrize("enforcement", ["blocking", "advisory"])
@pytest.mark.parametrize("second_coverage", ["complete", "incomplete"])
def test_read_coverage_is_diagnostic_under_every_enforcement(
    tmp_path, monkeypatch, enforcement, second_coverage
):
    rows = {"scope_slot_1": _row(coverage="complete"),
            "scope_slot_2": _row(coverage=second_coverage, uncovered=("prompts/SYSTEM.md",))}
    result, ctx = _reduce(tmp_path, monkeypatch, rows, enforcement=enforcement)
    assert result.blocked is False and result.status == "responded"
    assert not result.block_message and not result.advisory_findings and not result.critical_findings
    assert result.context_manifest["scope_responded_count"] == 2
    assert result.context_manifest["scope_coverage_incomplete_count"] == (second_coverage == "incomplete")
    assert result.context_manifest["scope_degraded_reasons"] == []
    assert all(row["status"] == "responded" for row in ctx._last_scope_raw_results)
    diagnostic = result.context_manifest["scope_coverage_diagnostics"][1]
    assert diagnostic == {"slot_id": "scope_slot_2", "coverage": second_coverage,
                          "uncovered_sources": ["prompts/SYSTEM.md"] if second_coverage == "incomplete" else []}
    from ouroboros.tools.parallel_review import _scope_history_entry
    assert "Read coverage (diagnostic):" in _scope_history_entry(result)["summary"]


@pytest.mark.parametrize("enforcement", ["blocking", "advisory"])
@pytest.mark.parametrize("count", [1, 3])
def test_every_answer_counts_even_when_all_rows_have_incomplete_coverage(tmp_path, monkeypatch, enforcement, count):
    rows = {f"scope_slot_{i + 1}": _row(coverage="incomplete", uncovered=("ouroboros/safety.py",))
            for i in range(count)}
    result, ctx = _reduce(tmp_path, monkeypatch, rows, enforcement=enforcement)
    assert result.blocked is False and result.status == "responded"
    assert not result.block_message and not result.advisory_findings
    assert result.context_manifest["scope_responded_count"] == count
    assert result.context_manifest["scope_coverage_incomplete_count"] == count
    assert all(row["status"] == "responded" and row["failure_phase"] == ""
               for row in ctx._last_scope_raw_results)


@pytest.mark.parametrize("enforcement", ["blocking", "advisory"])
def test_incomplete_coverage_never_hides_substantive_critical_findings(tmp_path, monkeypatch, enforcement):
    rows = {"scope_slot_1": _row(coverage="complete"),
            "scope_slot_2": _row(coverage="complete"),
            "scope_slot_3": _row(coverage="incomplete", uncovered=("prompts/SYSTEM.md",))}
    finding = {"item": "cross_module_bugs", "severity": "critical", "verdict": "FAIL",
               "reason": "The producer and consumer use different units."}
    rows["scope_slot_3"].critical_findings = [finding]
    rows["scope_slot_3"].blocked = enforcement == "blocking"
    rows["scope_slot_3"].block_message = "Unit mismatch" if enforcement == "blocking" else ""
    result, _ctx = _reduce(tmp_path, monkeypatch, rows, enforcement=enforcement)
    assert result.blocked is (enforcement == "blocking")
    assert result.critical_findings == [finding]
    assert result.context_manifest["scope_responded_count"] == 3
    assert result.context_manifest["scope_coverage_incomplete_count"] == 1
    assert result.block_message == ("Unit mismatch" if enforcement == "blocking" else "")


def test_coverage_diagnostics_do_not_become_technical_failures(tmp_path):
    from ouroboros.tools.commit_gate import review_failure_is_technical

    assert not review_failure_is_technical({"failure_phase": "coverage_authority"})
    assert not review_failure_is_technical(
        {"failure_phase": "coverage_authority", "operation_state": "in_flight"})
    assert review_failure_is_technical({"failure_phase": "delivery"})


@pytest.mark.parametrize("coverage", ["complete", "incomplete"])
def test_read_diagnostics_do_not_hide_a_missing_reviewer_answer(tmp_path, monkeypatch, coverage):
    rows = {"scope_slot_1": _row(coverage=coverage),
            "scope_slot_2": _row(status="error", coverage="unobserved")}
    result, ctx = _reduce(tmp_path, monkeypatch, rows)
    assert result.blocked is True
    assert "SCOPE_QUORUM_NOT_MET" in result.block_message
    assert result.context_manifest["scope_responded_count"] == 1
    assert ctx._last_scope_raw_results[1]["status"] == "error"


def test_the_review_contract_fingerprint_binds_the_scope_delivery_class(monkeypatch):
    """Recorded free-replay authority must not survive this contract change: the
    delivery class, the retrieving output contract and the manifest policy are
    all hashed into the commit gate's contract identity."""
    import ouroboros.review_substrate as substrate
    from ouroboros.review_records import ReviewRouteKind, ReviewSlot
    from ouroboros.tools.commit_gate import commit_review_contract_fingerprint

    retrieving = ReviewSlot(slot_id="scope_slot_1", model="m",
                            route=ReviewRouteKind.API_CHAT, native_retrieval_override=True)
    packet = ReviewSlot(slot_id="scope_slot_1", model="m", route=ReviewRouteKind.API_CHAT)
    monkeypatch.setattr(substrate, "scope_reviewer_slots", lambda *_a, **_k: [retrieving])
    baseline = commit_review_contract_fingerprint()
    assert baseline

    # The SAME row identity with a different delivery class is a different contract.
    monkeypatch.setattr(substrate, "scope_reviewer_slots", lambda *_a, **_k: [packet])
    assert commit_review_contract_fingerprint() != baseline

    monkeypatch.setattr(substrate, "scope_reviewer_slots", lambda *_a, **_k: [retrieving])
    assert commit_review_contract_fingerprint() == baseline
    with monkeypatch.context() as patched:
        patched.setattr("ouroboros.tools.scope_required_sources.SCOPE_REQUIRED_SOURCES_POLICY",
                        "v-next")
        assert commit_review_contract_fingerprint() != baseline
    with monkeypatch.context() as patched:
        patched.setattr("ouroboros.tools.scope_review.SCOPE_RETRIEVING_OUTPUT_CONTRACT",
                        "a different retrieving contract")
        assert commit_review_contract_fingerprint() != baseline


def test_a_stored_bare_scope_row_discloses_its_migration_once_per_install(tmp_path, monkeypatch):
    """A row saved before the retrieving delivery changes what it spends and how
    long it takes, so the change is announced in the durable event stream — once."""
    import ouroboros.tools.scope_review as scope_mod
    from ouroboros import config as cfg
    from ouroboros.tools import review_admission
    from ouroboros.tools.registry import ToolContext

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", str(data))
    repo = _staged_protected_repo(tmp_path)
    monkeypatch.setattr(scope_mod, "_call_scope_llm", lambda *_a, **_k: ("", None, ""))
    ctx = ToolContext(repo_dir=repo, drive_root=data)
    ctx.pending_events = []

    for _ in range(2):
        scope_mod.run_scope_review(ctx, "amend the prompt", slot_id="scope_slot_1",
                                   scope_model="fixture/model")
    events = [e for e in ctx.pending_events
              if e.get("type") == review_admission.SCOPE_DELIVERY_MIGRATION_EVENT]
    assert len(events) == 1, ctx.pending_events
    assert events[0]["slot_id"] == "scope_slot_1"
    assert events[0]["model"] == "fixture/model"
    assert events[0]["delivery"] == "native_retrieval"
    marker = data / "state" / review_admission.SCOPE_DELIVERY_MIGRATION_FILENAME
    assert json.loads(marker.read_text(encoding="utf-8"))["slot_id"] == "scope_slot_1"


def test_a_bare_api_scope_seat_is_priced_as_its_native_first_send(tmp_path):
    """Wave admission must price what the row SENDS: a bare api scope row opens a
    native inspection episode, so its seat is the episode's first send (work
    order plus tool schemas), never a packet message pair it never assembles."""
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.review_native_episode import native_first_send_chars
    from ouroboros.review_records import ReviewSlot
    from ouroboros.reviewer_slot_config import SCOPE_ROLE_HINT
    from ouroboros.tools.review_admission import commit_gate_paid_seats
    from ouroboros.tools.scope_review import SCOPE_RETRIEVING_OUTPUT_CONTRACT

    repo = _repo(tmp_path)
    slot = ReviewSlot(slot_id="scope_slot_1", model="api/model",
                      route=ReviewRouteKind.API_CHAT, native_retrieval_override=True)
    prepared = {"scope_model_id": "api/model", "prompt": "", "stable_prefix_len": 0,
                "session_task": "BRIEF", "repo_dir": str(repo), "retrieves": True}
    seats = commit_gate_paid_seats(None, True, [{"slot": slot, "prepared": prepared, "final": None}])
    assert seats[0]["prompt_chars"] == native_first_send_chars(
        str(repo), surface="scope_review", role_hint=SCOPE_ROLE_HINT,
        slot_id="scope_slot_1", session_task="BRIEF",
        output_contract=SCOPE_RETRIEVING_OUTPUT_CONTRACT)
