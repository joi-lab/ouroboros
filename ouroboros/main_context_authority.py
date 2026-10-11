"""Defensive, Main-only projection of continuation authority.

The exact task-result reader and child/external contracts keep their complete
authority.  This module makes a deep provider-context copy: one predecessor
account, an authored narrative for oversized terminal text when available, and
an explicit source-resolvable gap when it is not.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Dict, MutableSet, Optional

from ouroboros.context_budget import PREDECESSOR_RESULT_INLINE_CHARS

_RAW_AUTHORITY_KEYS = frozenset({"result", "final_answer"})


def _canonical_result_ref(task_id: str) -> Dict[str, Any]:
    return {"kind": "task_result", "task_id": str(task_id or ""), "reader": "get_task_result"}


def _task_result_source(node: Mapping[str, Any], task_id: str) -> Dict[str, Any]:
    source = node.get("source")
    if isinstance(source, Mapping) and source:
        return copy.deepcopy(dict(source))
    return {
        "kind": "task_result",
        "task_id": str(task_id or ""),
        "reader": "get_task_result",
        "arguments": {
            "task_id": str(task_id or ""),
            "include_authority": True,
        },
    }


def _authority_identity(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    source = value.get("source")
    if isinstance(source, Mapping):
        source_id = str(source.get("task_id") or "").strip()
        if source_id:
            return source_id
        arguments = source.get("arguments")
        if isinstance(arguments, Mapping):
            source_id = str(arguments.get("task_id") or "").strip()
            if source_id:
                return source_id
    return str(value.get("task_id") or "").strip()


def _narrative_for(
    node: Mapping[str, Any], task_id: str, drive_root: Any,
) -> Optional[Dict[str, Any]]:
    from ouroboros.project_dialogue import continuation_narrative_is_valid

    candidate = node.get("continuation_narrative")
    if continuation_narrative_is_valid(candidate, task_id):
        return copy.deepcopy(dict(candidate))
    try:
        from ouroboros.project_dialogue import resolve_legacy_continuation_narrative

        legacy = resolve_legacy_continuation_narrative(
            drive_root, task_id, _canonical_result_ref(task_id),
        )
        if isinstance(legacy, dict) and continuation_narrative_is_valid(legacy, task_id):
            return copy.deepcopy(legacy)
    except Exception:
        # A missing or malformed legacy row is represented by the caller's
        # typed gap.  Main context assembly must not become a second writer.
        return None
    return None


def _narrative_value(
    raw: str,
    *,
    task_id: str,
    node: Mapping[str, Any],
    drive_root: Any,
    seen_narratives: MutableSet[str],
) -> Dict[str, Any]:
    source = _task_result_source(node, task_id)
    narrative = _narrative_for(node, task_id, drive_root)
    narrative_id = f"task-narrative:{task_id}"
    base = {
        "raw_result_resident": False,
        "original_chars": len(raw),
        "omitted_chars": len(raw),
        "source": copy.deepcopy(source),
    }
    if not narrative:
        return {
            **base,
            "status": "unavailable",
            "narrative_status": "unavailable",
            "narrative_gap": {
                "kind": "continuation_narrative_unavailable",
                "reason": "no_exact_authored_summary",
            },
        }
    if narrative_id in seen_narratives:
        return {
            **base,
            "status": "available",
            "narrative_status": "duplicate_reference",
            "narrative_ref": {
                "summary_id": narrative_id,
                "task_id": task_id,
            },
        }
    seen_narratives.add(narrative_id)
    return {
        **base,
        "status": "available",
        "narrative_status": "available",
        "narrative": copy.deepcopy(narrative),
    }


def _project_value(
    value: Any,
    *,
    key: str = "",
    task_id: str = "",
    authority_node: Optional[Mapping[str, Any]] = None,
    drive_root: Any = None,
    seen_narratives: MutableSet[str],
) -> Any:
    if key in _RAW_AUTHORITY_KEYS and isinstance(value, str) and len(value) > PREDECESSOR_RESULT_INLINE_CHARS:
        return _narrative_value(
            value,
            task_id=task_id,
            node=authority_node or {},
            drive_root=drive_root,
            seen_narratives=seen_narratives,
        )
    if isinstance(value, Mapping):
        current_id = str(value.get("task_id") or task_id or "").strip()
        is_authority = key == "predecessor_authority" or (
            "source" in value and "task_id" in value and "task_contract" in value
        )
        if is_authority:
            return _project_authority_node(
                value,
                task_id=current_id,
                drive_root=drive_root,
                seen_narratives=seen_narratives,
            )
        return {
            copy.deepcopy(k): _project_value(
                child,
                key=str(k),
                task_id=current_id,
                authority_node=authority_node,
                drive_root=drive_root,
                seen_narratives=seen_narratives,
            )
            for k, child in value.items()
        }
    if isinstance(value, list):
        return [
            _project_value(
                child,
                task_id=task_id,
                authority_node=authority_node,
                drive_root=drive_root,
                seen_narratives=seen_narratives,
            )
            for child in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _project_value(
                child,
                task_id=task_id,
                authority_node=authority_node,
                drive_root=drive_root,
                seen_narratives=seen_narratives,
            )
            for child in value
        )
    return copy.deepcopy(value)


def _project_authority_node(
    node: Mapping[str, Any],
    *,
    task_id: str,
    drive_root: Any,
    seen_narratives: MutableSet[str],
) -> Dict[str, Any]:
    current_id = str(node.get("task_id") or task_id or "").strip()
    projected = {
        copy.deepcopy(key): _project_value(
            value,
            key=str(key),
            task_id=current_id,
            authority_node=node,
            drive_root=drive_root,
            seen_narratives=seen_narratives,
        )
        for key, value in node.items()
    }
    # A materialized predecessor is already exposed once at the provider
    # projection's top level.  Remove only an equal nested copy; a different
    # identity is the legitimate second hop and remains available.
    contract = projected.get("task_contract")
    nested = contract.get("predecessor_authority") if isinstance(contract, dict) else None
    own = projected.get("predecessor_authority")
    if isinstance(contract, dict) and isinstance(nested, Mapping) and isinstance(own, Mapping):
        if _authority_identity(nested) == _authority_identity(own):
            # Keep the second hop in the complete contract, but avoid carrying
            # a second copy of it beside that contract inside the provider view.
            projected.pop("predecessor_authority", None)
    return projected


def project_main_task_authority(
    task: Mapping[str, Any], *, drive_root: Any = None,
) -> Dict[str, Any]:
    """Build the provider-only authority section without mutating ``task``."""
    seen_narratives: set[str] = set()
    projection: Dict[str, Any] = {}
    task_id = str(task.get("id") or task.get("task_id") or "").strip()
    predecessor = task.get("predecessor_authority")
    contract = task.get("task_contract")
    if isinstance(predecessor, Mapping) and predecessor:
        projection["predecessor_authority"] = _project_authority_node(
            predecessor,
            task_id=str(predecessor.get("task_id") or "").strip(),
            drive_root=drive_root,
            seen_narratives=seen_narratives,
        )
    if isinstance(contract, Mapping):
        projected_contract = _project_value(
            contract,
            task_id=task_id,
            authority_node=predecessor if isinstance(predecessor, Mapping) else {},
            drive_root=drive_root,
            seen_narratives=seen_narratives,
        )
        nested = projected_contract.get("predecessor_authority") if isinstance(projected_contract, dict) else None
        if isinstance(projected_contract, dict) and isinstance(nested, Mapping) and isinstance(predecessor, Mapping):
            if _authority_identity(nested) == _authority_identity(predecessor):
                projected_contract.pop("predecessor_authority", None)
        projection["task_contract"] = projected_contract
    origin_ref = task.get("origin_message_ref")
    origin_text = task.get("origin_message_text")
    if isinstance(origin_ref, Mapping) and origin_ref:
        projection["task_authority_origin"] = {
            "ref": copy.deepcopy(dict(origin_ref)),
            **({"text": str(origin_text)} if isinstance(origin_text, str) and origin_text else {}),
        }
    if isinstance(task.get("authority_historical_gaps"), list):
        projection["authority_historical_gaps"] = copy.deepcopy(task["authority_historical_gaps"])
    return projection


__all__ = ["project_main_task_authority"]
