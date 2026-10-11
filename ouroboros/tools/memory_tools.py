"""Memory registry tools for tracking data sources, gaps, and trust."""

from ouroboros.tools.tool_result import ToolResult, _publish_tool_result, completed_local_read

import re
import logging
from pathlib import Path
from typing import List

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

REGISTRY_PATH = "memory/registry.md"


def _registry_file(ctx: ToolContext) -> Path:
    return ctx.drive_path(REGISTRY_PATH)


@completed_local_read
def _memory_map(ctx: ToolContext) -> str:
    """Read the memory registry — a map of all data sources."""
    path = _registry_file(ctx)
    if not path.exists():
        return (
            "Memory registry not found at memory/registry.md.\n"
            "Use memory_update_registry to create entries."
        )
    return path.read_text(encoding="utf-8")


def _memory_update_registry(
    ctx: ToolContext, source_id: str, updates: str
) -> str:
    """Update or create an entry in the memory registry."""
    if not source_id or not isinstance(source_id, str):
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=("⚠️ source_id must be a non-empty string.")))
    source_id = source_id.strip()
    if "/" in source_id or "\\" in source_id or ".." in source_id:
        return _publish_tool_result(ctx, ToolResult(status="error", code="TOOL_ARG_ERROR", text=("⚠️ Invalid characters in source_id.")))

    path = _registry_file(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        content = path.read_text(encoding="utf-8")
    else:
        content = "# Memory Registry\n\nMetacognitive map: what I know, what I don't, and where to look.\n\n"

    pattern = rf'^### {re.escape(source_id)}\s*$'
    lines = content.split("\n")
    start_idx = None
    end_idx = None

    for i, line in enumerate(lines):
        if re.match(pattern, line):
            start_idx = i
        elif start_idx is not None and line.startswith("### "):
            end_idx = i
            break

    new_section = f"### {source_id}\n{updates.strip()}\n"

    if start_idx is not None:
        if end_idx is None:
            end_idx = len(lines)
        while end_idx > start_idx and not lines[end_idx - 1].strip():
            end_idx -= 1
        lines[start_idx:end_idx] = [new_section]
        content = "\n".join(lines)
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + new_section

    path.write_text(content, encoding="utf-8")
    return f"✅ Registry entry '{source_id}' updated."

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("memory_map", {
            "name": "memory_map",
            "description": (
                "Show the memory registry — a map of all data sources "
                "the agent has access to, with coverage, gaps, and trust levels. "
                "Use BEFORE generating content to verify you have actual source data, "
                "not just cached impressions."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            },
        }, _memory_map),
        ToolEntry("memory_update_registry", {
            "name": "memory_update_registry",
            "description": (
                "Update or create an entry in the memory registry. "
                "Use after acquiring new data sources or discovering gaps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_id": {
                        "type": "string",
                        "description": "Source identifier (e.g. 'user-context', 'project-notes')"
                    },
                    "updates": {
                        "type": "string",
                        "description": "Full entry content in markdown (- **Path:** ... \\n- **Type:** ... etc)"
                    }
                },
                "required": ["source_id", "updates"]
            },
        }, _memory_update_registry),
    ]
