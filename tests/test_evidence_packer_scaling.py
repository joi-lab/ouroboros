"""Task-local wakeups and operation counts on real evidence packs, never wall time."""
import contextlib
import json
from unittest.mock import patch

import pytest

from ouroboros import evidence_packer as packer, evidence_packs as packs, observability, store, usage_store
from supervisor import terminal_delivery
from tests.test_evidence_packs import REPO, _result, _task
from tests.test_evidence_packs import root as root


def _reserve(root, task):
    with usage_store.hold(root, write=True) as txn:
        txn.write({"attempt_id": f"attempt-{task}", "kind": "attempt", "state": "reserved",
                   "task_id": task, "root_task_id": task, "model": "offline", "provider": "local",
                   "reservation_upper_bound_usd": 0.01, "pricing_known": True})


def _settle_money(root, task):
    with usage_store.hold(root, write=True) as txn:
        previous = txn.attempt(f"attempt-{task}")
        txn.write({**previous, "state": "released", "reason": "not_dispatched"}, previous)


@pytest.mark.parametrize("change", ["money", "delivery", "row", "evidence"])
def test_unrelated_changes_do_not_wake_swept_task(root, monkeypatch, change):
    _task(root, "packed", blobs=1)
    assert packer.run_pass(root)["unlinked"] == 2
    packer.run_pass(root)  # record the directory revision after our own unlink
    before = packer.task_stamps(root, "packed")
    unit = packer.EvidencePacker()
    assert not unit.tick(root, REPO)
    if change == "money":
        _reserve(root, "unrelated")
    elif change == "delivery":
        assert terminal_delivery.register_pending_delivery(
            root, {"delivery_id": "owed-unrelated", "task_id": "unrelated", "chat_id": 1})
    elif change == "row":
        _result(root, "unrelated", "running")
    else:
        _task(root, "unrelated", status="running", blobs=1)
        packer.record_swept(root, "unrelated", packer.task_stamps(root, "unrelated"))
    monkeypatch.setattr(packer, "spawn_maintenance_job", lambda *a: pytest.fail("unrelated change spawned a packer"))
    assert packer.task_stamps(root, "packed") == before
    assert packer.changed_tasks(root) == []
    assert unit.tick(root, REPO)
    unit._thread.join(10)
    assert not unit._thread.is_alive()


@pytest.mark.parametrize("blocker", ["money", "delivery"])
def test_own_settlement_change_wakes_a_swept_task(root, blocker):
    _task(root, "later", blobs=1)
    if blocker == "money":
        _reserve(root, "later")
    else:
        assert terminal_delivery.register_pending_delivery(
            root, {"delivery_id": "owed-later", "task_id": "later", "chat_id": 1})
    assert packer.run_pass(root)["packed"] == 0
    assert packer.changed_tasks(root) == []
    if blocker == "money":
        _settle_money(root, "later")
    else:
        assert terminal_delivery.register_delivery(root, "owed-later")
    assert packer.changed_tasks(root) == ["later"]
    assert packer.run_pass(root)["unlinked"] == 2


@pytest.mark.parametrize("operation", ["changed_tasks", "run_pass", "tick"])
@pytest.mark.parametrize("status", ["running", "completed"])
def test_money_and_deliveries_read_once_for_all_tasks(root, monkeypatch, operation, status):
    for number in range(12):
        _task(root, f"live-{number}", status=status, blobs=1)
    _reserve(root, "unrelated")
    statements, registry_reads = [], []
    original_connect = usage_store._connect
    from ouroboros import utils
    original_read = utils.read_json_dict

    def connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    def read(path, *args, **kwargs):
        if path.name == "terminal_deliveries.json":
            registry_reads.append(path)
        return original_read(path, *args, **kwargs)

    assert terminal_delivery.register_pending_delivery(
        root, {"delivery_id": "owed-live", "task_id": "live-0", "chat_id": 1})
    unit = packer.EvidencePacker()
    unit.tick(root, REPO)
    if operation == "tick":
        packer.run_pass(root)
        packer.run_pass(root)
    monkeypatch.setattr(usage_store, "_connect", connect)
    monkeypatch.setattr(utils, "read_json_dict", read)
    if operation == "tick":
        assert unit.tick(root, REPO)
        unit._thread.join(10)
        assert not unit._thread.is_alive()
    else:
        getattr(packer, operation)(root)
    queries = [sql for sql in statements if "FROM attempts" in sql and sql.startswith("SELECT")]
    assert len(queries) == 1, queries
    assert len(registry_reads) == 1


def _measure(root, label):
    counts = {"case": label, "settlements": 0, "under_writer": 0, "verifies": 0}
    facts, verify = packs.task_settlement_facts, packs.verify_pack

    def observed_facts(*args, **kwargs):
        counts["settlements"] += 1
        counts["under_writer"] += int(kwargs.get("txn") is not None)
        return facts(*args, **kwargs)

    def observed_verify(*args, **kwargs):
        counts["verifies"] += 1
        return verify(*args, **kwargs)

    with patch.object(packs, "task_settlement_facts", observed_facts), patch.object(packs, "verify_pack", observed_verify):
        counts["report"] = packer.run_pass(root)
    print(json.dumps(counts, sort_keys=True), flush=True)
    assert counts["report"]["faults"] == 0
    return counts


@pytest.mark.parametrize("n", [12, 24, 48])
def test_measure_backfill_and_unrelated_money(root, n):
    usage_store.migrate_from_journal(root)
    for number in range(n):
        _task(root, f"settled-{number}", blobs=1)
    backfill = _measure(root, f"backfill-{n}")
    assert backfill["report"]["unlinked"] == 2 * n
    after_unlink = _measure(root, f"after-unlink-{n}")
    assert _measure(root, f"stable-{n}")["settlements"] == 0
    _reserve(root, "unrelated")
    print(json.dumps({"case": f"changed-after-money-{n}", "candidates": len(packer.changed_tasks(root))}), flush=True)
    unrelated = _measure(root, f"unrelated-money-{n}")
    assert backfill["settlements"] == 2 * n  # one census plus one candidate recheck
    assert backfill["under_writer"] == n
    assert backfill["verifies"] == n
    assert after_unlink["verifies"] == 0  # no loose members left to delete
    assert unrelated["settlements"] == unrelated["verifies"] == 0


def test_no_global_scan_or_foreign_settlement_inside_unlink_locks(root, monkeypatch):
    for number in range(12):
        _task(root, f"settled-{number}", blobs=1)
    active = {"writer": False, "publication": False, "task": None}
    scans = []
    open_store, name_lock = store.open_store, store.name_lock
    loose_ids, facts, unlink = packer._loose_task_ids, packs.task_settlement_facts, packs.unlink_loose

    @contextlib.contextmanager
    def opened(*args, **kwargs):
        with open_store(*args, **kwargs) as txn:
            before = active["writer"]
            active["writer"] |= bool(kwargs.get("write"))
            try:
                yield txn
            finally:
                active["writer"] = before

    @contextlib.contextmanager
    def locked(root, name, **kwargs):
        with name_lock(root, name, **kwargs):
            before = active["publication"]
            active["publication"] |= name == packs.PUBLICATION_LOCK_NAME
            try:
                yield
            finally:
                active["publication"] = before

    def ids(*args):
        scans.append((active["writer"], active["publication"]))
        return loose_ids(*args)

    def observed_facts(root, task, **kwargs):
        if active["writer"] or active["publication"]:
            assert task == active["task"], "foreign task evaluated during candidate unlink"
        return facts(root, task, **kwargs)

    def remove(root, task, *args, **kwargs):
        active["task"] = task
        try:
            return unlink(root, task, *args, **kwargs)
        finally:
            active["task"] = None

    monkeypatch.setattr(store, "open_store", opened)
    monkeypatch.setattr(store, "name_lock", locked)
    monkeypatch.setattr(packer, "_loose_task_ids", ids)
    monkeypatch.setattr(packs, "task_settlement_facts", observed_facts)
    monkeypatch.setattr(packs, "unlink_loose", remove)
    report = packer.run_pass(root)
    assert report["faults"] == 0 and report["unlinked"] == 24
    assert scans and not any(writer or publication for writer, publication in scans)


@pytest.mark.parametrize("rewrite", [False, True])
def test_manifest_published_after_keep_snapshot_protects_blob(root, monkeypatch, rewrite):
    refs, _ = _task(root, "packing", blobs=1)
    _task(root, "live", status="running", blobs=1)
    if rewrite:
        observability.write_call_manifest(root, task_id="live", call_id="late", manifest={})
    write = packs.write_pack
    manifests = []
    read = packs.manifest_blob_refs

    def read_refs(root, task):
        manifests.append(task)
        return read(root, task)

    def publish(*args, **kwargs):
        result = write(*args, **kwargs)
        assert "live" in manifests, "no keep-set snapshot was taken before compression"
        observability.write_call_manifest(root, task_id="live", call_id="late", manifest={"ref": refs[0]})
        return result

    with monkeypatch.context() as edits:
        edits.setattr(packs, "manifest_blob_refs", read_refs)
        edits.setattr(packs, "write_pack", publish)
        report = packer.run_pass(root)
    assert report["faults"] == 0 and report["kept"] == report["unlinked"] == 1
    assert len([task for task in manifests if task == "live"]) == 1
    assert packs.blob_bytes(root, refs[0])
    _result(root, "live")
    assert packer.run_pass(root)["faults"] == 0
    from pathlib import Path
    assert not Path(refs[0]["path"]).exists()


@pytest.mark.parametrize("change", ["money", "delivery", "obligation"])
def test_own_settlement_change_defers_the_candidate_without_scanning_under_locks(root, monkeypatch, change):
    from ouroboros import obligations
    from pathlib import Path

    refs, _ = _task(root, "packing", blobs=1)
    write = packs.write_pack

    def publish(*args, **kwargs):
        result = write(*args, **kwargs)
        if change == "money":
            _reserve(root, "packing")
        elif change == "delivery":
            terminal_delivery.register_pending_delivery(
                root, {"delivery_id": "owed-packing", "task_id": "packing", "chat_id": 1})
        else:
            obligations.add(root, "custom_duty", "op", {"task_id": "packing"})
        return result

    with monkeypatch.context() as edits:
        edits.setattr(packs, "write_pack", publish)
        report = packer.run_pass(root)
    assert report["faults"] == report["unlinked"] == 0
    assert Path(refs[0]["path"]).exists()
    if change == "money":
        _settle_money(root, "packing")
    elif change == "delivery":
        terminal_delivery.register_delivery(root, "owed-packing")
    elif change == "obligation":
        obligations.remove(root, "custom_duty", "op")
    assert packer.run_pass(root)["unlinked"] == 2


def test_unrelated_activity_during_a_pass_does_not_stop_the_backfill(root, monkeypatch):
    # A busy install commits rows and money between candidates; only the census's own tasks matter.
    from pathlib import Path

    refs = [_task(root, f"settled-{number}", blobs=1)[0][0] for number in range(6)]
    write, calls, evaluated = packs.write_pack, [], []
    facts = packs.task_settlement_facts

    def publish(*args, **kwargs):
        result = write(*args, **kwargs)
        calls.append(1)
        _result(root, "busy", "running", step=len(calls))
        _reserve(root, f"busy-{len(calls)}")
        return result

    def observed(root, task, **kwargs):
        evaluated.append(task)
        return facts(root, task, **kwargs)

    with monkeypatch.context() as edits:
        edits.setattr(packs, "write_pack", publish)
        edits.setattr(packs, "task_settlement_facts", observed)
        report = packer.run_pass(root)
    assert report["faults"] == 0 and report["packed"] == 6 and report["unlinked"] == 12
    assert not any(Path(ref["path"]).exists() for ref in refs)
    assert len(evaluated) <= 3 * 6  # census, keep-set and each candidate's own check: still linear


def test_body_change_while_candidates_are_chosen_keeps_the_shared_blob(root, monkeypatch):
    # The census baseline is older than every fact the pass reads: a body-only change made after a
    # candidate's facts were read (an open review-operation pointer on a completed task) is re-read.
    from pathlib import Path

    from ouroboros import task_store

    raw = b'{"shared": "window"}'
    refs, _ = _task(root, "packing", blobs=1, payloads=[raw])
    _task(root, "sharer", blobs=1, payloads=[raw], handle=b"small")
    original = packer.ordered_candidates

    def ordered(root, settled):
        task_store.patch_document(root, "sharer", lambda d: {**d, "review_operations": {
            "ctrl": {"surface": "task_acceptance", "state": "preparing"}}})
        return original(root, settled)

    with monkeypatch.context() as edits:
        edits.setattr(packer, "ordered_candidates", ordered)
        report = packer.run_pass(root)
    assert report["faults"] == 0 and report["kept"] == 1
    assert Path(refs[0]["path"]).exists()


def test_task_resumed_after_census_keeps_its_existing_shared_reference(root, monkeypatch):
    from pathlib import Path

    raw = b'{"shared": "retained"}'
    refs, _ = _task(root, "packing", blobs=1, payloads=[raw])
    _task(root, "other", blobs=1, payloads=[raw], handle=b"small")
    write = packs.write_pack

    def resume(*args, **kwargs):
        result = write(*args, **kwargs)
        _result(root, "other", "running")
        return result

    with monkeypatch.context() as edits:
        edits.setattr(packs, "write_pack", resume)
        report = packer.run_pass(root)
    # The same pass sees the resumed sharer: the shared blob stays, the rest of the pass goes on.
    assert report["faults"] == 0 and report["kept"] == 1 and report["unlinked"] == 1
    assert Path(refs[0]["path"]).exists()
    _result(root, "other")
    assert packer.run_pass(root)["faults"] == 0
    assert not Path(refs[0]["path"]).exists()


def test_publication_journal_exists_only_for_a_sweep_and_discards_crash_residue(root):
    refs, _ = _task(root, "packing", blobs=1)
    journal = packs.packs_dir(root) / packs.PUBLICATIONS_NAME
    observability.write_call_manifest(root, task_id="packing", call_id="extra", manifest={"ref": refs[0]})
    assert not journal.exists()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"incomplete old pass")
    assert packer.run_pass(root)["unlinked"] == 2
    assert not journal.exists()
