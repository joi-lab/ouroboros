"""Public Skill Review resumes exact paid producers in a new authorized lifecycle."""
import json
import threading
from types import SimpleNamespace

import pytest

from ouroboros import review_custody as custody, skill_review, skill_review_runner as runner
from ouroboros.review_dispatch import invoke_bound_api_review_paid_stamp
from ouroboros.review_execution import ReviewRouteKind
from ouroboros.skill_loader import load_review_state
from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.skill_exec import _handle_review_skill
from tests.test_skill_review_runner import _reset_queue


@pytest.fixture
def late_skill(tmp_path, monkeypatch):
    _reset_queue()
    skill_dir = tmp_path / "skills" / "late"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: late\ndescription: Test custody.\ntype: instruction\nversion: 1.0.0\n---\nExact payload.\n")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skill_dir.parent))
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", "1")
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    models = ["fake/one", "fake/two"]
    delivery = dict(models=models, routes=[ReviewRouteKind.API_CHAT] * 2,
                    efforts=["low"] * 2, session_targets=[""] * 2,
                    session_profiles=[""] * 2, slot_ids=["seat-a", "seat-b"])
    monkeypatch.setattr(skill_review, "commit_triad_delivery", lambda: delivery)
    monkeypatch.setattr(skill_review, "reviewer_slot_config_error", lambda: "")
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a: None)
    monkeypatch.setattr(skill_review, "_build_review_prompt_for_attempt",
                        lambda *a, **kw: ("Instructions\n" + kw["file_pack"], 13, {}))
    release, settled = threading.Event(), threading.Event()
    calls, completed = [], []
    rows = [dict(item=item, verdict="PASS", severity="critical", reason="Checked " + item)
            for item in skill_review._SKILL_REVIEW_ITEMS]
    rows[-1]["reason"] = "full source " * 4000 + "EOF-SKILL-VERDICT"
    result_text = json.dumps(rows)
    behavior = SimpleNamespace(before_wait=lambda: None, text=result_text,
                               wait=lambda kwargs: release.wait(10), expected_completions=2)
    class Slow:
        def chat(self, **kwargs):
            invoke_bound_api_review_paid_stamp()
            calls.append(kwargs)
            behavior.before_wait()
            assert behavior.wait(kwargs)
            return {"content": behavior.text}, {"prompt_tokens": 2, "completion_tokens": 1}
    monkeypatch.setattr("ouroboros.tools.review.LLMClient", Slow)
    original = custody._settle_review_attempt
    def settle(*args, **kwargs):
        original(*args, **kwargs)
        completed.append(1)
        if len(completed) == behavior.expected_completions:
            settled.set()
    monkeypatch.setattr(custody, "_settle_review_attempt", settle)
    monkeypatch.setattr(custody, "_logical_timeout", lambda *_: .05)
    def ctx():
        value = ToolContext(repo_dir=tmp_path, drive_root=tmp_path, task_id="skill-task")
        value.task_attempt = 1
        value.task_metadata = {"root_task_id": "skill-root"}
        return value
    history = tmp_path / "state/skills/late/review_history.jsonl"
    try:
        yield SimpleNamespace(root=tmp_path, skill_dir=skill_dir, ctx=ctx, release=release,
                              settled=settled, calls=calls, history=history, text=result_text,
                              delivery=delivery, behavior=behavior)
    finally:
        release.set()
        if calls:
            assert settled.wait(10)
        _reset_queue()


def test_public_skill_call_reaggregates_late_cas_at_cycle_cap(late_skill):
    h = late_skill
    first = _handle_review_skill(h.ctx(), skill="late")
    assert "pending" in first.lower()
    original = h.history.read_bytes()
    old = json.loads(original.splitlines()[-1])
    h.release.set()
    assert h.settled.wait(10)
    assert h.history.read_bytes() == original
    second = _handle_review_skill(h.ctx(), skill="late")
    state = load_review_state(h.root, "late")
    assert state.status == "clean", second
    assert len(h.calls) == 2
    assert h.history.read_bytes().startswith(original)
    new = json.loads(h.history.read_bytes().splitlines()[-1])
    assert new["job_id"] != old["job_id"]
    assert new["review_resume_of"] == old["wave_id"]
    assert new["review_wave"] == old["review_wave"]
    assert not new.get("paid")
    assert count_paid_skill_review_cycles(h.root, "late", "task:skill-root:late") == 1
    assert all(actor["raw_text"] == h.text for actor in state.raw_actor_records)
    assert not (h.root / "state/skills/late/enabled.json").exists()


@pytest.mark.parametrize("defect", ["missing", "partial", "tampered", "operation"])
def test_incomplete_skill_cas_never_authorizes_pass_or_another_send(late_skill, defect):
    from ouroboros.observability import call_manifest_path
    h = late_skill
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    old = json.loads(h.history.read_bytes().splitlines()[-1])
    operation = old["review_wave"]["chunks"][0]["operations"]["seat-a"]
    path = call_manifest_path(h.root, "skill-task", operation + "_response")
    manifest = json.loads(path.read_text())
    if defect == "missing":
        path.unlink()
    elif defect == "partial":
        manifest.pop("producer_complete")
        path.write_text(json.dumps(manifest))
    elif defect == "tampered":
        from pathlib import Path
        Path(manifest["full_payload_ref"]["path"]).write_bytes(b"bad gzip")
    else:
        old["review_wave"]["chunks"][0]["operations"]["seat-a"] = "wrong-operation"
        h.history.write_text(json.dumps(old) + "\n")
    original = h.history.read_bytes()
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "pending"
    assert len(h.calls) == 2
    assert h.history.read_bytes().startswith(original)


@pytest.mark.parametrize("axis", ["task", "attempt", "content", "contract", "rebuttal", "cancel", "superseded", "chunks"])
def test_unrelated_or_superseded_skill_intent_does_not_inherit(late_skill, monkeypatch, axis):
    h = late_skill
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    ctx, rebuttal = h.ctx(), ""
    if axis == "task": ctx.task_id = "new-task"
    if axis == "attempt": ctx.task_attempt = 2
    if axis == "content": (h.skill_dir / "SKILL.md").write_text((h.skill_dir / "SKILL.md").read_text() + "Changed.\n")
    if axis == "contract": h.delivery["efforts"] = ["high"] * 2
    if axis == "rebuttal": rebuttal = "Please assess this new counterargument."
    if axis in {"cancel", "superseded"}:
        row = json.loads(h.history.read_bytes().splitlines()[-1])
        row["job_status"] = "cancelled" if axis == "cancel" else "failed"
        if axis == "superseded": row["job_id"] = "unrelated-job"
        h.history.write_text(json.dumps(row) + "\n")
    if axis == "chunks":
        build = skill_review._build_skill_file_packs
        monkeypatch.setattr(skill_review, "_build_skill_file_packs", lambda *a, **kw: build(*a, **kw) + ["changed chunk"])
    original = h.history.read_bytes()
    _handle_review_skill(ctx, skill="late", review_rebuttal=rebuttal)
    assert not json.loads(h.history.read_bytes().splitlines()[-1]).get("review_resume_of")
    assert load_review_state(h.root, "late").status == "pending"
    assert len(h.calls) == 2
    assert h.history.read_bytes().startswith(original)


def test_new_root_buys_its_own_wave_instead_of_borrowing(late_skill, monkeypatch):
    h = late_skill
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    old = json.loads(h.history.read_bytes().splitlines()[-1])
    ctx = h.ctx()
    ctx.task_metadata = {"root_task_id": "new-root"}
    monkeypatch.setattr(custody, "_logical_timeout", lambda *_: 2)
    _handle_review_skill(ctx, skill="late")
    new = json.loads(h.history.read_bytes().splitlines()[-1])
    assert new["wave_id"] != old["wave_id"] and not new.get("review_resume_of")
    assert new["paid"] and len(h.calls) == 4
    assert load_review_state(h.root, "late").status == "clean"


def test_timeout_history_is_immutable_and_current_lifecycle_keeps_postconditions(late_skill, monkeypatch):
    h = late_skill
    guard, timed_out, postconditions = threading.Lock(), [], []
    def timeout():
        with guard:
            if not timed_out:
                runner._mark_review_job_timeout(h.root, "late", "", reason="lifecycle_timeout")
                timed_out.append(1)
    h.behavior.before_wait = timeout
    _handle_review_skill(h.ctx(), skill="late")
    original = h.history.read_bytes()
    old = json.loads(original.splitlines()[-1])
    assert old["status"] == "timeout" and old["paid"] and old["review_wave"]
    h.release.set()
    assert h.settled.wait(10)
    assert h.history.read_bytes() == original
    monkeypatch.setattr(runner, "_reconcile_deps_after_pass_review",
                        lambda *a, **kw: postconditions.append("deps") or ("not_required", ""))
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "clean"
    assert postconditions == ["deps"] and len(h.calls) == 2
    assert h.history.read_bytes().startswith(original)
    assert json.loads(h.history.read_bytes().splitlines()[-1])["review_resume_of"] == old["wave_id"]


def test_late_skill_fail_preserves_verdict_and_full_source(late_skill):
    h = late_skill
    rows = json.loads(h.text)
    rows[0]["verdict"] = "FAIL"
    h.behavior.text = json.dumps(rows)
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    state = load_review_state(h.root, "late")
    assert state.status == "blockers"
    assert all(actor["raw_text"] == h.behavior.text for actor in state.raw_actor_records)
    assert len(h.calls) == 2


def test_full_chunk_roster_is_reserved_before_first_paid_send(late_skill, monkeypatch):
    h = late_skill
    monkeypatch.setattr(skill_review, "_build_skill_file_packs", lambda *a, **kw: ["chunk a", "chunk b"])
    observed = []
    def inspect_roster():
        job = json.loads(runner.review_job_state_path(h.root, "late").read_text())
        chunks = job["review_wave"]["chunks"]
        assert len(chunks) == 2 and all(set(c["operations"]) == {"seat-a", "seat-b"} for c in chunks)
        assert len({op for chunk in chunks for op in chunk["operations"].values()}) == 4
        observed.append(chunks)
    h.behavior.before_wait = inspect_roster
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    # The unstarted second chunk has no complete producer; no synthetic quorum
    # and no dispatch are authorized by merely having reserved its operation id.
    assert load_review_state(h.root, "late").status == "pending"
    assert len(h.calls) == 2 and observed


def test_all_completed_chunks_reaggregate_once_without_a_new_paid_stamp(late_skill, monkeypatch):
    h = late_skill
    monkeypatch.setattr(skill_review, "_build_skill_file_packs", lambda *a, **kw: ["chunk a", "chunk b"])
    monkeypatch.setattr(custody, "_logical_timeout",
                        lambda slot, request, meta: 2 if "chunk a" in str(request.messages) else .05)
    h.behavior.expected_completions = 4
    h.behavior.wait = lambda kwargs: "chunk a" in str(kwargs["messages"]) or h.release.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    old = h.history.read_bytes()
    h.release.set()
    assert h.settled.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "clean"
    assert len(h.calls) == 4
    assert count_paid_skill_review_cycles(h.root, "late", "task:skill-root:late") == 1
    assert h.history.read_bytes().startswith(old)
