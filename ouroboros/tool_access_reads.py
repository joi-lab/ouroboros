"""Inherited physical read bindings, derived from existing task provenance.

The policy is parent-equivalent reading, not a path grant list. Each delegation
edge records where its parent actually worked; exact parent links recover earlier
bindings even when every child selects a different starting folder. Writes never
consult this projection.
"""
from __future__ import annotations

import pathlib
from typing import Any


def _access():
    from ouroboros import tool_access
    return tool_access


def capture_parent_workspace(ctx: Any) -> dict[str, str]:
    """Capture the parent's actual focus before choosing the child's folder."""
    from ouroboros.tools.tool_resolution import active_repo_dir_for

    access = _access()
    meta = getattr(ctx, "task_metadata", None) or {}
    if (getattr(ctx, "is_direct_chat", False) and not getattr(ctx, "workspace_root", None)
            and isinstance(meta, dict) and meta.get("_project_room_note")):
        return {"root": str(meta.get("_project_room_dir") or ""), "mode": "", "source": "project_room",
                "availability": "unavailable", "detail": str(meta["_project_room_note"])}
    if getattr(ctx, "workspace_root", None):
        source = "active_workspace"
    elif access.project_room_lens_dir(ctx) is not None:
        source = "project_room"
    elif access.folderless_scratch_dir(ctx) is not None:
        source = "task_drive"
    else:
        source = "system_repo"
    return {"root": str(active_repo_dir_for(ctx).resolve(strict=False)),
            "mode": str(getattr(ctx, "workspace_mode", "") or ""), "source": source}


def _value(record: dict, key: str):
    meta = record.get("metadata")
    return record.get(key) or (meta.get(key) if isinstance(meta, dict) else None)


def _parent_records(ctx: Any):
    """Read exact ancestry only; never enumerate tasks or a mutable project map."""
    from ouroboros.task_results import load_task_result, validate_task_id

    meta = getattr(ctx, "task_metadata", None)
    current = meta if isinstance(meta, dict) else {}
    yield current
    seen = {str(getattr(ctx, "task_id", "") or "")}
    canonical = _access().canonical_data_root(ctx)
    while parent := _value(current, "parent_task_id"):
        try:
            parent = validate_task_id(parent)
            if parent in seen:
                return
            seen.add(parent)
            current = load_task_result(canonical, parent, strict=True) or {}
        except (OSError, ValueError, TypeError):
            return  # Missing historical provenance does not invent a different folder.
        if not current:
            return
        yield current


def inherited_read_roots(ctx: Any) -> list[tuple[str, pathlib.Path]]:
    """Physical aliases of existing logical roots, for READ operations alone."""
    access = _access()
    if access.active_tool_profile(ctx) not in {"local_readonly_subagent", "acting_subagent"}:
        return []
    roots = [("runtime_data", access.canonical_data_root(ctx))]
    for record in _parent_records(ctx):
        edge = _value(record, "parent_workspace")
        candidates = [("active_workspace", edge.get("root"))] if isinstance(edge, dict) else []
        # The current context already contributes its own default. Ancestor rows
        # also retain the selected folder and any nonstandard execution drive.
        candidates += [("active_workspace", _value(record, "workspace_root")),
                       ("runtime_data", _value(record, "drive_root")),
                       ("runtime_data", _value(record, "child_drive_root"))]
        for label, raw in candidates:
            if not str(raw or "").strip():
                continue
            try:
                roots.append((label, pathlib.Path(raw).expanduser().resolve(strict=False)))
            except (OSError, ValueError, RuntimeError):
                continue
    return list(dict.fromkeys(roots))


def inherited_read_base(ctx: Any, root: str, target: pathlib.Path) -> pathlib.Path | None:
    access = _access()
    bases = [base for label, base in inherited_read_roots(ctx)
             if label == root and access.path_is_relative_to(target, base)]
    return max(bases, key=lambda base: len(base.parts), default=None)


def read_allows_outside_home(ctx: Any) -> bool:
    """Inherit the parent's READ path class without changing action authority."""
    access = _access()
    if access.active_tool_profile(ctx) not in {"local_readonly_subagent", "acting_subagent"}:
        return access.is_external_workspace(ctx)
    found_edge = False
    for record in _parent_records(ctx):
        edge = _value(record, "parent_workspace")
        if isinstance(edge, dict):
            found_edge = True
            if edge.get("mode") == "external":
                return True
    # Legacy children have no captured source edge; keep their former path class.
    return not found_edge and access.is_external_workspace(ctx)


def readonly_start_folder(value: Any) -> str:
    """A read starting place is a directory, not a Git mutation boundary."""
    path = pathlib.Path(str(value or "").strip()).expanduser().resolve(strict=False)
    if not path.is_dir():
        raise ValueError(f"workspace_root is not a directory: {value}")
    return str(path)


def admit_child_start_folder(ctx: Any, value: Any, params: dict) -> str:
    """Select an existing parent READ binding; the folder grants no authority."""
    if str(params.get("write_surface") or "").strip().lower() == "genesis":
        raise ValueError("workspace_root cannot select a starting folder for genesis, which creates its own empty project")
    folder = readonly_start_folder(value)
    write_root = str(params.get("write_root") or "").strip()
    if (str(params.get("write_surface") or "").strip().lower() == "external_workspace" and write_root
            and pathlib.Path(write_root).expanduser().resolve(strict=False) != pathlib.Path(folder)):
        raise ValueError("workspace_root and write_root name different folders; select one acting workspace")
    from ouroboros.tools.tool_resolution import _root_containing_absolute_path

    root = _root_containing_absolute_path(ctx, "read_file", folder)
    # External/Cyber parents can already READ off-home addresses through
    # user_files even when no finite root contains the address. Its existing
    # resolver decides that reach, including refusals for ordinary parents.
    binding = _access().build_resolved_resource_binding(
        ctx, root=root or "user_files", operation="read", path=folder)
    return str(binding.target_path)
