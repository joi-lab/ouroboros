#!/usr/bin/env python3
"""Run Viktor-Friday retention cleanup job."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.settings import load_settings
from vfriday.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Retention window in days")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    storage = Storage(settings.db_path, settings.audit_jsonl_path)
    result = storage.run_retention(retention_days=args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
