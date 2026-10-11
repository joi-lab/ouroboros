"""A seal audit reads its selected history once, including cold and unknown paths."""

import json
import pathlib
import time
from types import SimpleNamespace

from ouroboros import model_send_seal as seals
from ouroboros import usage_compaction as compaction
from ouroboros.usage_ledger import UsageLedgerCorrupt
from tests import fixtures_usage_compaction as usage_fixtures
from tests import test_model_send_seal as seal_fixtures


data_root = seal_fixtures.data_root


def _manifest(root, attempt_id):
    """Only the manifest projection consumed by the reverse direction."""
    path = root / "observability" / "calls" / "batch" / (attempt_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "task_id": "batch", "call_id": attempt_id,
        "model_send_seal": {"attempt_id": attempt_id},
    }), encoding="utf-8")


def _archive_chain(root, count):
    """Real compaction creates a short valid ledger for every linked generation."""
    for _ in range(count):
        attempt = usage_fixtures._settle(root, cost=0.0, cost_final=True)
        assert usage_fixtures._compact(root) is not None
        _manifest(root, attempt.attempt_id)
    compaction._SEGMENT_CACHE.clear()
    compaction._CHAIN_UNION_CACHE.clear()


def test_live_only_sweep_never_loads_archive(data_root, monkeypatch):
    seal_fixtures._dispatch(data_root, "live")
    queries = []
    monkeypatch.setattr(compaction, "archived_attempt_ids", lambda root: queries.append(root))

    report = seals.reconcile_model_send_seals(data_root)

    assert report["seals"] == report["sealed_attempts"] == 1
    assert report["facts_written"] == 0
    assert queries == []


def test_large_seal_batch_reads_chain_once_even_after_cache_expiry(data_root, monkeypatch):
    # The incident had 415 generations. Small genuine segments exercise the
    # multiplier without copying gigabytes of private history into a fixture.
    count = 415
    _archive_chain(data_root, count)
    queries, loaded_segments, parsed_bytes = [], [], []
    read_archive = compaction.archived_attempt_ids
    load_segment = compaction._load_segment
    parse = compaction._parse_ledger_lines
    read_text = pathlib.Path.read_text
    clock = [time.time() + 100]
    monkeypatch.setattr(compaction, "time", SimpleNamespace(time=lambda: clock[0]))

    def query(root):
        queries.append(root)
        return read_archive(root)

    def load(root, header, dir_fd):
        loaded_segments.append(header["archive_rel"])
        return load_segment(root, header, dir_fd)

    def parse_bytes(payload):
        parsed_bytes.append(len(payload))
        return parse(payload)

    def read_manifest(path, *args, **kwargs):
        if path.parent.name == "batch":
            clock[0] += compaction._SEGMENT_CACHE_TTL_SEC + 1
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(compaction, "archived_attempt_ids", query)
    monkeypatch.setattr(compaction, "_load_segment", load)
    monkeypatch.setattr(compaction, "_parse_ledger_lines", parse_bytes)
    monkeypatch.setattr(pathlib.Path, "read_text", read_manifest)

    report = seals.reconcile_model_send_seals(data_root)

    assert report["seals"] == count and report["facts_written"] == 0
    assert len(queries) == 1
    assert len(loaded_segments) == len(set(loaded_segments)) == count
    segments = list((data_root / "archive" / "usage_ledger").glob("*.jsonl"))
    assert len(parsed_bytes) == count
    assert sum(parsed_bytes) == sum(path.stat().st_size for path in segments)


def test_each_sweep_rechecks_archive_and_does_not_retain_a_stale_answer(data_root, monkeypatch):
    _archive_chain(data_root, 2)
    queries, failures = [], []
    read_archive = compaction.archived_attempt_ids

    def query(root):
        queries.append(root)
        try:
            return read_archive(root)
        except UsageLedgerCorrupt as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(compaction, "archived_attempt_ids", query)
    assert seals.reconcile_model_send_seals(data_root)["facts_written"] == 0
    segment = next((data_root / "archive" / "usage_ledger").glob("*.jsonl"))
    segment.write_text("unreadable archive\n", encoding="utf-8")
    _manifest(data_root, "actually-absent")

    report = seals.reconcile_model_send_seals(data_root)

    assert len(queries) == 2 and len(failures) == 1
    assert report["seals"] == 3
    assert report["orphan_seals"] == report["facts_written"] == 0


def test_unknown_archive_is_attempted_once_and_forward_checks_continue(data_root, monkeypatch):
    _archive_chain(data_root, 3)
    live = seal_fixtures._dispatch(data_root, "live-missing-seal")
    pathlib.Path(live["candidate_manifest_ref"]["path"]).unlink()
    queries = []

    def unknown(root):
        queries.append(root)
        raise UsageLedgerCorrupt("compaction advanced during the archive read")

    monkeypatch.setattr(compaction, "archived_attempt_ids", unknown)
    report = seals.reconcile_model_send_seals(data_root)

    assert len(queries) == 1
    assert report["seals"] == 3 and report["orphan_seals"] == 0
    assert report["unlogged_attempts"] == report["facts_written"] == 1
    assert seal_fixtures._violation_events(data_root)[0]["kind"] == "unlogged_attempt"


def test_manifest_selection_precedes_live_snapshot(data_root, monkeypatch):
    seal_fixtures._dispatch(data_root, "selected")
    select = seals._seal_manifest_paths

    def new_attempt_after_selection(root, limit):
        selected = select(root, limit)
        seal_fixtures._dispatch(root, "arrived-after-selection")
        return selected

    monkeypatch.setattr(seals, "_seal_manifest_paths", new_attempt_after_selection)
    report = seals.reconcile_model_send_seals(data_root)

    # Reverse audit is pinned to the selected manifests, while the later live
    # snapshot also sees the new reservation and its forward sealing evidence.
    assert report["seals"] == 1 and report["sealed_attempts"] == 2
    assert report["facts_written"] == 0


def test_compaction_after_live_snapshot_keeps_selected_attempts_recorded(data_root, monkeypatch):
    _archive_chain(data_root, 1)
    seal_fixtures._dispatch(data_root, "about-to-fold")
    read_archive = compaction.archived_attempt_ids
    queries = []

    def compact_before_archive(root):
        queries.append(root)
        assert usage_fixtures._compact(root) is not None
        seal_fixtures._dispatch(root, "arrived-during-archive-read")
        return read_archive(root)

    monkeypatch.setattr(compaction, "archived_attempt_ids", compact_before_archive)
    report = seals.reconcile_model_send_seals(data_root)

    assert len(queries) == 1
    assert report["seals"] == 2 and report["facts_written"] == 0
