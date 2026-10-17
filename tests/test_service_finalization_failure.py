"""Unconfirmed stop stays an error in teardown, acceptance evidence and UI events."""
from __future__ import annotations

import json
import queue
from types import SimpleNamespace

import pytest

from ouroboros import loop_budget, loop_forced_finalization
from ouroboros.tools.registry import ToolContext


@pytest.mark.parametrize("live", [False, True])
def test_pending_stop_reaches_evidence_and_checkpoint_then_success_is_fresh(tmp_path, monkeypatch, live):
    from ouroboros import acceptance_preparation
    from ouroboros.tools import services

    tool_ctx = ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path, task_id="task")
    ctx = SimpleNamespace(drive_root=tmp_path, task_id="task", tools=SimpleNamespace(_ctx=tool_ctx),
                          drive_logs=tmp_path / "logs", event_queue=queue.Queue() if live else None, llm_trace={})
    pending = {"service_id": "task:svc", "name": "svc", "task_id": "task",
               "lifecycle": "cleanup_pending", "state": "exited", "stop_failed": True,
               "cleanup_dispatched": False, "stop_error": "unreadable /proc " + "x" * 2200}
    stopped = {"service_id": "task:svc", "name": "svc", "task_id": "task",
               "lifecycle": "stopped", "state": "stopped", "cleanup_dispatched": True}
    kept = {"service_id": "task:kept", "name": "kept", "lifecycle": "kept", "state": "running"}
    results = iter([[pending, kept], [dict(pending, uptime_sec=100), kept], [stopped]])
    monkeypatch.setattr(services, "stop_task_services", lambda _ctx: next(results))

    assert loop_budget._finalize_task_services(ctx) is True
    evidence = loop_budget._service_finalization_evidence(ctx.llm_trace)
    assert [event["kind"] for event in evidence] == ["service_finalization_error", "services_kept"]
    [row] = evidence[0]["services"]
    assert row["lifecycle"] == "cleanup_pending" and row["state"] == "exited"
    assert row["stop_failed"] is True and row["cleanup_dispatched"] is False
    assert len(row["stop_error"]) == 2000
    assert "OMISSION NOTE" in row["stop_error"]
    assert f"original length {len(pending['stop_error'])}" in row["stop_error"]
    before = acceptance_preparation.preparation_material(tool_ctx, ctx.llm_trace)
    assert loop_budget._finalize_task_services(ctx) is False
    assert acceptance_preparation.preparation_material(tool_ctx, ctx.llm_trace) == before
    assert loop_budget._finalize_task_services(ctx) is True
    assert acceptance_preparation.preparation_material(tool_ctx, ctx.llm_trace) != before
    evidence = loop_budget._service_finalization_evidence(ctx.llm_trace)
    assert [event["kind"] for event in evidence] == ["service_finalization_error", "services_kept", "services_stopped"]
    assert evidence[-1]["services"][0]["cleanup_dispatched"] is True
    if live:
        events = []
        while not ctx.event_queue.empty():
            events.append(ctx.event_queue.get_nowait()["data"])
    else:
        events = [json.loads(line) for line in (ctx.drive_logs / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event["checkpoint_kind"] for event in events] == [event["kind"] for event in evidence]


def test_wrapper_corrects_legacy_stopped_label_on_failed_executor_stop(tmp_path, monkeypatch):
    from ouroboros.tools import services

    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path / "data", task_id="task")
    monkeypatch.setattr(services, "_SERVICES", {})
    monkeypatch.setattr(services, "executor_stop_task_services", lambda _ctx: [
        {"service_id": "task:svc", "lifecycle": "stopped", "state": "exited",
         "stop_failed": True, "cleanup_dispatched": False, "stop_error": "surviving members: 43"}])
    [pending] = services.stop_task_services(ctx)
    assert pending["lifecycle"] == "cleanup_pending"
    assert pending["state"] == "exited" and pending["stop_error"] == "surviving members: 43"


def _ordinary_completion(tmp_path, monkeypatch, rows, answer):
    """Run the real ordinary rail: teardown holds the first answer, the author
    selects again and the host panel receives its packet. Returns both views
    and the author's own registry."""
    import ouroboros.loop as loop
    import ouroboros.review_substrate as review_substrate
    from ouroboros.tools import services
    from tests.test_delivery_forced_finalization import _forced_test_context, _select_completion

    _loop, registry, ctx, trace = _forced_test_context(tmp_path)
    registry._ctx.is_direct_chat = False
    registry._ctx._task_acceptance_reviewed = False
    results = iter([rows])
    monkeypatch.setattr(services, "stop_task_services", lambda _ctx: next(results, []))
    clean = review_substrate.ReviewRunResult(
        request={"surface": "task_acceptance", "policy": {"require_criterion_evidence": True}},
        actors=[{"signal": "PASS", "slot_id": "host-1", "parsed": {
            "outcome_tier": "solved", "completion_coach": "ship", "criteria_used": [{
                "criterion": "owner request", "status": "supported", "evidence_refs": ["artifact:1"]}]}}],
        parsed_findings=[], aggregate_signal="PASS")
    packets = []
    monkeypatch.setattr(loop, "get_task_review_mode", lambda: "auto")
    monkeypatch.setattr(loop, "_execute_task_acceptance_panel", lambda review_ctx: packets.append(review_ctx.evidence) or clean)
    monkeypatch.setattr(loop, "_compute_subagent_handoff", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_maybe_enforce_child_absorption_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(loop, "_maybe_inject_finalization_nudges", lambda *_a, **_k: False)
    # Auto reviews the author's typed nomination, never the declared expected_output (#1731).
    from tests.test_loop_acceptance_gate import _nominate_acceptance

    _nominate_acceptance(trace, task_id="parent1", subject="The service is stopped.")

    held = loop._no_tool_final_answer("The service is stopped.", ctx, trace, registry,
                                      queue.Queue(), set(), lambda _text: None)
    assert held is None and not packets and registry._ctx._delivery_control_required is True
    shown = [dict(message) for message in ctx.messages]
    _select_completion(registry, ctx, trace, answer=answer)
    # Even a clean PASS reaches the author before its decision (G1 #1737/D12); the
    # informed re-selection then delivers on the same paid panel.
    assert loop._no_tool_final_answer(answer, ctx, trace, registry, queue.Queue(), set(),
                                      lambda _text: None, explicit_candidate=True) is None
    from ouroboros.acceptance_settlement import expose_acceptance_feedback

    expose_acceptance_feedback(trace, ctx.messages, "parent1")  # the next Main request carried it
    _select_completion(registry, ctx, trace, answer=answer)
    text, _usage, _trace = loop._no_tool_final_answer(answer, ctx, trace, registry, queue.Queue(), set(),
                                                      lambda _text: None, explicit_candidate=True)
    assert text == answer  # Evidence for the author and panel, not a finality veto.
    [packet] = packets
    return shown, packet, registry


def test_ordinary_completion_shows_pending_cleanup_to_author_and_panel_without_veto(tmp_path, monkeypatch):
    pending = {"service_id": "parent1:svc", "name": "svc", "task_id": "parent1", "lifecycle": "cleanup_pending",
               "state": "exited", "stop_failed": True, "cleanup_dispatched": False,
               "stop_error": "surviving process group members: 43"}
    messages, packet, _registry = _ordinary_completion(
        tmp_path, monkeypatch, [pending], "Service cleanup is unconfirmed: process 43 survived the stop.")
    shown = str(messages)
    assert "[SERVICE_FINALIZATION_EVIDENCE]" in shown
    assert "cleanup_pending" in shown and "surviving process group members: 43" in shown
    [row] = packet["service_finalization"]
    assert row["kind"] == "service_finalization_error" and packet["__provenance__"]["service_finalization"] == "host_attested"
    assert row["services"][0]["lifecycle"] == "cleanup_pending"
    assert row["services"][0]["stop_error"] == "surviving process group members: 43"


def test_acceptance_packet_carries_service_section_only_when_teardown_recorded(tmp_path):
    from ouroboros.review_evidence import build_task_acceptance_evidence

    tool_ctx = ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path, task_id="task")
    quiet = build_task_acceptance_evidence(tool_ctx, llm_trace={"tool_calls": []}, drive_root=tmp_path, task_id="task")
    assert "service_finalization" not in quiet
    trace = {"tool_calls": [], "verification_events": [{"kind": "services_stopped", "services": [
        {"service_id": "task:svc", "lifecycle": "stopped", "state": "stopped", "pid": 7, "log_path": "/private",
         "artifact_outputs": "report.html is missing: " + "x" * 9000}]}]}
    packet = build_task_acceptance_evidence(tool_ctx, llm_trace=trace, drive_root=tmp_path, task_id="task")
    # Source-complete task facts use the existing packet/context fitting layer.
    assert packet["service_finalization"] == loop_forced_finalization._service_finalization_projection(
        trace, drive_root=tmp_path, task_id="task")
    [row] = packet["service_finalization"]
    assert row["services"][0]["artifact_outputs"] == trace["verification_events"][0]["services"][0]["artifact_outputs"]
    rendered = json.dumps(packet["service_finalization"])
    assert "report.html is missing" in rendered and "log_path" not in rendered


def test_only_a_host_attested_section_is_a_closure_edge():
    from ouroboros.review_source_closure import source_carrier

    for provenance, carrier in (("host_attested", "metadata"), ("agent_supplied", ""), (None, "")):
        evidence = {"service_finalization": [], "__provenance__": {"service_finalization": provenance}}
        assert source_carrier(evidence, "service_finalization", "evidence") == carrier


def test_service_disclosure_carries_and_fingerprints_full_projection():
    ctx = SimpleNamespace(messages=[], forced_service_evidence_fingerprint="")
    service = {"service_id": "task:svc", "lifecycle": "cleanup_pending",
               "artifact_outputs": "x" * 10000 + "a"}
    trace = {"verification_events": [{"kind": "service_finalization_error", "services": [service]}]}
    preview = loop_forced_finalization._service_finalization_text(trace)
    loop_forced_finalization._disclose_service_finalization(ctx, trace)
    first_fingerprint = ctx.forced_service_evidence_fingerprint
    service["artifact_outputs"] = "x" * 10000 + "b"
    assert loop_forced_finalization._service_finalization_text(trace) != preview
    loop_forced_finalization._disclose_service_finalization(ctx, trace)
    assert ctx.forced_service_evidence_fingerprint != first_fingerprint
    assert str(ctx.messages).count("[SERVICE_FINALIZATION_EVIDENCE]") == 2


def test_cleanup_failure_after_long_successes_reaches_author_and_acceptance(tmp_path):
    from ouroboros.review_evidence import build_task_acceptance_evidence

    stopped = {"kind": "services_stopped", "services": [
        {"service_id": f"task:success-{i}", "lifecycle": "stopped", "artifact_outputs": "x" * 1000}
        for i in range(12)]}
    pending = {"kind": "service_finalization_error", "services": [
        {"service_id": "task:late-failure", "lifecycle": "cleanup_pending", "stop_failed": True,
         "cleanup_dispatched": False, "stop_error": "surviving members: 43"}]}
    trace = {"tool_calls": [], "verification_events": [stopped, pending]}
    ctx = SimpleNamespace(messages=[], forced_service_evidence_fingerprint="", drive_root=tmp_path, task_id="task")
    loop_forced_finalization._disclose_service_finalization(ctx, trace)
    assert "task:late-failure" in str(ctx.messages) and "surviving members: 43" in str(ctx.messages)
    packet = build_task_acceptance_evidence(ToolContext(repo_dir=tmp_path / "repo", drive_root=tmp_path,
                                                      task_id="task"), llm_trace=trace, drive_root=tmp_path, task_id="task")
    rows = packet["service_finalization"]
    assert len(rows[0]["services"]) == 12
    assert rows[-1]["kind"] == pending["kind"]
    assert rows[-1]["services"] == pending["services"]
    assert packet["__provenance__"]["service_finalization"] == "host_attested"


def _finalized_log(drive_root, name, text):
    """Real local log finalization: masked tail, masked full gzip blob, live log removed."""
    from ouroboros.tools import services

    log = drive_root / "services" / "parent1" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(text, encoding="utf-8")
    return services._finalize_service_log_for_drive(drive_root, SimpleNamespace(secret_values=(), log_path=log))


def _chatty_rows(drive_root, monkeypatch, count=12):
    """Many services whose 80k display tails are windows of longer retained logs,
    plus both capture gaps and a Docker row whose log exists only in the container."""
    from ouroboros.tools import services

    def refuse_blob(*_args, **_kwargs):
        raise OSError("blob store full")

    rows = [{"service_id": f"parent1:svc{i}", "name": f"svc{i}", "task_id": "parent1", "lifecycle": "stopped",
             "log_finalization": _finalized_log(drive_root, f"svc{i}", f"HEAD-{i}\n" + "x" * 90_000 + f"\nLAST-{i}\n")}
            for i in range(count)]
    with monkeypatch.context() as patch:
        patch.setattr(services, "_MAX_SERVICE_LOG_BLOB_BYTES", 1000)
        oversized = _finalized_log(drive_root, "oversized", "OVERSIZED-HEAD\n" + "y" * 4000 + "\nOVERSIZED-LAST\n")
    with monkeypatch.context() as patch:
        patch.setattr(services, "write_blob", refuse_blob)
        failed = _finalized_log(drive_root, "failed", "FAILED-HEAD\n" + "z" * 4000 + "\nFAILED-LAST\n")
    return rows + [
        {"service_id": "parent1:oversized", "name": "oversized", "task_id": "parent1", "lifecycle": "stopped",
         "log_finalization": oversized},
        {"service_id": "parent1:failed", "name": "failed", "task_id": "parent1", "lifecycle": "stopped",
         "log_finalization": failed},
        {"service_id": "parent1:docker", "name": "docker", "task_id": "parent1", "lifecycle": "stopped",
         "state": "stopped", "cleanup_dispatched": True, "executor": {"type": "docker_exec"},
         "backend_log_path": "/tmp/ouroboros-service-docker.log"},
    ]


def _author_evidence(messages):
    """The projection and its size inside the author's disclosure, merged or not."""
    texts = []
    for message in messages:
        content = message.get("content")
        for part in content if isinstance(content, list) else [content]:
            texts.append(part.get("text", "") if isinstance(part, dict) else str(part or ""))
    [text] = [text for text in texts if "[SERVICE_FINALIZATION_EVIDENCE]" in text]
    start = text.index("honestly:\n", text.index("[SERVICE_FINALIZATION_EVIDENCE]")) + len("honestly:\n")
    return json.JSONDecoder().raw_decode(text[start:])


def _assert_retained_tails_by_ref(read, rows, size):
    """Each compact row is complete through ``read`` (the consumer's read_file),
    the source is byte-identical to the retained blob, and every gap keeps its tail."""
    shown = {service["service_id"]: service for row in rows for service in row["services"]}
    assert set(shown) == {f"parent1:svc{i}" for i in range(12)} | {"parent1:oversized", "parent1:failed", "parent1:docker"}
    for i in range(12):
        log = shown[f"parent1:svc{i}"]["log_finalization"]
        assert "tail" not in log and log["tail_chars"] == 80_000 and log["deleted_live_log"] is True
        source = log["source_ref"]
        assert source["kind"] == "task_source" and source["read"]["tool"] == "read_file"
        assert (source["sha256"], source["size"]) == (log["full_log_ref"]["sha256"], log["full_log_ref"]["size"])
        # Readable by the actual reader, including the head that the 80k tail never held.
        text = read(source["read"]["arguments"])
        assert "lines 1–3 of 3" in text and f"HEAD-{i}" in text and f"LAST-{i}" in text
    oversized, failed = shown["parent1:oversized"]["log_finalization"], shown["parent1:failed"]["log_finalization"]
    assert "OVERSIZED-LAST" in oversized["tail"] and "blob cap" in oversized["full_log_omitted"]
    assert oversized["retained_live_log_path"] and not oversized["full_log_ref"] and "source_ref" not in oversized
    assert "FAILED-LAST" in failed["tail"] and "blob store full" in failed["errors"][0] and "source_ref" not in failed
    assert "log_finalization" not in shown["parent1:docker"]  # an in-container path is no retained source
    assert "ouroboros-service-docker.log" not in json.dumps(rows)
    assert size < 40_000  # twelve 80k tails would be 960k


def _tool_reader(registry):
    return lambda arguments: registry.execute("read_file", arguments)


def test_many_chatty_services_reach_ordinary_author_and_panel_by_readable_ref(tmp_path, monkeypatch):
    rows = _chatty_rows(tmp_path, monkeypatch)
    assert sum(len(row.get("log_finalization", {}).get("tail", "")) for row in rows) > 12 * 80_000
    messages, packet, registry = _ordinary_completion(tmp_path, monkeypatch, rows,
                                                      "Twelve previews stopped; two logs have gaps.")
    _assert_retained_tails_by_ref(_tool_reader(registry), *_author_evidence(messages))
    _assert_retained_tails_by_ref(_tool_reader(registry), packet["service_finalization"],
                                  len(json.dumps(packet["service_finalization"])))
    assert packet["__provenance__"]["service_finalization"] == "host_attested"


def test_many_chatty_services_reach_the_forced_author_by_readable_ref(tmp_path, monkeypatch):
    from ouroboros.review_evidence import build_task_acceptance_evidence
    from ouroboros.tools import services
    from tests.test_delivery_forced_finalization import _forced_test_context

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    rows = _chatty_rows(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "stop_task_services", lambda _ctx: rows)
    sent = []
    monkeypatch.setattr(loop, "call_llm_with_retry", lambda _llm, messages, *_a, **_k: (
        sent.append([dict(message) for message in messages])
        or ({"role": "assistant", "content": "Twelve previews stopped; two logs have gaps."}, 0.0)))
    text, _usage, returned = loop._forced_final_answer(ctx, prompt="finalize", fallback_text="fallback",
                                                       reason_code="deadline_local")
    assert text == "Twelve previews stopped; two logs have gaps."
    _assert_retained_tails_by_ref(_tool_reader(registry), *_author_evidence(sent[0]))
    # Raw teardown evidence keeps every tail; only the shown projection is compact.
    raw = returned["verification_events"][0]["services"]
    assert all(len(row["log_finalization"]["tail"]) == 80_000 for row in raw[:12])
    packet = build_task_acceptance_evidence(registry._ctx, llm_trace=returned, drive_root=tmp_path, task_id="parent1")
    _assert_retained_tails_by_ref(_tool_reader(registry), packet["service_finalization"],
                                  len(json.dumps(packet["service_finalization"])))


def test_display_drops_a_tail_only_for_a_readable_source_and_identity_sees_hidden_changes(tmp_path):
    import copy
    import pathlib

    log = _finalized_log(tmp_path, "svc", "HEAD\n" + "x" * 90_000 + "\nLAST\n")
    trace = {"verification_events": [{"kind": "services_stopped", "services": [
        {"service_id": "parent1:svc", "lifecycle": "stopped", "log_finalization": log}]}]}
    changed = copy.deepcopy(trace)
    changed_log = changed["verification_events"][0]["services"][0]["log_finalization"]
    changed_log["tail"] = changed_log["tail"][:-len("late line")] + "late line"  # same size, hidden by display
    display = loop_forced_finalization._service_finalization_text
    store = {"drive_root": tmp_path, "task_id": "parent1"}
    assert display(trace, **store) == display(changed, **store)
    assert loop_budget._service_finalization_evidence(trace) != loop_budget._service_finalization_evidence(changed)
    ctx = SimpleNamespace(messages=[], forced_service_evidence_fingerprint="", **store)
    loop_forced_finalization._disclose_service_finalization(ctx, trace)
    loop_forced_finalization._disclose_service_finalization(ctx, changed)
    assert str(ctx.messages).count("[SERVICE_FINALIZATION_EVIDENCE]") == 2
    # Without a task store no reader exists for the copy: the tail stays.
    [row] = json.loads(display(trace))
    assert row["services"][0]["log_finalization"] == log
    pathlib.Path(log["full_log_ref"]["path"]).unlink()  # an unreadable blob is no retained source
    [row] = json.loads(display(trace, **store))
    shown = row["services"][0]["log_finalization"]
    assert shown["tail"] == log["tail"] and "source_ref" not in shown
    assert shown["full_log_ref"] == {"kind": "txt", "sha256": log["full_log_ref"]["sha256"], "encoding": "gzip",
                                     "size": log["full_log_ref"]["size"], "availability": "unavailable",
                                     "reason": "FileNotFoundError"}


@pytest.mark.parametrize("many", [False, True], ids=["one-small-row", "many-chatty-rows"])
def test_retrieving_reviewer_reads_complete_logs_after_the_author_drive_is_gone(tmp_path, monkeypatch, many):
    """The packet's host-attested rows are typed closure edges: the review closure
    retains each log source, and the native reviewer's read_file reads it after
    the author's execution drive is deleted. Gaps stay tails and refuse nothing."""
    import pathlib
    import shutil

    from ouroboros import artifacts, observability
    from ouroboros.review_evidence import build_task_acceptance_evidence
    from ouroboros.review_native_episode import inspection_registry
    from ouroboros.review_records import ReviewRequest
    from ouroboros.review_source_closure import retain_review_request_sources
    from ouroboros.task_results import write_task_result

    author, canonical, repo = tmp_path / "author", tmp_path / "canonical", tmp_path / "repo"
    repo.mkdir()
    write_task_result(author, "parent1", "running", description="author record")
    if many:
        rows = _chatty_rows(author, monkeypatch)
        rows.append({"service_id": "parent1:lost", "name": "lost", "task_id": "parent1", "lifecycle": "stopped",
                     "log_finalization": _finalized_log(author, "lost", "LOST-HEAD\nLOST-LAST\n")})
        pathlib.Path(rows[-1]["log_finalization"]["full_log_ref"]["path"]).unlink()
    else:
        rows = [{"service_id": "parent1:small", "name": "small", "task_id": "parent1", "lifecycle": "stopped",
                 "log_finalization": _finalized_log(author, "small", "HEAD-small\nLAST-small\n")}]
    trace = {"tool_calls": [], "verification_events": [{"kind": "services_stopped", "services": rows}]}
    packet = build_task_acceptance_evidence(ToolContext(repo_dir=repo, drive_root=author, task_id="parent1"),
                                            llm_trace=trace, drive_root=author, task_id="parent1")
    request = ReviewRequest(surface="task_acceptance", task_id="parent1", goal="review", subject="answer",
                            evidence=packet, policy={})
    retain_review_request_sources(request, source_root=author, custody_root=canonical)
    shutil.rmtree(author)

    read_root = pathlib.Path(request.policy["native_data_root"])
    registry, _ctx, _schemas = inspection_registry(str(repo), str(read_root), "parent1")
    refmap = request.policy["review_source_closure"]["refmap"]
    services = {service["service_id"]: service for row in request.evidence["service_finalization"]
                for service in row["services"]}
    readable = [sid for sid, service in services.items() if "source_ref" in service.get("log_finalization", {})]
    assert readable == ([f"parent1:svc{i}" for i in range(12)] if many else ["parent1:small"])
    for sid in readable:
        log = services[sid]["log_finalization"]
        name = sid.split(":")[1].replace("svc", "")
        [binding] = [row for row in refmap if row["sha256"] == log["source_ref"]["sha256"]]
        for arguments in (binding["read"]["arguments"], log["source_ref"]["read"]["arguments"]):
            text = registry.execute_result("read_file", arguments).text
            assert f"HEAD-{name}" in text and f"LAST-{name}" in text
        # The gzip blob is retained beside it for host readers; neither is the author's copy.
        assert observability.read_blob_ref(read_root, log["full_log_ref"], expected_kind="txt").startswith(f"HEAD-{name}")
        assert artifacts.read_actor_source_bytes(read_root, "parent1", log["source_ref"])
    if many:
        lost = services["parent1:lost"]["log_finalization"]
        assert lost["full_log_ref"]["availability"] == "unavailable" and "LOST-LAST" in lost["tail"]
        assert "FAILED-LAST" in services["parent1:failed"]["log_finalization"]["tail"]
