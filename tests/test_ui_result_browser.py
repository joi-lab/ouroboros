"""Real gateway/SPA result projections plus a native required-question journey."""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from devtools.benchmarks.common.server_runner import _api
from tests.test_owner_wait_integration import wait_clone as clone_fixture
from tests.test_native_owner_wait_browser import chat_connection
from tests._shared import producer_evidence_dir
from tests.test_ui_coherence_browser import pill_landing
from tests.system_e2e.harness import (
    ArtifactOracle, KeylessIsolatedServer, ScriptedStubModel, keyless_settings,
    wait_durable_result, wait_until, write_settings_file,
)

wait_clone = clone_fixture
pytestmark = [pytest.mark.serial, pytest.mark.browser]


def _seed_history(root):
    """Use production result/chat producers; only reviewer/model facts are fixtures."""
    from ouroboros import agent_task_pipeline as pipeline, dialogue_store
    from ouroboros.project_dialogue import append_terminal_task_projection
    from ouroboros.store import document_text
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.task_store import delete_document, read_document
    from supervisor.message_bus import log_chat
    from tests.test_delivery_forced_finalization import _forced_test_context, _bind_host_pass

    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for task_id in ("old-cancelled", "old-unknown"):
        dialogue_store.append(root, "progress", {"task_id": task_id, "chat_id": 1,
            "content": f"Inspecting retained history for {task_id}", "ts": "2026-09-08T10:00:00Z"})
    task = {"id": "old-cancelled", "chat_id": 1, "root_task_id": "old-cancelled", "delegation_role": "root"}
    result = write_task_result(root, task["id"], "cancelled", result="Retained cancellation")
    log_chat("out", 1, 1, "Retained ordinary answer before cancellation", task_id=task["id"], drive_root=root)
    assert append_terminal_task_projection(root, task["id"], task, result, {"chat_id": 1})
    # The old result is lost. Results live in the store now, not in task_results/<id>.json:
    # set its exact document aside and remove it there.
    preserved = root / "old-cancelled-preserved.json"
    preserved.write_text(document_text(read_document(root, task["id"])), encoding="utf-8")
    assert delete_document(root, task["id"]) and load_task_result(root, task["id"]) is None

    loop, registry, ctx, trace = _forced_test_context(root)
    candidate = loop._replace_delivery_candidate(registry, ctx, trace, "The accepted report is available.", control="candidate")
    _bind_host_pass(loop, registry, trace, candidate)
    trace["delivery_candidate"].update(degraded=True, degraded_reason="advisory_plan_review_open")
    env = SimpleNamespace(drive_root=root, repo_dir=root)
    task = {"id": "parent1", "type": "task", "chat_id": 1, "text": "Produce a verified report",
            "suggested_name": "Accepted report", "_skip_post_task_synthesis": True}
    pipeline._store_task_result(env, task, "The accepted report is available.", {}, trace)
    stored = load_task_result(root, "parent1")
    assert stored["outcome_axes"]["objective"]["status"] == "pass"
    assert stored["outcome_axes"]["execution"]["status"] == "degraded"
    dialogue_store.append(root, "progress", {"task_id": "parent1", "chat_id": 1,
        "content": "Verifying the report", "ts": "2026-09-08T10:01:00Z"})
    log_chat("out", 1, 1, "The accepted report is available.", task_id="parent1", drive_root=root)

    pending = []
    direct = {"id": "routing-history", "type": "task", "chat_id": 1,
              "text": "Read the evidence and route the follow-up into its Project.",
              "_is_direct_chat": True, "_skip_post_task_synthesis": True}
    direct_trace = {"tool_calls": [{"tool": "read_file"}, {"tool": "route_to_project"}], "reasoning_notes": []}
    pipeline.emit_task_results(env, None, None, pending, direct, "The work continues in the Project.",
        {"rounds": 2}, direct_trace,
        start_time=0.0, drive_logs=logs)
    final = next(row for row in pending if row["type"] == "send_message")
    log_chat("out", 1, 1, final["text"], task_id=direct["id"], message_meta=final.get("progress_meta", {}), drive_root=root)
    # Even a stopped native turn receives the free facts row from the real
    # emission path; replay consumes it without a second fixture write.
    from ouroboros.gateway.history import _assemble_history_response
    summaries = [row for row in json.loads(_assemble_history_response(root, 1, 50, 200))["messages"]
                 if row.get("task_id") == direct["id"] and row.get("system_type") == "task_summary"]
    assert len(summaries) == 1 and summaries[0]["tool_calls"] == 2 and summaries[0]["text"] == ""
    return {"preserved_path": str(preserved), "preserved_bytes": preserved.read_bytes()}


def _seed_early_failure(root):
    from ouroboros import dialogue_store
    from ouroboros.task_results import write_task_result
    from supervisor.message_bus import log_chat
    from ouroboros.task_finalization import stamp_root_final_phase

    early = {"type": "send_message", "progress_meta": {"outcome_axes": {"execution": {"status": "infra_failed"}}}}
    stamp_root_final_phase(early, {}, post_task_open=True, terminal_status="failed")
    write_task_result(root, "known-failure", "failed", title="Known failed work",
                      root_phase_checkpoint={"post_task_synthesis": "running"},
                      outcome_axes={"execution": {"status": "infra_failed"}})
    dialogue_store.append(root, "progress", {"task_id": "known-failure", "chat_id": 1,
        "content": "Checking the requested result", "ts": "2026-09-08T10:00:00Z"})
    log_chat("out", 1, 1, "The provider failed; retained work remains available.",
             task_id="known-failure", message_meta=early["progress_meta"], drive_root=root)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_main_current_optional_question_survives_history_horizon_and_reload(wait_clone, tmp_path, engine):
    """A real candidate serves a terminal optional ask absent from initial Main history.

    The census names it and mounts nothing; Main's waiting pill counts it, mounts its copy at its
    own date, lands the card under the chat header with the question focused; the copy takes a
    late answer and leaves, and the pill with it. The same holds after a reload."""
    from playwright.sync_api import expect, sync_playwright
    from ouroboros.gateway.history import _DEFAULT_N_HUMAN
    from ouroboros.owner_quiz import record_asked, record_publication, reconcile_terminal, quiz_states, mark_published
    from ouroboros.project_dialogue import project_question_pointer
    from ouroboros.projects_registry import create_project
    from ouroboros.task_results import write_task_result
    from supervisor.message_bus import log_chat

    root = tmp_path / "instance" / "data"
    root.mkdir(parents=True)
    project = create_project(root, "old-optional", name="Older optional decision")
    task_id, quiz_id = "old-optional-task", "old-optional-question"
    write_task_result(root, task_id, "completed", project_id=project["id"], chat_id=project["chat_id"],
                      result="The draft uses HTML while the format decision remains open.")
    record_asked(root, task_id, quiz_id=quiz_id, question="Which format should the next edition use?",
                 options=["PDF", "HTML"], option_details=["Printable edition", "Accessible web edition"],
                 assumption="HTML for the current draft", chat_id=project["chat_id"])
    record_publication(root, task_id, quiz_id, project["chat_id"])
    reconcile_terminal(root, task_id)
    block = quiz_states(root, task_id)[quiz_id]
    log_chat("out", project["chat_id"], 1, block["question"], task_id=task_id, record_type="quiz",
             quiz=block, drive_root=root, require_write=True)
    mark_published(root, task_id, quiz_id)
    block = quiz_states(root, task_id)[quiz_id]
    pointer = project_question_pointer({"task_id": task_id, "quiz_id": quiz_id}, block, project)
    log_chat("out", 1, 1, pointer["text"], task_id=task_id, message_meta=pointer, drive_root=root, require_write=True)
    for index in range(_DEFAULT_N_HUMAN + 12):
        log_chat("in", 1, 1, f"Later Main history row {index}", drive_root=root)

    evidence = producer_evidence_dir(tmp_path / "screenshots", "ui-result",
                                     env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    evidence.mkdir(parents=True, exist_ok=True)
    with ScriptedStubModel([{"final": "Future editions will use PDF."}]) as stub:
        settings_path = root / "settings.json"
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        try:
            history = _api(server.base_url, "GET", "/api/chat/history?chat_id=1")["messages"]
            assert not any(row.get("quiz_id") == quiz_id for row in history)
            canonical = _api(server.base_url, "GET", "/api/state")
            assert canonical["current_questions_complete"]
            assert any(row["quiz_id"] == quiz_id for row in canonical["current_questions"])
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={"width": 1100, "height": 900}, reduced_motion="reduce")
                try:
                    page.goto(server.base_url, wait_until="domcontentloaded")
                    card = page.locator(f'#chat-messages .project-question-card[data-quiz-id="{quiz_id}"]')
                    pill = page.locator('#page-chat .chat-toolbar-row > button.chat-question-nav')
                    landings = []
                    for visit in ("first", "reloaded"):
                        if visit == "reloaded":
                            page.reload()
                        # The census names the question beyond the loaded history; it mounts no copy.
                        expect(pill).to_be_visible(timeout=30000)
                        expect(pill).to_have_text("1 question")
                        expect(pill).to_have_attribute("aria-label", "1 unanswered: show the next question")
                        assert card.count() == 0, "the census never mounts a copy"
                        pill.click()
                        card.get_by_text("Unanswered · task finished", exact=True).wait_for()
                        landing = pill_landing(page, quiz_id)
                        landings.append(landing)
                        assert landing["focused"], landing
                        assert landing["below_header"] >= 0 and landing["above_composer"] >= 0, landing
                        assert abs(landing["below_header"] - 12) <= 3 or landing["scroll_top"] == 0, landing
                        expect(pill).to_be_hidden()  # its only target is on screen
                        assert card.locator('.chat-quiz-comment').count() == 1
                    card.get_by_text("Accessible web edition", exact=True).wait_for()
                    page.screenshot(path=str(evidence / f"{engine}-main-old-optional.png"), animations="disabled")
                    # One quiet x closes the question; it waits while an answer is typed.
                    close = card.get_by_role("button", name="Close question without answering", exact=True)
                    expect(close).to_be_enabled()
                    card.locator('.chat-quiz-comment').fill("A draft that must not be discarded")
                    expect(close).to_be_disabled()
                    card.locator('.chat-quiz-comment').fill("")
                    expect(close).to_be_enabled()
                    with page.expect_response(lambda response: response.url.endswith('/api/decisions')
                                              and response.request.method == 'POST') as answer:
                        card.get_by_role("button", name="PDF Printable edition").click()
                    confirmation = answer.value.json()
                    assert confirmation["ok"] and confirmation["answered_after_terminal"] and confirmation["forwarded"]
                    card.get_by_text("You answered", exact=True).wait_for()
                    page.screenshot(path=str(evidence / f"{engine}-main-old-optional-answered.png"), animations="disabled")
                    stored = _api(server.base_url, "GET", f"/api/tasks/{task_id}")["owner_quiz"][quiz_id]
                    assert stored["state"] == "answered" and stored["answered_index"] == 0
                    assert stored["publication_chat_id"] == project["chat_id"]
                    card.wait_for(state="detached", timeout=15000)
                    expect(pill).to_be_hidden(timeout=15000)
                    (evidence / f"{engine}-main-old-optional.json").write_text(json.dumps({
                        "task_id": task_id, "quiz_id": quiz_id, "project": project,
                        "initial_history_rows": len(history), "confirmation": confirmation,
                        "landings": landings, "server_pid": server.proc.pid, "server_url": server.base_url,
                    }, indent=2))
                finally:
                    browser.close()
        finally:
            server.stop()


@pytest.mark.parametrize("live_engine", ["chromium", "webkit"])
def test_ui_results_and_required_question_journey(wait_clone, tmp_path, monkeypatch, live_engine):
    from playwright.sync_api import sync_playwright

    root = tmp_path / "instance" / "data"
    root.mkdir(parents=True)
    seeded = _seed_history(root)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    original_env = KeylessIsolatedServer._env
    def isolated_env(server):
        return {**original_env(server), "HOME": str(fake_home), "USERPROFILE": str(fake_home),
                "XDG_CONFIG_HOME": str(fake_home / ".config")}
    monkeypatch.setattr(KeylessIsolatedServer, "_env", isolated_env)
    screenshots = producer_evidence_dir(tmp_path / "screenshots", "ui-result",
                                        env="OUROBOROS_BROWSER_EVIDENCE_OUT")
    screenshots.mkdir(parents=True, exist_ok=True)
    steps = [
        {"tool": "escalate", "arguments": {"question": "Which evidence should the report use?",
            "options": [{"label": "Primary sources", "detail": "Use the complete original measurements."},
                        {"label": "Both sources", "detail": "Include the independent replication too."}],
            "stake": "The answer determines the report's evidence.", "wait_for_answer": True}},
        {"final": "The report will include both sources, as requested."},
    ]
    with ScriptedStubModel(steps) as stub:
        settings_path = root / "settings.json"
        write_settings_file(settings_path, keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        server = KeylessIsolatedServer(wait_clone, root, settings_path)
        server.start(ready_timeout=120)
        oracle = ArtifactOracle(root)
        try:
            project = _api(server.base_url, "POST", "/api/projects", {"name": "Evidence review"})["project"]
            _seed_early_failure(root)
            with sync_playwright() as pw:
                browser = getattr(pw, live_engine).launch()
                page = browser.new_page(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
                try:
                    page.goto(server.base_url, wait_until="domcontentloaded")
                    early_card = page.locator('.chat-live-card[data-task-id="known-failure"]')
                    early_card.locator('[data-live-phase]').filter(has_text="Failed").wait_for(timeout=30000)
                    early_card.get_by_text("Finalizing…", exact=True).wait_for()
                    # This seeded row has no live actor. The real census can
                    # correctly retire it; it is a replay metadata assertion,
                    # not a screenshot claim of a live finalizing process.
                    cancelled = page.locator('.chat-live-card[data-task-id="old-cancelled"]')
                    cancelled.locator('[data-live-phase]').filter(has_text="Stopped").wait_for(timeout=30000)
                    assert page.get_by_text("Retained ordinary answer before cancellation", exact=True).count() == 1
                    cancelled.screenshot(animations="disabled", path=str(screenshots / f"{live_engine}-retained-cancelled.png"))
                    unknown = page.locator('.chat-live-card[data-task-id="old-unknown"]')
                    unknown.locator('[data-live-meta]').filter(has_text="Outcome unavailable").wait_for(timeout=30000)
                    assert unknown.locator('[data-cancel-run]').count() == 0
                    route = page.locator('.chat-live-card[data-task-id="routing-history"]')
                    route.locator('[data-live-meta]').filter(has_text="2 tool calls").wait_for()
                    assert route.locator('.chat-live-line.done').count() == 1
                    accepted = page.locator('.chat-live-card[data-task-id="parent1"]')
                    accepted.locator('[data-live-phase]').filter(has_text="Done with warnings").wait_for()
                    # The collapsed card shows only the quiet Reviews count; the section opens inside
                    # the expanded card (DESIGN "Reviews inside task cards").
                    accepted.locator('[data-live-review-summary]').filter(has_text="Reviews").wait_for()
                    if accepted.get_attribute('data-expanded') != '1':
                        accepted.locator(':scope > [data-live-summary-button]').click()
                    accepted.locator('[data-review-section-toggle]').click()
                    accepted.get_by_text("Task acceptance", exact=True).wait_for()
                    accepted.locator('[data-review-group-toggle]').click()
                    accepted.get_by_text("PASS", exact=True).first.wait_for()
                    page.screenshot(animations="disabled", path=str(screenshots / f"{live_engine}-result-history.png"), full_page=True)
                    with chat_connection(server) as ws:
                        message_id = uuid.uuid4().hex
                        ws.send(json.dumps({"type": "chat", "content": "Ask which evidence to use, then wait for my answer.",
                            "client_message_id": message_id, "chat_id": project["chat_id"], "project_id": project["id"]}))
                        task = wait_until(lambda: next((row["task"] for row in oracle.events("task_received")
                            if (row.get("task", {}).get("metadata", {}).get("origin_message_ref") or {}).get("client_message_id") == message_id), None), 90)
                        assert task
                        wait = wait_until(lambda: (block if (block := oracle.task_result(task["id"]).get("owner_wait", {})).get("state") == "waiting" else None), 90)
                        assert wait
                        pointer = page.locator(f'#chat-messages .project-question-card[data-task-id="{task["id"]}"]')
                        pointer.get_by_text("Waiting for your answer", exact=True).wait_for(timeout=30000)
                        # Main mirrors the Project's own form: the options with their details, the
                        # stake and the own-answer field, plus the chip that opens the Project.
                        main_question = page.locator('#chat-messages .chat-bubble.project-question').filter(
                            has=page.locator(f'.project-question-card[data-task-id="{task["id"]}"]'))
                        assert main_question.locator('.chat-quiz-option').count() == 2
                        assert main_question.locator('.chat-quiz-comment').count() == 1
                        main_question.get_by_text("Include the independent replication too.", exact=True).wait_for()
                        main_question.get_by_text("At stake: The answer determines the report's evidence.", exact=True).wait_for()
                        page.screenshot(animations="disabled", path=str(screenshots / f"{live_engine}-main-question-pointer.png"), full_page=True)
                        # Age the original question beyond the ordinary Project
                        # window. Navigation must reconstruct this exact task/quiz
                        # from its existing detail, including both option details.
                        from ouroboros.gateway.history import _DEFAULT_N_HUMAN
                        from supervisor.message_bus import log_chat
                        # The Main pointer can precede the Project's own question row, which keeps its
                        # ask-time ts when appended: age the window only once that row is written.
                        assert wait_until(lambda: any(row.get("msg_type") == "quiz" for row in _api(
                            server.base_url, "GET", f"/api/chat/history?chat_id={project['chat_id']}")["messages"]), 30)
                        for index in range(_DEFAULT_N_HUMAN + 1):
                            log_chat("in", project["chat_id"], 1, f"Retained later project note {index}", drive_root=root)
                        project_history = _api(server.base_url, "GET", f"/api/chat/history?chat_id={project['chat_id']}")["messages"]
                        assert not any(row.get("msg_type") == "quiz" for row in project_history)
                        pointer.locator('.chat-quiz-project').click()
                        quiz = page.locator(f'.chat-quiz-card[data-task-id="{task["id"]}"][data-quiz-id="{wait["quiz_id"]}"]'
                                            ':not(.project-question-card)')
                        quiz.get_by_text("Include the independent replication too.", exact=True).wait_for()
                        # This new Project instance cold-replayed cancelable history;
                        # current activity must restore the existing Stop control.
                        waiting_card = page.locator(f'.chat-live-card[data-task-id="{task["id"]}"]')
                        waiting_card.locator('[data-cancel-run]').wait_for(state="visible", timeout=15000)
                        page.wait_for_function('qid => { const el=document.querySelector(`[data-quiz-id="${qid}"] .chat-quiz-question`); if(!el) return false; const r=el.getBoundingClientRect(); return r.top>=0 && r.bottom<=innerHeight; }', arg=wait["quiz_id"], timeout=15000)
                        waiting_card.screenshot(animations="disabled", path=str(screenshots / f"{live_engine}-cold-stop-control.png"))
                        # The cropped control capture intentionally scrolled;
                        # restore the quiz for the overview after its viewport assertion.
                        quiz.scroll_into_view_if_needed()
                        page.screenshot(animations="disabled", path=str(screenshots / f"{live_engine}-project-question.png"), full_page=True)
                        # One touch in Main answers it: the Main copy shows the recorded answer, then
                        # leaves; the Project card keeps the record (replayed below, in a fresh browser).
                        page.locator('#project-panel-close').click()
                        # The acknowledged answer shows for MIRROR_SETTLE_MS (chat_decision.js), then the copy
                        # leaves: the page records that state itself, so a loaded host cannot miss the window.
                        page.evaluate("""tid => { window.__answerShown = false;
                            const card = () => document.querySelector(`#chat-messages .project-question-card[data-task-id="${tid}"]`);
                            const look = () => { const el = card(); if (el && [...el.querySelectorAll('*')].some(n => n.childElementCount === 0
                                && n.textContent.trim() === 'You answered') && [...el.querySelectorAll('.chat-quiz-option.chosen')]
                                .some(o => o.textContent.includes('Both sources'))) window.__answerShown = true; };
                            new MutationObserver(look).observe(document.querySelector('#chat-messages'),
                                {subtree: true, childList: true, characterData: true, attributes: true}); }""", task["id"])
                        main_question.get_by_role("button", name="Both sources").click()
                        main_question.wait_for(state="detached", timeout=15000)
                        assert page.evaluate("window.__answerShown"), "the Main copy left without showing the recorded answer"
                        result = wait_durable_result(oracle, task["id"], timeout=90)
                        assert result["owner_quiz"][wait["quiz_id"]]["answered_index"] == 1
                        # Only the Main copy went: the exact question still opens in its Project.
                        page.evaluate("""([project, task, quiz]) => window.dispatchEvent(new CustomEvent('ouro:open-project',
                            {detail: {project, task_id: task, quiz_id: quiz}}))""", [project, task["id"], wait["quiz_id"]])
                        quiz.locator('.chat-quiz-option.chosen').filter(has_text="Both sources").wait_for(timeout=30000)
                        task_card = page.locator(f'.chat-live-card[data-task-id="{task["id"]}"]')
                        task_card.locator('[data-live-meta]').filter(has_text="Last solve response:").wait_for(timeout=30000)
                        # The stub authors "still working" as narration and the reopened Project's first
                        # page carries that row, so it names the card the moment the card is painted;
                        # "Task activity" is only the no-narration fallback. A task still finishing can
                        # send a live frame that reaches the new instance before that page: the card
                        # then loses its narration title (chat.js applyLiveCardStateMutation exempts
                        # only a finished record via initialContent): a product defect this check never waits out.
                        assert task_card.locator('[data-live-title]').inner_text() == "still working"
                        page.screenshot(animations="disabled", path=str(screenshots / f"{live_engine}-question-answered.png"), full_page=True)
                        (screenshots / f"{live_engine}-journey.json").write_text(json.dumps({"task_id": task["id"], "quiz_id": wait["quiz_id"],
                            "project": project, "model_execution": result.get("model_execution"), "server_pid": server.proc.pid,
                            "server_url": server.base_url, "owner_quiz": result["owner_quiz"][wait["quiz_id"]]}, indent=2))
                finally:
                    browser.close()
                for engine, viewport in (("webkit", {"width": 1440, "height": 1000}), ("chromium", {"width": 390, "height": 844})):
                    browser = getattr(pw, engine).launch()
                    page = browser.new_page(viewport=viewport, has_touch=viewport["width"] < 500, reduced_motion="reduce")
                    try:
                        page.goto(server.base_url, wait_until="domcontentloaded")
                        # An answered question never enters Main again; its Project replays the record.
                        page.locator('#chat-messages .chat-live-card[data-task-id="parent1"]').wait_for(timeout=30000)
                        assert page.locator('#chat-messages .project-question-card').count() == 0
                        page.evaluate("""([project, task, quiz]) => window.dispatchEvent(new CustomEvent('ouro:open-project',
                            {detail: {project, task_id: task, quiz_id: quiz}}))""", [project, task["id"], wait["quiz_id"]])
                        replay_quiz = page.locator('.chat-quiz-card').filter(has_text="Which evidence should the report use?")
                        replay_quiz.wait_for()
                        replay_quiz.locator('.chat-quiz-option.chosen').filter(has_text="Both sources").wait_for()
                        # Existing scroll restoration spans 12 animation frames;
                        # the explicit question navigation must still own the
                        # viewport after that restoration would have finished.
                        page.evaluate("() => new Promise(resolve => { let n=0; const step=()=>++n>=14 ? resolve() : requestAnimationFrame(step); requestAnimationFrame(step); })")
                        assert replay_quiz.evaluate("el => { const r=el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight; }")
                        replay_card = page.locator(f'.chat-live-card[data-task-id="{task["id"]}"]')
                        assert replay_card.locator('[data-live-title]').inner_text() == "still working"
                        replay_card.locator('[data-live-meta]').filter(has_text="Last solve response:").wait_for()
                        page.screenshot(animations="disabled", path=str(screenshots / f'{live_engine}-{engine}-{viewport["width"]}-question-replay.png'), full_page=True)
                    finally:
                        browser.close()
            assert Path(seeded["preserved_path"]).read_bytes() == seeded["preserved_bytes"]
            from ouroboros.task_results import load_task_result
            assert load_task_result(root, "old-cancelled") is None  # its card came from history alone
        finally:
            server.stop()
            assert server.proc.poll() is not None
