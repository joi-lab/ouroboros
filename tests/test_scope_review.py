"""Tests for the review stack: scope review, review_helpers, enriched triad.

Verifies:
- Checklist section loader extracts exact sections
- Goal/scope precedence: goal > scope > commit_message > fallback
- Touched-file pack builds correctly (the triad packet's own evidence)
- Scope review module structure, dispatch, parsing and the output contract
- Reviewer-window sizing and its provenance wording
- Path-aware freshness
- Stale marking lifecycle
- repo_commit doesn't bypass the new stack
- review_helpers imports cleanly (no circular deps)
"""

import importlib
import inspect
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from ouroboros.reviewer_window import ReviewerWindow

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_module(name):
    sys.path.insert(0, REPO)
    return importlib.import_module(name)


def test_review_thoroughness_is_count_free_and_evidence_bound():
    helpers = _get_module("ouroboros.tools.review_helpers")
    block = helpers.REVIEW_THOROUGHNESS_BLOCK

    assert "5 bugs" not in block
    assert "zero, one, or many findings are all valid" in block
    assert "Never invent a finding to increase the count" in block


def test_scope_review_uses_active_subject_and_system_governance(tmp_path, monkeypatch):
    mod = _get_module("ouroboros.tools.scope_review")
    registry = _get_module("ouroboros.tools.registry")
    governance = tmp_path / "system"
    subject = tmp_path / "subject"
    drive = tmp_path / "data"
    governance.mkdir()
    subject.mkdir()
    drive.mkdir()
    captured = {}

    session_mod = _get_module("ouroboros.tools.scope_review_session")

    def fake_build(repo_dir, brief):
        captured["subject"] = pathlib.Path(repo_dir)
        captured["governance"] = pathlib.Path(brief.governance_repo_dir)
        return "brief", dict(native_required_sources=brief.required_sources, native_required_sources_ref=brief.required_sources_ref)

    monkeypatch.setattr(session_mod, "build_scope_session_task", fake_build)
    monkeypatch.setattr(mod, "_call_scope_llm", lambda *_a, **_k: ("", None, ""))
    ctx = registry.ToolContext(
        repo_dir=governance,
        system_repo_dir=governance,
        workspace_root=subject,
        workspace_mode="external",
        drive_root=drive,
    )

    mod.run_scope_review(ctx, "review external subject", scope_model="test-scope")

    assert captured == {
        "subject": subject.resolve(),
        "governance": governance.resolve(),
    }


def test_scope_review_refuses_ambiguous_workspace_root(tmp_path):
    mod = _get_module("ouroboros.tools.scope_review")
    registry = _get_module("ouroboros.tools.registry")
    system = tmp_path / "system"
    subject = tmp_path / "subject"
    drive = tmp_path / "data"
    system.mkdir()
    subject.mkdir()
    drive.mkdir()
    ctx = registry.ToolContext(
        repo_dir=system,
        system_repo_dir=system,
        workspace_root=subject,
        workspace_mode="",
        drive_root=drive,
    )

    result = mod.run_scope_review(ctx, "must not inspect the wrong repo")

    assert result.blocked is True
    assert result.status == "error"
    assert "workspace_root is set without workspace_mode" in result.block_message


def test_managed_resolver_subject_reaches_the_retrieving_brief(tmp_path, monkeypatch):
    """The managed REVIEW SUBJECT (predicate + authorized tx artifact) must reach
    the reviewer's brief, where it inlines the authoritative resolution delta —
    a scope row that re-derived `git diff --cached` itself would review the whole
    two-parent candidate instead of the resolver's work."""
    mod = _get_module("ouroboros.tools.scope_review")
    registry = _get_module("ouroboros.tools.registry")
    admission = _get_module("ouroboros.tools.review_admission")
    subject_mod = _get_module("ouroboros.tools.review_subject")
    session_mod = _get_module("ouroboros.tools.scope_review_session")
    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir()
    drive.mkdir()
    captured = {}

    def fake_build(_repo_dir, brief):
        captured["managed_subject"] = brief.managed_subject
        return "brief", dict(native_required_sources=brief.required_sources, native_required_sources_ref=brief.required_sources_ref)

    monkeypatch.setattr(session_mod, "build_scope_session_task", fake_build)
    monkeypatch.setattr(mod, "_call_scope_llm", lambda *_a, **_k: ("", None, ""))
    fake_subject = object()
    monkeypatch.setattr(
        subject_mod, "managed_review_subject", lambda _ctx, _repo: fake_subject
    )
    assert admission  # the prepare half resolves the seams patched above
    ctx = registry.ToolContext(repo_dir=repo, drive_root=drive, task_id="resolver")

    result = mod.run_scope_review(ctx, "review assisted update", scope_model="test")

    assert result.status == "empty_response"  # the fixture transport answered nothing
    assert captured == {"managed_subject": fake_subject}


# ---------------------------------------------------------------------------
# review_helpers tests
# ---------------------------------------------------------------------------

class TestChecklistSectionLoader:
    def test_loads_repo_commit_section(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.load_checklist_section("Repo Commit Checklist")
        assert "## Repo Commit Checklist" in section
        assert "bible_compliance" in section
        # Must NOT contain scope checklist
        assert "Intent / Scope Review Checklist" not in section

    def test_loads_scope_section(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.load_checklist_section("Intent / Scope Review Checklist")
        assert "## Intent / Scope Review Checklist" in section
        assert "intent_alignment" in section
        # Must NOT contain repo commit checklist items
        assert "## Repo Commit Checklist" not in section

    def test_raises_on_missing_section(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        with pytest.raises(ValueError):
            mod.load_checklist_section("Nonexistent Section")


class TestGoalSection:
    def test_goal_section_has_source(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.build_goal_section(goal="fix bug", scope="", commit_message="msg")
        assert "Source: goal" in section
        assert "fix bug" in section

    def test_scope_section_empty_when_no_scope(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.build_scope_section()
        assert section == ""

    def test_scope_section_present_when_scope(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.build_scope_section(scope="only review.py")
        assert "only review.py" in section
        assert "IMPORTANT" in section


class TestTouchedFilePack:
    def test_reads_existing_files(self, tmp_path):
        (tmp_path / "a.py").write_text("print('hello')", encoding="utf-8", newline="\n")
        (tmp_path / "b.md").write_text("# readme", encoding="utf-8", newline="\n")
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["a.py", "b.md"])
        assert "a.py" in pack
        assert "print('hello')" in pack
        assert "b.md" in pack
        assert omitted == []

    def test_skips_binary_files(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["image.png"])
        assert "image.png" in omitted
        assert "```" not in pack or "image.png" not in pack.split("```")[1] if "```" in pack else True

    def test_represents_binary_with_exact_git_metadata(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@ouroboros"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "TestBot"],
            cwd=str(tmp_path), check=True,
        )
        binary = tmp_path / "native.so"
        binary.write_bytes(b"old\x00payload")
        subprocess.run(["git", "add", "-f", "native.so"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(tmp_path), check=True)
        binary.write_bytes(b"new\x00payload")
        subprocess.run(["git", "add", "native.so"], cwd=str(tmp_path), check=True)

        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(
            tmp_path, ["native.so"], represent_binary=True
        )

        assert omitted == []
        assert "staged blob" in pack
        assert "pre-merge HEAD blob" in pack
        assert "official MERGE_HEAD blob" in pack
        assert "unknown" not in pack

    def test_binary_metadata_without_stage_zero_stays_omitted(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        (tmp_path / "native.so").write_bytes(b"unstaged\x00payload")

        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(
            tmp_path, ["native.so"], represent_binary=True
        )

        assert omitted == ["native.so"]
        assert "no readable stage-0" in pack

    def test_staged_binary_deletion_has_exact_parent_metadata(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@ouroboros"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "TestBot"], cwd=str(tmp_path), check=True)
        binary = tmp_path / "logo.png"
        binary.write_bytes(b"png\x00payload")
        subprocess.run(["git", "add", "logo.png"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "rm", "logo.png"], cwd=str(tmp_path), check=True)

        helpers = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = helpers.build_touched_file_pack(
            tmp_path, ["logo.png"], represent_binary=True
        )
        assert omitted == []
        assert "staged blob: `absent (deletion)`" in pack
        assert "pre-merge HEAD:" in pack

    def test_extensionless_binary_deletion_has_exact_parent_metadata(self, tmp_path):
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@ouroboros"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "TestBot"], cwd=str(tmp_path), check=True)
        binary = tmp_path / "firmware"
        binary.write_bytes(b"firmware\x00payload")
        subprocess.run(["git", "add", "firmware"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "rm", "firmware"], cwd=str(tmp_path), check=True)

        helpers = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = helpers.build_touched_file_pack(
            tmp_path, ["firmware"], represent_binary=True
        )
        assert omitted == []
        assert "staged blob: `absent (deletion)`" in pack
        assert "pre-merge HEAD:" in pack

    def test_omits_large_files(self, tmp_path):
        # _FILE_SIZE_LIMIT is now 1MB; write a file slightly above that threshold
        (tmp_path / "huge.py").write_bytes(b"x" * (1_048_576 + 1))
        mod = _get_module("ouroboros.tools.review_helpers")
        pack, omitted = mod.build_touched_file_pack(tmp_path, ["huge.py"])
        assert "huge.py" in omitted
        assert "omitted" in pack.lower()




# ---------------------------------------------------------------------------
# Scope review module tests
# ---------------------------------------------------------------------------



def test_scope_history_keeps_all_rounds_and_structured_ids():
    mod = _get_module("ouroboros.tools.scope_review")
    history = [
        {
            "attempt": idx,
            "critical": [{
                "item": f"bug_{idx}",
                "severity": "critical",
                "reason": f"bug {idx}",
                "obligation_id": f"obl-00{idx}",
            }],
            "advisory": [{
                "item": f"advice_{idx}",
                "severity": "advisory",
                "reason": f"advice {idx}",
            }],
        }
        for idx in range(1, 5)
    ]
    out = mod._build_review_history_section(history, open_obligations=None)
    assert "Round 1" in out
    assert "Round 4" in out
    assert "⚠️ OMISSION NOTE" not in out
    assert "obligation=obl-001" in out


class TestRunScopeReviewFailClosed:
    """End-to-end fail-closed tests that execute run_scope_review()."""






    def test_sub_floor_windows_get_scaled_output_reserves(self):
        """Provider Independence: the absolute 1M reserves must not swallow a
        small window whole. A 131K route asks for a fraction of its window as
        output; a >=1M window keeps the absolute reserves unchanged."""
        mod = _get_module("ouroboros.tools.scope_review")

        out, margin = mod._window_scaled_reserves(131_072)
        assert out == 32_768 and margin == 16_384
        assert mod._window_scaled_reserves(1_000_000) == (
            mod._SCOPE_MAX_TOKENS, mod._SCOPE_OUTPUT_MARGIN_TOKENS
        )



    def test_run_scope_review_blocks_incomplete_scope_matrix(self, tmp_path, monkeypatch):
        """A parseable but incomplete scope checklist is a reviewer failure."""
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-contract-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        raw = json.dumps([
            {
                "item": "intent_alignment",
                "verdict": "PASS",
                "severity": "advisory",
                "reason": "Checked the staged intent against the changed review gate path.",
            }
        ])
        monkeypatch.setattr(
            mod,
            "_call_scope_llm",
            lambda *a, **k: (raw, {
                "prompt_tokens": 10, "completion_tokens": 5,
                "operation_id": "review-op", "operation_state": "late_settled",
            }, None),
        )

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="test-scope")

        assert result.blocked is True
        assert result.status == "parse_failure"
        assert "missing required items" in result.block_message
        assert result.parsed_items[0]["item"] == "intent_alignment"
        assert result.operation_id == "review-op"
        assert result.operation_state == "late_settled"

    def test_run_scope_review_blocks_bare_pass_and_invalid_severity(self, tmp_path, monkeypatch):
        """Scope output contract rejects weak PASS reasons and bad severities."""
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-contract-negative-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        raw_items = [
            {
                "item": item_id,
                "verdict": "PASS",
                "severity": "advisory",
                "reason": f"Checked {item_id} against the staged review-gate fixture.",
            }
            for item_id in sorted(mod._SCOPE_REQUIRED_ITEMS)
        ]
        raw_items[0]["reason"] = "PASS"
        raw_items[1]["severity"] = "blocker"
        # FAIL without severity stays fail-closed (severity decides blocking);
        # PASS without severity is deliberately legal (defaulted to advisory).
        raw_items[2]["verdict"] = "FAIL"
        raw_items[2].pop("severity")
        monkeypatch.setattr(
            mod,
            "_call_scope_llm",
            lambda *a, **k: (json.dumps(raw_items), {"prompt_tokens": 10, "completion_tokens": 5}, None),
        )

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="test-scope")

        assert result.blocked is True
        assert result.status == "parse_failure"
        assert "PASS reason is too terse" in result.block_message
        assert "missing or invalid severity 'blocker'" in result.block_message
        assert "missing or invalid severity ''" in result.block_message

    @pytest.mark.parametrize("crit_item", sorted(_get_module("ouroboros.tools.scope_review")._SCOPE_REQUIRED_ITEMS))
    def test_advisory_downgrades_every_scope_critical_item(self, crit_item, tmp_path, monkeypatch):
        """NW-2 guardrail (58a52c4 class): under owner-chosen advisory enforcement,
        a critical scope finding for ANY required item must NOT block.

        ``forgotten_touchpoints`` is the scope-side item the 58a52c4 incident
        hardcoded to always block. The only pre-existing advisory-mode scope
        test hand-built a ScopeReviewResult and never exercised the real
        enforcement branch at run_scope_review level; this parametrization runs
        the real branch for every required item with a complete matrix (one
        critical FAIL + the rest valid PASS) so a per-item always-block hardcode
        fails here.
        """
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-advisory-guardrail-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        raw_items = []
        for item_id in sorted(mod._SCOPE_REQUIRED_ITEMS):
            if item_id == crit_item:
                raw_items.append({
                    "item": item_id,
                    "verdict": "FAIL",
                    "severity": "critical",
                    "reason": f"Staged diff violates {item_id} per the review-gate fixture.",
                })
            else:
                raw_items.append({
                    "item": item_id,
                    "verdict": "PASS",
                    "severity": "advisory",
                    "reason": f"Checked {item_id} against the staged review-gate fixture.",
                })
        monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "advisory")
        monkeypatch.setattr(
            mod,
            "_call_scope_llm",
            lambda *a, **k: (json.dumps(raw_items), {"prompt_tokens": 10, "completion_tokens": 5}, None),
        )
        # Treat the fake reviewer as >=1M so this test isolates the ADVISORY-ENFORCEMENT
        # downgrade (its target) from the separate sub-floor downgrade: an off-default
        # model with no Capability Evidence now fail-closes to the sub-floor (v6.46.0 fix),
        # which would empty critical_findings via the sub-floor path instead.
        monkeypatch.setattr(mod, "_scope_window",
                            lambda m, **_k: ReviewerWindow(1_000_000, "confirmed"))

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="test-scope")

        assert result.blocked is False, (
            f"advisory mode must NOT block critical scope item {crit_item!r}; "
            "a per-item always-block hardcode (58a52c4 class) would fail here"
        )
        assert result.status == "responded"
        assert any(f.get("item") == crit_item for f in result.critical_findings)


    def test_small_window_scope_reviewer_keeps_blocking_authority(self, tmp_path, monkeypatch):
        """BIBLE P3 as amended: window size is not a condition of authority. A
        scope reviewer on a 200K route retrieves the surface it needs across
        successive working views, so its critical findings gate the commit
        exactly as a 1M reviewer's do — the former sub-floor downgrade is gone."""
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-sub-floor-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        raw_items = []
        for item_id in sorted(mod._SCOPE_REQUIRED_ITEMS):
            if item_id == "intent_alignment":
                raw_items.append({
                    "item": item_id,
                    "verdict": "FAIL",
                    "severity": "critical",
                    "reason": "Staged diff contradicts the declared intent per the fixture.",
                })
            else:
                raw_items.append({
                    "item": item_id,
                    "verdict": "PASS",
                    "severity": "advisory",
                    "reason": f"Checked {item_id} against the staged review-gate fixture.",
                })
        monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
        monkeypatch.setattr(
            mod,
            "_call_scope_llm",
            lambda *a, **k: (json.dumps(raw_items), {"prompt_tokens": 10, "completion_tokens": 5}, None),
        )

        # Capability Evidence sources the reviewer window (no static table, v6.33.0):
        # treat opus-4.8 / gigachat as KNOWN sub-floor (<1M), fable-5 as >=1M.
        monkeypatch.setattr(
            mod, "_scope_window",
            lambda m, **_k: ReviewerWindow(
                200_000 if ("opus" in str(m) or "gigachat" in str(m).lower()) else 1_000_000,
                "confirmed",
            ),
        )

        result = mod.run_scope_review(
            MockCtx(), "test commit", scope_model="anthropic/claude-opus-4.8"
        )
        assert result.blocked is True
        assert result.status == "responded"
        assert any(f.get("item") == "intent_alignment" for f in result.critical_findings)
        assert not any(f.get("item") == "scope_review_sub_floor" for f in result.advisory_findings)

        # GigaChat direct-provider form — a small window, the same authority.
        result_giga = mod.run_scope_review(
            MockCtx(), "test commit", scope_model="gigachat::GigaChat-3-Ultra"
        )
        assert result_giga.blocked is True
        assert result_giga.status == "responded"
        assert any(f.get("item") == "intent_alignment" for f in result_giga.critical_findings)

        # The 1M reviewer (fable-5 pin) behaves identically.
        result_full = mod.run_scope_review(
            MockCtx(), "test commit", scope_model="anthropic/claude-fable-5"
        )
        assert result_full.blocked is True
        assert any(f.get("item") == "intent_alignment" for f in result_full.critical_findings)

    def test_clean_small_window_pass_is_an_authoritative_verdict(self, tmp_path, monkeypatch):
        """A clean response from a small-window retrieving reviewer IS the
        authoritative verdict: authority rests on the required-source manifest
        and the recorded receipts, not on the size of the working view."""
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-clean-sub-floor-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        clean_items = [
            {
                "item": item_id,
                "verdict": "PASS",
                "severity": "advisory",
                "reason": f"Checked {item_id} against the complete staged evidence.",
            }
            for item_id in sorted(mod._SCOPE_REQUIRED_ITEMS)
        ]
        monkeypatch.setattr(mod, "_scope_window",
                            lambda _m, **_k: ReviewerWindow(200_000, "confirmed"))
        monkeypatch.setattr(
            mod,
            "_call_scope_llm",
            lambda *a, **k: (
                json.dumps(clean_items),
                {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.02},
                None,
            ),
        )

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="unknown/reviewer")

        assert result.blocked is False
        assert result.status == "responded"
        assert result.cost_usd == 0.02
        assert not any(f.get("item") == "scope_review_sub_floor" for f in result.advisory_findings)



    def test_generic_provider_error_stays_fail_closed(self, tmp_path, monkeypatch):
        """B3 guard: non-oversize provider errors keep blocking (fail-closed)."""
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-generic-error-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        generic_error = (
            "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer (test-scope) failed — commit blocked.\n"
            "Error: APIConnectionError: Connection error.\n"
            "Retry the commit, or check API key and network connectivity."
        )
        monkeypatch.setattr(mod, "_call_scope_llm", lambda *a, **k: ("", None, generic_error))

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="test-scope")

        assert result.blocked is True
        assert result.status == "error"


    def test_gateway_provider_error_400_non_oversize_stays_fail_closed(self, tmp_path, monkeypatch):
        """F2 guard: a NON-size gateway 400 (auth/param/policy — same code, same empty
        body) must NOT downgrade. No size evidence (small prompt, large window) keeps the
        fail-closed empty_response block, so a misconfiguration never silently skips the
        blocking scope review."""
        mod = _get_module("ouroboros.tools.scope_review")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-gateway-auth400-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        monkeypatch.setattr(
            mod, "_call_scope_llm",
            lambda *a, **k: ("", {"prompt_tokens": 0, "completion_tokens": 0,
                                  "provider_error": {"code": 400, "kind": "provider_error", "message": "invalid api key"}}, ""),
        )
        # Even a large prompt near the resolved window must stay fail-closed when the
        # provider gives a concrete non-size message. Size proximity is reserved for
        # opaque/empty gateway 400 bodies.

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="test-scope")

        assert result.blocked is True
        assert result.status == "empty_response"


    def test_run_scope_review_preserves_pass_rows_in_actor_record(self, tmp_path, monkeypatch):
        """scope_raw_result.parsed_items must keep PASS rows for audit coverage."""
        mod = _get_module("ouroboros.tools.scope_review")
        helpers = _get_module("ouroboros.tools.review_helpers")

        class MockCtx:
            repo_dir = str(tmp_path)
            task_id = "scope-pass-audit-test"
            pending_events = []

            def drive_logs(self):
                return tmp_path

        raw_items = [
            {
                "item": item_id,
                "verdict": "PASS",
                "severity": "advisory",
                "reason": f"Checked {item_id} against the staged review-gate fixture.",
            }
            for item_id in sorted(mod._SCOPE_REQUIRED_ITEMS)
        ]
        monkeypatch.setattr(
            mod,
            "_call_scope_llm",
            lambda *a, **k: (json.dumps(raw_items), {"prompt_tokens": 10, "completion_tokens": 5}, None),
        )
        monkeypatch.setattr(mod, "_scope_window",
                            lambda _m, **_k: ReviewerWindow(1_000_000, "confirmed"))

        result = mod.run_scope_review(MockCtx(), "test commit", scope_model="test-scope")
        record = helpers.build_scope_actor_record(result, fallback_model_id="fallback-scope")

        assert result.blocked is False
        assert result.critical_findings == []
        assert result.advisory_findings == []
        assert len(result.parsed_items) == len(mod._SCOPE_REQUIRED_ITEMS)
        assert record["parsed_items"] == result.parsed_items
        assert {item["verdict"] for item in record["parsed_items"]} == {"PASS"}


class TestScopeReviewModule:
    # test_scope_review_imports removed in v5.15.x — pure callable-existence
    # check. The fail-closed test below already imports the module, and the
    # behavioral integration tests exercise run_scope_review end-to-end.

    def test_scope_review_fail_closed_design(self):
        """run_scope_review must be fail-closed: errors return blocking strings."""
        mod = _get_module("ouroboros.tools.scope_review")
        source = inspect.getsource(mod.run_scope_review)
        assert "SCOPE_REVIEW_BLOCKED" in source
        assert "fail" in source.lower() or "block" in source.lower()

    def test_scope_review_default_is_terra(self):
        mod = _get_module("ouroboros.tools.scope_review")
        assert "gpt-5.6-terra" in mod._SCOPE_MODEL_DEFAULT
        # Verify the getter returns the shipped default when no override env var is set
        import os
        if not os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL"):
            assert "gpt-5.6-terra" in mod._get_scope_model()
        # else: env override is active — default check not applicable in this env

    def test_scope_review_model_configurable_via_env(self):
        """OUROBOROS_SCOPE_REVIEW_MODEL env overrides the default."""
        mod = _get_module("ouroboros.tools.scope_review")
        import os
        old = os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL")
        old_plural = os.environ.get("OUROBOROS_SCOPE_REVIEW_MODELS")
        try:
            os.environ.pop("OUROBOROS_SCOPE_REVIEW_MODELS", None)
            os.environ["OUROBOROS_SCOPE_REVIEW_MODEL"] = "google/gemini-2.5-pro"
            assert mod._get_scope_model() == "google/gemini-2.5-pro"
        finally:
            if old is None:
                os.environ.pop("OUROBOROS_SCOPE_REVIEW_MODEL", None)
            else:
                os.environ["OUROBOROS_SCOPE_REVIEW_MODEL"] = old
            if old_plural is None:
                os.environ.pop("OUROBOROS_SCOPE_REVIEW_MODELS", None)
            else:
                os.environ["OUROBOROS_SCOPE_REVIEW_MODELS"] = old_plural

    def test_scope_review_effort_configurable(self):
        """OUROBOROS_EFFORT_SCOPE_REVIEW should resolve via resolve_effort."""
        from ouroboros.config import resolve_effort
        import os
        old = os.environ.get("OUROBOROS_EFFORT_SCOPE_REVIEW")
        try:
            os.environ["OUROBOROS_EFFORT_SCOPE_REVIEW"] = "low"
            assert resolve_effort("scope_review") == "low"
            assert resolve_effort("scope-review") == "low"
        finally:
            if old is None:
                os.environ.pop("OUROBOROS_EFFORT_SCOPE_REVIEW", None)
            else:
                os.environ["OUROBOROS_EFFORT_SCOPE_REVIEW"] = old

    def test_scope_brief_includes_scope_checklist(self):
        """The brief builder must load the scope checklist, not the repo checklist."""
        session = _get_module("ouroboros.tools.scope_review_session")
        assert session.SCOPE_CHECKLIST_SECTION == "Intent / Scope Review Checklist"
        source = inspect.getsource(session.build_scope_session_task)
        assert "load_checklist_section(SCOPE_CHECKLIST_SECTION)" in source





# ---------------------------------------------------------------------------
# review_state path-aware freshness
# ---------------------------------------------------------------------------

class TestPathAwareFreshness:
    def test_snapshot_hash_stable_without_message(self, tmp_path):
        """Snapshot hash should NOT change when only commit_message changes."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        rs = _get_module("ouroboros.review_state")
        h1 = rs.compute_snapshot_hash(tmp_path, "message A")
        h2 = rs.compute_snapshot_hash(tmp_path, "message B")
        # Hash now based on code only — should be SAME for different messages
        assert h1 == h2

    def test_snapshot_hash_changes_with_file_content(self, tmp_path):
        """Snapshot hash must change when file content changes."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "file.py").write_text("v1", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "file.py"], cwd=str(tmp_path), capture_output=True)
        rs = _get_module("ouroboros.review_state")
        h1 = rs.compute_snapshot_hash(tmp_path, "msg")
        # Modify file
        (tmp_path / "file.py").write_text("v2", encoding="utf-8", newline="\n")
        h2 = rs.compute_snapshot_hash(tmp_path, "msg")
        assert h1 != h2

    def test_path_scoped_hash(self, tmp_path):
        """When paths= is provided, only those files affect the hash."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "a.py").write_text("aaa", encoding="utf-8", newline="\n")
        (tmp_path / "b.py").write_text("bbb", encoding="utf-8", newline="\n")
        rs = _get_module("ouroboros.review_state")
        h_a = rs.compute_snapshot_hash(tmp_path, paths=["a.py"])
        h_b = rs.compute_snapshot_hash(tmp_path, paths=["b.py"])
        assert h_a != h_b

    def test_stale_lifecycle(self):
        """add_run marks previous non-matching fresh runs as stale."""
        rs = _get_module("ouroboros.review_state")
        state = rs.AdvisoryReviewState()
        run1 = rs.AdvisoryRunRecord(
            snapshot_hash="hash1", commit_message="m1",
            status="fresh", ts="2026-01-01T00:00:00",
        )
        state.add_run(run1)
        assert state.advisory_runs[0].status == "fresh"

        run2 = rs.AdvisoryRunRecord(
            snapshot_hash="hash2", commit_message="m2",
            status="fresh", ts="2026-01-01T01:00:00",
        )
        state.add_run(run2)
        assert state.advisory_runs[0].status == "stale"  # hash1 became stale
        assert state.advisory_runs[1].status == "fresh"   # hash2 is fresh


# ---------------------------------------------------------------------------
# Triad review enrichment
# ---------------------------------------------------------------------------

class TestTriadReviewEnriched:
    def test_triad_prompt_has_touched_files_placeholder(self):
        """The dynamic review prompt template must include current_files_section."""
        mod = _get_module("ouroboros.tools.review")
        assert "{current_files_section}" in mod._REVIEW_PROMPT_TEMPLATE_DYNAMIC

    def test_triad_prompt_has_goal_section(self):
        """The dynamic review prompt template must include goal_section (the
        per-commit tail; the stable prefix carries the cache marker)."""
        mod = _get_module("ouroboros.tools.review")
        assert "{goal_section}" in mod._REVIEW_PROMPT_TEMPLATE_DYNAMIC
        assert "{goal_section}" not in mod._REVIEW_PROMPT_TEMPLATE_STABLE

    def test_run_unified_review_accepts_goal_scope(self):
        """_run_unified_review must accept goal and scope keyword args."""
        mod = _get_module("ouroboros.tools.review")
        sig = inspect.signature(mod._run_unified_review)
        assert "goal" in sig.parameters
        assert "scope" in sig.parameters


# ---------------------------------------------------------------------------
# git.py wiring
# ---------------------------------------------------------------------------

class TestGitWiring:
    def test_repo_commit_schema_has_goal_scope(self):
        git = _get_module("ouroboros.tools.git")
        tools = git.get_tools()
        commit = next(t for t in tools if t.name == "commit_reviewed")
        props = commit.schema["parameters"]["properties"]
        assert "goal" in props
        assert "scope" in props

    def test_repo_commit_push_accepts_goal_scope(self):
        git = _get_module("ouroboros.tools.git")
        sig = inspect.signature(git._repo_commit_push)
        assert "goal" in sig.parameters
        assert "scope" in sig.parameters

    def test_scope_review_wired_in_commit(self):
        """The shared reviewed stage must call the parallel review helper."""
        git = _get_module("ouroboros.tools.git")
        source = inspect.getsource(git._run_reviewed_stage_cycle)
        assert "_run_parallel_review" in source
        # The parallel helper must contain both triad and scope review
        # (Q25-A two-phase contract: assembly, then dispatch).
        parallel_source = inspect.getsource(git._run_parallel_review)
        assert "_prepare_unified_review" in parallel_source
        assert "_dispatch_unified_review" in parallel_source
        # scope dispatch lives one seam deeper since the Q25-A split
        from ouroboros.tools import parallel_review as _pr
        assert "run_scope_review" in inspect.getsource(_pr._run_scope)
        # ThreadPoolExecutor must be used for parallel execution
        assert "ThreadPoolExecutor" in parallel_source

    def test_repo_commit_not_bypass_scope(self):
        """repo_commit must reach scope review via the shared stage helper."""
        git = _get_module("ouroboros.tools.git")
        source = inspect.getsource(git._repo_commit_push)
        assert "_run_reviewed_stage_cycle" in source
        shared_source = inspect.getsource(git._run_reviewed_stage_cycle)
        # The advisory-freshness check lives in the extracted gate helper the
        # stage cycle calls before any paid dispatch.
        assert "_advisory_and_tests_gate" in shared_source
        assert "_check_advisory_freshness" in inspect.getsource(git._advisory_and_tests_gate)
        assert "_run_parallel_review" in shared_source
        parallel_source = inspect.getsource(git._run_parallel_review)
        from ouroboros.tools import parallel_review as _pr
        assert "run_scope_review" in inspect.getsource(_pr._run_scope)
        assert "ThreadPoolExecutor" in parallel_source

    def test_parallel_execution_both_always_run(self):
        """SUPERSESSION (lane L-review, Q25=A): the retired contract was
        "both futures always submitted regardless of each other's result" — it
        let one side SPEND while the other failed assembly deterministically.
        The ratified ordering: BOTH packets are assembled first, and both
        dispatches are submitted to the pool only past the admission; each
        submission still precedes any result() collection."""
        git = _get_module("ouroboros.tools.git")
        source = inspect.getsource(git._run_parallel_review)
        prepare_triad = source.find("_prepare_unified_review(")
        prepare_scope = source.find("_prepare_scope_rows(")
        # Each submission runs under a copy of the admitting context (one fence
        # for admission and reservation); the anchors name the submitted seam.
        submit_triad = source.find("copy_context().run, _dispatch_unified_review")
        submit_scope = source.find("copy_context().run, _run_scope")
        result_triad = source.find("triad_fut.result()")
        result_scope = source.find("scope_fut.result()")
        for position in (prepare_triad, prepare_scope, submit_triad, submit_scope,
                         result_triad, result_scope):
            assert position > 0
        # Assembly of BOTH sides precedes ANY dispatch submission...
        assert prepare_triad < submit_triad and prepare_triad < submit_scope
        assert prepare_scope < submit_triad and prepare_scope < submit_scope
        # ...and both submissions precede their result() collection.
        assert submit_triad < result_triad
        assert submit_scope < result_scope

    def test_aggregated_verdict_both_blockers_shown(self):
        """When both triad and scope block, both messages must appear in combined output."""
        import types
        import unittest.mock as mock
        scope_mod = _get_module("ouroboros.tools.scope_review")
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        triad_error = "⚠️ REVIEW_BLOCKED: triad finding"
        scope_blocked = scope_mod.ScopeReviewResult(
            blocked=True,
            block_message="⚠️ SCOPE_REVIEW_BLOCKED: scope finding",
            critical_findings=[{"verdict": "FAIL", "item": "intent_alignment",
                                "severity": "critical", "reason": "scope blocked", "model": "test"}],
        )
        ctx = types.SimpleNamespace(
            repo_dir=None, _last_review_critical_findings=[], _review_advisory=[])
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            blocked, combined_msg, block_reason, findings, scope_adv = pr_mod.aggregate_review_verdict(
                triad_error, scope_blocked, "critical_findings", [], ctx,
                "test commit", 0.0, ctx.repo_dir)
        assert blocked
        assert "triad finding" in combined_msg
        assert "scope finding" in combined_msg
        assert "Both triad review AND scope review" in combined_msg
        assert len(findings) == 1

    def test_triad_advisory_included_when_scope_blocks(self):
        """When triad passes but has advisory findings and scope blocks, all findings appear."""
        import types
        import unittest.mock as mock
        scope_mod = _get_module("ouroboros.tools.scope_review")
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        scope_blocked = scope_mod.ScopeReviewResult(
            blocked=True,
            block_message="⚠️ SCOPE_REVIEW_BLOCKED: scope critical finding",
            critical_findings=[{"verdict": "FAIL", "item": "intent_alignment",
                                "severity": "critical", "reason": "scope blocked", "model": "test"}],
        )
        triad_advisory = [{"item": "context_building", "reason": "advisory note"}]
        ctx = types.SimpleNamespace(
            repo_dir=None, _last_review_critical_findings=[], _review_advisory=[])
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            blocked, combined_msg, block_reason, findings, scope_adv = pr_mod.aggregate_review_verdict(
                None, scope_blocked, "scope_blocked", triad_advisory, ctx,
                "test commit", 0.0, ctx.repo_dir)
        assert blocked
        assert "scope critical finding" in combined_msg
        assert "advisory note" in combined_msg
        assert len(findings) == 1

    def test_advisory_mode_scope_criticals_not_in_blocking_findings(self):
        """Advisory-mode scope critical findings must NOT be added to _combined_findings."""
        import types
        import unittest.mock as mock
        scope_mod = _get_module("ouroboros.tools.scope_review")
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        # Triad blocks; scope does NOT block but has critical findings (advisory enforcement)
        triad_error = "⚠️ REVIEW_BLOCKED: triad issue"
        scope_advisory_crit = scope_mod.ScopeReviewResult(
            blocked=False,  # advisory mode — not blocked
            block_message="",
            critical_findings=[{"verdict": "FAIL", "item": "intent_alignment",
                                "severity": "critical", "reason": "advisory-only scope note", "model": "test"}],
            advisory_findings=[],
        )
        ctx = types.SimpleNamespace(
            repo_dir=None, _last_review_critical_findings=[], _review_advisory=[])
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            blocked, combined_msg, block_reason, findings, scope_adv = pr_mod.aggregate_review_verdict(
                triad_error, scope_advisory_crit, "critical_findings", [], ctx,
                "test commit", 0.0, ctx.repo_dir)
        assert blocked
        # Advisory-mode scope criticals must NOT appear in durable blocking findings
        assert all(f.get("item") != "intent_alignment" for f in findings), \
            "Advisory-mode scope criticals must not be recorded as blocking findings"
        # But should appear in scope_advisory_items for visibility
        assert any(
            (isinstance(item, dict) and item.get("item") == "intent_alignment")
            or (isinstance(item, str) and "intent_alignment" in item)
            for item in scope_adv
        )

    def test_scope_advisory_visible_on_successful_commit(self):
        """Non-blocking scope advisory findings must be returned even when commit is not blocked."""
        import types
        import unittest.mock as mock
        scope_mod = _get_module("ouroboros.tools.scope_review")
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        # Scope passes (not blocked) but has advisory findings
        scope_advisory = scope_mod.ScopeReviewResult(
            blocked=False,
            block_message="",
            critical_findings=[],
            advisory_findings=[{"verdict": "PASS", "item": "architecture_fit",
                                "severity": "advisory", "reason": "minor concern", "model": "test"}],
        )
        ctx = types.SimpleNamespace(
            repo_dir=None, _last_review_critical_findings=[], _review_advisory=[])
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            blocked, combined_msg, block_reason, findings, scope_adv = pr_mod.aggregate_review_verdict(
                None, scope_advisory, "", [], ctx, "test commit", 0.0, ctx.repo_dir)
        # Should NOT block
        assert not blocked
        assert combined_msg is None
        # But scope advisory items must be returned for caller to surface
        assert len(scope_adv) > 0
        assert any(
            (isinstance(item, dict) and item.get("item") == "architecture_fit")
            or (isinstance(item, str) and "architecture_fit" in item)
            for item in scope_adv
        )

    @pytest.mark.parametrize("crit_item", sorted(_get_module("ouroboros.tools.scope_review")._SCOPE_REQUIRED_ITEMS))
    def test_aggregation_does_not_block_on_advisory_scope_criticals(self, crit_item):
        """NW-2 guardrail (aggregation seam): a 58a52c4-class hardcode could be
        re-introduced downstream in aggregate_review_verdict instead of in
        scope_review.py. With no triad error and a non-blocked scope result that
        merely CARRIES a critical finding (advisory pass-through), the aggregator
        must NOT flip to blocked for ANY item id.
        """
        import types
        import unittest.mock as mock
        scope_mod = _get_module("ouroboros.tools.scope_review")
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        scope_advisory_crit = scope_mod.ScopeReviewResult(
            blocked=False,
            block_message="",
            critical_findings=[{"verdict": "FAIL", "item": crit_item,
                                "severity": "critical", "reason": "advisory-only scope note", "model": "test"}],
            advisory_findings=[],
        )
        ctx = types.SimpleNamespace(
            repo_dir=None, _last_review_critical_findings=[], _review_advisory=[])
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            blocked, combined_msg, block_reason, findings, scope_adv = pr_mod.aggregate_review_verdict(
                None, scope_advisory_crit, "", [], ctx, "test commit", 0.0, ctx.repo_dir)
        assert not blocked, (
            f"aggregation must NOT block on an advisory-pass-through scope critical "
            f"for item {crit_item!r}; a per-item always-block hardcode would fail here"
        )
        assert combined_msg is None

    def test_scope_review_skipped_surfaces_through_aggregation_path(self):
        """Budget-skip advisories must survive aggregation and caller-side surfacing."""
        import types
        import unittest.mock as mock
        scope_mod = _get_module("ouroboros.tools.scope_review")
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        scope_advisory = scope_mod.ScopeReviewResult(
            blocked=False,
            block_message="",
            critical_findings=[],
            advisory_findings=[{
                "verdict": "FAIL",
                "item": "scope_review_skipped",
                "severity": "advisory",
                "reason": "⚠️ SCOPE_REVIEW_SKIPPED: Full scope-review prompt exceeds budget.",
                "model": "scope_reviewer",
            }],
        )
        ctx = types.SimpleNamespace(
            repo_dir=None, _last_review_critical_findings=[], _review_advisory=[])
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            blocked, combined_msg, block_reason, findings, scope_adv = pr_mod.aggregate_review_verdict(
                None, scope_advisory, "", [], ctx, "test commit", 0.0, ctx.repo_dir)

        if scope_adv:
            ctx._review_advisory.extend(scope_adv)

        assert not blocked
        assert combined_msg is None
        assert findings == []
        assert any(
            (isinstance(item, dict) and item.get("item") == "scope_review_skipped")
            or (isinstance(item, str) and "scope_review_skipped" in item)
            for item in scope_adv
        )
        assert any(
            (isinstance(item, dict) and item.get("item") == "scope_review_skipped")
            or (isinstance(item, str) and "scope_review_skipped" in item)
            for item in ctx._review_advisory
        )

    def test_triad_crash_resets_stale_findings(self):
        """If triad crashes, stale ctx findings from prior attempt must not bleed into current run."""
        import types
        import unittest.mock as mock
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        # Seed stale fields from a previous attempt
        ctx = types.SimpleNamespace(
            repo_dir=None,
            _last_review_block_reason="critical_findings",
            _last_review_critical_findings=[
                {"verdict": "FAIL", "item": "secrets_check", "severity": "critical",
                 "reason": "stale from prior run", "model": "old-model"}
            ],
            _review_advisory=[],
            _review_history=[],
            _scope_review_history={},
        )
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            with mock.patch("ouroboros.tools.review._run_unified_review",
                            side_effect=RuntimeError("triad crashed")):
                with mock.patch("ouroboros.tools.scope_review.run_scope_review") as mock_scope:
                    from ouroboros.tools.scope_review import ScopeReviewResult
                    mock_scope.return_value = ScopeReviewResult(blocked=False)
                    review_err, scope_result, triad_block_reason, _ = pr_mod.run_parallel_review(
                        ctx, "test commit")
        # Triad crash must yield infra_failure reason, not the stale critical_findings
        assert triad_block_reason == "infra_failure"
        # Stale findings must be cleared — no bleed-through to aggregate
        assert ctx._last_review_critical_findings == []
        assert "crashed" in review_err

    def test_scope_crash_resets_stale_actor_records(self):
        """If scope crashes, current raw evidence must not reuse previous scope actors."""
        import types
        import unittest.mock as mock
        pr_mod = _get_module("ouroboros.tools.parallel_review")

        ctx = types.SimpleNamespace(
            repo_dir=None,
            _last_review_block_reason="",
            _last_review_critical_findings=[],
            _review_advisory=[],
            _review_history=[],
            _scope_review_history={},
            _last_scope_raw_results=[
                {"slot_id": "stale", "model_id": "old-scope", "status": "responded"}
            ],
        )
        fake_slot = types.SimpleNamespace(
            model="new-scope", slot_id="scope_slot_1", route=None, effort="",
            session_target="", session_profile="")
        with mock.patch.object(pr_mod, "run_cmd", return_value=""):
            with mock.patch("ouroboros.tools.review._prepare_unified_review",
                            return_value=(None, None, True)):
                with mock.patch.object(
                        pr_mod, "_prepare_scope_rows",
                        return_value=[{"slot": fake_slot, "prepared": {"p": 1}, "final": None}]):
                    with mock.patch.object(pr_mod, "run_scope_review", side_effect=RuntimeError("scope crashed")):
                        review_err, scope_result, triad_block_reason, _ = pr_mod.run_parallel_review(
                            ctx, "test commit")

        assert review_err is None
        assert triad_block_reason == ""
        assert scope_result.blocked is True
        assert scope_result.status == "error"
        assert ctx._last_scope_raw_results
        assert ctx._last_scope_raw_results[0]["status"] == "error"
        assert ctx._last_scope_raw_results[0]["slot_id"] == "scope_slot_error"
        assert ctx._last_scope_raw_results[0]["model_id"] != "old-scope"
        assert ctx._last_scope_raw_result["raw_results"][0]["status"] == "error"

    def test_advisory_freshness_path_aware(self):
        """_check_advisory_freshness must accept paths parameter."""
        git = _get_module("ouroboros.tools.git")
        sig = inspect.signature(git._check_advisory_freshness)
        assert "paths" in sig.parameters


# ---------------------------------------------------------------------------
# HEAD snapshot section tests (Phase 3, item 5)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# LLM routing validation (Phase 3, item 6)
# ---------------------------------------------------------------------------

class TestSharedLLMRouting:
    def test_triad_review_uses_llm_client(self):
        """Triad review (_query_model) must use LLMClient, not ad-hoc HTTP."""
        mod = _get_module("ouroboros.tools.review")
        source = inspect.getsource(mod._query_model)
        assert "LLMClient" in source or "llm_client" in source.lower()
        # Must NOT use requests or httpx directly
        assert "requests.post" not in source
        assert "httpx" not in source

    def test_triad_emits_llm_usage_once_via_substrate(self):
        """Triad usage is emitted exactly ONCE, by the shared review substrate.

        The former job-level re-emit in _multi_model_review_async doubled every
        triad call in llm_usage telemetry and mislabelled a delegated session's
        provider: the substrate per-slot emission is the single source, the
        same shape scope review already received.
        """
        source = inspect.getsource(_get_module("ouroboros.tools.review"))
        assert 'source="review"' not in source  # no job-level re-emit
        substrate = inspect.getsource(_get_module("ouroboros.review_substrate"))
        assert 'source=f"review_substrate:{request.surface}"' in substrate
        helper = inspect.getsource(_get_module("ouroboros.tools.review_helpers").emit_review_usage)
        assert "llm_usage" in helper
        assert "emit_review_event" in helper

    def test_scope_review_uses_llm_client(self):
        """Scope review must use LLMClient for its model call.

        LLMClient is used in _call_scope_llm (called by run_scope_review),
        so we check the whole module for its presence rather than just
        the top-level run_scope_review function.
        """
        mod = _get_module("ouroboros.tools.scope_review")
        # LLMClient is instantiated in _call_scope_llm which run_scope_review delegates to
        source = inspect.getsource(mod._call_scope_llm)
        assert "LLMClient" in source

    def test_scope_review_emits_usage_once_via_substrate(self):
        """Scope usage is emitted exactly ONCE, by the shared review substrate.

        The former job-level re-emit in run_scope_review duplicated every scope
        call in llm_usage telemetry without ledger_attempt_ids (v6.69.0 dedup):
        the substrate per-slot emission is the single telemetry source.
        """
        mod = _get_module("ouroboros.tools.scope_review")
        source = inspect.getsource(mod)
        assert 'source="scope_review")' not in source  # no job-level re-emit
        substrate = inspect.getsource(_get_module("ouroboros.review_substrate"))
        assert 'source=f"review_substrate:{request.surface}"' in substrate
        helper = inspect.getsource(_get_module("ouroboros.tools.review_helpers").emit_review_usage)
        assert "llm_usage" in helper
        assert "emit_review_event" in helper


# ---------------------------------------------------------------------------
# Advisory schema enrichment
# ---------------------------------------------------------------------------

class TestAdvisorySchemaEnriched:
    def test_advisory_schema_has_goal_scope_paths(self):
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        tools = adv.get_tools()
        adv_tool = next(t for t in tools if t.name == "advisory_review")
        props = adv_tool.schema["parameters"]["properties"]
        assert "goal" in props
        assert "scope" in props
        assert "paths" in props

    def test_advisory_prompt_uses_section_loader(self):
        """Advisory prompt builder must use precise section loader, not full CHECKLISTS.md."""
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        source = inspect.getsource(adv._build_advisory_prompt)
        assert "load_checklist_section" in source

    def test_advisory_no_blind_truncation(self):
        """Advisory must not silently truncate raw_result."""
        adv = _get_module("ouroboros.tools.claude_advisory_review")
        source = inspect.getsource(adv._handle_advisory_pre_review)
        assert "raw_result[:4000]" not in source


class TestScopePromptMatrixContract:
    """v4.34.0: scope prompt requires full 8-item matrix + anti-pattern-lock guard.

    Regression-pins two behavioural contracts added in v4.34.0:
    (1) scope reviewer must emit one entry per Intent/Scope checklist item
        (not only FAILs as before), with mandatory PASS justification;
    (2) scope prompt carries an explicit Anti pattern-lock guard asking
        the reviewer to do a second focused pass on a different concern
        class without imposing a numeric finding quota.
    """

    def _get_scope_prompt(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "CHECKLISTS.md").write_text(
            "## Intent / Scope Review Checklist\n\nplaceholder\n", encoding="utf-8", newline="\n"
        )
        (tmp_path / "docs" / "DEVELOPMENT.md").write_text("dev guide\n", encoding="utf-8", newline="\n")
        (tmp_path / "a.py").write_text("aaa", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@o", "-c", "user.name=T", "commit", "-m", "init"],
            cwd=str(tmp_path), capture_output=True,
        )
        (tmp_path / "a.py").write_text("bbb", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        session = _get_module("ouroboros.tools.scope_review_session")
        brief, _manifest = session.build_scope_session_task(
            tmp_path, session.ScopeBriefInputs(commit_message="test"),
        )
        assert brief
        return brief

    def test_full_matrix_contract_is_present(self, tmp_path):
        """Scope prompt must require coverage for every checklist item."""
        prompt = self._get_scope_prompt(tmp_path)
        assert "cover every checklist item" in prompt
        assert "Skipping an item is not allowed" in prompt
        assert "multiple distinct concrete problems" in prompt

    def test_pass_justification_is_mandatory(self, tmp_path):
        """PASS entries must require 1-2 sentences of justification.

        Guard: without this, reviewers can return bare `PASS` for items
        they never actually reviewed, defeating the matrix contract.
        """
        prompt = self._get_scope_prompt(tmp_path)
        # Some form of mandatory justification language must be present.
        assert "stating WHY this item passes" in prompt
        # And the bare-PASS anti-pattern must be called out explicitly.
        assert "bare" in prompt.lower()
        assert "reviewer failure" in prompt.lower()

    def test_anti_pattern_lock_guard_is_present(self, tmp_path):
        """Scope prompt must carry the Anti pattern-lock guard section."""
        prompt = self._get_scope_prompt(tmp_path)
        assert "Anti pattern-lock guard" in prompt
        assert "exactly one FAIL" not in prompt
        # The guard must instruct a second pass on a different concern class.
        # Normalize whitespace before checking so a reflow of the prompt
        # wrapping doesn't break the contract.
        import re
        flat = re.sub(r"\s+", " ", prompt)
        assert "zero or one FAIL is valid" in flat
        assert "numeric finding quota" in flat
        assert "SECOND pass" in flat
        assert "DIFFERENT concern class" in flat

    def test_anti_pattern_lock_pairings_cover_checklist_items(self, tmp_path):
        """Concrete pairings must reference real Intent/Scope checklist item names.

        Without real item names the guidance is generic and models fall
        back to pattern-locking; the prompt has to name pairings by
        actual checklist identifiers.
        """
        prompt = self._get_scope_prompt(tmp_path)
        # At least the four most common concern classes must appear as
        # "if FAIL was in X, re-examine Y" pairings.
        for item in (
            "intent_alignment",
            "forgotten_touchpoints",
            "cross_surface_consistency",
            "regression_surface",
        ):
            assert item in prompt, f"Anti-pattern-lock pairing for `{item}` missing"


class TestTriadPromptAntiPatternLock:
    """v4.34.0: triad pre-commit review prompt now also carries the
    Anti pattern-lock guard. Scope and triad must stay symmetric so
    semantic breadth is guarded without pressuring either surface to invent findings.
    """

    def test_triad_template_has_anti_pattern_lock_guard(self):
        mod = _get_module("ouroboros.tools.review")
        tpl = mod._REVIEW_PROMPT_TEMPLATE_STABLE + mod._REVIEW_PROMPT_TEMPLATE_DYNAMIC
        assert "Anti pattern-lock guard" in tpl
        assert "exactly one FAIL" not in tpl
        guard = mod.REPO_ANTI_PATTERN_LOCK_GUARD
        # Normalize whitespace so prompt reflow doesn't break the contract.
        import re
        flat = re.sub(r"\s+", " ", f"{tpl}\n{guard}")
        assert "zero or one FAIL is valid" in flat
        assert "numeric finding quota" in flat
        # Accept any casing — "different concern class" / "DIFFERENT concern class"
        assert "concern class" in flat.lower()
        assert "second pass" in flat.lower()


def test_scope_reviewer_window_sizes_down_on_absent_evidence(monkeypatch, tmp_path):
    """claudexor B4 + v6.46.0 false-1M fix: with NO capability evidence an OFF-DEFAULT
    reviewer (e.g. an OUROBOROS_SCOPE_REVIEW_MODEL pin) sizes down to the conservative
    fallback instead of asking a 200K model for a 1M-calibrated output reserve. The
    SHIPPED designated reviewer keeps the full-window sentinel as a SIZE. Neither
    number decides authority — that rests on the required-source manifest."""
    from ouroboros.reviewer_window import REVIEWER_FULL_WINDOW
    from ouroboros.tools import scope_review as sr
    from ouroboros import capability_evidence
    from types import SimpleNamespace

    # Isolated, empty evidence -> no model gets Capability Evidence.
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        capability_evidence,
        "probe",
        lambda *a, **k: SimpleNamespace(window_tokens=0),
    )

    # An OFF-DEFAULT reviewer with no evidence sizes down to the fallback...
    w_adv = sr._scope_window("gigachat::GigaChat-3-Ultra")
    assert 0 < w_adv.window_tokens < REVIEWER_FULL_WINDOW, w_adv

    # ...as does a pinned off-default 200K model (the v6.46.0 bug: it used to be
    # wrongly trusted as 1M and overflowed).
    w_offdefault = sr._scope_window("anthropic/claude-sonnet-4.5")
    assert w_offdefault.window_tokens == sr._SCOPE_SIZING_FALLBACK, w_offdefault

    # The SHIPPED designated reviewer keeps the 1M sentinel as a SIZING number...
    w_designated = sr._scope_window(sr._SCOPE_MODEL_DEFAULT)
    assert w_designated.window_tokens == REVIEWER_FULL_WINDOW, w_designated

    # Direct-provider and explicit OpenRouter spellings of the same shipped reviewer
    # are also the designated default. Regression guard for a provider spelling
    # (openai::/openrouter::) being misclassified as off-default.
    for spelling in ("openai::gpt-5.6-terra", "openrouter::openai/gpt-5.6-terra"):
        assert sr._scope_window(spelling).window_tokens == REVIEWER_FULL_WINDOW



def test_scope_reviewer_window_uses_scope_slot_route_not_main(monkeypatch, tmp_path):
    """Capability Evidence for scope review must use the scope slot's route.

    A local-routed main lane (`USE_LOCAL_MAIN=true`) must not turn a remote direct
    OpenAI scope reviewer into a local route lookup.
    """
    from types import SimpleNamespace
    from ouroboros import capability_evidence, config
    from ouroboros.tools import scope_review as sr

    captured = {}

    def fake_probe(drive_root, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(window_tokens=333_333)

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(
        config,
        "load_settings",
        lambda: {
            "USE_LOCAL_MAIN": True,
            "OPENAI_BASE_URL": "https://api.openai.test/v1",
        },
    )
    monkeypatch.setattr(capability_evidence, "probe", fake_probe)

    assert sr._scope_window("openai::gpt-5.5").window_tokens == 333_333
    assert captured["provider"] == "openai"
    assert captured["model"] == "openai::gpt-5.5"
    assert captured["base_url"] == "https://api.openai.test/v1"
    assert captured["use_local"] is False


def test_parallel_commit_scope_is_one_substantive_call(monkeypatch, tmp_path):
    """P3 wrapper must not fan a budget result into a second degraded call."""
    from types import SimpleNamespace

    from ouroboros import config
    from ouroboros.tools import parallel_review, review
    from ouroboros.tools.scope_review import ScopeReviewResult

    calls = []

    def fake_scope(_ctx, _message, **kwargs):
        calls.append((kwargs.get("scope_model"), kwargs.get("degraded", False)))
        return ScopeReviewResult(
            blocked=False,
            status="budget_exceeded",
            model_id=str(kwargs.get("scope_model") or ""),
        )

    ctx = SimpleNamespace(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        task_id="one-pass-scope",
        _review_history=[],
        _review_advisory=[],
        _scope_review_history={},
    )
    monkeypatch.setattr(parallel_review, "run_cmd", lambda *_a, **_k: "staged diff")
    monkeypatch.setattr(parallel_review, "run_scope_review", fake_scope)
    monkeypatch.setattr(config, "get_scope_review_models", lambda: ["scope/model"])
    monkeypatch.setattr(
        review, "_prepare_unified_review", lambda *_a, **_k: (None, None, True)
    )
    from ouroboros.tools import review_admission
    monkeypatch.setattr(
        review_admission, "prepare_scope_review",
        lambda *_a, **_k: ({"packet": 1}, None),
    )

    parallel_review.run_parallel_review(ctx, "test commit")

    assert calls == [("scope/model", False)]


# --- scope review applies in every context mode (owner decision 2026-09-17) ----

def test_scope_review_runs_in_every_context_mode(monkeypatch, tmp_path):
    """The `low` coupling is removed: a retrieving scope reviewer costs a brief
    and a bounded episode, not a whole-repository pack, so the cost policy that
    declared whole-repo scope review "not performed" in `low` no longer applies.
    Every mode calls the reviewer, and no mode records a skip row."""
    from ouroboros import config
    from ouroboros.tools import scope_review as sr

    class _Ctx:
        repo_dir = str(tmp_path)
        task_id = "context-mode-scope"
        pending_events = []

        def drive_logs(self):
            return tmp_path

    called = []
    monkeypatch.setattr(sr, "_call_scope_llm", lambda *a, **k: called.append(1) or ("", None, ""))
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "false")

    for mode in ("nano", "low", "max"):
        called.clear()
        monkeypatch.setattr(config, "get_context_mode", lambda mode=mode: mode)
        monkeypatch.setattr(config, "get_owner_context_mode", lambda mode=mode: mode)
        result = sr.run_scope_review(_Ctx(), "test commit", scope_model="anthropic/claude-fable-5")
        assert called == [1], f"{mode} mode must call the scope reviewer"
        assert result.status != "skipped_low_context_mode", mode


def test_default_context_mode_is_max_and_generic_settings_merge_preserves_it(monkeypatch):
    """The default and ordinary settings writer retain their explicit contract."""
    from ouroboros import config
    from ouroboros.gateway.settings import _merge_settings_payload

    assert config.SETTINGS_DEFAULTS["OUROBOROS_CONTEXT_MODE"] == "max"
    monkeypatch.delenv("OUROBOROS_CONTEXT_MODE", raising=False)
    assert config.get_context_mode() == "max"

    merged = _merge_settings_payload({"OUROBOROS_CONTEXT_MODE": "max"},
                                     {"OUROBOROS_CONTEXT_MODE": "low"})
    assert merged["OUROBOROS_CONTEXT_MODE"] == "max"


def test_window_provenance_wording_is_five_way():
    """RS5: the cases must read differently — a conservative fallback must not be
    reported with the same words as a confirmed measurement, and an EXPIRED record
    must not be reported with the same words as a live one."""
    from ouroboros.tools import scope_window as sw

    phrases = {
        sw.window_provenance_phrase(200_000, sw.WINDOW_CONFIRMED),
        sw.window_provenance_phrase(200_000, sw.WINDOW_ASSERTED),
        sw.window_provenance_phrase(200_000, sw.WINDOW_UNKNOWN),
        sw.window_provenance_phrase(1_000_000, sw.WINDOW_STALE),
        sw.window_provenance_phrase(1_000_000, sw.WINDOW_SENTINEL),
    }
    assert len(phrases) == 5
    assert "confirmed" in sw.window_provenance_phrase(200_000, sw.WINDOW_CONFIRMED)
    assert "owner-asserted" in sw.window_provenance_phrase(200_000, sw.WINDOW_ASSERTED)
    assert "unknown window" in sw.window_provenance_phrase(200_000, sw.WINDOW_UNKNOWN)
    assert "designated-default" in sw.window_provenance_phrase(1_000_000, sw.WINDOW_SENTINEL)
    assert "EXPIRED" in sw.window_provenance_phrase(1_000_000, sw.WINDOW_STALE)

    # The label is read off the EVIDENCE, so a stale 1M record can never be labelled
    # (or worded) as a confirmed one just because its number clears the floor.
    stale = ReviewerWindow(1_000_000, "confirmed", stale=True)
    assert sw.scope_window_provenance(stale) == sw.WINDOW_STALE
    assert sw.scope_window_provenance(ReviewerWindow(250_000)) == sw.WINDOW_UNKNOWN




















# ~90K estimated tokens of test body; the INDENTED marker is unreachable through
# the staged diff (outside any -U3 hunk of an end-of-file change, and never a
# hunk-header funcname), so its absence from the prompt proves the full
# snapshot is really gone from the whole pack.
_BIG_TEST_BODY = "\n".join(
    ["def test_big():", "    UNCHANGED_BIG_TEST_MARKER_QQQ = 1"]
    + ["    filler = 1"] * 24_000
    + ["    assert True"]
) + "\n"
_BIG_TEST_CHANGED = _BIG_TEST_BODY + "\n\ndef test_added():\n    assert True\n"
































# --- scope-slot identity: one owner, one id per configured row ----------------


def _run_scope_fanout(monkeypatch, tmp_path, models):
    """Run the parallel scope fan-out over ``models`` and collect every id surface.

    Returns (substrate_ids, actor_record_ids, manifest_ids): the ids the review
    substrate physically ran the rows under (sorted — the rows run concurrently,
    so completion order is not meaningful), the ids stamped on the durable actor
    records, and the ids in the scope context manifest.
    """
    from types import SimpleNamespace

    from ouroboros import config, review_substrate
    from ouroboros.tools import parallel_review, review
    from ouroboros.tools import scope_review as sr

    rows = [
        {
            "item": item,
            "verdict": "PASS",
            "severity": "advisory",
            "reason": "Concrete scope artifact was checked and passes.",
        }
        for item in sorted(sr._SCOPE_REQUIRED_ITEMS)
    ]
    substrate_ids: list = []
    lock = threading.Lock()

    def fake_run_review_request(request, *, slots, drive_root, llm, usage_ctx=None):
        with lock:
            substrate_ids.extend(slot.slot_id for slot in slots)
        return SimpleNamespace(actors=[{
            "slot_id": slots[0].slot_id,
            "model": slots[0].model,
            "status": "ok",
            "raw_text": json.dumps(rows),
            "usage": {},
            "prompt_ref": {},
            "response_ref": {},
        }])

    monkeypatch.setattr(config, "get_scope_review_models", lambda: list(models))
    monkeypatch.setattr(review_substrate, "run_review_request", fake_run_review_request)
    monkeypatch.setattr(sr, "_scope_window",
                        lambda _model, **_k: ReviewerWindow(window_tokens=1_000_000, status="confirmed"))
    monkeypatch.setattr(parallel_review, "run_cmd", lambda *_a, **_k: "staged diff")
    monkeypatch.setattr(
        review, "_prepare_unified_review", lambda *_a, **_k: (None, None, True)
    )

    ctx = SimpleNamespace(
        repo_dir=tmp_path, drive_root=tmp_path, task_id="scope-slot-identity",
        pending_events=[], _review_history=[], _review_advisory=[], _scope_review_history={},
    )
    parallel_review.run_parallel_review(ctx, "identity commit")
    actor_ids = [str(r.get("slot_id") or "") for r in (ctx._last_scope_raw_results or [])]
    manifest = (ctx._last_scope_raw_result or {}).get("context_manifest") or {}
    manifest_ids = [str(a.get("slot_id") or "") for a in (manifest.get("actors") or [])]
    return sorted(substrate_ids), actor_ids, manifest_ids


def test_scope_rows_sharing_a_model_keep_distinct_identities(tmp_path, monkeypatch):
    """Duplicate model ids are valid independent slots (review_substrate contract,
    and get_scope_review_models preserves them on purpose). Naming a row after its
    model collapsed both rows onto one receipt id."""
    substrate_ids, actor_ids, manifest_ids = _run_scope_fanout(
        monkeypatch, tmp_path, ["model/a", "model/a"]
    )
    assert len(set(substrate_ids)) == 2, substrate_ids
    assert len(set(actor_ids)) == 2, actor_ids
    assert len(set(manifest_ids)) == 2, manifest_ids


def test_scope_rows_whose_models_sanitize_alike_keep_distinct_identities(tmp_path, monkeypatch):
    """Two DIFFERENT models can normalize to the same token (``openai::gpt-5`` and
    ``openai/gpt/5`` both sanitize to ``openai_gpt_5``), which merged two rows."""
    substrate_ids, actor_ids, manifest_ids = _run_scope_fanout(
        monkeypatch, tmp_path, ["openai::gpt-5", "openai/gpt/5"]
    )
    assert len(set(substrate_ids)) == 2, substrate_ids
    assert len(set(actor_ids)) == 2, actor_ids
    assert len(set(manifest_ids)) == 2, manifest_ids


def test_scope_row_identity_survives_editing_that_row_model(tmp_path, monkeypatch):
    """Editing a slot's model in the settings UI must not re-identify the slot:
    its receipts have to keep lining up with its own history."""
    before_substrate, before_actors, _ = _run_scope_fanout(
        monkeypatch, tmp_path, ["model/a", "model/b"]
    )
    after_substrate, after_actors, _ = _run_scope_fanout(
        monkeypatch, tmp_path, ["model/a", "model/EDITED"]
    )
    assert before_substrate == after_substrate, (before_substrate, after_substrate)
    assert before_actors == after_actors, (before_actors, after_actors)


def test_scope_actor_records_and_substrate_agree_on_one_identity(tmp_path, monkeypatch):
    """The durable actor record, the context manifest, and the substrate call that
    produced the prompt/response refs must name the SAME row. They were derived
    independently — positionally in the coordinator, from the model in the reviewer —
    so one row carried two disagreeing identities."""
    substrate_ids, actor_ids, manifest_ids = _run_scope_fanout(
        monkeypatch, tmp_path, ["model/a", "model/b"]
    )
    assert sorted(substrate_ids) == sorted(actor_ids) == sorted(manifest_ids), (
        substrate_ids, actor_ids, manifest_ids
    )
    # Pinned spelling: durable records written before v6.87.21 already carry these
    # ids, so historical receipts line up with new ones without a translation table.
    assert actor_ids == ["scope_slot_1", "scope_slot_2"], actor_ids


def test_scope_row_ids_come_from_the_one_mint(tmp_path, monkeypatch):
    """The coordinator must READ the row's id, not re-derive an identical string.

    parallel_review stamped ``scope_slot_{idx + 1}`` on the actor record and the
    manifest — byte-identical to the mint's output today, so nothing could tell
    the two apart. Repointing the ONE mint separates them: a surface that reads it
    follows, a surface that spells its own literal does not.
    """
    from ouroboros import review_substrate

    monkeypatch.setattr(
        review_substrate, "slot_id_for_row",
        lambda index, *, prefix=review_substrate.SLOT_ID_PREFIX: f"{prefix}_row{int(index)}",
    )
    substrate_ids, actor_ids, manifest_ids = _run_scope_fanout(
        monkeypatch, tmp_path, ["model/a", "model/b"]
    )
    expected = ["scope_slot_row1", "scope_slot_row2"]
    assert substrate_ids == expected, substrate_ids
    assert actor_ids == expected, actor_ids
    assert manifest_ids == expected, manifest_ids

# --- Blocking scope authority is a property of the EVIDENCE (v6.87.44) ----------

def _seed_scope_evidence(monkeypatch, tmp_path, model, *, window, status, ts, use_ack=False):
    """Write one Capability-Evidence record for ``model``'s real scope route."""
    import json as _json
    from ouroboros import capability_evidence as ce
    from ouroboros.reviewer_window import reviewer_route

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    provider, base_url = reviewer_route(model)
    fp = ce.route_fingerprint(provider=provider, base_url=base_url, model=model)
    store = tmp_path / "state" / "capability_evidence.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    key = "owner_acks" if use_ack else "probes"
    store.write_text(_json.dumps({key: {fp: {
        "window_tokens": window, "status": status, "source": "provider_metadata",
        "route_fp": fp, "model": model, "provider": provider, "ts": ts,
    }}}), encoding="utf-8", newline="\n")
    return fp


def test_stale_scope_evidence_arrives_marked_and_dated(monkeypatch, tmp_path):
    """An EXPIRED record the probe could not re-verify is a dated impression, not a
    measurement, and the scope route's resolution says so: before the typed result,
    `(window, status)` dropped `stale` on the floor, so a five-day-old 1M record kept
    across a provider outage read as `confirmed 1M`. The number still SIZES the row's
    output reserve; nothing about authority reads it."""
    import datetime

    from ouroboros import capability_evidence as ce
    from ouroboros.reviewer_window import resolve_reviewer_window
    from ouroboros.tools import scope_review as sr
    from ouroboros.tools import scope_window as sw

    model = "anthropic/claude-fable-5"
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)).isoformat()
    _seed_scope_evidence(monkeypatch, tmp_path, model, window=1_000_000,
                         status="confirmed", ts=old)
    # The provider is unreachable now, so `probe` keeps the prior record — as STALE.
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    monkeypatch.setattr(ce, "_metadata_fetch_transport_failed", lambda *a, **k: True)

    resolved = resolve_reviewer_window(model)
    assert resolved.window_tokens == 1_000_000 and resolved.status == "confirmed"
    assert resolved.stale is True, "the outage-carried record must arrive marked stale"
    assert resolved.observed_at == old, "the observation time must survive the hand-off"

    # The wording the owner reads says EXPIRED, not "confirmed" — and WHEN it was
    # last confirmed, which is the difference between a blip and a dead route.
    phrase = sw.window_provenance_phrase(
        resolved.sizing_window(sr._SCOPE_SIZING_FALLBACK),
        sw.scope_window_provenance(resolved), resolved.observed_at)
    assert "EXPIRED" in phrase and f"last confirmed {old}" in phrase

    # A CURRENT record for the same route resolves as a live measurement — the fix
    # rejects staleness, not the route.
    fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _seed_scope_evidence(monkeypatch, tmp_path, model, window=1_000_000,
                         status="confirmed", ts=fresh)
    assert resolve_reviewer_window(model).stale is False


def test_designated_default_is_probed_like_any_other_route(monkeypatch, tmp_path):
    """A designated model gets no special treatment beyond its sizing sentinel.

    The sentinel SIZES an unevidenced default at the full window (so the review is
    dispatched rather than declined before it starts) and is labelled as a sentinel,
    never as a measurement. The same name-check used to disable the ONE lazy probe
    that could source the default's window, which is why it could never stop being
    invented."""
    from types import SimpleNamespace

    from ouroboros import capability_evidence as ce
    from ouroboros.reviewer_window import REVIEWER_FULL_WINDOW
    from ouroboros.tools import scope_review as sr
    from ouroboros.tools import scope_window as sw

    fetches = []

    def fake_probe(_drive_root, **kw):
        fetches.append(bool(kw.get("allow_fetch")))
        return SimpleNamespace(window_tokens=0, status="unprobeable", source="none",
                               route_fp="fp", stale=False, ts="")

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(ce, "probe", fake_probe)

    resolved = sr._scope_window(sr._SCOPE_MODEL_DEFAULT)
    assert resolved.window_tokens == REVIEWER_FULL_WINDOW  # sizing survives
    assert sw.scope_window_provenance(resolved) == sw.WINDOW_SENTINEL
    assert fetches == [True], "the default route must get the lazy probe like any other"

    # Owner-acking that exact route replaces the sentinel with sourced evidence.
    ce.record_owner_ack(tmp_path, provider="openrouter", model=sr._SCOPE_MODEL_DEFAULT,
                        window_tokens=1_050_000, note="test")
    monkeypatch.undo()
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    acked = sr._scope_window(sr._SCOPE_MODEL_DEFAULT)
    assert acked.window_tokens == 1_050_000
    assert sw.scope_window_provenance(acked) == sw.WINDOW_ASSERTED


def test_concurrent_resolution_of_one_route_shares_one_probe(monkeypatch, tmp_path):
    """parallel_review runs the triad and the scope slots concurrently. Without the
    per-route lock two slots on the SAME route both reach the provider for a window
    the first one is already fetching; with it the second enters after the evidence
    has been stored and reads it back, so one route costs one metadata fetch."""
    import threading
    from types import SimpleNamespace

    from ouroboros import capability_evidence as ce
    from ouroboros.tools import scope_review as sr

    model = "anthropic/claude-fable-5"
    in_probe, release = threading.Event(), threading.Event()
    store: dict = {}   # stands in for capability_evidence.json, which the real probe writes
    fetches: list = []

    def fake_probe(_drive_root, **kw):
        # `probe` serves a CURRENT record straight from its cache without touching the
        # network whatever `allow_fetch` says; only an absent/expired one goes out.
        if "ev" in store:
            return store["ev"]
        if not kw.get("allow_fetch"):
            return SimpleNamespace(window_tokens=0, status="unprobeable", stale=False, ts="")
        fetches.append(str(kw.get("model") or ""))
        in_probe.set()
        release.wait(10)              # the network probe is still in flight
        store["ev"] = SimpleNamespace(
            window_tokens=1_000_000, status="confirmed", stale=False,
            ts="2026-08-02T00:00:00+00:00")
        return store["ev"]

    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(ce, "probe", fake_probe)
    monkeypatch.setattr("ouroboros.reviewer_window._LAZY_ROUTE_LOCKS", {})

    out = {}
    threads = [
        threading.Thread(target=lambda k=k: out.__setitem__(k, sr._scope_window(model)))
        for k in ("a", "b")
    ]
    threads[0].start()
    assert in_probe.wait(10), "the first thread never reached the probe"
    threads[1].start()
    threads[1].join(0.5)
    assert threads[1].is_alive(), (
        "the second thread must WAIT for the in-flight probe on its route"
    )
    release.set()
    for thread in threads:
        thread.join(10)

    assert fetches == [model], (
        f"one route must cost ONE metadata fetch; got {len(fetches)}"
    )
    assert out["a"].window_tokens == out["b"].window_tokens == 1_000_000
    assert out["a"].status == out["b"].status == "confirmed"


def test_expired_evidence_is_re_sourced_instead_of_wedging_the_process(monkeypatch, tmp_path):
    """A long-lived process must be able to RE-confirm its scope reviewer.

    The lazy probe used to be memoised for the lifetime of the process while the
    evidence it produced expired after 24h, so a healthy, connected install that
    stayed up past the TTL read its own reviewer as EXPIRED on every later
    resolution and sized every scope send against a number it had just disowned.
    How often a route may be re-probed is `capability_evidence.probe`'s TTL to
    decide — a second, never-expiring rate limit here could only ever wedge."""
    import datetime

    from ouroboros import capability_evidence as ce
    from ouroboros.reviewer_window import resolve_reviewer_window
    from ouroboros.tools import scope_review as sr

    model = "openai/gpt-5.6-terra"
    now = datetime.datetime.now(datetime.timezone.utc)
    _seed_scope_evidence(monkeypatch, tmp_path, model, window=1_050_000,
                         status="confirmed", ts=now.isoformat())
    # The provider is up the whole time: a metadata read returns the real window.
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 1_050_000)
    monkeypatch.setattr(ce, "_metadata_fetch_transport_failed", lambda *a, **k: False)

    assert resolve_reviewer_window(model).stale is False

    # ...25 hours later, in the SAME process: the one stored record has aged past the
    # 24h confirmed TTL. Nothing about the install changed.
    _seed_scope_evidence(monkeypatch, tmp_path, model, window=1_050_000, status="confirmed",
                         ts=(now - datetime.timedelta(hours=25)).isoformat())

    resolved = resolve_reviewer_window(model)
    assert resolved.stale is False, "an expired record must be RE-SOURCED, not read as expired"
    assert resolved.window_tokens == 1_050_000
    assert sr._scope_window(model).sizing_window(sr._SCOPE_SIZING_FALLBACK) == 1_050_000


class TestTriadPackExclusions:
    """The triad pack's disclosed exclusion classes (review economics, D-06a).

    The builder takes the advisory seam's ``exclude_paths`` shape and marks an
    excluded path ONCE; ``triad_pack_exclusions`` names exactly two classes the
    host can back — span-only release carriers on a VERSION-staged commit
    (``release_sync`` carrier SSOT) and governance docs byte-identical to the
    inlined prefix copy — and returns the disclosure note the caller appends."""

    def test_exclude_paths_withhold_the_text_with_one_marker(self, tmp_path):
        mod = _get_module("ouroboros.tools.review_helpers")
        # Oversize AND excluded: the exclusion marker wins, never two markers.
        (tmp_path / "uv.lock").write_bytes(b"x" * (1_048_576 + 1))
        (tmp_path / "a.py").write_text("print('kept')", encoding="utf-8", newline="\n")
        pack, omitted = mod.build_touched_file_pack(
            tmp_path, ["uv.lock", "a.py"], exclude_paths={"uv.lock"})
        assert omitted == ["uv.lock"]
        assert pack.count("### uv.lock") == 1
        assert "withheld by the caller's exclusion note" in pack
        assert "byte limit" not in pack and "xxxx" not in pack
        assert "print('kept')" in pack
        # The default is byte-identical to the pre-exclusion builder.
        pack_default, omitted_default = mod.build_touched_file_pack(tmp_path, ["a.py"])
        assert omitted_default == [] and "print('kept')" in pack_default

    @staticmethod
    def _carrier_repo(tmp_path, *, with_lock=True):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
        (repo / "VERSION").write_text("1.0.0\n", encoding="utf-8", newline="\n")
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "ouroboros"\nversion = "1.0.0"\n', encoding="utf-8", newline="\n")
        if with_lock:
            (repo / "uv.lock").write_text(_uv_lock_text("1.0.0"), encoding="utf-8", newline="\n")
        (repo / "docs").mkdir()
        (repo / "docs" / "ARCHITECTURE.md").write_text(
            "# Ouroboros v1.0.0 — Architecture\n\nArchitecture body.\n", encoding="utf-8", newline="\n")
        (repo / "docs" / "DEVELOPMENT.md").write_text("# DEV\n\nHandbook body.\n", encoding="utf-8", newline="\n")
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=str(repo), check=True)
        return repo

    @staticmethod
    def _staged_paths(repo):
        out = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(repo),
                             check=True, capture_output=True, text=True).stdout
        return [line for line in out.splitlines() if line]

    def test_span_only_carriers_and_prefix_duplicates_are_cut_on_a_version_bump(self, tmp_path):
        mod = _get_module("ouroboros.tools.review_file_pack")
        repo = self._carrier_repo(tmp_path)
        (repo / "VERSION").write_text("1.0.1\n", encoding="utf-8", newline="\n")
        (repo / "uv.lock").write_text(_uv_lock_text("1.0.1"), encoding="utf-8", newline="\n")
        # pyproject: version bump PLUS a dependency edit outside its span.
        (repo / "pyproject.toml").write_text(
            '[project]\nname = "ouroboros"\nversion = "1.0.1"\ndependencies = ["httpx"]\n',
            encoding="utf-8", newline="\n")
        (repo / "docs" / "ARCHITECTURE.md").write_text(
            "# Ouroboros v1.0.1 — Architecture\n\nArchitecture body.\n", encoding="utf-8", newline="\n")
        (repo / "docs" / "DEVELOPMENT.md").write_text("# DEV\n\nHandbook body, revised.\n", encoding="utf-8", newline="\n")
        (repo / "app.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        paths = self._staged_paths(repo)
        dev_text = (repo / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")

        excluded, note = mod.triad_pack_exclusions(
            repo, paths, prefix_texts={"docs/DEVELOPMENT.md": dev_text, "docs/DESIGN.md": ""})

        assert excluded == {"VERSION", "uv.lock", "docs/ARCHITECTURE.md", "docs/DEVELOPMENT.md"}
        assert "pyproject.toml" not in excluded and "app.py" not in excluded
        assert note.startswith("⚠️ PACK EXCLUSION NOTE: full text withheld for 4 touched file(s)")
        assert "VERSION_CARRIER_SPANS" in note and "version_carrier_desyncs" in note
        assert "uv.lock" in note and "byte-identical" in note and "docs/DEVELOPMENT.md" in note
        # The pack renders the cut through the builder's own marker + omitted list.
        pack, omitted = mod.build_touched_file_pack(repo, paths, exclude_paths=excluded)
        assert set(omitted) == excluded
        assert "httpx" in pack and "x = 2" in pack  # kept texts
        assert "Handbook body, revised." not in pack and "editable" not in pack  # withheld texts

    def test_without_version_staged_carriers_keep_their_text(self, tmp_path):
        """The carrier class is a release-bump mechanism: no VERSION staged, no
        carrier cut (the preflight carrier gate did not run); the prefix-dedup
        class is independent of it."""
        mod = _get_module("ouroboros.tools.review_file_pack")
        repo = self._carrier_repo(tmp_path)
        (repo / "uv.lock").write_text(_uv_lock_text("1.0.1"), encoding="utf-8", newline="\n")
        (repo / "docs" / "DEVELOPMENT.md").write_text("# DEV\n\nHandbook body, revised.\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        paths = self._staged_paths(repo)
        dev_text = (repo / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")

        excluded, note = mod.triad_pack_exclusions(
            repo, paths, prefix_texts={"docs/DEVELOPMENT.md": dev_text})
        assert excluded == {"docs/DEVELOPMENT.md"}
        assert "release carrier" not in note and "byte-identical" in note
        # A prefix copy with DIFFERENT bytes (or none) keeps the doc's full text.
        assert mod.triad_pack_exclusions(
            repo, paths, prefix_texts={"docs/DEVELOPMENT.md": "# DEV\n\nOther bytes.\n"}) == (set(), "")
        assert mod.triad_pack_exclusions(repo, paths, prefix_texts={}) == (set(), "")

    def test_a_carrier_new_at_head_keeps_its_text(self, tmp_path):
        mod = _get_module("ouroboros.tools.review_file_pack")
        repo = self._carrier_repo(tmp_path, with_lock=False)
        (repo / "VERSION").write_text("1.0.1\n", encoding="utf-8", newline="\n")
        (repo / "uv.lock").write_text(_uv_lock_text("1.0.1"), encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
        excluded, _note = mod.triad_pack_exclusions(
            repo, self._staged_paths(repo), prefix_texts={})
        assert excluded == {"VERSION"}


def _uv_lock_text(version):
    return (
        'version = 1\n\n[[package]]\nname = "ouroboros"\n'
        f'version = "{version}"\nsource = {{ editable = "." }}\n\n'
        '[[package]]\nname = "httpx"\nversion = "0.27.0"\n'
    )
