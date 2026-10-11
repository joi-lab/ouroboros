"""Tests for the skill-review pass runner (P5) — esp. the chunked over-budget merge."""

import json

from ouroboros.skill_review_passes import (
    _skill_review_retry_key,
    run_skill_review_passes,
)


def _fake_build_prompt(ctx, drive_root, skill, *, manifest_dump, content_hash, file_pack, history, review_rebuttal):
    return f"STABLE::DYNAMIC[{file_pack}]", len("STABLE::"), {"adv": file_pack}


def _run(
    file_packs, run_review, required_items=(), *, models=None, row_plan=None,
    usage_attribution=None, review_contract_fingerprint="",
    rebuttal_sha256="",
):
    return run_skill_review_passes(
        None, None, None,
        evidence={"manifest_dump": "", "content_hash": "h", "history": [],
                  "review_rebuttal": "", "required_items": required_items},
        file_packs=file_packs, models=models or ["m"], row_plan=row_plan,
        session_root="/repo",
        usage_attribution=usage_attribution,
        review_contract_fingerprint=review_contract_fingerprint,
        rebuttal_sha256=rebuttal_sha256,
        build_prompt=_fake_build_prompt, run_review=run_review,
    )


def _actor(reason, verdict="PASS"):
    """A PARSEABLE reviewer actor whose `text` is the JSON findings array the real
    parser (parse_model_review_results) consumes."""
    return {"model": "m", "text": json.dumps([{"item": "x", "verdict": verdict, "reason": reason}])}


def test_session_contract_hash_tracks_retrieval_assignment(monkeypatch):
    import ouroboros.skill_review_passes as passes
    from ouroboros.review_execution import AgentSessionReviewExecutor

    before = passes.skill_review_session_contract_hash()
    assert before and len(before) == 64
    with monkeypatch.context() as changed:
        changed.setattr(passes, "_SESSION_RETRIEVAL", passes._SESSION_RETRIEVAL + "changed\n")
        assert before != passes.skill_review_session_contract_hash()
    with monkeypatch.context() as changed:
        changed.setattr(passes, "_SINGLE_CONTENT", passes._SINGLE_CONTENT + " changed")
        assert before != passes.skill_review_session_contract_hash()

    original = AgentSessionReviewExecutor.session_prompt

    def changed_prompt(self):
        return original.fget(self) + "changed"

    with monkeypatch.context() as changed:
        changed.setattr(AgentSessionReviewExecutor, "session_prompt", property(changed_prompt))
        assert before != passes.skill_review_session_contract_hash()


def test_single_pass_returns_review_object_verbatim():
    def fake_run_review(ctx, *, content, prompt, models, stable_prefix_len=0):
        return json.dumps({"model_count": 1, "results": [_actor("ok")]})

    _prompt, _adv, text, err = _run(["only"], fake_run_review)
    assert err == ""
    parsed = json.loads(text)
    assert parsed["results"][0]["model"] == "m"  # single pass returns the object as-is


def test_chunked_merges_results_into_one_object():
    # run_review returns the multi-model OBJECT {"results":[...]} per chunk (NOT a bare
    # array). The merged result must also be such an object, or the downstream
    # parse_model_review_results crashes on a list (the bug this guards).
    def fake_run_review(ctx, *, content, prompt, models, stable_prefix_len=0):
        pack = prompt[len("STABLE::DYNAMIC["):-1]
        return json.dumps({"model_count": 1, "results": [_actor(pack)]})

    _prompt, _adv, text, err = _run(["p1", "p2", "p3"], fake_run_review)
    assert err == ""
    parsed = json.loads(text)
    assert isinstance(parsed, dict) and "results" in parsed  # not a bare list
    assert len(parsed["results"]) == 3  # every chunk's record merged


def test_chunked_passes_keep_the_full_row_plan_and_exact_session_evidence():
    from ouroboros.review_execution import ReviewRouteKind

    row_plan = {
        "routes": [ReviewRouteKind.AGENT_SESSION, ReviewRouteKind.API_CHAT],
        "efforts": ["high", "medium"],
        "session_targets": ["codex=gpt-5.6-sol", ""],
        "session_profiles": ["profile-a", ""],
        "slot_ids": ["session-slot", "api-slot"],
    }
    calls = []

    def fake_run_review(ctx, **kwargs):
        calls.append(kwargs)
        return json.dumps({"results": [
            {"model": model, "text": json.dumps([
                {"item": "x", "verdict": "PASS", "reason": "ok"},
            ])}
            for model in kwargs["models"]
        ]})

    _prompt, _adv, _text, err = _run(
        ["p1", "p2"], fake_run_review, models=["session-model", "api-model"],
        row_plan=row_plan,
    )

    assert err == ""
    assert len(calls) == 2
    for idx, call in enumerate(calls, 1):
        assert call["routes"] == row_plan["routes"]
        assert call["row_plan"] is row_plan
        assert call["session_root"] == "/repo"
        assert "exact frozen skill evidence" in call["session_task"]
        assert f"DYNAMIC[p{idx}]" in call["session_task"]
        assert "STABLE::" not in call["session_task"]
        assert all(path in call["session_task"] for path in (
            "BIBLE.md", "docs/CHECKLISTS.md", "ouroboros/contracts/plugin_api.py"))
        assert "every physical chapter" in call["session_task"]
        assert "`Chapters` membership list in full, in the order you choose" in call["session_task"]
        assert "legacy monolith without `Chapters`" in call["session_task"]
        assert "physical file paths and file-local lines" in call["session_task"]
        assert f"PART {idx} of 2" in call["session_task"]


def test_retry_identity_separates_skill_waves_and_exact_chunks():
    row_plan = {"routes": []}

    def collect(wave_id, packs):
        keys = []

        def fake_run_review(ctx, **kwargs):
            keys.append(kwargs["retry_key"])
            return json.dumps({"results": [_actor("ok")]})

        _prompt, _adv, _text, error = _run(
            packs,
            fake_run_review,
            row_plan=row_plan,
            usage_attribution={
                "review_skill": "demo",
                "review_wave_id": wave_id,
            },
            review_contract_fingerprint="contract-1",
            rebuttal_sha256="rebuttal-1",
        )
        assert error == ""
        return keys

    first = collect("wave-a", ["same-pack", "same-pack"])
    replay = collect("wave-a", ["same-pack", "same-pack"])
    other_wave = collect("wave-b", ["same-pack", "same-pack"])

    assert first == replay
    assert all(key.startswith("skill_review:") for key in first)
    assert len(set(first)) == 2  # identical bytes remain distinct chunk positions
    assert set(first).isdisjoint(other_wave)


def test_retry_identity_binds_every_material_contract_field():
    base = {
        "skill_name": "demo",
        "wave_id": "wave-a",
        "content_hash": "content-a",
        "contract_fingerprint": "contract-a",
        "rebuttal_sha256": "rebuttal-a",
        "pack": "pack-a",
        "chunk_index": 0,
        "chunk_count": 2,
    }
    expected = _skill_review_retry_key(**base)
    mutations = {
        "skill_name": "other",
        "wave_id": "wave-b",
        "content_hash": "content-b",
        "contract_fingerprint": "contract-b",
        "rebuttal_sha256": "rebuttal-b",
        "pack": "pack-b",
        "chunk_index": 1,
        "chunk_count": 3,
    }
    for field, value in mutations.items():
        assert _skill_review_retry_key(**{**base, field: value}) != expected


def test_chunk_service_error_propagates_as_infra_error():
    def fake_run_review(ctx, *, content, prompt, models, stable_prefix_len=0):
        return json.dumps({"error": "boom"})

    _p, _a, text, err = _run(["p1", "p2"], fake_run_review)
    assert text == ""
    assert "service error" in err


def test_chunk_without_parseable_quorum_fails_closed():
    # A chunk where the reviewer returns an ERROR / unparseable response (not a real
    # parseable verdict) must fail the WHOLE review closed, not let the oversized skill
    # pass with that chunk effectively under-reviewed.
    def fake_run_review(ctx, *, content, prompt, models, stable_prefix_len=0):
        pack = prompt[len("STABLE::DYNAMIC["):-1]
        if pack == "p2":
            return json.dumps({"results": [{"model": "m", "verdict": "ERROR", "text": ""}]})
        return json.dumps({"results": [_actor(pack)]})

    _p, _a, text, err = _run(["p1", "p2"], fake_run_review)
    assert text == ""
    assert "parsed" in err
