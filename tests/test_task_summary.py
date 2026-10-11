"""The free host facts row of ``ouroboros.agent_task_pipeline`` (no paid narrative).

Owner decision 2=A (TZ-2 C5) removed the paid "task summary" narrative. What
survives is `_record_task_facts`: one ``task_summary`` chat row of kind
``host_task_facts`` with the facts its readers need (chat_id, flat snapshot cost
fields, outcome axes, tool metrics, routing) and no prose, never labelled as an
authored narrative. Also covers the Light consolidation route the remaining
chat consolidation uses and `build_trace_summary` failure facts.
"""

import json
from types import SimpleNamespace

import pytest

import ouroboros.agent_task_pipeline as pipeline


def _rows(drive_logs):
    return [json.loads(line) for line in (drive_logs / "chat.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def no_model_calls(monkeypatch):
    monkeypatch.setattr("ouroboros.llm_observability.chat_observed",
                        lambda *_a, **_k: pytest.fail("the facts row buys no model call"))
    monkeypatch.setattr("ouroboros.llm.LLMClient", lambda *_a, **_k: pytest.fail("the facts row needs no client"))


def test_facts_row_buys_no_model_call_and_carries_the_facts(tmp_path, no_model_calls):
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    # Non-trivial (several rounds, a tool call): the old paid narrative path.
    pipeline._record_task_facts(
        env=None,
        task={"id": "task-123", "type": "task", "text": "Reply with exactly OK."},
        usage={"rounds": 3, "cost": 0.01, "result_status": "failed", "reason_code": "empty_final_text"},
        llm_trace={"tool_calls": [{"tool": "read_file", "args": {}}], "reasoning_notes": []},
        drive_logs=drive_logs,
    )

    [payload] = _rows(drive_logs)
    assert payload["type"] == "task_summary"
    assert payload["summary_kind"] == "host_task_facts"
    assert payload["summary_id"] == "task-facts:task-123"
    assert payload["text"] == ""  # no prose: the task text is not retold either
    assert payload["outcome_final"] is False
    assert payload["tool_calls"] == 1 and payload["tool_call_counts"] == {"read_file": 1}
    assert payload["rounds"] == 3
    assert payload["outcome_axes"]["execution"]["status"] == "failed"
    assert payload["outcome_axes"]["objective"]["status"] == "not_evaluated"
    assert payload["reason_code"] == "empty_final_text"
    assert "source_coverage" not in payload


def test_facts_row_is_never_an_authored_narrative_while_legacy_rows_still_resolve(tmp_path, no_model_calls):
    from ouroboros.main_context_authority import project_main_task_authority
    from ouroboros.project_dialogue import append_canonical_task_summary
    from ouroboros.task_results import load_task_result, write_task_result

    ref = {"kind": "task_result", "task_id": "", "reader": "get_task_result"}
    for task_id in ("new-root", "legacy-root"):
        write_task_result(tmp_path, task_id, "completed", result="R" * 200001)
    pipeline._record_task_facts(
        SimpleNamespace(drive_root=tmp_path),
        {"id": "new-root", "root_task_id": "new-root", "type": "task", "chat_id": 1, "text": "work"},
        {"rounds": 4, "cost": 0.0}, {"tool_calls": [{"tool": "read_file"}]}, tmp_path / "logs",
    )
    # A historical paid narrative keeps resolving through the unchanged reader.
    legacy_ref = {**ref, "task_id": "legacy-root"}
    assert append_canonical_task_summary(tmp_path, {
        "type": "task_summary", "summary_kind": "authored_root_summary",
        "summary_id": "task-narrative:legacy-root", "task_id": "legacy-root",
        "result_ref": legacy_ref, "source_coverage": {"task_result": legacy_ref},
        "text": "Legacy authored account",
    })

    assert "continuation_narrative" not in load_task_result(tmp_path, "new-root")

    def projected(task_id):
        authority = {"task_id": task_id, "result": "R" * 200001, "task_contract": {"objective": "old"},
                     "source": {**ref, "task_id": task_id, "arguments": {"task_id": task_id, "include_authority": True}}}
        return project_main_task_authority(
            {"id": "next", "predecessor_authority": authority}, drive_root=tmp_path,
        )["predecessor_authority"]["result"]

    new = projected("new-root")
    assert new["narrative_status"] == "unavailable"
    assert new["narrative_gap"]["kind"] == "continuation_narrative_unavailable"
    legacy = projected("legacy-root")
    assert legacy["narrative_status"] == "available"
    assert legacy["narrative"]["text"] == "Legacy authored account"


def test_facts_row_has_no_visible_summary_even_for_a_project(tmp_path, no_model_calls):
    drive_logs = tmp_path / "logs"
    pipeline._record_task_facts(
        None, {"id": "bound", "type": "task", "text": "Ship it", "chat_id": 1, "project_id": "launch"},
        {"rounds": 5, "cost": 0.0}, {"tool_calls": []}, drive_logs,
    )
    pipeline._record_task_facts(
        None, {"id": "unbound", "type": "task", "text": "Ship it", "chat_id": 1},
        {"rounds": 5, "cost": 0.0}, {"tool_calls": []}, drive_logs,
    )
    texts = {row["task_id"]: row["text"] for row in _rows(drive_logs)}
    assert texts == {"bound": "", "unbound": ""}


def test_facts_row_carries_chat_id_for_trivial_task(tmp_path, no_model_calls):
    """The facts row stamps the project chat_id, so it routes to its project
    thread on history reload instead of defaulting to the main chat."""
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)
    pipeline._record_task_facts(
        env=None,
        task={"id": "p1", "type": "task", "text": "hi", "chat_id": 1234},
        usage={"rounds": 1, "cost": 0.0, "result_status": "infra_failed", "reason_code": "llm_api_error"},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        drive_logs=drive_logs,
    )
    [summary] = [r for r in _rows(drive_logs) if r.get("type") == "task_summary"]
    assert summary["chat_id"] == 1234
    assert summary["text"] == ""  # the former trivial-task host line is gone
    assert summary["tool_calls"] == 0 and summary["rounds"] == 1
    assert summary["outcome_axes"]["execution"]["status"] == "infra_failed"
    assert summary["reason_code"] == "llm_api_error"


def test_facts_row_carries_flat_snapshot_cost_fields(tmp_path):
    """v6.82 P1: the task_summary chat row carries the pre-synthesis snapshot's
    flat cost fields so history replay can show honest card cost. Fields absent
    from the snapshot (cost_usd, cost_accounting_error) are never fabricated."""
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)
    snapshot_usage = {
        "rounds": 1,
        "cost": 0.0,
        # _pre_synthesis_usage_snapshot root-shape keys:
        "cost_snapshot_at": "2026-07-29T00:00:00Z",
        "cost_final": False,
        "cost_with_children_partial": True,
        "accounted_upper_bound_usd_with_children": 1.25,
        "reserved_usd": 0.1,
        "unresolved_upper_bound_usd": 0.2,
        "unknown_unmetered": 0,
        "ledger_integrity": "ok",
        "cost_accounting_status": "available",
    }
    pipeline._record_task_facts(
        env=None,
        task={"id": "p2", "type": "task", "text": "hi", "chat_id": 1},
        usage=snapshot_usage,
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        drive_logs=drive_logs,
    )
    row = next(r for r in _rows(drive_logs) if r.get("type") == "task_summary")
    assert row["cost_final"] is False
    assert row["cost_with_children_partial"] is True
    # ABI-3 fix-round-2: the snapshot producer emits the honest name only
    # (the legacy fixture spelling here was stale).
    assert row["accounted_upper_bound_usd_with_children"] == 1.25
    assert "cost_usd_with_children" not in row
    assert row["reserved_usd"] == 0.1
    assert row["unresolved_upper_bound_usd"] == 0.2
    assert row["unknown_unmetered"] == 0
    assert row["cost_accounting_status"] == "available"
    assert "cost_usd" not in row
    assert "cost_accounting_error" not in row


def test_facts_row_failure_is_contained(tmp_path, monkeypatch, caplog):
    """A failed append names the task and never raises into post-task work."""
    import ouroboros.project_dialogue as dialogue

    monkeypatch.setattr(dialogue, "append_canonical_task_summary",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    with caplog.at_level("WARNING"):
        pipeline._record_task_facts(None, {"id": "full-disk", "chat_id": 1}, {"rounds": 2}, {"tool_calls": []},
                                    tmp_path / "logs")
    assert "full-disk" in caplog.text


def test_consolidation_route_uses_configured_light_model_when_openrouter_present(monkeypatch):
    from ouroboros.consolidator import _consolidation_route

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    # Unprefixed provider/model ids use OpenRouter, so this Light model is
    # credentialed by the key above and MUST be kept verbatim. An ``openai::``
    # id would select the direct OpenAI transport instead — uncredentialed here
    # (no OPENAI_API_KEY) — and the documented provider-independence fallback in
    # resolve_credentialed_model() would then rewrite it to the first credentialed
    # slot, making the assertion depend on ambient OUROBOROS_MODEL* env leaked by
    # earlier tests in the same worker (the chronic v6.64.2..v6.65.4 CI red).
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai/gpt-5.5-mini")

    assert _consolidation_route() == ("openai/gpt-5.5-mini", False)


def test_consolidation_route_accepts_openai_compatible_when_legacy_base_url_is_present(monkeypatch):
    from ouroboros.consolidator import _consolidation_route

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic/claude-opus-4.6")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "openai-compatible::custom-model")
    monkeypatch.setenv("OUROBOROS_MODEL", "anthropic/claude-opus-4.6")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "anthropic/claude-opus-4.6")

    assert _consolidation_route() == ("openai-compatible::custom-model", False)


def test_build_trace_summary_shows_structured_failure_facts():
    trace = {
        "tool_calls": [{
            "tool": "run_command",
            "args": {"cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"]},
            "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=-9 (signal=SIGKILL).",
            "is_error": True,
            "status": "non_zero_exit",
            "exit_code": -9,
            "signal": "SIGKILL",
        }],
        "reasoning_notes": ["Thought this might still work."],
    }

    summary = pipeline.build_trace_summary(trace)

    assert "status=non_zero_exit" in summary
    assert "exit_code=-9" in summary
    assert "signal=SIGKILL" in summary
    assert "Agent notes (supplementary, not source of truth)" in summary

    long_trace = {
        "tool_calls": [
            {
                "tool": "run_command",
                "args": {"cmd": "x" * 5000},
                "is_error": False,
            }
            for _ in range(40)
        ],
        "reasoning_notes": ["note" * 2000],
    }
    assert "OMISSION NOTE" in pipeline.build_trace_summary(long_trace)


def test_facts_row_states_files_rescued_from_the_shared_unmeasured_listing(tmp_path, no_model_calls):
    """TZ-2 C2: at terminal the free facts row says how many files reached the task's
    artifact store — a positive count, a confirmed zero, or unknown — from the shared
    unmeasured listing, disclosing that no hash was computed. Store bookkeeping is not a
    rescued file, an empty readable manifest alone never proves zero (the listing does),
    something other than a store directory is unknown (never zero), and a split root lists
    the child-drive store too."""
    from ouroboros.headless import task_artifacts_dir

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir()

    def fact(task_id, env=None, **task_extra):
        pipeline._record_task_facts(env=env, task={"id": task_id, "chat_id": 1, **task_extra},
                                    usage={"rounds": 1}, llm_trace={"tool_calls": []}, drive_logs=drive_logs)
        [row] = [r for r in _rows(drive_logs) if r["summary_id"] == f"task-facts:{task_id}"]
        return row["files_rescued"]

    store = task_artifacts_dir(tmp_path, "pos-1")
    (store / "report.md").write_text("r", encoding="utf-8")
    (store / "nested").mkdir()
    (store / "nested" / "data.csv").write_text("1,2", encoding="utf-8")
    (store / ".artifact_manifest.json").write_text("{}", encoding="utf-8")
    (store / ".scratch_manifest.json").write_text("{}", encoding="utf-8")
    assert fact("pos-1") == {"count": 2, "state": "positive", "hash_computed": False,
                             "stores": [{"store": str(store), "count": 2, "readable": True}]}

    store = task_artifacts_dir(tmp_path, "zero-1")
    (store / ".artifact_manifest.json").write_text('{"schema_version": 1, "artifacts": {}}', encoding="utf-8")
    assert fact("zero-1") == {"count": 0, "state": "zero", "hash_computed": False,
                              "stores": [{"store": str(store), "count": 0, "readable": True}]}
    never_created = task_artifacts_dir(tmp_path, "none-1", create=False)
    assert fact("none-1")["state"] == "zero" and not never_created.exists()

    blocked = task_artifacts_dir(tmp_path, "unk-1", create=False)
    blocked.write_text("a file where the store directory should be", encoding="utf-8")
    assert fact("unk-1") == {"count": 0, "state": "unknown", "hash_computed": False,
                             "stores": [{"store": str(blocked), "count": 0, "readable": False}]}

    child = tmp_path / "child-drive"
    (task_artifacts_dir(child, "split-1") / "out.txt").write_text("o", encoding="utf-8")
    canonical = task_artifacts_dir(tmp_path, "split-1")
    assert fact("split-1", env=SimpleNamespace(drive_root=child), budget_drive_root=str(tmp_path)) == {
        "count": 1, "state": "positive", "hash_computed": False,
        "stores": [{"store": str(canonical), "count": 0, "readable": True},
                   {"store": str(task_artifacts_dir(child, "split-1", create=False)), "count": 1, "readable": True}]}


def test_rescued_files_count_listed_deliverables_never_store_metadata_inputs_or_receipts(tmp_path):
    """The count is the shared listing's: registration metadata, the scratch manifest, the
    receipt stream, staged inputs, chat media and source handles are not rescued files; an
    unregistered output is one; a registration whose file is gone is not a file."""
    from ouroboros.artifacts import _ARTIFACT_MANIFEST
    from ouroboros.headless import SCRATCH_MANIFEST_NAME, task_artifacts_dir
    from ouroboros.outcome_receipt_store import verification_receipts_path
    from ouroboros.task_finalization import rescued_files_fact

    store = task_artifacts_dir(tmp_path, "mix-1")
    (store / _ARTIFACT_MANIFEST).write_text(json.dumps({"schema_version": 1, "artifacts": {
        "report.md": {"kind": "task_artifact", "name": "report.md", "path": str(store / "report.md")},
        "gone.md": {"kind": "task_artifact", "name": "gone.md", "path": str(store / "gone.md")}}}), encoding="utf-8")
    (store / (_ARTIFACT_MANIFEST + ".lock")).write_text("", encoding="utf-8")
    (store / SCRATCH_MANIFEST_NAME).write_text("{}", encoding="utf-8")
    verification_receipts_path(tmp_path, "mix-1").write_text('{"check": "x"}\n', encoding="utf-8")
    for rel in ("attachments/brief.pdf", "chat_media/photo.png", "source_handles/tool_results/r.txt"):
        (store / rel).parent.mkdir(parents=True, exist_ok=True)
        (store / rel).write_text("input", encoding="utf-8")
    (store / "report.md").write_text("registered", encoding="utf-8")
    (store / "unregistered.csv").write_text("1,2", encoding="utf-8")
    (store / "nested").mkdir()
    (store / "nested" / "notes.txt").write_text("n", encoding="utf-8")

    assert rescued_files_fact("mix-1", [store]) == {
        "count": 3, "state": "positive", "hash_computed": False,
        "stores": [{"store": str(store), "count": 3, "readable": True}]}

    only_inputs = task_artifacts_dir(tmp_path, "inputs-1")
    (only_inputs / "attachments").mkdir()
    (only_inputs / "attachments" / "brief.pdf").write_text("input", encoding="utf-8")
    (only_inputs / _ARTIFACT_MANIFEST).write_text(json.dumps({"schema_version": 1, "artifacts": {
        "gone.md": {"kind": "task_artifact", "name": "gone.md"}}}), encoding="utf-8")
    assert rescued_files_fact("inputs-1", [only_inputs])["state"] == "zero"


def test_rescued_files_are_unknown_never_zero_when_the_listing_cannot_vouch(tmp_path, monkeypatch):
    """A corrupt registration, a failed tree read, a link where the store should be and a
    path that is not the task's store are each unknown with the readable stores' floor —
    never a confirmed zero; a store never created is zero."""
    import os

    from ouroboros import artifacts
    from ouroboros.headless import task_artifacts_dir
    from ouroboros.task_finalization import rescued_files_fact, rescued_files_sentence

    good = task_artifacts_dir(tmp_path, "unk-2")
    (good / "kept.txt").write_text("k", encoding="utf-8")
    corrupt_drive = tmp_path / "corrupt-drive"
    corrupt = task_artifacts_dir(corrupt_drive, "unk-2")
    (corrupt / "out.txt").write_text("o", encoding="utf-8")
    (corrupt / artifacts._ARTIFACT_MANIFEST).write_text('{"artifacts": {', encoding="utf-8")
    fact = rescued_files_fact("unk-2", [good, corrupt])
    assert fact == {"count": 1, "state": "unknown", "hash_computed": False,
                    "stores": [{"store": str(good), "count": 1, "readable": True},
                               {"store": str(corrupt), "count": 0, "readable": False}]}
    assert rescued_files_sentence(fact) == ("Files rescued: unknown — a task artifact store could not be "
                                            "listed; 1 listed before the failure (hashes not computed).")

    def unreadable_tree(_root):
        raise PermissionError("tree read denied")
        yield  # pragma: no cover - a generator that fails on first read

    monkeypatch.setattr(artifacts, "iter_artifact_tree", unreadable_tree)
    assert rescued_files_fact("unk-2", [good])["state"] == "unknown"
    monkeypatch.undo()

    assert rescued_files_fact("unk-2", [good.parent / "someone-else"])["state"] == "unknown"
    assert rescued_files_fact("unk-2", [tmp_path / "shallow"])["state"] == "unknown"
    never = task_artifacts_dir(tmp_path, "never-2", create=False)
    assert rescued_files_fact("never-2", [never]) == {
        "count": 0, "state": "zero", "hash_computed": False,
        "stores": [{"store": str(never), "count": 0, "readable": True}]}

    linked = task_artifacts_dir(tmp_path, "link-2", create=False)
    try:
        os.symlink(good, linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this platform")
    assert rescued_files_fact("link-2", [linked])["state"] == "unknown"


def test_rescued_files_count_physical_files_per_distinct_store_without_deduplicating(tmp_path):
    """The canonical store and a custom actor drive's store are both listed; the same
    relative path in each is two listed files (nothing proves one copy), and one store
    named twice is listed once by ``artifact_store_roots``."""
    from ouroboros.headless import task_artifacts_dir
    from ouroboros.task_finalization import artifact_store_roots, rescued_files_fact, rescued_files_sentence

    actor = tmp_path / "custom" / "actor-drive"
    for drive in (tmp_path, actor):
        (task_artifacts_dir(drive, "dup-1") / "out.txt").write_text("o", encoding="utf-8")
    stores = artifact_store_roots(tmp_path, "dup-1", task={"child_drive_root": str(actor)})
    assert stores == [task_artifacts_dir(tmp_path, "dup-1", create=False),
                      task_artifacts_dir(actor, "dup-1", create=False)]
    fact = rescued_files_fact("dup-1", stores)
    assert (fact["count"], fact["state"], [row["count"] for row in fact["stores"]]) == (2, "positive", [1, 1])
    assert rescued_files_sentence(fact) == "Files rescued: 2 listed in the task's artifact stores (hashes not computed)."
    assert artifact_store_roots(tmp_path, "dup-1", child_root=tmp_path) == [task_artifacts_dir(tmp_path, "dup-1", create=False)]


def test_rescued_files_fact_hashes_copies_registers_and_opens_nothing_but_the_registration(tmp_path, monkeypatch):
    """Pure: no hash, no copy, no registration write, and the only file whose content is
    read is the store's shared registration metadata."""
    import hashlib
    import io

    from ouroboros import artifacts
    from ouroboros.headless import task_artifacts_dir
    from ouroboros.task_finalization import rescued_files_fact

    store = task_artifacts_dir(tmp_path, "pure-1")
    (store / artifacts._ARTIFACT_MANIFEST).write_text(json.dumps({"schema_version": 1, "artifacts": {
        "a.bin": {"kind": "task_artifact", "name": "a.bin", "immutable": True, "size": 2, "sha256": "0" * 64}}}),
        encoding="utf-8")
    (store / "a.bin").write_bytes(b"ab")
    (store / "deep").mkdir()
    (store / "deep" / "b.bin").write_bytes(b"cd")

    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else b"")
                      for p in tmp_path.rglob("*"))

    before = snapshot()
    opened = []
    real_open = io.open

    def spying_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the rescued-files fact hashes, measures, copies or registers nothing")

    monkeypatch.setattr(io, "open", spying_open)
    monkeypatch.setattr(artifacts, "sha256", forbidden)
    monkeypatch.setattr(hashlib, "sha256", forbidden)
    for name in ("artifact_record", "stream_artifact_file", "_register_task_artifact_records",
                 "copy_file_to_task_artifacts", "update_json_locked"):
        monkeypatch.setattr(artifacts, name, forbidden)
    fact = rescued_files_fact("pure-1", [store])
    monkeypatch.undo()

    assert (fact["count"], fact["state"], fact["hash_computed"]) == (2, "positive", False)
    assert set(opened) <= {str(store / artifacts._ARTIFACT_MANIFEST)}, opened
    assert snapshot() == before
