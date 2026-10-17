"""Create, import, reconcile and export the current-facts store.

``ensure_current_store(root)`` is the only function that creates or imports a store:
the sibling build with ``synchronous=FULL``, ``meta`` (provenance, counts, the freeze
instant, the per-file content baseline: size, mtime_ns and sha256 of every frozen
``task_results/*.json``) written before publication, publish by ``os.replace`` plus a
directory fsync, the generation stamp after; a published but unstamped store is
accepted by its own ``meta``; the lock tier is re-probed on every boot and governs an
EXISTING store too: a header naming the other tier is re-tiered in place (application id
and ``meta.lock_tier``) under the import lock before any writer opens it, so the open
mode follows what the kernel enforces today, and every underived flag (a missing column, a row an
older body wrote) is derived (``_derive_flag_columns``); every own child drive down the tree (``descendant_drives``,
over ``task_custody.own_child_drives``) gets its own store by the same code; inadmissible rows are quarantined at import
through the existing emitter; the cancel-latch migration runs as the first pass.
Reconciliation (a published store found at boot) is per row against the baseline: a
changed file replaces its row, a deleted file moves its row to quarantine, a new file is
imported, an untouched row keeps the DB version, a row changed on both sides lets the
file win and keeps the DB version in ``meta.conflicts``; a full rebuild happens ONLY
after a proven complete export. A file whose size and mtime_ns equal its baseline is
untouched without being read (one listing plus one stat per boot; a rewrite that preserves
both is the disclosed residual); a stat-changed file is compared by sha256, so a touch with
the same bytes stays untouched; edits to obligation set files alone are not detected.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import pathlib
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from ouroboros import obligations as o
from ouroboros import store
from ouroboros.store import (
    GENERATION, GENERATION_KEY, IMPORT_LOCK_NAME, LOCK_NAME, PUBLISHED, TIER_ENFORCED, TIER_NAME,
    StoreLost, Txn, document_text, supervisor_row, utc_stamp,
)
from ouroboros.task_custody import HEADLESS_TASKS_DIR, TASK_DRIVES_DIR, own_child_drives
from ouroboros.task_result_schema import (
    TASK_RESULT_QUARANTINE_DIR, emit_quarantine_event, quarantine_task_result, stamp_task_result_schema,
    task_result_schema_refusal,
)
from ouroboros.task_results import task_result_path, task_results_dir
from ouroboros.utils import append_jsonl, atomic_write_json, machine_json, utc_now_iso, write_text_atomic

Record = Tuple[str, Dict[str, Any]]


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _own_children(root: pathlib.Path) -> List[pathlib.Path]:
    """The own child drives of every task ONE root knows (result files and drive directories)."""
    ids = {path.stem for path in task_results_dir(root, create=False).glob("*.json")}
    for base in (root / HEADLESS_TASKS_DIR, root / TASK_DRIVES_DIR):
        if base.is_dir():
            ids.update(child.name for child in base.iterdir() if child.is_dir() and not child.is_symlink())
    drives: List[pathlib.Path] = []
    for task_id in sorted(ids):
        with contextlib.suppress(ValueError):
            drives.extend(own_child_drives(root, task_id))
    return drives


def descendant_drives(root: Any) -> List[pathlib.Path]:
    """Every own child drive down the tree (a child's drive names its own children the same
    way), parents before children: the one walk the import, the switch, the export, the
    retirement and the recovery all use."""
    found: List[pathlib.Path] = []
    pending = [pathlib.Path(root)]
    while pending:
        children = _own_children(pending.pop(0))
        found.extend(children)
        pending.extend(children)
    return found


def ensure_current_store(root: Any, *, with_children: bool = True, rederive: bool = False) -> Dict[str, Any]:
    """Idempotent boot import; returns its logs/supervisor.jsonl report. Details above.
    ``rederive`` (the explicit rebuild) derives every nullable flag again from its document."""
    root = pathlib.Path(root)
    path = store.store_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with store.process_lock(path), store.name_lock(root, IMPORT_LOCK_NAME, timeout_sec=600.0, stale_sec=600.0):
        for name in os.listdir(path.parent):
            if name.startswith(path.name + ".import-"):
                with contextlib.suppress(OSError):
                    (path.parent / name).unlink()  # an unfinished import never published: redone from scratch
        report, quarantined = _ensure(root, path, rederive=rederive)
    emit_quarantine_event(root, quarantined)
    if with_children:
        report["drives"] = {str(drive): ensure_current_store(drive, with_children=False, rederive=rederive)["status"]
                            for drive in descendant_drives(root)}
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    supervisor_row(root, "current_store_import", {"phase": report["status"],
                                                  **{key: value for key, value in report.items() if key != "drives"}})
    return report


def _ensure(root: pathlib.Path, path: pathlib.Path, *, rederive: bool = False) -> Tuple[Dict[str, Any], list]:
    from ouroboros.platform_layer import kernel_file_locks_enforced
    from ouroboros.startup_migrations import stamp

    state, header = store.probe(root)
    tier = TIER_ENFORCED if kernel_file_locks_enforced(root / "state" / LOCK_NAME) else TIER_NAME
    if state == "present":
        retiered = {"retiered_from": header} if header != tier else {}
        with store.open_store(root, write=True, service=True) as txn:  # opened under the header's protocol, left under today's
            if retiered:
                txn.conn.execute(f"PRAGMA application_id = {store._APPLICATION_ID[tier]}")
                txn.set_meta("lock_tier", tier)
            flags = _derive_flag_columns(txn, rederive=rederive)  # before any row write needs the column
            phase = txn.phase()
            if phase == PUBLISHED:
                _upgrade_review_collection(txn)
                report, quarantined = _reconcile(root, txn)
            export = txn.meta("export") or {}
        if phase == PUBLISHED:
            if not store.generation_stamped(root):
                stamp(root, **{GENERATION_KEY: GENERATION})  # the stamp a crash after publication never wrote
            _latches_into_intents(root, report.pop("latched", []))
            return {**report, "lock_tier": tier, **retiered, **flags}, quarantined
        if export.get("status") != "completed":
            return {"status": "switch_pending", "phase": phase, "lock_tier": tier, **retiered}, []  # the seam resumes it
        stamp(root, **{GENERATION_KEY: None})  # before the rename: absent + stamped is always a loss
        os.replace(path, path.with_name(f"{path.name}.exported-{utc_stamp()}"))  # a full rebuild after a proven complete export
    elif store.generation_stamped(root):
        raise StoreLost(f"current store missing beside its completed stamp: {path}")
    return _build_and_publish(root, path, tier)


def _upgrade_review_collection(txn: Txn) -> None:
    """Schema only: old bodies stay unknown until their ordinary maintenance owner visits them.
    DDL and its version commit together; an interrupted upgrade is retried in place."""
    columns = {row["name"] for row in txn.conn.execute("PRAGMA table_info(tasks)")}
    for column, index in (("review_collection_pending", store.REVIEW_COLLECTION_INDEX),
                          ("plan_collection_pending", store.PLAN_COLLECTION_INDEX)):
        if column not in columns:
            txn.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} INTEGER")
        txn.conn.execute(index)
    if "drive_retention_at" not in columns:
        txn.conn.execute("ALTER TABLE tasks ADD COLUMN drive_retention_at TEXT")
    if int(txn.meta("schema_version") or 1) < store.SCHEMA_VERSION:
        txn.set_meta("schema_version", store.SCHEMA_VERSION)


def upgrade_current_schema(root: Any) -> None:
    """First-touch writer/maintenance preparation, never a passive read or history import.
    The ordinary write gate and inherited acquisition budget also govern this DDL:
    a caller under a queue/launch lock cannot acquire an extra long import lock."""
    with store.open_store(root, write=True) as txn:
        _derive_flag_columns(txn)  # all prior flags must exist before the version advances
        _upgrade_review_collection(txn)


def _derive_flag_columns(txn: Txn, *, rederive: bool = False) -> Dict[str, Any]:
    """Derive every nullable flag (``store.FLAG_COLUMNS``) left NULL, "not derived": a store
    published before the column gains it NULL with its index, and a row a body that predates the
    column wrote through its own column list (``INSERT OR REPLACE``: a rollback's writes, its new
    tasks) holds NULL whatever ``meta.schema_version`` says. Each such row takes the predicate
    every later write applies (``result_memberships``) over its document; an undecodable body is
    owed (unknown is never absence) until its next write recomputes it. Cost: one narrow pass over
    ``tasks`` per boot, a body decode per underived row; ``rederive`` (the explicit rebuild)
    forgets every nullable flag first, so it decodes every body. Only complete schema
    preparation publishes the version; this flag-only step may also run during export."""
    columns = {record["name"]: record["notnull"] for record in txn.conn.execute("PRAGMA table_info(tasks)")}
    added = sorted(name for name, column in store.FLAG_COLUMNS.items() if column not in columns)
    for name in added:
        column = store.FLAG_COLUMNS[name]
        txn.conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} INTEGER")
        txn.conn.execute(f"CREATE INDEX tasks_{column} ON tasks({column}) WHERE {column}=1")
    nullable = {name: column for name, column in store.FLAG_COLUMNS.items() if not columns.get(column)}
    if not nullable:
        return {}
    if rederive:
        txn.conn.execute(f"UPDATE tasks SET {', '.join(f'{column}=NULL' for column in nullable.values())}")
    underived = [record["task_id"] for record in txn.conn.execute(
        f"SELECT task_id FROM tasks WHERE {' OR '.join(f'{column} IS NULL' for column in nullable.values())}")]
    for task_id in underived:
        try:
            names = o.result_memberships(txn.load(task_id) or {})
        except ValueError:
            names = set(nullable)
        txn.conn.execute(f"UPDATE tasks SET {', '.join(f'{column}=?' for column in nullable.values())} WHERE task_id=?",
                         (*(int(name in names) for name in nullable), task_id))
    return {**({"flag_columns_added": added} if added else {}),
            **({"flag_rows_derived": len(underived)} if underived else {})}


def _build_and_publish(root: pathlib.Path, path: pathlib.Path, tier: str) -> Tuple[Dict[str, Any], list]:
    from ouroboros.startup_migrations import boot_admissions, migrate_cancel_latches, stamp

    sets = _read_sets(root)
    sets_baseline = _sets_baseline(sets)  # the same file snapshot, before boot/chain admissions
    records, baseline, quarantined, refused = _read_results(root)
    for name, rows in boot_admissions(root).items():  # under the set files: an interrupted publication stays a candidate
        sets[name] = {**rows, **sets.get(name, {})}
    folded, custody = _custody_rows(root, records)
    # The chain decides a pending invocation: a request the retained chain bound to a run or
    # refused (an old body's append after the export) is not readmitted from the exported set file.
    resolved = {str(facts.get("invocation_id") or "") for facts, _hold in folded["runs"].values()} | set(folded["refused"])
    for name, rows in custody.items():
        if name == "custody_open":
            kept = {key: facts for key, facts in sets.get(name, {}).items() if key.split(":", 1)[-1] not in resolved}
            sets[name] = {**kept, **rows}
        else:
            sets[name] = _merge_receipts(sets.get(name, {}), rows)
    sets = {name: o.owner_held_rows(name, rows) for name, rows in sets.items()}  # derived members come from the columns
    provenance = {"status": "completed", "imported_at": utc_now_iso(), "lock_tier": tier,
                  "results": len(records), "quarantined": len(quarantined), "refused": refused,
                  "sets": {name: len(rows) for name, rows in sets.items()},
                  "custody_runs": len(folded.get("runs", {}))}
    tmp = path.with_name(f"{path.name}.import-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        _build(tmp, tier, root, records, sets, baseline, provenance, folded,
               sets_baseline=sets_baseline, chain_offset=folded.get("chain_offset", 0),
               occupied={**_quarantined_occupied(root, [task_id for task_id, _ in records]),
                         **{row["task_id"]: row["reason"] for row in quarantined}, **refused})
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    store.fsync_directory(path.parent)
    stamp(root, **{GENERATION_KEY: GENERATION})  # after publication: a published, unstamped store is still ours
    _latches_into_intents(root, [task_id for task_id, entry in baseline.items() if entry.get("latch_stamped")])
    migrate_cancel_latches(root, records=[(task_id, document, "") for task_id, document in records])
    return {"status": "completed", "lock_tier": tier, "results": len(records), "quarantined": len(quarantined),
            "refused": len(refused), "sets": len(sets)}, quarantined


def _latches_into_intents(root: pathlib.Path, latched: List[str]) -> None:
    """The legacy latch rows this import stamped: one typed events row per import, and each
    becomes a cancel intent (the generation watermark does not gate rows an old body wrote later)."""
    if not latched:
        return
    from ouroboros.cancel_intents import migrate_legacy_cancel_latches

    append_jsonl(root / "logs/events.jsonl", {"ts": utc_now_iso(), "type": "task_result_cancel_latch_admitted",
                                              "count": len(latched), "task_ids": latched, "reason": "unstamped_pre_7_0"})
    with store.open_store(root, write=False) as txn:
        records = [(task_id, txn.load(task_id), "") for task_id in latched]
    migrate_legacy_cancel_latches(root, records=[record for record in records if record[1]])


def _custody_rows(root: pathlib.Path, records: List[Record]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Dict[str, Any]]]]:
    """The custody half of the import, folded ONCE from the retained event chain: every run's
    row (``custody_runs``), the pending invocations (``custody_open``), the definite refusals
    and the task marks, plus the closing receipts still owed to a result's terminal refresh
    (``delegated_runs``). A chain that exists but cannot be read in full (EMFILE, EIO, an
    unopenable segment) ABORTS the import typed — nothing is built or stamped and the next
    boot retries — never a published store with no custody row; a missing chain is empty."""
    from ouroboros import delegate_custody as c
    from ouroboros.delegate_custody_current import fold

    chain = c.event_log_path(root)
    try:
        if c.custody_chain_unreadable(root):
            raise OSError(errno.EIO, "custody chain exists but cannot be opened")
        rows, offset = c.rows_after(chain, 0)
    except OSError as exc:
        raise _unreadable(exc, chain, "custody chain") from exc
    folded = {**fold(rows), "chain_offset": offset}  # the boundary: what an old body appends later is P6' input
    owed = {task_id for task_id, document in records if document.get("delegated_runs_unreconciled")}
    receipts: Dict[str, Dict[str, Any]] = {}
    for rid, (facts, _hold) in folded["runs"].items():
        task_id = str(facts.get("task_id") or "")
        if facts.get("settled") and task_id in owed:
            receipts.setdefault(task_id, {"task_id": task_id, "closed_runs": {}})["closed_runs"][rid] = facts
    return folded, {"custody_open": folded["open"], "delegated_runs": receipts}


def _merge_receipts(held: Dict[str, Dict[str, Any]], folded: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    merged = {task_id: dict(facts) for task_id, facts in held.items()}
    for task_id, facts in folded.items():
        entry = merged.setdefault(task_id, {"task_id": task_id})
        entry["closed_runs"] = {**facts.get("closed_runs", {}), **entry.get("closed_runs", {})}
    return merged


def _admit(path: pathlib.Path, raw: bytes) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """``(document, reason, outcome)``: ``admitted``; ``quarantined`` (the file moved, bytes
    unchanged, through the existing quarantine); ``refused`` (left in place: a task-id
    mismatch, or a quarantine that could not move the file)."""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    refusal = task_result_schema_refusal(data)
    if (refusal == "unstamped_pre_7_0" and isinstance(data, dict) and data.get("status") == "cancel_requested"
            and data.get("task_id") == path.stem):
        # The one legacy carve-out: an admissible pre-7 cancel latch is stamped on import (same
        # status, same fields) so custody can drive it to its terminal; every other unstamped row quarantines.
        return stamp_task_result_schema(data), "latch_stamped", "admitted"
    if refusal:  # writers are quiescent: a row the quarantine finds admissible again stays refused here
        return None, refusal, "quarantined" if quarantine_task_result(path, refusal) == "moved" else "refused"
    if str(data.get("task_id") or "") != path.stem:
        return None, "task_id_mismatch", "refused"
    return data, "", "admitted"


def _baseline_entry(path: pathlib.Path, raw: bytes, document: Optional[Dict[str, Any]], reason: str = "") -> Dict[str, Any]:
    info = path.stat()
    entry = {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "sha256": sha256_hex(raw)}
    if document is None:
        return {**entry, "refused": reason}  # remembered, so an untouched refused file stays untouched
    if reason == "latch_stamped":
        entry["latch_stamped"] = True
    return {**entry, "body_sha256": sha256_hex(document_text(document).encode("utf-8"))}  # the whole projected document


def _read_result(path: pathlib.Path) -> Optional[bytes]:
    """The file's bytes, ``None`` when it is gone; a resource or I/O error is typed and ABORTS
    the import (an unreadable file is never a refused row, and nothing gets stamped)."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unreadable(exc, path) from exc


def _stat_result(path: pathlib.Path) -> Optional[Tuple[int, int]]:
    """``(size, mtime_ns)``, ``None`` when the file is gone; other errors typed as for a read."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _unreadable(exc, path) from exc
    return info.st_size, info.st_mtime_ns


def _unreadable(exc: OSError, path: pathlib.Path, what: str = "task result") -> store.StoreError:
    name = errno.errorcode.get(exc.errno, str(exc.errno))
    kind = store.StoreBusy if exc.errno in (errno.EMFILE, errno.ENFILE) else store.StoreError
    return kind(f"{what} not readable ({name}), import aborted: {path}")


def _read_results(root: pathlib.Path):
    records: List[Record] = []
    baseline: Dict[str, Dict[str, Any]] = {}
    quarantined: List[Dict[str, str]] = []
    refused: Dict[str, str] = {}
    for path in sorted(task_results_dir(root, create=False).glob("*.json")):
        raw = _read_result(path)
        if raw is None:
            continue
        document, reason, outcome = _admit(path, raw)
        if outcome == "quarantined":
            quarantined.append({"task_id": path.stem, "reason": reason})
            continue
        if outcome == "refused":
            refused[path.stem] = reason
        else:
            records.append((path.stem, document))
        baseline[path.stem] = _baseline_entry(path, raw, document, reason)
    return records, baseline, quarantined, refused


def set_file_path(root: pathlib.Path, name: str) -> pathlib.Path:
    """The old body's set file ``state/obligations/<name>.json``: the importer's input, the exporter's output."""
    return pathlib.Path(root) / "state" / "obligations" / f"{name}.json"


def _read_sets(root: pathlib.Path) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """One complete snapshot: missing files are absent; unreadable or malformed files abort
    typed, leaving their bytes in place and published rows intact for a later retry."""
    sets: Dict[str, Dict[str, Dict[str, Any]]] = {}
    directory = set_file_path(root, "nonterminal").parent
    try:
        with os.scandir(directory) as entries:  # Path.glob can suppress directory read errors
            paths = sorted(pathlib.Path(entry.path) for entry in entries if entry.name.endswith(".json"))
    except FileNotFoundError:
        return sets
    except OSError as exc:
        raise _unreadable(exc, directory, "obligation sets") from exc
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not all(isinstance(row, dict) for row in value.values()):
                raise ValueError("expected an id-to-facts object")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _unreadable(exc, path, "obligation set") from exc
        except ValueError as exc:
            raise store.StoreError(f"obligation set malformed, import aborted: {path}") from exc
        sets[path.stem] = value
    return sets


OCCUPIED = "occupied"  # meta: the identities whose file the import refused or quarantined (ABI-2: not readmitted)


def _quarantined_occupied(root: pathlib.Path, admitted: List[str]) -> Dict[str, str]:
    """Recover reservations after a quarantine rename outlived an interrupted import/transaction.
    Admitted rows win; deletion backups are evidence of retirement, not refused admission."""
    directory = task_results_dir(root, create=False) / TASK_RESULT_QUARANTINE_DIR
    try:
        with os.scandir(directory) as entries:
            identities = {re.sub(r"\.[0-9a-f]{8}$", "", pathlib.Path(entry.name).stem)
                          for entry in entries if entry.name.endswith(".json") and not re.search(r"\.deleted-\d{8}T\d{6}Z\.json$", entry.name)}
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise _unreadable(exc, directory, "task result quarantine") from exc
    return {task_id: "quarantined_before_import" for task_id in identities - set(admitted)}


def occupied_reason(root: pathlib.Path, task_id: str, *, write: bool = False) -> str:
    """Why TASK_ID's identity is occupied without a row (its file was refused or quarantined by
    the import or a P6'), ``""`` for a free identity: what a strict read or a create-only write
    (``write``: through the write gate, so a first touch imports first) consults instead of
    scanning the quarantine directory."""
    with store.open_store(root, write=write) as txn:
        return str((txn.meta(OCCUPIED) or {}).get(str(task_id)) or "")


def holds_facts(root: pathlib.Path) -> bool:
    """Whether ROOT holds facts the importer would read: result files or obligation set files."""
    for directory in (task_results_dir(root, create=False), set_file_path(root, "nonterminal").parent):
        try:
            with os.scandir(directory) as entries:
                if any(entry.name.endswith(".json") for entry in entries):
                    return True
        except OSError:
            continue
    return False


def _sets_baseline(files: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, str]]:
    """The owner-held rows of every set file as last seen, by content digest: what P6' compares a
    re-read file against so that only the rows the old body added, changed or retired move."""
    return {name: {identity: sha256_hex(machine_json(facts).encode("utf-8")) for identity, facts in held.items()}
            for name, rows in files.items() for held in [o.owner_held_rows(name, rows)]}


def _build(tmp: pathlib.Path, tier: str, root: pathlib.Path, records: List[Record], sets: Dict[str, Any],
           baseline: Dict[str, Any], provenance: Dict[str, Any], folded: Optional[Dict[str, Any]] = None, *,
           sets_baseline: Optional[Dict[str, Any]] = None, chain_offset: int = 0,
           occupied: Optional[Dict[str, str]] = None) -> None:
    from ouroboros.delegate_custody_current import CHAIN_BOUNDARY, write_fold

    with contextlib.closing(store.connect(tmp, tier, create=True)) as conn:
        conn.execute("PRAGMA synchronous = FULL")
        conn.executescript(store._SCHEMA)
        conn.execute(f"PRAGMA application_id = {store._APPLICATION_ID[tier]}")
        conn.execute("BEGIN IMMEDIATE")
        txn = Txn(conn, root)
        for task_id, document in records:
            txn.store_task(document, pending_drive=bool(o.pending_drive(root, task_id, document)))
        for name, rows in sets.items():
            txn.replace_set(name, rows)
        if folded:
            write_fold(txn, folded, replace=False)  # the sets above already hold the fold's open rows
        for key, value in (("schema_version", store.SCHEMA_VERSION), ("lock_tier", tier), ("generation", GENERATION),
                           ("phase", PUBLISHED), ("import", provenance), ("freeze_utc", utc_now_iso()),
                           ("baseline", baseline), ("sets_baseline", sets_baseline or {}), (CHAIN_BOUNDARY, chain_offset),
                           (OCCUPIED, occupied or {})):
            txn.set_meta(key, value)
        store.commit(conn)


def _quarantine_copy(root: pathlib.Path, task_id: str, body: str) -> None:
    """The DB version of a row whose file an old body deleted: kept beside the quarantined bytes."""
    directory = task_results_dir(root) / TASK_RESULT_QUARANTINE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    write_text_atomic(directory / f"{task_id}.deleted-{utc_stamp()}.json", body, fsync=True)


def _reconcile(root: pathlib.Path, txn: Txn) -> Tuple[Dict[str, Any], list]:
    """P6' per-row reconciliation; stat prefilter and same-stat residual: module docstring.
    Stat-changed bytes are hashed; a matching digest refreshes only the stored stat."""
    baseline: Dict[str, Dict[str, Any]] = txn.meta("baseline") or {}
    present: Dict[str, Tuple[pathlib.Path, Optional[bytes]]] = {}
    changed: List[str] = []
    for path in sorted(task_results_dir(root, create=False).glob("*.json")):
        entry = baseline.get(path.stem)
        stat = _stat_result(path)
        if stat is None:
            continue
        if entry is not None and stat == (entry["size"], entry["mtime_ns"]):
            present[path.stem] = (path, None)  # untouched: not read
            continue
        raw = _read_result(path)
        if raw is None:
            continue
        present[path.stem] = (path, raw)
        if entry is None:
            continue  # new: imported below
        if sha256_hex(raw) != entry["sha256"]:
            changed.append(path.stem)
        else:
            baseline[path.stem] = {**entry, "size": stat[0], "mtime_ns": stat[1]}  # touched, same bytes
    fresh = sorted(task_id for task_id in present if task_id not in baseline)
    deleted = sorted(task_id for task_id in baseline if task_id not in present)
    counts = {"changed": len(changed), "new": len(fresh), "deleted": len(deleted), "results": len(baseline)}
    if not (changed or fresh or deleted):
        txn.set_meta("baseline", baseline)
        txn.set_meta(OCCUPIED, {**_quarantined_occupied(root, txn.task_ids()), **(txn.meta(OCCUPIED) or {})})
        return {"status": "unchanged", **counts}, []
    sets = _read_sets(root)  # validate before any result is moved into quarantine
    now = utc_now_iso()
    quarantined: List[Dict[str, str]] = []
    refused: Dict[str, str] = {}
    latched: List[str] = []
    conflicts: Dict[str, Any] = txn.meta("conflicts") or {}
    occupied: Dict[str, str] = txn.meta(OCCUPIED) or {}
    for task_id in changed + fresh:  # the old body was the authority while it ran: its files win
        path, raw = present[task_id]
        document, reason, outcome = _admit(path, raw)
        if outcome == "quarantined":  # the file left the directory with its bytes; the row goes with it
            quarantined.append({"task_id": task_id, "reason": reason})
            occupied[task_id] = reason
            txn.delete_task(task_id)
            baseline.pop(task_id, None)
            continue
        if outcome == "refused":
            refused[task_id] = occupied[task_id] = reason
            baseline[task_id] = _baseline_entry(path, raw, None, reason)
            continue
        occupied.pop(task_id, None)  # an admissible file re-opens the identity
        if reason == "latch_stamped":
            latched.append(task_id)
        body = _projected(txn, task_id)  # the store's version WITH its handoff rows (B5)
        if body is not None and sha256_hex(body.encode("utf-8")) != baseline.get(task_id, {}).get("body_sha256"):
            conflicts[task_id] = {"at": now, "db_body": body, "file_sha256": sha256_hex(raw)}
        txn.store_task(document, pending_drive=bool(o.pending_drive(root, task_id, document)))
        baseline[task_id] = _baseline_entry(path, raw, document, reason)
    for task_id in deleted:
        body = _projected(txn, task_id)
        if body is not None:
            _quarantine_copy(root, task_id, body)  # saved before the task and its handoffs go
            quarantined.append({"task_id": task_id, "reason": "deleted_by_old_body"})
        txn.delete_task(task_id)
        baseline.pop(task_id, None)
    moved = _reconcile_sets(txn, sets)  # the old body maintained the set files too
    legacy = _apply_legacy_custody(root, txn)
    occupied = {**_quarantined_occupied(root, txn.task_ids()), **occupied}
    for key, value in (("baseline", baseline), ("freeze_utc", now), ("conflicts", conflicts), (OCCUPIED, occupied),
                       ("reconciled", {"at": now, **counts, "quarantined": len(quarantined), "refused": refused,
                                       "conflicts": len(conflicts), "set_rows": moved, "legacy_custody": legacy})):
        txn.set_meta(key, value)
    return {"status": "reconciled", **counts, "quarantined": len(quarantined), "refused": len(refused),
            "conflicts": len(conflicts), "latched": latched, "set_rows": moved, "legacy_custody": legacy}, quarantined


def _projected(txn: Txn, task_id: str) -> Optional[str]:
    """The store's whole document (body plus handoff rows) as a file would hold it."""
    document = txn.load(task_id)
    return None if document is None else document_text(document)


def _reconcile_sets(txn: Txn, files: Dict[str, Dict[str, Dict[str, Any]]]) -> int:
    """Owner-held rows against their own baseline (B4): a row the old body added or changed in a
    set file is put, one it retired is deleted, and every row the file left as it was keeps the
    store's version — the facts this store published since (its rows, its retirements) survive
    a P6' instead of being replaced by a stale file. Returns the rows moved."""
    baseline: Dict[str, Dict[str, str]] = txn.meta("sets_baseline") or {}
    current = _sets_baseline(files)
    moved = 0
    for name in sorted(set(current) | set(baseline)):
        held, before, after = o.owner_held_rows(name, files.get(name, {})), baseline.get(name, {}), current.get(name, {})
        for identity in sorted(set(before) | set(after)):
            if before.get(identity) == after.get(identity):
                continue  # untouched by the old body: the store's own answer stands
            if identity in after:
                txn.put_row(name, identity, held[identity])
            else:
                txn.delete_row(name, identity)
            moved += 1
    txn.set_meta("sets_baseline", current)
    return moved


def _apply_legacy_custody(root: pathlib.Path, txn: Txn) -> int:
    """The custody events an old body appended to the chain after this store's boundary (its
    import, its own last row) are reduced onto the rows — the reducer's event-id dedup and
    idempotent replays included — and the boundary moves to the chain's end (B4)."""
    from ouroboros import delegate_custody as c
    from ouroboros.delegate_custody_current import CHAIN_BOUNDARY, _loaded, _reduce

    chain = c.event_log_path(root)
    try:
        if c.custody_chain_unreadable(root):
            raise OSError(errno.EIO, "custody chain exists but cannot be opened")
        rows, offset = c.rows_after(chain, int(txn.meta(CHAIN_BOUNDARY) or 0))
    except OSError as exc:
        raise _unreadable(exc, chain, "custody chain") from exc
    applied = 0
    for row in c._unique_rows(rows):
        _reduce(txn, row, _loaded(txn, row), {})
        applied += 1
    txn.set_meta(CHAIN_BOUNDARY, offset)
    return applied


def _identity(text: str) -> Dict[str, Any]:
    data = text.encode("utf-8")
    return {"size": len(data), "sha256": sha256_hex(data)}


def export_facts(root: Any) -> Dict[str, Any]:
    """The exact inverse of the import, in two transactions around the writes.

    T1 commits ``meta.export_manifest``: the name, size and sha256 of every file the export
    will write (every row's whole ``load`` document to ``task_results/<id>.json``, every
    obligation set file in FULL) and the stale result files it removes (admissible files
    whose row the store no longer holds; refused files and the quarantine stay as evidence).
    Then the files: tmp + rename, a byte-identical file left alone, fsync of files and
    directories. T2 refreshes the content baseline to what was written and completes the
    manifest. A death in between leaves a manifest ``absorb_own_export`` reads at
    abandonment, so the store's own partial writes are never mistaken for an old body's."""
    root = pathlib.Path(root)
    names: List[str] = []
    with store.open_store(root, write=True, service=True) as txn:  # T1: the plan, durable before the first write
        baseline = txn.meta("baseline") or {}
        task_ids = txn.task_ids()
        names = sorted(set(txn.set_names()) | set(o.BOOT_SETS))  # an older body expects every set file
        stale = sorted(path.stem for path in task_results_dir(root, create=False).glob("*.json")
                       if path.stem not in task_ids and not (baseline.get(path.stem) or {}).get("refused"))
        manifest = {"status": "writing", "started_at": utc_now_iso(),
                    "results": {task_id: _identity(document_text(txn.load(task_id))) for task_id in task_ids},
                    "sets": {name: _identity(machine_json(o.members_in(txn, name)))  # the set file in FULL, as atomic_write_json writes it
                             for name in names},
                    "removed": stale}
        txn.set_meta("export_manifest", manifest)
    written = total = 0
    entries: Dict[str, Dict[str, Any]] = {}
    with store.open_store(root, write=False) as txn:  # the frozen rows, streamed to their files
        for task_id in task_ids:
            document = txn.load(task_id)
            text = document_text(document)
            data = text.encode("utf-8")
            path = task_result_path(root, task_id)
            if _read_result(path) != data:  # a read error aborts the export typed, never «rewrite blindly»
                write_text_atomic(path, text, fsync=True)
                written += 1
            total += len(data)
            entries[task_id] = _baseline_entry(path, data, document)
        for name in names:
            atomic_write_json(set_file_path(root, name), o.members_in(txn, name), fsync=True)
    for task_id in stale:
        with contextlib.suppress(FileNotFoundError):
            task_result_path(root, task_id).unlink()
    store.fsync_directory(task_results_dir(root, create=False))
    store.fsync_directory(set_file_path(root, "nonterminal").parent)
    with store.open_store(root, write=True, service=True) as txn:  # T2: the baseline follows what was written
        refused = {task_id: entry for task_id, entry in (txn.meta("baseline") or {}).items() if entry.get("refused")}
        txn.set_meta("baseline", {**refused, **entries})
        txn.set_meta("sets_baseline", _sets_baseline(_read_sets(root)))
        txn.set_meta("export_manifest", {**manifest, "status": "completed", "completed_at": utc_now_iso()})
    return {"results": len(task_ids), "written": written, "removed": len(stale), "bytes": total, "sets": len(names)}


def absorb_own_export(root: Any, txn: Txn) -> Dict[str, Any]:
    """At abandonment, adopt manifest-matching (size + sha256) files and stale deletions into the
    baseline while republishing the store. Its partial export then triggers no old-body
    re-import at boot, preserving every row, including owner-held sets."""
    root = pathlib.Path(root)
    manifest = txn.meta("export_manifest") or {}
    if manifest.get("status") != "writing":
        return {}
    baseline = txn.meta("baseline") or {}
    adopted = 0
    for task_id, identity in manifest.get("results", {}).items():
        path = task_result_path(root, task_id)
        raw = _read_result(path)
        if raw is not None and len(raw) == identity["size"] and sha256_hex(raw) == identity["sha256"]:
            baseline[task_id] = _baseline_entry(path, raw, txn.load(task_id))
            adopted += 1
    for task_id in manifest.get("removed", []):
        if _read_result(task_result_path(root, task_id)) is None:
            baseline.pop(task_id, None)
    txn.set_meta("baseline", baseline)
    files = _read_sets(root)
    sets_baseline = dict(txn.meta("sets_baseline") or {})
    for name, identity in manifest.get("sets", {}).items():  # a set file this export wrote is its own, not an old body's
        if name in files and _identity(machine_json(files[name])) == identity:
            sets_baseline[name] = _sets_baseline({name: files[name]})[name]
    txn.set_meta("sets_baseline", sets_baseline)
    txn.set_meta("export_manifest", {**manifest, "status": "abandoned", "adopted": adopted, "at": utc_now_iso()})
    return {"adopted": adopted, "planned": len(manifest.get("results", {}))}
