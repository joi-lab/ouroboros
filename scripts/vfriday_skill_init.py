#!/usr/bin/env python3
"""Initialize Viktor-Friday skills state and base snapshot."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.skills_engine.state import init_skills_state


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize .vfriday state")
    parser.add_argument("--force", action="store_true", help="Re-create state and base snapshot")
    args = parser.parse_args()

    state = init_skills_state(ROOT, force=args.force)
    print(json.dumps({"status": "ok", "state": state}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

