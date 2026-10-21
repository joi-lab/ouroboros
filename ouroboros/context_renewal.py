"""The floor of the refusal path: the same task continues in a fresh view of its own record.

Owner decision Q1=A (17.10.2026). When the model-free refusal passes and the configured
fallback could not bring a refused request under the window, the task does not end: its
conversation is rebuilt as a seed of what continuity needs, and everything else stays one
address away in an exact checkpoint of the whole transcript. The task id, attempt,
children, budget, route, pending questions and custody are those of the running loop, so
only the view changes. No model is called and nothing is summarized; the mind's own notes
are never dropped for age.

The seed keeps, in their original order: the system view and the assignment; every delivered
message (the owner's and principals' words typed at delivery, review feedback, notices, and
any row written before host state was bound to its text, which may hold merged owner words);
Ouroboros's outward dialogue; the mind's authored notes (actor capsules) and a predecessor's
notes; the current review decisions; the address records the refusal passes wrote since the
newest note; and the mind's own prose since its newest note, including the text it wrote beside
a tool call. The record lists every tool unit since the newest note with its typed facts, shown
or never shown, and the address of every source the mind restored (restored sources carry the whole
unit, so they leave the view and restore again from there). Complete tool units, replies the notes
already cover, host state the host sends afresh (facts, clock, roster and skill snapshots,
bound and unchanged), superseded review copies and earlier address records leave the view;
one host record names the checkpoint that holds them exactly. A renewal that would remove no
tool unit, no covered reply and no restored source, or a seed not smaller than the refused request, is
never sent: the round then ends in the named refusal.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple

from ouroboros.context_budget import HOST_CONTEXT_KIND_KEY, OWNER_ROW_KINDS, bound_host_row

RENEWAL_RECORD_NAME = "ouroboros_view_renewal"


def _is_note(row: Dict[str, Any]) -> bool:
    """An actor capsule: a note the mind authored and kept."""
    from ouroboros.context_compaction import _capsule_metadata

    capsule, meta = _capsule_metadata(row)
    return bool(capsule and meta and meta.get("authorship") == "actor")


def _newest_note(messages: Sequence[Dict[str, Any]]) -> int:
    return max((i for i, row in enumerate(messages) if _is_note(row)), default=-1)


def _restored_address(row: Dict[str, Any]) -> Dict[str, Any]:
    """The exact address of a source the mind restored (``restore_unit_refs``), else ``{}``.

    A restored source is a host source view built at the actor's request (``route_fp == "actor"``);
    the refusal passes' address records carry the refused route instead (review r2, 18.10)."""
    from ouroboros.context_compaction import _capsule_metadata

    capsule, meta = _capsule_metadata(row)
    if not (capsule and meta and meta.get("retention") == "source_view" and meta.get("route_fp") == "actor"):
        return {}
    return {"checkpoint_ref": meta["checkpoint_ref"], "unit_id": meta["unit_id"], "raw_sha256": meta["source_unit_sha256"]}


def restored_sources(messages: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Addresses of the sources the mind restored in this view, in order (a restore made in the same call as
    a note sits before that note)."""
    return [address for row in messages if (address := _restored_address(row))]


def units_since_note(messages: Sequence[Dict[str, Any]], observation: Any) -> str:
    """Typed facts of every tool unit after the newest note, each named shown or never shown to me.

    Plan item 16 (review r1, 17.10): the record indexes what left the view since the newest note, so a
    renewal never hides which calls ran (a child started, a commit attempted) or which results the mind
    never saw. The facts are ``context_source_view._unit_facts``; exposure is the usable-send evidence
    (``_exposed_units``); unknown exposure reads as never shown.
    """
    from ouroboros import context_compaction as compaction
    from ouroboros.context_source_view import _exposed_units, _unit_facts

    newest = _newest_note(messages)
    units = [unit for unit in compaction.context_units(list(messages), scope="tool")
             if unit.start > newest and compaction.unit_kind(messages, unit) == "tool"]  # calls, not records
    shown = _exposed_units(list(messages), units, observation if isinstance(observation, dict) else {})
    return "\n".join(f"{_unit_facts(messages, unit)}\n  {'shown to me' if unit.unit_id in shown else 'never shown to me'}"
                     for unit in units)


def renewal_seed(messages: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """``(kept rows in order, counts of the rows that leave the view)``; pure, no I/O."""
    from ouroboros.context_budget import host_row_intact
    from ouroboros.context_compaction import _capsule_metadata, _plain_text
    from ouroboros.loop_messages import CONTEXT_FACTS_NAME
    from ouroboros.main_context_authority import PREDECESSOR_NOTES_NAME
    from ouroboros.peer_roster import ROSTER_SNAPSHOT_NAME, ROSTER_UPDATE_NAME
    from ouroboros.review_history_view import REVIEW_CONTEXT_INDEX_KEY, obsolete_review_index_positions
    from ouroboros.send_clock import CLOCK_ROW_NAME
    from ouroboros.skill_context_snapshot import SNAPSHOT_KIND

    resent = {CONTEXT_FACTS_NAME, CLOCK_ROW_NAME, ROSTER_SNAPSHOT_NAME, ROSTER_UPDATE_NAME, SNAPSHOT_KIND,
              RENEWAL_RECORD_NAME}

    note = _is_note
    first_user = next((i for i, row in enumerate(messages) if row.get("role") == "user"), -1)
    newest_note = _newest_note(messages)
    superseded = set(obsolete_review_index_positions(messages))
    kept: List[Dict[str, Any]] = []
    left = {"tool_rows": 0, "covered_replies": 0, "host_rows": 0, "restored_sources": 0}
    for i, row in enumerate(messages):
        role, kind = row.get("role"), row.get(HOST_CONTEXT_KIND_KEY)
        if ((i == 0 and role == "system") or i == first_user or kind in OWNER_ROW_KINDS or note(row)
                or kind == PREDECESSOR_NOTES_NAME or (row.get(REVIEW_CONTEXT_INDEX_KEY) and i not in superseded)):
            kept.append(row)
        elif role == "tool" or row.get("tool_calls"):
            left["tool_rows"] += 1
            prose = _plain_text(row.get("content")) if role == "assistant" and i > newest_note else None
            if prose and prose.strip():
                kept.append({"role": "assistant", "content": prose})  # my words beside the call stay
        elif _restored_address(row):
            left["restored_sources"] += 1  # the whole unit the mind restored: its address goes in the record
        elif (_capsule_metadata(row)[0] or kind == RENEWAL_RECORD_NAME) and i > newest_note:
            kept.append(row)  # address records and earlier renewal indexes since my newest note: what left, with facts
        elif (_capsule_metadata(row)[0] or row.get(REVIEW_CONTEXT_INDEX_KEY)
              or (kind in resent and host_row_intact(row))):
            left["host_rows"] += 1  # older addresses, superseded review copies, state the host sends afresh
        elif role == "assistant" and i <= newest_note:
            left["covered_replies"] += 1
        else:
            kept.append(row)  # every delivered message and my replies since my newest note
    return kept, left


def renew_after_refusal(limit_ctx: Any, tools: Any) -> bool:
    """Publish the renewal seed for a round that ended in a size refusal; False when renewal cannot help."""
    from ouroboros.artifacts import store_actor_source_bytes
    from ouroboros.context_fit import estimate_context_prompt_tokens
    from ouroboros.loop_llm_call import TRANSPORT_DEATHS_KEY
    from ouroboros.loop_model_call import CONTEXT_REFUSAL_ROUND_KEY
    from ouroboros.loop_round_limits import _publish_reclaimed_transcript

    usage = limit_ctx.accumulated_usage
    # This round's own size refusal decides, not the kind a configured fallback failed with afterwards
    # (its resource refusal stays recorded beside the renewal; the next primary call clears it).
    if (usage.get(CONTEXT_REFUSAL_ROUND_KEY) != limit_ctx.round_idx
            or isinstance(usage.get(TRANSPORT_DEATHS_KEY), dict) or usage.get("_pending_transport_outcome")):
        return False
    messages = limit_ctx.messages
    before = len(messages)
    seed, left = renewal_seed(messages)
    if not (left["tool_rows"] or left["covered_replies"] or left["restored_sources"]):
        return _cannot_help(limit_ctx)  # only host rows would leave: a renewal cannot make this conversation fit
    raw = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    try:
        source = store_actor_source_bytes(limit_ctx.drive_root or limit_ctx.drive_logs.parent, limit_ctx.task_id,
                                          category="context_checkpoints", source_id=hashlib.sha256(raw).hexdigest(),
                                          data=raw, extension="json")
    except (OSError, ValueError):
        return False  # no exact source, no renewal: every address must resolve (BIBLE P1)
    index = units_since_note(messages, getattr(getattr(tools, "_ctx", None), "_last_context_observation", None))
    restored = restored_sources(messages)
    seed.append(bound_host_row(RENEWAL_RECORD_NAME, (
        "[Host record: working view renewed after the provider refused this conversation's size. Same task; "
        "nothing summarized; no model called.] Kept, in order: the assignment, every delivered message and "
        "outward dialogue, my notes, my replies since my newest note and the current review decisions. "
        f"Left the view: {left['tool_rows']} tool rows, {left['covered_replies']} replies before my newest note, "
        f"{left['host_rows']} host rows, {left['restored_sources']} sources I restored. The exact transcript "
        f"of {before} rows: {json.dumps(source, ensure_ascii=False, sort_keys=True)}."
        + (f"\nTool units since my newest note (they left the view; each stays exact there):\n{index}" if index
           else "")
        + (f"\nSources I restored (they left the view; restore_unit_refs brings each back): "
           f"{json.dumps(restored, ensure_ascii=False, sort_keys=True)}" if restored else ""))))
    schemas = getattr(limit_ctx, "tool_schemas", None)
    if estimate_context_prompt_tokens(seed, schemas) >= estimate_context_prompt_tokens(messages, schemas):
        return _cannot_help(limit_ctx)  # never send a candidate that is not smaller than the refused one
    _publish_reclaimed_transcript(SimpleNamespace(messages=messages, task_id=limit_ctx.task_id, tools=tools), seed,
                                  by="renewal")
    from ouroboros.loop import _emit_checkpoint_event

    _emit_checkpoint_event(limit_ctx.event_queue, limit_ctx.task_id, limit_ctx.drive_logs, {
        "checkpoint_kind": "context_view_renewed", "round": limit_ctx.round_idx, "owner_visible": True,
        "source": source, "rows_before": before, "rows_after": len(seed), **left})
    return True


def _cannot_help(limit_ctx: Any) -> bool:
    """Name the frame's largest parts for the named refusal (``loop_transport``), then decline."""
    from ouroboros.loop_messages import _context_components

    view = SimpleNamespace(messages=limit_ctx.messages, tool_schemas=getattr(limit_ctx, "tool_schemas", None) or [],
                           accumulated_usage=limit_ctx.accumulated_usage)
    try:
        limit_ctx.accumulated_usage["_context_refusal_frame"] = [list(part) for part in _context_components(
            view, measured=False)]
    except Exception:  # the refusal text is facts only; an unreadable frame stays unnamed
        pass
    return False
