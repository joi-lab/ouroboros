"""Offline evidence and settlement guards for the live smoke's ordinary author task.

#2189 retired the direct-review helper and its automatic retry. The consumer now
exports exact shared-ledger sources and refuses ambiguous work after one task;
only an independent, substantive rebuttal can supply a second paid verdict.
"""

import json
from types import SimpleNamespace

import pytest

from ouroboros import review_ledger as ledger
from ouroboros.review_state import CommitAttemptRecord, update_state
from ouroboros.skill_loader import SkillReviewState, find_skill, save_review_state
from tests import skill_smoke_author as author
from tests import test_skill_smoke_official as smoke

NAME, TASK, MODEL = "probe", "smoke-task", smoke._REVIEW_MODEL


def _payload(data):
    path = data / "skills" / "ouroboroshub" / NAME
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: probe\ntype: instruction\n---\nInert diagnostic fixture.\n", encoding="utf-8")
    return find_skill(data, NAME)


def _wave(data, raw, *, status="clean", operation="settled", record_id="rl-current", rebuttal=""):
    """Persist the same three carriers the consumer reads: source, ledger, paid stamp."""
    skill = find_skill(data, NAME)
    response = ledger.retain_text_source(
        data, TASK, record_id=record_id, seat_id="critic", role="response", part="skill", text=raw)
    assert response["status"] == "retained"
    record = ledger.ReviewLedgerRecord(
        record_id=record_id, task_id=TASK, root_task_id=TASK, surface="skill", enforcement="blocking",
        subject={"skill": NAME, "tree_sha": skill.content_hash, "root": str(skill.skill_dir.resolve())},
        brief={"skill": {"group_id": f"task:{TASK}:{NAME}"}},
        fingerprints={"review_contract": "contract"},
        verdict={"aggregate": {"clean": "PASS", "pending": "QUORUM_FAILED", "blockers": "FAIL"}[status]},
        rows=[{"seat_id": "critic", "observed_model": MODEL,
               "status": "error" if status == "pending" else "responded",
               "operation_state": operation, "source_refs": [response]}])
    ledger.write_record(data, record)
    update_state(data, lambda state: state.attempts.append(CommitAttemptRecord(
        ts="2026-10-22T00:00:00Z", commit_message="", status="reviewed", task_id=TASK,
        root_task_id=TASK, tool_name="review_skill", paid=True, review_record_id=record_id,
        review_contract_fingerprint="contract", rebuttal_sha256=rebuttal,
        attempt=1 if record_id == "rl-current" else 2)))
    return record


def _save_review(data, *, status="clean", record_id="rl-current"):
    save_review_state(data, NAME, SkillReviewState(
        status=status, content_hash=find_skill(data, NAME).content_hash, record_id=record_id,
        reviewer_models=[MODEL], raw_actor_records=[{"model_id": MODEL, "raw_text": "persisted judgment"}]))


def _export(tmp_path, monkeypatch, data):
    destination = tmp_path / "diagnostics"
    monkeypatch.setenv("OUROBOROS_TEST_EVIDENCE_ROOT", str(destination))
    review = data / "state" / "skills" / NAME / "review.json"
    evidence = {"task_id": TASK, "original_hash": find_skill(data, NAME).content_hash,
                "review_history": author.load_review_waves(data, NAME),
                "review": json.loads(review.read_text(encoding="utf-8")) if review.exists() else None}
    assert not author.persist_author_evidence(tmp_path / "author", evidence, "unused-credential")
    [path] = destination.glob("skill-smoke-author/*/author-evidence.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("prior_review", [False, True])
@pytest.mark.parametrize("provider_error", [False, True])
def test_smoke_diagnostics_show_current_quorum_failure(tmp_path, monkeypatch, prior_review, provider_error):
    data = tmp_path / "data"
    skill = _payload(data)
    if prior_review:
        save_review_state(data, NAME, SkillReviewState(status="clean", content_hash="old-hash"))
    review = data / "state" / "skills" / NAME / "review.json"
    before = review.read_bytes() if prior_review else None
    raw = ("403 Inference is blocked on this account" if provider_error
           else "I have not completed the review") + " evidence" * 2000 + " END-WITNESS"
    _wave(data, raw, status="pending", operation="custody_lost" if provider_error else "settled")
    diagnostic = _export(tmp_path, monkeypatch, data)
    [wave] = diagnostic["review_history"]
    assert wave["status"] == "pending" and wave["content_hash"] == skill.content_hash
    assert wave["raw_actor_records"][0]["raw_text"] == raw
    assert (review.read_bytes() if review.exists() else None) == before
    with pytest.raises(AssertionError, match="pending/unknown review stays red"):
        author.assert_review_evidence(data, NAME, skill.content_hash, TASK, MODEL)


def test_smoke_diagnostics_keep_successful_review_actors(tmp_path, monkeypatch):
    data = tmp_path / "data"
    skill = _payload(data)
    raw = "complete successful review" + " evidence" * 2000 + " END-WITNESS"
    _wave(data, raw)
    _save_review(data)
    diagnostic = _export(tmp_path, monkeypatch, data)
    assert diagnostic["review"]["reviewer_models"] == [MODEL]
    assert diagnostic["review"]["raw_actor_records"] == [{"model_id": MODEL, "raw_text": "persisted judgment"}]
    assert diagnostic["review_history"][0]["raw_actor_records"][0]["raw_text"] == raw
    assert author.assert_review_evidence(data, NAME, skill.content_hash, TASK, MODEL)["status"] == "clean"


@pytest.mark.parametrize("case,error", [
    ("in_flight", "still unsettled"), ("custody_lost", "still unsettled"),
    ("pending_dispatch", "still unsettled"), ("late_result_pending", "still unsettled"),
    ("missing_history", "missing review"), ("wrong_hash", "material drift"),
    ("empty_records", "missing independent reviewer"), ("unknown_record", "settlement is unknown"),
    ("nested_pending", "still unsettled"), ("blockers", "criticism remains unresolved"),
    ("settled_error", "pending/unknown"), ("late_settled_error", "pending/unknown"),
    ("not_dispatched", "settlement is unknown"), ("unjustified_reroll", "substantive rebuttal"),
    ("clean", ""), ("late_settled_clean", ""), ("grounded_rebuttal", ""),
])
def test_smoke_author_preserves_original_wave_and_requires_settlement(tmp_path, monkeypatch, case, error):
    source = tmp_path / "source"
    _payload(source)
    root = tmp_path / "author"
    data = root / "data"
    lifecycle, original_records = [], {}
    original_loader = author.load_review_waves

    def finish(*_args, **_kwargs):
        lifecycle.append("wait")
        status = ("pending" if case in {"settled_error", "late_settled_error"} else
                  "blockers" if case in {"blockers", "grounded_rebuttal"} else "clean")
        operation = case if case in {"in_flight", "custody_lost", "pending_dispatch", "not_dispatched"} else "settled"
        if case in {"late_settled_error", "late_settled_clean"}:
            operation = "late_settled"
        if case != "missing_history":
            record = _wave(data, "Original full judgment", status=status, operation=operation)
            if case == "wrong_hash":
                record.subject["tree_sha"] = "other-bytes"
            elif case == "empty_records":
                record.rows = []
            elif case == "unknown_record":
                record.rows[0]["operation_state"] = ""
            elif case == "late_result_pending":
                record.rows[0]["late_result_pending"] = True
            record.revision += 1
            ledger.write_record(data, record)
        final_id = "rl-current"
        if case in {"grounded_rebuttal", "unjustified_reroll"}:
            final_id = "rl-reassessment"
            _wave(data, "Independent reassessment", record_id=final_id, rebuttal="source-grounded-argument")
            status = "clean"
        _save_review(data, status=status, record_id=final_id)
        for record_id in ("rl-current", final_id):
            path = ledger.record_path(data, record_id)
            if path.exists():
                original_records[path] = path.read_bytes()
        return {"status": "completed", "result": "Author prose claims PASS"}

    def waves(*args):
        history = original_loader(*args)
        if case == "nested_pending":
            history[0]["raw_actor_records"][0]["raw_results"] = [{"operation_state": "in_flight"}]
        return history

    def api(_url, method, path, _body):
        assert (method, path) == ("POST", "/api/tasks")
        lifecycle.append("post")
        return {"task_id": TASK}

    server = SimpleNamespace(start=lambda: lifecycle.append("start"), stop=lambda: lifecycle.append("stop"),
                             base_url="unused", wait_task=finish)
    monkeypatch.setattr(author, "SmokeAuthorServer", lambda *_args, **_kwargs: server)
    monkeypatch.setattr(author, "_api", api)
    monkeypatch.setattr(author, "load_review_waves", waves)
    monkeypatch.setenv("OUROBOROS_TEST_EVIDENCE_ROOT", str(tmp_path / "diagnostics"))
    kwargs = dict(candidate=SimpleNamespace(path=tmp_path / "repo"), source_data=source, root=root,
                  name=NAME, model=MODEL, timeout_sec=1, credential=("OPENROUTER_API_KEY", "synthetic-key"))
    if error:
        with pytest.raises(AssertionError, match=error):
            author.run_author_review(**kwargs)
    else:
        assert author.run_author_review(**kwargs)["review"]["status"] == "clean"
    assert lifecycle == ["start", "post", "wait", "stop"], "ambiguity must never start another author task"
    assert all(path.read_bytes() == before for path, before in original_records.items())
    assert not author.load_enabled(data, NAME)
    evidence = json.loads((root / "author-evidence.json").read_text(encoding="utf-8"))
    assert evidence["server_stopped"] and not evidence["evidence_errors"]
    assert [row["record_id"] for row in evidence["review_history"]] == (
        [] if case == "missing_history" else ["rl-current", "rl-reassessment"]
        if case in {"grounded_rebuttal", "unjustified_reroll"} else ["rl-current"])
