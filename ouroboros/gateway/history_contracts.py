"""Descriptive Chat history response, re-exported by the gateway facade."""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from typing import TypedDict
except ImportError:  # pragma: no cover - Python 3.10 compatibility.
    from typing_extensions import TypedDict


class ChatHistoryResponse(TypedDict, total=False):
    messages: list[Dict[str, Any]]
    has_more: bool
    next_before_ts: str
    next_cursor: Optional[str]
    page_cursor: Optional[str]
    window: Dict[str, Any]
    coverage: Dict[str, Any]  # v1 delivered physical spans after quota/lineage deferrals
    error: str
    reason_code: str
