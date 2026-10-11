"""Small, typed focus records shared by live-root projections.

Focus is an authored pointer, not a second transcript or an authority channel.
The helpers here deliberately reject payloads that could smuggle dialogue,
attachments, or filesystem contents into the cross-focus projections.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from ouroboros.project_facts import explicit_project_id_ok
from ouroboros.utils import utc_now_iso

_MAX_TEXT = 280
_MAX_SOURCE = 512
_MAX_HANDLE = 640
_HANDLE_PATH_PREFIX = "source_handles/context_checkpoints/"
_HANDLE_KEYS = {"kind", "root", "path", "size", "sha256"}

# A focus source is a pointer to one of the existing bounded readers.  Keep
# this contract deliberately small: source references are metadata, not a new
# reader, path language, or free-form payload channel.
_SOURCE_FIELDS = {
    "journal_read": {"project_id", "offset", "snapshot"},
    "workpad_read": {"project_id"},
    "recent_tasks": {"offset", "snapshot"},
    "get_task_result": {"task_id"},
    "chat_history": {"offset", "snapshot"},
    "live_roots": {"offset", "snapshot"},
}
_SOURCE_REQUIRED = {
    "journal_read": {"project_id"},
    "workpad_read": {"project_id"},
    "get_task_result": {"task_id"},
}


def _safe_source(value: Any) -> Optional[Any]:
    if not isinstance(value, Mapping):
        return None
    try:
        source = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError):
        return None
    if not isinstance(source, dict):
        return None
    # Provider-valid calls may spell an omitted optional field as "": treat
    # that exactly like omission so the schema and the handler agree.
    source = {key: item for key, item in source.items()
              if not (isinstance(item, str) and not item.strip() and key != "reader")
              and not (key == "offset" and item == 0 and not isinstance(item, bool))}
    reader = source.get("reader")
    if not isinstance(reader, str) or reader not in _SOURCE_FIELDS:
        return None
    keys = set(source)
    if "reader" not in keys or not keys.issubset({"reader", *_SOURCE_FIELDS[reader]}):
        return None
    if not _SOURCE_REQUIRED.get(reader, set()).issubset(keys):
        return None
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        return None
    for key, item in source.items():
        if key == "reader":
            continue
        if key == "offset":
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return None
        elif key == "project_id":
            if not isinstance(item, str) or not explicit_project_id_ok(item):
                return None
        elif key == "task_id":
            try:
                from ouroboros.task_results import validate_task_id

                if not isinstance(item, str):
                    return None
                validate_task_id(item)
            except (ImportError, TypeError, ValueError):
                return None
        elif not isinstance(item, str) or not item.strip() or "\x00" in item:
            return None
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_SOURCE:
        return None
    return source


def _safe_handle(value: Any) -> Optional[Dict[str, Any]]:
    """Validate the retained-source pointer a focus carries.

    A source_ref names a reader; the handle proves what that reader ANSWERED at
    authoring time: exact bytes stored write-once in the author task's
    source-handle store on the CANONICAL data root, in the native ``task_source``
    shape ``artifacts.read_actor_source_bytes`` verifies (size + sha256, no
    symlink, no escape). Peers read it through
    ``get_task_result(task_id=<author>, include_focus_source=True)`` — the one
    cross-task reader — so the pointer keeps identifying its evidence after the
    author is dormant, from any drive. The shape is closed: no free path
    language, no foreign root.
    """
    if not isinstance(value, Mapping):
        return None
    handle = dict(value)
    if set(handle) != _HANDLE_KEYS:
        return None
    if handle.get("kind") != "task_source" or handle.get("root") != "artifact_store":
        return None
    path = handle.get("path")
    if not isinstance(path, str) or not path.startswith(_HANDLE_PATH_PREFIX):
        return None
    parts = path.split("/")
    if len(parts) != 3 or any(part in ("", ".", "..") for part in parts) or "\\" in path or "\x00" in path:
        return None
    size = handle.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    digest = handle.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    encoded = json.dumps(handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_HANDLE:
        return None
    return handle


def normalize_focus(text: Any, source_ref: Any, *, task_id: str = "", authored_at: str = "",
                    source_handle: Any = None) -> Dict[str, Any]:
    body = str(text or "").strip()
    if not body:
        raise ValueError("focus text is required")
    if len(body) > _MAX_TEXT:
        raise ValueError(f"focus text exceeds {_MAX_TEXT} characters")
    source = _safe_source(source_ref)
    if source is None:
        raise ValueError("source_ref must name an existing typed reader")
    focus = {
        "text": body,
        "source_ref": source,
        "authored_at": str(authored_at or utc_now_iso()),
        "author_task_id": str(task_id or ""),
    }
    if source_handle is not None:
        handle = _safe_handle(source_handle)
        if handle is None:
            raise ValueError("source_handle must be a retained task_source pointer")
        if not focus["author_task_id"]:
            raise ValueError("source_handle requires an authoring task")
        focus["source_handle"] = handle
    return focus


def compact_focus(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    authored_at = str(value.get("authored_at") or "").strip()
    if not authored_at:
        return None
    try:
        return normalize_focus(
            value.get("text"), value.get("source_ref"),
            task_id=str(value.get("author_task_id") or ""),
            authored_at=authored_at,
            source_handle=value.get("source_handle"),
        )
    except ValueError:
        return None


def focus_fingerprint(value: Any) -> str:
    compact = compact_focus(value)
    if compact is None:
        return ""
    return json.dumps(compact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["compact_focus", "focus_fingerprint", "normalize_focus"]
