#!/usr/bin/env python3
"""Run a verification command with disposable filesystem and configuration roots.

Use `python -I -S scripts/safe_test.py --temp-parent /tmp -- <venv-python> -m pytest ...`.
This stdlib launcher audits its environment helper before loading it, then prints
the boundary before starting the command. It installs nothing and is not a sandbox.

It never deletes the tree it creates. A command's descendants can outlive the
command, and exit status is not proof that they are gone, so the launcher keeps
the new root in place and prints its exact path (`SAFE_TEST_RETAINED`); whoever
later proves the run's processes gone removes it. Retention is ordinary, not a
failure: the exit status is the command's own.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile


def _git_ancestor(path: Path):
    """The nearest directory at or above ``path`` that Git would treat as a checkout."""
    for directory in (path, *path.parents):
        if (directory / ".git").exists():
            return directory
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("supply a verification command after --")
    repo = Path(__file__).resolve().parents[1]
    parent = Path(args.temp_parent or tempfile.gettempdir()).resolve()
    if parent == repo or repo in parent.parents:
        # A disposable root nested in the checkout is not disposable: `git`
        # started in it walks up into THIS working tree, and a run that
        # snapshots or resets its own temp path then operates on the
        # operator's source. Refuse instead of accepting a false boundary.
        parser.error("--temp-parent must be outside the repository working tree")
    ancestor = _git_ancestor(parent)
    if ancestor is not None:
        # The same hazard one level up: any enclosing checkout, not only this one.
        parser.error(f"--temp-parent must not be inside a Git working tree ({ancestor})")
    helper = repo / "ouroboros" / "test_environment.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([item.name for item in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        if any(name not in {"__future__", "ast", "os", "pathlib"} for name in names):
            raise RuntimeError("test environment helper must import only its audited stdlib leaves")
    boundary = runpy.run_path(str(helper))["isolated_environment"]
    # Short on purpose: server sockets live several levels below this root and
    # macOS caps an AF_UNIX path at 104 bytes.
    root = Path(tempfile.mkdtemp(prefix="ob-", dir=parent)).resolve()
    try:
        env = boundary(root, repo, keep=(
            "OUROBOROS_RUN_UI_SMOKE", "OUROBOROS_EXPECT_BROWSER_ENGINES",
            "OUROBOROS_PREFLIGHT_TEST_WORKERS", "OUROBOROS_PREFLIGHT_SERIAL",
            "PLAYWRIGHT_BROWSERS_PATH", "OUROBOROS_E2E_DEEP",
        ))
        # Each pytest controller claims its own fresh basetemp beneath this retained
        # root (tests/conftest.py): pytest deletes an EXISTING --basetemp, so no two
        # invocations — e.g. run_tests.py's two passes — may ever share one.
        env["OUROBOROS_TEST_TEMP_ROOT"] = str(root)
        print("SAFE_TEST_BOUNDARY " + json.dumps({key: value for key, value in env.items()
              if key in {"HOME", "PYTHONUSERBASE", "PYTHONPYCACHEPREFIX"}
              or key.startswith("OUROBOROS_")}), flush=True)
        # Complete environment replacement reaches the interpreter BEFORE site,
        # plugins, conftest or any application module can import.
        return subprocess.call(command, cwd=repo, env=env)
    finally:
        print(f"SAFE_TEST_RETAINED {root}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
