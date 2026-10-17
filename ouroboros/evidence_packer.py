"""Supervised maintenance packer; scheduling and storage protocol: docs/PERSISTENCE.md, evidence pack row."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ouroboros import evidence_packs as packs, store
from ouroboros.startup_historical_audit import _REPORT_BYTES, finish_maintenance_job, spawn_maintenance_job

log = logging.getLogger(__name__)
# ``packed`` = new generations written; ``examined`` = candidates processed; ``verified_bytes`` = raw bytes re-read.
_COUNTERS = ("packed", "examined", "swept", "candidates", "raw", "compressed", "verified_bytes",
             "unlinked", "kept", "busy", "faults", "archived")


def task_stamps(root: pathlib.Path, task_id: str, *, obligation_rows: Optional[dict] = None,
                external: Optional[dict] = None, txn=None) -> str:
    """One task's stamp: its row and duties from one short read (or the caller's ``txn``), then ``_stamp``."""
    from ouroboros.task_store import signature

    with (store.open_store(root, write=False) if txn is None else contextlib.nullcontext(txn)) as txn:
        seq = signature(root, task_id, txn=txn)
        if obligation_rows is None:
            obligation_rows = packs.settlement_obligations(root, txn=txn)
    return _stamp(root, task_id, seq, obligation_rows, packs.settlement_external(root) if external is None else external)


def _stamp(root: pathlib.Path, task_id: str, seq, obligation_rows: dict, external: dict) -> str:
    """Evidence and settlement-source revisions (row, owner-held duties, outbox sends, the two directories),
    built from facts already read: no store access, so no transaction spans its ``stat`` calls."""
    from ouroboros.artifacts import task_artifact_dir_path

    stamps = [("task", seq), ("obligations", obligation_rows.get(task_id, {}))]
    stamps.extend((name, None if tasks is None else task_id in tasks) for name, tasks in external.items())
    paths = [root / "observability" / "calls" / task_id,
             task_artifact_dir_path(root, task_id, create=False).joinpath(*packs.CHECKPOINTS_REL.parts)]
    for path in paths:
        try:
            stat = path.stat()
            stamps.append((str(path.relative_to(root)), stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except OSError:
            stamps.append((str(path.relative_to(root)), 0))
    return json.dumps(stamps, sort_keys=True, separators=(",", ":"))


def _loose_task_ids(root: pathlib.Path, stop: Optional[Callable[[], bool]] = None) -> set:
    ids = set()
    for directory, checkpoints in ((root / "observability" / "calls", False),
                                   (root / "task_results" / "artifacts", True)):
        if stop is not None and stop():
            break
        with contextlib.suppress(OSError), os.scandir(directory) as entries:
            for entry in entries:
                if stop is not None and stop():
                    return ids
                if entry.is_dir(follow_symlinks=False) and (not checkpoints or
                        (pathlib.Path(entry.path) / "source_handles" / "context_checkpoints").is_dir()):
                    ids.add(entry.name)
    return ids


def swept_stamps(root: pathlib.Path, *, discovery: bool = False) -> Optional[Dict[str, str]]:
    """Per task, the stamps the last sweep recorded; ``None`` when the cache is unavailable."""
    if not packs.packs_dir(root).is_dir():
        return {}
    try:
        with packs.open_index(root, **({"wait_sec": 0, "rebuild": False} if discovery else {})) as conn:
            return {row["task_id"]: row["stamps"] for row in conn.execute("SELECT task_id, stamps FROM swept")}
    except store.StoreBusy:
        if discovery:
            raise  # advisory discovery skips this pass instead of waiting or spawning on contention
        return None
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        if discovery and isinstance(exc, sqlite3.OperationalError) and store.busy(exc):
            raise store.StoreBusy("evidence index read busy", reason="contention") from exc
        return None


def record_swept(root: pathlib.Path, task_id: str, stamps: str) -> None:
    with contextlib.suppress(store.StoreError, sqlite3.Error, OSError):
        with packs.open_index(root, write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO swept VALUES (?,?)", (task_id, stamps))


def _changed_stamps(root: pathlib.Path, *, obligation_rows: Optional[dict] = None, external: Optional[dict] = None,
                    seqs: Optional[dict] = None, stop: Optional[Callable[[], bool]] = None) -> Iterator[Tuple[str, str]]:
    """Tasks with loose evidence whose directories or result changed since a sweep recorded them, with their
    stamps. The rows and duties come from ONE short read that is closed before the directory probes."""
    root = pathlib.Path(root)
    ids = _loose_task_ids(root) if stop is None else _loose_task_ids(root, stop)
    if not ids or (stop is not None and stop()):
        return
    if obligation_rows is None or seqs is None:
        with store.open_store(root, write=False) as txn:
            obligation_rows = packs.settlement_obligations(root, txn=txn) if obligation_rows is None else obligation_rows
            seqs = txn.write_seqs() if seqs is None else seqs
    if stop is not None and stop():
        return
    external = packs.settlement_external(root, **({"timeout_sec": 0} if stop is not None else {})) if external is None else external
    if stop is not None and stop():
        return
    swept = swept_stamps(root, discovery=stop is not None)
    for task in sorted(ids):
        if stop is not None and stop():
            return
        stamp = _stamp(root, task, seqs.get(task), obligation_rows, external)
        if swept is None or swept.get(task) != stamp:
            yield task, stamp


def changed_stamps(root: pathlib.Path, **census) -> Dict[str, str]:
    """Full child census; never reuse the parent's advisory discovery as unlink authority."""
    return dict(_changed_stamps(root, **census))


def packing_pending(root: pathlib.Path, stop: Callable[[], bool]) -> bool:
    """Advisory, stop-aware discovery: archives first, then only the first changed stamp.

    Contended stores defer the pass. Index reconstruction belongs to the supervised
    child, not this reader. A quiet nonempty root still needs a full stamp walk.
    """
    if stop():
        return False
    if archive_pending(root):
        return not stop()
    with store.acquisition_budget(0):
        changed = next(_changed_stamps(root, stop=stop), None)
    return changed is not None and not stop()


def changed_tasks(root: pathlib.Path, **census) -> List[str]:
    return sorted(changed_stamps(root, **census))


def archive_pending(root: pathlib.Path) -> bool:
    directory = pathlib.Path(root) / packs.USAGE_ARCHIVE_REL
    return directory.is_dir() and any(directory.glob("segment_ep*.jsonl"))


def ordered_candidates(root: pathlib.Path, settled: List[str]) -> List[Tuple[str, int]]:
    """Settled tasks with their loose raw size: the ones whose result changed after the newest
    published pack first (newest result first), then the backfill by descending raw size."""
    from ouroboros.task_store import signature

    def timestamp(value):
        try:
            return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0

    newest_pack = max((timestamp(sidecar.get("created_at")) for _, sidecar in packs.list_sidecars(root)), default=0)
    recent, backfill = [], []
    for task in settled:
        raw = sum(int(m["size"]) for m in packs.loose_members(root, task))
        revision = signature(root, task)
        touched = timestamp(revision[1]) if revision else 0
        (recent if newest_pack and touched > newest_pack else backfill).append((task, raw, touched))
    recent.sort(key=lambda item: (-item[2], item[0]))
    backfill.sort(key=lambda item: (-item[1], item[0]))
    return [(task, raw) for task, raw, _ in recent + backfill]


def _external_revision(root: pathlib.Path) -> list:
    """A snapshot-validity fence, never part of a task's candidate stamp."""
    from ouroboros.usage_store import STORE_REL
    from ouroboros.usage_ledger import LEDGER_REL

    stamps = []
    for rel in (STORE_REL, str(STORE_REL) + "-wal", LEDGER_REL, "state/terminal_deliveries.json"):
        try:
            stat = (root / rel).stat()
            stamps.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return stamps


class PackSweep:
    """One settlement census, outside deletion locks. Before each unlink ``refresh`` brings it
    current, still outside the locks: only tasks settled at the census whose facts moved are
    re-read, and one that left settlement adds its references to the keep-set, so activity
    elsewhere never stops a pass. Publications add their new refs under the publication lock."""
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.obligation_cache: dict = {}
        with store.open_store(root, write=False) as txn:  # one baseline, older than every fact this pass reads
            self.duties = packs.settlement_obligations(root, txn=txn, cache=self.obligation_cache)
            self.seqs = txn.write_seqs()
        self.external_revision = _external_revision(root)
        self.external = packs.settlement_external(root)
        self.keep: set = set()
        self.settled: Dict[str, Optional[int]] = {}  # census-settled loose task -> its write sequence
        self.journal = None

    def current(self, txn=None) -> bool:
        if txn is None:
            with store.open_store(self.root, write=False) as txn:
                return self.current(txn)
        return (self.obligation_cache["revision"] == (str(txn.root), txn.revision())
                and self.external_revision == _external_revision(self.root))

    def collect_keep(self, facts: dict) -> None:
        for task in _loose_task_ids(self.root):
            fact = facts.get(task)
            if fact is None:
                fact = packs.task_settlement_facts(self.root, task, obligation_rows=self.duties, external=self.external)
            if packs.facts_settled(fact):
                self.settled[task] = self.seqs.get(task)
            else:
                self.keep.update(sha for _, sha, _ in packs.manifest_blob_refs(self.root, task))

    def refresh(self) -> None:
        """Bring the census current outside the deletion locks (class docstring)."""
        if self.current():
            return
        external_revision = _external_revision(self.root)
        with store.open_store(self.root, write=False) as txn:
            self.duties = packs.settlement_obligations(self.root, txn=txn, cache=self.obligation_cache)
            seqs = txn.write_seqs()
        self.external, self.external_revision = packs.settlement_external(self.root), external_revision
        for task, seq in list(self.settled.items()):
            if seqs.get(task) == seq and task not in self.duties and all(
                    tasks is not None and task not in tasks for tasks in self.external.values()):
                continue
            fact = packs.task_settlement_facts(self.root, task, obligation_rows=self.duties, external=self.external)
            if packs.facts_settled(fact):
                self.settled[task] = seqs.get(task)
            else:
                del self.settled[task]
                self.keep.update(sha for _, sha, _ in packs.manifest_blob_refs(self.root, task))

    def refresh_keep(self) -> set:
        # The caller holds the publication lock: only the appended suffix is read.
        for line in self.journal:
            sha = line.decode("ascii").strip()
            if len(sha) != 64 or sha.strip("0123456789abcdef"):
                raise OSError("incomplete evidence publication journal")
            self.keep.add(sha)
        return self.keep


@contextlib.contextmanager
def pack_sweep(root: pathlib.Path):
    """Serialize packer passes, tracking publications from before the census to the
    last unlink. A crashed pass's scratch journal is replaced by the next pass."""
    with store.name_lock(root, "packs_sweep.lock", timeout_sec=packs.INDEX_WAIT_SEC):
        sweep = PackSweep(root)
        path = packs.packs_dir(root) / packs.PUBLICATIONS_NAME
        with store.name_lock(root, packs.PUBLICATION_LOCK_NAME, timeout_sec=packs.INDEX_WAIT_SEC):
            path.parent.mkdir(parents=True, exist_ok=True)
            sweep.journal = path.open("w+b")
        try:
            yield sweep
        finally:
            with store.name_lock(root, packs.PUBLICATION_LOCK_NAME, timeout_sec=packs.INDEX_WAIT_SEC):
                sweep.journal.close()
                path.unlink()



def run_pass(root: pathlib.Path, *, sweep=None) -> Dict[str, Any]:
    """Pack every settled candidate one task at a time, then the money archive and the unpacker."""
    root = pathlib.Path(root)
    if sweep is None:
        with pack_sweep(root) as sweep:
            return run_pass(root, sweep=sweep)
    report: Dict[str, Any] = {"status": "completed", **dict.fromkeys(_COUNTERS, 0)}
    duties, external = sweep.duties, sweep.external
    # Stamps from the census baseline (older than every fact read below): evidence that arrives during the
    # pass makes the recorded stamp stale, so the next pass selects that task again.
    revisions = changed_stamps(root, obligation_rows=duties, external=external, seqs=sweep.seqs)
    facts = {task: packs.task_settlement_facts(root, task, obligation_rows=duties, external=external) for task in revisions}
    settled = {task for task, fact in facts.items() if packs.facts_settled(fact)}
    for task, fact in facts.items():
        if task not in settled and fact["money_open"] is not None and "unknowns" not in fact["obligations"]:
            record_swept(root, task, revisions[task])
            report["swept"] += 1
    candidates = ordered_candidates(root, sorted(settled))
    report["candidates"] = len(candidates)
    listing = packs.list_sidecars(root)
    if candidates:
        sweep.collect_keep(facts)
    for task, raw in candidates:
        try:
            outcome = packs.pack_task(root, task, keep_shas=set(), listing=listing, revision=revisions[task],
                                      sweep=sweep)
        except Exception as exc:
            report["faults"] += 1
            packs.record_storage_pack(root, phase="fault", task_id=task, exception_class=type(exc).__name__)
            log.warning("Evidence pack of %s failed", task, exc_info=True)
            continue
        report["examined"] += 1
        report["packed"] += int(outcome.get("generation") is not None)
        report["raw"] += int(outcome.get("raw") or 0)
        report["compressed"] += int(outcome.get("packed") or 0)
        for key in ("unlinked", "kept", "busy", "verified_bytes"):
            report[key] += int(outcome.get(key) or 0)
        # A blob kept for a live sharer does not re-queue this task: the sharer's own pass unlinks it
        # once it settles (its stamp moves then); busy files and deferred candidates retry.
        if not any(outcome.get(key) for key in ("busy", "deferred")):
            record_swept(root, task, revisions[task])
            report["swept"] += 1
    try:
        report["archived"] = packs.archive_usage_segments(root)["archived"]
    except Exception as exc:
        report["faults"] += 1
        packs.record_storage_pack(root, phase="fault", task_id="", exception_class=type(exc).__name__)
    return report



class EvidencePacker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._armed = False
        self._process = None
        self._thread: Optional[threading.Thread] = None

    def tick(self, data_root: pathlib.Path, repo_dir: pathlib.Path) -> bool:
        """Reserve one discovery thread, never on the boot tick; no filesystem walk here.

        True means discovery started, not that a packing child was needed or spawned.
        """
        with self._lock:
            if self._stopped.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            if not self._armed:
                self._armed = True  # the boot tick only arms the packer
                return False
            root = pathlib.Path(data_root)
            self._thread = threading.Thread(target=self._run, args=(root, pathlib.Path(repo_dir)),
                                            name="evidence-packer", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        self._stopped.set()
        process = self._process
        if process is not None:
            self._signal(process)

    def live_children(self) -> list:
        """Exit census after Stop: include an unfinished spawn until its owner returns."""
        process, thread = self._process, self._thread
        if process is not None and process.poll() is None:
            return [process]
        return [thread] if thread is not None and thread.is_alive() else []

    @staticmethod
    def _signal(process) -> None:
        with contextlib.suppress(OSError):
            process.kill()  # its own process group: the signal reaches only our child

    def _run(self, data_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        started = time.monotonic()
        try:
            try:
                pending = packing_pending(data_root, self._stopped.is_set)
            except Exception:
                log.debug("Evidence pack candidate check failed", exc_info=True)
                return
            if not pending or self._stopped.is_set():
                return
            process = spawn_maintenance_job(data_root, repo_dir, "evidence_packer")
            fields = finish_maintenance_job(self, process, data_root, started, _report_fields, packs.record_storage_pack)
            if fields.get("packed"):
                log.info("Evidence packs: packed %d/%d tasks, %.2f GB -> %.2f GB", fields["packed"], fields["candidates"],
                         fields["raw"] / 1e9, fields["compressed"] / 1e9)
        except Exception as exc:
            packs.record_storage_pack(data_root, phase="failed", exception_class=type(exc).__name__,
                                      duration_seconds=time.monotonic() - started)
        finally:
            with self._lock:
                self._process = None


def _report_fields(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw) if len(raw) <= _REPORT_BYTES else None
        if not isinstance(value, dict) or value.get("status") not in {"completed", "unknown", "failed"}:
            return {}
        if any(type(value.get(key)) is not int or value[key] < 0 for key in _COUNTERS):
            return {}
        return {key: value[key] for key in ("status", *_COUNTERS)}
    except (ValueError, UnicodeError, TypeError):
        return {}


packer = EvidencePacker()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--custody-session", default="")
    args = parser.parse_args()
    os.environ["OUROBOROS_DATA_DIR"] = str(pathlib.Path(args.data_root).resolve())
    from ouroboros.process_custody import adopt_session_id, start_parent_lifeline

    adopt_session_id(args.custody_session)
    start_parent_lifeline(label="evidence_packer")
    try:
        report = run_pass(pathlib.Path(args.data_root))
    except Exception as exc:
        report = {"status": "failed", "exception_class": type(exc).__name__}
    print(json.dumps(report, separators=(",", ":")), flush=True)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
