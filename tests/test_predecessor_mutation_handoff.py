"""Explicit independent-task custody preserves Blocking review and foreign WIP."""

from types import SimpleNamespace

from ouroboros.mutation_attribution import (
    advance_mutation_baseline, attributed_git_candidates, capture_mutation_baseline,
    record_terminal_mutation_candidates, resolve_attributed_git_paths,
)
from ouroboros.task_results import load_task_result, write_task_result
from tests.test_mutation_attribution import _git, _repo


def _source(task_id="previous"):
    return {"kind": "task_result", "task_id": task_id, "tool": "get_task_result",
            "arguments": {"task_id": task_id, "include_authority": True}}


def _previous(tmp_path):
    root = _repo(tmp_path)
    data = tmp_path / "data"
    (root / "dirty.txt").write_text("unrelated owner WIP\n", encoding="utf-8")
    write_task_result(data, "previous", "running")
    capture_mutation_baseline(data, "previous", [{"surface_type": "system_repo", "host_root": str(root)}])
    (root / "clean.txt").write_text("corrected after final review\n", encoding="utf-8")
    (root / "new.txt").write_text("retained new file\n", encoding="utf-8")
    record_terminal_mutation_candidates(data, "previous")
    write_task_result(data, "previous", "completed", review_status={"status": "fail"},
                      reason_code="review_cycles_exhausted")
    return root, data


def _start(data, root, task_id, source=None):
    write_task_result(data, task_id, "running")
    return capture_mutation_baseline(data, task_id,
        [{"surface_type": "system_repo", "host_root": str(root)}], predecessor_source=source)


def test_selected_predecessor_transfers_exact_candidates_without_forging_clean_baseline(tmp_path):
    root, data = _previous(tmp_path)
    before = load_task_result(data, "previous")
    head = _git(root, "rev-parse", "HEAD")
    _start(data, root, "unrelated")
    assert attributed_git_candidates(data, "unrelated", root)["candidates"] == []

    evidence = _start(data, root, "successor", _source())
    git = evidence["baseline"]["surfaces"][0]["git"]
    assert git["dirty_paths"] == ["clean.txt", "dirty.txt", "new.txt"]
    assert git["predecessor_adoption"]["paths"] == ["clean.txt", "new.txt"]
    selected, facts, error = resolve_attributed_git_paths(data, "successor", root, None)
    assert not error
    assert selected == ["clean.txt", "new.txt"]
    assert facts["excluded_preexisting_dirty"] == ["dirty.txt"]
    assert load_task_result(data, "previous") == before
    assert load_task_result(data, "successor").get("review_status") is None
    assert _git(root, "rev-parse", "HEAD") == head
    assert (root / "dirty.txt").read_text(encoding="utf-8") == "unrelated owner WIP\n"


def test_changed_prior_bytes_remain_foreign_and_late_selection_cannot_reset_baseline(tmp_path):
    root, data = _previous(tmp_path)
    (root / "clean.txt").write_text("someone else changed this\n", encoding="utf-8")
    _start(data, root, "successor", _source())
    assert attributed_git_candidates(data, "successor", root)["candidates"] == ["new.txt"]
    assert resolve_attributed_git_paths(data, "successor", root, ["clean.txt"])[2]
    original = _start(data, root, "late")
    assert _start(data, root, "late", _source()) == original
    assert attributed_git_candidates(data, "late", root)["candidates"] == []


def test_successor_can_edit_adopted_paths_and_keep_leftovers_across_own_commit(tmp_path):
    root, data = _previous(tmp_path)
    _start(data, root, "successor", _source())
    (root / "clean.txt").write_text("successor improvement\n", encoding="utf-8")
    assert not attributed_git_candidates(data, "successor", root)["blockers"]
    # This is the attribution epoch after a separately authorized commit, not a
    # test of reviewer approval. No product commit gate is bypassed by adoption.
    _git(root, "add", "clean.txt")
    _git(root, "commit", "-qm", "fixture reviewed commit")
    advance_mutation_baseline(data, "successor", root)
    selected, _, error = resolve_attributed_git_paths(data, "successor", root, None)
    assert selected == ["new.txt"] and not error
    terminal = record_terminal_mutation_candidates(data, "successor")
    row = terminal["terminal_candidate_snapshot"]["surfaces"][0]
    assert row["candidate_fingerprints"]["new.txt"]["sha256"]
    assert row["excluded_preexisting_dirty"] == ["dirty.txt"]


def test_changed_base_for_one_path_does_not_authorize_its_old_patch(tmp_path):
    root, data = _previous(tmp_path)
    retained = (root / "clean.txt").read_text(encoding="utf-8")
    (root / "clean.txt").write_text("new landed base\n", encoding="utf-8")
    _git(root, "add", "clean.txt")
    _git(root, "commit", "-qm", "changed base")
    (root / "clean.txt").write_text(retained, encoding="utf-8")
    _start(data, root, "successor", _source())
    assert attributed_git_candidates(data, "successor", root)["candidates"] == ["new.txt"]


def test_unrelated_landed_change_does_not_block_handoff(tmp_path):
    root, data = _previous(tmp_path)
    (root / "other.txt").write_text("landed independently\n", encoding="utf-8")
    _git(root, "add", "other.txt")
    _git(root, "commit", "-qm", "unrelated landed change")
    _start(data, root, "successor", _source())
    assert attributed_git_candidates(data, "successor", root)["candidates"] == ["clean.txt", "new.txt"]


def test_deletion_is_retained_but_size_only_fingerprints_are_not_exact_content(tmp_path, monkeypatch):
    from ouroboros import mutation_attribution

    monkeypatch.setattr(mutation_attribution, "_FINGERPRINT_MAX_BYTES", 2)
    root, data = _previous(tmp_path)
    (root / "clean.txt").unlink()
    record_terminal_mutation_candidates(data, "previous")
    _start(data, root, "successor", _source())
    assert attributed_git_candidates(data, "successor", root)["candidates"] == ["clean.txt"]


def test_legacy_or_running_predecessor_cannot_invent_exact_terminal_identity(tmp_path):
    root, data = _previous(tmp_path)
    old = load_task_result(data, "previous")["mutation_evidence"]
    old["terminal_candidate_snapshot"]["surfaces"][0].pop("candidate_fingerprints")
    write_task_result(data, "previous", "completed", mutation_evidence=old)
    _start(data, root, "successor", _source())
    assert attributed_git_candidates(data, "successor", root)["candidates"] == []
    write_task_result(data, "active", "running", mutation_evidence=old)
    _start(data, root, "other", _source("active"))
    assert attributed_git_candidates(data, "other", root)["candidates"] == []


def test_real_startup_passes_validated_predecessor_to_baseline(tmp_path):
    from ouroboros.agent import OuroborosAgent
    from ouroboros.agent_startup_checks import validate_task_authority_sources

    root, data = _previous(tmp_path)
    task = {"id": "successor", "root_task_id": "successor", "budget_drive_root": str(data),
            "predecessor_authority_source": _source()}
    assert not validate_task_authority_sources(data, task)
    write_task_result(data, "successor", "running")
    agent = SimpleNamespace(env=SimpleNamespace(repo_dir=root, drive_root=data, budget_drive_root=str(data)))
    OuroborosAgent._capture_mutation_baseline(agent, task, {})
    assert attributed_git_candidates(data, "successor", root)["candidates"] == ["clean.txt", "new.txt"]
    # Merely carrying old context is not an explicit selection by this task.
    unselected = {"id": "unselected", "root_task_id": "unselected", "budget_drive_root": str(data),
                  "metadata": {"project_last_task_result": {"task_id": "previous"}}}
    write_task_result(data, "unselected", "running")
    assert not validate_task_authority_sources(data, unselected)
    OuroborosAgent._capture_mutation_baseline(agent, unselected, {})
    assert attributed_git_candidates(data, "unselected", root)["candidates"] == []
