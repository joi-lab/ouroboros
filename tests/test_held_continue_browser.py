"""#1721 in the real browser and server: a held Continue names its observed blocker everywhere.

The candidate server runs from the candidate bytes (``direct_server_with_data``); the predecessor's
interruption and its unbound delegated start are real durable rows; the Continue press, the Activity
read and the Resume refusal go through the real endpoints. The toast is transient, so the persistent
Activity row must keep the cause readable on its own: on a phone and a desktop, in both themes and both
engines, once every toast is gone. Screenshots for visual review: ``OUROBOROS_UI_EVIDENCE_DIR``, else
``<data>/ui-evidence/held-continue``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.test_continue_independent_review import _current_pool, _pending, _rows  # noqa: F401 - pytest fixture
from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
UNBOUND = "a delegated start has an unknown outcome (no run was recorded)"
# Where the cause text sits against its row and the row's one control: the cause must be laid out whole
# (a clipped or ellipsized line still has its full innerText) and never under the control.
ROW_GEOMETRY = """(row) => {
    const sub = row.querySelector('.activity-sub');
    const control = row.querySelector('[data-act="task-control"]');
    const range = document.createRange();
    range.selectNodeContents(sub);
    const lines = [...range.getClientRects()].filter((r) => r.width > 0);
    const box = row.getBoundingClientRect(), ctl = control.getBoundingClientRect();
    return {
        clipped: sub.scrollWidth > sub.clientWidth + 1 || sub.scrollHeight > sub.clientHeight + 1,
        outside_row: lines.some((r) => r.left < box.left - 0.5 || r.right > box.right + 0.5
            || r.bottom > box.bottom + 0.5),
        under_control: lines.some((r) => r.right > ctl.left && r.left < ctl.right
            && r.bottom > ctl.top && r.top < ctl.bottom),
        control_on_screen: ctl.left >= 0 && ctl.right <= innerWidth && ctl.width > 0,
        lines: new Set(lines.map((r) => Math.round(r.top))).size,
        selectable: getComputedStyle(sub).userSelect !== 'none',
        theme: document.documentElement.dataset.theme,
    };
}"""


def _toasts_gone(page):
    """The transient toast expires by itself (6 s); only the Activity row remains to say why."""
    page.wait_for_function("() => !document.querySelector('#toast-stack .toast')", timeout=15000)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_held_continue_toast_activity_reason_and_resume_refusal(direct_server_with_data, engine):  # noqa: F811
    from playwright.sync_api import sync_playwright

    from ouroboros import delegate_custody as dc
    from tests.test_continue_independent_review import PRED
    from tests.test_owner_continue import _interrupted

    server = direct_server_with_data
    root = server["data_dir"]
    server["stop_server"]()
    _interrupted(root, PRED, origin={"source": "owner_restart", "reason": "owner_restart", "scope": "single"})
    server["start_server"]()
    # Seeded while the server runs (as G1 seeds intents): a boot would try to reconcile the start
    # with a harness daemon this isolated home does not have.
    _rows(root, leaf=False, review=False)
    _pending(root, "inv-unbound", source=None)  # its start outcome is unknown: no run id was recorded
    dc._CUSTODY.clear()
    evidence = Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR") or root / "ui-evidence") / "held-continue"
    evidence.mkdir(parents=True, exist_ok=True)
    observed = {}
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 960}, color_scheme="dark")
            page.goto(server["url"], wait_until="domcontentloaded")
            # The card's own Continue door (task_continue.continueTaskAction) against the real endpoint.
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/continue")) as sent:
                successor = page.evaluate(
                    "async (id) => (await import('/static/modules/task_continue.js')).continueTaskAction(id)", PRED)
            ack = sent.value.json()
            assert ack["ok"] and ack["held"] is True and successor == ack["successor_task_id"], ack
            assert ack["blockers"][0]["stop_outcome"] == "pending_invocation_unbound", ack
            toast = page.locator("#toast-stack .toast").filter(has_text="Continue accepted and held")
            toast.wait_for()
            assert toast.inner_text().strip() == f"Continue accepted and held: {UNBOUND}. Resume in Activity checks it again."
            page.wait_for_function("() => [...document.querySelectorAll('#toast-stack .toast')]"
                                   ".every(el => Number(getComputedStyle(el).opacity) > 0.95)")
            page.screenshot(path=str(evidence / f"{engine}-1-continue-held-toast.png"), full_page=True)

            page.click('[data-nav-page="dashboard"]')
            page.click('[data-dashboard-tab="activity"]')
            row = page.locator('[data-activity-section="queue"] .activity-row').filter(has_text="held: earlier work")
            row.wait_for()
            meta = row.locator(".activity-sub").inner_text().strip()
            assert meta.startswith(f"held: earlier work not proven settled · {UNBOUND}"), meta
            assert "paused (budget)" not in row.inner_text()
            control = row.locator('[data-act="task-control"]')
            assert control.get_attribute("data-budget-paused") == "1", "its existing Resume is the one action"
            _toasts_gone(page)
            for theme in ("dark", "light"):
                page.emulate_media(color_scheme=theme)  # System, the default choice, follows the OS
                page.wait_for_function("(t) => document.documentElement.dataset.theme === t", arg=theme)
                for width in (1440, 390):
                    page.set_viewport_size({"width": width, "height": 960})
                    if width < 700:
                        page.wait_for_function(
                            "() => document.querySelector('#primary-sidebar').getBoundingClientRect().right <= 1")
                    row.scroll_into_view_if_needed()
                    geometry = row.evaluate(ROW_GEOMETRY)
                    observed[f"{theme}-{width}"] = geometry
                    assert geometry["theme"] == theme, geometry
                    assert not geometry["clipped"] and not geometry["outside_row"], (theme, width, geometry)
                    assert not geometry["under_control"] and geometry["control_on_screen"], (theme, width, geometry)
                    assert geometry["selectable"], "the cause stays selectable content"
                    if width < 700:
                        assert geometry["lines"] > 1, "the phone row wraps the cause instead of cutting it"
                    else:
                        assert geometry["lines"] == 1, "a desktop row keeps its one compact line"
                    row.screenshot(path=str(evidence / f"{engine}-2-activity-held-row-{theme}-{width}.png"))
                    page.screenshot(path=str(evidence / f"{engine}-2-activity-page-{theme}-{width}.png"),
                                    full_page=True)
            page.emulate_media(color_scheme="dark")

            # At phone width and from the keyboard: the row's one control opens its menu, Resume rechecks.
            control.focus()
            page.keyboard.press("Enter")
            item = page.locator('[data-task-control="resume"]')
            item.wait_for()
            item.focus()
            with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/resume")) as resumed:
                page.keyboard.press("Enter")
            assert resumed.value.status == 409, resumed.value.status
            body = resumed.value.json()
            assert body["error"] == "predecessor_writers_unsettled" and "action" not in body, body
            refusal = page.locator("#toast-stack .toast-danger").filter(has_text="Resume refused")
            refusal.wait_for()
            assert refusal.inner_text().strip() == f"Resume refused: {UNBOUND}."
            page.wait_for_function("() => [...document.querySelectorAll('#toast-stack .toast')]"
                                   ".every(el => Number(getComputedStyle(el).opacity) > 0.95)")
            page.screenshot(path=str(evidence / f"{engine}-3-resume-refused-toast-390.png"), full_page=True)
            _toasts_gone(page)
            # The refusal changed nothing: the same row still says why, whole, and focus is back on its control.
            after = row.evaluate(ROW_GEOMETRY)
            assert not after["clipped"] and not after["under_control"] and after["lines"] > 1, after
            assert row.locator(".activity-sub").inner_text().strip() == meta
            assert page.evaluate("() => document.activeElement?.dataset?.act") == "task-control"
            row.screenshot(path=str(evidence / f"{engine}-4-activity-held-row-after-refusal-390.png"))
            (evidence / f"{engine}-facts.json").write_text(json.dumps({
                "continue_ack": ack, "activity_meta": meta, "row_geometry": observed, "after_refusal": after,
                "resume_refusal": body, "candidate": server["candidate_identity"]}, indent=2, default=str))
            print("HELD_CONTINUE_EVIDENCE", engine, evidence)
        finally:
            browser.close()
