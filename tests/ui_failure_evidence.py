"""Opt-in failure evidence for isolated, synthetic UI smoke scenarios.

This is deliberately not a pytest/browser lifetime plugin. Callers supply their
existing page and browser; successful cases discard the trace and keep no rich
bundle. Capture and cleanup errors never replace the original test exception.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import platform
import sys
import time
import uuid
from pathlib import Path


_OBSERVE_EVENTS = """(() => {
    window.__ciUiEvents = [];
    const instrumentation = window.__ciUiInstrumentation = {
        wheel_after_dispatch: 'next task; completion requires event_phase === 0',
        scroll_top: {state: 'unavailable', reason: 'not installed'},
        unobserved_scroll_methods: ['scroll', 'scrollTo', 'scrollBy', 'scrollIntoView'],
    };
    const identity = e => e instanceof Element
        ? {tag: e.tagName, id: e.id, class: String(e.className)} : null;
    const focus = () => ({document_focused: document.hasFocus(),
        visibility_state: document.visibilityState, active_element: identity(document.activeElement)});
    for (const type of ['focus', 'blur', 'focusin', 'focusout', 'visibilitychange']) {
        document.addEventListener(type, event => {
            window.__ciUiEvents.push({type, time_ms: performance.now(),
                target: identity(event.target), ...focus()});
        }, {capture: true, passive: true});
    }
    for (const type of ['wheel', 'scroll', 'scrollend']) {
        document.addEventListener(type, event => {
            const row = {type, time_ms: performance.now(),
                target: identity(event.target), scroll_top: event.target?.scrollTop,
                x: event.clientX, y: event.clientY, delta_x: event.deltaX,
                delta_y: event.deltaY, delta_mode: event.deltaMode,
                is_trusted: event.isTrusted, event_phase: event.eventPhase, ...focus(),
                default_prevented_at_capture: event.defaultPrevented};
            window.__ciUiEvents.push(row);
            // A microtask can run between listeners. The next task sees whether
            // dispatch actually finished, including a later preventDefault.
            if (type === 'wheel') setTimeout(() => {
                window.__ciUiEvents.push({type: 'wheel_after_dispatch',
                    wheel_time_ms: row.time_ms, time_ms: performance.now(),
                    event_phase: event.eventPhase, dispatch_complete: event.eventPhase === 0,
                    default_prevented: event.defaultPrevented, is_trusted: event.isTrusted, ...focus()});
            }, 0);
        }, {capture: true, passive: true});
    }
    // Chat's own position writers use scrollTop; native scrolling does not call
    // this JS setter. Other methods are explicitly unobserved, never ruled out.
    try {
        const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop');
        if (!descriptor?.get || !descriptor?.set || !descriptor.configurable) {
            instrumentation.scroll_top = {state: 'unavailable', reason: 'native descriptor not wrappable'};
            return;
        }
        Object.defineProperty(Element.prototype, 'scrollTop', {...descriptor, set(value) {
            'use strict';
            if (!(this instanceof Element) || this.id !== 'chat-messages')
                return Reflect.apply(descriptor.set, this, [value]);
            let row;
            try {
                row = {type: 'js_scroll_write', operation: 'scrollTop', time_ms: performance.now(),
                    target: identity(this), before: Reflect.apply(descriptor.get, this, []),
                    requested: ['number', 'string'].includes(typeof value) ? value : {type: typeof value},
                    stack: new Error('feed scrollTop write').stack, returned: false};
            } catch (error) {
                instrumentation.scroll_top = {state: 'unavailable', reason: String(error)};
            }
            try {
                const result = Reflect.apply(descriptor.set, this, [value]);
                if (row) row.returned = true;
                return result;
            } finally {
                try {
                    if (row) {
                        row.after = Reflect.apply(descriptor.get, this, []);
                        window.__ciUiEvents.push(row);
                    }
                } catch (error) {
                    instrumentation.scroll_top = {state: 'unavailable', reason: String(error)};
                }
            }
        }});
        instrumentation.scroll_top = {state: 'installed', target: '#chat-messages',
            getter_preserved: true, configurable: descriptor.configurable, enumerable: descriptor.enumerable};
    } catch (error) {
        instrumentation.scroll_top = {state: 'unavailable', reason: String(error)};
    }
})()"""

_FAILURE_GEOMETRY = """point => {
    const describe = element => {
        if (!(element instanceof Element)) return null;
        const css = getComputedStyle(element), rect = element.getBoundingClientRect();
        return {tag: element.tagName, id: element.id, class: String(element.className),
            task_id: element.dataset.taskId, scroll_top: element.scrollTop,
            scroll_height: element.scrollHeight, client_height: element.clientHeight,
            remaining: element.scrollHeight - element.scrollTop - element.clientHeight,
            overflow_y: css.overflowY, overscroll_y: css.overscrollBehaviorY,
            rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height}};
    };
    const events = window.__ciUiEvents || [];
    const wheel = [...events].reverse().find(event => event.type === 'wheel');
    const xy = wheel ? {x: wheel.x, y: wheel.y} : point;
    const chain = [];
    if (xy) for (let e = document.elementFromPoint(xy.x, xy.y); e; e = e.parentElement) {
        chain.push(describe(e));
    }
    const feed = document.querySelector('#chat-messages');
    return {time_ms: performance.now(), url: location.href,
        viewport: {width: innerWidth, height: innerHeight, device_scale: devicePixelRatio},
        document_focused: document.hasFocus(), visibility_state: document.visibilityState,
        active_element: describe(document.activeElement),
        status: document.querySelector('#chat-status')?.textContent,
        active_page: document.querySelector('.page.active')?.id,
        feed: describe(feed), hit_point: xy, hit_chain: chain,
        instrumentation: window.__ciUiInstrumentation || {state: 'unavailable', reason: 'observer missing'},
        nested_scroll: feed ? [...feed.querySelectorAll('*')].filter(e =>
            e.scrollHeight > e.clientHeight + 1 &&
            ['auto', 'scroll', 'overlay'].includes(getComputedStyle(e).overflowY)
        ).map(describe) : [], events};
}"""


def exception_fact(exc):
    """Only used in the isolated mock fixture, never for provider exceptions."""
    return {"type": type(exc).__name__, "message": str(exc)}


class FailureEvidence:
    """Buffer passive facts, capture before close, and flush after close."""

    def __init__(self, page, browser, output_dir, nodeid, engine):
        self.page, self.browser = page, browser
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.nodeid, self.engine = nodeid, engine
        self.timeline = []
        self.console = []
        self.details = {}
        self.errors = []
        self.point = None
        self.bundle = None
        self.tracing = False
        self.stage = "browser_setup"
        self.started = time.monotonic()

    def checkpoint(self, stage, **facts):
        if self.output_dir is not None:
            self.stage = stage
            self.timeline.append({"stage": stage, "elapsed_s": time.monotonic() - self.started,
                                  **copy.deepcopy(facts)})

    def _attempt(self, operation, action):
        try:
            return action()
        except Exception as exc:
            self.errors.append({"operation": operation, **exception_fact(exc)})
            return None

    def __enter__(self):
        if self.output_dir is not None:
            def start_trace():
                self.page.context.tracing.start(screenshots=True, snapshots=True, sources=False)
                self.tracing = True
            self._attempt("trace_start", start_trace)
            self._attempt("event_observer", lambda: self.page.add_init_script(_OBSERVE_EVENTS))
            self._attempt("console_observer", lambda: self.page.on("console", lambda msg:
                self.console.append({"type": msg.type, "text": msg.text})))
            self._attempt("pageerror_observer", lambda: self.page.on("pageerror", lambda exc:
                self.console.append({"type": "pageerror", **exception_fact(exc)})))
        return self

    def _directory(self):
        if self.bundle is None:
            identity = hashlib.sha256(f"{self.nodeid}:{self.engine}".encode()).hexdigest()[:16]
            self.bundle = self.output_dir / "browser" / f"{identity}-{uuid.uuid4().hex[:8]}"
            self.bundle.mkdir(parents=True)
        return self.bundle

    def _json(self, name, payload):
        (self._directory() / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _capture(self):
        directory = self._attempt("create_directory", self._directory)
        if directory is None:
            return
        geometry = self._attempt("geometry", lambda: self.page.evaluate(_FAILURE_GEOMETRY, self.point))
        self._attempt("geometry_write", lambda: self._json("geometry.json", geometry))
        self._attempt("screenshot", lambda: self.page.screenshot(
            path=str(directory / "screenshot.png"), timeout=5_000))
        self._attempt("dom", lambda: (directory / "page.html").write_text(
            self.page.content(), encoding="utf-8"))

    def __exit__(self, exc_type, exc, traceback):
        primary = exc
        failure_stage = self.stage
        if primary is not None:
            self.checkpoint("primary_failure", failure_stage=failure_stage,
                            exception=exception_fact(primary))
        if self.output_dir is not None:
            if primary is not None:
                self._capture()
            if self.tracing:
                path = self.bundle / "trace.zip" if self.bundle is not None else None
                self._attempt("trace_stop", lambda: self.page.context.tracing.stop(
                    **({"path": str(path)} if path is not None else {})))
        self.checkpoint("teardown_start")
        close_error = None
        try:
            self.browser.close()
        except BaseException as error:
            close_error = error
            self.errors.append({"operation": "browser_close", **exception_fact(error)})
            if primary is None:
                primary = error
                failure_stage = "browser_close"
        self.checkpoint("teardown_end")
        # Synchronous close may finish/abort fixture callbacks. Do not wait for
        # pending requests; export their last observed phase after this close.
        if self.output_dir is not None and (primary is not None or self.errors):
            self._attempt("metadata_write", lambda: self._json("evidence.json", {
                "nodeid": self.nodeid, "engine": self.engine,
                "failure_stage": failure_stage if primary else None,
                "primary_exception": exception_fact(primary) if primary else None,
                "diagnostics_incomplete": bool(self.errors), "capture_errors": self.errors,
                "os": platform.platform(), "python": sys.version,
                "playwright": importlib.metadata.version("playwright"),
                "browser_version": self.browser.version,
                "timeline": self.timeline, "console": self.console, "fixture": self.details,
            }))
            if self.errors:
                print("CI browser diagnostics_incomplete: " + ", ".join(
                    error["operation"] for error in self.errors), file=sys.stderr)
        if close_error is not None and exc is None:
            raise close_error
        return False
