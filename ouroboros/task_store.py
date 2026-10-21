"""Task result documents in the current-facts store: the primitives every consumer uses.

Readers select typed facts before decoding only the bodies they need, after closing
the transaction. ``patch_document`` owns document updates; ``patch_handoff`` /
``put_handoffs`` write one operation row of the ``store.ROW_BACKED_FIELDS``
projections (a tool claim, a model wait, a retired consumer) without a body write;
``load`` projects the rows back.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ouroboros import store, store_task_cards
from ouroboros.store import Txn, open_store

FACT_KEYS = ("task_id", "status", "ts", "updated_at", "delegation_role", "parent_task_id", "project_id", "root_task_id",
             "child_drive_root", "headless_child_drive_root", "superseded_by", "retry_task_id",
             "presence_binding_id", "presence_authority_recorded")
_NEWEST_ORDER = "ORDER BY ts DESC, task_id DESC"  # NULL ts sorts last: a row without one is oldest


def read_document(root: Any, task_id: str, *, txn: Optional[Txn] = None, signed: bool = False) -> Any:
    """One coherent document; implicit reads decode only after releasing the store.

    A supplied transaction keeps its own read/modify/write snapshot and lifetime.
    ``signed=True`` returns ``(document, signature)`` captured in that same read
    transaction, so an ownership check compares the stamp of the body it holds and
    never a second read's.
    """
    if txn is not None:
        document = txn.load(str(task_id))
        return (document, signature(root, task_id, txn=txn)) if signed else document
    task_id = str(task_id)
    with open_store(root, write=False) as txn:
        stamp = signature(root, task_id, txn=txn) if signed else None
        raw = txn.raw(task_id)
    document = store.materialize(task_id, *raw)
    return (document, stamp) if signed else document


def exists(root: Any, task_id: str) -> bool:
    with open_store(root, write=False) as txn:
        return txn.row(str(task_id), "task_id") is not None


def signature(root: Any, task_id: str, *, txn: Optional[Txn] = None) -> Optional[Tuple[int, str]]:
    """``(write_seq, updated_at)``: changes on every write; ``None`` for an absent task. The key
    the memos compare that used to compare a result file's stat."""
    if txn is not None:
        return txn.row(str(task_id), "write_seq", "updated_at")
    with open_store(root, write=False) as txn:
        return signature(root, task_id, txn=txn)


def delete_document(root: Any, task_id: str) -> bool:
    """Remove one task's row and body (a scheduling refusal undoing its own requested row)."""
    with open_store(root, write=True) as txn:
        present = txn.row(str(task_id), "task_id") is not None
        txn.delete_task(str(task_id))
        return present


def patch_document(root: Any, task_id: str, mutator: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Mutate CURRENT in one write; None writes nothing. Return the current document."""
    from ouroboros.obligations import pending_drive_rule

    task_id = str(task_id)
    seen: Dict[str, Any] = {}

    def _mutate(current: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        seen["current"] = current
        return mutator(current)

    updated = store.update_task(root, task_id, _mutate, pending_drive=pending_drive_rule)
    return seen.get("current", {}) if updated is None else {**updated, "task_id": task_id}


def _fetch(txn: Txn, where: str = "", params: Iterable[Any] = (), order: str = "ORDER BY t.task_id") -> Tuple[list, list]:
    """The matching rows and their handoff rows as stored (text): all the store is held for.
    A reader decodes AFTER its transaction closed (``_decode``), so a writer's COMMIT never
    waits behind a listing's ``json.loads``."""
    clause = f"WHERE {where}" if where else ""
    cards, join = store_task_cards.select(txn)
    rows = txn.conn.execute(
        f"SELECT b.body, t.*{cards} FROM tasks AS t JOIN task_bodies AS b ON b.task_id = t.task_id{join} {clause} {order}",
        tuple(params)).fetchall()
    return rows, txn.handoff_records([record["task_id"] for record in rows])


def _decode(rows: list, handoff_records: list) -> List[Dict[str, Any]]:
    """The documents of ``_fetch``'s rows: each row carries its body and its card columns."""
    handoffs = store.decode_handoffs([record["task_id"] for record in rows], handoff_records)
    return [store_task_cards.project(store.project_handoffs(json.loads(record["body"]), handoffs[record["task_id"]]), record)
            for record in rows]


class Reprepare(Exception):
    """A mutator's answer when the write needs an input it may only prepare OUTSIDE the lock
    (a mailbox read, an event-chain snapshot, an artifact write): the transaction rolls back
    empty and ``prepared_write`` repeats the whole write with that input prepared first."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def prepared_write(write: Callable[[set], Any]) -> Any:
    """Run ``write(needs)`` until its mutator stops asking: every ``Reprepare`` adds its name to
    ``needs`` and the next round prepares it before ``BEGIN``. No I/O happens under the lock."""
    needs: set = set()
    for _round in range(4):
        try:
            return write(needs)
        except Reprepare as exc:
            needs.add(exc.what)
    return write(needs)


DELETE_ROW = object()  # a handoff mutator's answer: drop the row


def patch_handoff(root: Any, task_id: str, kind: str, op_id: str,
                  mutator: Callable[[Optional[Dict[str, Any]]], Any]) -> Optional[Dict[str, Any]]:
    """One operation row of a task's projection ``kind`` (a ``store.ROW_BACKED_FIELDS`` name) in
    its own write transaction, no body write: the mutator sees the current row (``None`` when
    absent) and returns the new row, ``None`` to leave the store untouched, or ``DELETE_ROW``.
    ``LookupError`` when the task has no lifecycle row: a projection needs its owner. Returns the
    row that is current when the transaction ends."""
    task_id, op_id = str(task_id), str(op_id)
    with open_store(root, write=True) as txn:
        if not txn.row(task_id, "status"):
            raise LookupError(f"task {task_id} has no lifecycle row")
        current = txn.handoff(task_id, op_id)
        updated = mutator(json.loads(json.dumps(current)) if current is not None else None)
        if updated is None:
            return current
        if updated is DELETE_ROW:
            txn.delete_handoff(task_id, op_id)
        else:
            txn.put_handoff(task_id, kind, op_id, updated)
        txn.touch(task_id)
        return None if updated is DELETE_ROW else updated


def put_handoffs(root: Any, task_id: str, kind: str, rows: Dict[str, Any]) -> None:
    """Upsert several operation rows of one projection in one transaction (``LookupError`` as above)."""
    task_id = str(task_id)
    with open_store(root, write=True) as txn:
        if not txn.row(task_id, "status"):
            raise LookupError(f"task {task_id} has no lifecycle row")
        for op_id, facts in rows.items():
            txn.put_handoff(task_id, kind, str(op_id), facts)
        txn.touch(task_id)


def list_documents(root: Any, *, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """Every document, or those whose stored status is one of ``statuses`` (the WHERE prefilter;
    a caller filtering on the EFFECTIVE status still filters the overlaid rows itself)."""
    wanted = None if statuses is None else sorted({str(status) for status in statuses})
    if wanted is not None and not wanted:
        return []
    with open_store(root, write=False) as txn:
        fetched = _fetch(txn) if wanted is None else _fetch(txn, f"t.status IN ({','.join('?' * len(wanted))})", wanted)
    return _decode(*fetched)


def lineage_where(parent: str, root_id: str, *, direct_only: bool,
                  queue_members: Iterable[str] = ()) -> Tuple[str, Tuple[str, ...]]:
    """The ``find_child_tasks`` prefilter as SQL, starting from this parent's children. Anchors: the
    rows naming PARENT as their parent or (subtree scope) ROOT_ID as their root, any role, and the
    QUEUE_MEMBERS (queued items naming either, whose parent or role may live only in the queue),
    bound as ONE JSON array: the statement's variables never grow with the queue.
    Their lineage closes over ``superseded_by``/``retry_task_id`` in both directions to a fixed
    point. Selected from that closure: the rows the projection can fold onto a child (a retry or
    supersession pointer, or a ``subagent`` row naming the parent/root). The typed pointers are the
    body's own (``store.typed_columns``), so a predecessor whose chain ends on a child is reached
    however long the chain, and unrelated retry history is never read."""
    kin = [(column, value) for column, value in (("parent_task_id", parent),
                                                ("root_task_id", "" if direct_only else root_id)) if value]
    if not kin:
        return "0", ()
    values = tuple(value for _column, value in kin)
    members = sorted({str(member) for member in queue_members if str(member or "").strip()})
    seeds = (json.dumps(members),) if members else ()
    anchors = " OR ".join([f"{column}=?" for column, _value in kin]
                          + ["task_id IN (SELECT value FROM json_each(?))"] * len(seeds))
    reach = (f"WITH RECURSIVE reach(id, successor, retry) AS (SELECT task_id, superseded_by, retry_task_id "
             f"FROM tasks WHERE {anchors} UNION SELECT tasks.task_id, tasks.superseded_by, tasks.retry_task_id "
             "FROM reach JOIN tasks ON tasks.superseded_by=reach.id OR tasks.retry_task_id=reach.id "
             "OR tasks.task_id=reach.successor OR tasks.task_id=reach.retry) SELECT id FROM reach")
    child = " OR ".join(f"t.{column}=?" for column, _value in kin)
    return (f"t.task_id IN ({reach}) AND (t.superseded_by IS NOT NULL OR t.retry_task_id IS NOT NULL "
            f"OR (t.delegation_role='subagent' AND ({child})))", (*values, *seeds, *values))


def select_lineage(root: Any, parent: str, root_id: str, *, direct_only: bool = False,
                   queue_members: Iterable[str] = ()) -> List[Dict[str, Any]]:
    """The child-lookup candidates in ``task_id`` order (``lineage_where``); the Python projection decides the rest."""
    where, params = lineage_where(str(parent or ""), str(root_id or ""), direct_only=direct_only,
                                  queue_members=queue_members)
    with open_store(root, write=False) as txn:
        fetched = _fetch(txn, where, params)
    return _decode(*fetched)


def task_facts(root: Any, *, where: str = "", params: Iterable[Any] = ()) -> Dict[str, Dict[str, Any]]:
    """Typed facts of every task (keyed by task id) without reading a body: the keys
    ``FACT_KEYS`` as strings (``""`` for no fact), ``presence_authority_recorded`` as a bool."""
    clause = f"WHERE {where}" if where else ""
    with open_store(root, write=False) as txn:
        records = txn.conn.execute(
            f"SELECT {','.join(FACT_KEYS)} FROM tasks {clause} ORDER BY task_id", tuple(params)).fetchall()
    facts = {}
    for record in records:
        row = {key: str(record[key] or "") for key in FACT_KEYS if key != "presence_authority_recorded"}
        row["presence_authority_recorded"] = bool(record["presence_authority_recorded"])
        facts[row["task_id"]] = row
    return facts


def newest_ids(root: Any, limit: Optional[int] = None, *, where: str = "", params: Iterable[Any] = ()) -> List[str]:
    """Task ids newest first by the stored ``ts`` (a row without one sorts oldest), ties by id."""
    clause = f"WHERE {where} " if where else ""
    with open_store(root, write=False) as txn:
        bound = "" if limit is None else f" LIMIT {int(limit)}"
        return [record["task_id"] for record in
                txn.conn.execute(f"SELECT task_id FROM tasks {clause}{_NEWEST_ORDER}{bound}", tuple(params))]


ROOT_RESULT_WHERE = "parent_task_id IS NULL AND (delegation_role IS NULL OR delegation_role != 'subagent')"


def documents_by_ids(root: Any, task_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    ids = [str(task_id) for task_id in task_ids]
    if not ids:
        return {}
    with open_store(root, write=False) as txn:
        fetched = _fetch(txn, f"t.task_id IN ({','.join('?' * len(ids))})", ids)
    return {str(document.get("task_id")): document for document in _decode(*fetched)}


# A display read (``task_reads``: history cards; ``effective_task_result`` with
# ``materialize_artifacts=False``, retry successors and child replicas included) takes every
# stored key except the bulky evidence no such reader uses — 94 MB of heads for 1.46 GB of
# bodies on a long-lived install. A DROPPED key goes; a KEPT key keeps only the named sub-keys
# it is read through; every other key, a new fact included, arrives as stored. The evidence
# goes to task detail, Continue and writers (cost planes, verification, plan/review state,
# traces, artifacts — whose materialized view is the ``materialize_artifacts=True`` read).
# A display reader that needs a dropped path names it here; ``tests/test_task_reads.py``
# builds a page from display views and from full documents and requires the same page.
DISPLAY_DROPPED: Tuple[str, ...] = (
    "loop_outcome", "trace_refs", "review_evidence", "artifacts", "result", "trace_summary",
    "governing_owner_words", "verification_ledger", "plan_review_state", "acceptance_debt",
    "mutation_evidence", "configured_subagent", "context", "continuation_narrative", "origin_message_text",
    "owner_mailbox", "subagent_availability", "plan_record", "review_status", "schedule_admission",
    "task_acceptance_review_accounting",
    *(f"metadata.{key}" for key in (
        "task_contract", "main_routing_manifest", "governing_owner_words", "routing_contract",
        "configured_subagent", "continuation", "project_routing_manifest", "subagent_envelope", "owner_corpus",
        "origin_message_text", "wake_observation", "project_last_task_result", "current_chat",
        "workspace_preflight", "workspace_copy", "late_answer")),
)
DISPLAY_KEPT: Dict[str, Tuple[str, ...]] = {"artifact_bundle": ("status",), "task_contract": ("deadline_at",)}
_KEEP = "__keep__"  # a path no kept object holds: the no-op target of a conditional edit


def _display_sql(column: str = "b.body") -> str:
    """One stored body's display view in SQLite JSON1 functions of the store's SQLite floor (no
    ``->``): presence, booleans and nulls are kept exactly, a non-object KEPT value as stored."""
    def path(*parts: str) -> str:
        return "'$." + ".".join(parts) + "'"

    view = (f"json_remove({column}, {', '.join(path(*key.split('.')) for key in DISPLAY_DROPPED)})"
            if DISPLAY_DROPPED else f"json({column})")
    for key, kept in DISPLAY_KEPT.items():
        is_object = f"json_type({column}, {path(key)}) = 'object'"
        values = [f"CASE json_type({column}, {path(key, sub)}) WHEN 'true' THEN json('true') WHEN 'false' "
                  f"THEN json('false') WHEN 'null' THEN json('null') ELSE json_extract({column}, {path(key, sub)}) END"
                  for sub in kept]
        built = "json_object(" + ", ".join(f"'{sub}', {value}" for sub, value in zip(kept, values)) + ")"
        absent = ", ".join(f"CASE WHEN json_type({column}, {path(key, sub)}) IS NULL THEN {path(sub)} "
                           f"ELSE {path(_KEEP)} END" for sub in kept)
        view = (f"json_replace({view}, CASE WHEN {is_object} THEN {path(key)} ELSE {path(key, _KEEP)} END, "
                f"CASE WHEN {is_object} THEN json_remove({built}, {absent}) END)")
    return view


def _card_sql(view: str) -> str:
    """``view`` with the task's card row merged over it (``store_task_cards.project`` in SQL, one
    ``json_patch`` per carded row): the row's non-NULL facts replace the view's, in place or appended."""
    patch = ("json_remove(json_object(" + ", ".join(f"'{name}', c.{name}" for name in store_task_cards.FIELDS) + "), "
             + ", ".join(f"CASE WHEN c.{name} IS NULL THEN '$.{name}' ELSE '$.held' END" for name in store_task_cards.FIELDS) + ")")
    return f"CASE WHEN c.task_id IS NULL THEN {view} ELSE json_patch({view}, {patch}) END"


def display_documents(root: Any, task_ids: Iterable[str],
                      versions: Optional[Dict[str, int]] = None) -> Dict[str, str]:
    """The display views (``DISPLAY_DROPPED``) of several documents as JSON text, row-backed
    fields projected: ONE aggregated row in ONE short read, everything else after it closes.
    A page of tasks costs a few interpreter hand-offs and kilobytes, not a row step per task and
    the megabytes of evidence; each caller decodes its own private copy of a text. ``versions``
    receives each view's ``write_seq`` from the same read."""
    ids = sorted({str(task_id) for task_id in task_ids if str(task_id or "").strip()})
    if not ids:
        return {}
    with open_store(root, write=False) as txn:
        _columns, join = store_task_cards.select(txn)
        view = _card_sql(_display_sql()) if join else _display_sql()
        record = txn.conn.execute(  # '' || ...: the view travels as a string, decoded per use
            f"SELECT json_group_array(json_array(t.task_id, '' || {view}, t.write_seq)) FROM tasks AS t "
            f"JOIN task_bodies AS b ON b.task_id = t.task_id{join} WHERE t.task_id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),)).fetchone()
        handoff_rows = txn.handoff_records(ids)
    handoffs = store.decode_handoffs(ids, handoff_rows)
    held = {record["task_id"] for record in handoff_rows}
    found = json.loads(record[0] or "[]")
    if versions is not None:
        versions.update((task_id, int(seq or 0)) for task_id, _text, seq in found)
    return {task_id: (text if task_id not in held else
                      store.document_text(store.project_handoffs(json.loads(text), handoffs[task_id])))
            for task_id, text, _seq in found}


def task_versions(root: Any, task_ids: Iterable[str]) -> Dict[str, Tuple[int, str, bool]]:
    """Each stored task's ``(write_seq, status, owes_synthesis)``, typed columns only, in ONE
    aggregated row: whether documents a window shows changed since it read them, and which of them
    are settled now. An id the store does not hold is absent from the answer."""
    ids = sorted({str(task_id) for task_id in task_ids if str(task_id or "").strip()})
    if not ids:
        return {}
    with open_store(root, write=False) as txn:
        record = txn.conn.execute(
            "SELECT json_group_array(json_array(task_id, write_seq, status, owes_synthesis)) FROM tasks "
            "WHERE task_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)).fetchone()
    return {task_id: (int(seq or 0), str(status or ""), bool(owes))
            for task_id, seq, status, owes in json.loads(record[0] or "[]")}


def task_ids(root: Any) -> List[str]:
    with open_store(root, write=False) as txn:
        return txn.task_ids()


def tree_facts(root: Any, tree_id: str, *, descendants: bool = False,
               queue_rows: Iterable[dict] = ()) -> Dict[str, dict]:
    """Typed membership; only underived roots need their legacy body.

    Descendant selection closes over the indexed parent chain, including bridges
    present only in the queue snapshot. Durable rows win over queue rows. The
    caller still applies its own depth/ancestry rule to this selected superset.
    """
    from ouroboros.store_hot_facts import task_columns

    tree_id = str(tree_id)
    with open_store(root, write=False) as txn:
        legacy = {}
        for record in txn.conn.execute(
                "SELECT t.task_id,t.status,t.parent_task_id,b.body FROM tasks AS t "
                "JOIN task_bodies AS b ON b.task_id=t.task_id WHERE t.root_task_id IS NULL"):
            document = json.loads(record["body"])
            legacy[record["task_id"]] = {"task_id": record["task_id"], "status": record["status"],
                "parent_task_id": record["parent_task_id"], "root_task_id": task_columns(document)["root_task_id"]}
        selected = {row["task_id"]: dict(row) for row in txn.conn.execute(
            "SELECT task_id,status,parent_task_id,root_task_id FROM tasks WHERE root_task_id=?", (tree_id,))}
        selected.update({tid: row for tid, row in legacy.items() if row["root_task_id"] == tree_id})
        if not descendants:
            return selected
        queued = {}
        for row in queue_rows:
            tid = str(row.get("task_id") or row.get("id") or "")
            if tid:
                queued.setdefault(tid, row)
        # A queue record cannot replace a durable row, even when that row is
        # outside the selected tree. This addressed read preserves that rule.
        durable_queue = {row["task_id"]: dict(row) for row in txn.conn.execute(
            "SELECT task_id,status,parent_task_id,root_task_id FROM tasks "
            "WHERE task_id IN (SELECT value FROM json_each(?))", (json.dumps(list(queued)),))}
        queued.update({tid: legacy.get(tid, row) for tid, row in durable_queue.items()})
        anchors = {tree_id, *selected}
        while True:
            rows = txn.conn.execute(
                "WITH RECURSIVE reach(id) AS (SELECT value FROM json_each(?) UNION "
                "SELECT t.task_id FROM tasks AS t JOIN reach ON t.parent_task_id=reach.id) "
                "SELECT task_id,status,parent_task_id,root_task_id FROM tasks "
                "WHERE task_id IN (SELECT id FROM reach)", (json.dumps(sorted(anchors)),)).fetchall()
            selected.update({row["task_id"]: legacy.get(row["task_id"], dict(row)) for row in rows})
            reached = {tree_id, *selected}
            while True:
                extra = {tid for tid, row in queued.items() if tid not in reached and (
                    str(row.get("root_task_id") or "") == tree_id or str(row.get("parent_task_id") or "") in reached)}
                if not extra:
                    break
                reached.update(extra)
            new = reached - anchors - selected.keys()
            selected.update({tid: row for tid, row in queued.items() if tid in reached and tid not in selected})
            if not new:
                return selected
            anchors.update(reached)


_COLLECTION_BATCH = 64  # bounds selection/materialization and one classification commit
DRIVE_RETENTION_BATCH = 64  # bounds directory-name selection and one classification commit


def drive_retention_facts(root: Any, task_ids: Iterable[str], *,
                          stop: Optional[Callable[[], bool]] = None, record: bool = True) -> Dict[str, Dict[str, Any]]:
    """Cheap facts for one bounded batch of drive names, never a scan of task history.

    A missing row/body or NULL projection stays a strict-read candidate. Legacy
    classification reads coherent bodies outside the selector transaction and
    commits only still-NULL write_seq matches, without changing bodies/signatures.
    An old body's REPLACE clears the nullable projection even after schema upgrade.
    ``record=False`` is the read-only reader (the free-space preview): the facts a
    legacy body yields fill the answer but no column, and a store behind the current
    schema answers nothing rather than upgrading (the next prune does both).
    """
    from ouroboros.retention import task_result_retention_at
    from ouroboros.task_results import load_task_result

    ids = [str(task_id) for task_id in task_ids]
    if not ids:
        return {}
    stopped = stop or (lambda: False)
    with open_store(root, write=False) as txn:
        schema = txn.meta("schema_version")
    if schema is not None and int(schema) < store.SCHEMA_VERSION:
        if not record:
            return {}
        from ouroboros.store_import import upgrade_current_schema

        upgrade_current_schema(root)
    with open_store(root, write=False) as txn:
        rows = txn.conn.execute(
            "SELECT t.task_id, t.status, t.delegation_role, t.drive_retention_at, "
            "b.task_id IS NOT NULL AS body_present FROM tasks AS t "
            "LEFT JOIN task_bodies AS b ON b.task_id=t.task_id "
            f"WHERE t.task_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    facts = {row["task_id"]: dict(row) for row in rows}
    classified = []
    for task_id, row in facts.items():
        if stopped():
            break
        if row["drive_retention_at"] is not None or not row["body_present"]:
            continue
        try:
            document, stamp = load_task_result(root, task_id, strict=True, signed=True)
            if document is None or stamp is None:
                continue
            value = task_result_retention_at(document)
            if value is None:
                continue
            # This coherent body's facts improve this pass's hint too. A racing
            # writer may still postpone a skip; actual candidates are read afresh.
            row.update(status=document["status"], delegation_role=document.get("delegation_role"),
                       drive_retention_at=value)
            classified.append((value, task_id, stamp[0]))
        except (store.StoreError, ValueError, TypeError, AttributeError):
            # Leave NULL: the pruner's strict read retains the original diagnostic.
            continue
    if record and classified and not stopped():
        with open_store(root, write=True) as txn:
            txn.conn.executemany(
                "UPDATE tasks SET drive_retention_at=? WHERE task_id=? AND write_seq=? "
                "AND drive_retention_at IS NULL", classified)
    return facts


def _collection_ids(root: Any, column: str, pending: Optional[int], after: str) -> List[str]:
    where = "IS NULL" if pending is None else "=1"
    with open_store(root, write=False) as txn:
        return [record[0] for record in txn.conn.execute(
            f"SELECT task_id FROM tasks WHERE {column} {where} AND task_id>? "
            "ORDER BY task_id LIMIT ?", (after, _COLLECTION_BATCH))]


def _classify_collection(root: Any, column: str, predicate: Callable[[dict], bool],
                        task_ids: List[str], stop: Callable[[], bool]) -> List[str]:
    """Decode each legacy body after its coherent read; batch still-NULL sequence CAS updates.
    Classification changes no body/write_seq. A racing body writer or handoff wins."""
    classified, candidates = [], []
    for task_id in task_ids:
        if stop():
            break
        try:
            with open_store(root, write=False) as txn:
                stamp = txn.row(task_id, "write_seq", column)
                body = txn.body_text(task_id) if stamp is not None and stamp[1] is None else None
            if body is None:
                if stamp is not None and stamp[1] == 1:
                    candidates.append(task_id)
                continue
            pending = int(predicate(json.loads(body)))
            classified.append((pending, task_id, stamp[0]))
            if pending:
                candidates.append(task_id)
        except (store.StoreError, ValueError, TypeError, AttributeError):
            candidates.append(task_id)  # unreadable stays unknown; the collector retains its diagnostics
    if classified and not stop():
        with open_store(root, write=True) as txn:
            txn.conn.executemany(f"UPDATE tasks SET {column}=? WHERE task_id=? AND write_seq=? "
                                 f"AND {column} IS NULL", classified)
    return candidates


def _collection_candidates(root: Any, column: str, predicate: Callable[[dict], bool],
                           stop: Optional[Callable[[], bool]]) -> Iterable[str]:
    """Known work first, then bounded legacy classification through the existing maintenance owner.
    Column/predicate belong to the two callers below. No transaction spans a yield;
    every candidate's authority is re-read by its collector."""
    stopped = stop or (lambda: False)
    try:
        with open_store(root, write=False) as txn:
            schema = txn.meta("schema_version")
        if schema is not None and int(schema) < store.SCHEMA_VERSION:
            from ouroboros.store_import import upgrade_current_schema
            upgrade_current_schema(root)
        for pending in (1, None):
            after = ""
            while not stopped():
                identities = _collection_ids(root, column, pending, after)
                if not identities:
                    break
                after = identities[-1]
                yield from identities if pending == 1 else _classify_collection(root, column, predicate, identities, stopped)
    except store.StoreError:
        return  # failed acquisition/classification leaves the same candidates owed


def reclassify_review_collection(root: Any, task_id: str) -> bool:
    """Write back a ``review_collection_pending`` 1 the current predicate no longer selects (a row an older
    body flagged for its acceptance collection, which nothing rewrites now). Changes no body or write_seq;
    a racing body writer wins. True when the column was cleared."""
    from ouroboros.obligations import review_collection_pending

    with open_store(root, write=False) as txn:
        stamp = txn.row(str(task_id), "write_seq", "review_collection_pending")
        body = txn.body_text(str(task_id)) if stamp is not None and stamp[1] == 1 else None
    if body is None or review_collection_pending(json.loads(body)):
        return False
    with open_store(root, write=True) as txn:
        return txn.conn.execute("UPDATE tasks SET review_collection_pending=0 WHERE task_id=? AND write_seq=? "
                                "AND review_collection_pending=1", (str(task_id), stamp[0])).rowcount == 1


def review_collection_candidates(root: Any, *, stop: Optional[Callable[[], bool]] = None) -> Iterable[str]:
    """Tasks holding an open review operation pointer (``obligations.review_collection_pending``);
    unrelated plans stay out."""
    from ouroboros.obligations import review_collection_pending

    yield from _collection_candidates(root, "review_collection_pending", review_collection_pending, stop)


def plan_collection_candidates(root: Any, *, stop: Optional[Callable[[], bool]] = None) -> Iterable[str]:
    """Possible pending plan seats; their record and ended-task reader keep authority."""
    from ouroboros.plan_record import PLAN_RECORD_KEY, collection_pending

    yield from _collection_candidates(root, "plan_collection_pending",
                                     lambda row: collection_pending(row.get(PLAN_RECORD_KEY)), stop)


def inventory(root: Any) -> List[Dict[str, Any]]:
    """Every task's paging identity, newest write first: ``name`` (the task id), ``write_seq``
    (changes with every write, the role the file size and mtime played) and ``updated_at``."""
    with open_store(root, write=False) as txn:
        records = txn.conn.execute("SELECT task_id, write_seq, updated_at FROM tasks "
                                   "ORDER BY updated_at DESC, task_id DESC").fetchall()
    return [{"name": record["task_id"], "write_seq": int(record["write_seq"] or 0),
             "updated_at": str(record["updated_at"] or "")} for record in records]
