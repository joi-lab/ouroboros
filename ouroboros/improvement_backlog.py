"""The improvement backlog: one ordinary authored global knowledge note.

The acting mind writes it through ``knowledge_write`` like any note (shelf lock,
exact ``expected_revision``, anchored edit, full old/new history); no host writer
appends, counts, reopens, ranks or closes its items. Older stored fields (counts,
fingerprints, statuses) stay as the text the author left. This module renders the
complete current note for the readers that consult it: the evolution chooser
(``post_task_evolution``) and an evolution task's context (``context.py``). No
parser selects, sorts or hides its prose.
"""

from __future__ import annotations

import pathlib
from typing import Any

BACKLOG_TOPIC = "improvement-backlog"
BACKLOG_READER = f"knowledge_read(topic='{BACKLOG_TOPIC}', scope='global')"


def authored_backlog_text(drive_root: Any) -> str:
    """The complete current backlog note with its source facts, or its named absence.

    A missing note is a known absence; an unreadable one stays unavailable,
    never an empty backlog.
    """
    from ouroboros.knowledge import read_knowledge_note, resolve_knowledge_address

    try:
        note = read_knowledge_note(resolve_knowledge_address(pathlib.Path(drive_root), BACKLOG_TOPIC, "global"))
    except FileNotFoundError:
        return (f"No improvement-backlog note has been written yet; "
                f"knowledge_write(topic='{BACKLOG_TOPIC}', scope='global', ...) creates it.")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"Improvement-backlog source unavailable ({type(exc).__name__}); it is unknown, not empty: {BACKLOG_READER}."
    return f"Source: {BACKLOG_READER}, revision {note.revision}, {len(note.text)} chars, complete.\n\n{note.text}"
