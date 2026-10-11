"""Small real-engine acceptance for the explicit synthetic failure recorder."""

from __future__ import annotations

import json
import zipfile

import pytest

from tests.ui_failure_evidence import FailureEvidence


@pytest.mark.ui_browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_ui_failure_evidence_real_browser(tmp_path, monkeypatch, engine):
    playwright = pytest.importorskip("playwright.sync_api")
    secret = "owner-secret-sentinel-do-not-export-771c0f"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    html = """<!doctype html><title>Synthetic evidence specimen</title>
      <style>body{font:20px system-ui;background:#eef1f6;margin:32px}
      #chat-messages{height:260px;overflow:auto;background:white;padding:20px}
      .content{height:900px;background:linear-gradient(#ddf,#dff)}</style>
      <h1>Browser failure evidence</h1><p id="chat-status">Thinking...</p>
      <div id="chat-messages"><div class="content">Synthetic scroll target</div></div>"""
    with playwright.sync_playwright() as pw:
        for mode in ("disabled", "pass", "failure", "capture_error"):
            browser = getattr(pw, engine).launch(headless=True)
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
            destination = tmp_path / mode
            evidence = FailureEvidence(page, browser, None if mode == "disabled" else destination,
                                       f"synthetic::{mode}", engine)
            actions = []
            sentinel = AssertionError("controlled browser sentinel")
            if mode == "capture_error":
                def fail_capture(**kwargs):
                    raise OSError("synthetic screenshot writer error")
                monkeypatch.setattr(page, "screenshot", fail_capture)
            caught = None
            try:
                with evidence:
                    actions.append("goto")
                    page.goto("https://ui-evidence.invalid/")
                    actions.append("move")
                    page.mouse.move(200, 220)
                    actions.append("wheel")
                    page.mouse.wheel(0, 200)
                    actions.append("wait")
                    page.wait_for_function("document.querySelector('#chat-messages').scrollTop > 0")
                    evidence.checkpoint("sentinel_after_existing_actions")
                    if mode in {"failure", "capture_error"}:
                        raise sentinel
            except AssertionError as exc:
                caught = exc
            assert actions == ["goto", "move", "wheel", "wait"]
            assert not browser.is_connected()
            if mode in {"disabled", "pass"}:
                assert caught is None and not destination.exists()
                continue
            assert caught is sentinel
            data = json.loads((evidence.bundle / "evidence.json").read_text(encoding="utf-8"))
            geometry = json.loads((evidence.bundle / "geometry.json").read_text(encoding="utf-8"))
            assert data["primary_exception"]["message"] == str(sentinel)
            assert data["failure_stage"] == "sentinel_after_existing_actions"
            assert geometry["feed"]["scroll_top"] > 0
            assert geometry["hit_chain"]
            assert any(row["type"] == "wheel" for row in geometry["events"])
            assert "Synthetic scroll target" in (evidence.bundle / "page.html").read_text(encoding="utf-8")
            if mode == "failure":
                assert (evidence.bundle / "screenshot.png").read_bytes().startswith(b"\x89PNG")
            else:
                assert data["capture_errors"][0]["operation"] == "screenshot"
            with zipfile.ZipFile(evidence.bundle / "trace.zip") as trace:
                assert any(name.endswith(".trace") for name in trace.namelist())
                assert all(secret.encode() not in trace.read(name) for name in trace.namelist())
            assert all(secret.encode() not in path.read_bytes() for path in evidence.bundle.iterdir())


@pytest.mark.ui_browser
def test_observer_preserves_scroll_writes_and_sees_late_wheel_cancellation():
    from tests.ui_failure_evidence import _OBSERVE_EVENTS

    playwright = pytest.importorskip("playwright.sync_api")
    html = "<div id='chat-messages' style='width:600px;height:300px;overflow:auto'><div style='height:2000px'>Specimen</div></div>"
    with playwright.sync_playwright() as pw:
        browser = pw.webkit.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 800, "height": 600})
            page.set_content(html)
            page.evaluate("""() => {
                window.original = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
                window.prototype = Object.getPrototypeOf(document.querySelector('#chat-messages'));
                original.set.call(document.querySelector('#chat-messages'), 70);
            }""")
            page.evaluate(_OBSERVE_EVENTS)
            page.evaluate("""() => {
                const feed = document.querySelector('#chat-messages');
                feed.tabIndex = 0;
                feed.focus({preventScroll: true});
            }""")
            facts = page.evaluate("""() => {
                const feed = document.querySelector('#chat-messages');
                const current = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
                function programmaticRestore() { return current.set.call(feed, 175); }
                const returned = programmaticRestore();
                const failure = setter => {
                    try { setter.call(null, 1); return 'missing exception'; }
                    catch (error) { return error.name + ': ' + error.message; }
                };
                return {top: feed.scrollTop, returnsUndefined: returned === undefined,
                    getter: current.get === original.get, enumerable: current.enumerable === original.enumerable,
                    configurable: current.configurable === original.configurable,
                    prototype: Object.getPrototypeOf(feed) === window.prototype,
                    originalError: failure(original.set), observedError: failure(current.set),
                    writes: window.__ciUiEvents.filter(row => row.type === 'js_scroll_write'),
                    instrumentation: window.__ciUiInstrumentation};
            }""")
            assert facts["top"] == 175 and facts["returnsUndefined"]
            assert all(facts[key] for key in ("getter", "enumerable", "configurable", "prototype"))
            assert facts["originalError"] == facts["observedError"]
            assert facts["instrumentation"]["scroll_top"]["state"] == "installed"
            [write] = facts["writes"]
            assert (write["before"], write["requested"], write["after"], write["returned"]) == (70, 175, 175, True)
            assert "programmaticRestore" in write["stack"]

            # The target listener runs AFTER our document capture listener.
            page.evaluate("""() => {
                window.preventWheel = event => event.preventDefault();
                document.querySelector('#chat-messages').addEventListener('wheel', preventWheel, {passive: false});
            }""")
            page.mouse.move(200, 150)
            page.mouse.wheel(0, 200)
            page.wait_for_function("window.__ciUiEvents.some(row => row.type === 'wheel_after_dispatch')")
            events = page.evaluate("window.__ciUiEvents")
            [wheel] = [row for row in events if row["type"] == "wheel"]
            [after] = [row for row in events if row["type"] == "wheel_after_dispatch"]
            assert wheel["default_prevented_at_capture"] is False and wheel["is_trusted"] is True
            assert wheel["document_focused"] is True and wheel["visibility_state"] == "visible"
            assert wheel["active_element"]["id"] == "chat-messages"
            assert after["event_phase"] == 0 and after["dispatch_complete"] is True
            assert after["default_prevented"] is True and after["is_trusted"] is True
            assert after["wheel_time_ms"] == wheel["time_ms"] and after["time_ms"] >= wheel["time_ms"]
            assert after["active_element"] == wheel["active_element"]
            assert any(row["type"] == "focusin" and row["active_element"]["id"] == "chat-messages"
                       for row in events)
            assert page.evaluate("document.querySelector('#chat-messages').scrollTop") == 175

            page.evaluate("document.querySelector('#chat-messages').removeEventListener('wheel', preventWheel)")
            page.mouse.wheel(0, 200)
            page.wait_for_function("document.querySelector('#chat-messages').scrollTop > 175")
            page.wait_for_function("window.__ciUiEvents.filter(row => row.type === 'wheel_after_dispatch').length === 2")
            events = page.evaluate("window.__ciUiEvents")
            assert [row for row in events if row["type"] == "wheel_after_dispatch"][-1]["default_prevented"] is False
            assert len([row for row in events if row["type"] == "js_scroll_write"]) == 1

            # An unwrappable native descriptor is explicit, and still works.
            other = browser.new_page()
            other.set_content(html)
            other.evaluate("""() => {
                const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
                Object.defineProperty(Element.prototype, 'scrollTop', {...descriptor, configurable: false});
            }""")
            other.evaluate(_OBSERVE_EVENTS)
            assert other.evaluate("window.__ciUiInstrumentation.scroll_top.state") == "unavailable"
            assert other.evaluate("document.querySelector('#chat-messages').scrollTop = 123") == 123
            assert other.evaluate("document.querySelector('#chat-messages').scrollTop") == 123
        finally:
            browser.close()
