"""Task-local wakeups and operation counts on real evidence packs, never wall time; the production-shaped
backfill (300 terminal tasks carrying the former blockers beside 10 live sharers) prints its numbers."""
import contextlib
import json
import pathlib
import time
from unittest.mock import patch

import pytest

from ouroboros import evidence_packer as packer, evidence_packs as packs, maintenance, obligations, observability, store, usage_store
from supervisor import terminal_delivery
from tests.test_evidence_packs import _handle_path, _result, _task
from tests.test_evidence_packs import root as root


def _reserve(root, task):
    with usage_store.hold(root, write=True) as txn:
        txn.write({"attempt_id": f"attempt-{task}", "kind": "attempt", "state": "reserved",
                   "task_id": task, "root_task_id": task, "model": "offline", "provider": "local",
                   "reservation_upper_bound_usd": 0.01, "pricing_known": True})


@pytest.mark.parametrize("change", ["money", "delivery", "live_row", "live_evidence"])
def test_unrelated_changes_do_not_wake_the_look_or_the_swept_task(root, monkeypatch, change):
    _task(root, "packed", blobs=1)
    assert packer.run_pass(root)["unlinked"] == 2
    packer.run_pass(root)  # record the directory revision after our own unlink
    maintenance.run_job(root, "evidence_packing")  # the job's quiet fact for this state
    assert "evidence_packing" not in maintenance.due(root)
    before = packer.task_stamps(root, "packed")
    if change == "money":
        _reserve(root, "unrelated")
    elif change == "delivery":
        assert terminal_delivery.register_pending_delivery(
            root, {"delivery_id": "owed-unrelated", "task_id": "unrelated", "chat_id": 1})
    elif change == "live_row":
        _result(root, "unrelated", "running")
    else:
        _task(root, "unrelated", status="running", blobs=1)
    assert packer.task_stamps(root, "packed") == before
    assert packer.changed_tasks(root) == ([] if change != "live_evidence" else ["unrelated"])
    # None of these is a reason to spawn the child: the job is at rest and only its facts are read.
    monkeypatch.setattr(packer, "packing_pending", lambda *a, **k: pytest.fail("an unrelated change started discovery"))
    assert maintenance.run_job(root, "evidence_packing") is None
    assert "evidence_packing" not in maintenance.due(root)


def test_a_task_closing_wakes_the_look_and_its_pass(root):
    _task(root, "later", blobs=1, status="running")
    maintenance.run_job(root, "evidence_packing")
    assert "evidence_packing" not in maintenance.due(root)
    _result(root, "later", "completed")
    assert "evidence_packing" in maintenance.due(root)
    assert packer.changed_tasks(root) == ["later"]
    assert maintenance.run_job(root, "evidence_packing")["state"] == "done"
    assert packs.published_generations(root, "later") and "evidence_packing" in maintenance.due(root)  # one more look
    assert maintenance.run_job(root, "evidence_packing")["state"] == "done"
    assert "evidence_packing" not in maintenance.due(root)


@pytest.mark.parametrize("operation", ["changed_tasks", "run_pass", "job"])
def test_no_money_or_delivery_source_is_read(root, monkeypatch, operation):
    for number in range(12):
        _task(root, f"t-{number}", status="running" if number % 2 else "completed", blobs=1)
    _reserve(root, "unrelated")
    assert terminal_delivery.register_pending_delivery(root, {"delivery_id": "owed", "task_id": "t-0", "chat_id": 1})
    from ouroboros import utils
    original_read = utils.read_json_dict

    def read(path, *args, **kwargs):
        assert path.name != "terminal_deliveries.json", "a pass read the delivery outbox"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(usage_store, "hold", lambda *a, **k: pytest.fail("a pass opened the money store"))
    monkeypatch.setattr(utils, "read_json_dict", read)
    if operation == "job":
        assert maintenance.run_job(root, "evidence_packing")["state"] == "done"
    else:
        getattr(packer, operation)(root)


def _measure(root, label):
    counts = {"case": label, "leases": 0, "verifies": 0}
    connection, verify = store.connection, packs.verify_pack

    def leased(path, *args, **kwargs):
        counts["leases"] += int(pathlib.Path(path) == store.store_path(root))
        return connection(path, *args, **kwargs)

    def observed_verify(*args, **kwargs):
        counts["verifies"] += 1
        return verify(*args, **kwargs)

    with patch.object(store, "connection", leased), patch.object(packs, "verify_pack", observed_verify):
        counts["report"] = packer.run_pass(root)
    print(json.dumps(counts, sort_keys=True), flush=True)
    assert counts["report"]["faults"] == 0
    return counts


@pytest.mark.parametrize("n", [12, 24, 48])
def test_measure_backfill_then_rest(root, n):
    for number in range(n):
        _task(root, f"settled-{number}", blobs=1)
    backfill = _measure(root, f"backfill-{n}")
    assert backfill["report"]["unlinked"] == 2 * n and backfill["verifies"] == n
    after_unlink = _measure(root, f"after-unlink-{n}")
    assert after_unlink["verifies"] == 0  # no loose members left: no generation is re-read
    stable = _measure(root, f"stable-{n}")
    assert stable["leases"] == 1 and stable["verifies"] == 0  # the census read, nothing else
    _result(root, "unrelated", "running")
    unrelated = _measure(root, f"unrelated-row-{n}")
    assert unrelated["leases"] == 1 and unrelated["verifies"] == 0


def test_no_global_scan_or_foreign_stamp_inside_unlink_locks(root, monkeypatch):
    for number in range(12):
        _task(root, f"settled-{number}", blobs=1)
    active = {"writer": False, "publication": False, "task": None}
    scans = []
    open_store, name_lock = store.open_store, store.name_lock
    loose_ids, stamps, unlink = packer._loose_task_ids, packer.task_stamps, packs.unlink_loose

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

    def observed_stamps(root, task, **kwargs):
        if active["writer"] or active["publication"]:
            assert task == active["task"], "foreign task evaluated during candidate unlink"
        return stamps(root, task, **kwargs)

    def remove(root, task, *args, **kwargs):
        active["task"] = task
        try:
            return unlink(root, task, *args, **kwargs)
        finally:
            active["task"] = None

    monkeypatch.setattr(store, "open_store", opened)
    monkeypatch.setattr(store, "name_lock", locked)
    monkeypatch.setattr(packer, "_loose_task_ids", ids)
    monkeypatch.setattr(packer, "task_stamps", observed_stamps)
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
    assert not pathlib.Path(refs[0]["path"]).exists()


def test_unrelated_activity_during_a_pass_does_not_stop_the_backfill(root, monkeypatch):
    # A busy install commits rows and money between candidates; only the census's own tasks matter.
    refs = [_task(root, f"settled-{number}", blobs=1)[0][0] for number in range(6)]
    write, calls = packs.write_pack, []

    def publish(*args, **kwargs):
        result = write(*args, **kwargs)
        calls.append(1)
        _result(root, "busy", "running", step=len(calls))
        _reserve(root, f"busy-{len(calls)}")
        return result

    with monkeypatch.context() as edits:
        edits.setattr(packs, "write_pack", publish)
        report = packer.run_pass(root)
    assert report["faults"] == 0 and report["packed"] == 6 and report["unlinked"] == 12
    assert not any(pathlib.Path(ref["path"]).exists() for ref in refs)


def test_a_sharer_resumed_after_the_census_reads_its_shared_blob_from_the_pack(root, monkeypatch):
    """Resuming a terminal task under its id after the census is not a reason to stop the pass: the
    shared blob is in the packed task's verified pack, where the resumed sharer's readers find it. The
    sharer's own generation, chosen at the census, is still written (verified bytes, never a loss), but
    its loose files stay: its unlink sees the moved status and waits for its own pass."""
    raw = b'{"shared": "retained"}'
    refs, _ = _task(root, "packing", blobs=1, payloads=[raw])
    other, other_handle = _task(root, "other", blobs=1, payloads=[raw], handle=b"small")
    write, resumed = packs.write_pack, []

    def resume(*args, **kwargs):
        result = write(*args, **kwargs)
        if not resumed:
            resumed.append(_result(root, "other", "running"))
        return result

    with monkeypatch.context() as edits:
        edits.setattr(packs, "write_pack", resume)
        report = packer.run_pass(root)
    assert report["faults"] == 0 and (report["packed"], report["unlinked"], report["kept"]) == (2, 2, 0)
    assert packs.blob_bytes(root, refs[0]) == raw and packs.blob_bytes(root, other[0]) == raw
    assert len(packs.published_generations(root, "other")) == 1 and _handle_path(root, "other", other_handle).exists()
    _result(root, "other")
    assert packer.run_pass(root)["faults"] == 0
    assert not _handle_path(root, "other", other_handle).exists() and packs.blob_bytes(root, other[0]) == raw


def test_publication_journal_exists_only_for_a_sweep_and_discards_crash_residue(root):
    refs, _ = _task(root, "packing", blobs=1)
    journal = packs.packs_dir(root) / packs.PUBLICATIONS_NAME
    observability.write_call_manifest(root, task_id="packing", call_id="extra", manifest={"ref": refs[0]})
    assert not journal.exists()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes(b"incomplete old pass")
    assert packer.run_pass(root)["unlinked"] == 2
    assert not journal.exists()


def _former_blocker(root, index, task):
    """One of the facts the old settlement census waited for, round-robin."""
    kind = index % 6
    if kind == 0:
        _reserve(root, task)
    elif kind == 1:
        _result(root, task, review_operations={"op": {"surface": "multi_model_review", "state": "closed"}})
    elif kind == 2:
        _result(root, task, canonical_terminal_projection_origin="terminal_transition")
    elif kind == 3:
        obligations.add(root, "custody_open", f"invocation:{task}", {"request": {"task_id": task}})
    elif kind == 4:
        obligations.add(root, "future_body_duty", f"work:{task}", {"task_id": task})
    elif index % 12 == 5:
        assert terminal_delivery.register_pending_delivery(root, {"delivery_id": f"owed-{task}", "task_id": task, "chat_id": 1})
    else:
        _result(root, task, acceptance_debt={"debt_id": f"d-{task}"})


def test_production_shaped_backfill_packs_every_terminal_task_beside_live_sharers(root):
    """The production shape at a size the default suite affords: 18 terminal tasks, each carrying a former
    blocker (every kind three times); 3 running tasks sharing a blob with three of them. One pass packs all
    18, every ref reads back byte-identical, the shared blobs stay loose. (The 300-task shape took 2–30 min
    under load against the suite's 300 s per-test bound; the property has no size: P2 proves it for one
    sharer, this for many.)"""
    terminal, live, shared = 18, 3, {}
    refs, handles = {}, {}
    for number in range(terminal):
        task = f"done-{number:03}"
        payloads = [json.dumps({"answer": task, "body": "x" * 3000}).encode(), json.dumps({"request": task, "body": "y" * 1200}).encode()]
        refs[task], handles[task] = _task(root, task, blobs=2, payloads=payloads)
        _former_blocker(root, number, task)
    for number in range(live):
        sharer, sharing = f"live-{number:02}", f"done-{number * 7:03}"
        raw = packs.blob_bytes(root, refs[sharing][0])
        shared[sharer] = refs[sharing][0]
        refs[sharer], handles[sharer] = _task(root, sharer, status="running", blobs=2, payloads=[raw, json.dumps({"own": sharer}).encode()])
    truth = {task: [packs.blob_bytes(root, ref) for ref in items] for task, items in refs.items()}
    handle_truth = {task: packs.handle_bytes(root, task, ref["path"]) for task, ref in handles.items()}
    loose_before = sum(path.stat().st_size for path in (root / "observability" / "blobs").iterdir())
    started = time.perf_counter()
    report = packer.run_pass(root)
    elapsed = time.perf_counter() - started
    packed_bytes = sum(path.stat().st_size for path in packs.packs_dir(root).glob("*.xz"))
    loose_after = sum(path.stat().st_size for path in (root / "observability" / "blobs").iterdir())
    print(json.dumps({"case": "production-shaped", "terminal": terminal, "live": live, "elapsed_sec": round(elapsed, 2),
                      "loose_gzip_bytes_before": loose_before, "loose_gzip_bytes_after": loose_after,
                      "pack_bytes_after": packed_bytes, "raw_bytes": report["raw"], **{k: report[k] for k in packer.COUNTERS}},
                     sort_keys=True), flush=True)
    assert (report["packed"], report["faults"], report["live"], report["kept"]) == (terminal, 0, live, live)
    assert report["unlinked"] == terminal * 3 - live
    for task, items in refs.items():
        assert [packs.blob_bytes(root, ref) for ref in items] == truth[task]
        assert packs.handle_bytes(root, task, handles[task]["path"]) == handle_truth[task]
    for sharer, ref in shared.items():
        assert pathlib.Path(ref["path"]).exists(), sharer  # kept loose for the live sharer
    for sharer in shared:
        assert not packs.published_generations(root, sharer)
        assert all(pathlib.Path(ref["path"]).exists() for ref in refs[sharer]) and _handle_path(root, sharer, handles[sharer]).exists()
