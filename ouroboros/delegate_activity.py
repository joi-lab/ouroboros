"""What a delegated executor said and did, kept typed on the existing progress carrier (#1350).

``delegate_progress`` observes a run by polling its detail, whose ``timeline`` rows carry
typed facts (text kind, delta, severity, actor) but no identity of their own. Flattening
them into one string before any consumer saw them made the executor's sentences sit behind
``Bash · tool_result`` labels, dropped severity, and left history with overlapping window
snapshots that no reader could reconcile. This module keeps the facts typed instead:

* **Source identity.** Each record covers an exact run-journal range, ``after_seq``
  (exclusive) to ``source.read_through`` (inclusive), read from the engine's own event
  stream (``gateways.claudexor_run_events``) whose ``seq`` is the same cursor
  ``delegate_wait`` already advances. Every event in the range belongs to exactly one
  part or to the technical seq runs; a joined delta part names each fragment's seq and
  Unicode code point offset (``cuts``). Within the disclosed cut/run budgets a reader
  can present each ``(run_id, seq)`` once however two records' ranges overlap; beyond
  them it discloses possibly repeated text/counts with the retained source. Equal
  texts or timestamps are never merged, and a range
  is never counted as if every seq in it were an event.
* **Retained originals.** The redacted lines exactly as served are stored as a task source
  handle (``artifacts.store_actor_source_bytes``, category ``delegated_activity``, in the
  canonical custody store) and the record names that ref; the task-file route serves it
  by its content address (``gateway.task_archive.serve_task_source``).
* **Commit after emission.** The per-run cursor moves only when the caller reports the
  frame emitted. A process that lost it resumes at the last retained range, re-reading
  that range once; a reader's seq identity keeps it shown once.
* **Honest gaps.** A range a bounded read did not reach is named by seq with its reason.
  A failed read does not advance the cursor: the record shows the bounded timeline window
  as provisional and the next read retries the exact range. At the run's end the wait
  drains the tail within its own read allowance, checking owner controls and remaining
  time before each catalog/stream read; an unread tail stays a final gap with its source.
  An engine without the stream keeps the window view.
* **Voice.** The executor's words stay attributed to the executor; the frame keeps
  ``narration=False``. An executor sentence is not evidence that a claim holds.

The record rides ``progress_meta["delegated_activity"]`` (validated by
``subagent_messages.delegated_activity_meta`` at emission, delivery and replay). The frame
text stays a speech-first plain rendering for text-only readers: Telegram, logs, the
model's recent-progress context and older clients.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ouroboros.utils import truncate_within_limit

log = logging.getLogger(__name__)

ACTIVITY_VERSION = 1
SOURCE_CATEGORY = "delegated_activity"
# Per-part preview bounds. The retained source keeps every original byte; these only
# size what one progress row carries, generous enough that ordinary narration is whole.
_TEXT_CHARS = {"message": 6000, "thinking": 2000, "problem": 1200}
_SPEECH_BUDGET = 24_000
_MAX_PARTS = 40
# Identity inside one record: fragment boundaries of joined parts, and runs of technical
# seqs. Beyond these the record says so (``cuts_truncated`` / ``seqs_truncated``).
_MAX_CUTS = 1000
_MAX_SEQ_RUNS = 500
# The carrier's own bound (``subagent_messages._ACTIVITY_MAX_CHARS``) with headroom.
_RECORD_CHARS = 60_000
_TOP_LABELS = 6
_RECENT_TECHNICAL = 24
_DETAIL_CHARS = 160
_RECAP_CHARS = 700
_LINE_MESSAGE_CHARS = 600
_PROGRESS_TEXT_CHARS = 2000
_RETRY_SEC = 0.5
_OMISSION = "\n⚠️ OMISSION NOTE: truncated at "
# The engine's typed error kinds beyond ``payload.error`` / ``tool.status == "error"``:
# mirrors ERROR_EVENT_TYPES in Claudexor control-api/src/run-timeline.ts (80ef3839).
_ERROR_RUN_EVENTS = frozenset({"run.failed", "reviewer.failed", "reviewer.timed_out"})
_MEMO_LIMIT = 256


@dataclass
class _RunMemo:
    """What this process committed to the human for one (task, run)."""

    shown_through: Optional[int] = None  # None: unknown here, resolved from retained sources
    latest_message: Dict[str, Any] = field(default_factory=dict)


_MEMO: "OrderedDict[Tuple[str, str], _RunMemo]" = OrderedDict()
_MEMO_LOCK = threading.Lock()


def reset_process_memo() -> None:
    """Forget what this process showed and which engines list the stream (tests reuse ids)."""
    from ouroboros.gateways import claudexor_run_events

    with _MEMO_LOCK:
        _MEMO.clear()
    with claudexor_run_events._SUPPORT_LOCK:
        claudexor_run_events._SUPPORT.clear()


def _memo(task_id: str, run_id: str) -> _RunMemo:
    with _MEMO_LOCK:
        memo = _MEMO.pop((task_id, run_id), None) or _RunMemo()
        _MEMO[(task_id, run_id)] = memo
        while len(_MEMO) > _MEMO_LIMIT:
            _MEMO.popitem(last=False)
        return memo


def _text(value: Any, limit: int) -> str:
    return truncate_within_limit(value, limit) if isinstance(value, str) else ""


def _actor(harness: Any, attempt: Any) -> str:
    return "/".join(str(v) for v in (harness, attempt) if isinstance(v, str) and v)[:120]


def _safe_run(run_id: str) -> str:
    # ``store_actor_source_bytes`` normalizes a source id this way; the seq suffix is never stripped.
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(run_id)).lstrip("._")


def _source_id(run_id: str, first: int, last: int) -> str:
    return f"{_safe_run(run_id)}-{first}-{last}"[:160]


# -- classification (typed fields only) ------------------------------------------------

def _event_item(seq: int, value: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """One served journal event as speech, problem or technical activity."""
    if value is None:
        return {"kind": "technical", "seq": seq, "actor": "", "label": "unreadable event line", "detail": ""}
    etype = str(value.get("type") or "event")
    payload = value.get("payload") if isinstance(value.get("payload"), dict) else {}
    tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
    actor = _actor(payload.get("harness_id") or payload.get("harness"),
                   payload.get("attempt_id") or payload.get("attemptId"))
    htype = str(payload.get("type") or "") if etype == "harness.event" else ""
    if htype in ("message", "thinking") and isinstance(payload.get("text"), str):
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        return {"kind": htype, "seq": seq, "actor": actor, "text": payload["text"],
                "delta": nested.get("delta") is True}
    if payload.get("error") or tool.get("status") == "error" or etype in _ERROR_RUN_EVENTS:
        text = next((v for v in (payload.get("error"), tool.get("error_summary"), payload.get("text"),
                                 payload.get("message"), payload.get("title")) if isinstance(v, str) and v), etype)
        return {"kind": "problem", "seq": seq, "actor": actor, "text": text,
                "label": str(tool.get("name") or htype or etype)[:120]}
    if htype == "tool_call":
        label, detail = str(tool.get("name") or htype), tool.get("target")
    elif htype == "tool_result":
        label, detail = "tool results", " ".join(str(v) for v in (tool.get("name"), tool.get("status")) if v)
    else:
        label = htype or etype
        detail = next((v for v in (payload.get("title"), payload.get("message"), payload.get("summary"))
                       if isinstance(v, str) and v and v != label), "")
    return {"kind": "technical", "seq": seq, "actor": actor, "label": label[:80],
            "detail": _text(detail, _DETAIL_CHARS)}


def _window_item(row: Dict[str, Any]) -> Dict[str, Any]:
    """One bounded timeline-window row (an engine without the stream): no seq exists."""
    actor = _actor(row.get("harnessId"), row.get("attemptId"))
    title = row.get("title")
    if row.get("textKind") in ("message", "thinking") and isinstance(title, str):
        return {"kind": row["textKind"], "actor": actor, "text": title, "delta": row.get("textDelta") is True}
    label = str(title or row.get("type") or "event")
    if row.get("severity") == "error":
        return {"kind": "problem", "actor": actor, "text": label, "label": str(row.get("type") or "")[:120]}
    return {"kind": "technical", "actor": actor, "label": label[:80], "detail": ""}


def _speech(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split items into ordered speech/problem parts and technical items.

    Only the engine's delta fact joins text, and only adjacent fragments of one stream
    (kind + actor); complete texts and every other event stay separate parts, as the
    engine's timeline contract states (``textDelta``). A joined part keeps its first and
    last seq, its fragment count and each later fragment's ``[seq, code_point_offset]``.
    """
    parts: List[Dict[str, Any]] = []
    technical: List[Dict[str, Any]] = []
    stream = None
    for item in items:
        kind = item["kind"]
        if kind in ("message", "thinking"):
            key = (kind, item["actor"])
            last = parts[-1] if parts else None
            if item["delta"] and last is not None and stream == key:
                if "seq" in item:
                    last.setdefault("cuts", []).append([item["seq"], len(last["text"])])
                    last["last_seq"] = item["seq"]
                last["text"] += item["text"]
                last["fragments"] += 1
            else:
                part = {"kind": kind, "actor": item["actor"], "text": item["text"]}
                if "seq" in item:
                    part.update(seq=item["seq"], last_seq=item["seq"])
                if item["delta"]:
                    part.update(delta=True, fragments=1)
                parts.append(part)
            stream = key if item["delta"] else None
            continue
        stream = None
        if kind == "problem":
            parts.append({key: item[key] for key in ("kind", "seq", "actor", "label", "text") if key in item})
        else:
            technical.append(item)
    return [part for part in parts if part["text"].strip()], technical


def _bounded(parts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Preview bounds per row: messages and problems claim the budget before thinking.

    A truncated part keeps only the fragment cuts inside its preview prefix; the record's
    cut budget is spent in order and a part it ran out on says ``cuts_truncated``.
    """
    budget, chosen, omitted = _SPEECH_BUDGET, set(), Counter()
    order = sorted(range(len(parts)), key=lambda i: parts[i]["kind"] == "thinking")
    for index in order:
        part = parts[index]
        cost = min(len(part["text"]), _TEXT_CHARS[part["kind"]])
        if len(chosen) >= _MAX_PARTS or cost > budget:
            omitted[part["kind"]] += 1
            continue
        budget -= cost
        chosen.add(index)
    kept, cuts_left = [], _MAX_CUTS
    for index, part in enumerate(parts):
        if index not in chosen:
            continue
        limit, chars = _TEXT_CHARS[part["kind"]], len(part["text"])
        part["chars"] = chars
        if chars > limit:
            part.update(text=truncate_within_limit(part["text"], limit), truncated=True)
        if "cuts" in part:
            marker = part["text"].rfind(_OMISSION) if part.get("truncated") else -1
            prefix = marker if marker >= 0 else len(part["text"])
            cuts = [cut for cut in part["cuts"] if cut[1] < prefix]
            if len(cuts) > cuts_left:
                cuts, part["cuts_truncated"] = cuts[:cuts_left], True
            cuts_left -= len(cuts)
            part["cuts"] = cuts
        kept.append(part)
    return kept, dict(omitted)


def _seq_runs(seqs: List[int]) -> List[List[int]]:
    runs: List[List[int]] = []
    for seq in seqs:
        if runs and seq == runs[-1][1] + 1:
            runs[-1][1] = seq
        else:
            runs.append([seq, seq])
    return runs


def _technical(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return {}
    counts = Counter(item["label"] for item in items)
    top = counts.most_common(_TOP_LABELS)
    recent = [{key: item[key] for key in ("seq", "actor", "label", "detail") if item.get(key) not in (None, "")}
              for item in items[-_RECENT_TECHNICAL:]]
    out: Dict[str, Any] = {"count": len(items), "labels": [[label, n] for label, n in top], "recent": recent}
    if len(counts) > len(top):
        out["other_kinds"] = len(counts) - len(top)
    if len(items) > len(recent):
        out["recent_omitted"] = len(items) - len(recent)
    seqs = [item["seq"] for item in items if "seq" in item]
    if seqs:
        runs = _seq_runs(seqs)
        out.update({"seqs": runs} if len(runs) <= _MAX_SEQ_RUNS else {"seqs_truncated": True})
    return out


def _fit(record: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the record inside the carrier bound, shedding identity detail before words."""
    def size() -> int:
        return len(json.dumps(record, ensure_ascii=False))

    technical = record.get("technical") or {}
    for shed in (lambda: [part.update(cuts=[], cuts_truncated=True) for part in record["parts"] if part.get("cuts")],
                 lambda: technical.pop("seqs", None) is not None and technical.update(seqs_truncated=True),
                 lambda: technical.update(recent=technical.get("recent", [])[-6:])):
        if size() <= _RECORD_CHARS:
            break
        shed()
    # JSON escaping can exceed the carrier even when code-point previews fit.
    # At most _MAX_PARTS removals; the source remains whole and every removed
    # preview is counted, so the binder never silently drops the entire record.
    while size() > _RECORD_CHARS and record["parts"]:
        part = record["parts"].pop()
        omitted = record.setdefault("omitted", {})
        omitted[part["kind"]] = omitted.get(part["kind"], 0) + 1
    return record


# -- the observation record --------------------------------------------------------------

def _persist(ctx: Any, task_id: str, run_id: str, events: List[Any]) -> Dict[str, Any]:
    """Retain the served redacted lines as one task source handle; ``{}`` when it failed."""
    try:
        from ouroboros.artifacts import store_actor_source_bytes, task_id_for_artifacts
        from ouroboros.delegate_custody import custody_root

        if task_id_for_artifacts(ctx) != task_id:
            return {}  # a store not bound to the record's own task publishes no ref
        data = "".join(event.data + "\n" for event in events).encode("utf-8")
        ref = store_actor_source_bytes(
            custody_root(ctx), task_id, category=SOURCE_CATEGORY,
            source_id=_source_id(run_id, events[0].seq, events[-1].seq), data=data, extension="jsonl")
        return {key: ref[key] for key in ("kind", "root", "path", "size", "sha256")}
    except Exception:
        log.warning("delegated activity source for %s could not be retained", run_id, exc_info=True)
        return {}


def _retained_range(ctx: Any, task_id: str, run_id: str) -> Optional[Tuple[int, int]]:
    """``(first, last)`` of this run's latest retained range in the task's own store."""
    try:
        from ouroboros.artifacts import task_artifact_dir_path
        from ouroboros.delegate_custody import custody_root

        directory = task_artifact_dir_path(custody_root(ctx), task_id) / "source_handles" / SOURCE_CATEGORY
        pattern = re.compile(re.escape(_safe_run(run_id)) + r"-(\d+)-(\d+)-[0-9a-f]{64}\.jsonl")
        best: Optional[Tuple[int, int]] = None
        with os.scandir(directory) as entries:
            for entry in entries:
                match = pattern.fullmatch(entry.name)
                if match and (best is None or int(match[2]) > best[1]):
                    best = (int(match[1]), int(match[2]))
        return best
    except (OSError, ValueError):
        return None


def _record(task_id: str, run_id: str, after: int, through: int, items: List[Dict[str, Any]],
            source: Dict[str, Any], gaps: List[Dict[str, Any]], memo: _RunMemo) -> Dict[str, Any]:
    parts, technical_items = _speech(items)
    parts, omitted = _bounded(parts)
    record: Dict[str, Any] = {"v": ACTIVITY_VERSION, "task_id": task_id, "run_id": run_id,
                              "after_seq": after, "through_seq": through, "source": source, "parts": parts}
    for key, value in (("omitted", omitted), ("technical", _technical(technical_items)), ("gaps", gaps)):
        if value:
            record[key] = value
    if memo.latest_message and not any(part["kind"] == "message" for part in parts):
        # Text-only readers (the Telegram card) keep the run's latest words, marked earlier.
        record["latest_message"] = dict(memo.latest_message)
    return _fit(record)


def _commit(memo: _RunMemo, record: Dict[str, Any], shown_through: Optional[int]) -> Callable[[], None]:
    def commit() -> None:
        if shown_through is not None:
            memo.shown_through = max(int(memo.shown_through or 0), shown_through)
        messages = [part for part in record["parts"] if part["kind"] == "message"]
        if messages:
            last = messages[-1]
            memo.latest_message = {key: last[key] for key in ("seq", "actor") if key in last}
            memo.latest_message.update(text=truncate_within_limit(last["text"], _RECAP_CHARS))
    return commit


def _owner_stop(ctx: Any, task_id: str) -> str:
    """The existing native owner and durable Stop/Panic facts; ``""`` when none."""
    try:
        from ouroboros.cancel_intents import cancel_pending
        from ouroboros.delegate_custody import custody_root
        from ouroboros.model_wait import current_model_wait
        from supervisor.state_initialization import confirm_absent

        owner = current_model_wait()
        if owner is not None and (reason := owner.control_reason()):
            return reason
        root = custody_root(ctx)
        try:
            for name in ("panic_stop.flag", "owner_restart_no_resume.flag"):
                confirm_absent(root / "state" / name)
        except OSError:  # present, linked or unknown is not a proven absent Stop flag
            return "panic"
        return "cancelled" if cancel_pending(root, task_id) else ""
    except Exception:
        log.debug("owner stop facts unreadable during a delegated drain", exc_info=True)
        return "control_unavailable"


def _read_allowance(ctx: Any, task_id: str, until: float, read_sec: float,
                    read_remaining: Optional[Callable[[], float]]) -> Tuple[float, str]:
    """Narrow to existing operation/task clocks, without minting a positive floor."""
    from ouroboros.deadline_utils import dispatch_window_remaining_sec
    from ouroboros.model_wait import current_model_wait, dispatch_deadline_remaining_sec
    from ouroboros.task_pacing import effective_finalization_reserve_sec

    stopped = _owner_stop(ctx, task_id)
    if stopped:
        return 0.0, f"owner_{stopped}"
    remaining = min(float(read_sec), until - time.monotonic())
    if read_remaining is not None:
        remaining = min(remaining, read_remaining())
    if remaining <= 0:
        return 0.0, "read_bound:time"
    metadata = getattr(ctx, "task_metadata", {}) or {}
    owner = current_model_wait()
    bounds = (dispatch_window_remaining_sec(deadline_at=metadata.get("deadline_at"),
                                           reserve_sec=effective_finalization_reserve_sec(ctx)),
              dispatch_deadline_remaining_sec(),
              owner.execution_window_remaining() if owner is not None else None)
    for bound in bounds:
        if bound is not None:
            remaining = min(remaining, bound)
    return (remaining, "") if remaining > 0 else (0.0, "owner_deadline")


def _gap(after: int, through: int, reason: str, final: bool) -> Dict[str, Any]:
    gap: Dict[str, Any] = {"after_seq": after, "through_seq": through, "reason": reason}
    return {**gap, "final": True, "where": "run_journal"} if final else gap


def observations(ctx: Any, gateway: Any, run_id: str, advance: Any, *, after_seq: int, read_sec: float,
                 drain_sec: Optional[float] = None,
                 read_remaining: Optional[Callable[[], float]] = None) -> Iterator[Tuple[Dict[str, Any], Callable[[], None]]]:
    """The typed records for one wait advance, each with the commit its emitter calls.

    Resumes from what this process committed for the run (else the last retained range,
    else the journal's beginning). The wait's ``after_seq`` is a wake cursor, not evidence
    that its earlier events reached this consumer. One bounded read per advance; with
    ``drain_sec`` (the run ended) the
    reads repeat within that allowance until the journal's end or the bound, retrying a
    failed read. A record is yielded before its cursor moves: an emitter that raises leaves
    the range to be read again. Without the stream, the advance's window rows are the view.
    ``read_remaining`` consults the caller's original operation clock before each request;
    in-flight HTTP socket phases retain the transport's bounds, not total-wall preemption.
    """
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.gateways.claudexor_run_events import read_run_events, run_events_supported

    task_id = str(getattr(ctx, "task_id", "") or "")
    memo = _memo(task_id, run_id)
    if memo.shown_through is None:
        retained = _retained_range(ctx, task_id, run_id)
        memo.shown_through = min(int(after_seq or 0), retained[0] - 1) if retained else 0
    through = int(advance.seq)
    final = drain_sec is not None
    until = time.monotonic() + max(0.0, float(drain_sec if final else read_sec))
    rows = [_window_item(row) for row in list(getattr(advance, "events", None) or [])]
    window = {"rows": len(rows), **({"rows_omitted": advance.events_omitted}
                                    if getattr(advance, "events_omitted", 0) else {})}
    after = int(memo.shown_through or 0)
    while True:
        if through <= after:
            return
        read, listed = None, True
        allowance, reason = _read_allowance(ctx, task_id, until, read_sec, read_remaining)
        try:
            if allowance > 0:
                listed = run_events_supported(gateway, timeout_sec=allowance)
                allowance, reason = _read_allowance(ctx, task_id, until, read_sec, read_remaining)
                if listed and allowance > 0:
                    read = read_run_events(gateway, run_id, after_seq=after, timeout_sec=allowance,
                                           through_seq=None if final else through)
        except ClaudexorUnavailable as exc:
            reason = f"stream_unavailable:{exc.code}"
        except Exception:
            log.debug("delegated activity read of %s failed", run_id, exc_info=True)
            reason = "stream_read_failed"
        if not listed:
            # No exact source on this engine at all: the bounded window is the whole view
            # and nothing better will come, so the cursor passes the range.
            record = _record(task_id, run_id, after, through, rows,
                             {"kind": "timeline_window", "reason": "stream_not_listed", **window}, [], memo)
            yield record, _commit(memo, record, through)
            return
        allowance, cut = _read_allowance(ctx, task_id, until, read_sec, read_remaining)
        if read is None:
            if final and allowance > 0:
                time.sleep(min(_RETRY_SEC, allowance))
                allowance, cut = _read_allowance(ctx, task_id, until, read_sec, read_remaining)
                if allowance > 0:
                    continue
            # The exact range stays unread and the cursor stays: this row is the window,
            # marked provisional, and a later exact record covering the range supersedes it.
            if not reason:
                reason = cut
            elif cut.startswith("owner_") and cut != reason:
                reason = f"{reason};{cut}"
            record = _record(task_id, run_id, after, through, rows,
                             {"kind": "timeline_window", "reason": reason, "provisional": True, **window},
                             [_gap(after, through, reason, final)], memo)
            yield record, _commit(memo, record, None)
            return
        through = max(through, read.through_seq)
        caught_up = read.through_seq >= through
        if not caught_up and not read.ended and not read.events and final and allowance > 0:
            time.sleep(min(_RETRY_SEC, allowance))
            continue  # a quiet read of an ended run: nothing to show yet, the allowance remains
        more = not caught_up and not read.ended and final and allowance > 0
        gap_reason = "stream_end_before_fence" if read.ended else cut or f"read_bound:{read.stop}"
        gaps = [] if caught_up or more else [_gap(read.through_seq, through, gap_reason, final)]
        source: Dict[str, Any] = {"kind": "run_events", "read_through": read.through_seq, "events": len(read.events)}
        if read.ended:
            source["ended"] = True
        if read.events:
            ref = _persist(ctx, task_id, run_id, read.events)
            source.update({"ref": ref} if ref else {"retained": False})
        record = _record(task_id, run_id, after, through,
                         [_event_item(event.seq, event.value) for event in read.events], source, gaps, memo)
        yield record, _commit(memo, record, read.through_seq)
        if not more:
            return
        # Resumed only after its emitter committed the record: the drain reads on from here.
        after, rows, window = read.through_seq, [], {"rows": 0}


# -- plain renderings (frame text, Telegram card) ----------------------------------------

def _actor_label(actor: str) -> str:
    return actor.replace("/", " · ") if actor else "executor"


def gap_line(gap: Dict[str, Any], run_id: str) -> str:
    """One unread range, as the frame text and the card state it."""
    span = f"seq {int(gap.get('after_seq', 0)) + 1}–{gap.get('through_seq')}"
    if gap.get("reason") == "stream_end_before_fence":
        return f"not shown: {span} (stream ended before the observed cursor); coverage unresolved on run {run_id}"
    if gap.get("final"):
        return (f"not shown: {span} ({gap.get('reason')}); the wait returned before reading them — "
                f"they remain on run {run_id}'s journal, and another delegate_wait on the run reads them")
    return f"not shown yet: {span} ({gap.get('reason')}); read at the run's next advance or its end"


def activity_lines(activity: Dict[str, Any], *, message_chars: int, recap: bool) -> List[str]:
    """Speech first, then problems, then counts, then gaps — one plain line each."""
    lines: List[str] = []
    parts = [part for part in activity.get("parts") or [] if isinstance(part, dict)]
    for part in parts:
        if part.get("kind") == "message":
            lines.append(f"{_actor_label(part.get('actor', ''))}: {_text(part.get('text'), message_chars)}")
    if recap and not lines and isinstance(activity.get("latest_message"), dict):
        earlier = activity["latest_message"]
        lines.append(f"{_actor_label(earlier.get('actor', ''))} (earlier): {_text(earlier.get('text'), message_chars)}")
    for part in parts:
        if part.get("kind") == "problem":
            label = " ".join(v for v in (_actor_label(part.get("actor", "")), part.get("label", "")) if v)
            lines.append(f"⚠ {label}: {_text(part.get('text'), 300)}")
    thinking = sum(1 for part in parts if part.get("kind") == "thinking")
    if thinking:
        lines.append(f"thinking ×{thinking} (in the task card's details)")
    technical = activity.get("technical") or {}
    if technical.get("count"):
        kinds = " · ".join(f"{label} ×{n}" for label, n in technical.get("labels") or [])
        lines.append(" · ".join(v for v in (f"{technical['count']} technical events", kinds) if v))
    source = activity.get("source") or {}
    if source.get("provisional"):
        cause = "the exact read failed" if str(source.get("reason", "")).startswith("stream_") else "the exact range was not read"
        lines.append(f"bounded timeline window shown meanwhile ({cause})")
    for gap in activity.get("gaps") or []:
        lines.append(gap_line(gap, str(activity.get("run_id") or "")))
    if source.get("rows_omitted"):
        lines.append(f"+{source['rows_omitted']} earlier timeline rows not shown (bounded window; the run's own timeline has them)")
    return lines


def progress_text(activity: Dict[str, Any]) -> str:
    """The frame text: speech-first, bounded, naming the retained source."""
    head = f"🛰 delegated run {activity['run_id']} @seq {activity['through_seq']}"
    lines = activity_lines(activity, message_chars=_LINE_MESSAGE_CHARS, recap=False)
    ref = (activity.get("source") or {}).get("ref") or {}
    if ref.get("path"):
        lines.append(f"source: {ref.get('root')}:{ref['path']}")
    return truncate_within_limit("\n".join([head, *lines]) if lines else f"{head}: (new session events)",
                                 _PROGRESS_TEXT_CHARS)


def card_text(activity: Dict[str, Any], limit: int) -> str:
    """Speech first; reserve the tail for problems, incomplete coverage and counts."""
    parts = [part for part in activity.get("parts") or [] if isinstance(part, dict)]
    speech = "\n".join(activity_lines({**activity, "parts": [p for p in parts if p.get("kind") == "message"],
                                      "technical": {}, "gaps": [], "source": {}}, message_chars=limit, recap=True))
    details = activity_lines({**activity, "parts": [p for p in parts if p.get("kind") != "message"]},
                             message_chars=limit, recap=False)
    omitted = [(kind, count) for kind, count in (activity.get("omitted") or {}).items() if count]
    if omitted:
        details.append("Not in this card: " + ", ".join(f"{count} {kind}" for kind, count in omitted))
    tail = "\n".join(details)
    if len(tail) > limit // 2:
        # Many diagnostics also need a compact summary; no category disappears
        # behind the speech preview. Telegram appends the full task reference.
        details = []
        problems = [p for p in parts if p.get("kind") == "problem"]
        if problems:
            details.append(f"⚠ {len(problems)} problems; first: {_text(problems[0].get('text'), 120)}")
        thinking = sum(p.get("kind") == "thinking" for p in parts)
        if thinking:
            details.append(f"thinking ×{thinking} (in the task card's details)")
        technical = activity.get("technical") or {}
        if technical.get("count"):
            details.append(f"{technical['count']} technical events")
        gaps, source = activity.get("gaps") or [], activity.get("source") or {}
        if gaps:
            first = gaps[0]
            details.append(f"not shown: {len(gaps)} unread ranges (first seq {int(first.get('after_seq', 0)) + 1}–{first.get('through_seq')})")
        if source.get("provisional"):
            details.append("Incomplete source: exact range not read; bounded window shown")
        if source.get("rows_omitted"):
            details.append(f"{source['rows_omitted']} earlier timeline rows not shown")
        if omitted:
            details.append("Not in this card: " + ", ".join(f"{count} {kind}" for kind, count in omitted))
        tail = "\n".join(details)
    preview = truncate_within_limit(speech, max(0, limit - len(tail) - bool(tail)))
    return "\n".join(part for part in (preview, tail) if part)
