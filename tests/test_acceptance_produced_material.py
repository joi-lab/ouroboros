"""Actual retained producer sources, never requirements or proposed actions."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros.acceptance_material import retain_acceptance_material
from ouroboros.artifacts import read_actor_source_bytes, store_task_artifact_bytes
from ouroboros.loop_acceptance import _latch_final_answer_marker, _task_acceptance_eligible
from ouroboros.outcomes import selected_final_answer


def context(tmp_path):
    return SimpleNamespace(drive_root=tmp_path, task_id="produced", repo_dir=None,
                           task_metadata={"answer_protocol": "final_answer_line"}, task_contract={})


def selected(ctx, trace, text):
    _latch_final_answer_marker(trace, text)
    return retain_acceptance_material(ctx, trace, text)


def test_answer_replacement_fallback_and_stale_latch_use_exact_sources(tmp_path):
    ctx, trace = context(tmp_path), {"tool_calls": []}
    first = selected(ctx, trace, "Reasoning.\nFINAL ANSWER: éclair")
    second = selected(ctx, trace, "Revised.\nFINAL ANSWER: π")
    assert first["selected_answer"]["source_ref"] != second["selected_answer"]["source_ref"]
    for material, value in ((first, "éclair"), (second, "π")):
        assert read_actor_source_bytes(tmp_path, ctx.task_id, material["selected_answer"]["source_ref"]) == value.encode()
    fallback = retain_acceptance_material(ctx, trace, "No new grounding; retaining the selected answer.")
    assert fallback["selected_answer"] == second["selected_answer"]
    assert selected_final_answer("", trace) == "π"
    trace["tool_calls"].append({"tool": "read_file", "status": "ok"})
    stale = retain_acceptance_material(ctx, trace, "New grounding without a selected answer.")
    assert stale["produced"] is False and "selected_answer" not in stale
    assert selected_final_answer("", trace) == ""


@pytest.mark.parametrize("text", ["", "   ", "Narrative without the typed protocol marker.", "FINAL ANSWER: "])
def test_empty_or_narrative_is_not_a_deliverable(tmp_path, text):
    ctx, trace = context(tmp_path), {"tool_calls": []}
    material = selected(ctx, trace, text)
    assert material["produced"] is False
    assert _task_acceptance_eligible("required", trace, False, task_contract={"expected_output": "42"})[0] is False


def test_failed_answer_retention_does_not_resurrect_old_source(tmp_path, monkeypatch):
    ctx, trace = context(tmp_path), {"tool_calls": []}
    assert selected(ctx, trace, "FINAL ANSWER: old")["produced"]
    monkeypatch.setattr("ouroboros.artifacts.persist_exact_text_source",
                        lambda *_a, **_k: ("", {}, {"reason": "fixture unavailable"}))
    material = selected(ctx, trace, "FINAL ANSWER: new")
    assert material["produced"] is False and "selected_answer" not in material
    assert material["issues"][0]["status"] == "source_unavailable"
    assert not _task_acceptance_eligible("required", trace, False)[0]


def test_registered_output_is_read_verified_and_frozen_before_eligibility(tmp_path):
    ctx, trace = context(tmp_path), {"tool_calls": []}
    published = store_task_artifact_bytes(tmp_path, ctx.task_id, "report.txt", b"produced report")
    material = retain_acceptance_material(ctx, trace, "Narration")
    assert _task_acceptance_eligible("required", trace, False)[0]
    ref = next(source["source_ref"] for source in material["sources"] if source["name"] == "report.txt")
    assert ref["sha256"] == published["sha256"]
    from ouroboros.artifacts import task_artifact_dir_path
    (task_artifact_dir_path(tmp_path, ctx.task_id) / "report.txt").write_bytes(b"replaced report")
    assert read_actor_source_bytes(tmp_path, ctx.task_id, ref) == b"produced report"
    changed = retain_acceptance_material(ctx, trace, "Narration")
    assert not changed["produced"] and changed["issues"]


def test_declared_process_outputs_without_a_producer_are_ineligible(tmp_path):
    ctx = context(tmp_path)
    trace = {"tool_calls": [{"tool": "run_command", "status": "ok", "is_error": False,
                             "args": {"outputs": ["missing.txt"]}, "result": "nothing produced"}]}
    retain_acceptance_material(ctx, trace, "")
    assert not _task_acceptance_eligible("required", trace, False)[0]


@pytest.mark.parametrize("method,ok,expected", [
    ("click", True, True), ("double_click", True, True), ("type_text", True, True),
    ("click", False, False), ("screenshot", True, False), ("cursor_position", True, False),
    ("remote_exec", True, False),
])
def test_gui_requires_a_successful_completed_input_receipt(tmp_path, method, ok, expected):
    from ouroboros.extension_surface_names import extension_surface_name
    ctx = context(tmp_path)
    # Private receipt parsing belongs to the producer; the shared reader consumes
    # only the documented execution fact, independent of extension/method names.
    payload = {"observed_effect": expected, "producer_receipt": {"ok": ok}}
    trace = {"tool_calls": [{"tool": extension_surface_name("unix_computer_use", method),
                             "tool_call_id": "executed", "is_error": False, "result": json.dumps(payload)}]}
    material = retain_acceptance_material(ctx, trace, "")
    assert material["produced"] is expected
    assert _task_acceptance_eligible("required", trace, False)[0] is expected
    if expected:
        ref = material["observed_effects"][0]["source_ref"]
        assert json.loads(read_actor_source_bytes(tmp_path, ctx.task_id, ref)) == payload


def test_proposed_gui_action_is_not_an_executed_effect(tmp_path):
    trace = {"tool_calls": []}
    material = retain_acceptance_material(context(tmp_path), trace, '{"action":"pyautogui.click(10, 20)"}')
    assert not material["produced"]
    assert not _task_acceptance_eligible("required", trace, False)[0]


@pytest.mark.parametrize("recorded", [True, False])
def test_forced_recorder_retains_replacement_before_existing_decision_return(tmp_path, monkeypatch, recorded):
    from ouroboros.loop_acceptance import _record_forced_acceptance_bypass

    ctx, trace = context(tmp_path), {"tool_calls": []}
    previous = selected(ctx, trace, "FINAL ANSWER: previous")["selected_answer"]
    ctx._delivery_candidate = SimpleNamespace(full_text="Changed result.\nFINAL ANSWER: replacement")
    ctx._task_acceptance_reviewed = not recorded
    if recorded:
        trace["acceptance_decision"] = {"status": "accepted", "reason": "clean_pass"}
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "off")
    collected = []
    monkeypatch.setattr("ouroboros.review_dispatch.reconcile_pending_acceptance_runs",
                        lambda actual, **_kw: collected.append(actual))
    monkeypatch.setattr("ouroboros.loop_acceptance.publish_acceptance_checkpoint", lambda *_a, **_k: None)
    _record_forced_acceptance_bypass(SimpleNamespace(
        tools=SimpleNamespace(_ctx=ctx), task_id=ctx.task_id,
        accumulated_usage={"reason_code": "deadline_local"},
    ), trace, "deadline_local")
    retained = trace["acceptance_material"]["selected_answer"]
    assert retained["text"] == "replacement" and retained["source_ref"] != previous["source_ref"]
    assert read_actor_source_bytes(tmp_path, ctx.task_id, retained["source_ref"]) == b"replacement"
    assert collected == [trace] and not trace.get("review_runs")


def test_reader_failure_stays_a_gap_instead_of_reusing_previous_material(tmp_path, monkeypatch):
    ctx, trace = context(tmp_path), {"tool_calls": []}
    store_task_artifact_bytes(tmp_path, ctx.task_id, "report.txt", b"produced report")
    assert retain_acceptance_material(ctx, trace, "")["produced"]

    def unreadable(*_a, **_k):
        raise RuntimeError("fixture reader unavailable")

    monkeypatch.setattr("ouroboros.artifacts.collect_task_artifact_records", unreadable)
    material = retain_acceptance_material(ctx, trace, "")
    assert not material["produced"] and material["sources"] == []
    assert material["issues"] == [{"status": "source_unavailable", "reason": "fixture reader unavailable",
                                   "tool": "artifact_manifest"}]


def test_repeated_capture_reuses_frozen_output_without_copying(tmp_path, monkeypatch):
    from ouroboros import artifacts
    from unittest.mock import Mock

    ctx, trace = context(tmp_path), {"tool_calls": []}
    # Several independently registered outputs, large enough to expose repeated IO.
    for index in range(8):
        store_task_artifact_bytes(tmp_path, ctx.task_id, f"report-{index}.bin", bytes([index]) * 131072)
    copy = Mock(wraps=artifacts.copy_artifact_file)
    monkeypatch.setattr(artifacts, "copy_artifact_file", copy)
    first = retain_acceptance_material(ctx, trace, "")
    assert copy.call_count == 8
    again = retain_acceptance_material(ctx, trace, "")
    assert again == first
    assert copy.call_count == 8


def test_unchanged_selected_source_is_not_registered_again(tmp_path, monkeypatch):
    from ouroboros import artifacts
    from unittest.mock import Mock

    ctx, trace = context(tmp_path), {"tool_calls": []}
    persist = Mock(wraps=artifacts.persist_exact_text_source)
    monkeypatch.setattr(artifacts, "persist_exact_text_source", persist)
    first = selected(ctx, trace, "FINAL ANSWER: exact selection")
    assert persist.call_count == 1
    assert selected(ctx, trace, "FINAL ANSWER: exact selection") == first
    assert persist.call_count == 1


def test_observation_does_not_capture_artifacts_or_git(tmp_path, monkeypatch):
    from ouroboros.loop_acceptance_review import prepare_acceptance_observation
    from ouroboros import artifacts
    from unittest.mock import Mock
    from ouroboros import mutation_attribution

    ctx, trace = context(tmp_path), {"tool_calls": []}
    ctx.repo_dir = tmp_path
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    store_task_artifact_bytes(tmp_path, ctx.task_id, "report.txt", b"produced report")
    collect = Mock(wraps=artifacts.collect_task_artifact_records)
    monkeypatch.setattr(artifacts, "collect_task_artifact_records", collect)
    git_capture = Mock(wraps=mutation_attribution.attributed_git_candidates)
    monkeypatch.setattr(mutation_attribution, "attributed_git_candidates", git_capture)
    schemas = [{"function": {"name": "task_acceptance_review"}}]
    for _ in range(10):
        prepare_acceptance_observation(ctx, trace, None, [], schemas)
    assert collect.call_count == 0
    assert git_capture.call_count == 0


def test_explicit_empty_answer_withdraws_previous_selection(tmp_path):
    ctx, trace = context(tmp_path), {"tool_calls": []}
    assert selected(ctx, trace, "FINAL ANSWER: previous")["produced"]
    empty = selected(ctx, trace, "Reconsidered.\nFINAL ANSWER: ")
    assert not empty["produced"] and "selected_answer" not in empty
    assert selected_final_answer("No replacement", trace) == ""


def test_effect_receipt_is_independent_of_extension_identity(tmp_path):
    from ouroboros.extension_surface_names import extension_surface_name
    ctx = context(tmp_path)
    receipt = '{"observed_effect":true,"receipt":"executed input"}'
    trace = {"tool_calls": [{"tool": extension_surface_name("independent_device", "apply_input"),
                             "tool_call_id": "effect", "result": receipt, "is_error": False}]}
    material = retain_acceptance_material(ctx, trace, "")
    assert material["produced"]
    ref = material["observed_effects"][0]["source_ref"]
    assert read_actor_source_bytes(tmp_path, ctx.task_id, ref) == receipt.encode()


def test_plain_extension_result_is_not_a_material_capture_failure(tmp_path):
    from ouroboros.extension_surface_names import extension_surface_name
    trace = {'tool_calls': [{'tool': extension_surface_name('notes_reader', 'read'),
                             'result': 'A successful plain text observation.', 'is_error': False}]}
    material = retain_acceptance_material(context(tmp_path), trace, '')
    assert not material['produced'] and not material['issues']


def test_cold_reader_keeps_selected_sources_after_promotion(tmp_path):
    from ouroboros.review_source_closure import retain_review_refs
    from ouroboros.artifacts import task_artifact_dir_path
    source, canonical = tmp_path / 'source', tmp_path / 'canonical'
    ctx, trace = context(source), {'tool_calls': []}
    store_task_artifact_bytes(source, ctx.task_id, 'report.txt', b'complete report')
    material = selected(ctx, trace, 'FINAL ANSWER: éclair')
    promoted = retain_review_refs({'acceptance_material': material}, source, canonical, ctx.task_id)
    # Retain rather than delete the original QA tree; its old address becomes unavailable.
    source.rename(tmp_path / 'retired-source')
    retained = promoted['acceptance_material']
    answer = retained['selected_answer']
    assert read_actor_source_bytes(canonical, ctx.task_id, answer['source_ref']) == 'éclair'.encode()
    output = next(row['source_ref'] for row in retained['sources'] if row['name'] == 'report.txt')
    assert read_actor_source_bytes(canonical, ctx.task_id, output) == b'complete report'
    (task_artifact_dir_path(canonical, ctx.task_id) / output['path']).write_bytes(b'corrupt')
    with pytest.raises((ValueError, OSError)):
        read_actor_source_bytes(canonical, ctx.task_id, output)


def test_a_new_id_retry_keeps_the_sources_of_the_work_it_continues(tmp_path, monkeypatch):
    """A retry under a new execution id restores the record of the work it continues: that
    work's sources keep their references and places, now in the retry's own store; what the
    retry produces itself replaces or extends them; a capture of its own that fails is a gap,
    never the continued answer; an unreadable continued source is a gap, never an identity."""
    import copy

    from ouroboros.artifacts import task_artifact_dir_path

    first, trace = context(tmp_path), {"tool_calls": []}
    store_task_artifact_bytes(tmp_path, first.task_id, "report.txt", b"produced report")
    paid = copy.deepcopy(selected(first, trace, "Work.\nFINAL ANSWER: 42"))
    assert [row["name"] for row in paid["sources"]] == ["selected-final-answer", "report.txt"]
    restored = copy.deepcopy(trace)  # what the checkpoint gives a new-id retry

    def retry(task_id):
        return SimpleNamespace(**{**vars(first), "task_id": task_id}), copy.deepcopy(restored)

    ctx, trace = retry("produced-retry")
    carried = selected(ctx, trace, "Work.\nFINAL ANSWER: 42")
    assert carried["sources"] == paid["sources"] and carried["selected_answer"]["text"] == "42"
    for row in carried["sources"]:
        assert read_actor_source_bytes(tmp_path, ctx.task_id, row["source_ref"], retained=False)
    assert selected(ctx, trace, "Work.\nFINAL ANSWER: 42")["sources"] == paid["sources"]  # a later pass

    store_task_artifact_bytes(tmp_path, ctx.task_id, "note.txt", b"new material")
    grown = selected(ctx, trace, "Work.\nFINAL ANSWER: 43")
    assert [row["name"] for row in grown["sources"]] == ["selected-final-answer", "report.txt", "note.txt"]
    assert grown["sources"][0] != paid["sources"][0] and grown["sources"][1] == paid["sources"][1]

    with monkeypatch.context() as patch:
        patch.setattr("ouroboros.artifacts.persist_exact_text_source",
                      lambda *_a, **_k: ("", {}, {"reason": "fixture unavailable"}))
        gap = selected(ctx, trace, "Work.\nFINAL ANSWER: 44")
    assert [row["name"] for row in gap["sources"]] == ["report.txt", "note.txt"] and "selected_answer" not in gap

    report = next(row["source_ref"] for row in paid["sources"] if row["name"] == "report.txt")
    (task_artifact_dir_path(tmp_path, first.task_id) / report["path"]).write_bytes(b"damaged")
    ctx, trace = retry("produced-retry-2")
    damaged = selected(ctx, trace, "Work.\nFINAL ANSWER: 42")
    assert [row["name"] for row in damaged["sources"]] == ["selected-final-answer"]
    assert damaged["issues"][-1]["tool"] == "continued_material"
