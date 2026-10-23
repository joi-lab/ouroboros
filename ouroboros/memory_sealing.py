"""Own-work candidate selection and terminal links; neither invents page coverage."""
from __future__ import annotations

from ouroboros import chat_chain, dialogue_store, memory_inventory
from ouroboros.chronicle_store import DELEGATED_FOCUS_ROLES


def failed_work_units(root, store):
    from ouroboros.task_results import list_task_results
    from ouroboros.memory_fallback import FallbackUnit
    from ouroboros.memory_inventory import OpenSegment

    failed = {str(row.get("task_id") or row.get("id")): row for row in list_task_results(
        root, statuses=["failed", "cancelled"], strict=True)}
    if not failed:
        return []
    pages = store.records(kinds=["page"])
    sealed_by_mind = {str(record.get("author", {}).get("task_id") or "") for record in pages
                      if record.get("author", {}).get("kind") == "mind"}
    pending = {}
    for record in store.pending_candidates():
        pending.setdefault(record["room_id"], set()).update(record["covers"]["rows"])
    units = []
    for room, entries in memory_inventory.open_rows_by_room(root).items():
        by_task = {}
        for entry in entries:
            task_id = str(entry[1].get("task_id") or "")
            if task_id in failed and task_id not in sealed_by_mind and entry[0]["row_sha256"] not in pending.get(room, ()):
                by_task.setdefault(task_id, []).append(entry)
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


def capture_terminal_rows(root, records):
    """Group the writers' later terminal rows once, through task indexes at one captured boundary."""
    starts = {}
    for record in records:
        task_id = _terminal_writer(record)
        if task_id:
            key = (str(record["room_id"]), task_id)
            starts[key] = min(starts.get(key, _terminal_after(record)), _terminal_after(record))
    if not starts:
        return {}
    upper = dialogue_store.stream_end(root, "chat")
    lower = chat_chain.position_boundary(root, min(starts.values()) + 1)
    dialogue_store.require_imported(root, "chat", lower=lower, upper=upper)
    tasks = {task_id for _room, task_id in starts}
    facts, grouped, after = memory_inventory.membership_facts(root), {}, lower - 1
    while entries := dialogue_store.task_rows(root, tasks, stream="chat", after=after, before=upper):
        for entry in entries:
            row = entry.row
            task_id = str(row.get("task_id") or "")
            host = row.get("type") in ("task_summary", "project_completion_summary")
            final = row.get("direction") == "out" and not row.get("is_progress") and (
                row.get("terminal_origin") or row.get("task_terminal_status") or row.get("task_phase") == "finalizing")
            if task_id not in tasks or not (host or final):
                continue
            rooms = memory_inventory.rooms_of_row(memory_inventory.row_meta(row), facts)
            for address, _row, pos in chat_chain._addressed(root, (entry,)):
                for room in rooms:
                    key = (room, task_id)
                    if key in starts and pos > starts[key]:
                        grouped.setdefault(key, []).append((address, row, pos))
        after = entries[-1].seq
    return grouped


def terminal_links(root, record, cache=None):
    """Only this page writer's later typed terminal rows, as separately attributed addresses."""
    task_id = _terminal_writer(record)
    if not task_id:
        return []
    end, links = _terminal_after(record), []
    cache = capture_terminal_rows(root, (record,)) if cache is None else cache
    for address, row, pos in cache.get((str(record["room_id"]), task_id), ()):
        if pos <= end:
            continue
        addr = chat_chain.format_address(address)
        speaker = "host summary" if row.get("type") in ("task_summary", "project_completion_summary") else "task final report"
        status = row.get("status") or row.get("task_terminal_status") or "status not recorded"
        links.append(f"- Linked {speaker} of task {task_id} ({status}); not page coverage; "
                     f"memory_read(room_id='{record['room_id']}', rows=true, from='{addr}', to='{addr}')")
    return links
