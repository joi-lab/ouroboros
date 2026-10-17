"""A room's own pages over the existing retained chat/progress JSONL chains.

A page is counted in the room's rows, never in bytes of the shared chain: the
newest page holds the room's newest rows however far back they lie, and each
older page the next older ones (owner decisions 2026-10-05, DESIGN "History
edges"). The chat and progress streams keep separate quotas (``human``,
``progress``), each counted by its own predicate. Archives a Project's lens rules out are skipped unread
(``history_segments``); only a physical read ceiling can end a page early.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from bisect import bisect_left, bisect_right
from datetime import datetime
from pathlib import Path

from ouroboros import task_store
from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.gateway import _helpers
from ouroboros.gateway._helpers import _TAIL_WINDOW_START_BYTES
from ouroboros.gateway.history_segments import segment_summary
from ouroboros.jsonl_tail import JsonlChainSnapshot
from ouroboros.project_dialogue import _ANNOTATIONS_NAME, MAIN_PINNED_ROW_TYPES, ORIGIN_ADDRESSED_NOTICE_TYPES
from ouroboros.store import StoreError
from ouroboros.subagent_messages import CARD_ROW_PLACEMENTS, is_task_card_message, subagent_message_meta

_SOURCES = ("chat", "progress")
_READ_BYTES = 64 * 1024
# Physical bound on the bytes one request parses per stream; the cursor resumes
# where a read stopped, so it bounds latency, never what a room can reach.
_READ_CEILING_BYTES = 16 * 1024 * 1024
# Physical bound on what one ``after`` read replays per stream: a window that fell further
# behind (a client offline for hours) takes a fresh recent page instead (``history_delta_reset``).
_DELTA_CEILING_BYTES = 2 * 1024 * 1024
# The newest-arrival search every recent Project read pays for; a bound reached
# first is carried on by the chain's cursors (``latest_before``/``quiet``).
_ARRIVAL_SEARCH_BYTES = 512 * 1024
_CHAIN_WITNESSES = 16
# Bound on an opaque cursor's length (a query parameter): an ``after`` cursor carries the versions
# of the tasks its window shows, and a window too large to name them reads its newest page instead.
_CURSOR_CHARS = 8192
# A projection that failed part-way: the rows after the failing one are missing
# from the response, so neither its coverage nor its newest arrival is known.
PROJECTION_FAILED = "projection_failed"
# Stored kinds chat.js shows as a bubble of their own whoever sent them: it
# renders them before a child's words are routed to its card, and their
# producers (message_bus ``send_photo`` ...) count every one.
_STANDALONE_KINDS = ("document", "links", "photo", "video", "quiz")
# A read around one task (``around=task:<id>``, DESIGN "Room work status") holds a quarter of
# each stream's quota before the task's earliest row in the room and the rest from it on.
_AROUND_OLDER_SHARE = 4


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
    """The task an around read lands on: ``task:<task_id>``, the one address it takes (400 otherwise)."""
    kind, _sep, task_id = str(value or "").partition(":")
    if kind != "task" or not task_id or len(task_id) > 128 or any(char.isspace() for char in task_id):
        raise HistoryCursorError("history_around_invalid", 400)
    return task_id


def task_began(data_dir, task_id):
    """When the task's stored facts say it began (epoch seconds), or None when they cannot say."""
    try:
        stamp = task_store.task_facts(data_dir, where="task_id = ?", params=(task_id,)).get(task_id, {}).get("ts")
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp() if stamp else None
    except (StoreError, ValueError):
        return None


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
        stat = (Path(data_dir) / "logs" / _ANNOTATIONS_NAME).stat()
    except OSError:
        return ""
    return f"{stat.st_ino}:{stat.st_size}"


class HistorySource(JsonlChainSnapshot):
    """One metadata snapshot, with no handles retained across a read batch."""

    def __init__(self, path: Path, source: str, upper=None, unfinished=False):
        super().__init__(path, upper=upper)
        self.source = source
        self.warm = None  # the room rule's lineage batch read (``RoomResolver.warm``), before rows are counted
        # Bytes after the frozen boundary that a writer had not finished when it
        # was frozen (a cursor carries the fact with ``upper``): every read that
        # reaches the boundary discloses them (``incomplete_live_line``), a clean EOF has none.
        self.unfinished_live_line = bool(unfinished)
        if upper is None and self.snapshot["entries"] and self.snapshot["entries"][-1][2]:
            base, end = self.segment(len(self.entries) - 1)
            # A writer owns an unfinished live line. Freeze only complete rows,
            # so completing/rotating that line later cannot alter this page.
            eof = end
            while end > base and self._read(end - 1, end) != b"\n":
                start = max(base, end - _READ_BYTES)
                data = self._read(start, end)
                newline = data.rfind(b"\n")
                end = start + newline + 1 if newline >= 0 else start
            self.upper, self.unfinished_live_line = end, end < eof

    def _boundary_gaps(self, end):
        return {"incomplete_live_line"} if self.unfinished_live_line and end == self.upper else set()

    def _warmed(self, rows):
        """``rows``, after their tasks' lineage (and their ancestors') was read in batches."""
        if self.warm is not None and rows:
            self.warm({str(row.get(key) or "") for row in rows for key in ("task_id", "parent_task_id", "root_task_id")
                       if isinstance(row, dict)} - {""})
        return rows

    def _entries(self, start, end, gaps):
        data = self._read(start, end)
        position = start
        stream = self.source
        is_live_end = bool(self.snapshot["entries"] and self.snapshot["entries"][-1][2]
                           and end == self.upper)

        class Lines:
            row_start = row_end = start

            def __iter__(self):
                nonlocal position
                for raw in io.BytesIO(data):
                    self.row_start, self.row_end = position, position + len(raw)
                    position = self.row_end
                    if not raw.endswith(b"\n") and is_live_end:
                        gaps.add("incomplete_live_line")
                        continue
                    yield raw

        lines = Lines()
        rows = []
        for entry in _helpers.iter_jsonl_objects(self.path, _handle=lines, gap_reasons=gaps):
            from ouroboros.chat_references import projection_fields
            rows.append({**entry, **projection_fields(entry), "history_id": f"{stream}:{lines.row_start}",
                         "history_position": {"source": stream, "offset": lines.row_start},
                         "_history_end": lines.row_end})
        return rows

    def _aligned_start(self, start, end, base):
        if start <= base or self._read(start - 1, start) == b"\n":
            return start
        data = self._read(start, end)
        newline = data.find(b"\n")
        return end if newline < 0 else start + newline + 1

    def _ruled_out(self, index, lens):
        """A closed archive whose summary proves it holds none of the room's rows."""
        path, stat, live = self.snapshot["entries"][index]
        if lens is None or live:
            return False
        summary = segment_summary(path, stat)
        return summary is not None and not lens(summary)

    def recent(self, want, counts, lens=None):
        """The room's newest rows: the live tail, then whole archives newest-first
        until ``want`` counted rows, the chain's start or the read ceiling."""
        entries, before, counted, read = [], self.upper, 0, 0
        gaps = self._boundary_gaps(self.upper)
        for index in reversed(range(len(self.snapshot["entries"]))):
            base, end = self.segment(index)
            if base >= self.upper:
                continue
            live = self.snapshot["entries"][index][2]
            if not live and (counted >= want or read >= _READ_CEILING_BYTES):
                break  # the live tail is always read: its overlays need no quota
            if self._ruled_out(index, lens):
                before = base
                continue
            window = _TAIL_WINDOW_START_BYTES if live else end - base
            while True:
                start = self._aligned_start(max(base, end - window), end, base)
                selected = self._warmed(self._entries(start, end, gaps))
                found = sum(map(counts, selected))
                if start == base or counted + found >= want:
                    break
                window *= 2
            counted, read = counted + found, read + end - start
            entries = selected + entries
            before = start
        return entries, before, gaps

    def older(self, before, want, counts, lens=None, ceiling=None):
        """Consume one backward page of ``want`` counted rows; foreign/invalid bytes
        and ruled-out archives advance the position, the read ceiling ends it early."""
        if want <= 0:
            return [], before, set()
        ceiling = _READ_CEILING_BYTES if ceiling is None else ceiling
        selected, gaps, counted, read = [], self._boundary_gaps(before), 0, 0
        while before > 0 and read < ceiling:
            index = bisect_left(self.snapshot["ends"], before)
            base, _end = self.segment(index)
            if self._ruled_out(index, lens):
                before = base
                continue
            window = _READ_BYTES
            while True:
                start = self._aligned_start(max(base, before - window), before, base)
                if start < before or start == base:
                    break
                window *= 2  # one large JSONL row keeps its existing support
            entries = self._warmed(self._entries(start, before, gaps))
            for entry in reversed(entries):
                selected.append(entry)
                read += before - entry["history_position"]["offset"]
                before = entry["history_position"]["offset"]
                counted += bool(counts(entry))
                if counted >= want or read >= ceiling:
                    return list(reversed(selected)), before, gaps
            read += before - start
            before = start
        return list(reversed(selected)), before, gaps

    def task_pivot(self, task_id, member, since=None, lens=None, in_room=None):
        """``(state, row)`` of ``task_id`` in this stream: ``found`` and its earliest row the room draws,
        ``moved`` when its rows here belong to another room (``in_room`` says which rows the room's rule
        keeps), ``none`` when the stream holds none the room could show.
        Oldest first; a closed archive is read only
        when its summary may name the task, it was written to after the task began (``since``) and
        the room's lens admits it, so a press costs the archives of the task's own time."""
        self._warmed([{"task_id": task_id}])  # its lineage, before membership is judged
        seen = False
        for index, (path, stat, live) in enumerate(self.snapshot["entries"]):
            base, end = self.segment(index)
            if base >= end:
                continue
            if not live:
                if since is not None and stat.st_mtime < since or self._ruled_out(index, lens):
                    continue  # last written before the task began, or holding none of the room's rows
                summary = segment_summary(path, stat)
                if summary is not None and task_id not in summary.task_ids:
                    continue
            for row in self._entries(base, end, set()):
                if str(row.get("task_id") or "") != task_id:
                    continue
                if member(row):
                    return "found", row
                seen = seen or in_room is None or not in_room(row)
        return ("moved" if seen else "none"), None

    def ahead(self, start, want, counts, gaps, lens=None, ceiling=None):
        """The room's rows from ``start`` on, oldest first, until ``want`` counted rows, the chain's
        end or the read ceiling (the mirror of ``older``); returns them and where the read stopped."""
        ceiling = _READ_CEILING_BYTES if ceiling is None else ceiling
        rows, counted, read, position = [], 0, 0, start
        while position < self.upper and counted < want and read < ceiling:
            index = bisect_right(self.snapshot["ends"], position)
            _base, end = self.segment(index)
            if self._ruled_out(index, lens):
                position = end
                continue
            window, stop = _READ_BYTES, end
            while position + window < end:
                newline = self._read(position, position + window).rfind(b"\n")
                if newline >= 0:
                    stop = position + newline + 1
                    break
                window *= 2  # one large JSONL row keeps its existing support
            for entry in self._warmed(self._entries(position, stop, gaps)):
                rows.append(entry)
                counted += bool(counts(entry))
                if counted >= want:
                    return rows, entry["_history_end"]
            read += stop - position
            position = stop
        return rows, position

    def replay(self, lower, before, lens=None):
        """Re-read one frozen page; the archives its first read ruled out stay unread
        (same room view, immutable archives), so a replay costs what that read did."""
        gaps, rows = self._boundary_gaps(before), []
        for index in range(len(self.snapshot["entries"])):
            base, end = self.segment(index)
            start, end = max(base, lower), min(end, before)
            if start < end and not self._ruled_out(index, lens):
                rows.extend(self._warmed(self._entries(start, end, gaps)))
        return rows, lower, gaps


def chain_witness(reader):
    """Rolling prefix witnesses through the trailing retained segments below ``upper``.

    Physical byte coordinates survive append and rotation: the live file keeps
    its inode and base when renamed into the archive. Every witness rolls each
    earlier nonempty segment's identity and base, so replacing, removing or
    resizing ANY earlier segment changes it. A span stays comparable while its
    own last witness is still listed by a newer read, i.e. within
    ``_CHAIN_WITNESSES`` rotations; older spans are disclosed as gaps. Metadata
    only: no archive is read to establish it. Empty sources have no prefix.
    """
    witnesses, digest = [], b""
    for index, (_, stat, _) in enumerate(reader.entries):
        base = reader.ends[index - 1] if index else 0
        if base >= reader.upper:
            break
        if stat.st_size:
            digest = hashlib.sha256(digest + f"{stat.st_dev}:{stat.st_ino}@{base}".encode()).digest()
            witnesses.append(digest.hex()[:16])
    return ".".join(witnesses[-_CHAIN_WITNESSES:]) or "empty"


def select_history_page(data_dir, thread_id, view, cursor, quotas, predicates, caps, lens=None, notes="", warm=None,
                        around=None, thread_filter=None):
    """``lens``: the room's archive filter (``history_segments.room_segment_lens``); ``notes``:
    ``notes_stamp``; ``warm``: the room rule's lineage batch read, given rows before they are counted;
    ``around``: ``task:<id>``, the page around that task (``_select_around``); ``thread_filter``: the
    room rule and stored-chat coercion it decides membership with (``room_member_predicate``)."""
    if around is not None:
        if cursor:
            raise HistoryCursorError("history_around_invalid", 400)  # a page starts at one place
        return _select_around(data_dir, thread_id, view, parse_around(around), quotas, predicates, lens, notes, warm,
                              room_member_predicate(*thread_filter))
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
    selections, upper, before, page_ends, chains, unfinished = {}, {}, {}, {}, {}, []
    paths = {"chat": data_dir / "logs" / "chat.jsonl", "progress": data_dir / "logs" / "progress.jsonl"}
    for source, quota in (("chat", "human"), ("progress", "progress")):
        try:
            # Rows an ``after`` read has not seen are everything appended after the window's last read,
            # so it freezes the chain fresh; every other read keeps the boundary its cursor froze.
            reader = HistorySource(paths[source], source,
                                   continuation["upper"][source] if continuation and not after else None,
                                   bool(continuation) and not after and source in continuation["unfinished"])
            reader.warm = warm
            upper[source] = reader.upper
            chains[source] = chain_witness(reader)
            if reader.unfinished_live_line:
                unfinished.append(source)
            page_ends[source] = continuation["before"][source] if continuation and not after else reader.upper
            if after:
                seen = continuation["upper"][source]
                if not seen <= reader.upper or reader.upper - seen > _DELTA_CEILING_BYTES:
                    raise HistoryCursorError("history_delta_reset")
                selections[source] = reader.replay(seen, reader.upper, lens)
                if selections[source][2] - {"incomplete_live_line"}:  # rows it could not read stay unaccounted for
                    raise HistoryCursorError("history_delta_reset")
            elif continuation and continuation["kind"] == "page":
                selections[source] = reader.replay(continuation["lower"][source], page_ends[source], lens)
            elif continuation:
                selections[source] = reader.older(page_ends[source], quotas[quota], predicates[source], lens)
            else:
                selections[source] = reader.recent(quotas[quota], predicates[source], lens)
        except OSError:
            if continuation:
                raise
            # The existing recent collector can still show readable rows. An
            # unknown chain prefix cannot establish global offsets or EOF.
            selections[source] = (None, 0, {"source_unavailable"})
        before[source] = selections[source][1]
    return {"v": 1, "chat_id": thread_id, "view": view, "upper": upper, "unfinished": unfinished,
            "quotas": quotas, "recent": recent, "after": after, "replayed": bool(continuation),
            "quiet": bool(continuation and continuation["quiet"]), "lens": lens, "notes": notes,
            "selections": selections, "before": before, "page_ends": page_ends, "chains": chains,
            **({key: continuation[key] for key in ("tasks", "newest", "lifecycle")} if after else {})}


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


def _select_around(data_dir, thread_id, view, task_id, quotas, predicates, lens, notes, warm, member):
    """The page around one task in this room (DESIGN "Room work status"): per stream, a quarter of
    the quota of the room's rows before the task's earliest row of the room, that row, and the rest
    after it -- never a walk of the chain from the present. ``anchor`` says what was found:
    ``found`` and the row a client lands on, ``moved`` (the task's rows belong to another room) or
    ``absent`` (the chain holds no row of it a client could show). A stream holding none of the task's
    rows has no known place near it: the page and the chain it starts carry none of that stream (its
    quota is 0, so its coverage is unknown) and the page names the gap (``around_unpositioned``)."""
    paths = {"chat": data_dir / "logs" / "chat.jsonl", "progress": data_dir / "logs" / "progress.jsonl"}
    quotas = dict(quotas)
    page = {"v": 1, "chat_id": thread_id, "view": view, "upper": {}, "unfinished": [], "quotas": quotas,
            "recent": False, "after": False, "replayed": False, "quiet": False, "lens": lens, "notes": notes,
            "selections": {}, "before": {}, "page_ends": {}, "chains": {}}
    since, readers, pivots, states = task_began(data_dir, task_id), {}, {}, set()
    for source in _SOURCES:
        reader = readers[source] = HistorySource(paths[source], source)
        reader.warm = warm
        page["upper"][source], page["chains"][source] = reader.upper, chain_witness(reader)
        if reader.unfinished_live_line:
            page["unfinished"].append(source)
        progress = source == "progress"
        state, pivots[source] = reader.task_pivot(
            task_id, lambda row, progress=progress: member(row, progress), since, lens,
            lambda row, progress=progress: member.in_room(row, progress))
        states.add(state)
    for source, quota in (("chat", "human"), ("progress", "progress")):
        reader, pivot, counts, want = readers[source], pivots[source], predicates[source], quotas[quota]
        if pivot is None or not any(pivots.values()):
            page["selections"][source], quotas[quota] = ([], 0, {"around_unpositioned"}), 0
            page["before"][source] = page["page_ends"][source] = 0
            continue
        start, older_want, gaps = pivot["history_position"]["offset"], want // _AROUND_OLDER_SHARE, set()
        older, lower, older_gaps = reader.older(start, older_want, counts, lens) if older_want else ([], start, set())
        newer, end = reader.ahead(pivot["_history_end"], want - older_want - bool(counts(pivot)), counts, gaps, lens)
        page["selections"][source] = (older + [pivot] + newer, lower, gaps | older_gaps | reader._boundary_gaps(end))
        page["before"][source], page["page_ends"][source] = lower, end
    anchor = pivots["chat"] or pivots["progress"]
    page["anchor"] = ({"state": "found", "task_id": task_id, "history_id": anchor["history_id"]} if anchor else
                      {"state": "moved" if "moved" in states else "absent", "task_id": task_id})
    return page


def history_page_coverage(page, stream_gaps):
    """Delivered physical spans, called AFTER quota/lineage deferrals.

    A projected origin, detail overlay or shared row ID proves no scanned span.
    Quota-disabled and unreadable streams are unknown, even when they emit []
    and no continuation. Parse gaps remain part of the coverage evidence; the
    unfinished live line a frozen boundary discloses lies after ``upper``, so
    it is no gap in a span that ends there.
    """
    return {"v": 1, "view": page["view"], "upper": dict(page["upper"]), "spans": {
        source: ({"from": page["before"][source], "to": page["page_ends"][source],
                  "chain": page["chains"][source], "gaps": sorted(stream_gaps[source] - (
                      {"incomplete_live_line"} if source in page["unfinished"] else set()))}
                 if page["selections"][source][0] is not None
                 and page["quotas"]["human" if source == "chat" else source] else None)
        for source in _SOURCES}}


def history_page_tokens(page, messages=(), reads=None):
    """The conversation decides whether older history remains: narration (progress)
    keeps paging alongside it but never keeps a press alive on its own, so no press
    lands narration alone. A narration-only read (no human quota) pages narration.

    The ``after`` cursor also carries what its window shows besides rows: the version of each
    task the projection read (``reads``, the page's ``TaskReads``), the newest message's time and
    whether a running skill operation was shown. A later ``after`` read refuses when a shown task
    settled or changed without running, or the operation is gone (``history_delta_reset``)."""
    if any(selection[0] is None for selection in page["selections"].values()):
        return {"has_more": True, "next_cursor": None, "page_cursor": None,
                "reason_code": "history_source_unavailable"}
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


def _skipped_row_after(entries, after, upper):
    """Whether the physical selection skipped a row at or after ``after``."""
    position = after
    for start, end in sorted((entry["history_position"]["offset"], entry["_history_end"])
                             for entry in entries if entry["history_position"]["offset"] >= after):
        if start != position:
            return True
        position = end
    return position != upper


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
    """``window.latest_message`` of a recent Project read (DESIGN "Project unread dot").

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
    delivers is shown alone and is a message. ``None``: its arrival is unknown — the chat source
    is unreadable, its projection failed part-way (``projected_gaps`` holds
    ``PROJECTION_FAILED``: any row after the fault may be the newest), a row
    after the newest readable one is unreadable, or the live chat ends in
    a line its writer has not finished (``incomplete_live_line``: this read froze
    before it, so it may be the newest message) — and on a replayed recent page,
    which cannot see what arrived after its frozen boundary.

    Card rows, a child's words and the owner's own messages can fill the recent
    selection's quota. One bounded older read of the room (``_ARRIVAL_SEARCH_BYTES``)
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
    if entries is None or PROJECTION_FAILED in projected_gaps or "incomplete_live_line" in gaps or (
            gaps and _skipped_row_after(entries, offset(latest) if latest else start, end)):
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
        try:
            entries, before, gaps = HistorySource(data_dir / "logs" / "chat.jsonl", "chat", end).older(
                start, 1, message, page.get("lens"), min(_ARRIVAL_SEARCH_BYTES, _READ_CEILING_BYTES))
        except OSError:
            return {"latest_message": None}
        found = next(filter(message, entries), None)  # ``older`` stops at the newest one
        if gaps and _skipped_row_after(entries, offset(found) if found else before, start):
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
