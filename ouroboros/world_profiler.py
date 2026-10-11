import os
import platform
import shutil

from ouroboros.platform_layer import (
    bootstrap_process_path,
    get_cpu_info,
    get_system_memory,
    node_runtime_health,
    resolve_bundled_node,
)

def generate_world_profile(output_path: str):
    """Generates a WORLD.md file containing the system profile and hardware details."""
    
    os_name = platform.system()
    os_release = platform.release()
    arch = platform.machine()
    
    mem_total = get_system_memory()
    cpu_info = get_cpu_info()
        
    # User and paths
    user = os.environ.get("USER", "unknown")
    cwd = os.getcwd()
    
    # Check for CLI tools against the PATH the tool subprocesses will actually
    # see (the shell/verify surfaces bootstrap before resolving).
    bootstrap_process_path()
    # `which` success is not runnability: a corrupted PATH node is SIGKILLed on
    # launch, so WORLD.md must not enshrine it — or the npm that starts through
    # `#!/usr/bin/env node` — as usable. Probe node ONCE up front; npm below is
    # gated on a usable node (PATH-healthy, or the bundled emergency fallback).
    node_path = shutil.which("node")
    node_health = node_runtime_health(node_path, timeout_sec=3) if node_path else None
    path_node_ok = bool(node_health is not None and node_health.healthy)
    bundled_ok = False
    if not path_node_ok:
        # Consulted lazily: a healthy PATH node needs no bundle at all.
        bundled = resolve_bundled_node()
        bundled_ok = bool(bundled) and node_runtime_health(bundled, timeout_sec=3).healthy
    node_usable = path_node_ok or bundled_ok
    tools = []
    for tool in ["git", "python3", "python", "pip", "npm", "node", "claude"]:
        located = shutil.which(tool)
        if tool == "node":
            if node_health is not None and node_health.healthy:
                tools.append(tool)
            elif node_path and node_health is not None and node_health.status == "broken":
                # Only a PROBED failure earns the broken label; a stat-level
                # miss (vanished/not-executable) reads as absent, like any
                # other missing tool (T16).
                suffix = "; bundled fallback available)" if bundled_ok else ")"
                tools.append(f"node (broken: {node_health.reason or node_health.status}{suffix}")
            elif bundled_ok:
                tools.append("node (bundled fallback available)")
            continue
        if tool == "npm" and located and not node_usable:
            tools.append("npm (needs a working node)")
            continue
        if located:
            tools.append(tool)
            
    content = f"""# WORLD.md — Environment Profile

This is where I currently exist. It defines my hardware, OS, and local constraints.

## System
- **OS**: {os_name} {os_release} ({arch})
- **CPU**: {cpu_info}
- **RAM**: {mem_total}
- **User**: {user}
- **Current Directory**: {cwd}

## Available Tools
The following binaries are available in my PATH:
`{', '.join(tools)}`

## File System Rules
I live inside `~/Ouroboros/`. 
- `repo/` contains my codebase.
- `data/` contains my memory, state, and logs.
I should generally confine my writes to these directories, though I have read access to the rest of the filesystem if needed for exploration.

*(Generated automatically on first boot)*
"""
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

if __name__ == "__main__":
    generate_world_profile("WORLD.md")
