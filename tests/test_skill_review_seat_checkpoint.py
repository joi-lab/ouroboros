"""Skill-review resume collects its own recorded invocations (#1582).

Each seat's invocation token is bound into its reserved wave chunk before the POST;
a resume rebuilds no prompt and runs no advisory; the resumed wave's paid stamp writes
nothing; the lifecycle's terminal writers keep a checkpoint that landed meanwhile.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros.skill_review_passes import _chunk_checkpoint, run_skill_review_passes
from ouroboros.skill_review_runner import _mark_review_job_timeout, review_job_state_path


def _job(drive, *, status="running"):
    path = review_job_state_path(drive, "demo")
    path.write_text(json.dumps({"job_id": "job-1", "status": status, "review_wave": {
        "wave_id": "w1", "binding": {}, "chunks": [{"retry_key": "rk", "operations": {"t1": "op-1", "t2": "op-2"}}]}}))
    return path


def test_a_seat_token_binds_under_the_job_lock_before_its_post(tmp_path):
    path = _job(tmp_path)
    record = _chunk_checkpoint(tmp_path, "demo", "job-1", "w1", 0, "rk")
    record(surface="skill_review", slot_id="t1", operation_id="op-1", invocation_id="inv-1")
    record(surface="skill_review", slot_id="t1", operation_id="op-1", invocation_id="inv-1")  # idempotent
    chunk = json.loads(path.read_text())["review_wave"]["chunks"][0]
    assert chunk["invocations"] == {"t1": "inv-1"}
    for kwargs in ({"invocation_id": "inv-2"},  # a different token for the seat
                   {"operation_id": "op-other"},  # not the reserved operation
                   {"surface": "multi_model_review"}):
        with pytest.raises(RuntimeError):
            record(**{"surface": "skill_review", "slot_id": "t1", "operation_id": "op-1",
                      "invocation_id": "inv-1", **kwargs})
    _mark_review_job_timeout(tmp_path, "demo", "hash", reason="lifecycle_timeout", job_id="job-1")
    with pytest.raises(RuntimeError):  # a job no longer running sends nothing new
        record(surface="skill_review", slot_id="t2", operation_id="op-2", invocation_id="inv-3")
    stored = json.loads(path.read_text())
    assert stored["status"] == "timeout" and stored["review_wave"]["chunks"][0]["invocations"] == {"t1": "inv-1"}


def test_a_resume_rebuilds_no_prompt_and_rejoins_each_recorded_token(tmp_path, monkeypatch):
    seen = {}

    def build_prompt(*args, **kwargs):
        raise AssertionError("a resume must not rebuild the prompt or rerun the advisory")

    def run_review(ctx, **kwargs):
        raise AssertionError("collection must not enter the dispatch callable")

    def collect(ctx, drive_root, file_packs, *, required_items):
        seen.update(wave=ctx._skill_review_resume, packs=file_packs, root=drive_root)
        return {"results": [], "recorded_models": ["m1", "m2"]}

    monkeypatch.setattr("ouroboros.skill_review_collect.collect_skill_review_wave", collect)

    wave = {"wave_id": "w1", "chunks": [{"retry_key": "", "operations": {"t1": "op-1", "t2": "op-2"},
                                         "invocations": {"t1": "inv-1"}}]}
    ctx = SimpleNamespace(_skill_review_resume=wave, _skill_review_lifecycle_job_id="job-1")
    skill = SimpleNamespace(name="demo")
    attribution = {"review_skill": "demo", "review_wave_id": "w1"}
    from ouroboros import skill_review_passes

    wave["chunks"][0]["retry_key"] = skill_review_passes._skill_review_retry_key(
        skill_name="demo", wave_id="w1", content_hash="h", contract_fingerprint="cf", rebuttal_sha256="",
        pack="PACK", chunk_index=0, chunk_count=1)
    prompt, advisory, _text, error = run_skill_review_passes(
        ctx, tmp_path, skill, evidence={"manifest_dump": "{}", "content_hash": "h", "history": [],
                                        "review_rebuttal": "", "required_items": []},
        file_packs=["PACK"], models=["m1", "m2"], row_plan={"routes": [], "slot_ids": ["t1", "t2"]},
        usage_attribution=attribution, review_contract_fingerprint="cf", build_prompt=build_prompt,
        run_review=run_review)
    assert not error and prompt == "" and advisory["status"] == "not_rerun"
    assert seen["wave"] is wave
    assert seen["wave"]["chunks"][0]["invocations"] == {"t1": "inv-1"}
    assert seen["packs"] == ["PACK"] and seen["root"] == tmp_path


def test_the_resumed_wave_stamp_writes_no_second_marker(tmp_path, monkeypatch):
    from ouroboros import skill_review_history
    from ouroboros.skill_review_cycles import install_skill_dispatch_stamp, skill_wave_purchased

    monkeypatch.setattr(skill_review_history, "write_dispatch_marker",
                        lambda *a, **k: pytest.fail("a resume replays its paid wave; it buys nothing new"))
    ctx = SimpleNamespace(_skill_review_resume={"wave_id": "w1"})
    stamp, _previous = install_skill_dispatch_stamp(ctx, tmp_path, "demo", group_id="g", content_hash="h",
                                                    contract_fp="cf", rebuttal_sha="")
    stamp()  # an exact pending replay reaches the boundary: no raise, no marker
    assert stamp.wave_id == "w1"
    assert stamp.fired and not skill_wave_purchased(stamp)  # and no second paid fact


from tests.test_skill_review_late_cas import late_skill  # noqa: E402,F401 - imported pytest fixture


def test_a_failed_preflight_still_collects_the_resumed_wave_and_keeps_it_pending(late_skill, monkeypatch):  # noqa: F811
    from ouroboros import skill_review
    from ouroboros.skill_loader import load_review_state
    from ouroboros.skill_review_status import STATUS_PENDING
    from ouroboros.tools.skill_exec import _handle_review_skill

    h = late_skill
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    failure = skill_review.SkillReviewOutcome(
        skill_name="late", status=STATUS_PENDING, content_hash="x", error="deterministic skill_preflight failed",
        findings=[{"item": "skill_preflight", "verdict": "FAIL", "severity": "critical", "reason": "missing dep",
                   "model": "deterministic_preflight"}])
    monkeypatch.setattr(skill_review, "_run_deterministic_preflight", lambda *a, **k: failure)
    _handle_review_skill(h.ctx(), skill="late")
    state = load_review_state(h.root, "late")
    assert state.status == STATUS_PENDING  # collection never grants execution
    assert any(f.get("item") == "skill_preflight" for f in state.findings)
    assert any(f.get("model") != "deterministic_preflight" for f in state.findings)  # the paid findings survive
    assert len(h.calls) == 2  # nothing new was sent
    assert json.loads(h.history.read_bytes().splitlines()[-1]).get("review_resume_of")


def _started(drive, job_id="job-1"):
    """A lifecycle owner after its real ``_on_started``, its wave reserved under the job lock."""
    from ouroboros.skill_lifecycle_queue import LifecycleJob
    from ouroboros.skill_review_runner import _on_started
    from ouroboros.utils import update_json_locked

    started = {}
    job = LifecycleJob(id=job_id, kind="review", target="demo")
    job.status, job.started_at = "running", "2026-10-08T00:00:00+00:00"
    _on_started(drive, "demo", "hash", started, {"group_id": "manual:demo"})(job)
    update_json_locked(review_job_state_path(drive, "demo"), lambda current: {**current, "review_wave": {
        "wave_id": "w1", "binding": {}, "chunks": [{"retry_key": "rk", "operations": {"t1": "op-1", "t2": "op-2"}}]}})
    return job, started


def test_the_finisher_keeps_a_seat_token_bound_while_it_finished(tmp_path, monkeypatch):
    from ouroboros import skill_review_runner as runner
    from ouroboros.skill_review_history import load_history

    job, started = _started(tmp_path)
    record = _chunk_checkpoint(tmp_path, "demo", "job-1", "w1", 0, "rk")
    executions = runner._skill_review_executions

    def a_seat_posts_meanwhile(*args, **kwargs):
        # A delegated worker still preparing past its logical timeout binds its token and
        # POSTs while the lifecycle finishes; its finisher already started (#1582).
        record(surface="skill_review", slot_id="t1", operation_id="op-1", invocation_id="inv-late")
        return executions(*args, **kwargs)

    monkeypatch.setattr(runner, "_skill_review_executions", a_seat_posts_meanwhile)
    job.status, job.error, job.finished_at = "failed", "lifecycle deadline", "2026-10-08T00:01:00+00:00"
    runner._on_finished(tmp_path, "demo", "hash", started)(job, None, TimeoutError("lifecycle deadline"))
    stored = json.loads(review_job_state_path(tmp_path, "demo").read_text())
    assert stored["status"] == "failed" and stored["job_id"] == "job-1"
    assert stored["review_wave"]["chunks"][0]["invocations"] == {"t1": "inv-late"}
    row = load_history(tmp_path, "demo")[-1]  # what a resume selects its wave from
    assert row["job_id"] == "job-1" and row["review_wave"]["chunks"][0]["invocations"] == {"t1": "inv-late"}
    with pytest.raises(RuntimeError):  # a finished job sends nothing new
        record(surface="skill_review", slot_id="t2", operation_id="op-2", invocation_id="inv-after")


def test_a_finisher_never_writes_over_another_owners_job(tmp_path):
    from ouroboros import skill_review_runner as runner
    from ouroboros.utils import update_json_locked

    job, started = _started(tmp_path)
    # Healed as interrupted, then started again by another owner, before this finisher ran.
    update_json_locked(review_job_state_path(tmp_path, "demo"),
                       lambda current: {**current, "job_id": "job-2", "review_wave": {"wave_id": "w2"}})
    before = review_job_state_path(tmp_path, "demo").read_text()
    job.status = "succeeded"
    runner._on_finished(tmp_path, "demo", "hash", started)(job, None, None)
    assert review_job_state_path(tmp_path, "demo").read_text() == before
    events = [json.loads(line) for line in (tmp_path / "logs" / "events.jsonl").read_text().splitlines()]
    assert events[-1]["type"] == "skill_review_finish_skipped" and events[-1]["job_id"] == "job-1"
    assert events[-1]["reason"] == "review job id no longer matches lifecycle owner"


@pytest.mark.parametrize("caller", ["", "another-job"])
def test_a_timeout_marks_only_the_callers_own_job(tmp_path, caller):
    path = _job(tmp_path)
    before = path.read_text()
    _mark_review_job_timeout(tmp_path, "demo", "hash", reason="lifecycle_timeout", job_id=caller)
    assert path.read_text() == before  # same skill, any content hash: not this caller's job


def test_a_waiter_that_never_owned_the_lifecycle_leaves_the_owner_job_alone(tmp_path, monkeypatch):
    """A second review of the same payload times out waiting for the lifecycle the owner holds."""
    import os

    from ouroboros import skill_lifecycle_queue
    from ouroboros import skill_review_runner as runner
    from ouroboros.platform_layer import file_lock_exclusive_nb, file_unlock
    from ouroboros.tools.registry import ToolContext
    from ouroboros.utils import utc_now_iso
    from tests.test_skill_review_runner import _reset_queue

    _reset_queue()
    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Waiter.\ntype: instruction\nversion: 1.0.0\n---\nPayload.\n")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skill_dir.parent))
    waited = []
    dedupe = runner._review_dedupe_key
    monkeypatch.setattr(runner, "_review_dedupe_key", lambda name, digest: waited.append(digest) or dedupe(name, digest))
    monkeypatch.setattr(skill_lifecycle_queue, "_lifecycle_deadline_sec", lambda: 0.2)
    owner_hash = runner._skill_content_hash(tmp_path, "demo", str(skill_dir.parent))
    path = review_job_state_path(tmp_path, "demo")
    owner = {"job_id": "owner-a", "status": "running", "skill": "demo", "content_hash": owner_hash,
             "pid": os.getpid(), "started_at": utc_now_iso(), "last_heartbeat_at": utc_now_iso(),
             "review_wave": {"wave_id": "wa", "binding": {}, "chunks": [
                 {"retry_key": "rk", "operations": {"t1": "op-1"}, "invocations": {"t1": "inv-a"}}]}}
    path.write_text(json.dumps(owner))
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="waiter-task")
    ctx.task_metadata = {"root_task_id": "waiter-root"}
    review = []
    with (tmp_path / "state" / "skill_lifecycle.lock").open("a+") as held:  # the owner's lifecycle lock
        file_lock_exclusive_nb(held.fileno())
        try:
            with pytest.raises(TimeoutError):
                runner.run_skill_review_lifecycle_blocking(ctx, "demo", review_impl=lambda *a, **kw: review.append(1))
        finally:
            file_unlock(held.fileno())
            _reset_queue()
    assert waited == [owner_hash] and review == []  # the same payload; the waiter never ran
    assert json.loads(path.read_text()) == owner
    assert not (tmp_path / "state" / "skills" / "demo" / "review_history.jsonl").exists()
