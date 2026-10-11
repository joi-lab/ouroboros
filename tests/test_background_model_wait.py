"""Model-wait facts that survive the Background Consciousness redesign: an owner
without a task ceiling keeps lexical deadlines, an image window is not a cycle
deadline, and an auth wait preserves the profile intent. (A wake-up is an ordinary
Main turn now; its model waits are the direct turn's own — see test_model_wait.)"""

import json

import pytest

from ouroboros import config, model_wait
from ouroboros import llm_claudexor as transport
from tests.test_llm_claudexor import MODEL


def test_image_window_fallback_does_not_create_a_ceilingless_owner_deadline(tmp_path, monkeypatch):
    from ouroboros.tools.vision import _vision_execution_window

    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 900)
    with model_wait.task_model_wait_scope(task={"id": "ceilingless"}, drive_root=tmp_path,
                                          event_queue=None, worker_slot_held=False, owner_control=lambda: None) as owner:
        owner.started_monotonic = 0
        assert owner.execution_window_remaining() is None
        assert _vision_execution_window() == 900
        assert owner.control_reason() is None


def test_ceilingless_owner_retains_lexical_deadlines_without_task_ceiling(tmp_path, monkeypatch):
    owner = model_wait.TaskModelWait(task={"id": "ceilingless"}, drive_root=tmp_path,
                                     event_queue=None, worker_slot_held=False, owner_control=lambda: None)
    owner.started_monotonic = 0
    monkeypatch.setattr(config, "get_task_abs_ceiling_sec", lambda: 0)
    assert owner.control_reason() is None and owner.execution_window_remaining() is None
    with model_wait.task_model_wait_scope(task={"id": "ceilingless"}, drive_root=tmp_path,
                                          event_queue=None, worker_slot_held=False, owner_control=lambda: None) as bound:
        with model_wait.execution_deadline_scope(model_wait.monotonic_now() - 1):
            assert bound.control_reason() == "execution_deadline"
        with model_wait.calendar_scope("2000-01-01T00:00:00Z"):
            assert bound.control_reason() == "deadline"


@pytest.mark.parametrize("override,problem_context,expected", [
    (None, {}, "configured-pin"), ("", {}, ""),
    (None, {"credentialProfileId": "proved-profile"}, "proved-profile"),
])
def test_auth_wait_profile_hint_preserves_intent_without_inventing_route(tmp_path, monkeypatch, override, problem_context, expected):
    monkeypatch.setenv("OUROBOROS_MODEL_ACCOUNTS", json.dumps({"main": "configured-pin"}))
    waiter = model_wait.TaskModelWait(task={"id": "actor"}, drive_root=tmp_path,
                                      event_queue=None, worker_slot_held=False,
                                      row_mutator=lambda key, fn: model_wait.mutate_live_wait(waiter, key, fn),
                                      owner_control=lambda: "stopped")
    error = transport.ClaudexorModelError({"code": "auth_required", "context": problem_context},
                                          model_role="main", route={})
    with pytest.raises(model_wait.ModelWaitInterrupted):
        waiter.wait(None, error, {"model": MODEL, "model_role": "main", "model_account_override": override})
    row, = waiter.waits.values()
    assert row["credential_profile_id"] == expected
    assert error.route == {}  # The UI hint is not claimed as an actual provider route.
