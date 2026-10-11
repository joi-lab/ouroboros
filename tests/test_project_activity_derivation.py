"""Project thread-activity derivation + legacy backfill (perf sprint P1).

``projects_summary`` derives ``has_thread_activity`` from registry facts alone
(origin / bindings / ``visible_revision`` / the write-once backfill flag) —
the per-request chat/progress log scan is retired. Legacy projects whose
activity predates the ``visible_revision`` counter are covered by a one-time
archive-aware backfill at boot reconcile, which persists ``thread_activity_seen``
through the registry's own write path. The GET path never scans and never writes.
"""
from __future__ import annotations

import json

import pytest

from ouroboros import projects_registry as pr
from ouroboros.contracts.chat_id_policy import project_chat_id


@pytest.fixture(autouse=True)
def _fresh_backfill_guard():
    pr._ACTIVITY_BACKFILL_DONE.clear()
    yield
    pr._ACTIVITY_BACKFILL_DONE.clear()


def _write_rows(path, chat_id, *, count=2, key="text"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(json.dumps({"chat_id": chat_id, key: f"row {index}"}) + "\n")


def _summary_row(data, pid):
    return next(row for row in pr.projects_summary(data) if row["id"] == pid)


def test_backfill_seeds_flag_for_legacy_project_with_old_chat_rows(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    entry = pr.create_project(data, "legacy", origin="reconcile")
    _write_rows(data / "logs" / "chat.jsonl", int(entry["chat_id"]))
    # Unrelated chat rows must not activate other projects.
    quiet = pr.create_project(data, "quiet", origin="reconcile")

    pr.reconcile_projects(data)

    assert pr.get_reserved_project(data, "legacy")["thread_activity_seen"] is True
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "quiet")
    assert _summary_row(data, "legacy")["has_thread_activity"] is True
    assert _summary_row(data, "quiet")["has_thread_activity"] is False
    assert int(quiet["chat_id"]) != int(entry["chat_id"])


def test_backfill_is_archive_aware(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    entry = pr.create_project(data, "rotated", origin="reconcile")
    # Activity lives ONLY in a rotated archive segment (live logs empty/absent).
    _write_rows(data / "archive" / "chat_20260101T000000.jsonl", int(entry["chat_id"]))

    pr.reconcile_projects(data)

    assert pr.get_reserved_project(data, "rotated")["thread_activity_seen"] is True
    assert _summary_row(data, "rotated")["has_thread_activity"] is True


def test_telemetry_only_new_project_reads_inactive_and_get_never_scans(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    # Boot reconcile runs first (empty root) — the one-time backfill is done.
    pr.reconcile_projects(data)

    # A NEW project whose thread carries ONLY telemetry rows afterwards: no
    # owner-visible canonical row, no binding, revision 0 => inactive (the
    # disclosed micro-delta of the derivation).
    entry = pr.create_project(data, "telem", origin="reconcile")
    _write_rows(data / "logs" / "progress.jsonl", int(entry["chat_id"]), key="content")

    def _no_scan(*_args, **_kwargs):
        raise AssertionError("projects_summary must never scan logs on the GET path")

    monkeypatch.setattr(pr, "iter_jsonl_objects", _no_scan)
    registry_before = (data / "state" / "projects.json").read_bytes()
    assert _summary_row(data, "telem")["has_thread_activity"] is False
    assert (data / "state" / "projects.json").read_bytes() == registry_before, (
        "GET path must never write the registry"
    )


def test_derivation_paths_owner_ui_binding_and_revision(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pr.reconcile_projects(data)  # backfill done; everything below is pure derivation

    pr.create_project(data, "fresh", name="Fresh", origin="owner_ui")
    assert _summary_row(data, "fresh")["has_thread_activity"] is True

    bound = pr.create_project(data, "bound", origin="reconcile")
    assert _summary_row(data, "bound")["has_thread_activity"] is False
    pr.bind_task_to_project(data, "t1", "bound", bound["chat_id"], origin={"absent": "system"})
    assert _summary_row(data, "bound")["has_thread_activity"] is True

    pr.create_project(data, "revised", origin="reconcile")
    assert _summary_row(data, "revised")["has_thread_activity"] is False
    pr.increment_project_visible_revision(data, project_id="revised")
    assert _summary_row(data, "revised")["has_thread_activity"] is True
    assert _summary_row(data, "revised")["visible_revision"] == 1


def test_backfill_runs_once_per_process_per_root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    pr.reconcile_projects(data)

    late = pr.create_project(data, "late", origin="reconcile")
    _write_rows(data / "logs" / "chat.jsonl", int(late["chat_id"]))
    # The periodic reconcile tick must NOT rescan the logs for this root.
    pr.reconcile_projects(data)
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "late")
    assert _summary_row(data, "late")["has_thread_activity"] is False

    # A fresh process (guard cleared) picks the legacy rows up at its boot.
    pr._ACTIVITY_BACKFILL_DONE.clear()
    pr.reconcile_projects(data)
    assert pr.get_reserved_project(data, "late")["thread_activity_seen"] is True


def test_transiently_failed_backfill_retries_on_next_reconcile(tmp_path, monkeypatch):
    """Review-wave fix pin: the once-per-process marker is set only AFTER a
    successful pass, so a transient failure retries on the next reconcile tick
    instead of waiting for a process restart."""
    data = tmp_path / "data"
    data.mkdir()
    legacy = pr.create_project(data, "legacy", origin="reconcile")
    _write_rows(data / "logs" / "chat.jsonl", int(legacy["chat_id"]))

    def _boom(_root):
        raise OSError("transient bindings read failure")

    monkeypatch.setattr(pr, "_load_bindings", _boom)
    pr.reconcile_projects(data)  # fails (log.warning), must NOT mark done
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "legacy")

    monkeypatch.undo()
    pr.reconcile_projects(data)  # next tick retries without a process restart
    assert pr.get_reserved_project(data, "legacy")["thread_activity_seen"] is True


def test_backfill_skips_projects_already_active_by_derivation(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    owner = pr.create_project(data, "mine", name="Mine", origin="owner_ui")
    revised = pr.create_project(data, "rev", origin="reconcile")
    pr.increment_project_visible_revision(data, project_id="rev")
    _write_rows(data / "logs" / "chat.jsonl", int(owner["chat_id"]))
    _write_rows(data / "logs" / "chat.jsonl", int(revised["chat_id"]))

    def _no_scan(*_args, **_kwargs):
        raise AssertionError("backfill must not scan when every candidate derives active")

    monkeypatch.setattr(pr, "iter_jsonl_objects", _no_scan)
    pr.reconcile_projects(data)
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "mine")
    assert "thread_activity_seen" not in pr.get_reserved_project(data, "rev")
    assert _summary_row(data, "mine")["has_thread_activity"] is True
    assert _summary_row(data, "rev")["has_thread_activity"] is True


def test_project_chat_id_helper_matches_registry_rows(tmp_path):
    # Guard the assumption the backfill scan rests on: registry rows carry the
    # deterministic chat id the log rows were written with.
    data = tmp_path / "data"
    data.mkdir()
    entry = pr.create_project(data, "det", origin="reconcile")
    assert int(entry["chat_id"]) == project_chat_id("det")
