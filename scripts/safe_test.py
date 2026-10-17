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

`--timeout SECONDS` is a deadline that stops everything the command started, which
`subprocess.run(timeout=)`, `perl -e 'alarm N; exec ...'` or killing the runner's PID
do not: `node --test`, for one, keeps each test file in a child that outlives its
runner. The command gets a session of its own; at the deadline its group and every
descendant (captured first, whatever group it joined) get SIGTERM, SIGKILL ten
seconds later, and the launcher exits 124 like GNU `timeout`. A SIGINT, SIGTERM or
SIGHUP that stops the launcher stops the command the same way. Windows force-stops
the tree at once with `taskkill /T`.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time

_KILL_AFTER_SECONDS = 10  # --timeout: from SIGTERM to SIGKILL
_TIMEOUT_STATUS = 124  # GNU timeout's status for an expired deadline


def _git_ancestor(path: Path):
    """The nearest directory at or above ``path`` that Git would treat as a checkout."""
    for directory in (path, *path.parents):
        if (directory / ".git").exists():
            return directory
    return None


def _descendants(root: int) -> list[int]:
    """Every descendant of ``root`` in one ``ps`` snapshot, whatever process group it joined."""
    try:
        listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True,
                                 timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            children.setdefault(int(fields[1]), []).append(int(fields[0]))
    found: list[int] = []
    frontier = [root]
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in found:
                found.append(child)
                frontier.append(child)
    return found


def _signal_all(group: int, members: list[int], signum: int) -> None:
    for send, target in ((os.killpg, group), *((os.kill, pid) for pid in members)):
        try:
            send(target, signum)
        except OSError:  # already gone, or no longer ours to signal
            pass


def _exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # EPERM: it exists, but is not ours
    return True


def _stop(process: subprocess.Popen) -> None:
    """Stop the command's group and every descendant. The tree is captured before the first
    signal: a parent that dies first orphans its children, and a test may have moved them
    into groups of their own (``tests/group_run.py``)."""
    if os.name == "nt":  # a console tree has no group SIGTERM
        subprocess.call(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, signal.SIG_IGN)  # a second signal must not abandon a half-stopped tree
        members = _descendants(process.pid)
        _signal_all(process.pid, members, signal.SIGTERM)
        grace = time.monotonic() + _KILL_AFTER_SECONDS
        while time.monotonic() < grace and (process.poll() is None or any(map(_exists, members))):
            time.sleep(0.1)
        late = _descendants(process.pid) if process.poll() is None else []
        _signal_all(process.pid, members + late, signal.SIGKILL)
    process.wait()


def _call_with_deadline(command: list[str], repo: Path, env: dict, seconds: float) -> int:
    """``subprocess.call`` whose deadline, or a signal that stops the launcher, stops the whole tree."""
    if os.name == "nt":
        process = subprocess.Popen(command, cwd=repo, env=env,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        process = subprocess.Popen(command, cwd=repo, env=env, start_new_session=True)
        for signum in (signal.SIGTERM, signal.SIGHUP):  # the command's own session no longer gets them
            signal.signal(signum, lambda received, _frame: sys.exit(128 + received))
    try:
        return process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        print(f"SAFE_TEST_TIMEOUT after {seconds:g}s: stopping the command and every process it started",
              file=sys.stderr, flush=True)
        _stop(process)
        return _TIMEOUT_STATUS
    except BaseException:  # Ctrl-C, or SIGTERM/SIGHUP turned into SystemExit above
        _stop(process)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-parent", type=Path)
    parser.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="stop the command and every process it started after SECONDS; exit 124")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("supply a verification command after --")
    if args.timeout is not None and not args.timeout > 0:
        parser.error("--timeout must be a positive number of seconds")
    repo = Path(__file__).resolve().parents[1]
    helper = repo / "ouroboros" / "test_environment.py"
    tree = ast.parse(helper.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([item.name for item in node.names] if isinstance(node, ast.Import)
                 else [node.module] if isinstance(node, ast.ImportFrom) else [])
        if any(name not in {"__future__", "ast", "os", "pathlib", "sys", "tempfile"} for name in names):
            raise RuntimeError("test environment helper must import only its audited stdlib leaves")
    environment = runpy.run_path(str(helper))
    boundary = environment["isolated_environment"]
    parent = Path(args.temp_parent or environment["default_temp_parent"]()).resolve()
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
    # Short on purpose: server sockets live several levels below this root and
    # macOS caps an AF_UNIX path at 104 bytes.
    root = Path(tempfile.mkdtemp(prefix="ob-", dir=parent)).resolve()
    try:
        env = boundary(root, repo, keep=(
            "OUROBOROS_RUN_UI_SMOKE", "OUROBOROS_EXPECT_BROWSER_ENGINES",
            "OUROBOROS_PREFLIGHT_TEST_WORKERS", "OUROBOROS_PREFLIGHT_SERIAL",
            "PLAYWRIGHT_BROWSERS_PATH", "OUROBOROS_E2E_DEEP", "OUROBOROS_UI_EVIDENCE_DIR",
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
        if args.timeout is None:
            return subprocess.call(command, cwd=repo, env=env)
        return _call_with_deadline(command, repo, env, args.timeout)
    finally:
        print(f"SAFE_TEST_RETAINED {root}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
