"""The one background maintenance process: history-sized named jobs, never on a server thread.

Work whose cost grows with accumulated history (evidence discovery and packing, and the other
named jobs in ``JOBS``) runs in ONE child process at background priority: under one GIL a server
thread doing it turns every request's I/O into waiting. Each job keeps its facts in its
``maintenance_jobs`` row of ``state/current.sqlite``, so "once" is a fact of the installation,
a restart of either process resumes from the recorded facts, and a second run over unchanged
inputs decodes no history body, copies nothing and commits nothing.

Server half: ``child`` (``MaintenanceChild``). The supervisor's maintenance dispatcher calls
``tick`` at the end of each reconcile pass and on its first tick of a process (``owed``;
``request`` asks again, e.g. after a boot scheduled a job). ``tick`` only reserves the one slot:
its thread reads cheap facts (``due``: the job rows, the store revision, the terminal-write
counter) and spawns the child only when one of them says work may wait, never because
time passed. The spawn is the existing custodied one (``startup_historical_audit.
spawn_maintenance_job``: session custody, its own process group, a parent lifeline; ``stop`` and
``live_children`` serve the Panic and exit census). Queue-owned work (orphan recovery, drive
settlement under the queue lock and the supervisor's liveness probe) stays in the server: a
fresh child owns no queue.

Child half: ``main`` (``python -m ouroboros.maintenance``) lowers its priority and runs ``JOBS``
in order, each asking its own facts again, and prints one bounded JSON report.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import pathlib
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ouroboros import maintenance_jobs, store
from ouroboros.startup_historical_audit import _REPORT_BYTES, finish_maintenance_job, spawn_maintenance_job

log = logging.getLogger(__name__)
PURPOSE = "maintenance"  # the custody purpose and the module the child runs


def _plain(value: Any) -> Any:
    """The JSON form a job row stores, so a fresh reading compares equal to a recorded one."""
    return json.loads(json.dumps(value, default=str))


def _store_facts(root: pathlib.Path) -> Dict[str, Any]:
    """The dispatcher's store reading, one read transaction: the revision and each pending
    promotion member's row signature (indexed flag column; no body)."""
    from ouroboros.obligations import members_in

    with store.open_store(root, write=False) as txn:
        return {"revision": txn.revision(),
                "promotions": {task_id: _plain(txn.row(task_id, "write_seq", "updated_at"))
                               for task_id in members_in(txn, "promotions")}}


def _now() -> str:
    from ouroboros.utils import utc_now_iso

    return utc_now_iso()


def _revision(root: pathlib.Path) -> int:
    """The store revision now (one meta read): what a pass beginning here has seen."""
    with store.open_store(root, write=False) as txn:
        return txn.revision()


# ---- jobs ------------------------------------------------------------------------------------

def _promotion_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """A pending promotion member with no remembered refusal at its current row and body: a new
    member, a transient failure, or a row that changed since (``source_retention.RetentionMemo``)."""
    from ouroboros.source_retention import memo_epoch

    observed, epoch = row.get("observed") or {}, memo_epoch()
    return any((observed.get(task_id) or {}).get("signature") != signature
               or (observed.get(task_id) or {}).get("epoch") != epoch
               for task_id, signature in facts["promotions"].items())


def _promotion_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """The pending child-ref promotion retry (copies a settled child's call history and sources
    into the canonical store under the task's custody file lock); its durable observations are
    this job's row, its state is written only when it changes."""
    from ouroboros.observability import retry_pending_child_ref_promotions

    report = retry_pending_child_ref_promotions(root, stop=should_stop)
    row = maintenance_jobs.job(root, "child_ref_promotion") or {}
    state = "pending" if _promotion_due(root, row, _store_facts(root), True) else "done"
    if row.get("state") != state:
        maintenance_jobs.record(root, "child_ref_promotion", state=state)
    return {"state": state, "items": len(report["retried"])}


def _any_entry(directory: pathlib.Path) -> bool:
    """Whether DIRECTORY holds anything (one scandir that stops at the first entry)."""
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def _legacy_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Scheduled (``pending``) by the boot detector or the explicit command (``store_import``);
    an interrupted run (``running``) resumes at its cursor; a failed one waits to be scheduled."""
    return row.get("state") in {"pending", "running"}


def _legacy_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """``store_import.run_legacy_reconcile``: the reconcile of the files a store-unaware body wrote,
    root and every descendant drive, with its own durable cursor in this job's row."""
    from ouroboros import store_import

    reconcile = getattr(store_import, "run_legacy_reconcile", None)
    if reconcile is None:  # a body without the job: the row waits for one that has it
        return {"state": "pending", "items": 0}
    row = reconcile(root, should_stop=should_stop) or {}
    return {"state": str(row.get("state") or "unknown"), "items": int(row.get("done") or 0)}


def _trash_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Drives a settlement moved aside (``state/custody_trash``) wait for their deletion. An entry
    the last run could not remove is no reason to spawn the child until its identity changes, and
    the child retries it whenever it runs for another job: a released handle (Windows, a scanner)
    changes neither its inode nor its time."""
    from ouroboros.task_custody import custody_trash_entries

    stuck, entries = row.get("stuck") or {}, custody_trash_entries(root)
    return bool(entries) if child else any(stuck.get(name) != identity for name, identity in entries)


def _trash_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """Delete the moved drives (each was custodied under the queue lock before its move; nothing
    serves or references it), so a child drive's tree is removed off the serving process. The row is
    written only when something changed: a retry that leaves the same entries stuck commits nothing."""
    from ouroboros.task_custody import remove_custody_trash

    report = remove_custody_trash(root, stop=should_stop)
    state = "failed" if report["stuck"] else "done"
    row = maintenance_jobs.job(root, "custody_trash") or {}
    if report["removed"] or (row.get("state"), row.get("stuck") or {}) != (state, report["stuck"]):
        maintenance_jobs.record(root, "custody_trash", state=state, stuck=report["stuck"], removed=report["removed"])
    return {"state": state, "items": report["removed"]}


def _release() -> str:
    from ouroboros import __version__

    return str(__version__)


def _temp_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Once per installed release: a hard kill's orphaned atomic temps are reaped by the next
    release's first child run. Never by itself a reason to spawn the child."""
    return child and row.get("release") != _release()


def _temp_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """The atomic writers' directories only (``temp_sweep.ATOMIC_TEMP_DIRS``), never the whole root or
    ``observability/``; the age guard keeps any in-flight write's temp. The startup sweep of the
    ``tmp_scripts`` fallback (no script can be live then) stays the server's."""
    from ouroboros.temp_sweep import sweep_stale_temp_files

    removed = sweep_stale_temp_files(root, scripts=False)
    maintenance_jobs.record(root, "temp_sweep", state="done", release=_release(), removed=removed)
    return {"state": "done", "items": removed}


def _task_rows(root: pathlib.Path) -> int:
    with store.open_store(root, write=False) as txn:
        return int(txn.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def _analyze_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Planner statistics once per store file (this row lives in it), and again once the task
    table grew tenfold since: statistics describe the data's magnitude, so a fresh install's
    would mislead the planner as it grows. Never by itself a reason to spawn the child."""
    if not child:
        return False
    return row.get("state") != "done" or _task_rows(root) >= 10 * max(1, int(row.get("tasks") or 0))


def _analyze_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    with store.open_store(root, write=True) as txn:
        tasks = int(txn.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        txn.conn.execute("ANALYZE")
    maintenance_jobs.record(root, "sqlite_analyze", state="done", tasks=tasks)
    return {"state": "done", "items": tasks}


def _packing_inputs(root: pathlib.Path) -> Dict[str, Any]:
    """The facts the packing rule reads, cheaply: the store's terminal-write counter (a task closing
    or a closed task written again moves it; a live task's writes do not; one meta row, never a read
    over the rows), whether archived usage segments wait, and whether any loose evidence exists."""
    from ouroboros import evidence_packer

    return {"terminal": _plain(evidence_packer.terminal_revision(root)),
            "archive_pending": evidence_packer.archive_pending(root),
            "loose": any(_any_entry(root / rel) for rel in ("observability/calls", "task_results/artifacts"))}


def _packing_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Evidence may wait when archived usage segments wait, or when loose evidence exists and the
    terminal rows moved since the last pass, or the last pass unlinked (one more look is owed: it
    records the moved directories' stamps), or it left candidates unfinished (``pending``: deferred
    by a held custody lock, busy files, faults) and the store has been written since that pass began
    (the contender that held the lock or the file writes its receipt, during the pass or after it;
    nothing but a write re-arms it, so a persistent fault costs one pass per store change, never a
    tight loop); in the child the unfinished work runs whenever the child runs for another reason."""
    inputs = _packing_inputs(root)
    unfinished = bool(row.get("pending")) and (child or facts["revision"] != row.get("revision"))
    return inputs["archive_pending"] or (inputs["loose"] and (
        row.get("terminal") != inputs["terminal"] or not row.get("quiet") or unfinished))


def _packing_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """Discovery (archive first, then the first changed task stamp) and, when it finds work, one
    packing pass with its own fresh census. The inputs are read before the pass and recorded after
    it, so a task closing during the pass makes the next look differ. A pass that unlinked records
    ``quiet: False``; one that left candidates deferred, busy or faulted records their count as
    ``pending`` with the store revision it has seen: the one its own record leaves when nothing
    else wrote during the pass, else the one the pass began at, since a write during the pass (the
    contender's receipt, committed after the deferral and before this record) is one the pass has
    not seen, and recording the later revision would swallow it (``_packing_due`` re-arms on the
    next write; a quiet install then retries once and rests until the store moves again). A raised
    discovery or pass records nothing: the job stays due for the next look."""
    from ouroboros import evidence_packer, evidence_packs as packs

    inputs = _packing_inputs(root)
    began = _revision(root)  # the census reads the store after this point
    report: Dict[str, Any] = {"status": "completed", **dict.fromkeys(evidence_packer.COUNTERS, 0)}
    if evidence_packer.packing_pending(root, should_stop):
        report = evidence_packer.run_pass(root)
        packs.record_storage_pack(root, phase=report["status"], **{key: report[key] for key in evidence_packer.COUNTERS})
    if should_stop():
        return {"state": "pending", "items": report["examined"]}
    pending = report["deferred"] + report["busy"] + report["faults"]
    with store.open_store(root, write=True) as txn:  # one write: the row and the revision it has seen
        seen = txn.revision() + 1 if txn.revision() == began else began
        maintenance_jobs.record_in(txn, "evidence_packing", state="done", terminal=inputs["terminal"],
                                   quiet=not report["unlinked"], pending=pending, revision=seen,
                                   packed=report["packed"], examined=report["examined"])
    return {"state": "done", "items": report["examined"]}


def _dialogue_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Archive segments the cutover planned and the import has not read (``import_ledger`` pending);
    a failed segment waits on its own input, which the runner compares, so it is no reason to spawn."""
    from ouroboros import dialogue_import, dialogue_store

    if row.get("state") == "failed":
        return child
    if not dialogue_store.present(root):
        return False
    return any(int(view.get("pending") or 0) for view in dialogue_import.import_status(root).values()
               if isinstance(view, dict))


def _dialogue_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    from ouroboros.dialogue_import import run_dialogue_import

    row = run_dialogue_import(root, should_stop=should_stop) or {}
    return {"state": str(row.get("state") or "unknown"), "items": int(row.get("done") or 0)}


def _hot_facts_due(root: pathlib.Path, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """Rows written before the hot-fact columns (or by an older writer since) wait for a classification
    pass (``store_hot_facts.run_hot_facts``); a root without such rows has nothing to do. A failed pass
    waits on its damaged input, which the runner compares itself, so it is no reason to spawn the child."""
    from ouroboros.store_hot_facts import underived

    if row.get("state") == "failed":
        return child
    return underived(root)


def _hot_facts_run(root: pathlib.Path, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    from ouroboros.store_hot_facts import run_hot_facts

    row = run_hot_facts(root, should_stop=should_stop) or {}
    return {"state": str(row.get("state") or "unknown"), "items": int(row.get("done") or 0)}


# name -> (due(root, row, store facts, child), run(root, should_stop)); the child runs them in this
# order. ``child`` is False for the server's look: a job may then decline to be a reason to spawn
# (it still runs whenever the child runs for another reason).
JOBS: Dict[str, tuple] = {
    # Before the archive import: the drives it reconciles can leave dialogue work, which the import
    # then finishes in the same pass (the next pass rests; tests/test_cost_contract.py B6).
    "legacy_reconcile": (_legacy_due, _legacy_run),
    "dialogue_import": (_dialogue_due, _dialogue_run),  # the owner's history: newest segments first
    "hot_facts": (_hot_facts_due, _hot_facts_run),
    "child_ref_promotion": (_promotion_due, _promotion_run),
    "custody_trash": (_trash_due, _trash_run),
    "temp_sweep": (_temp_due, _temp_run),
    "sqlite_analyze": (_analyze_due, _analyze_run),
    "evidence_packing": (_packing_due, _packing_run),  # last: its quiet fact follows every other write
}
JOB_NAMES = tuple(JOBS)


def due(root: Any) -> List[str]:
    """The jobs whose facts say work may wait (cheap reads only; nothing is walked here)."""
    root = pathlib.Path(root)
    rows = maintenance_jobs.jobs(root)
    facts = _store_facts(root)
    return [name for name, (is_due, _run) in JOBS.items() if is_due(root, rows.get(name) or {}, facts, False)]


def _contention(exc: BaseException) -> bool:
    """A held store or lock (current facts, packs index, money): the job stays due, nothing failed."""
    return isinstance(exc, store.StoreBusy) or getattr(exc, "reason", None) == "contention"


def run_job(root: Any, name: str, *, should_stop: Optional[Callable[[], bool]] = None) -> Optional[Dict[str, Any]]:
    """Run one named job in THIS process when its own facts say work may wait (the child's step;
    tests and the cost contract call it); ``None`` when the job is at rest: only its facts were read."""
    root = pathlib.Path(root)
    is_due, run_one = JOBS[name]
    if not is_due(root, maintenance_jobs.job(root, name) or {}, _store_facts(root), True):
        return None
    return run_one(root, should_stop or (lambda: False))


def run(root: Any, *, should_stop: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """The child's pass: every due job in order; one job's failure never skips the next."""
    root, stop = pathlib.Path(root), should_stop or (lambda: False)
    report: Dict[str, Any] = {"status": "completed", "jobs": {}}
    for name in JOBS:
        if stop():
            break
        try:
            outcome = run_job(root, name, should_stop=stop)
        except Exception as exc:
            if not _contention(exc):
                log.warning("Maintenance job %s failed", name, exc_info=True)
                report["status"] = "failed"
            outcome = {"state": "pending" if _contention(exc) else "failed", "items": 0}
        if outcome is not None:
            report["jobs"][name] = {"state": str(outcome.get("state") or "unknown"),
                                    "items": int(outcome.get("items") or 0)}
    return report


# ---- the server half -------------------------------------------------------------------------

def _record(data_root: Any, phase: str, **facts: Any) -> None:
    from ouroboros.utils import append_jsonl

    append_jsonl(pathlib.Path(data_root) / "logs" / "supervisor.jsonl",
                 {"ts": _now(), "type": "maintenance", "phase": phase, **facts})


def _report_fields(raw: bytes) -> Dict[str, Any]:
    """The child's report under a closed schema; anything else is ``{}`` (phase unknown)."""
    try:
        value = json.loads(raw) if len(raw) <= _REPORT_BYTES else None
        if not isinstance(value, dict) or value.get("status") not in {"completed", "unknown", "failed"}:
            return {}
        jobs = value.get("jobs")
        if not isinstance(jobs, dict) or set(jobs) - set(JOB_NAMES):
            return {}
        fields: Dict[str, Any] = {"status": value["status"], "jobs": {}}
        for name, outcome in jobs.items():
            state, items = (outcome or {}).get("state"), (outcome or {}).get("items")
            if state not in {*maintenance_jobs.STATES, "unknown"} or type(items) is not int or items < 0:
                return {}
            fields["jobs"][name] = {"state": state, "items": items}
        return fields
    except (ValueError, UnicodeError, TypeError, AttributeError):
        return {}


class MaintenanceChild:
    """The one maintenance child of this server process (module docstring)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._owed = True  # the first maintenance tick of a process looks at the job facts once
        self._process = None
        self._thread: Optional[threading.Thread] = None

    def request(self) -> None:
        """Ask the next supervisor maintenance tick to look at the job facts (a job was scheduled)."""
        self._owed = True

    def owed(self) -> bool:
        return self._owed and not self._stopped.is_set()

    def tick(self, data_root: Any, repo_dir: Any) -> bool:
        """Reserve the one slot; True means the dispatch thread started (it spawns the child only
        when a job is due). No filesystem or store access on the caller's thread."""
        with self._lock:
            if self._stopped.is_set() or (self._thread is not None and self._thread.is_alive()):
                return False
            self._owed = False
            self._thread = threading.Thread(target=self._run, args=(pathlib.Path(data_root), pathlib.Path(repo_dir)),
                                            name="maintenance-child", daemon=True)
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
                with store.acquisition_budget(0):  # contention defers to the next look, never waits
                    names = due(data_root)
            except Exception:
                log.debug("Maintenance job facts unavailable; the next look retries", exc_info=True)
                return
            if not names or self._stopped.is_set():
                return
            process = spawn_maintenance_job(data_root, repo_dir, PURPOSE)
            finish_maintenance_job(self, process, data_root, started, _report_fields, _record)
        except Exception as exc:
            with contextlib.suppress(Exception):
                _record(data_root, phase="failed", exception_class=type(exc).__name__,
                        duration_seconds=time.monotonic() - started)
        finally:
            with self._lock:
                self._process = None


child = MaintenanceChild()


# ---- the child half --------------------------------------------------------------------------

def _lower_priority() -> None:
    """Background priority: POSIX nice, the Windows below-normal class; best effort."""
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except (OSError, AttributeError):
        log.debug("Maintenance priority unchanged", exc_info=True)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--custody-session", default="")
    args = parser.parse_args(argv)
    os.environ["OUROBOROS_DATA_DIR"] = str(pathlib.Path(args.data_root).resolve())
    # Roots are bound before importing readers that capture config globals.
    from ouroboros.process_custody import adopt_session_id, start_parent_lifeline

    adopt_session_id(args.custody_session)
    start_parent_lifeline(label=PURPOSE)
    _lower_priority()
    try:
        report = run(pathlib.Path(args.data_root))
    except Exception as exc:
        report = {"status": "failed", "jobs": {}, "exception_class": type(exc).__name__}
    print(json.dumps({key: report[key] for key in ("status", "jobs")}, separators=(",", ":")), flush=True)
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
