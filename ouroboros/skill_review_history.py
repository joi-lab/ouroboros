"""Tolerant reader of the append-only ``review_history.jsonl`` an earlier version wrote.

Rows were appended per lifecycle terminal (one per ``job_id``) until the skill review joined the
shared review mechanism (owner, 21.10): since then a skill review's durable record is the review
ledger (``surface="skill"``) and nothing writes here. The Skills card's history and the chat card's
detail of an old row still read it, bounded as before: every reader windows the 4 MB tail, and
``review_round`` / ``snapshot_attempt`` / ``snapshot_revised`` of rows that never persisted them are
derived at read time (``normalize_history``). Rows are never rewritten.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Iterator, List, Optional

from ouroboros.utils import iter_jsonl_objects

_DETAIL_LOOKUP_MAX_BYTES = 4 * 1024 * 1024
_DETAIL_LOOKUP_MAX_RECORDS = 128


def review_history_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "skills" / skill_name / "review_history.jsonl"


def _ordinal(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalize_bounded_ordinals(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize stored ordinals when a bounded read cannot derive them."""
    normalized = dict(record)
    for key in ("review_round", "snapshot_attempt"):
        try:
            normalized[key] = max(0, int(record.get(key)))
        except (TypeError, ValueError, OverflowError):
            return None
    normalized["snapshot_revised"] = bool(record.get("snapshot_revised"))
    return normalized


def normalize_history(entries: List[Dict[str, Any]], skill_name: str) -> List[Dict[str, Any]]:
    """Add read-time ordinals to legacy rows without rewriting the audit log."""
    group_rounds: Dict[str, int] = {}
    snapshot_attempts: Dict[tuple[str, str], int] = {}
    last_hash: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []
    for source in entries:
        entry = dict(source)
        group_id = str(entry.get("group_id") or f"manual:{skill_name}")
        content_hash = str(entry.get("content_hash") or "")
        review_round = max(group_rounds.get(group_id, 0) + 1, _ordinal(entry.get("review_round")))
        group_rounds[group_id] = review_round
        attempt_key = (group_id, content_hash)
        snapshot_attempt = max(snapshot_attempts.get(attempt_key, 0) + 1, _ordinal(entry.get("snapshot_attempt")))
        snapshot_attempts[attempt_key] = snapshot_attempt
        revised = bool(last_hash.get(group_id) and last_hash[group_id] != content_hash)
        if content_hash:
            last_hash[group_id] = content_hash
        entry.update(group_id=group_id, review_round=review_round, snapshot_attempt=snapshot_attempt,
                     snapshot_revised=bool(entry.get("snapshot_revised", revised)))
        out.append(entry)
    return out


def load_history(drive_root: pathlib.Path, skill_name: str, limit: int = 3, *, group_id: str = "") -> List[Dict[str, Any]]:
    """History rows inside the bounded tail window; ``limit=0`` is every row of the window."""
    try:
        entries = normalize_history(
            [row for row in iter_jsonl_objects(review_history_path(drive_root, skill_name), tail_bytes=_DETAIL_LOOKUP_MAX_BYTES)
             if isinstance(row, dict)], skill_name)
    except OSError:
        return []
    if group_id:
        entries = [entry for entry in entries if entry.get("group_id") == group_id]
    return entries[-limit:] if limit > 0 else entries


def iter_history_rows_bounded(drive_root: pathlib.Path, skill_name: str) -> Iterator[Dict[str, Any]]:
    """RAW history rows of one skill inside the same bounded tail window."""
    for row in iter_jsonl_objects(review_history_path(pathlib.Path(drive_root), skill_name), tail_bytes=_DETAIL_LOOKUP_MAX_BYTES):
        if isinstance(row, dict):
            yield row


def find_history_job_bounded(drive_root: pathlib.Path, skill_name: str, job_id: str) -> tuple[Optional[Dict[str, Any]], str]:
    """Find one immutable terminal row inside a fixed tail window: ``(row, status)`` with ``status`` one of
    ``found`` / ``not_found`` / ``absent`` / ``unavailable`` (outside the bounded window) / ``io_error``."""
    path = review_history_path(drive_root, skill_name)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "io_error"
    if size <= 0:
        return None, "absent"
    byte_window_truncated = size > _DETAIL_LOOKUP_MAX_BYTES
    gaps: set[str] = set()
    try:
        parsed = list(iter_jsonl_objects(path, max_entries=_DETAIL_LOOKUP_MAX_RECORDS + 1,
                                         tail_bytes=_DETAIL_LOOKUP_MAX_BYTES, gap_reasons=gaps))
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "io_error"
    records_truncated = len(parsed) > _DETAIL_LOOKUP_MAX_RECORDS
    if records_truncated:
        parsed = parsed[-_DETAIL_LOOKUP_MAX_RECORDS:]
    source_record = next((row for row in reversed(parsed) if str(row.get("job_id") or "") == job_id), None)
    # max_entries counts raw tail lines while parsed contains valid objects: a malformed or partial raw
    # row can hide ordinal-bearing authority just as surely as byte or record truncation.
    projection_incomplete = byte_window_truncated or records_truncated or bool(gaps)
    if source_record is not None:
        ordinal_fields = ("review_round", "snapshot_attempt", "snapshot_revised")
        if projection_incomplete and any(key not in source_record for key in ordinal_fields):
            return None, "unavailable"
        if projection_incomplete:
            normalized = _normalize_bounded_ordinals(source_record)
            return (normalized, "found") if normalized is not None else (None, "unavailable")
        record = next((row for row in reversed(normalize_history(parsed, skill_name))
                       if str(row.get("job_id") or "") == job_id), None)
        return record, "found"
    if projection_incomplete:
        return None, "unavailable"
    return None, "not_found" if parsed else "absent"


__all__ = ["find_history_job_bounded", "iter_history_rows_bounded", "load_history", "normalize_history", "review_history_path"]
