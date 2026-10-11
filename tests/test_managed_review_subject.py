"""Lane L-review: the managed resolution-delta review subject (Δ4), the
assembly-before-dispatch admission (Q25=A), the Q28-A oversized outcomes, and
the enforcement-honest advisory guidance (O1).

The managed fixtures drive the REAL flow: a temp repo with a genuine conflicted
merge materialized by ``materialize_assisted_merge_live`` (which pins M0), a
resolver edit, and the durable tx + authority metadata the registry predicate
actually verifies.
"""

import pathlib
import subprocess
from types import SimpleNamespace

import pytest

import supervisor.git_ops as git_ops
import supervisor.update_merge as update_merge
from ouroboros.tools.review_binary_context import capture_staged_diff
from ouroboros.tools.review_subject import (
    build_triad_session_task,
    capture_review_diff,
    managed_review_subject,
)


@pytest.fixture(autouse=True)
def _packet_default_panel(monkeypatch):
    """This module pins the PACKET assembly of the default panel; the shipped
    default triad reads the work itself since #1334, so pin packet explicitly."""
    monkeypatch.setattr("ouroboros.reviewer_slot_config.DEFAULT_TRIAD_DELIVERY", "")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def _point_at(monkeypatch, tmp_path, repo, head):
    monkeypatch.setattr(git_ops, "REPO_DIR", repo)
    monkeypatch.setattr(git_ops, "BRANCH_DEV", head)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)


def _managed_resolution_repo(tmp_path, monkeypatch, official_binary=False):
    """A real conflicted managed merge, materialized (M0 pinned), then resolved.

    Layout: the official target edits ``conflict.txt`` (collides with a local
    commit) AND adds ``official.txt`` (merges mechanically — it must NEVER
    appear in the resolution delta). The resolver resolves the conflict and
    adds one file of its own. ``official_binary=True`` also lets the official
    target add ``payload`` — an EXTENSIONLESS binary absent from HEAD (the R4
    managed-deletion topology)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "conflict.txt").write_text("base\n")
    (repo / "keep.txt").write_bytes(b"k1\nk2\nk3\nk4\nk5\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    head = _git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "remote-sim")
    (repo / "conflict.txt").write_text("official\n")
    (repo / "official.txt").write_text("released official change\n")
    if official_binary:
        (repo / "payload").write_bytes(b"\x00\x01\x02official binary\x00\xff")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "official release")
    target_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", head)
    (repo / "conflict.txt").write_text("local\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local line")
    pre_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _point_at(monkeypatch, tmp_path, repo, head)

    ok, msg, m0_tree = update_merge.materialize_assisted_merge_live(
        head, pre_sha, target_sha, base_sha
    )
    assert ok and m0_tree, msg
    # The resolver's work: resolve the conflict, add one file of its own.
    (repo / "conflict.txt").write_text("resolved by the agent\n")
    (repo / "resolver_note.txt").write_text("resolver-added\n")
    _git(repo, "add", "-A")

    tx = {
        "phase": "assisted_resolution",
        "task_id": "resolver-task",
        "pre_update_sha": pre_sha,
        "target_sha": target_sha,
        "m0_tree": m0_tree,
        "conflict_paths": ["conflict.txt"],
    }
    update_merge.write_update_tx(tx)
    ctx = SimpleNamespace(
        task_id="resolver-task",
        task_metadata={
            "managed_update": {
                "authority_fingerprint": update_merge.assisted_authority_fingerprint(tx),
            }
        },
        repo_dir=str(repo),
    )
    return repo, ctx, tx


# ---------------------------------------------------------------------------
# A. The resolution-delta capture
# ---------------------------------------------------------------------------

def test_managed_capture_returns_resolution_delta_only(tmp_path, monkeypatch):
    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)

    rendered = capture_review_diff(ctx, repo)

    # Only the resolver's work is rendered...
    assert "resolved by the agent" in rendered
    assert "resolver_note.txt" in rendered
    # ...the mechanically merged official delta is NOT re-rendered...
    assert "official.txt" not in rendered
    # ...while the FULL staged candidate provably contains it (the contrast).
    assert "official.txt" in capture_staged_diff(repo)
    # Disclosure header: identities, both parents, counters, anchors.
    assert f"mechanical merge M0 {tx['m0_tree'][:12]}" in rendered
    assert tx["pre_update_sha"][:12] in rendered and tx["target_sha"][:12] in rendered
    assert "full candidate paths:" in rendered
    assert "reviewed resolution paths:" in rendered
    assert "conflict anchors" in rendered and "conflict.txt" in rendered
    assert "is not re-rendered" in rendered


def test_managed_capture_supports_zero_context_rung(tmp_path, monkeypatch):
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    # A mid-file resolver edit gives the ladder real context lines to drop.
    (repo / "keep.txt").write_bytes(b"k1\nk2\nk3-resolved\nk4\nk5\n")
    _git(repo, "add", "-A")

    full = capture_review_diff(ctx, repo)
    compact = capture_review_diff(ctx, repo, unified=0)

    assert compact != full
    assert " k1\n" in full and " k1\n" not in compact  # -U0 drops unchanged context
    assert "resolved by the agent" in compact
    assert "official.txt" not in compact


def test_dual_counters_reflect_candidate_vs_resolution(tmp_path, monkeypatch):
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)

    subject = managed_review_subject(ctx, repo)

    # Candidate vs local HEAD: conflict.txt + official.txt (>= 2 paths);
    # resolution delta: conflict.txt + resolver_note.txt (exactly 2).
    assert subject.full_candidate_paths >= 2
    assert subject.resolution_paths == 2
    assert {p for _s, p in subject.name_status} == {"conflict.txt", "resolver_note.txt"}
    assert subject.touched_paths() == ["conflict.txt", "resolver_note.txt"]
    assert subject.counters_line() in subject.header()


def test_gate_subject_carries_index_content_not_worktree(tmp_path, monkeypatch):
    """M1 regression: the reviewed gate subject is bound to the tree that
    commits. Pre-stage EVIL content, then restore an innocent worktree copy:
    the old worktree-snapshot S showed reviewers the innocent copy while the
    EVIL index committed."""
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    (repo / "conflict.txt").write_text("EVIL-PAYLOAD\n")
    _git(repo, "add", "conflict.txt")
    (repo / "conflict.txt").write_text("resolved by the agent\n")  # index keeps EVIL

    subject = managed_review_subject(ctx, repo)  # gate surface (default)

    index_tree = _git(repo, "write-tree").stdout.strip()
    assert subject.staged_tree == index_tree  # same tree the fingerprint pins
    rendered = subject.render_prompt_diff()
    assert "EVIL-PAYLOAD" in rendered           # what commits IS what is reviewed
    assert "resolved by the agent" not in rendered  # worktree-only content is not
    # The gate subject records its tree for the commit gate's binding assert.
    assert index_tree in getattr(ctx, "_last_review_subject_trees", set())

    # The ADVISORY surface stays on the worktree by contract (work-in-progress
    # review; freshness handles staleness) and records NO gate binding tree.
    advisory = managed_review_subject(ctx, repo, surface="advisory")
    assert "resolved by the agent" in advisory.render_prompt_diff()
    assert advisory.staged_tree != subject.staged_tree
    assert advisory.staged_tree not in ctx._last_review_subject_trees


def test_gate_subject_binding_mismatch_blocks_commit(tmp_path, monkeypatch):
    """M1 defense-in-depth: the commit gate asserts (typed failure) that the
    reviewed subject tree equals the review-binding fingerprint's tree_sha."""
    from ouroboros.tools import git as git_mod
    from ouroboros.tools.registry import ToolContext

    repo = tmp_path / "gaterepo"
    repo.mkdir()
    drive = tmp_path / "gatedata"
    (drive / "logs").mkdir(parents=True)
    (drive / "locks").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / "a.txt").write_text("b\n")
    ctx = ToolContext(repo_dir=repo, drive_root=drive)

    recorded_tree = {}

    def _fake_parallel(ctx, *a, **kw):
        # Simulate the divergence: the gate subjects reviewed some OTHER tree.
        ctx._last_review_subject_trees = {recorded_tree["value"]}
        return None, None, "", []

    monkeypatch.setattr(git_mod, "_check_advisory_freshness", lambda *a, **kw: None)
    monkeypatch.setattr(git_mod, "_run_review_preflight_tests", lambda *a, **kw: None)
    monkeypatch.setattr(git_mod, "_run_parallel_review", _fake_parallel)
    monkeypatch.setattr(
        git_mod, "_aggregate_review_verdict", lambda *a, **kw: (False, "", "", [], [])
    )

    recorded_tree["value"] = "f" * 40  # not the staged index tree
    outcome = git_mod._run_reviewed_stage_cycle(
        ctx, commit_message="binding mismatch", commit_start=0.0,
        skip_advisory_pre_review=True,
    )
    assert outcome["status"] == "blocked"
    assert outcome["block_reason"] == "review_subject_binding_mismatch"
    assert "not bound to the staged candidate" in outcome["message"]

    # Positive control: a subject tree equal to the binding passes the assert.
    def _fake_parallel_ok(ctx, *a, **kw):
        tree = subprocess.run(
            ["git", "-C", str(repo), "write-tree"], capture_output=True, text=True
        ).stdout.strip()
        ctx._last_review_subject_trees = {tree}
        return None, None, "", []

    monkeypatch.setattr(git_mod, "_run_parallel_review", _fake_parallel_ok)
    outcome = git_mod._run_reviewed_stage_cycle(
        ctx, commit_message="binding ok", commit_start=0.0,
        skip_advisory_pre_review=True,
    )
    assert outcome["status"] == "passed"


def test_non_managed_capture_is_byte_identical(tmp_path, monkeypatch):
    repo, _ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    stranger = SimpleNamespace(task_id="someone-else", task_metadata=None, repo_dir=str(repo))

    assert capture_review_diff(stranger, repo) == capture_staged_diff(repo)
    assert capture_review_diff(None, repo, unified=0) == capture_staged_diff(repo, unified=0)


def test_m0_missing_falls_back_to_full_diff_with_loud_disclosure(tmp_path, monkeypatch):
    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    tx.pop("m0_tree")
    tx["m0_missing_reason"] = "resumed_with_progress_before_m0_pin"
    update_merge.write_update_tx(tx)
    ctx.task_metadata = {
        "managed_update": {
            "authority_fingerprint": update_merge.assisted_authority_fingerprint(tx),
        }
    }

    rendered = capture_review_diff(ctx, repo)

    assert "M0 BASELINE UNAVAILABLE" in rendered
    assert "resumed_with_progress_before_m0_pin" in rendered
    assert "official.txt" in rendered  # the full candidate diff is the fallback body
    # M4: no delta exists — the counters must not claim a resolution count.
    assert "reviewed resolution paths: n/a (M0 missing" in rendered
    subject = managed_review_subject(ctx, repo)
    assert subject.fallback_full_diff is True
    # M2: the reviewed path set covers the FULL candidate (what the full diff
    # and the commit contain), never just the conflict anchors.
    assert set(subject.touched_paths()) >= {
        "conflict.txt", "official.txt", "resolver_note.txt",
    }


def test_m0_missing_session_fallback_texts_are_honest(tmp_path, monkeypatch):
    """M4: the SESSION variant of the M0-missing packet renders NO diff body —
    its header must instruct retrieval, not claim a rendering below, for BOTH
    the triad and the scope session builders."""
    from ouroboros.tools.review_helpers import REPO_ROOT
    from ouroboros.tools.scope_review_session import (
        ScopeBriefInputs,
        ScopeIntentContext,
        build_scope_session_task,
    )

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    tx.pop("m0_tree")
    tx["m0_missing_reason"] = "resumed_with_progress_before_m0_pin"
    update_merge.write_update_tx(tx)
    ctx.task_metadata = {
        "managed_update": {
            "authority_fingerprint": update_merge.assisted_authority_fingerprint(tx),
        }
    }
    subject = managed_review_subject(ctx, repo)
    assert subject.fallback_full_diff is True

    triad_task = build_triad_session_task(subject=subject, **_SESSION_SECTIONS)
    scope_task, _m = build_scope_session_task(repo, ScopeBriefInputs(
        commit_message="land the update",
        intent=ScopeIntentContext(goal="g", scope="s"),
        governance_repo_dir=pathlib.Path(REPO_ROOT),
        managed_subject=subject,
    ))
    for task in (triad_task, scope_task):
        assert "M0 BASELINE UNAVAILABLE" in task
        assert "retrieve the FULL staged candidate diff yourself" in task
        assert "`git diff --cached`" in task
        assert "rendered below" not in task  # no body follows in session delivery
        assert "reviewed resolution paths: n/a (M0 missing" in task


def test_binary_rows_carry_m0_baseline_identity(tmp_path, monkeypatch):
    from ouroboros.tools.review_binary_context import render_staged_binary_metadata

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    (repo / "blob.bin").write_bytes(b"\x00\x01resolver binary\x02")
    _git(repo, "add", "blob.bin")

    plain = render_staged_binary_metadata(repo, "blob.bin")
    managed = render_staged_binary_metadata(repo, "blob.bin", m0_tree=tx["m0_tree"])

    assert plain is not None and "mechanical merge M0 blob" not in plain
    assert managed is not None
    assert "- mechanical merge M0 blob: `absent`" in managed
    # Both real merge parents stay rendered.
    assert "pre-merge HEAD blob" in managed and "official MERGE_HEAD blob" in managed


# ---------------------------------------------------------------------------
# Session task texts inline the managed artifact
# ---------------------------------------------------------------------------

_SESSION_SECTIONS = dict(
    goal_section="## Goal\nland the update",
    scope_section="## Scope\nresolution only",
    checklist_section="## Review Checklist\n- correctness",
    rebuttal_section="",
    review_history_section="",
    dev_guide_text="# Dev\n\n## Rules\n\ntext\n",
    architecture_text="## Parent\nbody\n",
)


def test_triad_session_task_inlines_managed_delta(tmp_path, monkeypatch):
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    subject = managed_review_subject(ctx, repo)

    task = build_triad_session_task(subject=subject, **_SESSION_SECTIONS)

    assert "AUTHORITATIVE review subject" in task
    assert "resolved by the agent" in task
    assert "do NOT substitute your own `git diff --cached`" in task
    assert "Retrieve it yourself" not in task
    # An ordinary commit keeps the retrieval pointer.
    plain = build_triad_session_task(subject=None, **_SESSION_SECTIONS)
    assert "Retrieve it yourself" in plain and "resolved by the agent" not in plain


def test_scope_session_task_inlines_managed_delta(tmp_path, monkeypatch):
    from ouroboros.tools.review_helpers import REPO_ROOT
    from ouroboros.tools.scope_review_session import (
        ScopeBriefInputs,
        ScopeIntentContext,
        build_scope_session_task,
    )

    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    subject = managed_review_subject(ctx, repo)

    task, manifest = build_scope_session_task(repo, ScopeBriefInputs(
        commit_message="land the update",
        intent=ScopeIntentContext(goal="g", scope="s"),
        governance_repo_dir=pathlib.Path(REPO_ROOT),
        managed_subject=subject,
    ))

    assert "AUTHORITATIVE review subject" in task
    assert "resolved by the agent" in task
    assert "conflict.txt" in task
    assert manifest["diff_delivery"] == "inline"
    # The subject is the resolution delta: the already-released official change
    # is NOT re-rendered, and the brief never points at `git diff --cached`.
    assert "released official change" not in task
    assert "do NOT substitute your own `git diff --cached`" in task

    # An ordinary commit's subject is the staged diff itself, official delta
    # included — and it carries none of the managed subject's authority wording.
    plain_task, plain_manifest = build_scope_session_task(repo, ScopeBriefInputs(
        commit_message="land the update",
        intent=ScopeIntentContext(goal="g", scope="s"),
        governance_repo_dir=pathlib.Path(REPO_ROOT),
    ))
    assert "AUTHORITATIVE review subject" not in plain_task
    assert "released official change" in plain_task
    assert plain_manifest["diff_delivery"] == "inline"


# ---------------------------------------------------------------------------
# B. Assembly-before-dispatch admission (Q25=A) — $0 on deterministic no-fit
# ---------------------------------------------------------------------------

def _admission_ctx(repo):
    return SimpleNamespace(
        repo_dir=str(repo),
        drive_root=str(repo.parent / "data"),
        task_id="t-admission",
        task_metadata=None,
        _review_history=[],
        _review_advisory=[],
    )


def _plain_repo(tmp_path):
    repo = tmp_path / "plainrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "x.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "x.txt").write_text("y\n")
    _git(repo, "add", "-A")
    return repo


def _fake_slot(name="scope_slot_1", route=None):
    return SimpleNamespace(
        model=f"m/{name}", slot_id=name, route=route, effort="",
        session_target="", session_profile="",
    )


def test_triad_assembly_block_dispatches_nothing(tmp_path, monkeypatch):
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review as review_mod

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    block = "⚠️ REVIEW_BLOCKED: deterministic assembly failure"
    monkeypatch.setattr(
        review_mod, "_prepare_unified_review", lambda *a, **k: (None, block, True)
    )
    monkeypatch.setattr(
        review_mod, "_dispatch_unified_review",
        lambda *a, **k: pytest.fail("triad dispatched despite assembly block"),
    )
    monkeypatch.setattr(
        pr, "run_scope_review",
        lambda *a, **k: pytest.fail("scope dispatched despite assembly block"),
    )
    monkeypatch.setattr(
        pr, "_prepare_scope_rows",
        lambda *a, **k: [{"slot": _fake_slot(), "prepared": {"ok": True}, "final": None}],
    )

    review_err, scope_result, _reason, _advisory = pr.run_parallel_review(ctx, "msg")

    assert review_err == block
    assert scope_result is not None and scope_result.blocked is False
    assert scope_result.status == "not_dispatched"
    reasons = " ".join(
        f.get("reason", "") for f in (scope_result.advisory_findings or [])
    )
    assert "$0 spent" in reasons


def test_scope_assembly_block_dispatches_nothing(tmp_path, monkeypatch):
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review as review_mod
    from ouroboros.tools.scope_review import ScopeReviewResult

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    monkeypatch.setattr(
        review_mod, "_prepare_unified_review",
        lambda *a, **k: ({"prompt": "p", "blocking_review": False}, None, False),
    )
    monkeypatch.setattr(
        review_mod, "_dispatch_unified_review",
        lambda *a, **k: pytest.fail("triad dispatched despite deterministic scope block"),
    )
    monkeypatch.setattr(
        pr, "run_scope_review",
        lambda *a, **k: pytest.fail("scope dispatched its blocked row"),
    )
    blocked_row = ScopeReviewResult(
        blocked=True, status="error",
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context",
        model_id="m/scope_slot_1",
    )
    monkeypatch.setattr(
        pr, "_prepare_scope_rows",
        lambda *a, **k: [{"slot": _fake_slot(), "prepared": None, "final": blocked_row}],
    )

    review_err, scope_result, reason, _advisory = pr.run_parallel_review(ctx, "msg")

    assert review_err is None
    assert scope_result is not None and scope_result.blocked is True
    assert "Failed to build review context" in scope_result.block_message
    assert any(
        "triad_not_dispatched_assembly_block" in r
        for r in getattr(ctx, "_review_degraded_reasons", [])
    )


def test_healthy_assembly_dispatches_both(tmp_path, monkeypatch):
    """The REAL triad assembly runs over the plain-repo fixture; only the LLM
    dispatch seam is patched — the dispatched packet must carry the ACTUAL
    staged hunk, proving assembly assembled the real evidence (m3a)."""
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review as review_mod
    from ouroboros.tools.scope_review import ScopeReviewResult

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    ctx._review_iteration_count = 0
    calls = {"triad": 0, "scope": 0}

    def fake_dispatch(_ctx, _msg, prepared):
        calls["triad"] += 1
        # The REAL assembled api pack carries the actual staged hunk (x -> y).
        assert "+y" in prepared["prompt"] and "-x" in prepared["prompt"]
        assert prepared["models"], "resolved reviewer rows must ride with the packet"
        return None

    monkeypatch.setattr(review_mod, "_dispatch_unified_review", fake_dispatch)
    prepared_row = {"slot": _fake_slot(), "prepared": {"packet": 1}, "final": None}
    monkeypatch.setattr(pr, "_prepare_scope_rows", lambda *a, **k: [prepared_row])

    def fake_scope(_ctx, _msg, **kwargs):
        calls["scope"] += 1
        assert kwargs["prepared"] == {"packet": 1}
        return ScopeReviewResult(blocked=False, status="responded", model_id="m/scope_slot_1")

    monkeypatch.setattr(pr, "run_scope_review", fake_scope)

    review_err, scope_result, _reason, _advisory = pr.run_parallel_review(
        ctx, "healthy assembly test commit"
    )

    assert review_err is None
    assert calls == {"triad": 1, "scope": 1}
    assert scope_result is not None and scope_result.status == "responded"


# ---------------------------------------------------------------------------
# Q28-A oversized outcomes
# ---------------------------------------------------------------------------

def _triad_real_fit_env(tmp_path, monkeypatch, row_plan):
    """_prepare_unified_review over the REAL managed repo with the REAL fit
    ladder (m3c): every api slot's calibrated input limit is pinned tiny
    through the documented patch seam, so the genuinely assembled pack — full
    snapshots, then the fit note, then the subject's own -U0 rung — overflows
    at every rung and terminates in the ladder's own block message. No error
    string is injected anywhere."""
    from ouroboros.tools import review as review_mod
    import ouroboros.reviewer_slot_config as slot_cfg

    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    ctx.drive_root = str(tmp_path / "data")
    ctx._review_history = []
    ctx._review_advisory = []
    ctx._review_iteration_count = 0
    monkeypatch.setattr(slot_cfg, "commit_triad_delivery", lambda: row_plan)
    monkeypatch.setattr(
        review_mod, "calibrated_input_token_limit", lambda *a, **k: 50
    )
    return review_mod, ctx


def _row_plan(routes):
    from ouroboros.review_execution import ReviewRouteKind

    kinds = {
        "api": ReviewRouteKind.API_CHAT,
        "session": ReviewRouteKind.AGENT_SESSION,
    }
    return {
        "models": [f"m/{i}-{r}" for i, r in enumerate(routes)],
        "routes": [kinds[r] for r in routes],
        "efforts": ["" for _ in routes],
        "session_targets": ["" for _ in routes],
        "session_profiles": ["" for _ in routes],
        "use_local": [False for _ in routes],
        "slot_ids": [f"slot_{i}" for i, _ in enumerate(routes)],
    }


def test_fit_error_with_session_quorum_drops_api_rows(tmp_path, monkeypatch):
    review_mod, ctx = _triad_real_fit_env(
        tmp_path, monkeypatch, _row_plan(["api", "session", "session"])
    )

    prepared, early, exited = review_mod._prepare_unified_review(
        ctx, "resolve managed update conflicts"
    )

    assert not exited and early is None
    assert prepared["models"] == ["m/1-session", "m/2-session"]
    assert prepared["prompt"] == ""
    # Sessions still get the REAL task text, with the managed delta inlined.
    assert "AUTHORITATIVE review subject" in prepared["session_task"]
    assert "resolved by the agent" in prepared["session_task"]
    assert any(
        "triad_api_rows_dropped_oversize_pack" in r for r in ctx._review_degraded_reasons
    )
    assert ctx._last_triad_models == ["m/1-session", "m/2-session"]


def test_fit_error_without_session_quorum_is_typed_zero_spend_with_guidance(
    tmp_path, monkeypatch
):
    from ouroboros.tools.review_admission import (
        MANAGED_OVERSIZE_GUIDANCE,
        MANAGED_SPLIT_IMPOSSIBLE,
    )

    review_mod, ctx = _triad_real_fit_env(
        tmp_path, monkeypatch, _row_plan(["api", "api", "session"])
    )

    prepared, early, exited = review_mod._prepare_unified_review(
        ctx, "resolve managed update conflicts"
    )

    assert exited and prepared is None
    # The REAL ladder's terminal, with the managed wording REPLACING the
    # structurally impossible split clause (M3) — never appended below it.
    assert "irreducible one-pass triad prompt" in early
    assert MANAGED_SPLIT_IMPOSSIBLE in early
    assert MANAGED_OVERSIZE_GUIDANCE in early  # managed resolver carries Q28-A guidance
    assert "Settings → Agents → Review lanes" in early
    assert "Split or shrink the staged change" not in early
    assert ctx._last_review_block_reason == "fixed_overflow"


def test_no_scope_row_yields_its_seat_to_a_retrieving_quorum(tmp_path, monkeypatch):
    """Every scope row retrieves, so no row can be refused for a packet it never
    receives and there is nothing for a quorum to absorb: a terminal produced at
    assembly is preserved as the row's own outcome, whatever its neighbours are.
    """
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review_admission as admission
    from ouroboros.tools.scope_review import ScopeReviewResult

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    api_slot = _fake_slot("scope_api", route=ReviewRouteKind.API_CHAT)
    s1 = _fake_slot("scope_s1", route=ReviewRouteKind.AGENT_SESSION)
    s2 = _fake_slot("scope_s2", route=ReviewRouteKind.AGENT_SESSION)
    terminal = ScopeReviewResult(
        blocked=True, status="error", failure_phase="context",
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context",
        model_id=api_slot.model,
    )

    def fake_prepare(_ctx, _msg, **kwargs):
        if kwargs["slot_id"] == "scope_api":
            return None, terminal
        return {"brief": kwargs["slot_id"], "delegated": True}, None

    monkeypatch.setattr(admission, "prepare_scope_review", fake_prepare)
    monkeypatch.setattr(pr, "scope_reviewer_slots", lambda: [api_slot, s1, s2])

    rows = pr._prepare_scope_rows(
        ctx, "msg", goal="", scope="", review_rebuttal="",
        history_snapshot=[], scope_history=[],
    )

    kept = rows[0]["final"]
    assert kept.blocked is True and kept.block_message
    assert not any(f.get("item") == "scope_api_row_oversize_yielded"
                   for f in (kept.advisory_findings or []))
    assert rows[1]["prepared"] and rows[2]["prepared"]


def test_dead_session_rows_do_not_count_toward_yield_quorum(tmp_path, monkeypatch):
    """m8: a session row that already terminated at assembly (final is not
    None) is a dead seat — it can never deliver the verdict the yield leans on,
    so the fit-blocked api row must KEEP its terminal."""
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review_admission as admission
    from ouroboros.tools.scope_review import ScopeReviewResult

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    api_slot = _fake_slot("scope_api", route=ReviewRouteKind.API_CHAT)
    s1 = _fake_slot("scope_s1", route=ReviewRouteKind.AGENT_SESSION)
    s2 = _fake_slot("scope_s2", route=ReviewRouteKind.AGENT_SESSION)
    fit_blocked = ScopeReviewResult(
        blocked=True, status="sub_floor",
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: pack did not assemble",
        model_id=api_slot.model,
    )
    dead_session = ScopeReviewResult(
        blocked=True, status="error",
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context",
        model_id=s2.model,
    )

    def fake_prepare(_ctx, _msg, **kwargs):
        if kwargs["slot_id"] == "scope_api":
            return None, fit_blocked
        if kwargs["slot_id"] == "scope_s2":
            return None, dead_session  # dead seat: terminated at assembly
        return {"packet": kwargs["slot_id"], "delegated": True}, None

    monkeypatch.setattr(admission, "prepare_scope_review", fake_prepare)
    monkeypatch.setattr(pr, "scope_reviewer_slots", lambda: [api_slot, s1, s2])

    rows = pr._prepare_scope_rows(
        ctx, "msg", goal="", scope="", review_rebuttal="",
        history_snapshot=[], scope_history=[],
    )

    # Only ONE live session row remains (< adaptive quorum 2 of 3): no yield.
    assert rows[0]["final"].blocked is True
    assert rows[0]["final"].block_message  # terminal preserved


def test_all_not_dispatched_scope_aggregate_is_typed(tmp_path, monkeypatch):
    """m9: a panel where EVERY row is a $0 not_dispatched placeholder must not
    record scope_quorum_not_met ("diversity was not achieved") — no reviewer
    ran to fall short; the manifest carries the distinct typed reason."""
    from ouroboros.tools import parallel_review as pr

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    rows = [
        {"slot": _fake_slot("scope_a"), "prepared": {"packet": "a"}, "final": None},
        {"slot": _fake_slot("scope_b"), "prepared": {"packet": "b"}, "final": None},
    ]

    result = pr._run_scope(
        ctx, "msg", rows, False, goal="", scope="", review_rebuttal="",
        history_snapshot=[], scope_history=[],
    )

    assert result.blocked is False
    items = [f.get("item") for f in (result.advisory_findings or [])]
    assert "scope_quorum_not_met" not in items
    assert items.count("scope_row_not_dispatched") == 2  # typed per-row records
    reasons = " ".join(result.context_manifest.get("scope_degraded_reasons", []))
    assert "scope_not_dispatched_assembly_block" in reasons
    assert "scope_quorum_not_met" not in reasons


def test_scope_fit_blocked_api_row_stays_blocking_without_session_quorum(
    tmp_path, monkeypatch
):
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review_admission as admission
    from ouroboros.tools.scope_review import ScopeReviewResult

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    api_slot = _fake_slot("scope_api", route=ReviewRouteKind.API_CHAT)
    fit_blocked = ScopeReviewResult(
        blocked=True, status="sub_floor",
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: pack did not assemble",
        model_id=api_slot.model,
    )
    monkeypatch.setattr(
        admission, "prepare_scope_review", lambda *a, **k: (None, fit_blocked)
    )
    monkeypatch.setattr(pr, "scope_reviewer_slots", lambda: [api_slot])

    rows = pr._prepare_scope_rows(
        ctx, "msg", goal="", scope="", review_rebuttal="",
        history_snapshot=[], scope_history=[],
    )

    assert rows[0]["final"].blocked is True  # pure-api panel keeps the terminal


# ---------------------------------------------------------------------------
# Managed advisory oversize → audited non-blocking skip (honest wording)
# ---------------------------------------------------------------------------

def test_managed_advisory_oversized_delta_becomes_audited_skip(tmp_path, monkeypatch):
    """m3b: the REAL managed repo with a genuinely >500k resolution delta rides
    the REAL _advisory_review_diff into the audited skip; the skip run is
    PERSISTED; and the trailing gate sentence branches on enforcement (M5)."""
    import json

    import ouroboros.tools.claude_advisory_review as adv
    from ouroboros.review_state import load_state

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    ctx.drive_root = str(tmp_path / "data")
    # The RESOLVER'S OWN edit is >500k chars, so the real M0->S delta overflows.
    (repo / "resolver_note.txt").write_text("resolver payload line\n" * 30_000)
    _git(repo, "add", "-A")

    monkeypatch.setattr(adv, "_get_review_enforcement", lambda: "blocking")
    items, raw, model, prompt_chars = adv._run_claude_advisory(
        repo, "land the update", ctx, options={"drive_root": tmp_path / "data"}
    )

    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED:")
    assert adv._MANAGED_SKIP_MARKER in raw
    assert "cannot be split" in raw
    assert "non-blocking and audited" in raw
    assert "Split the commit into smaller pieces" not in raw
    assert "Triad and scope review still gate the commit." in raw
    assert prompt_chars > adv._MAX_DIFF_CHARS_ERROR

    # M5: under advisory enforcement the gate sentence must not overpromise.
    monkeypatch.setattr(adv, "_get_review_enforcement", lambda: "advisory")
    _items2, raw2, _model2, _chars2 = adv._run_claude_advisory(
        repo, "land the update", ctx, options={"drive_root": tmp_path / "data"}
    )
    assert "still gate the commit" not in raw2
    assert "enforcement is advisory, so their findings are recorded rather than blocking" in raw2

    # Skip-record persistence: the full pre-review handler durably records the
    # skipped run (the pre-SDK gate is owned by its own suite and is stubbed).
    monkeypatch.setattr(
        adv, "_advisory_pre_sdk_gate", lambda **kw: ([], "conflict.txt", None)
    )
    resp = json.loads(adv._handle_advisory_pre_review(ctx, commit_message="land the update"))
    assert resp["status"] == "skipped"
    state = load_state(tmp_path / "data")
    skipped = [r for r in state.advisory_runs if r.status == "skipped"]
    assert skipped, "the audited skip must be durably persisted"
    assert adv._MANAGED_SKIP_MARKER in skipped[-1].raw_result
    # The persisted snapshot summary carries the disclosed dual counters.
    assert "reviewed resolution paths:" in skipped[-1].snapshot_summary


def test_non_managed_oversize_keeps_split_the_commit_error(tmp_path):
    import ouroboros.tools.claude_advisory_review as adv

    repo = _plain_repo(tmp_path)
    (repo / "huge.txt").write_text("line of filler text\n" * 40_000)
    _git(repo, "add", "-A")

    diff = adv._get_staged_diff(repo)

    assert diff.startswith("⚠️ ADVISORY_ERROR:")
    assert "Split the commit into smaller pieces." in diff


# ---------------------------------------------------------------------------
# C. Advisory honesty (O1): enforcement-branch guidance texts
# ---------------------------------------------------------------------------

def _fresh_run(items=None):
    from ouroboros.review_state import AdvisoryRunRecord

    return AdvisoryRunRecord(
        snapshot_hash="abc123", commit_message="t", status="fresh",
        ts="2026-01-01T00:00:00", items=items or [],
    )


def test_guidance_critical_findings_by_enforcement():
    import ouroboros.tools.claude_advisory_review as adv
    from ouroboros.review_state import AdvisoryReviewState

    critical = [{"item": "correctness", "verdict": "FAIL", "severity": "critical"}]
    run = _fresh_run(critical)
    state = AdvisoryReviewState(advisory_runs=[run])
    common = dict(
        latest=run, state=state, stale_from_edit=False, stale_from_edit_ts=None,
        open_obs=[], open_debts=[], effective_is_fresh=True,
    )

    blocking = adv._next_step_guidance(enforcement="blocking", **common)
    advisory = adv._next_step_guidance(enforcement="advisory", **common)

    # R6 superseded pin: the old "Fix ALL critical findings, then re-run …, or
    # audited skip" was a false dichotomy — a FRESH advisory with criticals
    # already satisfies the freshness gate and zero advisory FAILs is not a
    # hard gate. Both branches now state that honestly.
    assert "Fix ALL critical findings" not in blocking
    assert "already satisfies the commit gate's advisory-freshness requirement" in blocking
    # A5a tightened wording: the durable record is the advisory RUN record; the
    # "acknowledged" event exists only under advisory enforcement, so the
    # blocking branch must not claim the commit gate acknowledges the debt.
    assert "recorded durably on the advisory run record" in blocking
    assert "commit gate acknowledges" not in blocking
    assert "commit_reviewed is available" in blocking
    assert "blocking triad and scope reviews are the gate" in blocking
    assert "bypasses only the freshness/debt checks" in blocking
    assert "recorded durably" in advisory
    assert "you decide which to apply" in advisory
    assert "commit_reviewed is available" in advisory
    assert adv.ADVISORY_REVIEW_CHOICE_GUIDANCE in advisory


def test_guidance_open_debt_by_enforcement():
    import ouroboros.tools.claude_advisory_review as adv
    from ouroboros.review_state import AdvisoryReviewState

    run = _fresh_run()
    state = AdvisoryReviewState(advisory_runs=[run])
    common = dict(
        latest=run, state=state, stale_from_edit=False, stale_from_edit_ts=None,
        open_obs=[], open_debts=[object()], effective_is_fresh=True,
    )

    blocking = adv._next_step_guidance(enforcement="blocking", **common)
    advisory = adv._next_step_guidance(enforcement="advisory", **common)

    assert "will be blocked until that debt is cleared" in blocking
    assert "will be blocked" not in advisory
    assert "recorded durably" in advisory
    assert "commit_reviewed is available" in advisory
    # Both branches state the same outcome duty; the procedure is the author's.
    for msg in (blocking, advisory):
        assert adv.REVIEW_REPAIR_JUDGMENT in msg
        assert "group obligations by root cause" not in msg.lower()


def test_skipped_guidance_is_managed_aware():
    """m1: the skipped-branch guidance must not advise the impossible split for
    a managed skip; the plain skip keeps its historical advice. Pins both."""
    import ouroboros.tools.claude_advisory_review as adv
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord

    def _skip_run(raw):
        return AdvisoryRunRecord(
            snapshot_hash="abc123", commit_message="t", status="skipped",
            ts="2026-01-01T00:00:00", raw_result=raw,
        )

    def _guidance(run):
        return adv._next_step_guidance(
            latest=run, state=AdvisoryReviewState(advisory_runs=[run]),
            stale_from_edit=False, stale_from_edit_ts=None,
            open_obs=[], open_debts=[], effective_is_fresh=True,
        )

    managed = _guidance(_skip_run(
        "⚠️ ADVISORY_SKIPPED: managed resolution review diff too large — a "
        "managed update merge cannot be split into smaller commits."
    ))
    assert "Consider splitting the commit" not in managed
    assert "cannot be split into smaller commits" in managed
    assert "agent route" in managed or "larger-window" in managed

    plain = _guidance(_skip_run("⚠️ ADVISORY_SKIPPED: advisory prompt too large"))
    assert "did not fit the advisory route" in plain
    assert "agent_session" in plain


def test_prompt_size_gate_is_managed_aware(tmp_path, monkeypatch):
    """m1: the subject-blind 1.6M prompt gate must drop split advice when the
    diff under review is a managed resolution delta."""
    import ouroboros.tools.claude_advisory_review as adv

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    ctx.drive_root = str(tmp_path / "data")
    monkeypatch.setattr(
        adv, "_build_advisory_prompt",
        lambda *a, **k: "x" * (adv._ADVISORY_PROMPT_MAX_CHARS + 1),
    )

    _items, raw, model, _chars = adv._run_claude_advisory(
        repo, "land the update", ctx, options={"drive_root": tmp_path / "data"}
    )

    assert raw.startswith("⚠️ ADVISORY_SKIPPED:")
    assert "Consider splitting the commit." not in raw
    assert "cannot be split into smaller commits" in raw
    assert model  # the skip is attributed, exactly like the plain oversize skip

    # Non-managed contrast keeps the historical split advice.
    stranger = SimpleNamespace(
        repo_dir=str(repo), task_id="someone-else", task_metadata=None,
        drive_root=str(tmp_path / "data"),
    )
    _i2, raw2, _m2, _c2 = adv._run_claude_advisory(
        repo, "ordinary commit", stranger, options={"drive_root": tmp_path / "data"}
    )
    assert raw2.startswith("⚠️ ADVISORY_SKIPPED:")
    assert "Consider splitting the commit." in raw2


# ---------------------------------------------------------------------------
# m2: enforcement wiring — the real handlers, config patched per branch
# ---------------------------------------------------------------------------

def test_review_status_next_step_honors_enforcement(tmp_path, monkeypatch):
    import json

    import ouroboros.tools.claude_advisory_review as adv
    from ouroboros.review_state import (
        AdvisoryReviewState,
        AdvisoryRunRecord,
        compute_snapshot_hash,
        save_state,
    )

    repo = _plain_repo(tmp_path)
    drive = tmp_path / "drive"
    drive.mkdir()
    run = AdvisoryRunRecord(
        snapshot_hash=compute_snapshot_hash(repo, ""),
        commit_message="m2", status="fresh", ts="2026-01-01T00:00:00",
        items=[{"item": "correctness", "verdict": "FAIL", "severity": "critical"}],
    )
    save_state(drive, AdvisoryReviewState(advisory_runs=[run]))
    ctx = SimpleNamespace(
        repo_dir=repo, drive_root=drive,
        emit_progress_fn=lambda _: None, pending_events=[],
    )

    monkeypatch.setattr(adv, "_get_review_enforcement", lambda: "advisory")
    advisory_next = json.loads(adv._handle_review_status(ctx=ctx))["next_step"]
    assert "recorded durably" in advisory_next
    assert "recorded durably on the advisory run record" not in advisory_next

    monkeypatch.setattr(adv, "_get_review_enforcement", lambda: "blocking")
    blocking_next = json.loads(adv._handle_review_status(ctx=ctx))["next_step"]
    # R6 superseded pin: the blocking branch is honest now, never the old
    # "Fix ALL critical findings … or audited skip" false dichotomy.
    assert "Fix ALL critical findings" not in blocking_next
    assert "recorded durably on the advisory run record" in blocking_next
    assert "commit gate acknowledges" not in blocking_next
    assert "commit_reviewed is available" in blocking_next


def test_advisory_pre_review_completion_message_honors_enforcement(tmp_path, monkeypatch):
    import json

    import ouroboros.tools.claude_advisory_review as adv

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")
    repo = _plain_repo(tmp_path)
    drive = tmp_path / "drive2"
    drive.mkdir()
    ctx = SimpleNamespace(
        repo_dir=str(repo), drive_root=str(drive), task_id="m2-complete",
        task_metadata=None, emit_progress_fn=lambda _: None, pending_events=[],
    )
    monkeypatch.setattr(
        adv, "_advisory_pre_sdk_gate", lambda **kw: ([], "x.txt", None)
    )
    findings = [{"item": "correctness", "verdict": "FAIL", "severity": "critical",
                 "reason": "broken"}]
    monkeypatch.setattr(
        adv, "_run_claude_advisory", lambda *a, **k: (findings, "[…]", "opus", 100)
    )

    monkeypatch.setattr(adv, "_get_review_enforcement", lambda: "advisory")
    resp = json.loads(adv._handle_advisory_pre_review(ctx, commit_message="c1"))
    assert resp["status"] == "fresh" and resp["critical_count"] == 1
    assert "Findings are recorded durably" in resp["message"]
    assert "you decide which to apply" in resp["message"]
    assert "Fix issues" not in resp["message"]

    monkeypatch.setattr(adv, "_get_review_enforcement", lambda: "blocking")
    (repo / "x.txt").write_text("z\n")  # new snapshot: dodge the fresh-run replay
    resp2 = json.loads(adv._handle_advisory_pre_review(ctx, commit_message="c2"))
    assert resp2["status"] == "fresh"
    assert "Fix issues and run commit_reviewed when ready." in resp2["message"]


# ---------------------------------------------------------------------------
# Panel fix round (R2-R9): S-consistent fallback, loud tx failure, M0-aware
# binary deletion, typed withheld-seat records, n/a counters, friendly reason
# ---------------------------------------------------------------------------

def _drop_m0(ctx, tx, reason="resumed_with_progress_before_m0_pin"):
    tx.pop("m0_tree", None)
    tx["m0_missing_reason"] = reason
    update_merge.write_update_tx(tx)
    ctx.task_metadata = {
        "managed_update": {
            "authority_fingerprint": update_merge.assisted_authority_fingerprint(tx),
        }
    }


def test_m0_missing_gate_fallback_body_is_pinned_index_tree(tmp_path, monkeypatch):
    """R2 (gate surface): the fallback body renders from the PINNED S — the
    index write-tree — via ``git diff HEAD..S``, never a second ``--cached``
    capture (which would weaken the binding to the pinned S)."""
    import ouroboros.tools.review_subject as rs

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    _drop_m0(ctx, tx)
    # Index/worktree divergence: EVIL is staged, the worktree copy is innocent.
    (repo / "conflict.txt").write_text("EVIL-PAYLOAD\n")
    _git(repo, "add", "conflict.txt")
    (repo / "conflict.txt").write_text("innocent worktree copy\n")

    subject = managed_review_subject(ctx, repo)  # gate surface
    assert subject.fallback_full_diff is True
    monkeypatch.setattr(
        rs._rbc, "capture_staged_diff",
        lambda *a, **k: pytest.fail("fallback re-captured --cached instead of the pinned S"),
    )
    rendered = subject.render_prompt_diff()
    compact = subject.render_prompt_diff(unified=0)

    for body in (rendered, compact):
        assert "EVIL-PAYLOAD" in body          # what commits IS what is rendered
        assert "innocent worktree copy" not in body  # worktree-only content is not
    # R10a: the fallback lead must not claim the official delta is withheld.
    assert "is not re-rendered" not in rendered
    assert "INCLUDES the already-released official" in rendered


def test_m0_missing_advisory_fallback_body_matches_worktree_subject(tmp_path, monkeypatch):
    """R2 (advisory surface): S is the worktree snapshot — unstaged/untracked
    work must appear in the BODY, not only in the counters and name-status
    (the old ``--cached`` fallback body omitted it)."""
    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    _drop_m0(ctx, tx)
    (repo / "wip_unstaged.txt").write_text("worktree-only wip line\n")  # untracked

    subject = managed_review_subject(ctx, repo, surface="advisory")

    assert subject.fallback_full_diff is True
    assert "wip_unstaged.txt" in subject.touched_paths()
    rendered = subject.render_prompt_diff()
    assert "worktree-only wip line" in rendered  # body describes the same S
    assert "wip_unstaged.txt" in rendered


def test_authorized_resolver_with_broken_tx_gets_loud_fallback(tmp_path, monkeypatch):
    """R3: once the authority predicate says MANAGED, an unreadable/missing tx
    must never silently degrade to the non-managed full capture — it becomes
    the LOUD M0-missing fallback subject."""
    import ouroboros.tools.registry as registry

    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "_authorized_managed_update_resolver", lambda _ctx: True)

    def _boom(*_a, **_k):
        raise RuntimeError("tx storage exploded")

    monkeypatch.setattr(update_merge, "authorized_assisted_task", _boom)
    subject = managed_review_subject(ctx, repo)
    assert subject is not None, "authorized resolver must never get a silent None"
    assert subject.fallback_full_diff is True
    assert subject.m0_missing_reason.startswith("tx_unreadable:")
    assert "tx storage exploded" in subject.m0_missing_reason
    rendered = subject.render_prompt_diff()
    assert "M0 BASELINE UNAVAILABLE" in rendered
    assert "tx_unreadable" in rendered

    # An EMPTY tx for an authorized resolver is the same loud fallback.
    monkeypatch.setattr(update_merge, "authorized_assisted_task", lambda *_a, **_k: {})
    empty_tx_subject = managed_review_subject(ctx, repo)
    assert empty_tx_subject is not None
    assert empty_tx_subject.fallback_full_diff is True
    assert empty_tx_subject.m0_missing_reason.startswith("tx_missing:")

    # Genuinely non-managed contexts still resolve to None (byte-identical path).
    monkeypatch.setattr(registry, "_authorized_managed_update_resolver", lambda _ctx: False)
    assert managed_review_subject(ctx, repo) is None


def test_managed_binary_deletion_is_rendered_with_m0_evidence(tmp_path, monkeypatch):
    """R4: the official target added an extensionless binary absent from HEAD;
    the resolver DELETES it. The deletion row must render against M0/parent
    evidence, and the binary probe must call the path binary."""
    from ouroboros.tools.review_binary_context import (
        render_staged_binary_metadata,
        staged_path_is_binary,
    )
    from ouroboros.tools.review_helpers import build_touched_file_pack

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch, official_binary=True)
    result = _git(repo, "rm", "-qf", "payload")  # the resolver deletes the official binary
    assert result.returncode == 0, result.stderr

    subject = managed_review_subject(ctx, repo)
    m0_tree, staged_tree = subject.m0_tree, subject.staged_tree
    assert ("D", "payload") in subject.name_status  # M0→S sees the deletion

    # Detection: binary in the reviewed M0→S delta.
    assert staged_path_is_binary(repo, "payload", m0_tree=m0_tree, staged_tree=staged_tree)
    # Rendering: a real deletion row with M0 evidence, not a silent None.
    metadata = render_staged_binary_metadata(repo, "payload", m0_tree=m0_tree)
    assert metadata is not None, "managed binary deletion must be represented"
    assert "binary deletion is represented" in metadata
    assert "staged blob: `absent (deletion)`" in metadata
    assert "mechanical merge M0 blob" in metadata and "absent" not in metadata.split(
        "mechanical merge M0 blob"
    )[1].splitlines()[0]
    # Triad touched pack renders the metadata row instead of an omission.
    pack, omitted = build_touched_file_pack(
        pathlib.Path(repo), ["payload"], represent_binary=True,
        m0_tree=m0_tree, staged_tree=staged_tree,
    )
    assert "payload" not in omitted
    assert "mechanical merge M0 blob" in pack
    # The non-managed probe stays byte-identical (HEAD-only, blind to this
    # topology — documented).
    assert not staged_path_is_binary(repo, "payload")


def test_withheld_triad_seats_get_typed_records_on_assembly_block(tmp_path, monkeypatch):
    """R5a: a deterministic scope assembly block withholds a PREPARED triad —
    every configured triad seat must leave a typed $0 not_dispatched actor
    record (seat identity survives), not just a degraded-reason string."""
    from ouroboros.tools import parallel_review as pr
    from ouroboros.tools import review as review_mod
    from ouroboros.tools.scope_review import ScopeReviewResult

    repo = _plain_repo(tmp_path)
    ctx = _admission_ctx(repo)
    prepared = {
        "prompt": "p", "blocking_review": False, "row_plan": _row_plan(["api", "session"]),
    }
    monkeypatch.setattr(
        review_mod, "_prepare_unified_review", lambda *a, **k: (prepared, None, False)
    )
    monkeypatch.setattr(
        review_mod, "_dispatch_unified_review",
        lambda *a, **k: pytest.fail("triad dispatched despite deterministic scope block"),
    )
    blocked_row = ScopeReviewResult(
        blocked=True, status="error",
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context",
        model_id="m/scope_slot_1",
    )
    monkeypatch.setattr(
        pr, "_prepare_scope_rows",
        lambda *a, **k: [{"slot": _fake_slot(), "prepared": None, "final": blocked_row}],
    )

    _err, scope_result, _reason, _adv = pr.run_parallel_review(ctx, "msg")

    assert scope_result is not None and scope_result.blocked
    records = ctx._last_triad_raw_results
    assert [r["model_id"] for r in records] == ["m/0-api", "m/1-session"]
    assert all(r["status"] == "not_dispatched" for r in records)
    assert [r["slot_id"] for r in records] == ["slot_0", "slot_1"]
    assert all(r["cost_usd"] == 0.0 and r["tokens_in"] == 0 for r in records)
    assert all("$0 spent" in r["raw_text"] for r in records)


def test_q28_dropped_api_seats_survive_into_raw_results(tmp_path, monkeypatch):
    """R5b: a Q28-A-dropped api seat keeps its identity as a typed $0
    not_dispatched record MERGED beside the dispatched panel's records."""
    review_mod, ctx = _triad_real_fit_env(
        tmp_path, monkeypatch, _row_plan(["api", "session", "session"])
    )

    prepared, early, exited = review_mod._prepare_unified_review(
        ctx, "resolve managed update conflicts"
    )
    assert not exited and early is None

    withheld = getattr(ctx, "_triad_withheld_seat_records", [])
    assert [r["model_id"] for r in withheld] == ["m/0-api"]
    assert withheld[0]["status"] == "not_dispatched"
    assert withheld[0]["slot"] == 1 and withheld[0]["slot_id"] == "slot_0"

    # The dispatched panel reports; the dropped seat's record must survive.
    model_results = [
        {"model": "m/1-session", "text": "[]"},
        {"model": "m/2-session", "text": "[]"},
    ]
    review_mod._collect_review_findings(ctx, model_results)
    statuses = {r["model_id"]: r["status"] for r in ctx._last_triad_raw_results}
    assert statuses["m/0-api"] == "not_dispatched"
    assert set(statuses) == {"m/0-api", "m/1-session", "m/2-session"}


def test_full_candidate_count_failure_renders_na(tmp_path, monkeypatch):
    """R8: a failed full-candidate count renders "n/a", never a fake 0."""
    import dataclasses

    import ouroboros.tools.review_subject as rs

    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    assert rs._full_candidate_path_count(repo, "0" * 40) is None  # git failure

    subject = managed_review_subject(ctx, repo)
    broken = dataclasses.replace(subject, full_candidate_paths=None)
    assert "full candidate paths: n/a (count unavailable)" in broken.counters_line()
    assert "full candidate paths: n/a" in broken.header()
    assert "full candidate paths: 0" not in broken.header()


def test_review_status_message_names_subject_binding_mismatch():
    """R9: the review-status projection renders a friendly reason for
    review_subject_binding_mismatch instead of echoing the raw token."""
    from ouroboros.review_evidence import _review_status_message

    attempt = SimpleNamespace(status="blocked", block_reason="review_subject_binding_mismatch")
    message = _review_status_message({
        "selected_attempt": attempt,
        "effective_status": "stale",
        "open_debts": [],
    })
    assert "review_subject_binding_mismatch" in message  # the typed token stays
    assert "not the tree this commit would write" in message
    assert "Re-stage the intended candidate" in message


def test_advisory_pre_review_builds_exactly_one_subject(tmp_path, monkeypatch):
    """R7: the pre-review handler threads the counters out of the ONE subject
    _advisory_review_diff builds — never a second display-only recomputation."""
    import json

    import ouroboros.tools.claude_advisory_review as adv
    import ouroboros.tools.review_subject as rs

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")
    repo, ctx, _tx = _managed_resolution_repo(tmp_path, monkeypatch)
    ctx.drive_root = str(tmp_path / "data")
    # Force the oversize skip so the run terminates before any SDK call.
    (repo / "resolver_note.txt").write_text("resolver payload line\n" * 30_000)
    _git(repo, "add", "-A")
    monkeypatch.setattr(
        adv, "_advisory_pre_sdk_gate", lambda **kw: ([], "conflict.txt", None)
    )

    calls = {"n": 0}
    real_subject = rs.managed_review_subject

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real_subject(*args, **kwargs)

    monkeypatch.setattr(rs, "managed_review_subject", _counting)
    resp = json.loads(adv._handle_advisory_pre_review(ctx, commit_message="land the update"))

    assert resp["status"] == "skipped"
    assert calls["n"] == 1, "the subject must be built exactly once per pre-review"
    # The persisted summary still carries the disclosed dual counters, threaded
    # out of that one subject.
    from ouroboros.review_state import load_state

    skipped = [r for r in load_state(tmp_path / "data").advisory_runs if r.status == "skipped"]
    assert skipped and "reviewed resolution paths:" in skipped[-1].snapshot_summary


# ---------------------------------------------------------------------------
# Hardening round: C5 per-attempt subject memoization, C6 crashed-predicate
# marker probe.
# ---------------------------------------------------------------------------


def test_subject_is_built_once_per_key_and_reset_invalidates(tmp_path, monkeypatch):
    """C5: N consumers of one attempt (triad + scope rows + fit rungs) share
    ONE built subject per (repo, M0, S, surface) key — the memo hit returns
    the SAME object, the advisory surface is a separate key, and the
    per-attempt reset (clearing ``_managed_review_subject_memo``) forces a
    fresh build."""
    import ouroboros.tools.review_subject as review_subject_mod

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    builds = {"count": 0}
    real_delta = review_subject_mod._tree_delta_diff

    def _counting_delta(*args, **kwargs):
        builds["count"] += 1
        return real_delta(*args, **kwargs)

    monkeypatch.setattr(review_subject_mod, "_tree_delta_diff", _counting_delta)

    first = managed_review_subject(ctx, repo)
    second = managed_review_subject(ctx, repo)
    third = managed_review_subject(ctx, repo)
    assert first is not None and first is second is third
    assert builds["count"] == 1

    advisory_first = managed_review_subject(ctx, repo, surface="advisory")
    advisory_second = managed_review_subject(ctx, repo, surface="advisory")
    assert advisory_first is advisory_second and advisory_first is not first
    assert builds["count"] == 2  # the advisory surface is its own key

    # The per-attempt reset boundary invalidates the memo.
    ctx._managed_review_subject_memo = {}
    fresh = managed_review_subject(ctx, repo)
    assert fresh is not first
    assert builds["count"] == 3
    # Content is identical across the rebuild: memoization never changed
    # anything a consumer sees.
    assert fresh.render_prompt_diff() == first.render_prompt_diff()


def test_crashed_predicate_with_present_tx_marker_fails_loud(tmp_path, monkeypatch):
    """C6: an exception ESCAPING the authority predicate (programming/import
    error) while the managed update tx MARKER exists must raise the typed
    StagedDiffUnavailable — never silently review an apparently-managed
    candidate as an ordinary staged diff."""
    import ouroboros.tools.registry as registry_mod
    from ouroboros.tools.review_binary_context import StagedDiffUnavailable

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    assert update_merge._update_tx_marker_path().is_file()

    def _boom(_ctx):
        raise RuntimeError("predicate programming error")

    monkeypatch.setattr(registry_mod, "_authorized_managed_update_resolver", _boom)
    with pytest.raises(StagedDiffUnavailable):
        managed_review_subject(ctx, repo)


def test_crashed_predicate_without_marker_stays_non_managed(tmp_path, monkeypatch):
    """C6, the other branch: no tx marker → the crash is logged loudly and the
    caller stays on the ordinary staged-diff path (a non-managed commit must
    never be blocked by a managed-code bug)."""
    import ouroboros.tools.registry as registry_mod

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    assert update_merge.clear_update_tx()
    assert not update_merge._update_tx_marker_path().is_file()

    def _boom(_ctx):
        raise RuntimeError("predicate programming error")

    monkeypatch.setattr(registry_mod, "_authorized_managed_update_resolver", _boom)
    assert managed_review_subject(ctx, repo) is None
