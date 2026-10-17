"""What one participant is told about the council (``plan_ask.participant_content``).

Selection over the plan record: the asks since the one this seat last answered in, and the
peer replies to EARLIER asks that settled after that delivery was built — a late settlement
is never lost to the ask index (scope R1, F2). Hydration from the retained sources: prose a
reply or a revision lost to the record index's bound (``plan_record._fit``) is read back when
it fits one attachment, otherwise the omission is named with the source's address. Pure
selection plus bounded reads; nothing here writes.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional

from ouroboros import plan_record


def last_answer_row(record: Mapping[str, Any], seat_id: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """``(ask, participant_row)`` of this seat's latest answered ask."""
    found = (None, None)
    for ask in record.get("asks") or []:
        for participant in ask.get("participants") or []:
            if str(participant.get("seat_id") or "") == seat_id and participant.get("status") == "answered":
                found = (ask, participant)
    return found


def asks_since(record: Mapping[str, Any], last_ask_id: str) -> List[Dict[str, Any]]:
    """The asks from the one named onward (every ask when none is named)."""
    asks = list(record.get("asks") or [])
    if not last_ask_id:
        return asks
    index = next((i for i, a in enumerate(asks) if str(a.get("ask_id") or "") == last_ask_id), None)
    return asks if index is None else asks[index:]


def late_peer_replies(record: Mapping[str, Any], seat_id: str, anchor: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of the asks BEFORE ``anchor`` (the ask this seat last answered in) carrying only the
    peer replies that settled at or after the anchor's ``ts`` — answers the seat's last delivery
    could not contain, selected by the settlement fact ``answered_at``, never by ask order. The
    ask's head and the attribution are kept; what the seat already saw is not repeated. Without
    an anchor the whole record is new to the seat, so nothing is late."""
    if anchor is None:
        return []
    since = str(anchor.get("ts") or "")
    anchor_id = str(anchor.get("ask_id") or "")
    out: List[Dict[str, Any]] = []
    for ask in record.get("asks") or []:
        if str(ask.get("ask_id") or "") == anchor_id:
            break
        late = [p for p in ask.get("participants") or []
                if str(p.get("seat_id") or "") != seat_id and p.get("status") == "answered"
                and str(p.get("answered_at") or "") >= since]
        if late:
            out.append({**{k: ask.get(k) for k in ("ask_id", "ts", "revision", "message")},
                        "participants": copy.deepcopy(late), "late_replies": True})
    return out


def delivered_asks(state_root: Any, task_id: str, asks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The council's asks as a participant is told them, every answer's prose present: prose the
    index moved out (``plan_record._fit``) is read back from the retained reply source when it fits
    one attachment (``EVIDENCE_PER_ITEM_BYTES``), else the omission is named with the source's address."""
    from ouroboros.review_ledger import read_source
    from ouroboros.tools import plan_evidence

    out = copy.deepcopy(list(asks))
    for participant in (p for ask in out for p in ask.get("participants") or []):
        reply = participant.get("reply")
        if not isinstance(reply, dict) or reply.get("prose_inline", True):
            continue
        ref = participant.get("reply_ref") if isinstance(participant.get("reply_ref"), dict) else {}
        inner = ref.get("ref") if isinstance(ref.get("ref"), dict) else {}
        chars, retained = int(ref.get("chars") or 0), ref.get("status") == "retained"
        parsed = None
        if retained and chars <= plan_evidence.EVIDENCE_PER_ITEM_BYTES:
            try:
                parsed, _error = plan_record.parse_reply(read_source(state_root, task_id, ref).decode("utf-8"))
            except (OSError, ValueError, KeyError, TypeError):
                parsed = None
        if parsed is not None:
            reply.update(prose=str(parsed.get("prose") or ""), prose_inline=True)
            continue
        reply["prose_omitted"] = {"chars": chars, "address": str(inner.get("path") or ""),
                                  "reason": ("unreadable" if retained and chars <= plan_evidence.EVIDENCE_PER_ITEM_BYTES
                                             else "too_large" if retained else "not_retained")}
    return out


def delivered_revision(state_root: Any, task_id: str, revision: Mapping[str, Any]) -> Dict[str, Any]:
    """A revision with its prose present: text the index moved out is read back from the
    revision's retained source (``text_ref``), so a repeat participant's delta diffs the text it
    saw, never a blank. When the source cannot be read the copy says so (``plan_unavailable``
    names the address) instead of standing in as an empty plan."""
    out = dict(revision)
    if revision.get("plan_inline", True) or not isinstance(revision.get("text_ref"), dict):
        return out
    from ouroboros.artifacts import read_actor_source_bytes

    try:
        payload = json.loads(read_actor_source_bytes(state_root, task_id, revision["text_ref"]).decode("utf-8"))
        out.update(plan=str(payload.get("plan") or ""), plan_inline=True)
    except (OSError, ValueError, KeyError, TypeError):
        out["plan_unavailable"] = str(revision["text_ref"].get("path") or "")
    return out
