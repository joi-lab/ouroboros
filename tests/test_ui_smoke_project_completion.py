"""Browser proof for the terminal Project completion projection."""

from __future__ import annotations

import pytest

from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401


def _wait_status(page, expected, timeout=10_000):
    page.wait_for_function(
        """(expected) => {
            const el = document.querySelector('#chat-status');
            return el && el.textContent.trim() === expected;
        }""",
        arg=expected,
        timeout=timeout,
    )


@pytest.mark.ui_browser
def test_ui_project_completion_pointer_keeps_project_history_scoped(direct_server_with_data):  # noqa: F811
    """Main gets one human pointer; Project keeps its detailed terminal history."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.project_dialogue import append_chat_annotation
    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, "tower-defence", name="Tower Defence")
    project_chat = int(project["chat_id"])
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    target_label = "Tower Defence › Fix nested delegation"
    task_id = "tower-root-1"
    append_jsonl(logs / "chat.jsonl", {
        "ts": "2026-08-22T10:00:00+00:00", "direction": "out", "chat_id": 1,
        "user_id": 1, "text": f"{target_label} · Completed\nOpen the Project for details.",
        "type": "project_completion_summary", "task_id": task_id,
        "project_id": project["id"], "project_name": project["name"],
        "target_label": target_label, "status": "completed",
    })
    append_jsonl(logs / "chat.jsonl", {
        "ts": "2026-08-22T09:58:00+00:00", "direction": "in", "chat_id": 1,
        "user_id": 1, "text": "Continue the Tower Defence task",
        "client_message_id": "route-to-tower",
    })
    append_chat_annotation(data_dir, "route-to-tower", action="route_to_project",
                           target=project["id"], target_label=target_label, status="delivered")
    append_jsonl(logs / "progress.jsonl", {
        "ts": "2026-08-22T09:59:00+00:00", "type": "send_message", "direction": "out",
        "chat_id": project_chat, "task_id": task_id, "is_progress": True,
        "content": "Project progress: nested delegation is running.",
    })
    append_jsonl(logs / "chat.jsonl", {
        "ts": "2026-08-22T10:00:01+00:00", "direction": "system", "chat_id": project_chat,
        "user_id": 1, "text": "Project task summary", "type": "task_summary",
        "task_id": task_id, "tool_calls": 1, "rounds": 2, "status": "completed",
    })

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)
                summary = page.locator('#chat-messages .chat-bubble[data-system-type="project_completion_summary"]')
                summary.wait_for(state="visible", timeout=30_000)
                summary_text = summary.inner_text()
                assert target_label in summary_text
                # The Main row is a pointer: one status word and the shared cause sentence,
                # never the answer excerpt (owner Q5=A); the answer stays in the Project.
                assert "Open the Project for details." in summary_text
                assert "Release shipped." not in summary_text
                # The row points at its Project with the one reference: the Project's name, never a
                # button of its own and never the raw id.
                reference = summary.locator('[data-intent="open-project"]')
                assert reference.count() == 1
                assert reference.get_attribute("aria-label") == f"Open project {project['name']}"
                assert "Open Project" not in summary_text
                assert project["id"] not in summary_text

                annotation = page.locator('#chat-messages .msg-routing-annotation').filter(has_text=target_label)
                annotation.wait_for(state="visible", timeout=10_000)
                assert project["id"] not in annotation.inner_text()
                main_text = page.locator("#chat-messages").inner_text()
                assert "Project progress: nested delegation is running." not in main_text
                assert "Project task summary" not in main_text

                reference.click()
                page.wait_for_selector("#project-panel:not([hidden])", timeout=30_000)
                assert page.locator("#project-panel-title").inner_text() == project["name"]
                panel = page.locator(f"#panel-pchat-{project['id']}")
                panel.wait_for(state="visible", timeout=30_000)
                task_card = panel.locator(f'.chat-live-card[data-task-id="{task_id}"]')
                task_card.wait_for(state="visible", timeout=10_000)
                assert task_card.locator(".chat-live-phase").inner_text().strip() == "Done"
                task_card.locator("[data-live-summary-button]").click()
                task_card.get_by_text(
                    "Project progress: nested delegation is running.",
                    exact=False,
                ).first.wait_for(state="visible", timeout=30_000)
                assert "Project progress: nested delegation is running." in task_card.inner_text()
                assert panel.locator('.chat-bubble[data-system-type="project_completion_summary"]').count() == 0
                assert panel.locator('.system-message-actions [data-intent="open-project"]').count() == 0
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


_LONG_ANSWER = (
    "**Done: release shipped.** PR #7 merged into `main`.\n\n"
    + "\n\n".join(f"Paragraph {i}: " + "detail " * 30 for i in range(1, 9))
)


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1280, 390])
def test_ui_project_completion_mirror_is_an_ordinary_folded_message(direct_server_with_data, engine, width):  # noqa: F811
    """A root that ended with its own answer reaches Main as Ouroboros's message; others stay pointers."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, f"mirror-{engine}-{width}", name="A rather long Project name that must yield on a phone")
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    base = {"direction": "system", "chat_id": 1, "user_id": 1, "type": "project_completion_summary",
            "project_id": project["id"], "project_name": project["name"], "status": "completed"}
    append_jsonl(logs / "chat.jsonl", {
        **base, "ts": "2026-08-22T10:00:00+00:00", "task_id": "root-long",
        "text": "P › Long · Done\nOpen the Project for details.", "target_label": "P › Long",
        "completion_answer": _LONG_ANSWER})
    append_jsonl(logs / "chat.jsonl", {
        **base, "ts": "2026-08-22T10:01:00+00:00", "task_id": "root-short",
        "text": "P › Short · Done\nOpen the Project for details.", "target_label": "P › Short",
        "completion_answer": "All good, nothing to report."})
    append_jsonl(logs / "chat.jsonl", {
        **base, "ts": "2026-08-22T10:02:00+00:00", "task_id": "root-pointer", "status": "failed",
        "text": "P › Broken · Failed\nOpen the Project for details.", "target_label": "P › Broken"})

    try:
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": 900})
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)
                rows = page.locator('#chat-messages .chat-bubble[data-system-type="project_completion_summary"]')
                rows.nth(2).wait_for(state="visible", timeout=30_000)
                long_row, short_row, pointer = rows.nth(0), rows.nth(1), rows.nth(2)

                # The answered endings speak in Ouroboros's voice and say nothing in the host's.
                for row in (long_row, short_row):
                    classes = row.get_attribute("class")
                    assert "assistant" in classes and "project-answer" in classes and "system" not in classes.split()
                    text = row.inner_text()
                    assert "Open the Project for details." not in text and "Open Project" not in text
                    assert row.locator(".sender").inner_text().strip() == "Ouroboros"
                    assert row.locator(".system-message-actions .chat-quiz-project").count() == 1
                assert "release shipped" in long_row.inner_text()
                # The fold: the long answer is clamped and faded, the short one is neither.
                page.wait_for_function(
                    "() => document.querySelectorAll('.chat-bubble.project-answer.is-folded').length === 1",
                    timeout=10_000)
                geometry = long_row.locator(".message").evaluate(
                    "n => ({client: n.clientHeight, scroll: n.scrollHeight})")
                assert geometry["scroll"] > geometry["client"] > 0
                assert "is-folded" not in short_row.get_attribute("class")
                # The whole answer is in the node: the fold is a viewport, never a cut.
                assert "Paragraph 8" in long_row.locator(".message").evaluate("n => n.textContent")
                # No horizontal overflow of the transcript, the chip yields instead.
                assert page.locator("#chat-messages").evaluate("n => n.scrollWidth <= n.clientWidth + 1")

                # The ending without an answer is still the System pointer, and it points at the
                # Project with the SAME control as the answered ones: a row's voice never picks it.
                assert "system" in pointer.get_attribute("class").split()
                assert "Open the Project for details." in pointer.inner_text()
                shapes = [row.locator('[data-intent="open-project"]').evaluate(
                    "n => [n.className, n.getAttribute('aria-label'), [...n.children].map(c => c.className).join(' ')]")
                    for row in (long_row, pointer)]
                assert shapes[0] == shapes[1]

                # The chip names the Project and opens it.
                chip = long_row.locator(".chat-quiz-project")
                assert project["name"] in (chip.get_attribute("title") or "")
                chip.click()
                page.wait_for_selector("#project-panel:not([hidden])", timeout=30_000)
                assert page.locator("#project-panel-title").inner_text() == project["name"]
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ui_project_completion_mirror_folds_after_mounting_on_a_hidden_page(direct_server_with_data, engine):  # noqa: F811
    """A row mounted while another page is open has no layout box; the fade arrives when Chat is shown."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project
    from ouroboros.utils import append_jsonl

    url = direct_server_with_data["url"]
    data_dir = direct_server_with_data["data_dir"]
    project = create_project(data_dir, f"hidden-{engine}", name="Hidden mount")
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    append_jsonl(logs / "chat.jsonl", {
        "direction": "system", "chat_id": 1, "user_id": 1, "type": "project_completion_summary",
        "project_id": project["id"], "project_name": project["name"], "status": "completed",
        "ts": "2026-08-22T11:00:00+00:00", "task_id": f"root-hidden-{engine}",
        "text": "Hidden mount › Long · Done\nOpen the Project for details.",
        "target_label": "Hidden mount › Long", "completion_answer": _LONG_ANSWER})

    try:
        with sync_playwright() as pw:
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                page.goto(url + "#dashboard", wait_until="domcontentloaded", timeout=30_000)
                _wait_status(page, "Online", timeout=30_000)
                row = page.locator("#chat-messages .chat-bubble.project-answer")
                row.wait_for(state="attached", timeout=30_000)
                assert not page.locator("#page-chat").evaluate("n => n.classList.contains('active')")
                # No box, no measurement: the row is left unmarked rather than guessed.
                assert row.locator(".message").evaluate("n => n.clientHeight") == 0
                assert "is-folded" not in row.get_attribute("class")

                page.locator('[data-nav-page="chat"]').first.click()
                page.wait_for_function(
                    "() => document.querySelectorAll('.chat-bubble.project-answer.is-folded').length === 1",
                    timeout=10_000)
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc).lower():
            pytest.skip(str(exc))
        raise
