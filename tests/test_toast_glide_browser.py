"""A dismissed toast fades where it stood while the toasts that stay glide into its place (docs/DESIGN.md §10
"Motion"; the R2 board: the one below jumped 53 px in one frame). With reduced motion they take their place at once.
Uses the static-server fixture; never starts the runtime or reads live data."""
from __future__ import annotations

import json

import pytest

from tests import test_subscription_setup_browser as setup_browser
from tests.test_design_system_gate_browser import save_evidence, seed_page, settle

subscription_ui = setup_browser.subscription_ui
pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]


@pytest.fixture(params=['webkit', 'chromium'])
def toast_ui(request, monkeypatch):
    monkeypatch.setenv('OUROBOROS_UI_BROWSER_ENGINE', request.param)
    ui = request.getfixturevalue('subscription_ui')
    ui['engine'] = request.param
    return ui


# Three toasts, then the first dismissed: the top of the one that was second, frame by frame, until it rests.
GLIDE = """async () => {
    const { showToast } = await import('/static/modules/toast.js');
    const toasts = ['First notice', 'Second notice', 'Third notice'].map((text) => showToast(text, 'info', { ttl: 0 }));
    await new Promise((resolve) => setTimeout(resolve, 400));  // past their own arrival
    const second = toasts[1];
    const tops = [second.getBoundingClientRect().top];
    toasts[0].click();
    for (let i = 0; i < 40; i += 1) {
        await new Promise((resolve) => requestAnimationFrame(resolve));
        tops.push(second.getBoundingClientRect().top);
    }
    return { tops, gone: !toasts[0].isConnected, stack: document.querySelectorAll('#toast-stack .toast').length };
}"""


@pytest.mark.parametrize('reduce', [False, True], ids=['motion', 'reduced'])
def test_a_dismissed_toast_leaves_and_the_one_below_glides_into_its_place(toast_ui, tmp_path, reduce):
    page = seed_page(toast_ui, 1280, 800, touch=False)
    if reduce:
        page.emulate_media(reduced_motion='reduce')
    facts = {'engine': toast_ui['engine'], 'reduce': reduce}
    try:
        page.goto(toast_ui['url'] + '/')
        page.wait_for_selector('#chat-input', state='attached')
        settle(page)
        facts.update(page.evaluate(GLIDE))
    finally:
        save_evidence(tmp_path, f'toast-glide-{toast_ui["engine"]}-{"reduced" if reduce else "motion"}', facts)
        print(json.dumps(facts))
        page.close()
    tops = facts['tops']
    steps = [round(b - a, 2) for a, b in zip(tops, tops[1:])]
    travel = tops[0] - tops[-1]
    assert facts['gone'] and facts['stack'] == 2, facts
    assert travel > 30, ('the one below took the place of the one that left', facts)
    assert all(step <= 0.5 for step in steps), ('it only ever moves up into the place', steps)
    if reduce:
        assert sum(1 for step in steps if step < -0.5) <= 1, ('at once with reduced motion', steps)
    else:
        assert max(-step for step in steps) < travel * 0.6, ('no one-frame jump into the place', steps)
        assert sum(1 for step in steps if step < -0.5) >= 3, ('it glides over several frames', steps)
