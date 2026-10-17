"""Immutable promotion shares storage without sharing a mutable publication."""

import errno
import shutil
from pathlib import Path

import pytest

from ouroboros import artifacts, headless, observability, source_retention
from ouroboros.review_source_closure import retain_review_request_sources
from ouroboros.review_substrate import ReviewRequest


@pytest.fixture(params=[None, errno.EXDEV, errno.EPERM, errno.EACCES, errno.EOPNOTSUPP])
def link_error(request, monkeypatch):
    if request.param is not None:
        def unsupported(*_args, **_kwargs):
            raise OSError(request.param, "link unavailable in fixture")
        monkeypatch.setattr(source_retention.os, "link", unsupported)
    return request.param


def test_promoted_blob_shares_inode_or_copies_and_survives_child_removal(tmp_path, link_error):
    parent, task = tmp_path / "canonical", "link-task"
    child = headless.prepare_task_drive(parent, task, "empty")
    payload = {"messages": [{"role": "user", "content": "Retained Ω evidence"}]}
    call = observability.persist_call(child, task_id=task, call_id="request", call_type="llm_request", payload=payload)
    original = Path(call["redacted_projection_ref"]["path"])
    compressed, inode = original.read_bytes(), original.stat().st_ino
    _, state = observability.promote_child_task_refs(parent, child, task, {})
    assert state["status"] == "complete" and state["call_inventory_preserved"]
    destination = parent / "observability" / "blobs" / original.name
    assert destination.read_bytes() == compressed
    assert (destination.stat().st_ino == inode) is (link_error is None)
    if observability.posix_private_modes_supported():
        assert destination.stat().st_mode & 0o777 == 0o600
    for root in (parent, child):
        observability.write_blob(root, payload)
    assert original.stat().st_ino == inode and original.read_bytes() == compressed
    assert destination.read_bytes() == compressed
    shutil.rmtree(child)
    _, restored, _ = observability.read_call_payload(parent, task_id=task, call_id="request")
    assert restored == {**payload, "send_messages": payload["messages"]}


def test_nested_review_inputs_share_checkpoint_or_copy_on_link_failure(tmp_path, link_error):
    task, raw = "review-task", b'{"messages": [{"content": "exact checkpoint"}]}'
    ref = artifacts.store_actor_source_bytes(tmp_path, task, category="context_checkpoints",
                                            source_id="checkpoint", data=raw, extension="json")
    original = artifacts.task_artifact_dir_path(tmp_path, task) / ref["path"]
    request = ReviewRequest(surface="plan_review", task_id=task, goal="review", subject="plan",
                            evidence={"source_refs": [ref]})
    retain_review_request_sources(request, source_root=tmp_path, custody_root=tmp_path)
    read_root = Path(request.policy["review_source_closure"]["read_root"])
    retained = artifacts.task_artifact_dir_path(read_root, task) / ref["path"]
    assert retained.read_bytes() == raw
    assert (retained.stat().st_ino == original.stat().st_ino) is (link_error is None)
    # The normal writer reuses the published inode; removing one placement
    # leaves the other independently owned by the review's retained root.
    inode = original.stat().st_ino
    assert artifacts.store_actor_source_bytes(tmp_path, task, category="context_checkpoints",
        source_id="checkpoint", data=raw, extension="json") == ref
    assert original.stat().st_ino == inode and retained.read_bytes() == raw
    original.unlink()
    assert artifacts.read_actor_source_bytes(read_root, task, ref) == raw


def test_failed_blob_publication_retains_child_until_retry(tmp_path, monkeypatch, link_error):
    from ouroboros import utils
    from ouroboros.task_results import write_task_result

    parent, task = tmp_path / "canonical", "failed-placement"
    child = headless.prepare_task_drive(parent, task, "empty")
    payload = {"result": "exact evidence survives failed publication"}
    observability.persist_call(child, task_id=task, call_id="call", call_type="tool", payload=payload)
    write_task_result(child, task, "completed", result="answer", artifact_status="ready")
    headless.copy_child_task_result(parent, {"id": task, "drive_root": str(child)})
    replace, attempts = utils.replace_atomic, []

    def fail_blob_publication(source, destination, *args, **kwargs):
        if destination.parent == parent / "observability" / "blobs":
            original = child / "observability" / "blobs" / destination.name
            assert source.read_bytes() == original.read_bytes()
            assert (source.stat().st_ino == original.stat().st_ino) is (link_error is None)
            attempts.append(source)
            raise OSError("blob publication failed")
        return replace(source, destination, *args, **kwargs)

    with monkeypatch.context() as failure:
        failure.setattr(utils, "replace_atomic", fail_blob_publication)
        copied = headless.retry_child_task_refs(parent, child, task)
        assert copied["child_ref_promotion"]["status"] == "incomplete"
        assert copied["child_ref_promotion"]["pending_refs"]
        assert headless.remove_subagent_task_drive(parent, task, live=lambda _task: False) is False
        assert child.exists() and attempts and all(not path.exists() for path in attempts)
    copied = headless.retry_child_task_refs(parent, child, task)
    assert copied["child_ref_promotion"]["status"] == "complete"
    assert headless.remove_subagent_task_drive(parent, task, live=lambda _task: False) is True
    assert not child.exists()
    assert observability.read_call_payload(parent, task_id=task, call_id="call")[1] == payload
