"""A directory output's complete skip list is a durable row in the task's ``events.jsonl``
(``directory_output_members_skipped``); the rendered note stays bounded and names that row. A
context without a log root falls back to the process log rather than losing the list."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from ouroboros.tools import shell_outputs


def test_the_complete_skip_list_is_one_durable_task_event(tmp_path, caplog):
    logs = tmp_path / "logs"
    ctx = SimpleNamespace(task_id="t-1", drive_logs=lambda: logs)
    members = [f"site/.env.{index}: dotenv secret" for index in range(7)]

    with caplog.at_level(logging.INFO, logger=shell_outputs.log.name):
        shell_outputs._record_skipped_members(ctx, "site", members)

    rows = [json.loads(line) for line in (logs / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["type"] == "directory_output_members_skipped"
    assert rows[0]["task_id"] == "t-1" and rows[0]["output"] == "site" and rows[0]["count"] == 7
    assert rows[0]["members"] == members and rows[0]["ts"]
    assert not [r for r in caplog.records if "skip list" in r.getMessage()]

    def no_root():
        raise RuntimeError("no drive")

    with caplog.at_level(logging.INFO, logger=shell_outputs.log.name):
        shell_outputs._record_skipped_members(SimpleNamespace(task_id="t-2", drive_logs=no_root), "site", members)
    assert [r for r in caplog.records if "full export skip list (7)" in r.getMessage()]
