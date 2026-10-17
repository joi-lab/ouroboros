"""The historical pointer reaches rotated bytes from the same forked reader."""
import json

import pytest

from tests.test_authored_memory import _actor, _context_world, _mode_texts, _run


@pytest.mark.parametrize("forked", [False, True])
@pytest.mark.parametrize("crowded", [False, True])
def test_historical_reflection_archive_address_opens_the_counted_rows(tmp_path, monkeypatch, forked, crowded):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    env, memory = _context_world(tmp_path)
    live = env.drive_root / "logs/task_reflections.jsonl"
    rows = [json.loads(line) for line in live.read_text().splitlines()]
    archive = env.drive_root / "archive"
    archive.mkdir(exist_ok=True)
    if crowded:
        for index in range(870):
            (archive / f"events_{index:04d}.jsonl").write_text("{}\n", encoding="utf-8")
    segment = archive / "task_reflections_20261012T120000.jsonl"
    segment.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    live.write_text("".join(json.dumps(row) + "\n" for row in rows[1:]), encoding="utf-8")
    drive = tmp_path / "fork" if forked else env.drive_root
    drive.mkdir(exist_ok=True)
    actor = _actor(env, "archive-reader", drive_root=drive, budget_drive_root=str(env.drive_root))
    task = {"id": "archive-reader", "type": "task", "text": "Read history."}
    for text in _mode_texts(env, memory, task, actor).values():
        assert "Historical automatic reflections: 1 row(s)" in text
        assert "OLD AUTOMATIC LESSON BODY" not in text
        [call] = [line.removeprefix("Archived reflection source: ") for line in text.splitlines()
                  if line.startswith("Archived reflection source: ")]
        assert segment.name in call
        assert "OLD AUTOMATIC LESSON BODY" in _run(actor, call)
    for text in _mode_texts(env, memory, task).values():
        [address] = [line for line in text.splitlines() if line.startswith("Archived reflection source: ")]
        assert str(segment) in address and "no tool context here" in address
        assert "read_file(" not in address and "list_files(" not in address
