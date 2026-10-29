"""Where generated copies are born so that desktop search skips them.

Review checkouts, reviewer seat areas and acting checkouts with their test scratch are trees of
thousands of machine-made files that nobody searches, yet Spotlight imports every new one.
Spotlight skips a folder whose name ends in ``.noindex`` together with everything under it (the
suffix Xcode's build folders carry); a marker file such as ``.metadata_never_index`` or Finder's
hidden flag does not exclude a folder on current macOS. So a NEW copy is born under such a name,
while one that already exists keeps its spelling: nothing in use moves, and the copies made
before this rule leave the index through their normal removal. Other systems ignore the suffix,
and nothing a person keeps (projects, Deliverables, uploads, task results) is named this way.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

SUFFIX = ".noindex"
COPIES = "copies" + SUFFIX  # under a worktree root: new acting checkouts and their sibling scratch


def unindexed(parent: Any, name: str, recorded: Any = "") -> pathlib.Path:
    """``parent/name`` when that entry already exists or ``recorded`` (the path a marker bound this copy to)
    spells it so, else ``parent/name.noindex``: a bound copy keeps its name even while it is being replaced."""
    legacy = pathlib.Path(parent) / name
    bound = recorded and pathlib.PurePath(str(recorded)).name == name
    return legacy if bound or os.path.lexists(legacy) else legacy.with_name(name + SUFFIX)


def copy_path(root: Any, name: str) -> pathlib.Path:
    """The acting checkout ``name`` under the worktree ``root``: ``root/name`` when it already exists
    there, else ``root/copies.noindex/name``, so its ``.env`` sibling is excluded with it."""
    legacy = pathlib.Path(root) / name
    return legacy if os.path.lexists(legacy) else pathlib.Path(root) / COPIES / name
