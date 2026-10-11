"""The real worker reports entry before the potentially slow extension load."""

import json
import os

import pytest

from tests.test_terminal_file_boundary import worker as worker


@pytest.mark.parametrize("progress_write_fails", [False, True])
def test_entry_progress_precedes_extensions_and_logging_failure_does_not_block_ready(
    worker, monkeypatch, progress_write_fails,
):
    import ouroboros.extension_loader as extensions
    import ouroboros.utils as utils

    worker.task["type"] = "shutdown"
    events = worker.root / "logs" / "events.jsonl"
    real_append = utils.append_jsonl
    attempts, loads = [], []

    def append(path, row, **kwargs):
        if row.get("type") == "worker_starting":
            attempts.append(row)
            if progress_write_fails:
                raise OSError("entry log unavailable")
        return real_append(path, row, **kwargs)

    def load(*_args, **_kwargs):
        loads.append(True)
        assert len(attempts) == 1 and attempts[0]["pid"] == os.getpid()
        assert attempts[0]["worker_id"] == 0 and attempts[0]["phase"] == "entry"
        rows = events.read_text(encoding="utf-8") if events.exists() else ""
        assert '"worker_ready"' not in rows
        assert ('"worker_starting"' in rows) is not progress_write_fails

    monkeypatch.setattr(utils, "append_jsonl", append)
    monkeypatch.setattr(extensions, "reload_all", load)
    worker.run()

    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    assert loads == [True] and worker.crashes == [] and worker.calls == []
    assert [row["type"] for row in rows] == (
        ["worker_ready"] if progress_write_fails else ["worker_starting", "worker_ready"]
    )
    assert rows[-1]["pid"] == os.getpid() and rows[-1]["git_sha"] == "test-sha"
