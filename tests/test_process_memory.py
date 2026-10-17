"""Tests for process memory infrastructure.

Covers:
- A preserved commit's failed post-commit tests stay a typed trace fact
- Reflection-log loading into context (host rows whole, older automatic rows as a pointer)
- Post-task work stays off the reply critical path
- Durable reflection-log routing (project store + canonical pointer)
"""

import inspect
import os
import sys


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ─────────────────────────────────────────────────────────────────────────────
# A preserved commit's failed post-commit tests: a typed fact in the trace
# ─────────────────────────────────────────────────────────────────────────────

class TestPostCommitTestFact:
    """The producer-to-trace half of the retired reflection trigger test survives on its own."""

    def test_a_preserved_commit_with_failing_post_commit_tests_keeps_a_typed_fact(self):
        """The ordinary self-modification commit reports failing post-commit tests
        as a WARNING appended to a success: the commit is preserved and the call is
        ok on every axis, while the producer states the failure as a typed fact the
        trace keeps; a clean commit gains no such field. Driven through the real
        producer seam (tools/git.py) and trace projection, never a Git operation.
        """
        import types

        from ouroboros.loop_tool_execution import _typed_execution_failure, _typed_result_metadata
        from ouroboros.reflection import _trace_call_errored
        from ouroboros.tools import git as git_tools
        from ouroboros.tools.tool_result import (
            _install_tool_result_sidecar, _published_tool_result, _restore_tool_result_sidecar,
        )

        def _row(test_warning: str) -> dict:
            ctx, sentinel = types.SimpleNamespace(), object()
            token = _install_tool_result_sidecar(ctx, sentinel)
            expected = "OK: committed to dev: fix parser[pushed: abc1234]" + test_warning
            try:
                text = git_tools._publish_post_commit_test_fact(ctx, expected, test_warning)
                published = _published_tool_result(ctx, sentinel)
            finally:
                _restore_tool_result_sidecar(token)
            assert text == expected  # the publication rule: the text stays byte-identical
            is_error = _typed_execution_failure(True, published) if published else False
            return {"tool": "commit_reviewed", "result": text, "is_error": is_error,
                    **_typed_result_metadata("commit_reviewed", text, is_error, published)}

        failed = _row("\n\n⚠️ TESTS_FAILED (commit preserved): post-commit verification failed.")
        assert failed["is_error"] is False and failed["status"] == "ok"
        assert _trace_call_errored(failed) is False
        assert failed["post_commit_tests"] == "failed"
        assert "post_commit_tests" not in _row("")


# ─────────────────────────────────────────────────────────────────────────────
# Reflection context loading
# ─────────────────────────────────────────────────────────────────────────────

class TestReflectionContextLoading:
    """build_recent_sections must load execution reflections from JSONL."""

    def test_reflections_loaded_when_file_exists(self):
        from ouroboros.context import build_recent_sections
        source = inspect.getsource(build_recent_sections)
        assert "task_reflections.jsonl" in source, (
            "build_recent_sections must load from task_reflections.jsonl"
        )
        assert "Execution reflections" in source, (
            "Section header must contain 'Execution reflections'"
        )


# ─────────────────────────────────────────────────────────────────────────────
# emit_task_results: reflection stays OFF the reply critical path
# ─────────────────────────────────────────────────────────────────────────────

class TestEmitTaskResultsReflectionNotOnCriticalPath:
    """Regression guard for the v4.39.0 UX regression: paid post-task work must
    not run on the reply critical path. The synchronous variant added a 1–3 s
    LLM round before `send_message` was dispatched; the async daemon-thread
    variant keeps reply latency low.
    """

    def _make_minimal_env(self, tmp_path):
        import pathlib

        class FakeEnv:
            drive_root = tmp_path
            repo_dir = str(tmp_path)

            def drive_path(self, sub):
                return pathlib.Path(self.drive_root) / sub

        return FakeEnv()

    def test_post_task_work_is_not_called_synchronously_in_emit_task_results(self, tmp_path):
        """emit_task_results must NOT run a paid post-task stage inline — it
        belongs inside the daemon thread started by
        `_run_post_task_processing_async`."""
        import unittest.mock as mock
        from ouroboros.agent_task_pipeline import emit_task_results

        env = self._make_minimal_env(tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        task = {"id": "t1", "type": "task", "text": "goal", "chat_id": 1}
        usage = {"rounds": 5, "cost": 1.0, "prompt_tokens": 0, "completion_tokens": 0}
        llm_trace = {"tool_calls": []}

        with mock.patch("ouroboros.agent_task_pipeline._run_memory_fallback_draft") as mock_draft, \
             mock.patch("ouroboros.agent_task_pipeline._run_post_task_processing_async") as mock_async, \
             mock.patch("ouroboros.agent_task_pipeline._run_scratchpad_consolidation") as mock_scratch, \
             mock.patch("ouroboros.agent_task_pipeline._store_task_result"), \
             mock.patch("ouroboros.review_evidence.collect_review_evidence",
                        return_value={}, create=True):
            pending_events: list = []
            emit_task_results(
                env=env, memory=mock.MagicMock(), llm=mock.MagicMock(),
                pending_events=pending_events,
                task=task, text="reply",
                usage=usage, llm_trace=llm_trace,
                start_time=0.0,
                drive_logs=tmp_path / "logs",
            )

        # Paid stages are the responsibility of the async helper —
        # emit_task_results must not call them directly. Otherwise reply
        # latency regresses.
        mock_draft.assert_not_called()
        mock_scratch.assert_not_called()
        # The async helper must still be invoked exactly once.
        mock_async.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# F5: durable reflection routing (project drive + canonical pointer)
# ─────────────────────────────────────────────────────────────────────────────

def test_append_reflection_routed_project_task_writes_project_drive_and_pointer(tmp_path, monkeypatch):
    """A project-scoped root's FULL reflection lands on the project drive; the
    canonical log gets a bounded pointer row (never the full text — it feeds
    future global context); the prunable mirror drive gets nothing."""
    import json
    import types

    import ouroboros.project_facts as pf
    from ouroboros.reflection import append_reflection_routed

    monkeypatch.setattr(pf, "_project_store_root", lambda pid: tmp_path / "projects" / pid)
    canonical = tmp_path / "data"
    mirror = tmp_path / "mirror"  # headless mirror drive — prunable, never the home
    env = types.SimpleNamespace(drive_root=mirror)
    task = {"id": "t-proj", "project_id": "slime", "budget_drive_root": str(canonical)}
    entry = {
        "ts": "2026-08-10T00:00:00Z", "task_id": "t-proj",
        "reflection": "full project-local reflection text",
    }

    append_reflection_routed(env, task, entry)

    project_log = tmp_path / "projects" / "slime" / "logs" / "task_reflections.jsonl"
    rows = [json.loads(line) for line in project_log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reflection"] == "full project-local reflection text"

    canonical_log = canonical / "logs" / "task_reflections.jsonl"
    pointer_rows = [json.loads(line) for line in canonical_log.read_text(encoding="utf-8").splitlines()]
    assert pointer_rows[0]["type"] == "project_reflection_pointer"
    assert pointer_rows[0]["task_id"] == "t-proj"
    assert pointer_rows[0]["project_id"] == "slime"
    assert pointer_rows[0]["reflection_path"] == str(project_log)
    assert "write_failed" not in pointer_rows[0]  # successful project append
    assert "full project-local reflection text" not in canonical_log.read_text(encoding="utf-8")
    assert not (mirror / "logs" / "task_reflections.jsonl").exists()


def test_append_reflection_routed_stamps_pointer_when_project_write_fails(tmp_path, monkeypatch):
    """Finding 4: a failed project-drive append must not leave a pointer that
    claims a full text exists — the pointer row is stamped write_failed."""
    import json
    import types

    import ouroboros.project_facts as pf
    import ouroboros.reflection as refl

    monkeypatch.setattr(pf, "_project_store_root", lambda pid: tmp_path / "projects" / pid)
    canonical = tmp_path / "data"
    env = types.SimpleNamespace(drive_root=tmp_path / "mirror")
    task = {"id": "t-proj", "project_id": "slime", "budget_drive_root": str(canonical)}
    entry = {"ts": "2026-08-10T00:00:00Z", "task_id": "t-proj",
             "reflection": "full project-local reflection text"}

    real_append = refl.append_jsonl

    def _selective(path, row):
        if str(tmp_path / "projects") in str(path):
            raise OSError("disk full")
        return real_append(path, row)

    monkeypatch.setattr(refl, "append_jsonl", _selective)

    refl.append_reflection_routed(env, task, entry)

    canonical_log = canonical / "logs" / "task_reflections.jsonl"
    pointer = json.loads(canonical_log.read_text(encoding="utf-8").splitlines()[0])
    assert pointer["type"] == "project_reflection_pointer"
    assert pointer["write_failed"] is True
    assert "full project-local reflection text" not in canonical_log.read_text(encoding="utf-8")


def test_recent_reflections_renders_pointer_row_as_single_line():
    """Finding 3: a canonical pointer row (no reflection text) renders one
    informative line instead of an empty block burning a context slot."""
    from ouroboros.context import _format_recent_reflections

    text = _format_recent_reflections([{
        "ts": "2026-08-10T00:00:00Z",
        "task_id": "t-proj",
        "type": "project_reflection_pointer",
        "project_id": "slime",
        "reflection_path": "/proj/slime/logs/task_reflections.jsonl",
    }])

    assert ("Full reflection lives on project drive: slime — "
            "/proj/slime/logs/task_reflections.jsonl") in text
    assert len(text.strip().splitlines()) == 2  # header + one pointer line


def test_append_reflection_routed_non_project_task_uses_canonical_drive(tmp_path):
    """A non-project root reflects on the canonical budget drive in full — never
    on the prunable mirror the split root executes on."""
    import json
    import types

    from ouroboros.reflection import append_reflection_routed

    canonical = tmp_path / "data"
    mirror = tmp_path / "mirror"
    env = types.SimpleNamespace(drive_root=mirror)
    task = {"id": "t-plain", "budget_drive_root": str(canonical)}
    entry = {"ts": "2026-08-10T00:00:00Z", "task_id": "t-plain", "reflection": "plain reflection"}

    append_reflection_routed(env, task, entry)

    canonical_log = canonical / "logs" / "task_reflections.jsonl"
    rows = [json.loads(line) for line in canonical_log.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["reflection"] == "plain reflection"
    assert not (mirror / "logs" / "task_reflections.jsonl").exists()
