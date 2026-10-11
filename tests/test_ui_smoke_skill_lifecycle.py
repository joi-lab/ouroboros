"""Installed skill UI flows with the real browser modules and controlled HTTP."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest


@pytest.mark.ui_browser
@pytest.mark.parametrize("version", ["1.2.3", "", None])
def test_marketplace_update_retries_selected_version(version, tmp_path):
    """Two failed update responses never change the owner's selected version."""
    playwright = pytest.importorskip("playwright.sync_api")
    web = Path(__file__).resolve().parents[1] / "web"
    posts = []
    installed = {
        "name": "demo", "version": "1.0.0", "type": "script", "enabled": False,
        "review_status": "clean", "executable_review": True,
        "provenance": {"slug": "demo", "version": "1.0.0"},
    }

    def serve(route):
        path = urlsplit(route.request.url).path
        data = None
        if path == "/":
            route.fulfill(content_type="text/html", body="""
                <link rel="stylesheet" href="/style.css">
                <link rel="stylesheet" href="/settings.css">
                <main id="marketplace"></main>
                <script type="module">
                  import {initMarketplace} from '/modules/marketplace.js';
                  initMarketplace(document.querySelector('#marketplace'));
                </script>
            """)
            return
        if path == "/api/marketplace/clawhub/search":
            data = {"results": [{"slug": "demo", "latest_version": "2.0.0"}]}
        elif path == "/api/marketplace/clawhub/installed":
            data = {"skills": [installed]}
        elif path == "/api/extensions":
            data = {"skills": [installed]}
        elif path == "/api/skills/lifecycle-queue":
            data = {"events": []}
        elif path == "/api/marketplace/clawhub/update/demo":
            posts.append(route.request.post_data_json)
            data = {"ok": len(posts) > 2, "error": "controlled update failure", "review_status": "clean"}
        if data is not None:
            route.fulfill(content_type="application/json", body=json.dumps(data))
            return
        target = (web / path.lstrip("/")).resolve()
        if target.is_relative_to(web) and target.is_file():
            route.fulfill(path=target, content_type=mimetypes.guess_type(target)[0] or "text/plain")
        else:
            route.fulfill(status=404)

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 800})
            page.add_init_script("""
                window.skillEvents = [];
                window.addEventListener('ouro:skill-lifecycle', event => window.skillEvents.push(event.detail));
            """)
            page.route("**/*", serve)
            page.goto("http://skill-ui.test/")
            page.locator('[data-mp-update="demo"]').click()
            dialog = page.get_by_role("dialog")
            if version is None:
                dialog.get_by_role("button", name="Cancel", exact=True).click()
                assert posts == []
                return
            dialog.locator("[data-confirm-input]").fill(version)
            dialog.locator("[data-confirm-ok]").click()
            retry = page.get_by_role("button", name="Retry update", exact=True)
            retry.wait_for()
            assert posts == [{"version": version} if version else {}]
            retry.click()
            retry.wait_for()
            assert len(posts) == 2
            screenshot_dir = Path(os.environ.get("OUROBOROS_UI_SCREENSHOT_DIR", tmp_path))
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_dir / f"update-retry-{'pinned' if version else 'latest'}.png"))
            retry.click()
            page.wait_for_function("() => window.skillEvents.some(e => e.action === 'update' && e.name === 'demo' && e.ok === true)")
            assert posts == [{"version": version} if version else {}] * 3
        finally:
            browser.close()


from tests.test_ui_smoke_playwright import direct_server_with_data as _direct_server_with_data
from tests._extension_loader_shared import _clear_loader_state  # noqa: F401

direct_server_with_data = _direct_server_with_data


@pytest.mark.ui_browser
@pytest.mark.serial
def test_installed_repair_reloads_tool_route_widget_and_companion(direct_server_with_data, monkeypatch, tmp_path):
    import time
    import urllib.request
    from ouroboros import extension_loader, skill_review
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.platform_layer import pid_is_alive
    from ouroboros.skill_loader import compute_content_hash, load_review_state
    from ouroboros.skill_repair_admission import record_repair_admission
    from ouroboros.project_dialogue import build_owner_message_ref
    from ouroboros.utils import append_jsonl, utc_now_iso
    from ouroboros.tools.registry import ToolRegistry
    from tests._extension_loader_shared import _write_ext_skill
    from tests._skill_review_shared import _make_actor, _pass_array_for_script_skill, _patch_review
    from tests.test_skill_review import _mark_self_authored

    drive = direct_server_with_data["data_dir"]
    url = direct_server_with_data["url"]
    name = "repair_live_fixture"
    payload = _write_ext_skill(
        drive / "skills" / "external", name,
        permissions=["tool", "route", "widget", "companion_process"],
        plugin_body=(
            "async def status(request):\n    return {'value': 'version one'}\n"
            "def register(api):\n"
            "    api.register_tool('ping', lambda ctx: 'version one', description='probe', schema={})\n"
            "    api.register_route('status', status, methods=('GET',))\n"
            "    api.register_ui_tab('main', 'Repaired application', render={'kind':'module','entry':'widget.js','start':'auto'})\n"
            "    api.register_companion_process('worker')\n"
        ),
        extra_frontmatter=(
            'plugin_api: "2.0"\ncompanion_processes:\n  - name: worker\n    runtime: python3\n'
            '    command: ["python3", "worker.py"]\n    restart_policy: never\n'
        ),
    )
    (payload / "worker.py").write_text(
        "import os, pathlib, threading\n"
        "pathlib.Path(os.environ['OUROBOROS_SKILL_STATE_DIR'], 'ready.txt').write_text('version one')\n"
        "threading.Event().wait(120)\n"
    )
    (payload / "widget.js").write_text(
        "const style = document.createElement('style');\n"
        "style.textContent = 'body{color:#e8ecf3;font:16px system-ui;padding:16px}';\n"
        "document.head.appendChild(style);\n"
        f"fetch('/api/extensions/{name}/status').then(r => r.json()).then(value => {{\n"
        " document.getElementById('root').textContent = 'Installed widget: ' + value.value;\n});\n"
    )
    _mark_self_authored(payload, drive)
    monkeypatch.setenv("OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS", "true")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(tmp_path / "unused"))
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a, **k: None)
    registry = ToolRegistry(repo_dir=Path(__file__).resolve().parents[1], drive_root=drive)
    registry._ctx.task_id = "live-repair"
    registry._ctx.current_chat_id = 42
    registry._ctx.task_constraint = TaskConstraint(skill_name=name, payload_root=f"skills/external/{name}", allow_enable=False)
    owner_text = "Repair and run the installed application, leaving it working."
    origin = build_owner_message_ref(chat_id=42, client_message_id="repair-fixture", ts=utc_now_iso(), text=owner_text)
    append_jsonl(drive / "logs" / "chat.jsonl", {**origin, "direction": "in", "text": owner_text, "source": "web"})
    registry._ctx.task_metadata = {"origin_message_ref": origin}
    record_repair_admission(drive, name, task_id="live-repair", base_content_hash=compute_content_hash(payload))
    canned = json.dumps({"results": [_make_actor(f"reviewer-{n}", _pass_array_for_script_skill()) for n in range(3)]})

    def api(path, body=None):
        request = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    playwright = pytest.importorskip("playwright.sync_api")
    pids, revisions = [], []
    with _patch_review(canned), playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 850})
            for value in ("version one", "version two"):
                if value == "version two":
                    for path in ("plugin.py", "worker.py"):
                        # plugin.py has two intentional occurrences; the file writer
                        # uses exact complete content, while worker uses exact replace.
                        source = (payload / path).read_text()
                        result = registry.execute("write_file", {"root": "skill_payload", "path": path,
                                                   "content": source.replace("version one", value)})
                        assert result.startswith("OK:"), result
                reviewed = registry.execute("skill_review", {"skill": name})
                state = load_review_state(drive, name)
                assert state.status == "clean", reviewed
                revisions.append(state.content_hash)
                enabled = registry.execute("toggle_skill", {"skill": name, "enabled": True})
                assert json.loads(enabled)["enabled"], enabled
                reconciled = api(f"/api/skills/{name}/reconcile", {})
                assert not reconciled.get("load_error"), reconciled
                assert api(f"/api/extensions/{name}/status")["value"] == value
                surface = extension_loader.extension_surface_name(name, "ping")
                actual = registry.execute_result(surface, {})
                assert actual.status == "ok" and actual.text == value, actual
                assert actual.meta["content_hash"] == state.content_hash
                ready = drive / "state" / "skills" / name / "ready.txt"
                deadline = time.monotonic() + 15
                while (not ready.exists() or ready.read_text() != value) and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert ready.read_text() == value
                companion = api("/api/skills/daemons")["companions"][name + ":worker"]
                pids.append(companion["pid"])
                assert pid_is_alive(pids[-1])
                page.goto(url + "/#widgets")
                frame = page.frame_locator(f'[data-widget-key="{name}:main"] iframe')
                frame.locator("#root").filter(has_text="Installed widget: " + value).wait_for(timeout=30000)
                evidence = Path(os.environ.get("OUROBOROS_UI_SCREENSHOT_DIR", str(tmp_path)))
                evidence.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(evidence / ("repaired-widget-" + value.replace(" ", "-") + ".png")))
            assert revisions[0] != revisions[1]
            assert pids[0] != pids[1]
            assert not pid_is_alive(pids[0])
        finally:
            browser.close()
            api(f"/api/skills/{name}/toggle", {"enabled": False})
            extension_loader.unload_extension(name)
    assert name + ":worker" not in api("/api/skills/daemons")["companions"]
    assert all(not pid_is_alive(pid) for pid in pids)
    if os.environ.get("OUROBOROS_UI_SCREENSHOT_DIR"):
        (Path(os.environ["OUROBOROS_UI_SCREENSHOT_DIR"]) / "installed-execution.json").write_text(json.dumps({
            "revisions": revisions, "companion_pids": pids, "all_companions_stopped": True,
            "surfaces": ["review", "tool", "http", "widget", "companion"],
            "review_delivery": "real skill pipeline with controlled panel backend",
            "server": "real isolated server.py",
        }, indent=2) + "\n")
