"""Canonical presentation membership for indexed history and evidence readers."""
from __future__ import annotations

from typing import Callable, Optional


def room_membership(chat_id: int, project_chat_ids: set, source_refs: list,
                    rooms: Callable[..., int]):
    """Canonical room membership shared by history and evidence readers.

    ``rooms`` is the room rule (``projects_registry.RoomResolver``), the same one
    the live bus addresses by, so a task's rows replay in the room its frames went
    to. ``gateway.history_paging.room_lens`` addresses the Project admissions
    below in the dialogue store (its candidate superset); change both together.

    Presentation-only hiding and cross-room question pointers belong to the UI
    caller. A room source retains the actual cognitive result as well.
    """
    from ouroboros.contracts.chat_id_policy import HIDDEN_CHAT_ID, is_a2a_chat_id
    from ouroboros.dialogue_provenance import row_provenance
    from ouroboros.project_dialogue import (MAIN_PINNED_ROW_TYPES, ORIGIN_ADDRESSED_NOTICE_TYPES,
                                            bound_room_chat, entry_matches_source_ref)

    def matches(entry_chat: int, entry: Optional[dict] = None) -> bool:
        row = entry if isinstance(entry, dict) else {}
        if is_a2a_chat_id(entry_chat):
            return False
        if row_provenance(row) == "place":
            return chat_id == entry_chat and chat_id != 1
        # A routing refusal belongs to the issuing chat, even when the target
        # is bound to another Project. Its lineage must not move the notice.
        bound = 0 if row.get("type") in ORIGIN_ADDRESSED_NOTICE_TYPES else bound_room_chat(rooms, row)
        lifecycle = row.get("type") in MAIN_PINNED_ROW_TYPES
        if chat_id in project_chat_ids:
            # A task's resolved room is authoritative over the address its row was
            # written with: a later conversion moves the subtree's earlier rows too,
            # out of the old room (live delivery already went to the new one).
            return not lifecycle and ((bound == chat_id if bound else entry_chat == chat_id)
                                      or entry_matches_source_ref(row, source_refs))
        if chat_id != 1:
            return entry_chat == chat_id and not bound
        if entry_chat == HIDDEN_CHAT_ID:
            return False
        if lifecycle:
            return entry_chat not in project_chat_ids
        return entry_chat not in project_chat_ids and not bound

    return matches
