"""A Project root's Main notice, end to end in the real SPA (#1412).

A turn bound to a Project room may judge that the owner needs a short
plain-text notice in Main (``send_user_message(destination="main")``). What no
unit test certifies is the consumer: the shipped SPA must show that notice as
an assistant message in Main and never in the Project thread, the Project card
must not read it as the turn's answer while the turn still runs, the Project's
unread authority must not move for it, and a reload must replay it into Main
only while the Project keeps its own final answer. On a phone the Project room
is a full-screen sheet over Main, so its surface must be opaque over the notice.

The turn is a real keyless server turn. ``ModelGate`` holds it twice at the
HTTP boundary: before its first round, so the owner can leave the chat page
before the notice exists, and inside its second round, after the notice tool
returned and before the final answer, so "still running" is a fact, not a race.
"""

import re
import time
import uuid

import pytest

from devtools.benchmarks.common.server_runner import _api
from tests.system_e2e.harness import (
    ArtifactOracle,
    ModelGate,
    ScriptedStubModel,
    body_text,
    keyless_settings,
    message_text,
    start_server,
    wait_durable_result,
    wait_until,
)
from tests.test_owner_wait_integration import wait_clone as clone_fixture
from tests.ui_chat_viewport_smoke import _CAPTURE_TEST_SOCKET
from tests.ci_evidence import output_dir
from tests.ui_failure_evidence import FailureEvidence

wait_clone = clone_fixture
pytestmark = [pytest.mark.serial, pytest.mark.ui_browser]

# Tool-less namer calls answer with this, so it never matches a feed assertion.
CARD_NAME = "Project audit turn"
# One full period of the SPA's Project-list poll (app.js setInterval 20000) plus margin.
PROJECT_POLL_WINDOW_SEC = 22

_FEED_FACTS = """({feed, taskId, notice, answer}) => {
    const root = document.querySelector(feed);
    if (!root) return null;
    const bubbles = [...root.querySelectorAll('.chat-bubble')].map(node => ({
        classes: node.className,
        type: node.dataset.systemType || '',
        task: node.dataset.taskId || '',
        sender: node.querySelector(':scope > .sender')?.textContent.trim() || '',
        text: node.querySelector(':scope > .message')?.innerText || '',
    }));
    const cards = [...root.querySelectorAll(`.chat-live-card[data-task-id="${taskId}"]`)];
    const text = root.innerText || '';
    return {
        visible: root.getClientRects().length > 0,
        notice_bubbles: bubbles.filter(b => b.text.includes(notice)),
        answer_bubbles: bubbles.filter(b => b.text.includes(answer)),
        notice_anywhere: text.includes(notice),
        answer_anywhere: text.includes(answer),
        cards: cards.length,
        card_finished: cards[0]?.dataset.finished ?? null,
        card_phase: cards[0]?.querySelector('[data-live-phase]')?.textContent.trim() || '',
    };
}"""

_NAV_FACTS = """projectId => {
    const row = document.querySelector(`.nav-project-row[data-project-id="${projectId}"]`);
    return {
        main_badge: document.querySelector('[data-nav-page="chat"] .unread-badge')?.textContent.trim() || '',
        projects_count: document.querySelector('#nav-projects-count')?.textContent.trim() || '',
        project_label: row?.getAttribute('aria-label') || '',
        active_page: document.querySelector('.page.active')?.id || '',
    };
}"""

# Every value the Project unread counter ever shows, so a transient unread
# caused by the notice cannot pass by being overwritten before a later read.
_OBSERVE_PROJECT_COUNT = """() => {
    const node = document.querySelector('#nav-projects-count');
    window.__projectCounts = [node?.textContent.trim() || ''];
    new MutationObserver(() => window.__projectCounts.push(node.textContent.trim()))
        .observe(node, {childList: true, characterData: true, subtree: true});
}"""

_PANEL_COVER = """() => {
    const panel = document.querySelector('#project-panel');
    const notice = document.querySelector('#chat-messages .chat-bubble[data-system-type="main_notice"]');
    return {
        open: panel.classList.contains('open') && !panel.hidden,
        background: getComputedStyle(panel).backgroundColor,
        behind: document.querySelector('.page.active')?.id || '',
        notice_behind: Boolean(notice?.getClientRects().length),
    };
}"""


def _mobile(page):
    return page.viewport_size["width"] <= 640


def _open_drawer(page):
    """At phone width the sidebar is a drawer behind the page header toggle."""
    if not _mobile(page):
        return
    close = page.locator("#project-panel-close")
    if close.is_visible():
        close.click()
    if not page.evaluate("() => document.body.classList.contains('nav-drawer-open')"):
        page.locator(".page.active [data-mobile-nav-toggle]").click()
        page.wait_for_function("() => document.body.classList.contains('nav-drawer-open')")


def _nav(page, name):
    _open_drawer(page)
    page.locator(f'#primary-sidebar [data-nav-page="{name}"]').click()
    page.wait_for_selector(f"#page-{name}.active")


def _open_project(page, project):
    _open_drawer(page)
    page.locator(f'.nav-project-row[data-project-id="{project["id"]}"]').click()
    feed = f'[id="pchat-{project["id"]}-messages"]'
    page.locator(feed).wait_for(state="visible", timeout=30_000)
    return feed


def _alpha(color):
    """Alpha of a computed CSS color: rgb()/rgba() in comma or slash syntax, or color(<space> ...)."""
    fn, _, args = color.strip().partition("(")
    parts = re.split(r"[\s,/]+", args.rstrip(")").strip())
    if fn == "color":
        parts = parts[1:]  # the color space name
    assert fn in {"rgb", "rgba", "color"} and len(parts) in (3, 4), color
    alpha = parts[3] if len(parts) == 4 else "1"
    return float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha)


def _panel_cover(page):
    """On a phone the open Project room covers Main; a see-through surface would expose the notice."""
    if not _mobile(page):
        return None  # the desktop side sheet is translucent glass by design
    cover = page.evaluate(_PANEL_COVER)
    assert cover["open"] and cover["behind"] == "page-chat" and cover["notice_behind"], cover
    assert _alpha(cover["background"]) == 1, cover
    return cover


def _feed(page, feed, task_id, notice, answer):
    return page.evaluate(_FEED_FACTS, {"feed": feed, "taskId": task_id, "notice": notice, "answer": answer})


def _chat_rows(oracle, needle):
    return [{"direction": row.get("direction"), "chat_id": row.get("chat_id"), "type": row.get("type", ""),
             "task_id": row.get("task_id", ""), "text": str(row.get("text") or "")}
            for row in oracle._jsonl("logs/chat.jsonl") if needle in str(row.get("text") or "")]


def _identities(rows):
    return [(row["direction"], row["chat_id"], row["type"], row["task_id"]) for row in rows]


def _revision(server, project_id):
    rows = _api(server.base_url, "GET", "/api/projects")["projects"]
    return int(next(row for row in rows if row["id"] == project_id).get("visible_revision") or 0)


def _history(page, chat_id):
    return page.evaluate("async id => (await (await fetch(`/api/chat/history?chat_id=${id}`)).json()).messages || []",
                         chat_id)


@pytest.mark.parametrize("engine,width,theme", [("chromium", 1440, "dark"), ("webkit", 390, "light")],
                         ids=["chromium-desktop-dark", "webkit-mobile-light"])
def test_project_root_main_notice_reaches_main_only_and_keeps_the_project_answer(
    wait_clone, tmp_path, monkeypatch, request, engine, width, theme,
):
    from playwright.sync_api import sync_playwright

    from tests.system_e2e.harness import KeylessIsolatedServer

    tag = uuid.uuid4().hex[:12]
    marker = f"PROJECT_AUDIT_{tag}"
    notice = (f"Owner action: the executor account needs its billing re-enabled in Settings; "
              f"I am continuing this Project audit meanwhile. [MAIN_NOTICE_{tag}]")
    answer = f"The Project audit is complete and nothing else needs your action. [PROJECT_ANSWER_{tag}]"
    notice_mark, answer_mark = f"MAIN_NOTICE_{tag}", f"PROJECT_ANSWER_{tag}"
    tool_results, strangers = [], []

    def turn_round(body, *, after_tool):
        results = [message_text(m) for m in body.get("messages", [])
                   if isinstance(m, dict) and str(m.get("role") or "") == "tool"]
        matched = bool(body.get("tools")) and marker in body_text(body) and bool(results) is after_tool
        if matched and after_tool:
            # Read at the gate: the held round is the model's view of the tool's receipt.
            tool_results.extend(results)
        return matched

    before_notice = ModelGate(lambda body: turn_round(body, after_tool=False), timeout=300)
    before_answer = ModelGate(lambda body: turn_round(body, after_tool=True), timeout=300)

    def respond(body):
        if marker not in body_text(body):
            strangers.append(body_text(body)[:300])
            return {"final": "No scripted work for this actor."}
        if not any(str(m.get("role") or "") == "tool" for m in body.get("messages", []) if isinstance(m, dict)):
            # The model's own judgment: one short plain-text notice for Main.
            return {"tool": "send_user_message", "arguments": {
                "text": notice, "reason": "owner-only remedy outside this Project", "destination": "main"}}
        return {"final": answer}

    home = tmp_path / "home"
    home.mkdir()
    original_env = KeylessIsolatedServer._env
    monkeypatch.setattr(KeylessIsolatedServer, "_env", lambda server: {
        **original_env(server), "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
    })
    evidence = output_dir(request.config)
    gates = lambda body: (before_notice(body), before_answer(body))  # noqa: E731
    # The stub narrates its tool round ("still working"): that durable progress row is
    # what a re-mounted Project panel rebuilds the running card from.
    with ScriptedStubModel([respond] * 8, final_answer=CARD_NAME, gate=gates) as stub:
        server = start_server(wait_clone, tmp_path / "instance", keyless_settings(stub, OUROBOROS_MAX_WORKERS=1))
        oracle = ArtifactOracle(server.data_root)
        try:
            project = _api(server.base_url, "POST", "/api/projects", {"name": "Main notice room"})["project"]
            project_chat = int(project["chat_id"])
            assert project_chat != 1, project
            with sync_playwright() as pw:
                browser = getattr(pw, engine).launch()
                page = browser.new_page(viewport={"width": width, "height": 900}, has_touch=width < 980,
                                        color_scheme=theme)
                errors, state_reads = [], []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("request", lambda request: state_reads.append(time.monotonic())
                        if request.url.split("?")[0].endswith("/api/state") else None)
                page.add_init_script(f"({_CAPTURE_TEST_SOCKET})()")
                page.add_init_script(f"localStorage.setItem('ouroboros.theme', '{theme}')")
                main_feed = "#chat-messages"
                with FailureEvidence(page, browser, evidence, request.node.nodeid, engine) as capture:
                    capture.details.update({"theme": theme, "width": width})
                    page.goto(server.base_url, wait_until="domcontentloaded")
                    page.wait_for_function("() => window.__testSockets?.[0]?.readyState === WebSocket.OPEN")

                    # ---- the owner speaks in the Project room ----
                    project_feed = _open_project(page, project)
                    page.locator(f'[id="pchat-{project["id"]}-input"]').fill(
                        f"{marker} Audit this Project, and tell me in Main if something needs my action.")
                    page.locator(f'[id="pchat-{project["id"]}-send"]').click()
                    task = wait_until(lambda: next((row["task"] for row in oracle.events("task_received")
                        if marker in str(row.get("task", {}).get("text") or "")), None), 90)
                    assert task, "the Project composer send never reached a turn"
                    task_id = task["id"]
                    assert int(task.get("chat_id") or 0) == project_chat, task
                    assert before_notice.arrived.wait(120), "the Project turn never reached its first round"

                    # ---- the owner is away from chat before the notice exists ----
                    _nav(page, "dashboard")
                    page.evaluate(_OBSERVE_PROJECT_COUNT)
                    revision_before = _revision(server, project["id"])
                    away_before = page.evaluate(_NAV_FACTS, project["id"])
                    assert away_before["main_badge"] == "" and away_before["projects_count"] == "", away_before

                    before_notice.release.set()
                    assert before_answer.arrived.wait(120), "the turn never returned from its notice tool"
                    # The tool's "sent" receipt is the worker's enqueue; the supervisor persists the
                    # row after it. Anchor the refresh window on the exact durable Main row itself.
                    main_row = ("out", 1, "main_notice", task_id)
                    wait_until(lambda: main_row in _identities(_chat_rows(oracle, notice_mark)), 60)
                    notice_seen_at = time.monotonic()
                    notice_rows = _chat_rows(oracle, notice_mark)
                    assert _identities(notice_rows) == [main_row], notice_rows
                    assert any("notice sent to the main chat" in text for text in tool_results), tool_results
                    proactive = [row for row in oracle.events("proactive_message") if row.get("task_id") == task_id]
                    assert [row.get("destination") for row in proactive] == ["main"], proactive
                    # A Main message while the owner is elsewhere is a Main unread.
                    page.wait_for_function(
                        "() => document.querySelector('[data-nav-page=\"chat\"] .unread-badge')?.textContent.trim() === '1'",
                        timeout=30_000)
                    # Hold across one full Project poll: the notice must never read as Project unread.
                    page.wait_for_timeout(PROJECT_POLL_WINDOW_SEC * 1000)
                    polled = [t for t in state_reads if t > notice_seen_at]
                    assert polled, "the SPA never re-read /api/state after the notice"
                    counts_during_hold = page.evaluate("() => window.__projectCounts")
                    assert set(counts_during_hold) == {""}, counts_during_hold
                    revision_after_notice = _revision(server, project["id"])
                    assert revision_after_notice == revision_before, (revision_before, revision_after_notice)
                    assert str(oracle.task_result(task_id).get("status") or "") == "running"
                    live = {row.get("activity_id") for row in _api(server.base_url, "GET", "/api/state")
                            .get("active_direct_turns", [])}
                    assert task_id in live, live
                    away_notice = page.evaluate(_NAV_FACTS, project["id"])
                    assert away_notice["main_badge"] == "1" and "Unread" not in away_notice["project_label"], away_notice
                    _open_drawer(page)
                    capture.checkpoint("1-away-after-notice")

                    # ---- Main shows the notice as an assistant message ----
                    _nav(page, "chat")
                    page.locator(f'{main_feed} .chat-bubble.assistant[data-system-type="main_notice"]').filter(
                        has_text=notice_mark).wait_for(timeout=30_000)
                    main_live = _feed(page, main_feed, task_id, notice_mark, answer_mark)
                    assert page.evaluate(_NAV_FACTS, project["id"])["main_badge"] == ""
                    (bubble,) = main_live["notice_bubbles"]
                    assert "assistant" in bubble["classes"].split(), bubble
                    assert bubble["type"] == "main_notice" and bubble["task"] == task_id, bubble
                    assert bubble["sender"] == "Ouroboros", bubble
                    assert main_live["cards"] == 0 and not main_live["answer_anywhere"], main_live
                    capture.checkpoint("2-main-notice-live")

                    # ---- the Project still runs; the notice is not its answer ----
                    _open_project(page, project)
                    card = page.locator(f'{project_feed} .chat-live-card[data-task-id="{task_id}"]')
                    card.wait_for(timeout=30_000)
                    project_held = _feed(page, project_feed, task_id, notice_mark, answer_mark)
                    assert project_held["card_finished"] == "0", project_held
                    assert project_held["cards"] == 1, project_held
                    assert not project_held["notice_anywhere"] and not project_held["answer_anywhere"], project_held
                    capture.checkpoint("project_held:panel_cover")
                    _panel_cover(page)
                    capture.checkpoint("3-project-held-running")

                    # ---- the answer lands in the Project while the owner is away ----
                    _nav(page, "dashboard")
                    page.evaluate(_OBSERVE_PROJECT_COUNT)
                    before_answer.release.set()
                    stored = wait_durable_result(oracle, task_id, timeout=120)
                    assert stored["status"] == "completed", stored
                    assert answer_mark in str(stored.get("result") or "")
                    assert notice_mark not in str(stored.get("result") or ""), stored
                    answer_rows = wait_until(lambda: _chat_rows(oracle, answer_mark), 60) or []
                    assert answer_rows and {row["chat_id"] for row in answer_rows} == {project_chat}, answer_rows
                    assert all(row["type"] != "main_notice" for row in answer_rows), answer_rows
                    assert _chat_rows(oracle, notice_mark) == notice_rows, "the final re-delivered the notice"
                    revision_after_answer = _revision(server, project["id"])
                    assert revision_after_answer > revision_after_notice
                    # Positive control on the same poll: the Project's own answer IS Project unread.
                    page.wait_for_function(
                        "() => document.querySelector('#nav-projects-count')?.textContent.trim() === '1'",
                        timeout=45_000)
                    away_answer = page.evaluate(_NAV_FACTS, project["id"])
                    assert away_answer["main_badge"] == "", away_answer
                    _open_drawer(page)
                    capture.checkpoint("4-away-after-answer")

                    _nav(page, "chat")
                    main_final = _feed(page, main_feed, task_id, notice_mark, answer_mark)
                    assert len(main_final["notice_bubbles"]) == 1, main_final
                    assert main_final["cards"] == 0 and not main_final["answer_anywhere"], main_final
                    _open_project(page, project)
                    page.locator(project_feed).get_by_text(answer_mark, exact=False).first.wait_for(timeout=30_000)
                    page.wait_for_selector(
                        f'{project_feed} .chat-live-card[data-task-id="{task_id}"][data-finished="1"]', timeout=30_000)
                    project_final = _feed(page, project_feed, task_id, notice_mark, answer_mark)
                    assert not project_final["notice_anywhere"], project_final
                    page.wait_for_function(
                        "() => (document.querySelector('#nav-projects-count')?.textContent.trim() || '') === ''",
                        timeout=45_000)
                    capture.checkpoint("5-project-final")

                    # ---- reload: durable history keeps both identities apart ----
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_function("() => window.__testSockets?.[0]?.readyState === WebSocket.OPEN")
                    page.locator(f'{main_feed} .chat-bubble.assistant[data-system-type="main_notice"]').filter(
                        has_text=notice_mark).wait_for(timeout=30_000)
                    main_reloaded = _feed(page, main_feed, task_id, notice_mark, answer_mark)
                    (bubble,) = main_reloaded["notice_bubbles"]
                    assert "assistant" in bubble["classes"].split() and bubble["sender"] == "Ouroboros", bubble
                    assert bubble["type"] == "main_notice" and bubble["task"] == task_id, bubble
                    assert main_reloaded["cards"] == 0 and not main_reloaded["answer_anywhere"], main_reloaded
                    main_history = [row for row in _history(page, 1) if row.get("task_id") == task_id]
                    assert [(row.get("role"), row.get("system_type")) for row in main_history] == [
                        ("assistant", "main_notice")], main_history
                    project_history = [row for row in _history(page, project_chat) if row.get("task_id") == task_id]
                    assert not [row for row in project_history if notice_mark in str(row.get("text") or "")
                                or row.get("system_type") == "main_notice"], project_history
                    assert [row for row in project_history if answer_mark in str(row.get("text") or "")], project_history
                    nav_reloaded = page.evaluate(_NAV_FACTS, project["id"])
                    assert nav_reloaded["main_badge"] == "" and nav_reloaded["projects_count"] == "", nav_reloaded
                    capture.checkpoint("6-reloaded-main")
                    _open_project(page, project)
                    page.locator(project_feed).get_by_text(answer_mark, exact=False).first.wait_for(timeout=30_000)
                    page.wait_for_selector(
                        f'{project_feed} .chat-live-card[data-task-id="{task_id}"][data-finished="1"]', timeout=30_000)
                    project_reloaded = _feed(page, project_feed, task_id, notice_mark, answer_mark)
                    assert not project_reloaded["notice_anywhere"], project_reloaded
                    capture.checkpoint("project_reloaded:panel_cover")
                    _panel_cover(page)
                    capture.checkpoint("7-reloaded-project")
                    assert not strangers, strangers
                    assert not errors, errors
        finally:
            before_notice.release.set()
            before_answer.release.set()
            server.stop()
            assert server.proc.poll() is not None
            assert not before_notice.timed_out and not before_answer.timed_out
