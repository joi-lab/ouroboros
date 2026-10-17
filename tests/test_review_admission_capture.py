"""An admitted physical reviewer keeps its Pause allowance without a post-send read."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from ouroboros import _usage_wait, owner_pause, queue_observation, store
from ouroboros.task_results import write_task_result


@pytest.fixture
def source(tmp_path):
    write_task_result(tmp_path, "root", "running", root_task_id="root")
    return SimpleNamespace(task_id="root", root_task_id="root", drive_root=tmp_path,
                           task_metadata={}, task_lifecycle_bound=True)


def test_busy_after_physical_handoff_cannot_erase_started_review_allowance(source, monkeypatch):
    original = owner_pause.read_fence
    sent, after_send_reads = [], []
    blocked = False

    def read(*args, **kwargs):
        if blocked:
            after_send_reads.append(True)
            raise store.StoreBusy("busy after physical handoff", reason="contention")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner_pause, "read_fence", read)
    reservation = SimpleNamespace(scope=source, attempt_id="physical-review")
    with ThreadPoolExecutor(max_workers=1) as executor, owner_pause.review_episode("root") as episode:
        def submit(*args):
            nonlocal blocked
            future = executor.submit(*args)
            sent.append(True)
            blocked = True
            return future

        future = _usage_wait.retry_presend(
            lambda: owner_pause.submit_model(reservation, submit, lambda: "actual result"), check=False)
        assert future.result() == "actual result" and sent == [True]
        assert owner_pause.episode_armed(episode) and after_send_reads == []
        blocked = False
        owner_pause.install_fence(source.drive_root, "root", request_id="later-pause")
        assert owner_pause.member_fence(source) == {}, "this same started check may finish"
    assert owner_pause.member_fence(source), "the author and new work remain paused"


def test_capture_contention_retries_before_handoff_with_no_launch_lock_held(source, monkeypatch):
    original = owner_pause.read_fence
    calls, waits, sent = [], [], []

    def read(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:  # the same pre-send read now validates and captures the generation
            raise store.StoreBusy("admitted generation contended", reason="contention")
        return original(*args, **kwargs)

    def wait(_seconds):
        assert not sent
        with owner_pause.launch_lock(source.drive_root, "root", wait=False):
            waits.append(True)

    monkeypatch.setattr(owner_pause, "read_fence", read)
    monkeypatch.setattr(queue_observation, "_sleep", wait)
    reservation = SimpleNamespace(scope=source, attempt_id="same-physical-review")
    with ThreadPoolExecutor(max_workers=1) as executor, owner_pause.review_episode("root") as episode:
        def submit(*args):
            sent.append(True)
            return executor.submit(*args)
        future = _usage_wait.retry_presend(
            lambda: owner_pause.submit_model(reservation, submit, lambda: "answer"), check=False)
        assert future.result() == "answer"
        assert waits == [True] and sent == [True] and owner_pause.episode_armed(episode)


@pytest.mark.parametrize("boundary", ["model", "tool", "operation"])
def test_failed_handoff_never_arms_and_admission_does_not_escape_its_scope(source, monkeypatch, boundary):
    def refused(*args):
        raise RuntimeError("executor did not accept")

    with owner_pause.review_episode("root") as episode:
        with pytest.raises(RuntimeError, match="did not accept"):
            if boundary == "model":
                owner_pause.submit_model(SimpleNamespace(scope=source, attempt_id="not-sent"), refused, lambda: None)
            elif boundary == "tool":
                owner_pause.submit_tool(source, "read_file", refused, lambda: None)
            else:
                with owner_pause.operation_start(source):
                    refused()
        assert not owner_pause.episode_armed(episode)
        monkeypatch.setattr(owner_pause, "read_fence", lambda *args: pytest.fail("post-admission store read"))
        owner_pause.arm_review_episode(source)
        assert not owner_pause.episode_armed(episode), "an old admitted value grants nothing outside its scope"
