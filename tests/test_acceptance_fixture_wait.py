"""The controlled review fixture must wait for admitted, not merely started, work."""
from __future__ import annotations

import threading

import pytest

from ouroboros import review_substrate
from ouroboros.acceptance_settlement import panel_awaiting_this_turn
from tests import test_acceptance_async_loop as async_tests
from tests import test_acceptance_optional_control as optional_tests

full_loop = async_tests.full_loop


@pytest.mark.parametrize("earlier_settlement", [False, True])
def test_park_waits_for_current_operation_before_its_transport_starts(
    full_loop, monkeypatch, earlier_settlement,
):
    f = full_loop
    target = async_tests.ANSWER + (" Budget: $12." if earlier_settlement else "")
    entered, release = threading.Event(), threading.Event()
    factory = review_substrate._review_route_executor
    original_park = f.park
    checked = []

    def gated_factory(assignment, **kwargs):
        executor = factory(assignment, **kwargs)
        if assignment.request.subject == target:
            execute = executor.execute

            def gated_execute():
                entered.set()
                assert release.wait(10), "test did not release the current reviewer"
                return execute()

            executor.execute = gated_execute
        return executor

    def park(ctx, checkpoint):
        run = panel_awaiting_this_turn(ctx, ctx._execution_trace)
        if (run or {}).get("request", {}).get("subject") != target:
            return original_park(ctx, checkpoint)
        wait_for = f.condition.wait_for

        def check_wait(predicate, timeout=None):
            assert entered.wait(10), "current reviewer did not reach the transport gate"
            assert f.settled_count == len(f.review_sends) == int(earlier_settlement)
            # Force the race before a current send exists. A previous settlement
            # (or 0 >= 0) cannot release the awaited operation.
            assert not predicate(), "review wait completed before its current transport started"
            checked.append(True)
            release.set()
            return wait_for(predicate, timeout)

        with monkeypatch.context() as patcher:
            patcher.setattr(f.condition, "wait_for", check_wait)
            return original_park(ctx, checkpoint)

    monkeypatch.setattr(review_substrate, "_review_route_executor", gated_factory)
    f.ctx.owner_wait_callback = park
    try:
        if earlier_settlement:
            optional_tests.test_return_order_preserves_feedback_identity_and_new_subjects(
                f, monkeypatch, "ready", "effect",
            )
        else:
            async_tests.test_automatic_completion_uses_the_same_retained_candidate_and_free_collect(
                f, monkeypatch,
            )
        assert checked == [True]
    finally:
        release.set()
        f.release.set()
        if entered.is_set():
            with f.condition:
                assert f.condition.wait_for(
                    lambda: f.settled_count >= int(earlier_settlement) + 1, timeout=10,
                ), "reviewer did not finish after releasing the test gate"
