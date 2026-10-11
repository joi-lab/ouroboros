"""D-trace (owner 4=A): patch apply/reject decisions are attested, not gated.

``integrate_delegated_patch`` applies a child's diff on five mechanical
manifest fields; no review facts exist anywhere on the path (the child is
forbidden from reviewing its own diff, and no host review of the captured
bytes runs). Rather than inventing a review, every verdict lands as a typed
``subagent_patch_verdict`` custody row and the acceptance packet carries the
host-attested disposition section — first-class, bounded, never squeezed
through the 4KB artifact-preview cliff. Nothing refuses an apply.
"""

import json

from ouroboros import delegate_custody as custody
from ouroboros.delegate_evidence import acceptance_patch_dispositions


def _emit_verdict(tmp_path, *, task_id="t-parent", child="run_r1",
                  pipeline="delegated", disposition="applied", applied=True,
                  reason="looks right", sha="abc123", write_failed=False):
    assert custody.emit(tmp_path, "delegate_run_patch_verdict", {
        "run_id": "", "task_id": task_id, "child_task_id": child,
        "pipeline": pipeline, "disposition": disposition, "applied": applied,
        "reason": reason, "patch_sha256": sha,
        "verdict_artifact_write_failed": write_failed,
    })


def test_no_rows_means_no_section_not_clean(tmp_path):
    assert acceptance_patch_dispositions(tmp_path, "t-parent") == {}


def test_dispositions_project_with_the_unreviewed_headline(tmp_path):
    _emit_verdict(tmp_path)
    _emit_verdict(tmp_path, child="t-sub", pipeline="subagent",
                  disposition="rejected", applied=False, reason="conflicts")
    section = acceptance_patch_dispositions(tmp_path, "t-parent")
    assert section["total"] == 2
    rows = {r["child"]: r for r in section["rows"]}
    assert rows["run_r1"]["applied"] is True
    assert rows["run_r1"]["pipeline"] == "delegated"
    assert rows["t-sub"]["applied"] is False
    # The honest headline: a delegated patch landed with no host review of
    # its bytes. Visibility only — nothing on the apply path refuses.
    assert section["unreviewed_delegated_apply"] is True


def test_rejected_only_delegated_rows_carry_no_apply_headline(tmp_path):
    _emit_verdict(tmp_path, disposition="rejected", applied=False)
    section = acceptance_patch_dispositions(tmp_path, "t-parent")
    assert "unreviewed_delegated_apply" not in section


def test_other_tasks_rows_stay_out(tmp_path):
    _emit_verdict(tmp_path, task_id="t-other")
    assert acceptance_patch_dispositions(tmp_path, "t-parent") == {}


def test_bounded_with_exact_omitted_count(tmp_path):
    for i in range(25):
        _emit_verdict(tmp_path, child=f"run_r{i}")
    section = acceptance_patch_dispositions(tmp_path, "t-parent")
    assert section["total"] == 25
    assert section["omitted"] == 5
    assert len(section["rows"]) == 20
    # Newest rows survive the bound (the most recent decisions matter most).
    assert section["rows"][-1]["child"] == "run_r24"


def test_headline_survives_when_the_only_delegated_apply_is_truncated(tmp_path):
    """fable M1: the ONE delegated apply sits among the oldest rows past the
    cap of 20; the headline must be computed over the complete set, or the
    panel loses the exact fact the attest decision exists to surface."""
    _emit_verdict(tmp_path, child="run_early", pipeline="delegated",
                  disposition="applied", applied=True)
    for i in range(24):
        _emit_verdict(tmp_path, child=f"t-sub{i}", pipeline="subagent",
                      disposition="rejected", applied=False)
    section = acceptance_patch_dispositions(tmp_path, "t-parent")
    assert section["omitted"] == 5
    assert all(r["child"] != "run_early" for r in section["rows"])
    assert section["unreviewed_delegated_apply"] is True


def test_write_verdict_emits_the_custody_row(tmp_path):
    from types import SimpleNamespace

    from ouroboros.tools.subagent_integration import _write_verdict

    ctx = SimpleNamespace(drive_root=str(tmp_path), task_id="t-parent",
                          task_metadata={}, budget_drive_root=str(tmp_path))
    path = _write_verdict(
        ctx, "run_r9", outcome="applied", reason="ok", files=["a.py"],
        manifest={"sha256": "deadbeef", "diffstat": "1 file"},
        applied=True, conflicts=[], protected=[],
    )
    assert path, "verdict artifact write should succeed in a temp drive"
    rows = [
        row for row in custody._iter_rows(custody.event_log_path(tmp_path))
        if row.get("type") == "delegate_run_patch_verdict"
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["child_task_id"] == "run_r9"
    assert row["pipeline"] == "delegated"
    assert row["patch_sha256"] == "deadbeef"
    assert row["applied"] is True
    assert row["verdict_artifact_write_failed"] is False


def test_verdict_artifact_content_still_written(tmp_path):
    from types import SimpleNamespace

    from ouroboros.tools.subagent_integration import _write_verdict

    ctx = SimpleNamespace(drive_root=str(tmp_path), task_id="t-parent",
                          task_metadata={}, budget_drive_root=str(tmp_path))
    path = _write_verdict(
        ctx, "child-7", outcome="rejected", reason="conflicts", files=[],
        manifest={}, applied=False, conflicts=["x"], protected=[],
    )
    data = json.loads(open(path).read())
    assert data["outcome"] == "rejected"
    assert data["child_task_id"] == "child-7"
