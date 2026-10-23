"""Names and sizes of the selected context, read from its captured sources."""
from __future__ import annotations

from ouroboros import memory_floor as floor, memory_view as mv


def memory_snapshot(plan):
    core = getattr(plan, "core", None)
    return mv.snapshot_from_json(core.memory_view_json) if core and core.memory_view_json else None


def block_label(message, index, block, plan):
    text = block.get("text", "") if isinstance(block, dict) else str(block)
    if message.get("role") != "system":
        return "assignment" if index == 1 else message.get("role", "message")
    if "## My story" in text:
        return "my story and identity"
    if "## This room" in text or "## Live rooms" in text:
        return "this room, live rooms and runtime facts"
    if text.startswith("## DEVELOPMENT.md"):
        return "development book"
    return "core and books" if index == 0 else "runtime facts"


def wake_facts(plan, mode):
    snapshot = memory_snapshot(plan)
    if snapshot is None or snapshot.spec.role != "consciousness":
        return ""
    rooms = ([snapshot.room] if snapshot.room else []) + list(snapshot.live_rooms)
    chars = sum(int(room.get("facts", {}).get("chars") or 0) for room in rooms)
    story = len(mv.render_story(snapshot, floor.kept_level(snapshot, plan.projection(mode).memory_facts)))
    total = chars + story
    share = f"{100 * chars / total:.1f}%" if total else "0%"
    drafts = len(getattr(snapshot, "candidates", ()))
    return f"captured unsealed rooms {len(rooms)}, {chars:,} characters ({share} of story + open rooms); drafts awaiting a decision {drafts}"


def window_fact(plan, reserve):
    from ouroboros.context_window import resolve_window

    if plan is None:
        return ""
    record = resolve_window(plan, int(reserve or 0), bound=getattr(plan, "window_witness_bound", True))
    if record.boundary is None:
        accepted = (f"; accepted input ≥ {record.accepted:,} ({record.accepted_at.replace('T', ' ')[:16]})"
                    if record.accepted else "")
        return f"window unknown{accepted}"
    limit = min((limit for limit in (record.input, record.total) if limit),
                key=lambda limit: limit.tokens - (record.reserve if limit.kind == "total" else 0))
    return f"window {limit.tokens:,} {limit.kind}: {record.source}; usable input {record.usable:,}"
