#!/usr/bin/env python3
"""Run Viktor benchmark and persist report via orchestrator services."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.pipeline import Orchestrator
from vfriday.settings import load_settings
from vfriday.storage import Storage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models",
        default="o3,gpt-5-mini,gpt-4.1",
        help="Comma-separated candidate models",
    )
    parser.add_argument("--sample-size", type=int, default=20, help="Number of non-holdout cases to evaluate")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = load_settings()
    storage = Storage(settings.db_path, settings.audit_jsonl_path)
    orchestrator = Orchestrator(settings, storage)
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    result = orchestrator.run_benchmark(models, args.sample_size)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
