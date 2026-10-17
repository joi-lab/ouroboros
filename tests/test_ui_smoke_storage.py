"""A real browser consumer for Dashboard -> Storage over the candidate server.

No route stub: the isolated server owns the synthetic data folder, so the assertions cover
the HTTP contract, the SPA's lifecycle wiring and the rendered report together.
"""

from __future__ import annotations

import json

import pytest

from tests.test_emoji_free_browser import _browser, _open, _server

direct_server_with_data = _server
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


def _assert_unknown_marker_inside_bar(page):
    """Excluded locations always leave something uncounted: its dashed marker keeps its own
    width inside the bar instead of being pushed past the counted 100% and clipped."""
    bar = page.locator(".storage-bar").bounding_box()
    marker = page.locator(".storage-bar-seg.is-unmeasured").bounding_box()
    assert bar and marker, (bar, marker)
    assert marker["width"] >= 8, marker
    assert bar["x"] <= marker["x"] and marker["x"] + marker["width"] <= bar["x"] + bar["width"] + 0.5, (bar, marker)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_storage_dashboard_real_server_consumer(direct_server_with_data, request, engine):
    """GET is passive; an explicit click measures and renders a truthful breakdown."""
    from playwright.sync_api import expect

    server = direct_server_with_data
    data_dir = server["data_dir"]
    # Distinct known markers; the server writes runtime files of its own, so a category only has
    # to include its marker, while the response arithmetic must still reconcile exactly.
    markers = {
        "observability": (data_dir / "observability" / "storage-consumer-call.json", 127),
        "task_drives": (data_dir / "state" / "headless_tasks" / "storage-consumer-task.bin", 223),
        "results": (data_dir / "task_results" / "storage-consumer-result.bin", 347),
        "archives": (data_dir / "archive" / "storage-consumer-archive.bin", 431),
        "memory": (data_dir / "memory" / "storage-consumer-memory.bin", 509),
        "skills": (data_dir / "skills" / "storage-consumer-skill.bin", 613),
        "other": (data_dir / "storage-consumer-other.bin", 751),
    }
    for path, size in markers.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes([size % 251]) * size)
    posts: list[str] = []

    def storage_get(response):
        return response.url.endswith("/api/storage") and response.request.method == "GET"

    with _browser(server, request, engine) as (page, evidence, errors):
        page.on("request", lambda req: posts.append(req.method)
                if req.url.endswith("/api/storage/measure") else None)
        _open(page, server["url"])
        page.locator('[data-nav-page="dashboard"]').click()
        page.wait_for_selector("#page-dashboard")
        with page.expect_response(storage_get, timeout=10_000) as first_read:
            page.locator('[data-dashboard-tab="storage"]').click()
        expect(page.locator(".storage-summary")).to_contain_text("Not measured yet", timeout=10_000)
        assert posts == [], "opening the tab must not start a measurement"
        assert first_read.value.json() == {"measuring": False, "measurement": None, "error": None}

        measure = page.locator('[data-storage-action="measure"]')
        expect(measure).to_have_text("Measure now")
        with page.expect_response(lambda response: response.url.endswith("/api/storage/measure"),
                                  timeout=10_000) as posted:
            measure.click()
        # A folder this small can be measured before the POST answers, so a running walk and a
        # finished one are both legitimate answers; the transient busy button is pinned by the
        # controller test (web/tests/dashboard_read_states.test.js), not raced here.
        started = posted.value.json()
        assert posted.value.status == 202 and started["started"] is True
        assert started["measuring"] or started["measurement"], started
        page.wait_for_selector('[data-storage-group="exact"]', timeout=30_000)
        expect(measure).to_have_text("Refresh", timeout=30_000)
        expect(measure).to_have_attribute("aria-disabled", "false")

        report = page.evaluate("async () => (await fetch('/api/storage', {cache: 'no-store'})).json()")["measurement"]
        categories = {row["id"]: row for row in report["categories"]}
        for category, (_path, size) in markers.items():
            assert categories[category]["bytes"] >= size, (category, categories[category])
        assert report["measured_bytes"] == sum(row["bytes"] for row in report["categories"] if row["bytes"] is not None)
        assert posts == ["POST"]

        # Every group, a counted-only bar, file counts and the duration; excluded locations stay
        # explicit instead of zero-sized rows, and observability is one group head, not a nested row.
        for group in ("exact", "kept", "temporary", "other", "unmeasured"):
            expect(page.locator(f'[data-storage-group="{group}"]')).to_be_visible()
        assert page.locator(".storage-bar-seg").count() >= len(markers)
        expect(page.locator(".storage-summary")).to_contain_text("Measured data")
        expect(page.locator(".storage-summary")).to_contain_text("took")
        expect(page.locator('[data-storage-group="exact"]')).to_contain_text("Model call records")
        expect(page.locator('[data-storage-group="unmeasured"]')).to_contain_text("Size unknown")
        assert page.locator(".storage-coverage svg").count() <= 1, "the info mark is one stroke vector"
        _assert_unknown_marker_inside_bar(page)

        # A transient read failure keeps the last measurement beside the failure.
        failed = {"remaining": 1}

        def fail_one_read(route):
            if route.request.method == "GET" and failed["remaining"]:
                failed["remaining"] -= 1
                route.fulfill(status=503, content_type="application/json",
                              body=json.dumps({"error": "synthetic storage read refusal"}))
                return
            route.continue_()

        page.route("**/api/storage", fail_one_read)
        page.locator('[data-dashboard-tab="logs"]').click()
        page.locator('[data-dashboard-tab="storage"]').click()
        expect(page.locator(".storage-status")).to_contain_text("could not be read", timeout=10_000)
        expect(page.locator(".storage-summary")).to_contain_text("Measured")
        expect(page.locator('[data-storage-group="exact"]')).to_be_visible()
        page.unroute("**/api/storage", fail_one_read)
        page.screenshot(path=str(evidence / "storage-desktop.png"), full_page=True)

        page.set_viewport_size({"width": 390, "height": 844})
        if page.locator("body").evaluate("node => node.classList.contains('nav-drawer-open')"):
            if page.locator("#nav-drawer-backdrop").is_visible():
                page.locator("#nav-drawer-backdrop").click(position={"x": 380, "y": 20})
            page.wait_for_timeout(300)
        page.screenshot(path=str(evidence / "storage-narrow.png"), full_page=True)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 2, f"storage dashboard overflows the narrow viewport by {overflow}px"
        _assert_unknown_marker_inside_bar(page)
        assert not errors, errors
