#!/usr/bin/env python3
"""Standalone entry point for Ouroboros (non-Colab deployment).

Usage:
    python standalone_launcher.py
    python standalone_launcher.py --data-dir /srv/ouroboros/data
    python standalone_launcher.py --branch ouroboros-stable

Reads secrets and config from a .env file (default: .env in CWD or repo root).
Sets up the local data directory and delegates to colab_launcher.py.
"""

import argparse
import os
import pathlib
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Ouroboros standalone launcher")
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("OUROBOROS_DATA_DIR", "/opt/ouroboros/data"),
        help="Root directory for persistent data (state, logs, memory). "
             "Default: /opt/ouroboros/data or $OUROBOROS_DATA_DIR",
    )
    parser.add_argument(
        "--repo-dir",
        default=os.environ.get("OUROBOROS_REPO_DIR", "/opt/ouroboros/repo"),
        help="Directory for the git working copy. "
             "Default: /opt/ouroboros/repo or $OUROBOROS_REPO_DIR",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Git branch to boot from. Overrides $OUROBOROS_BOOT_BRANCH.",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to .env file. Default: .env in repo root, then CWD.",
    )
    args = parser.parse_args()

    # --- Load .env ---
    env_file = args.env_file
    if env_file is None:
        # Try repo root first, then CWD
        candidates = [
            pathlib.Path(__file__).resolve().parent / ".env",
            pathlib.Path.cwd() / ".env",
        ]
        for candidate in candidates:
            if candidate.is_file():
                env_file = str(candidate)
                break

    if env_file:
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file, override=False)
            print(f"[standalone] Loaded env from {env_file}")
        except ImportError:
            print("[standalone] WARNING: python-dotenv not installed. "
                  "Install it (pip install python-dotenv) or export env vars manually.")

    # --- Propagate CLI args to environment ---
    os.environ["OUROBOROS_DATA_DIR"] = str(pathlib.Path(args.data_dir).resolve())
    os.environ["OUROBOROS_REPO_DIR"] = str(pathlib.Path(args.repo_dir).resolve())

    if args.branch:
        os.environ["OUROBOROS_BOOT_BRANCH"] = args.branch

    # Default worker start method to "fork" on Linux (matches Colab behaviour)
    os.environ.setdefault("OUROBOROS_WORKER_START_METHOD", "fork")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    # --- Create data directory structure ---
    data_root = pathlib.Path(os.environ["OUROBOROS_DATA_DIR"])
    for sub in ("state", "logs", "memory", "index", "locks", "archive"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)

    repo_dir = pathlib.Path(os.environ["OUROBOROS_REPO_DIR"])
    repo_dir.mkdir(parents=True, exist_ok=True)

    print(f"[standalone] data_dir = {data_root}")
    print(f"[standalone] repo_dir = {repo_dir}")

    # --- Launch via the bootstrap shim (handles git clone/pull + launcher) ---
    shim_path = pathlib.Path(__file__).resolve().parent / "colab_bootstrap_shim.py"
    if not shim_path.exists():
        # Fallback: run colab_launcher.py directly (repo already checked out)
        shim_path = pathlib.Path(__file__).resolve().parent / "colab_launcher.py"

    assert shim_path.exists(), f"Missing launcher: {shim_path}"
    print(f"[standalone] Launching {shim_path.name} ...")

    os.execv(sys.executable, [sys.executable, str(shim_path)])


if __name__ == "__main__":
    main()
