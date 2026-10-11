"""Presentation labels never create a second managed-update route."""

from supervisor.update_merge_policy import (
    assisted_objective, classify_conflicts, is_document_path, is_hot_code,
)


def test_clean_when_no_conflicts():
    assert classify_conflicts([]) == {
        "kind": "clean",
        "doc_conflict_paths": [],
        "code_conflict_paths": [],
        "hot_code_paths": [],
    }


def test_every_filename_uses_the_same_conflicting_route():
    paths = ["README.md", "BIBLE.md", "docs/CHECKLISTS.md", "prompts/SAFETY.md", "ouroboros/loop.py"]
    result = classify_conflicts(paths)
    assert result["kind"] == "conflicting"
    assert set(result["doc_conflict_paths"] + result["code_conflict_paths"]) == set(paths)
    assert "protected_conflict_paths" not in result
    assert result["hot_code_paths"] == ["ouroboros/loop.py"]


def test_labels_normalize_paths_without_changing_route():
    assert is_document_path("docs\\guide.md")
    assert not is_document_path("docs/notes.txt")
    assert is_hot_code("./supervisor/queue.py")


# ``assisted_objective`` renders the resolver task's objective text; authority lives
# in the tx marker + fingerprint, never in this presentation string.


def test_objective_lists_conflicts_and_has_no_rescue_phrase_without_progress_rescue():
    objective = assisted_objective({
        "target_sha": "a" * 40,
        "conflict_paths": ["x.py", "docs/y.md"],
    })

    assert "Resolve each conflicting file (x.py, docs/y.md)" in objective
    assert ("a" * 12) in objective  # short target sha
    assert "was rescued to" not in objective
    assert "do not run git commands" not in objective


def test_objective_with_progress_rescue_points_at_the_rescue_path():
    objective = assisted_objective({
        "target_sha": "a" * 40,
        "conflict_paths": ["x.py"],
        "progress_rescue": {
            "path": "/data/archive/rescue/20260810_abc",
            "reason": "assisted_rematerialize",
        },
    })

    assert "was rescued to /data/archive/rescue/20260810_abc" in objective
    assert "changes.diff there is a plain diff against the reviewed base" in objective
    assert "do not run git commands" in objective
    # The rescue note extends — never replaces — the resolution instructions.
    assert "Resolve each conflicting file (x.py)" in objective


def test_objective_falls_back_to_the_rollback_rescue_pointer():
    objective = assisted_objective({
        "target_sha": "a" * 40,
        "rollback_rescue": {"path": "/data/archive/rescue/rb", "reason": "update_rollback"},
    })

    assert "was rescued to /data/archive/rescue/rb" in objective


def test_objective_names_only_the_latest_rescue_plus_a_count():
    """Several re-materializations overwrite the single pointer; the objective names
    the LATEST rescue path plus an honest tally — no history rendering."""
    objective = assisted_objective({
        "target_sha": "a" * 40,
        "conflict_paths": ["x.py"],
        "progress_rescue": {
            "path": "/data/archive/rescue/P2",
            "reason": "assisted_rematerialize",
            "count": 2,
        },
    })

    assert "was rescued to /data/archive/rescue/P2" in objective
    assert "(2 rescues were taken; this is the latest)" in objective


def test_objective_ignores_malformed_or_pathless_progress_rescue():
    for bad in ("corrupt-string", {}, {"reason": "no-path"}, None):
        objective = assisted_objective({"target_sha": "a" * 40, "progress_rescue": bad})

        assert "was rescued to" not in objective, bad
