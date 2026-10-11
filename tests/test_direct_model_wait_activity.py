"""Direct Project model-wait census uses the live owner through the JS reducer."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ouroboros import config, model_wait
from ouroboros.gateway import state as gateway_state
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from ouroboros.settings_integrity import TaskSettingsSnapshot
from ouroboros.task_results import write_task_result
from supervisor import queue as supervisor_queue
from supervisor.active_activity import get_direct_activity_registry

NODE_BIN = (
    str(Path.home() / ".claudexor" / "node" / "bin" / "node")
    if (Path.home() / ".claudexor" / "node" / "bin" / "node").exists()
    else "node"
)
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def _js_project_summary(rows: list[dict]) -> dict:
    script = """
import { buildProjectActivityIndex } from './modules/project_activity.js';
let raw = '';
for await (const chunk of process.stdin) raw += chunk;
const rows = JSON.parse(raw);
const summary = buildProjectActivityIndex(rows).byProject.get('project-1');
process.stdout.write(JSON.stringify(summary || null));
"""
    result = subprocess.run(
        [NODE_BIN, "--input-type=module", "-e", script],
        input=json.dumps(rows), text=True, capture_output=True, cwd=WEB_ROOT,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class _ModelCatalog:
    """A deterministic live catalog that resumes one real TaskModelWait owner."""

    available = False

    def claudexor_model_sources(self):
        return {"sources": [{"id": "codex", "credentialHarness": "fixture-harness"}]}

    def claudexor_model_catalog(self, source, _account=None, *, requested_model=None):
        assert source == "codex"
        assert requested_model == "exact-model"
        models = [{"id": "exact-model"}] if self.available else []
        return {"source": source, "credentialProfileId": "fixture", "models": models}


def test_direct_model_wait_owner_gap_keeps_static_row_and_discloses_partial():
    registry = get_direct_activity_registry()
    registry.clear()
    owner = SimpleNamespace(task_id="direct-project", closed=False, snapshot=lambda: (_ for _ in ()).throw(OSError("owner")))
    actor = SimpleNamespace(tools=SimpleNamespace(_ctx=SimpleNamespace(model_wait_context=owner)))
    registry.register("direct-project", 7, project_id="project-1", actor=actor)
    try:
        availability = {"complete": True}
        rows = gateway_state._direct_turns_snapshot_safe(availability=availability)
        assert rows[0]["activity_id"] == "direct-project"
        assert "model_waits" not in rows[0]
        assert availability["complete"] is False
    finally:
        registry.clear()


@pytest.mark.serial
@pytest.mark.parametrize(
    ("refusal_code", "wait_reason"),
    [("subscription_window_exhausted", "quota"), ("auth_required", "auth")],
)
def test_direct_project_model_wait_census_reaches_js_and_resumes(
    tmp_path, monkeypatch, refusal_code, wait_reason,
):
    """The real direct registry -> gateway census carries current wait evidence.

    The wait row is produced by ``TaskModelWait.wait`` from a typed quota/auth
    refusal; the test never seeds a model-wait row or reads history to rebuild
    one.  The same live owner then resumes on a compatible catalog and remains
    open long enough for the current-attempt projection to be observed again.
    """
    registry = get_direct_activity_registry()
    registry.clear()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setattr(supervisor_queue, "PENDING", [])
    monkeypatch.setattr(supervisor_queue, "RUNNING", {})
    monkeypatch.setattr(supervisor_queue, "BUDGET_ROOT_FENCES", {})
    monkeypatch.setattr(config, "CLAUDEXOR_MODEL_POLL_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_START_SEC", 0.01)
    monkeypatch.setattr(config, "NETWORK_WAIT_BACKOFF_MAX_SEC", 0.01)

    task = {
        "id": "direct-project",
        "chat_id": 7,
        "_attempt": 2,
        "_is_direct_chat": True,
        "metadata": {"project_id": "project-1"},
    }
    write_task_result(tmp_path, task["id"], "running", chat_id=task["chat_id"])
    actor = SimpleNamespace(
        _busy=True,
        _accepting_owner_messages=True,
        _current_task_id=task["id"],
        _current_chat_id=task["chat_id"],
        _current_task_metadata=task["metadata"],
        _current_task_text="wait for access",
        tools=SimpleNamespace(_ctx=SimpleNamespace(model_wait_context=None)),
    )
    registry.register(task["id"], task["chat_id"], project_id="project-1", actor=actor)

    catalog = _ModelCatalog()
    owner_ready = threading.Event()
    resumed = threading.Event()
    keep_owner_open = threading.Event()
    failures: list[BaseException] = []

    def run_wait():
        try:
            with config.task_settings_scope(TaskSettingsSnapshot(settings={}, environ={})):
                with model_wait.task_model_wait_scope(
                    task=task, drive_root=tmp_path, event_queue=queue.Queue(), worker_slot_held=False,
                ) as owner:
                    actor.tools._ctx.model_wait_context = owner
                    owner_ready.set()
                    owner.wait(
                        catalog,
                        ClaudexorUnavailable(refusal_code, f"fixture {wait_reason}"),
                        {"model": "claudexor::codex=exact-model", "model_role": "main"},
                    )
                    resumed.set()
                    keep_owner_open.wait(5)
        except BaseException as exc:  # surfaced after cleanup below
            failures.append(exc)

    thread = threading.Thread(target=run_wait, name="direct-project-model-wait")
    thread.start()
    try:
        assert owner_ready.wait(2)
        waiting_rows = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            availability = {"complete": True}
            direct = gateway_state._direct_turns_snapshot_safe(availability=availability)
            if direct and direct[0].get("model_waits"):
                waiting_rows = gateway_state._chat_activities_snapshot_safe(
                    tmp_path, direct_turns=direct, availability=availability,
                )
                break
            time.sleep(0.01)
        assert waiting_rows is not None
        assert availability["complete"] is True
        assert failures == []
        wait_activity = next(row for row in waiting_rows if row["activity_id"] == task["id"])
        assert wait_activity["project_id"] == "project-1"
        assert wait_activity["task_attempt"] == 2
        wait_row = list(wait_activity["model_waits"].values())[0]
        assert wait_row["state"] == "waiting"
        assert wait_row["reason"] == wait_reason
        assert wait_row["task_attempt"] == 2
        assert _js_project_summary(waiting_rows) == {
            "state": "waiting",
            "motion": False,
            "waiting": True,
            "label": "Waiting for access",
        }

        catalog.available = True
        assert resumed.wait(3), failures
        availability = {"complete": True}
        resumed_direct = gateway_state._direct_turns_snapshot_safe(availability=availability)
        resumed_rows = gateway_state._chat_activities_snapshot_safe(
            tmp_path, direct_turns=resumed_direct, availability=availability,
        )
        assert availability["complete"] is True
        resumed_activity = next(row for row in resumed_rows if row["activity_id"] == task["id"])
        assert resumed_activity["task_attempt"] == 2
        assert list(resumed_activity["model_waits"].values())[0]["state"] == "resolved"
        assert _js_project_summary(resumed_rows)["waiting"] is False
        assert _js_project_summary(resumed_rows)["motion"] is True
    finally:
        catalog.available = True
        keep_owner_open.set()
        thread.join(timeout=5)
        registry.unregister(task["id"])
        assert not thread.is_alive()
    assert failures == []
