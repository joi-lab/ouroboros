"""A room's own pages over the dialogue store (``state/dialog.sqlite``).

A page is counted in the room's rows, never in bytes of a shared chain: the
newest page holds the room's newest rows however far back they lie (read over a
generous recent window, ``_RECENT_WINDOW_ROWS``/``_BYTES``, so lineage, overlays and late
answers older than the quota's reach are still weighed by the quota logic), and
each older page the next older ones (owner decisions 2026-10-05, DESIGN "History
edges"). The chat and progress streams keep separate quotas (``human``,
``progress``), each counted by its own predicate. A room's rows are addressed
(``RoomLens``): one indexed probe per fact of the room -- its chat, the lineage
columns of its bound tasks, the owner ids its bindings name -- so a page costs
the room's own rows, never another room's history (``dialogue_store.probe_rows``).
Coordinates are the chain coordinates the store keeps: every cursor an older
body or a client holds names the same rows. The archive import's frontier is a
fact: a page never claims rows below a segment the import has not reached
(``window.truncated_by`` names the stream ``<stream>_importing``), and the older
page that would cross it answers "still importing" (``StoreMigrating``, 503),
never "no older history".
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import pathlib
from typing import Any, Dict, NamedTuple, Tuple

from ouroboros import dialogue_store as ds
from ouroboros.contracts.chat_id_policy import HIDDEN_CHAT_ID, is_a2a_chat_id
from ouroboros.dialogue_positions import skipped_between
from ouroboros.project_dialogue import (
    _ANNOTATIONS_NAME, MAIN_PINNED_ROW_TYPES, ORIGIN_ADDRESSED_NOTICE_TYPES, entry_matches_source_ref,
)
from ouroboros.store import StoreError, StoreMigrating
from ouroboros.subagent_messages import CARD_ROW_PLACEMENTS, is_task_card_message, subagent_message_meta

_SOURCES = ("chat", "progress")
# One indexed probe page per room fact: a page's cost is the room's facts times this, never its history.
_PROBE_ROWS = 200
# The recent page reads the room's newest rows per stream until its quota is met AND its window is:
# this many rows or this many stored bytes of the room's own rows, whichever comes first (the chain
# reader's 512 KB live window). The quota is applied over a generous window, so a swarm's finished
# children, a late answer or a review overlay older than the quota's reach are still weighed by
# ``history._apply_window_quotas`` (lineage anchoring, deferral to the first older page), and a room
# of long rows (Main's average about 4 KB) reads no more than the chain reader did.
_RECENT_WINDOW_ROWS = 1000
_RECENT_WINDOW_BYTES = 512 * 1024
# Bound on the candidate rows one request decodes per stream; the cursor resumes where a read
# stopped, so it bounds latency, never what a room can reach.
_READ_CEILING_ROWS = 10_000
# Bound on what one ``after`` read replays per stream (coordinates are bytes): a window that fell
# further behind (a client offline for hours) takes a fresh recent page instead (``history_delta_reset``).
_DELTA_CEILING_BYTES = 2 * 1024 * 1024
# The newest-arrival search every recent Project read pays for, in candidate rows; a bound reached
# first is carried on by the chain's cursors (``latest_before``/``quiet``).
_ARRIVAL_SEARCH_ROWS = 200
# The coverage span's chain witness: the store's coordinates are durable, so one witness names every read.
_CHAIN_WITNESS = "dialog"
# Bound on an opaque cursor's length (a query parameter): an ``after`` cursor carries the versions
# of the tasks its window shows, and a window too large to name them reads its newest page instead.
_CURSOR_CHARS = 8192
# A projection that failed part-way: the rows after the failing one are missing
# from the response, so neither its coverage nor its newest arrival is known.
PROJECTION_FAILED = "projection_failed"
# A stream's page stopped at the archive import's frontier: older rows exist and are not readable yet.
IMPORTING = "importing"
# A frozen archive segment that changed under the import: its rows are a gap, never coverage.
UNIMPORTABLE = "segment_unimportable"
# The gap names the chain readers gave the lines the import did not store (``dialogue_store.SKIPPED_KINDS``).
_GAP_NAMES = {"invalid": "malformed_jsonl", "non_object": "invalid_jsonl_row"}
# Stored kinds chat.js shows as a bubble of their own whoever sent them: it
# renders them before a child's words are routed to its card, and their
# producers (message_bus ``send_photo`` ...) count every one.
_STANDALONE_KINDS = ("document", "links", "photo", "video", "quiz", "card")
# A read around one task (``around=task:<id>``, DESIGN "Room work status") holds a quarter of
# each stream's quota before the task's earliest row in the room and the rest from it on.
_AROUND_OLDER_SHARE = 4

log = logging.getLogger(__name__)


def progress_quota_predicate(row_matches_thread, stored_chat_id):
    def counts(entry):
        if not isinstance(entry, dict) or entry.get("type") in {"review_reference", "task_model_wait"}:
            return False
        if is_a2a_chat_id(entry.get("chat_id", 1)):
            return False
        return bool(
            row_matches_thread(stored_chat_id(entry.get("chat_id"), 1), {"is_progress": True, **entry})
            and str(entry.get("content", entry.get("text", "")))
            and str(entry.get("delegation_role") or "").lower() != "subagent"
            and not entry.get("subagent_event")
        )
    return counts


def room_member_predicate(row_matches_thread, stored_chat_id):
    """Whether the room draws a stored row (``member``), and whether its room rule keeps the row at all
    (``member.in_room``: a row the room hides, such as Main's terminal projection, is still its own)."""
    def member(entry, progress=False, rule=row_matches_thread):
        row = {"is_progress": True, **entry} if progress else entry
        return bool(rule(stored_chat_id(entry.get("chat_id"), 1), row))
    rule = getattr(row_matches_thread, "evidence_matches", row_matches_thread)
    member.in_room = lambda entry, progress=False: member(entry, progress, rule)
    return member


class HistoryCursorError(ValueError):
    def __init__(self, reason: str, status: int = 409):
        super().__init__(reason)
        self.reason, self.status = reason, status


def parse_around(value):
    """An exact stored row or a task card; those are distinct navigation semantics."""
    if str(value or "").startswith("row:"):
        from ouroboros.chat_chain import parse_address
        try:
            parse_address(value)
        except ValueError:
            raise HistoryCursorError("history_around_invalid", 400) from None
        return value
    kind, _sep, task_id = str(value or "").partition(":")
    if kind != "task" or not task_id or len(task_id) > 128 or any(char.isspace() for char in task_id):
        raise HistoryCursorError("history_around_invalid", 400)
    return task_id


def room_view_fingerprint(thread_id, project_ids, source_refs, bindings):
    """Fingerprint the existing room lens, without introducing membership state.

    Every room hashes every binding: a task's resolved room outranks the room its
    rows were written to, so a conversion anywhere can move rows out of this room."""
    project = thread_id in project_ids
    value = [thread_id, project, [] if project else sorted(project_ids), dict(bindings),
             sorted(source_refs, key=lambda row: json.dumps(row, sort_keys=True))]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def encode_cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(value, thread_id, view):
    try:
        if not isinstance(value, str) or len(value) > _CURSOR_CHARS:
            raise ValueError
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        cursor = json.loads(decoded)
        if not isinstance(cursor, dict) or cursor.get("v") != 1 or cursor.get("kind") not in {"older", "page", "after"}:
            raise ValueError
        for field in ("upper", "before"):
            if set(cursor[field]) != set(_SOURCES):
                raise ValueError
            if any(type(cursor[field][source]) is not int or cursor[field][source] < 0 for source in _SOURCES):
                raise ValueError
        if any(cursor["before"][source] > cursor["upper"][source] for source in _SOURCES):
            raise ValueError
        unfinished = cursor.setdefault("unfinished", [])
        if not isinstance(unfinished, list) or unfinished != sorted(set(unfinished) & set(_SOURCES)):
            raise ValueError
        if type(cursor.setdefault("quiet", False)) is not bool:  # see ``latest_arrival``
            raise ValueError
        if type(cursor.setdefault("notes", "")) is not str or len(cursor["notes"]) > 64:  # see ``notes_stamp``
            raise ValueError
        tasks = cursor.setdefault("tasks", {})  # see ``history_page_tokens``
        if not isinstance(tasks, dict) or any(type(seq) is not int or seq < 0 for seq in tasks.values()):
            raise ValueError
        if type(cursor.setdefault("newest", "")) is not str or len(cursor["newest"]) > 64:  # see ``latest_arrival``
            raise ValueError
        if type(cursor.setdefault("lifecycle", False)) is not bool:
            raise ValueError
        if set(cursor["quotas"]) != {"human", "progress"} or any(
            type(value) is not int or value < 0 for value in cursor["quotas"].values()
        ):
            raise ValueError
        if cursor["kind"] == "page":
            if type(cursor.get("recent")) is not bool or set(cursor["lower"]) != set(_SOURCES):
                raise ValueError
            if any(type(cursor["lower"][source]) is not int
                   or not 0 <= cursor["lower"][source] <= cursor["before"][source] for source in _SOURCES):
                raise ValueError
    except (ValueError, TypeError, KeyError):
        raise HistoryCursorError("history_cursor_invalid", 400) from None
    if cursor.get("chat_id") != thread_id or cursor.get("view") != view:
        raise HistoryCursorError("history_view_changed")
    return cursor


def notes_stamp(data_dir):
    """The owner-message annotations file's identity and length, taken before a read uses them: an
    ``after`` read that finds them changed cannot bring the window's older owner rows up to date,
    so it refuses (``history_delta_reset``) and the window takes its newest page again."""
    try:
        stat = (pathlib.Path(data_dir) / "logs" / _ANNOTATIONS_NAME).stat()
    except OSError:
        return ""
    return f"{stat.st_ino}:{stat.st_size}"


class RoomLens(NamedTuple):
    """How one stream of a room is addressed in the store: one indexed probe per ``keys`` entry
    (a ``dialogue_store.probe_rows`` column and value) and, for the chat stream, the coordinates
    (``seqs``) of the owner rows a binding names without a client id, resolved by identity once per
    request. ``project_dialogue.room_membership`` decides each candidate; the lens is its superset."""

    keys: Tuple[Tuple[str, Any], ...]
    seqs: Tuple[int, ...] = ()


def _descendants(data_dir: pathlib.Path, tasks: list) -> list:
    """The task ids the store's lineage places under ``tasks``: the whole tree of a bound root by
    ``root_task_id``, and the children of a bound mid-tree task generation by generation by
    ``parent_task_id`` (one indexed read per generation, 500 ids per statement). An unavailable store
    names none (their rows then stay where they were written, as the room rule itself would leave them)."""
    from ouroboros.task_store import task_facts

    known, found, frontier = set(tasks), set(), sorted(tasks)
    while frontier:
        generation: set = set()
        for start in range(0, len(frontier), 500):
            chunk = frontier[start:start + 500]
            marks = ",".join("?" * len(chunk))
            try:
                facts = task_facts(data_dir, where=f"root_task_id IN ({marks}) OR parent_task_id IN ({marks})",
                                   params=(*chunk, *chunk))
            except StoreError:
                log.debug("task lineage unavailable for the room lens", exc_info=True)
                return sorted(found)
            generation.update(task for task in facts if task not in known)
        known |= generation
        found |= generation
        frontier = sorted(generation)
    return sorted(found)


def room_lens(data_dir: pathlib.Path, thread_id: int, project_chat_ids: set, source_refs: list,
               bindings_by_task: Dict[str, int]) -> Dict[str, RoomLens]:
    """How the room's rows are addressed in the store, per stream (``room_membership`` decides each
    candidate; the lens is its superset). A Project reads its own chat, the lineage columns of the
    tasks bound to it, the task ids of their descendants (the store's lineage: a child's row the
    supervisor recorded under Main with its task id alone belongs to the room its parent was turned
    into) and the owner rows its bindings name (by client id, else by chat and time). Other rooms
    probe their stored chat only. Main retains the historical non-Project candidate superset
    (A2A and hidden traffic excluded), rows without a chat and its question pointers; membership
    filters out place rows. A place-room read never scans another room's history."""
    if thread_id in project_chat_ids:
        bound = sorted(task for task, chat in bindings_by_task.items() if chat == thread_id)
        keys: list = [("chat_id", thread_id)]
        for task in bound:
            keys.extend((("task_id", task), ("parent_task_id", task), ("root_task_id", task)))
        keys.extend(("task_id", task) for task in _descendants(data_dir, bound))
        owners, seqs = [], []
        for ref in source_refs:
            if not isinstance(ref, dict):
                continue
            if ref.get("client_message_id"):
                owners.append(("client_message_id", str(ref["client_message_id"])))
            elif ref.get("ts"):  # a binding older than client ids names its owner row by chat and time
                try:
                    chat = int(ref["chat_id"]) if ref.get("chat_id") not in (None, "") else 1
                except (TypeError, ValueError):
                    chat = 1
                candidates = ds.rows_by_identity(data_dir, chat, str(ref["ts"]))
                if chat == 1:
                    candidates += ds.rows_by_identity(data_dir, None, str(ref["ts"]))
                seqs.extend(row.seq for row in candidates if entry_matches_source_ref(row.row, [ref]))
        return {"chat": RoomLens((*keys, *dict.fromkeys(owners)), tuple(sorted(set(seqs)))),
                "progress": RoomLens(tuple(keys))}
    if thread_id != 1:
        own = RoomLens((("chat_id", thread_id),))
        return {"chat": own, "progress": own}
    chats = [room["chat_id"] for room in ds.rooms(data_dir) if room["chat_id"] not in project_chat_ids
             and room["chat_id"] != HIDDEN_CHAT_ID and not is_a2a_chat_id(room["chat_id"])]
    keys = [("chat_id", chat) for chat in chats] + [("chat_id", None)]
    return {"chat": RoomLens((*keys, ("kind", "quiz"), ("kind", "quiz_answer"), ("kind", "quiz_closed"))),
            "progress": RoomLens(tuple(keys))}


def import_floor(root: pathlib.Path, source: str) -> int:
    """The coordinate below which the archive import has not reached yet (0 when it has): the
    end of the newest pending segment (segments never overlap), one seek of the ledger's state
    index whatever the archive holds. Rows below it are "still importing", never absent."""
    with ds.transaction(root, write=False) as conn:
        record = conn.execute("SELECT base + size AS stop FROM import_ledger INDEXED BY ledger_state "
                              "WHERE stream=? AND state='pending' ORDER BY base DESC LIMIT 1", (source,)).fetchone()
    return int(record["stop"]) if record else 0


class HistorySource:
    """One stream of one room, frozen at ``upper`` (the stream's end when None).

    ``recent``/``older`` page the room's rows newest first through the lens's probes until the
    quota of counted rows, the import frontier (``floor``) or the read ceiling; ``replay`` re-reads
    a frozen page by its coordinates. Every answer carries the coordinate the page reached and the
    gaps its range holds (lines the import skipped, a segment it could not import)."""

    def __init__(self, root: pathlib.Path, source: str, lens: RoomLens, upper=None):
        self.root, self.source, self.lens = pathlib.Path(root), source, lens
        end = ds.stream_end(self.root, source)
        self.upper = end if upper is None else int(upper)
        if not 0 <= self.upper <= end:
            raise HistoryCursorError("history_cursor_invalid", 400)  # a boundary this store never gave
        self.warm = None  # the room rule's lineage batch read (``RoomResolver.warm``), before rows are counted
        self.floor = import_floor(self.root, source)

    def _warmed(self, rows):
        """``rows``, after their tasks' lineage (and their ancestors') was read in batches."""
        if self.warm is not None and rows:
            self.warm({str(row.get(key) or "") for row in rows for key in ("task_id", "parent_task_id", "root_task_id")
                       if isinstance(row, dict)} - {""})
        return rows

    def _entries(self, rows):
        from ouroboros.chat_references import projection_fields  # the row's address, from the stored row alone
        return self._warmed([{**row.row, **projection_fields(row.row), "history_id": row.history_id,
                              "history_position": {"source": self.source, "offset": row.seq},
                              "_history_end": row.end} for row in rows])

    def _candidates(self, lower: int, before: int, *, newest_first: bool = True):
        """One probe page of the room's rows in ``[lower, before)`` plus the named owner rows the
        page's range covers (a short page covers the whole range)."""
        rows = ds.probe_rows(self.root, self.source, self.lens.keys, before=before, lower=lower,
                             limit=_PROBE_ROWS, newest_first=newest_first)
        if not self.lens.seqs:
            return rows
        found = {row.seq: row for row in rows}
        if len(rows) < _PROBE_ROWS:
            covered = (lower, before)
        else:
            covered = (rows[-1].seq, before) if newest_first else (lower, rows[-1].seq + 1)
        for seq in self.lens.seqs:
            if covered[0] <= seq < covered[1] and seq not in found:
                row = ds.row_at(self.root, self.source, seq)
                if row is not None:
                    found[seq] = row
        return sorted(found.values(), key=lambda row: row.seq, reverse=newest_first)

    def gaps(self, lower: int, upper: int) -> set:
        """The gap names of ``[lower, upper)``: the lines the import skipped there, a segment it
        could not import. A range the import has not reached is not a gap but "still importing"."""
        if lower >= upper:
            return set()
        coverage = ds.range_coverage(self.root, self.source, lower=lower, upper=upper)
        if coverage["pending"]:
            raise StoreMigrating("Dialogue history is still importing")
        names = {UNIMPORTABLE} if coverage["mismatch"] else set()
        if coverage["gaps"]:
            names.update(_GAP_NAMES.get(gap.kind, gap.kind) for gap in skipped_between(self.root, self.source, lower, upper))
        return names

    def recent(self, want, counts):
        """The room's newest rows: newest first until ``want`` counted rows and the recent window
        (``_RECENT_WINDOW_ROWS`` rows or ``_RECENT_WINDOW_BYTES``), the import frontier or the read ceiling."""
        return self._page(self.upper, want, counts, _READ_CEILING_ROWS, window=True)

    def older(self, before, want, counts, ceiling=None):
        """Consume one backward page of ``want`` counted rows below ``before``; the read ceiling
        ends it early. At the import frontier nothing older is readable yet: the page is empty and
        stays at the frontier (the request's caller answers "still importing")."""
        if want <= 0:
            return [], before, set()
        if 0 < self.floor >= before:
            return [], self.floor, set()
        return self._page(before, want, counts, _READ_CEILING_ROWS if ceiling is None else ceiling)

    def _page(self, before, want, counts, ceiling, window=False):
        top, selected, counted, read, seen = before, [], 0, 0, 0
        while True:
            rows = self._candidates(self.floor, before)
            for entry in self._entries(rows):  # newest first
                selected.append(entry)
                before = entry["history_position"]["offset"]
                counted += bool(counts(entry))
                read += 1
                seen += entry["_history_end"] - before  # the stored row's exact length
                windowed = not window or read >= _RECENT_WINDOW_ROWS or seen >= _RECENT_WINDOW_BYTES
                if (counted >= want and windowed) or read >= ceiling:
                    return list(reversed(selected)), before, self.gaps(before, top)
            if len(rows) < _PROBE_ROWS:  # the room's rows above the frontier are exhausted
                return list(reversed(selected)), self.floor, self.gaps(self.floor, top)

    def task_pivot(self, task_id, member, in_room=None):
        """``(state, row)`` of ``task_id`` in this stream: ``found`` and its earliest row the room draws,
        ``moved`` when its rows here belong to another room (``in_room`` says which rows the room's rule
        keeps), ``none`` when the stream holds none the room could show. The task's rows are one indexed
        probe, oldest first, never a walk of the room's history."""
        self._warmed([{"task_id": task_id}])  # its lineage, before membership is judged
        seen, lower = False, 0
        while True:
            rows = ds.probe_rows(self.root, self.source, [("task_id", task_id)], before=self.upper, lower=lower,
                                 limit=_PROBE_ROWS, newest_first=False)
            for entry in self._entries(rows):
                if member(entry):
                    return "found", entry
                seen = seen or in_room is None or not in_room(entry)
            if len(rows) < _PROBE_ROWS:
                return ("moved" if seen else "none"), None
            lower = rows[-1].seq + 1

    def ahead(self, start, want, counts, ceiling=None):
        """The room's rows from ``start`` on, oldest first, until ``want`` counted rows, the frozen end
        or the read ceiling (the mirror of ``older``): the rows, where the read stopped and its gaps."""
        if want <= 0:
            return [], start, set()
        ceiling = _READ_CEILING_ROWS if ceiling is None else ceiling
        selected, counted, position = [], 0, start
        while position < self.upper:
            rows = self._candidates(position, self.upper, newest_first=False)
            for entry in self._entries(rows):
                selected.append(entry)
                counted += bool(counts(entry))
                if counted >= want or len(selected) >= ceiling:
                    return selected, entry["_history_end"], self.gaps(start, entry["_history_end"])
            if len(rows) < _PROBE_ROWS:
                break
            position = rows[-1].seq + 1
        return selected, self.upper, self.gaps(start, self.upper)

    def replay(self, lower, before):
        """Re-read one frozen page by its coordinates: the same room view, the same rows."""
        rows, start = [], lower
        while start < before:
            page = self._candidates(start, before, newest_first=False)
            rows.extend(page)
            if len(page) < _PROBE_ROWS:
                break
            start = page[-1].seq + 1
        return self._entries(rows), lower, self.gaps(lower, before)


def select_history_page(data_dir, thread_id, view, cursor, quotas, predicates, caps, lens, notes="", warm=None,
                        around=None, thread_filter=None, row_target=None):
    """``lens``: the room's ``RoomLens`` per stream (``history._room_lens``); ``notes``:
    ``notes_stamp``; ``warm``: the room rule's lineage batch read, given rows before they are counted;
    ``around``: a task or exact row (``row_target`` is its resolved source/seq); ``thread_filter``: the
    room rule and stored-chat coercion it decides membership with (``room_member_predicate``)."""
    if around is not None:
        if cursor:
            raise HistoryCursorError("history_around_invalid", 400)  # a page starts at one place
        return _select_around(data_dir, thread_id, view, parse_around(around), quotas, predicates, lens, notes, warm,
                              room_member_predicate(*thread_filter), row_target)
    continuation = decode_cursor(cursor, thread_id, view) if cursor else None
    if continuation and continuation["kind"] == "after" and (
            continuation["notes"] != notes or _settled_since(data_dir, continuation["tasks"])):
        raise HistoryCursorError("history_delta_reset")
    if continuation:
        quotas = continuation["quotas"]
        if any(quotas[key] > caps[key] for key in quotas):
            raise HistoryCursorError("history_cursor_invalid", 400)
    recent = not continuation or (continuation["kind"] == "page" and continuation["recent"])
    after = bool(continuation) and continuation["kind"] == "after"
    selections, upper, before, page_ends, importing = {}, {}, {}, {}, []
    for source, quota in (("chat", "human"), ("progress", "progress")):
        # Rows an ``after`` read has not seen are everything stored after the window's last read,
        # so it freezes the stream fresh; every other read keeps the boundary its cursor froze.
        reader = HistorySource(data_dir, source, lens[source],
                               continuation["upper"][source] if continuation and not after else None)
        reader.warm = warm
        upper[source] = reader.upper
        page_ends[source] = continuation["before"][source] if continuation and not after else reader.upper
        if after:
            seen = continuation["upper"][source]
            if not seen <= reader.upper or reader.upper - seen > _DELTA_CEILING_BYTES:
                raise HistoryCursorError("history_delta_reset")
            selections[source] = reader.replay(seen, reader.upper)
            if selections[source][2]:  # rows it could not read stay unaccounted for
                raise HistoryCursorError("history_delta_reset")
        elif continuation and continuation["kind"] == "page":
            selections[source] = reader.replay(continuation["lower"][source], page_ends[source])
        elif continuation:
            selections[source] = reader.older(page_ends[source], quotas[quota], predicates[source])
        else:
            selections[source] = reader.recent(quotas[quota], predicates[source])
        before[source] = selections[source][1]
        if not after and 0 < reader.floor == before[source]:
            importing.append(source)
    if importing and not any(selections[source][0] for source in _SOURCES):
        raise StoreMigrating("Dialogue history is still importing")  # nothing on this page is readable yet
    return {"v": 1, "chat_id": thread_id, "view": view, "upper": upper, "unfinished": [],
            "quotas": quotas, "recent": recent, "after": after, "replayed": bool(continuation),
            "quiet": bool(continuation and continuation["quiet"]), "lens": lens, "notes": notes,
            "selections": selections, "before": before, "page_ends": page_ends, "importing": importing,
            **({key: continuation[key] for key in ("tasks", "newest", "lifecycle")} if after else {})}


def _select_around(data_dir, thread_id, view, task_id, quotas, predicates, lens, notes, warm, member, row_target=None):
    """The page around one task in this room (DESIGN "Room work status"): per stream, a quarter of
    the quota of the room's rows before the task's earliest row of the room, that row, and the rest
    after it -- never a walk of the room from the present. ``anchor`` says what was found: ``found``
    and the row a client lands on, ``moved`` (the task's rows belong to another room) or ``absent``
    (the store holds no row of it a client could show; while the archive still imports that is not
    known yet: 503 ``migrating``). ``row_target`` instead pins one resolved source/seq, with no task
    fallback. A stream holding none of the target's rows has no known place near
    it: the page and the chain it starts carry none of that stream (its quota is 0, so its coverage
    is unknown) and the page names the gap (``around_unpositioned``)."""
    quotas = dict(quotas)
    page = {"v": 1, "chat_id": thread_id, "view": view, "upper": {}, "unfinished": [], "quotas": quotas,
            "recent": False, "after": False, "replayed": False, "quiet": False, "lens": lens, "notes": notes,
            "selections": {}, "before": {}, "page_ends": {}, "importing": []}
    readers, pivots, states = {}, {}, set()
    for source in _SOURCES:
        reader = readers[source] = HistorySource(data_dir, source, lens[source])
        reader.warm = warm
        page["upper"][source] = reader.upper
        progress = source == "progress"
        if row_target is not None:
            entry = ds.row_at(data_dir, source, row_target["seq"]) if source == row_target["source"] else None
            pivots[source] = reader._entries([entry])[0] if entry and member(entry.row, progress) else None
            state = "none"
            if pivots[source] is not None:
                quotas["progress" if progress else "human"] = max(1, quotas["progress" if progress else "human"])
        else:
            state, pivots[source] = reader.task_pivot(task_id, lambda row, progress=progress: member(row, progress),
                                                      lambda row, progress=progress: member.in_room(row, progress))
        states.add(state)
    if not row_target and not any(pivots.values()) and any(reader.floor for reader in readers.values()):
        raise StoreMigrating("Dialogue history is still importing")  # its rows may be in a segment not imported yet
    for source, quota in (("chat", "human"), ("progress", "progress")):
        reader, pivot, counts, want = readers[source], pivots[source], predicates[source], quotas[quota]
        if pivot is None or not any(pivots.values()):
            page["selections"][source], quotas[quota] = ([], 0, {"around_unpositioned"}), 0
            page["before"][source] = page["page_ends"][source] = 0
            continue
        start, older_want = pivot["history_position"]["offset"], want // _AROUND_OLDER_SHARE
        older, lower, older_gaps = reader.older(start, older_want, counts) if older_want else ([], start, set())
        newer, end, newer_gaps = reader.ahead(pivot["_history_end"], want - older_want - bool(counts(pivot)), counts)
        page["selections"][source] = (older + [pivot] + newer, lower, older_gaps | newer_gaps | reader.gaps(start, pivot["_history_end"]))
        page["before"][source], page["page_ends"][source] = lower, end
        if 0 < reader.floor == lower:
            page["importing"].append(source)
    anchor = pivots["chat"] or pivots["progress"]
    identity = {"row_address": task_id, "chat_id": thread_id} if row_target else {"task_id": task_id}
    page["anchor"] = ({"state": "found", **identity, "history_id": anchor["history_id"]} if anchor else
                      {"state": "unavailable" if row_target else "moved" if "moved" in states else "absent", **identity})
    return page


def _settled_since(data_dir, tasks):
    """Whether a task the window shows changed since its read while it no longer runs: a question
    that closed, a late phase that settled. Its own rows cannot say so, so the window reads its
    newest page again; a running task's rows carry its facts. Unknown is a change."""
    if not tasks:
        return False
    from ouroboros import task_store
    from ouroboros.task_status import FINAL_STATUSES

    try:
        now = task_store.task_versions(data_dir, tasks)
    except Exception:
        return True
    for task_id, seen in tasks.items():
        if task_id not in now:
            return True
        seq, status, owes = now[task_id]
        if seq != seen and status in FINAL_STATUSES and not (owes and status in ("completed", "failed")):
            return True  # settled since (a cancelled record is terminal even while it owes its late phase)
    return False


def history_page_coverage(page, stream_gaps):
    """Delivered spans, called AFTER quota/lineage deferrals.

    A projected origin, detail overlay or shared row ID proves no delivered span.
    A quota-disabled stream is unknown, even when it emits [] and no continuation.
    Gaps (lines the import skipped, a segment it could not import) remain part of
    the coverage evidence. The chain witness is one constant: the store's coordinates
    are durable, so every read names the same chain.
    """
    return {"v": 1, "view": page["view"], "upper": dict(page["upper"]), "spans": {
        source: ({"from": page["before"][source], "to": page["page_ends"][source],
                  "chain": _CHAIN_WITNESS, "gaps": sorted(stream_gaps[source])}
                 if page["quotas"]["human" if source == "chat" else source] else None)
        for source in _SOURCES}}


def history_page_tokens(page, messages=(), reads=None):
    """The conversation decides whether older history remains: narration (progress)
    keeps paging alongside it but never keeps a press alive on its own, so no press
    lands narration alone. A narration-only read (no human quota) pages narration.

    The ``after`` cursor also carries what its window shows besides rows: the version of each
    task the projection read (``reads``, the page's ``TaskReads``), the newest message's time and
    whether a running skill operation was shown. A later ``after`` read refuses when a shown task
    settled or changed without running, or the operation is gone (``history_delta_reset``)."""
    anchor = page.get("anchor")
    if anchor is not None and anchor["state"] != "found":  # an around read with no place to land: no page
        return {"has_more": False, "next_cursor": None, "page_cursor": None, "anchor": anchor}
    state = {key: page[key] for key in ("v", "chat_id", "view", "upper", "unfinished", "quotas")}
    before = {source: position if page["quotas"]["human" if source == "chat" else source] else 0
              for source, position in page["before"].items()}
    has_more = bool(before["chat"]) if page["quotas"]["human"] else any(before.values())
    versions, overlaid = getattr(reads, "versions", {}), getattr(reads, "overlaid", set)
    # Tasks whose facts the rows show: not an owner's row, a notice naming another room's task or a pointer.
    shown = {str(row.get("task_id")) for row in messages if row.get("task_id") and row.get("role") != "user"
             and row.get("system_type") not in ("project_question_pointer", *ORIGIN_ADDRESSED_NOTICE_TYPES,
                                                *MAIN_PINNED_ROW_TYPES)}
    lifecycle = any(row.get("lifecycle_virtual") for row in messages)
    if page.get("after") and page.get("lifecycle") and not lifecycle:
        raise HistoryCursorError("history_delta_reset")  # the shown operation ended: nothing here says so
    arrivals = {**state, "kind": "after", "before": dict(page["upper"]), "quiet": False, "notes": page["notes"],
                "tasks": {**page.get("tasks", {}),
                          **{tid: versions[tid] for tid in sorted(overlaid(shown)) if tid in versions}},
                "newest": page.get("newest", ""), "lifecycle": lifecycle}
    after_cursor = encode_cursor(arrivals)
    after_cursor = after_cursor if len(after_cursor) <= _CURSOR_CHARS else None
    if page.get("after"):  # a read of what arrived after the window's last read: it never pages older
        if after_cursor is None:
            raise HistoryCursorError("history_delta_reset")
        return {"has_more": False, "next_cursor": None, "page_cursor": None, "after_cursor": after_cursor}
    return {
        "has_more": has_more,
        "next_cursor": encode_cursor({**state, "kind": "older", "before": before,
                                      "quiet": page.get("quiet_below", False)}) if has_more else None,
        "page_cursor": encode_cursor({**state, "kind": "page", "before": page["page_ends"], "quiet": page["quiet"],
                                      "lower": {source: value[1] for source, value in page["selections"].items()},
                                      "recent": page["recent"]}),
        # The newest page names where the next read of new arrivals starts.
        **({"after_cursor": after_cursor} if after_cursor and page["recent"] and not page["replayed"] else {}),
        **({"anchor": anchor} if anchor is not None else {}),
    }


def projected_history_ids(messages):
    """Folded attempts still account for the exact physical rows they contain."""
    ids = set()
    for message in messages:
        if message.get("history_id"):
            ids.add(message["history_id"])
        group = message.get("review_group")
        if isinstance(group, dict):
            ids.update(row["history_id"] for row in group.get("attempts", []) if row.get("history_id"))
    return ids


def deferred_before(source, entries, candidates, messages, before):
    """Do not advance a recent cursor beyond any quota-deferred physical row."""
    if entries is None:
        return before
    deferred = projected_history_ids(candidates) - projected_history_ids(messages)
    references = {(row.get("surface"), row.get("presentation_owner_task_id") or row.get("task_id"))
                  for row in messages if row.get("system_type") == "review_reference"}
    for row in candidates:
        if (row.get("system_type") == "review_reference"
                and (row.get("surface"), row.get("presentation_owner_task_id") or row.get("task_id")) in references):
            # Older invalidations of one already-present review owner are
            # intentionally folded by the existing projection, not quota loss.
            deferred.discard(row.get("history_id"))
    return max([before, *(entry["_history_end"] for entry in entries
                          if entry.get("history_id") in deferred
                          and entry["history_position"]["source"] == source)])


def _card_content(row, kind, child=lambda _row: False):
    """Task-card content: a host placement, or a child's own words (``child``: by the
    lineage its task result recovers). What a child delivers stands alone, and so does
    a routing notice: its task_id names the task it refused, never its speaker."""
    return row.get("card_row") in CARD_ROW_PLACEMENTS or (
        kind not in _STANDALONE_KINDS and kind not in ORIGIN_ADDRESSED_NOTICE_TYPES
        and (is_task_card_message(row) or child(row)))


def _feed_message(row):
    """A standalone conversation row with a physical chat position.

    Progress, a folded review, a task summary and task-card content change a
    card, not the conversation; a skill review is appended beside it and never
    counts as unread."""
    position = row.get("history_position")
    kind = str(row.get("system_type") or "")  # the stored ``type``
    return (isinstance(position, dict) and position.get("source") == "chat"
            and not row.get("is_progress") and not isinstance(row.get("review_group"), dict)
            and kind not in ("task_summary", "skill_review") and not _card_content(row, kind))


def _skipped_row_after(root, after, upper):
    """Whether a line the import did not store lies at or after ``after`` in the chat stream."""
    return bool(skipped_between(root, "chat", after, upper))


def _stored_message(row_matches_thread, stored_chat_id, child):
    """A stored chat row the history projection turns into a standalone message."""
    def message(entry):
        kind = str(entry.get("type") or "")
        return (str(entry.get("direction") or "").lower() in ("out", "system")
                and kind not in ("routing_options", "quiz_answer", "quiz_closed", "task_summary", "skill_review")
                and not is_a2a_chat_id(entry.get("chat_id", 1))
                and (str(entry.get("text") or "").strip() not in ("", "\u200b") or kind in _STANDALONE_KINDS)
                and row_matches_thread(stored_chat_id(entry.get("chat_id"), 1), entry)
                and not _card_content(entry, kind, child))  # last: it may read the row's task result
    return message


def latest_arrival(rows, page, data_dir, row_matches_thread, stored_chat_id, projected_gaps=frozenset(),
                   task_result=lambda _task_id: {}):
    """``window.latest_message`` of a recent Project read (DESIGN "Unread").

    The window is ordered and tailed by event time, but a Project message counts
    as unread when it ARRIVES: an answer the terminal outbox delivers late keeps
    its original ``ts``, so it can sort above messages that arrived before it
    (``out_of_order``) or below the window's floor, where the first older page
    (``deferred_before``) shows it. Reading the room means reading the standalone
    message that arrived last, named from the persisted rows before the tail and
    annotation rewrite them. A child's words are card content by their stored
    lineage or, on rows written before rows carried it, by the lineage their
    ``task_result`` or this page's progress recovers — the projection's own authority, which shows such a
    row in the child's card; a photo, video, file, link card or question a child
    delivers is shown alone and is a message. ``None``: its arrival is unknown — its
    projection failed part-way (``projected_gaps`` holds ``PROJECTION_FAILED``: any row
    after the fault may be the newest) or a line the import could not store lies after
    the newest readable row — and on a replayed recent page, which cannot see what
    arrived after its frozen boundary.

    Card rows, a child's words and the owner's own messages can fill the recent
    selection's quota. One bounded older read of the room (``_ARRIVAL_SEARCH_ROWS``)
    then names the newest stored message before it — ``out_of_order``, since this
    read cannot place it relative to the bottom — or proves the chat holds none
    (absent); an unreadable row reached first leaves the arrival unknown. Its
    bound reached first leaves it unknown too, but searched: ``latest_before``
    says none lies at or after that offset, and the chain's cursors carry that
    fact (``quiet``: none at or after the cursor's ``before``). An older page of
    a quiet chain names its own newest message — then the newest below the
    chain's frozen ``upper`` — or, holding none, passes the fact on, and the one
    that reaches the start of the chat without a gap proves the chat holds none
    below that ``upper`` (``latest_absent``); any other older page names nothing.
    A legacy child's lineage may first appear on another page. This bounded
    read can then name its final before the client places it in a child card;
    that unavailable standalone node remains unacknowledged, not guessed read.
    """
    quiet = page["quiet"]
    if page["replayed"] and page["recent"]:
        return {"latest_message": None}
    if not (page["recent"] or quiet or page["after"]):
        return {}
    entries, start, gaps = page["selections"]["chat"]
    end = page["page_ends"].get("chat", 0)
    # The client learns these facts before placing any row. A scheduling row's
    # task_id may name the parent; the shared projection names the actual child.
    known_children = {meta["subagent_task_id"] for row in rows if (meta := subagent_message_meta(row))}

    def child(row):
        task_id = str(row.get("task_id") or "")
        return bool(task_id) and (task_id in known_children
                                 or bool(subagent_message_meta(task_result(task_id), task_id=task_id)))

    offset = lambda row: row["history_position"]["offset"]  # noqa: E731
    words = lambda row: _card_content(row, str(row.get("system_type") or ""), child)  # noqa: E731
    feed = [row for row in rows if _feed_message(row)]
    spoken = sorted((row for row in feed if row.get("role") != "user" and (
        str(row.get("text") or "").strip() not in ("", "\u200b") or row.get("msg_type"))), key=offset)
    latest = next((row for row in reversed(spoken) if not words(row)), None)
    # The newest time a message this read shows was sent: a later arrival sent before it sorts above
    # it, which the window's next ``after`` read can only tell from its cursor.
    newest = next((str(row.get("ts") or "") for row in sorted(feed, key=lambda row: str(row.get("ts") or ""),
                                                              reverse=True) if not words(row)), "")
    shown = page.get("newest", "")
    page["newest"] = max(shown, newest) if page["after"] else newest
    if PROJECTION_FAILED in projected_gaps or (
            gaps and _skipped_row_after(data_dir, offset(latest) if latest else start, end)):
        return {"latest_message": None}
    if page["after"]:  # what arrived since the window's last read: its newest message, if one did
        return {"latest_message": {"history_id": latest["history_id"], "out_of_order": str(latest.get("ts") or "") < shown
                                   or any(offset(row) < offset(latest) and str(row.get("ts") or "") > str(latest.get("ts") or "")
                                          and not words(row) for row in feed)}} if latest else {}
    if not page["recent"]:
        page["quiet_below"] = latest is None
        if latest:
            return {"latest_message": {"history_id": latest["history_id"], "out_of_order": True}}
        return {"latest_absent": True} if start == 0 and not gaps else {}
    if latest is None and start > 0:
        message = _stored_message(row_matches_thread, stored_chat_id, child)
        entries, before, gaps = HistorySource(data_dir, "chat", page["lens"]["chat"], end).older(
            start, 1, message, min(_ARRIVAL_SEARCH_ROWS, _READ_CEILING_ROWS))
        found = next(filter(message, entries), None)  # ``older`` stops at the newest one
        if gaps and _skipped_row_after(data_dir, offset(found) if found else before, start):
            return {"latest_message": None}
        if not found and before > 0:
            page["quiet_below"] = True
            return {"latest_message": None, "latest_before": before}
        return {"latest_message": {"history_id": found["history_id"], "out_of_order": True}} if found else {}
    if latest is None:
        return {}
    ts = str(latest.get("ts") or "")
    return {"latest_message": {"history_id": latest["history_id"], "out_of_order": any(
        offset(row) < offset(latest) and str(row.get("ts") or "") > ts and not words(row) for row in feed)}}


def replay_evidence_rows(messages, evidence):
    """Carry cross-page evidence only when this page has no equivalent fact."""
    visible = {row.get("history_id") for row in messages if row.get("history_id")}
    settled = {(row.get("task_id"), row["quiz"].get("quiz_id"), row["quiz"].get("state")) for row in messages
               if row.get("msg_type") == "quiz" and row["quiz"].get("state") in {"answered", "closed"}}
    terminals = {row["task_id"]: row["historical_terminal"] for row in messages
                 if row.get("task_id") and row.get("historical_terminal")}
    return [row for row in evidence if row.get("history_id") not in visible
            and not (row.get("system_type") in {"quiz_answer", "quiz_closed"}
                     and (row.get("task_id"), row["quiz"].get("quiz_id"), row["quiz"].get("state")) in settled)
            and not (row.get("historical_terminal")
                     and terminals.get(row.get("task_id")) == row["historical_terminal"])]
