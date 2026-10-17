"""The room rule shaped from an explicit-roots map, for readers tested without a store.

``projects_registry.RoomResolver`` answers from the bindings file and the task store;
a test that only has ``{task_id: project_chat_id}`` in hand gets the same callable
shape here: own id, then parent, then root, as the explicit map names them.
"""
from __future__ import annotations

from typing import Any, Dict


class RoomsFromBindings:
    def __init__(self, bindings: Dict[str, int]) -> None:
        self.bindings = dict(bindings)

    def __call__(self, task_id: Any, parent_task_id: Any = "", root_task_id: Any = "") -> int:
        for candidate in (task_id, parent_task_id, root_task_id):
            chat = self.bindings.get(str(candidate or "").strip())
            if chat:
                return int(chat)
        return 0

    def lineage(self, task_id: Any) -> Dict[str, Any]:
        return {}

    def warm(self, task_ids: Any) -> None:
        return None
