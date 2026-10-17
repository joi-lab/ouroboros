"""Fresh cancellation checks use the addressed status, not an unrelated body."""
import pytest

from ouroboros import cancel_intents, store, task_store
from ouroboros.task_results import write_task_result


@pytest.mark.parametrize("status,pending", [("running", False), ("cancel_requested", True), ("completed", False)])
def test_cancel_latch_reads_no_body_and_observes_each_status_write(tmp_path, monkeypatch, status, pending):
    write_task_result(tmp_path, "task", "running", payload="x" * 100_000)
    monkeypatch.setattr(cancel_intents, "has_active_intent", lambda *a, **k: False)
    original = store.Txn.body_text
    reads = []
    def read(txn, task_id):
        reads.append(task_id)
        return original(txn, task_id)
    monkeypatch.setattr(store.Txn, "body_text", read)
    assert not cancel_intents.cancel_pending(tmp_path, "task", propagate_busy=True)
    assert reads == []
    write_task_result(tmp_path, "task", status)
    reads.clear()
    assert cancel_intents.cancel_pending(tmp_path, "task", propagate_busy=True) is pending
    assert reads == []


def test_strict_cancel_check_retains_body_identity_validation(tmp_path, monkeypatch):
    write_task_result(tmp_path, "task", "running")
    monkeypatch.setattr(cancel_intents, "has_active_intent", lambda *a, **k: False)
    monkeypatch.setattr(store.Txn, "body_text", lambda *_a: '{"task_id":"other","status":"running"}')
    with pytest.raises(ValueError, match="unreadable or invalid"):
        cancel_intents.cancel_pending(tmp_path, "task", strict=True)


def test_active_intent_remains_authoritative_without_status_read(tmp_path, monkeypatch):
    monkeypatch.setattr(cancel_intents, "has_active_intent", lambda *a, **k: True)
    monkeypatch.setattr(task_store, "task_facts", lambda *a, **k: pytest.fail("intent already proves cancellation"))
    assert cancel_intents.cancel_pending(tmp_path, "task", propagate_busy=True)


@pytest.mark.parametrize("propagate", [False, True])
def test_addressed_cancel_read_keeps_contention_contract(tmp_path, monkeypatch, propagate):
    monkeypatch.setattr(cancel_intents, "has_active_intent", lambda *a, **k: False)
    def busy(*_a, **_k):
        raise store.StoreBusy("current status", reason="contention")
    monkeypatch.setattr(task_store, "task_facts", busy)
    if propagate:
        with pytest.raises(store.StoreBusy):
            cancel_intents.cancel_pending(tmp_path, "task", propagate_busy=True)
    else:
        assert not cancel_intents.cancel_pending(tmp_path, "task")
