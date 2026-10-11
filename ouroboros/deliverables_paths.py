"""Small, shared path views for the configured Deliverables container."""

from __future__ import annotations

import os
import pathlib


def deliverables_root_lexical() -> pathlib.Path:
    """Return the configured Deliverables spelling without resolving children."""
    from ouroboros.config import runtime_setting

    from ouroboros.config import get_deliverables_root

    explicit = (runtime_setting("OUROBOROS_DELIVERABLES_ROOT") or "").strip()
    jail = (os.environ.get("OUROBOROS_USER_FILES_ROOT") or "").strip()
    raw = explicit or (os.path.join(jail, "Deliverables") if jail else get_deliverables_root())
    try:
        expanded = os.path.expanduser(raw)
    except (RuntimeError, OSError, TypeError):
        expanded = raw
    return pathlib.Path(os.path.abspath(expanded))


def deliverables_root_lexical_alias() -> pathlib.Path:
    """Canonicalize only the parent of the lexical Deliverables root."""
    root = deliverables_root_lexical()
    return root.parent.resolve(strict=False) / root.name


def lexical_path_is_relative_to_casefold(
    path: pathlib.Path,
    root: pathlib.Path,
) -> bool:
    """Case-insensitive containment without resolving descendant symlinks."""
    try:
        path_parts = pathlib.Path(os.path.abspath(path)).parts
        root_parts = pathlib.Path(os.path.abspath(root)).parts
    except (OSError, TypeError, ValueError):
        return False
    if len(path_parts) < len(root_parts):
        return False
    return tuple(part.casefold() for part in path_parts[: len(root_parts)]) == tuple(
        part.casefold() for part in root_parts
    )


# Private aliases preserve the existing imports while the path implementation
# lives in its own small module instead of growing the access-matrix module.
_deliverables_root_lexical = deliverables_root_lexical
_deliverables_root_lexical_alias = deliverables_root_lexical_alias
_lexical_path_is_relative_to_casefold = lexical_path_is_relative_to_casefold
