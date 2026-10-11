"""Stale-marker ATTRIBUTION (community #783, owner decision 6A).

The advisory stale marker is a fact about a checkout: one task's edit on a
shared tree really does invalidate every task's advisory coverage there. The
community donor (``ac2b0025``) filtered the marker out of other tasks' evidence
by task id, which hid a genuinely stale tree — the panel said ``stale`` with no
reason while ``review_status`` and the commit gate still reported the edit. The
marker is ATTRIBUTED instead: every reader on the checkout keeps seeing it, and
the task whose mutation or review wrote it is named relative to the reader.
An unrecorded writer or reader stays ``unknown``; freshness, obligations and
commit-readiness debt never read the attribution.
"""

from __future__ import annotations

import json
import types

import pytest

from ouroboros.review_evidence import collect_review_evidence
from ouroboros.review_state import (
    AdvisoryReviewState,
    AdvisoryRunRecord,
    compute_snapshot_hash,
    format_status_section,
    invalidate_advisory_after_mutation,
    load_state,
    make_repo_key,
    save_state,
    update_state,
)

_ATTRIBUTION_KEYS = ("stale_task_id", "stale_attribution", "stale_repo_key")


def _checkout(tmp_path, name):
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    return repo


def _drive(tmp_path):
    drive = tmp_path / "drive"
    (drive / "state").mkdir(parents=True)
    (drive / "logs").mkdir()
    return drive


def _fresh(drive, *repos, owner="task-a"):
    state = AdvisoryReviewState()
    for repo in repos:
        state.add_run(AdvisoryRunRecord(
            snapshot_hash=compute_snapshot_hash(repo), commit_message="ready", status="fresh",
            ts="2026-09-28T00:00:00+00:00", repo_key=make_repo_key(repo), task_id=owner,
        ))
    save_state(drive, state)


def _ctx(drive, repo, task_id):
    return types.SimpleNamespace(drive_root=str(drive), repo_dir=str(repo), task_id=task_id,
                                 drive_logs=lambda: drive / "logs")


def _edit(drive, repo, task_id):
    """The ordinary tool path: ``commit_gate._invalidate_advisory`` with the task's ctx."""
    from ouroboros.tools.commit_gate import _invalidate_advisory

    (repo / "tracked.py").write_text("x = 2\n", encoding="utf-8")
    _invalidate_advisory(_ctx(drive, repo, task_id), changed_paths=["tracked.py"], source_tool="edit_text")


def _panel(drive, repo, reader):
    return collect_review_evidence(drive, task_id=reader, repo_dir=repo)["current_repo"]


def test_another_tasks_edit_keeps_the_shared_checkout_stale_and_names_the_writer(tmp_path):
    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _fresh(drive, shared)
    _edit(drive, shared, "task-a")

    other, own = _panel(drive, shared, "task-b"), _panel(drive, shared, "task-a")

    # Not hidden: the task that did not edit still sees the stale checkout and why.
    assert other["advisory_status"] == "stale"
    assert "edit_text mutated the worktree" in other["stale_reason"] and other["stale_ts"]
    assert (other["stale_task_id"], other["stale_attribution"]) == ("task-a", "other_task")
    assert (own["stale_task_id"], own["stale_attribution"]) == ("task-a", "this_task")
    assert other["stale_repo_key"] == own["stale_repo_key"] == make_repo_key(shared)
    # The relation is the ONLY difference between the two readers' readiness panels.
    assert {k: v for k, v in other.items() if k != "stale_attribution"} == {
        k: v for k, v in own.items() if k != "stale_attribution"}


def test_a_marker_scoped_to_another_checkout_is_neither_shown_nor_attributed(tmp_path):
    drive = _drive(tmp_path)
    shared, other = _checkout(tmp_path, "shared"), _checkout(tmp_path, "other")
    _fresh(drive, shared, other)
    _edit(drive, shared, "task-a")

    panel = _panel(drive, other, "task-b")

    assert panel["stale_reason"] == panel["stale_ts"] == ""
    assert all(panel[key] == "" for key in _ATTRIBUTION_KEYS)
    run = load_state(drive).find_by_hash(compute_snapshot_hash(other), repo_key=make_repo_key(other))
    assert run.status == "fresh"


def test_an_ambiguous_scope_mutation_stales_every_checkout_and_says_so(tmp_path):
    drive = _drive(tmp_path)
    shared, other = _checkout(tmp_path, "shared"), _checkout(tmp_path, "other")
    _fresh(drive, shared, other)
    invalidate_advisory_after_mutation(drive, mutation_root=None, changed_paths=[],
                                       source_tool="run_shell", mutating_task_id="task-a")

    panel = _panel(drive, other, "task-b")

    # The unscoped marker genuinely applies here: the other checkout's run IS stale.
    assert panel["advisory_status"] == "stale" and panel["stale_reason"]
    assert (panel["stale_task_id"], panel["stale_attribution"], panel["stale_repo_key"]) == (
        "task-a", "other_task", "")
    assert "applies to every checkout" in format_status_section(load_state(drive), repo_dir=other)


def test_a_legacy_marker_without_a_writer_stays_unknown_for_every_reader(tmp_path):
    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _fresh(drive, shared)
    _edit(drive, shared, "task-a")
    path = drive / "state" / "advisory_review.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("last_stale_task_id")  # a state file written before the field existed
    path.write_text(json.dumps(raw), encoding="utf-8")

    for reader in ("task-a", "task-b", ""):
        panel = _panel(drive, shared, reader)
        assert "edit_text mutated the worktree" in panel["stale_reason"], reader
        assert (panel["stale_task_id"], panel["stale_attribution"]) == ("", "unknown"), reader
    assert "Invalidated by: an unrecorded writer" in format_status_section(load_state(drive), repo_dir=shared)


def test_missing_identity_on_either_side_is_unknown_never_a_guess(tmp_path):
    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _fresh(drive, shared)
    # A writer without task identity records unknown rather than inventing one.
    invalidate_advisory_after_mutation(drive, mutation_root=shared, source_tool="run_shell")
    panel = _panel(drive, shared, "task-b")
    assert panel["stale_reason"] and (panel["stale_task_id"], panel["stale_attribution"]) == ("", "unknown")

    drive_two = _drive(tmp_path / "second")
    _fresh(drive_two, shared)
    _edit(drive_two, shared, "task-a")
    # A reader without identity sees the recorded writer but is told no relation.
    panel = _panel(drive_two, shared, "")
    assert (panel["stale_task_id"], panel["stale_attribution"]) == ("task-a", "unknown")


def test_review_status_attributes_relative_to_the_caller_not_the_task_filter(tmp_path):
    from ouroboros.tools.claude_advisory_review import _handle_review_status

    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _fresh(drive, shared)
    _edit(drive, shared, "task-a")

    caller_b = json.loads(_handle_review_status(_ctx(drive, shared, "task-b"), task_id="task-a"))
    caller_a = json.loads(_handle_review_status(_ctx(drive, shared, "task-a")))

    assert (caller_b["stale_task_id"], caller_b["stale_attribution"]) == ("task-a", "other_task")
    assert caller_a["stale_attribution"] == "this_task"
    assert caller_b["stale_from_edit"] is True and caller_a["stale_from_edit"] is True
    assert caller_b["stale_reason"] == caller_a["stale_reason"]
    assert caller_b["repo_commit_ready"] == caller_a["repo_commit_ready"]


def test_the_commit_gate_names_whose_edit_invalidated_the_advisory(tmp_path, monkeypatch):
    from ouroboros.tools.commit_gate import _check_advisory_freshness

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _fresh(drive, shared)
    _edit(drive, shared, "task-a")

    result = _check_advisory_freshness(_ctx(drive, shared, "task-b"), "commit")

    assert result and "ADVISORY_PRE_REVIEW_REQUIRED" in result
    assert "by another task (task-a); the shared checkout stays stale for every task on it" in result


def test_text_surfaces_without_a_reader_identity_state_only_the_recorded_writer(tmp_path):
    from ouroboros.agent_task_pipeline import build_review_context

    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _fresh(drive, shared)
    _edit(drive, shared, "task-a")

    context = build_review_context(types.SimpleNamespace(drive_root=drive, repo_dir=shared))

    assert "invalidated_by=task task-a" in context
    assert "Invalidated by: task task-a" in context
    assert "this task" not in context and "another task" not in context


def test_a_failed_bypass_preflight_is_attributed_to_the_reviewing_task(tmp_path):
    from ouroboros.tools.git_review_cycle import _mark_failed_bypass_advisory_stale

    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    _mark_failed_bypass_advisory_stale(_ctx(drive, shared, "task-a"), "commit", None)

    state = load_state(drive)
    assert (state.last_stale_reason, state.last_stale_task_id) == ("tests_preflight_blocked", "task-a")


def test_the_writer_round_trips_names_the_invalidation_and_clears_with_the_marker(tmp_path):
    drive, shared = _drive(tmp_path), _checkout(tmp_path, "shared")
    repo_key = make_repo_key(shared)
    _fresh(drive, shared)
    _edit(drive, shared, "task-a")
    # A later edit finds nothing fresh left to invalidate: the marker keeps naming
    # the mutation that DID invalidate the coverage, never the latest editor.
    _edit(drive, shared, "task-b")
    state = load_state(drive)
    assert state.last_stale_task_id == "task-a"

    state.add_run(AdvisoryRunRecord(snapshot_hash="re-reviewed", commit_message="m", status="fresh",
                                    ts="2026-09-28T01:00:00+00:00", repo_key=repo_key, task_id="task-b"))
    assert (state.last_stale_from_edit_ts, state.last_stale_task_id) == ("", "")
    for commit_scope in (repo_key, None):
        state.mark_repo_stale(repo_key=repo_key, reason_ts="2026-09-28T02:00:00+00:00", reason="r",
                              stale_repo_key=repo_key, stale_task_id="task-a")
        assert state.last_stale_task_id == "task-a"
        state.on_successful_commit(repo_key=commit_scope)
        assert state.last_stale_task_id == ""
        state.add_run(AdvisoryRunRecord(snapshot_hash=f"after-{commit_scope}", commit_message="m",
                                        status="fresh", ts="2026-09-28T03:00:00+00:00", repo_key=repo_key))


def test_attribution_never_changes_freshness_obligations_or_debt(tmp_path):
    """Same staleness written by three different writers reads identically everywhere
    except the attribution itself."""
    from ouroboros.review_state import CommitAttemptRecord
    from ouroboros.review_status_projection import build_review_projection

    shared = _checkout(tmp_path, "shared")
    repo_key = make_repo_key(shared)
    observed = {}
    for writer in ("task-a", "task-b", ""):
        drive = _drive(tmp_path / (writer or "unattributed"))
        _fresh(drive, shared)
        state = load_state(drive)
        state.record_attempt(CommitAttemptRecord(  # an open obligation the edit must not touch
            ts="2026-09-28T00:30:00+00:00", commit_message="blocked", status="blocked", repo_key=repo_key,
            tool_name="commit_reviewed", task_id="task-a", attempt=1, block_reason="critical_findings",
            critical_findings=[{"item": "tests_affected", "reason": "add a test", "severity": "critical",
                                "verdict": "FAIL"}],
        ))
        save_state(drive, state)
        invalidate_advisory_after_mutation(drive, mutation_root=shared, changed_paths=["tracked.py"],
                                           source_tool="edit_text", mutating_task_id=writer)
        state = load_state(drive)
        projection = build_review_projection(drive, repo_dir=shared, reader_task_id="task-a")
        observed[writer] = (
            [run.status for run in state.advisory_runs],
            state.is_fresh(compute_snapshot_hash(shared), repo_key=repo_key),
            [(d.category, d.title, d.summary, list(d.evidence), d.fingerprint, d.status)
             for d in state.get_open_commit_readiness_debts(repo_key=repo_key)],
            [(o.item, o.status) for o in state.get_open_obligations(repo_key=repo_key)],
            {key: projection[key] for key in ("stale_from_edit", "effective_status", "effective_is_fresh",
                                              "repo_commit_ready")},
        )
    assert observed["task-a"] == observed["task-b"] == observed[""]
    assert observed[""][0] == ["stale"] and observed[""][2], "the stale debt is still owed"
    assert observed[""][3] == [("tests_affected", "still_open")], "the obligation is still owed"


@pytest.mark.serial
@pytest.mark.parametrize("writer", ["builtin", "generic", "generic_error"])
def test_registered_writer_persists_identity_without_changing_shared_freshness(tmp_path, monkeypatch, writer):
    """Real Git, registry dispatch and durable readers; only the generic handler is a fake."""
    import subprocess

    import ouroboros.safety as safety
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    from ouroboros.tools.commit_gate import _check_advisory_freshness
    from ouroboros.tools.registry import ToolRegistry

    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setattr(safety, "check_safety", lambda *args, **kwargs: (True, ""))
    drive, shared = _drive(tmp_path), tmp_path / "shared"
    shared.mkdir()
    subprocess.run(["git", "init", "-q", str(shared)], check=True)
    target = shared / "tracked.py"
    target.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=shared, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                    "commit", "-qm", "baseline"], cwd=shared, check=True)
    separate = tmp_path / "separate"
    subprocess.run(["git", "clone", "-q", str(shared), str(separate)], check=True)
    _fresh(drive, shared, separate)
    reader = _ctx(drive, shared, "task-b")
    assert _check_advisory_freshness(reader, "commit") is None
    tools = ToolRegistry(repo_dir=shared, drive_root=drive)
    tools._ctx.task_id = "task-a"
    if writer != "builtin":
        # run_command is a registered mutates_worktree producer. write_file
        # owns its invalidation inside its handler, so replacing that handler
        # would remove the very path this generic-dispatch control must test.
        assert tools._entries["run_command"].mutates_worktree
        def generic_handler(ctx, *, cmd, _resolved_binding=None, **kwargs):
            assert ctx.repo_dir == shared
            target.write_text(cmd[1], encoding="utf-8")
            if writer == "generic_error" and cmd[1] != "x = 1\n":
                raise RuntimeError("failure after writing")
            return "OK"
        tools.override_handler("run_command", generic_handler)
        result = tools.execute_result("run_command", {
            "cwd": "system_repo", "cmd": ["/usr/bin/printf", "x = 1\n"]})
        assert result.status == "ok", result.text
        assert load_state(drive).is_fresh(compute_snapshot_hash(shared), make_repo_key(shared))
        assert not load_state(drive).last_stale_from_edit_ts

    result = (tools.execute_result("write_file", {
        "root": "system_repo", "path": "tracked.py", "content": "x = 2\n"})
        if writer == "builtin" else tools.execute_result("run_command", {
            "cwd": "system_repo", "cmd": ["/usr/bin/printf", "x = 2\n"]}))
    assert (result.status == "ok") is (writer != "generic_error"), result.text
    assert target.read_text(encoding="utf-8") == "x = 2\n"
    state = load_state(drive)
    assert state.last_stale_task_id == "task-a"
    assert state.advisory_runs[0].status == "stale"
    panel = _panel(drive, shared, "task-b")
    assert panel["stale_attribution"] == "other_task" and panel["stale_reason"]
    assert not panel["repo_commit_ready"]
    prompt = format_review_evidence_for_prompt(
        collect_review_evidence(drive, task_id="task-b", repo_dir=shared))
    assert '"stale_task_id": "task-a"' in prompt and '"stale_attribution": "other_task"' in prompt
    refusal = _check_advisory_freshness(reader, "commit")
    assert "ADVISORY_PRE_REVIEW_REQUIRED" in refusal and "another task (task-a)" in refusal
    other_checkout = _panel(drive, separate, "task-b")
    assert other_checkout["repo_commit_ready"] and not other_checkout["stale_reason"]
    assert all(other_checkout[key] == "" for key in _ATTRIBUTION_KEYS)
    assert _check_advisory_freshness(_ctx(drive, separate, "task-b"), "commit") is None

    # A fresh review of the changed bytes clears the marker and admits either task.
    update_state(drive, lambda current: current.add_run(AdvisoryRunRecord(
        snapshot_hash=compute_snapshot_hash(shared), commit_message="ready", status="fresh",
        ts="2026-10-01T00:00:00+00:00", repo_key=make_repo_key(shared), task_id="task-b")))
    for task_id in ("task-a", "task-b"):
        panel = _panel(drive, shared, task_id)
        assert panel["repo_commit_ready"] and not panel["stale_reason"]
        assert all(panel[key] == "" for key in _ATTRIBUTION_KEYS)
        assert _check_advisory_freshness(_ctx(drive, shared, task_id), "commit") is None
