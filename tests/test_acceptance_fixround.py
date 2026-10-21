"""Survivors of the first P3 acceptance-packet fix round.

The acceptance packet, its evidence-ref vocabulary, its budget ladder and its
zero-physical refusals are gone with the task-acceptance machine. Two fixes of
that round outlive it: the skill-review history's root-task projection (bounded
reads, durable gaps, idempotent appends, hot-store enrollment) and a refused
review seat that reuses the prompt record it already persisted.
"""

import json
from pathlib import Path


def test_skill_history_root_task_projection_avoids_whole_history_reads(monkeypatch, tmp_path):
    from ouroboros.skill_readiness import _skill_names_from_review_history
    from ouroboros import utils

    skill_dir = tmp_path / "state" / "skills" / "large-skill"
    skill_dir.mkdir(parents=True)
    history = skill_dir / "review_history.jsonl"
    history.write_text(
        (json.dumps({"root_task_id": "some-other-root", "padding": "x" * 200}) + "\n")
        * 20_000,
        encoding="utf-8",
    )
    (tmp_path / "state" / "skill_review_root_tasks.jsonl").write_text(
        json.dumps({"root_task_id": "root-wanted", "skill": "large-skill"}) + "\n",
        encoding="utf-8",
    )
    original = Path.read_text
    original_iter = utils.iter_jsonl_objects
    bounded_projection_reads = []

    def _guarded_read(path, *args, **kwargs):
        if path.name == "review_history.jsonl":
            raise AssertionError("the root-task lookup rebuilt the full skill review history")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _guarded_read)
    def _bounded_iter(path, *args, **kwargs):
        if path.name == "skill_review_root_tasks.jsonl":
            bounded_projection_reads.append(kwargs)
            assert kwargs.get("tail_bytes")
            assert kwargs.get("max_entries")
        return original_iter(path, *args, **kwargs)

    monkeypatch.setattr(utils, "iter_jsonl_objects", _bounded_iter)
    history = _skill_names_from_review_history(tmp_path, "root-wanted")
    assert history["names"] == ["large-skill"]
    assert history["coverage"]["complete"] is True
    assert len(bounded_projection_reads) == 1


def test_terminal_skill_review_updates_the_root_task_projection(tmp_path):
    from ouroboros.skill_review_runner import _append_terminal_history

    assert _append_terminal_history(
        tmp_path,
        "projected-skill",
        {"job_id": "job-1", "root_task_id": "root-1"},
        status="pass",
        terminal_reason="review_complete",
        ts="2026-09-03T00:00:00+00:00",
    )
    assert _append_terminal_history(
        tmp_path,
        "projected-skill",
        {"job_id": "job-1", "root_task_id": "root-1"},
        status="pass",
        terminal_reason="review_complete",
        ts="2026-09-03T00:00:00+00:00",
    )
    rows = [
        json.loads(line)
        for line in (tmp_path / "state" / "skill_review_root_tasks.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert rows == [{
        "ts": "2026-09-03T00:00:00+00:00",
        "root_task_id": "root-1",
        "skill": "projected-skill",
        "job_id": "job-1",
    }]


def test_projection_failure_is_durable_in_coverage_and_runner_receipt(monkeypatch, tmp_path):
    from ouroboros import skill_review_history
    from ouroboros.skill_readiness import _skill_names_from_review_history
    from ouroboros.skill_review_runner import _append_terminal_history

    monkeypatch.setattr(
        skill_review_history, "_append_root_task_projection_once", lambda *_a, **_k: False,
    )
    assert not _append_terminal_history(
        tmp_path,
        "gap-skill",
        {"job_id": "job-gap", "root_task_id": "root-gap"},
        status="pass",
        terminal_reason="review_complete",
        ts="2026-09-04T00:00:00+00:00",
    )

    gap_path = tmp_path / "state" / "skill_review_root_tasks.gaps.jsonl"
    gap = json.loads(gap_path.read_text(encoding="utf-8").splitlines()[0])
    assert gap == {
        "ts": "2026-09-04T00:00:00+00:00",
        "root_task_id": "root-gap",
        "skill": "gap-skill",
        "job_id": "job-gap",
        "reason": "root_task_projection_append_failed",
    }
    history = _skill_names_from_review_history(tmp_path, "root-gap")
    assert history["names"] == []
    assert history["coverage"]["complete"] is False
    assert "root_task_projection_append_failed" in history["coverage"]["gap_reasons"]

    events = [
        json.loads(line)
        for line in (tmp_path / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    receipt = next(row for row in events if row.get("type") == "skill_review_history_append_failed")
    assert "root-task projection" in receipt["reason"]


def test_skill_review_projection_retry_finds_identity_before_bounded_tail(tmp_path):
    from ouroboros.skill_review_history import _append_root_task_projection_once

    original = {"job_id": "job-original", "root_task_id": "root-original"}
    assert _append_root_task_projection_once(tmp_path, "projected-skill", original)
    for index in range(129):
        assert _append_root_task_projection_once(
            tmp_path, "projected-skill",
            {"job_id": f"job-{index}", "root_task_id": f"root-{index}"},
        )
    assert _append_root_task_projection_once(tmp_path, "projected-skill", original)

    rows = [
        json.loads(line)
        for line in (tmp_path / "state" / "skill_review_root_tasks.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert sum(
        row.get("root_task_id") == "root-original"
        and row.get("skill") == "projected-skill"
        and row.get("job_id") == "job-original"
        for row in rows
    ) == 1


def test_refused_slot_reuses_the_already_persisted_prompt(monkeypatch, tmp_path):
    """A seat refused before its physical send (here: the owner deadline is already
    spent) carries the prompt record it persisted, never a second synthetic one."""
    import ouroboros.review_substrate as substrate

    calls = []

    def _persist(*_args, **kwargs):
        calls.append(kwargs["call_type"])
        return {"manifest_ref": kwargs["call_id"]}

    monkeypatch.setattr(substrate, "persist_call", _persist)
    request = substrate.ReviewRequest(
        surface="task_acceptance",
        goal="review",
        task_id="prompt-reuse",
        deadline_at="2020-01-01T00:00:00+00:00",
    )
    actor = substrate.ReviewCoordinator(drive_root=tmp_path)._run_slot(
        request,
        substrate.ReviewSlot(slot_id="slot_1", model="reviewer"),
        operation_id="operation-1",
    )

    assert actor.status == "not_dispatched"
    assert actor.prompt_ref == {"manifest_ref": "operation-1_prompt"}
    assert len([call for call in calls if call.endswith("_prompt")]) == 1
    assert len(calls) == 2
