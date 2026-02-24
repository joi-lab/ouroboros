#!/usr/bin/env python3
"""Generate PR proposal draft from latest benchmark report."""

from __future__ import annotations

import json
from pathlib import Path
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.settings import load_settings


def _latest_report(report_dir: Path) -> Path | None:
    files = sorted(report_dir.glob("bench-*.md"))
    if not files:
        return None
    return files[-1]


def main() -> None:
    settings = load_settings()
    generated_dir = settings.repo_root / "reports" / "generated"
    source = _latest_report(generated_dir)
    if source is None:
        print("No generated benchmark report found.")
        return

    text = source.read_text(encoding="utf-8")
    # Best-effort extraction of JSON block
    marker_start = "```json"
    marker_end = "```"
    start = text.find(marker_start)
    end = text.rfind(marker_end)
    payload = {}
    if start >= 0 and end > start:
        raw = text[start + len(marker_start):end].strip()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw}

    recommendation = str(payload.get("recommendation") or "No recommendation parsed.")
    report_id = str(payload.get("report_id") or source.stem)

    proposal = (
        f"# [PR Proposal] Solver Model Update ({report_id})\n\n"
        "## Context\n"
        "Weekly benchmark completed for Viktor-Benchmark.\n\n"
        "## Recommendation\n"
        f"{recommendation}\n\n"
        "## Safety Gate\n"
        "- This proposal does NOT apply config changes automatically.\n"
        "- Meta-Governor approval and explicit PR merge are required.\n"
    )

    out_dir = settings.repo_root / "reports" / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{report_id}_proposal.md"
    out_path.write_text(proposal, encoding="utf-8")
    print(f"Proposal written: {out_path}")


if __name__ == "__main__":
    main()
