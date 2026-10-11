"""Hardening round (codex code-review C1-C3): bounded update git plumbing,
the single-batch staged-marker scan, and the merge-write tests-evidence
record. Split out of ``test_update_merge_assisted.py`` at the module-size
gate; the repo fixtures are shared from that suite."""

import supervisor.git_ops as git_ops
import supervisor.update_merge as update_merge
from tests.test_update_merge_assisted import (
    _authority_metadata,
    _conflict_repo,
    _git,
    _init_repo,
    _materialized_conflict_tx,
    _point_at,
    _supervisor_events,
)

# ---------------------------------------------------------------------------
# Hardening round (C1-C3): bounded git plumbing, batched marker scan, and the
# merge-write tests-evidence record.
# ---------------------------------------------------------------------------


def test_git_run_is_bounded_and_reports_a_typed_timeout(tmp_path):
    """C1: the update plumbing runs while the UPDATE LOCK is held — a hung git
    (clean filter, merge driver, repo lock, blocked FS) must be killed and
    surface as the typed timeout rc, never wedge the flow forever."""
    import sys

    from supervisor import update_candidate

    rc, out, err = update_candidate._git_run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(tmp_path), timeout=1.0,
    )
    assert rc == git_ops.FETCH_TIMEOUT_RC
    assert "timed out" in err
    assert out == ""


def test_marker_check_scans_all_staged_blobs_in_one_batch_process(tmp_path, monkeypatch):
    """C2: the staged-blob scan is ONE bounded ``git cat-file --batch`` process
    for the whole staged set (plus the one name scan) — never a subprocess per
    path — and still flags exactly the marker-carrying file."""
    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    for index in range(12):
        (repo / f"f{index}.txt").write_text(f"content {index}\n")
    (repo / "f3.txt").write_text("<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n")
    _git(repo, "add", "-A")

    calls = []
    real_runner = git_ops._run_git_process_bounded

    def _counting(cmd, **kwargs):
        calls.append(list(cmd))
        return real_runner(cmd, **kwargs)

    monkeypatch.setattr(git_ops, "_run_git_process_bounded", _counting)
    ok, err = update_merge.managed_assisted_marker_check()

    assert not ok and "f3.txt" in err
    assert len(calls) == 2, calls  # the -z name scan + ONE cat-file --batch
    assert calls[1][:3] == ["git", "cat-file", "--batch"]


def test_marker_check_unmerged_staged_entry_is_an_inspection_error(tmp_path, monkeypatch):
    """C2 parity pin: an UNMERGED index entry has no stage-0 blob — the batch
    resolves it to ``missing`` and the gate reports the same typed inspection
    error the per-path scan reported."""
    repo, head, plan = _conflict_repo(tmp_path, monkeypatch)
    ok_m, msg, _m0 = update_merge.materialize_assisted_merge_live(
        head, plan["local_snapshot"], plan["target_sha"], plan["base_sha"]
    )
    assert ok_m, msg
    # a.txt is unmerged and NOT git-added: no stage-0 entry to inspect.
    ok, err = update_merge.managed_assisted_marker_check()
    assert not ok and "could not inspect staged file a.txt" in err


def test_tests_evidence_write_preserves_a_concurrent_phase_transition(tmp_path, monkeypatch):
    """C3 (W2 class): a concurrent finalizer/commit-transition write landing
    between the evidence record's tx read and its write must SURVIVE — the
    evidence lands as a merge-write of only its own key, never a wholesale
    write-back of the stale snapshot."""
    from supervisor import update_candidate

    repo, head, plan, tx = _materialized_conflict_tx(tmp_path, monkeypatch)
    meta = _authority_metadata(tx)
    real_snapshot = update_candidate.worktree_snapshot_tree

    def _racing_snapshot(base_ref, **kwargs):
        current = update_merge.read_update_tx()
        current["phase"] = "committing_assisted"
        current["race_key"] = "concurrent-writer"
        update_merge.write_update_tx(current)
        return real_snapshot(base_ref, **kwargs)

    monkeypatch.setattr(update_candidate, "worktree_snapshot_tree", _racing_snapshot)
    tree = update_merge.record_managed_tests_evidence("resolver", meta)

    assert tree
    durable = update_merge.read_update_tx()
    assert durable["phase"] == "committing_assisted"  # the concurrent write survived
    assert durable["race_key"] == "concurrent-writer"
    assert (durable.get("tests_evidence") or {}).get("tree") == tree


def test_tests_evidence_on_corrupt_marker_skips_loudly_and_keeps_the_tree(tmp_path, monkeypatch):
    """C3 corrupt branch (F1 semantics): a marker corrupted between the read
    and the write is NEVER overwritten by the forensic evidence — the write is
    skipped with a loud supervisor row and the caller still gets the tree for
    the process-held proof."""
    from supervisor import update_candidate

    repo, head, plan, tx = _materialized_conflict_tx(tmp_path, monkeypatch)
    meta = _authority_metadata(tx)
    marker_path = update_merge._update_tx_marker_path()
    real_snapshot = update_candidate.worktree_snapshot_tree

    def _corrupting_snapshot(base_ref, **kwargs):
        marker_path.write_text("{corrupt", encoding="utf-8")
        return real_snapshot(base_ref, **kwargs)

    monkeypatch.setattr(update_candidate, "worktree_snapshot_tree", _corrupting_snapshot)
    tree = update_merge.record_managed_tests_evidence("resolver", meta)

    assert tree  # the process-held proof still gets its tree
    assert marker_path.read_text(encoding="utf-8") == "{corrupt"  # evidence preserved
    skipped = _supervisor_events(tmp_path, "managed_update_tx_phase_write_skipped_corrupt")
    assert skipped and "tests_evidence" in skipped[-1]["patch_keys"]


def test_marker_check_handles_non_utf8_filenames(tmp_path, monkeypatch):
    """N1 (re-gate): a valid POSIX filename with non-UTF-8 bytes must ride the
    batch as RAW BYTES end-to-end — fsdecode surrogates re-encoded strict-UTF-8
    previously raised an uncaught UnicodeEncodeError. The marker-carrying file
    is still flagged, weird name and all."""
    import os

    repo, head = _init_repo(tmp_path)
    _point_at(monkeypatch, tmp_path, repo, head)
    raw_name = b"bad-\xff.txt"
    try:
        with open(os.path.join(os.fsencode(str(repo)), raw_name), "wb") as fh:
            fh.write(b"<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n")
    except (OSError, ValueError):
        import pytest as _pytest

        _pytest.skip("filesystem refuses non-UTF-8 filenames")
    (repo / "clean.txt").write_text("fine\n")
    _git(repo, "add", "-A")

    ok, err = update_merge.managed_assisted_marker_check()

    assert not ok
    assert "conflict markers" in err and "bad-" in err


def test_render_prompt_diff_caches_non_default_widths(tmp_path, monkeypatch):
    """C5 tail (re-gate): the -U0 fit rung re-renders per consumer — the body
    for a non-default width must be computed ONCE per (subject, width) and
    served from the subject's own render cache afterwards (the subject itself
    is ctx-memoized per attempt, so the cache inherits that invalidation)."""
    import ouroboros.tools.review_subject as review_subject_mod
    from ouroboros.tools.review_subject import managed_review_subject as _build

    from tests.test_managed_review_subject import _managed_resolution_repo

    repo, ctx, tx = _managed_resolution_repo(tmp_path, monkeypatch)
    subject = _build(ctx, repo)
    assert subject is not None

    calls = {"count": 0}
    real_delta = review_subject_mod._tree_delta_diff

    def _counting(*args, **kwargs):
        calls["count"] += 1
        return real_delta(*args, **kwargs)

    monkeypatch.setattr(review_subject_mod, "_tree_delta_diff", _counting)

    first = subject.render_prompt_diff(unified=0)
    second = subject.render_prompt_diff(unified=0)
    assert first == second
    assert calls["count"] == 1  # one subprocess for two -U0 renders
    # The default width never re-runs git at all (the built body is reused).
    subject.render_prompt_diff()
    assert calls["count"] == 1
