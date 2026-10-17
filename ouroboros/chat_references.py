"""Host-owned message addresses and Reply facts, shared by every owner ingress.

References describe what the sender pointed at. They do not select work or grant
authority. Hash the persisted row before adding any history/view fields.
"""
from __future__ import annotations

from ouroboros.chat_chain import format_address, resolve_row, row_address


def projection_fields(row: dict) -> dict:
    """Additive fields for live frames and history, from the same stored row."""
    fields = {key: row[key] for key in ("reply_to", "relay") if isinstance(row.get(key), dict)}
    if row.get("reply_to_relay"):
        fields["reply_to_relay"] = row["reply_to_relay"]
    fields["row_address"] = format_address(row_address(row))
    return fields


def reference_from_row(row: dict) -> dict:
    kind = str(row.get("type") or "message")
    kind = ("question" if kind == "quiz" else "completion" if kind in
            {"project_completion_summary", "task_summary"} else
            "system" if row.get("direction") == "system" else "message")
    role = {"in": "user", "out": "assistant", "system": "system"}.get(row.get("direction"), "system")
    return {"address": format_address(row_address(row)), "chat_id": int(row.get("chat_id", 1)),
            "role": role, "author_label": str(row.get("sender_label") or
                                               {"user": "You", "assistant": "Ouroboros", "system": "System"}[role]),
            "kind": kind, "snippet": " ".join(str(row.get("text") or row.get("caption") or "").split())[:160],
            "ts": str(row.get("ts") or ""), "task_id": str(row.get("task_id") or "")}


def resolve_reply(root, address: str) -> dict:
    """Resolve a transport-neutral Reply address once, at acceptance."""
    if not isinstance(address, str):
        raise ValueError("reply_to must be an address string")
    if not address:
        return {}
    if address.startswith("task:"):
        from ouroboros.task_status import load_effective_task_result
        task_id = address[5:]
        task = load_effective_task_result(root, task_id, materialize_artifacts=False) if task_id else None
        if task:
            return {"address": address, "chat_id": int(task.get("chat_id") or 1), "role": "assistant",
                    "author_label": "Ouroboros", "kind": "work_card",
                    "snippet": " ".join(str(task.get("title") or task.get("text") or "").split())[:160],
                    "ts": str(task.get("updated_at") or task.get("started_at") or ""), "task_id": task_id}
    else:
        try:
            row, _resolution = resolve_row(root, address)
            if row is None:
                row = resolve_progress_row(root, address)
        except ValueError:
            row = None
        if row:
            return reference_from_row(row)
    return {"address": address, "unresolved": True}


def ingress_reply(root, metadata: dict) -> dict:
    """Shared web/HTTP/skill seam. Raw input is resolved, never trusted as a preview."""
    return resolve_reply(root, metadata.get("reply_to", ""))


def origin_reply(root, task_id: str, metadata=None) -> dict:
    """The answer's host reference to the owner message that began its turn."""
    if not task_id or not root:
        return {}
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.project_dialogue import resolve_owner_message_source
    task = metadata or load_effective_task_result(root, task_id, materialize_artifacts=False) or {}
    ref = task.get("origin_message_ref") or (task.get("metadata") or {}).get("origin_message_ref")
    if not ref:
        return {}
    row = resolve_owner_message_source(root, ref)
    return reference_from_row(row) if row else {}


def resolve_progress_row(root, address):
    """Progress uses the same persisted-row hash; reuse the chain's exact resolver."""
    from pathlib import Path
    from ouroboros.chat_chain import _search, _normalized, _chat_log_signature
    root = Path(root)
    paths = [*sorted((root / "archive").glob("progress_*.jsonl")), root / "logs/progress.jsonl"]
    generations = [(p, str(_chat_log_signature(p).get("first_line_sha256") or "")) for p in paths if p.exists()]
    return _search(root, generations, _normalized(address))[0]


def origin_projection(root, ref):
    """A projected origin keeps its real source address, never hashes view fields."""
    from ouroboros.project_dialogue import resolve_owner_message_source
    row = resolve_owner_message_source(root, ref)
    return projection_fields(row) if row else {}
