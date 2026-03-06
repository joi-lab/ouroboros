#!/usr/bin/env python3
"""Print skills engine state and applied skills."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.skills_engine.state import load_state


def main() -> None:
    state = load_state(ROOT)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

