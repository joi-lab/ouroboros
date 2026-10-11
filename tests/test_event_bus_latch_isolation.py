"""The supervisor event bus latch never leaks from one test into the next (conftest autouse)."""
from __future__ import annotations


def test_a_shutdown_latches_the_bus_for_this_test_only():
    from supervisor import workers

    workers.shutdown_event_q()
    assert workers._EVENT_Q_SHUTDOWN is True


def test_the_next_test_starts_with_an_open_bus():
    from supervisor import workers

    assert workers._EVENT_Q_SHUTDOWN is False
