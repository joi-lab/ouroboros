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


def _frozen_skill(tmp_path):
    """A real payload, its loader hash, and the skill object the pass runner binds."""
    from types import SimpleNamespace

    from ouroboros.skill_loader import compute_content_hash

    skill_dir = tmp_path / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\ndescription: Fixture.\n---\nBody.\n", encoding="utf-8")
    skill = SimpleNamespace(name="demo", skill_dir=skill_dir, manifest=SimpleNamespace(entry="", scripts=[]))
    return skill, compute_content_hash(skill_dir)


def test_mixed_chunked_panel_sends_retrieving_seats_once_and_packets_every_chunk(tmp_path):
    from ouroboros.review_execution import ReviewRouteKind

    skill, content_hash = _frozen_skill(tmp_path)
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
            {"model": model, "slot_id": slot, "text": json.dumps([
                {"item": "x", "verdict": "PASS", "reason": "ok"},
            ])}
            for model, slot in zip(kwargs["models"], kwargs["row_plan"]["slot_ids"])
        ]})

    _prompt, _adv, text, err = run_skill_review_passes(
        None, tmp_path / "drive", skill,
        evidence={"manifest_dump": "", "content_hash": content_hash, "history": [],
                  "review_rebuttal": "", "required_items": ("x",)},
        file_packs=["p1", "p2"], models=["session-model", "api-model"], row_plan=row_plan,
        session_root="/repo", build_prompt=_fake_build_prompt, run_review=fake_run_review,
    )

    assert err == ""
    # The first pass carries both seats; the second only the packet seat.
    assert [call["models"] for call in calls] == [["session-model", "api-model"], ["api-model"]]
    assert [call["row_plan"]["slot_ids"] for call in calls] == [["session-slot", "api-slot"], ["api-slot"]]
    first, second = calls
    assert first["session_root"] == second["session_root"] == "/repo"
    session_task = first["row_plan"]["session_tasks"][0]
    assert first["row_plan"]["session_tasks"][1] == ""  # a packet seat receives the pack
    # The retrieving seat reviews the WHOLE payload once, never "PART i of n".
    assert "DYNAMIC[p1\n\np2]" in session_task and "PART 1 of 2" not in session_task
    assert "exact frozen copy" in session_task and "STABLE::" not in session_task
    assert all(path in session_task for path in (
        "BIBLE.md", "docs/CHECKLISTS.md", "ouroboros/contracts/plugin_api.py"))
    assert "every physical chapter" in session_task
    assert "`Chapters` membership list in full, in the order you choose" in session_task
    assert "legacy monolith without `Chapters`" in session_task
    assert "physical file paths and file-local lines" in session_task
    policy = first["row_plan"]["session_policies"][0]
    rows = policy["native_required_sources"]
    payload = [row for row in rows if row["disposition"] == "skill_payload"]
    assert [row["path"].rsplit("/", 1)[-1] for row in payload] == ["SKILL.md"]
    assert payload[0]["file"].startswith(policy["skill_payload_delivery"]["frozen_view"])
    assert {row["path"] for row in rows if row["disposition"] == "governance"} >= {
        "BIBLE.md", "docs/ARCHITECTURE.md", "docs/DEVELOPMENT.md", "docs/CREATING_SKILLS.md"}
    assert "PART 2 of 2" in second["content"]
    # Both merged chunks carry the packet seat; the retrieving seat answered once.
    assert [row["slot_id"] for row in json.loads(text)["results"]] == ["session-slot", "api-slot", "api-slot"]


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
