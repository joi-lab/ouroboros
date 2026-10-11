"""The retrieving advisory brief: tiered rules and a manifest, never bodies.

Both live advisory deliveries retrieve — the native inspection episode holds
the host's read-only tools, a delegated agent session holds its own — over the
exact tree the brief describes. The first send therefore carries intent, the
complete staged diff, the changed-file capture, the touched-path MANIFEST and
the governance tiers (``governance_context``: the rules this change activates
in full, the reference books as navigation), and leaves every file body one
``read_file`` away.

The defect these tests pin (measured 2026-09-17 on task 310dce596eb44bae):
governance docs by pointer plus the full body of every non-carrier touched file
is 882 KB of a 1,033 KB first send, above the episode's transcript bound, so the
send is refused as ``native_bound_below_first_send`` — and every one of those
bytes is something the reviewer's own tools already reach.
"""

import pathlib
import subprocess
from types import SimpleNamespace

import pytest

import ouroboros.tools.claude_advisory_review as advisory
from ouroboros.tools import preflight_review_prompt as prompt_mod


_BIG_MARKER = "BIG-BODY-MARKER-7Q"
_BIG_FILE_CHARS = 200_000


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True).stdout


def _repo_with_a_big_touched_file(tmp_path):
    """A one-line change inside a ~200 KB module — the shape of the defect: a
    tiny diff whose file body dwarfs every other section of the prompt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    filler = "filler = 'x' * 80\n" * (_BIG_FILE_CHARS // 18 + 100)
    (repo / "big.py").write_text(f"# {_BIG_MARKER}\n{filler}tail = 1\n", encoding="utf-8")
    (repo / "small.py").write_text("y = 0\n", encoding="utf-8")
    (repo / "gone.py").write_text("z = 0\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / "big.py").write_text(f"# {_BIG_MARKER}\n{filler}tail = 2\n", encoding="utf-8")
    (repo / "small.py").write_text("y = 1\n", encoding="utf-8")
    (repo / "gone.py").unlink()
    (repo / "added.py").write_text("w = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert (repo / "big.py").stat().st_size > _BIG_FILE_CHARS
    return repo


@pytest.fixture()
def api_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("OUROBOROS_REVIEWER_SLOTS", raising=False)


def test_retrieving_first_send_carries_the_manifest_and_the_diff_not_the_bodies(
        tmp_path, monkeypatch, api_env):
    """The whole dispatch, from the real working tree to the prompt the episode
    receives: the 200 KB touched module appears as ONE manifest row with its
    size and disposition, its body appears nowhere, and the complete staged
    diff is still inlined."""
    repo = _repo_with_a_big_touched_file(tmp_path)
    sent = {}

    def _capture(prompt, repo_dir, ctx_, slot, model, **_kwargs):
        sent["prompt"] = prompt
        return SimpleNamespace(
            success=True, result_text='[{"item": "correctness", "verdict": "PASS", '
                                      '"severity": "advisory", "reason": "ok"}]',
            session_id="s", cost_usd=0.0, usage={}, error="", stderr_tail="",
        ), model

    monkeypatch.setattr(advisory, "_run_advisory_native", _capture)
    ctx = SimpleNamespace(repo_dir=repo, drive_root=tmp_path / "data",
                          pending_events=[], emit_progress_fn=lambda *_: None,
                          task_id="brief-1")
    items, raw, _model, prompt_chars = advisory._run_claude_advisory(repo, "msg", ctx)

    assert not raw.startswith("⚠️ ADVISORY"), raw
    assert [item["item"] for item in items] == ["correctness"]
    prompt = sent["prompt"]

    # The body is absent; the file is named, sized and dispositioned instead.
    assert _BIG_MARKER not in prompt
    assert prompt_chars == len(prompt) < _BIG_FILE_CHARS // 2
    manifest = prompt[prompt.index("## Touched files"):prompt.index("## Staged diff")]
    rows = {line[2:].split(" — ")[0]: line[2:].split(" — ")
            for line in manifest.splitlines() if line.startswith("- ")}
    assert set(rows) == {"big.py", "small.py", "gone.py", "added.py"}
    assert rows["big.py"][2] == "modified" and "bytes" in rows["big.py"][1]
    assert rows["added.py"][2] == "added"
    assert rows["gone.py"] == ["gone.py", "no file in the tree", "deleted"]
    assert "read any path in full with read_file" in manifest

    # The complete change is still delivered, and the governance tiers name
    # every document they did not inline (this fixture tree has none of them).
    assert "diff --git a/big.py b/big.py" in prompt
    assert "+w = 2" in prompt and "-y = 0" in prompt
    assert "## Governance delivery (manifest)" in prompt
    assert "Governance navigation (read on demand)" in prompt


def test_the_mandatory_read_disclosure_is_a_reading_order_not_a_refusal():
    """A corpus larger than one working view is an ORDER, not a refusal: the
    native episode reads across successive working views
    (``review_native_episode._apply_working_view``), so the stale "cannot be
    honoured in this episode" claim must not reappear in the budget section."""
    from ouroboros.review_native_episode import native_mandatory_read_bound

    corpus, need = 600_000, 610_000
    short = native_mandatory_read_bound(need) - 1
    section = prompt_mod._mandatory_read_budget_section(corpus, need, short)

    assert "MANDATORY_READ_DISCLOSURE: native_multiple_windows_required" in section
    assert "cannot be honoured" not in section
    assert "CONTINUES across successive working views" in section
    assert "Mark as unverified only what you did not actually read" in section
    # The declared reading is the touched bodies; the rules this change
    # activates are delivered, not read.
    assert "touched bodies this review must read in full hold 600,000 chars" in section
    assert "already inline above" in section

    # The fits branch keeps its own instruction and raises no disclosure.
    roomy = prompt_mod._mandatory_read_budget_section(
        corpus, need, native_mandatory_read_bound(need))
    assert "MANDATORY_READ_DISCLOSURE" not in roomy
    assert "read every touched body in full" in roomy


def test_the_declared_mandatory_reading_is_the_touched_bodies_not_the_governance(tmp_path):
    """What the brief REQUIRES read in full is change-relative: the bodies of
    the touched paths, never the governance corpus (inline above, or named in
    the navigation and read at the reviewer's own choosing). A deleted path
    counts 0 — its complete change evidence is the staged diff."""
    repo = _repo_with_a_big_touched_file(tmp_path)

    corpus = prompt_mod._mandatory_read_corpus_chars(repo, ["big.py", "small.py", "gone.py"])
    assert corpus == (repo / "big.py").stat().st_size + (repo / "small.py").stat().st_size
    # No touched path at all (a supplied skill payload): nothing is required.
    assert prompt_mod._mandatory_read_corpus_chars(repo, []) == 0


def test_the_brief_carries_the_activated_rules_in_full_and_names_the_rest():
    """The governance tiers on the real corpus: the applicable checklist
    section, BIBLE.md and the standing disclosures arrive in full in the stable
    head, the review protocol this reviewer executes arrives with them, DESIGN
    arrives only for a change that touches ``web/``, and the map arrives as
    navigation with the read instruction. The manifest records every
    disposition, so nothing is silently absent (BIBLE P1)."""
    repo = pathlib.Path(__file__).resolve().parents[1]
    facts: dict = {}
    prompt = advisory._build_advisory_prompt(
        repo, "wire the governance tiers", goal="g", scope="s",
        resolved_paths=["web/modules/chat.js", "ouroboros/tools/preflight_review_prompt.py"],
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "M web/modules/chat.js",
                        "governance_facts": facts, "reviewer_model": "openai/gpt-5.6-sol"},
    )
    # Tier 1, in full, before anything change-relative (cache-friendly head).
    for marker in ("Philosophy version:", "# Review Checklist Standing Archive"):
        assert marker in prompt
    assert prompt.index("## CHECKLISTS.md (What to review)") < prompt.index("## BIBLE.md")
    assert prompt.index("## docs/CHECKLISTS_ARCHIVE.md") < prompt.index("## Governance delivery (manifest)")
    assert prompt.index("## BIBLE.md") < prompt.index("## Staged diff")
    # Tier 2: the protocol this reviewer executes, and DESIGN for a web/ change.
    assert "# Review & Commit Protocol" in prompt
    assert "# Ouroboros Design System" in prompt
    # Tier 3: the map is navigation with the exact read instruction, never whole.
    assert "Governance navigation (read on demand)" in prompt
    assert 'read_file(root="system_repo"' in prompt
    assert "docs/ARCHITECTURE.md" in prompt

    rows = {row["path"]: row for row in facts["governance_manifest"]}
    assert rows["BIBLE.md"]["disposition"] == "inline" and rows["BIBLE.md"]["tier"] == 1
    assert rows["docs/CHECKLISTS_ARCHIVE.md"]["disposition"] == "inline"
    assert rows["docs/CHECKLISTS.md"]["chars"] > 0  # the section this surface supplied
    assert rows["docs/DESIGN.md"]["disposition"] == "inline"
    assert rows["docs/ARCHITECTURE.md"]["disposition"] == "navigation"
    assert facts["governance_tokens_estimate"] > 0

    # No web/ path: DESIGN is named instead of inlined, and says why.
    plain: dict = {}
    other = advisory._build_advisory_prompt(
        repo, "wire the governance tiers",
        resolved_paths=["ouroboros/tools/preflight_review_prompt.py"],
        prompt_context={"diff": "DIFF-SENTINEL", "changed_files": "M ouroboros/tools/preflight_review_prompt.py",
                        "governance_facts": plain},
    )
    assert "# Ouroboros Design System" not in other
    design = {row["path"]: row for row in plain["governance_manifest"]}["docs/DESIGN.md"]
    assert design["disposition"] == "navigation" and "web/" in design["reason"]


def test_the_supplied_skill_payload_subject_keeps_its_own_pack(tmp_path):
    """A skill advisory reviews the SUPPLIED payload pack, not the worktree:
    it declares no touched paths and its scope section stays inlined."""
    repo = tmp_path / "repo"
    repo.mkdir()
    prompt = advisory._build_advisory_prompt(
        repo, "skill advisory", scope="PAYLOAD-PACK-MARKER", resolved_paths=[],
        prompt_context={"diff": "(not included)", "changed_files": "(not included)",
                        "review_surface": "skill"},
    )
    assert "PAYLOAD-PACK-MARKER" in prompt
    assert "## Skill payload pack" in prompt
    assert "(no touched files)" in prompt
