#!/usr/bin/env python3
"""Apply a Viktor-Friday skill package."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.skills_engine.apply import apply_skill


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a skill directory")
    parser.add_argument("skill_dir", help="Path to .claude/skills/<skill>")
    args = parser.parse_args()

    skill_dir = pathlib.Path(args.skill_dir)
    if not skill_dir.is_absolute():
        skill_dir = (ROOT / skill_dir).resolve()

    result = apply_skill(project_root=ROOT, skill_dir=skill_dir)
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    if not result.success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

