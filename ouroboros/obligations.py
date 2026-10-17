"""Addressed current obligations, maintained by the transitions that owe them.

The derived sets are a pure function of the result documents: every write of a
document recomputes its flag columns in ``tasks`` from ``result_memberships`` in
the same transaction, so membership can neither lag nor outlive the transition
that owes it. The owner-held sets (``custody_open``, closing custody receipts of
``delegated_runs``, ``pending_drives`` rows a pool handoff publishes, the receipt
sets, ``unknowns``, witnesses) are rows of the ``obligations`` table. ``members``
reads both halves in one transaction; no set is a file any more (the importer
reads the old set files once, the exporter writes them back for an older body).
Every set owes work except the declared ``RECORD_SETS``, which are kept facts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ouroboros import store
from ouroboros.store import StoreError

BOOT_SETS = ("nonterminal", "synthesis", "terminal_projection", "delegated_runs",
             "promotions", "custody_open", "pending_drives", "upgrade_notices", "unknowns",
             "pause_notices", "pause_notice_receipts", "owner_questions")
# Owed work: a task with a row here is not settled (the evidence packer's predicate). A stored set declared
# in neither tuple owes too (an older or newer body's duty, an imported set file): an unknown duty never packs.
DUTY_SETS = ("nonterminal", "synthesis", "terminal_projection", "delegated_runs", "promotions", "custody_open",
             "pending_drives", "unknowns", "pause_notices", "local_custody_witnesses", "owner_questions",
             "late_review_marks")
# Kept facts, never owed: chat notice receipts, custody marks, definite start refusals.
RECORD_SETS = ("upgrade_notices", "pause_notice_receipts", "custody_marks", "custody_refused")
ObligationsUnavailable = store.StoreError  # the sets are rows: unavailable exactly when the store is
_FACT_COLUMNS = ("task_id", "headless_child_drive_root", "child_drive_root", "drive_root")


def review_collection_pending(row: dict) -> bool:
    """Conservative body-only selection for free acceptance collection, not a packer duty.
    Live controllers remain candidates; actor death needs no body rewrite. Closed
    legacy panels acquire no notice unless their own matching receipt already owes it."""
    from ouroboros.review_projection import _actor_pending

    operations = row.get("review_operations", {})
    projection = row.get("review_projection", {})
    if operations is None:
        operations = {}
    if projection is None:
        projection = {}
    if not isinstance(operations, dict) or not isinstance(projection, dict):
        return True
    pointed = set()
    for entry in operations.values():
        if not isinstance(entry, dict):
            return True
        key = entry.get("retry_key")
        if key is not None and not isinstance(key, str):
            return True
        pointed.add(key)
        if entry.get("control_only") or entry.get("surface") != "task_acceptance":
            continue
        state = entry.get("state")
        if not isinstance(state, str) or state not in {"closed", "collected", "preparation_unknown", "preparation_refused"}:
            return True
    panels, receipts = projection.get("panels", []), projection.get("late_notice_receipts", {})
    if panels is None:
        panels = []
    if receipts is None:
        receipts = {}
    if not isinstance(panels, list) or not isinstance(receipts, dict):
        return True
    for panel in panels:
        if not isinstance(panel, dict):
            return True
        if panel.get("surface") != "task_acceptance" or panel.get("authority") != "host_root":
            continue
        actors = panel.get("actors") or []
        if not isinstance(actors, list) or any(not isinstance(actor, dict) for actor in actors):
            return True
        try:
            pending = any(_actor_pending(actor) for actor in actors)
        except TypeError:  # a malformed actor state cannot certify settled work
            return True
        if pending:
            return True  # only the retained source can identify its retry key
        late = panel.get("late_settlement")
        if late is None:
            continue
        if not isinstance(late, dict) or not isinstance(late.get("reviewed_subject", {}), dict):
            return True
        key = str((late.get("reviewed_subject") or {}).get("retry_key") or "")
        if not key or key in pointed:
            continue
        receipt = receipts.get(f"acceptance-late:{key}")
        if receipt is None or isinstance(receipt, dict) and receipt.get("panel_id") != panel.get("panel_id"):
            continue
        if not isinstance(receipt, dict) or receipt.get("custody") != "terminal_outbox" \
                or receipt.get("settled_at") != late.get("settled_at"):
            return True
    return False


def _check_name(name: str) -> str:
    if not name or Path(name).name != name:
        raise ValueError("an obligation set name must be a filename stem")
    return name


def members_in(txn: store.Txn, name: str) -> dict[str, dict]:
    """One set inside an open transaction: the owner-held rows, plus the members the flag
    and status columns derive with their facts from the typed columns (a held row of a
    flagged task keeps its own facts under the derived ones: the closing receipts of
    ``delegated_runs`` survive until the backfill acknowledges them)."""
    rows = dict(txn.set_rows(_check_name(name)))
    if name in store.FLAG_COLUMNS:
        where = f"{store.FLAG_COLUMNS[name]}=1"
    elif name == "nonterminal":
        where = "status IN ('running', 'interrupted')"
    else:
        return rows
    for record in txn.conn.execute(f"SELECT {','.join(_FACT_COLUMNS)} FROM tasks WHERE {where} ORDER BY task_id"):
        facts = {key: record[key] for key in _FACT_COLUMNS if record[key]}
        rows[record["task_id"]] = {**rows.get(record["task_id"], {}), **facts}
    return rows


def owner_held_rows(name: str, rows: dict[str, dict]) -> dict[str, dict]:
    """The rows of a set file an import may hold as owner-held rows: a derived set's members
    come only from the typed columns (``nonterminal`` and the flag columns), so its file rows
    import nothing; ``delegated_runs`` keeps its closing receipts, ``pending_drives`` the rows a
    pool handoff published (both retire by their own owner); every other set is owner-held."""
    if name == "delegated_runs":
        return {identity: facts for identity, facts in rows.items() if isinstance(facts, dict) and facts.get("closed_runs")}
    if name == "nonterminal" or (name in store.FLAG_COLUMNS and name != "pending_drives"):
        return {}
    return rows


def members(root: Any, name: str) -> dict[str, dict]:
    """Return an independent snapshot; no migration or historical fallback."""
    with store.open_store(root, write=False) as txn:
        return members_in(txn, name)


def add(root: Any, name: str, identity: str, facts: dict | None = None) -> None:
    """Publish (or update) an addressed owner-held obligation before its dependent effect."""
    with store.open_store(root, write=True) as txn:
        txn.put_row(_check_name(name), str(identity), dict(facts or {}))


def remove(root: Any, name: str, identity: str) -> None:
    """Retire an owner-held row after discharge (a derived member retires with its document)."""
    with store.open_store(root, write=True) as txn:
        txn.delete_row(_check_name(name), str(identity))


def result_memberships(row: dict) -> set[str]:
    """The result-owning writers share these predicates, including direct patches."""
    names = set()
    if row.get("status") in {"running", "interrupted"}:
        names.add("nonterminal")
    checkpoint = row.get("root_phase_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    if checkpoint.get("post_task_synthesis"):
        from ouroboros.post_task_checkpoint import post_task_synthesis_is_open
        if post_task_synthesis_is_open(checkpoint["post_task_synthesis"]):
            names.add("synthesis")
    if (row.get("canonical_terminal_projection_origin") == "terminal_transition"
            or isinstance(row.get("canonical_terminal_projection_ready"), dict)):
        from ouroboros.terminal_projection import terminal_projection_owed
        if terminal_projection_owed(str(row.get("task_id") or ""), row):
            names.update(("synthesis", "terminal_projection"))
    if row.get("delegated_runs_unreconciled"):
        names.add("delegated_runs")
    if row.get("child_ref_promotion"):
        from ouroboros.observability import _has_pending_ref_promotion
        if _has_pending_ref_promotion(row["child_ref_promotion"]):
            names.add("promotions")
    if row.get("pause_notices"):
        names.add("pause_notices")
    quizzes = row.get("owner_quiz")
    if isinstance(quizzes, dict):
        from ouroboros.owner_quiz import quiz_needs_retention
        if any(quiz_needs_retention(block) for block in quizzes.values()):
            names.add("owner_questions")
    return names


def result_facts(row: dict) -> dict:
    return {key: row[key] for key in _FACT_COLUMNS if row.get(key)}


def pending_drive(root: Path, task_id: str, document: dict) -> bool | None:
    """None preserves a writer's membership; import has no previous membership."""
    from ouroboros.headless import terminal_task_files_ready
    from ouroboros.task_status import SETTLED_STATUSES

    status = document.get("status")
    if status in {"running", "interrupted"}:
        return True
    if status in SETTLED_STATUSES:
        try:
            return not terminal_task_files_ready(root, {**document, "id": task_id}, document)
        except Exception:
            return True  # unknown readiness keeps the drive pending
    return None


def pending_drive_rule(txn: store.Txn, current: dict, updated: dict) -> bool:
    """A status outside the drive lifecycle keeps its previous membership."""
    task_id = str(updated.get("task_id") or current.get("task_id") or "")
    pending = pending_drive(Path(txn.root), task_id, updated)
    return bool(txn.row(task_id, "pending_drive")) if pending is None else pending


def drive_started(root: Any, task: dict) -> None:
    """Pool handoff: publish before the child can write anything or die."""
    tid = str(task.get("id") or task.get("task_id") or "")
    if tid:
        add(root, "pending_drives", tid, {**result_facts(task), "task_id": tid})


def drive_finished(root: Any, task: dict, row: dict | None = None) -> None:
    """Retire only after the canonical terminal file receipt is complete."""
    from ouroboros.headless import terminal_task_files_ready
    from ouroboros.task_results import load_task_result
    from ouroboros.task_status import SETTLED_STATUSES

    tid = str(task.get("id") or task.get("task_id") or "")
    try:
        row = row if row is not None else load_task_result(root, tid, strict=True)
    except (OSError, ValueError, StoreError):
        return  # an unreadable result keeps the drive pending: recovery decides later
    if row and row.get("status") in SETTLED_STATUSES and terminal_task_files_ready(Path(root), task, row):
        with store.open_store(root, write=True) as txn:
            txn.delete_row("pending_drives", tid)
            txn.conn.execute("UPDATE tasks SET pending_drive=0 WHERE task_id=?", (tid,))


def result_rows(root: Any, name: str, *, exclude=()):
    """Read only addressed candidates; unreadable members remain repair gaps."""
    import logging
    from ouroboros.task_results import load_task_result
    for tid in members(root, name):
        if tid in exclude:
            continue
        try:
            row = load_task_result(root, tid, strict=True)
            if row:
                yield row
            else:
                add(root, "unknowns", f"task:{tid}", {"task_id": tid, "reason": "result_missing"})
        except (OSError, ValueError, StoreError) as exc:
            add(root, "unknowns", f"task:{tid}", {"task_id": tid, "reason": type(exc).__name__})
            logging.getLogger(__name__).warning("Obligation %s/%s is unreadable", name, tid)
