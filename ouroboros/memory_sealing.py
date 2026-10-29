"""Own-work candidate selection and terminal links; neither invents page coverage."""
from __future__ import annotations

import json

from ouroboros import chat_chain, memory_inventory
from ouroboros.chronicle_store import DELEGATED_FOCUS_ROLES
from ouroboros.dialogue_provenance import recorded_finality


def failed_work_units(root, store):
    """Candidates over the open rows of finished failures (failed or cancelled) the mind never sealed.

    Every post-task run asks this while consciousness is on, a direct turn's in the server after its
    answer, so it is addressed by the open rows: they name their tasks, whose typed status alone is
    read (never a task body); only when one of them failed does the chronicle answer which of them
    the mind sealed (its page author index) and which rows a pending candidate of their room covers."""
    from ouroboros.memory_fallback import FallbackUnit
    from ouroboros.memory_inventory import OpenSegment
    from ouroboros.task_store import task_facts

    open_rows = memory_inventory.open_rows_by_room(root)
    named = sorted({str(entry[1].get("task_id") or "") for entries in open_rows.values() for entry in entries} - {""})
    # The named rows alone, through the primary key: a status filter in SQL lets the planner walk
    # every failed task through the status index instead.
    facts = task_facts(root, where="task_id IN (SELECT value FROM json_each(?))", params=(json.dumps(named),)) if named else {}
    failed = {task_id for task_id, fact in facts.items() if fact["status"] in ("failed", "cancelled")}
    if failed:
        failed -= store.mind_page_authors(failed)
    units = []
    for room, entries in open_rows.items():
        entries = [entry for entry in entries if str(entry[1].get("task_id") or "") in failed]
        pending = {ref for record in store.pending_candidates(room) for ref in record["covers"]["rows"]} if entries else set()
        by_task = {}
        for entry in entries:
            if entry[0]["row_sha256"] not in pending:
                by_task.setdefault(str(entry[1].get("task_id") or ""), []).append(entry)
        for task_id, rows in by_task.items():
            segment = OpenSegment(room, rows[0][0], rows[-1][0], tuple(rows), store.room_head(room))
            units.append(FallbackUnit("page", room, f"candidate:{task_id}:{rows[0][0]['row_sha256'][:12]}",
                                      segment=segment, candidate_task=task_id))
    return sorted(units, key=lambda unit: unit.segment.rows[0][2])


def _terminal_writer(record):
    author = record.get("author", {})
    task_id = str(author.get("task_id") or "")
    own_writer = author.get("kind") == "mind" or (author.get("kind") == "helper" and
        author.get("focus", {}).get("role") in DELEGATED_FOCUS_ROLES)
    return task_id if record.get("kind") == "page" and own_writer else ""


def _terminal_after(record):
    return int(record.get("metadata", {}).get("terminal_after_pos",
        (record.get("covers", {}).get("stream_span") or [0, 0])[-1]))


# The stored facts a linked row's finality is read from (``dialogue_provenance.recorded_finality``).
_FINALITY_FACTS = ("summary_kind", "outcome_final", "outcome_authority")


def capture_terminal_rows(root, records):
    """Group the writers' later terminal rows once, from the stored facts of the rows after the legacy frontier
    (``memory_inventory.terminal_rows``): a page's writer ran after the chronicle's activation, so its later
    terminal rows lie after the frontier. Each is ``(address, {"type", "status", <the finality facts it
    stored>}, pos)``."""
    starts = {}
    for record in records:
        task_id = _terminal_writer(record)
        if task_id:
            key = (str(record["room_id"]), task_id)
            starts[key] = min(starts.get(key, _terminal_after(record)), _terminal_after(record))
    if not starts:
        return {}
    tasks, first = {task_id for _room, task_id in starts}, min(starts.values())
    chosen = [(address, meta, pos, status) for address, meta, pos, status in memory_inventory.terminal_rows(root)
              if pos > first and str(meta.get("task_id") or "") in tasks]
    facts, grouped = memory_inventory.membership_facts(root), {}
    # The terminal rows' task lineages in one read a generation, then their rooms.
    facts.rooms.warm({str(meta.get(key) or "") for _address, meta, _pos, _status in chosen
                      for key in ("task_id", "parent_task_id", "root_task_id")} - {""})
    for address, meta, pos, status in chosen:
        task_id = str(meta["task_id"])
        for room in memory_inventory.rooms_of_row(meta, facts):
            if (room, task_id) in starts and pos > starts[(room, task_id)]:
                finality = {key: meta[key] for key in _FINALITY_FACTS if key in meta}
                grouped.setdefault((room, task_id), []).append(
                    (address, {"type": meta.get("type"), "status": status, **finality}, pos))
    return grouped


def terminal_links(root, record, cache=None):
    """This page writer's later typed terminal rows as one separately attributed line: each row's kind,
    status and recorded finality (its authority stays in the row), and one reader over their span."""
    task_id = _terminal_writer(record)
    if not task_id:
        return []
    end = _terminal_after(record)
    cache = capture_terminal_rows(root, (record,)) if cache is None else cache
    rows = [(address, row) for address, row, pos in cache.get((str(record["room_id"]), task_id), ()) if pos > end]
    if not rows:
        return []
    items = []
    for _address, row in rows:
        speaker = "host summary" if row.get("type") in ("task_summary", "project_completion_summary") else "task final report"
        finality = recorded_finality(row.get("outcome_final"), row.get("outcome_authority"),
                                     host_facts=row.get("summary_kind") == "host_task_facts").partition(";")[0]
        status = row.get("status") or row.get("task_terminal_status") or "status not recorded"
        items.append(f"{speaker} ({status}{', ' + finality if finality else ''})")
    first, last = (chat_chain.format_address(rows[index][0]) for index in (0, -1))
    return [f"- Later terminal rows of task {task_id} (host-linked, not page coverage): {'; '.join(items)}; "
            f"memory_read(room_id='{record['room_id']}', rows=true, from='{first}', to='{last}')"]
