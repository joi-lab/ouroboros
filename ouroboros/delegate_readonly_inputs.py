"""Materialize native lineage read authority for a folderless session request.

The engine accepts one readonly project scope, not additional read roots. Give
each invocation its own scratch with verified input copies; never pass parent
scratch or external runtime data as the engine's project. These copies confer
no workspace/write authority. Native and session inputs use one access owner.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import pathlib
import stat


def prepare_folderless_inputs(ctx, invocation_id: str) -> tuple[str, str]:
    from ouroboros.artifacts import stream_artifact_file
    from ouroboros.protected_artifacts import block_reason_for_path
    from ouroboros.task_custody import fence_publication
    from ouroboros.tool_access import (
        active_tool_profile,
        build_resolved_resource_binding,
        decide_tool_access,
        folderless_scratch_dir,
        lineage_read_roots,
    )
    from ouroboros.utils import write_bytes_atomic
    from ouroboros.tools.core_file_tools import _runtime_data_read_check

    scratch = folderless_scratch_dir(ctx)
    if scratch is None:
        return "", ""
    if not invocation_id or pathlib.Path(invocation_id).name != invocation_id:
        raise ValueError("invalid readonly invocation identity")
    reader = copy.copy(ctx)
    reader.task_constraint = {"mode": "local_readonly_subagent"}
    container = scratch / "delegated_readonly_inputs"
    if container.is_symlink() or container.resolve(strict=False).parent != scratch.resolve():
        raise ValueError("readonly scratch container is not owned by this task")
    root = container / invocation_id
    root.mkdir(parents=True, exist_ok=False)
    manifest = []
    runtime_check = _runtime_data_read_check(reader)
    for label in ("task_drive", "artifact_store"):
        if not decide_tool_access(profile=active_tool_profile(reader), root=label, operation="read").allow:
            continue
        for index, base in enumerate(lineage_read_roots(ctx, label)):
            try:
                if not stat.S_ISDIR(base.stat().st_mode):
                    raise OSError(f"lineage input container is not a directory: {base}")
            except FileNotFoundError:
                continue
            for source in _input_files(base):
                relative = source.relative_to(base)
                if source.is_symlink():
                    continue
                binding = build_resolved_resource_binding(reader, root=label, operation="read", path=str(source))
                if runtime_check(binding.target_path):
                    continue
                if block_reason_for_path(reader, binding.target_path, "read_bytes", binding):
                    continue
                local = pathlib.Path("inputs") / label / str(index) / relative
                # Verify the original once, then copy its exact bytes. The owned
                # copy does not inherit source permissions or workspace authority.
                fence_publication()
                contents = io.BytesIO()
                measured = stream_artifact_file(binding.target_path, contents)
                raw = contents.getvalue()
                write_bytes_atomic(root / local, raw)
                manifest.append({"root": label, "source": str(source), "local": local.as_posix(),
                                 "source_sha256": measured["sha256"], "source_size": measured["size"],
                                 "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)})
    (root / "inputs.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(root), (
        "\nREADONLY INPUTS: This invocation has its own scratch scope, not a workspace grant. "
        "inputs.json maps permitted native lineage sources to byte-identical verified local copies. "
        "Read those local paths when the work order names the original sources. "
        "The manifest describes staged copies, not an additional filesystem-read boundary.\n"
    )


def _input_files(base):
    def failed(exc):
        raise exc
    for folder, dirs, files in os.walk(base, followlinks=False, onerror=failed):
        dirs[:] = sorted(name for name in dirs if name != "delegated_readonly_inputs"
                         and not (pathlib.Path(folder) / name).is_symlink())
        for name in sorted(files):
            yield pathlib.Path(folder) / name
