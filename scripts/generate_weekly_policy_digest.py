#!/usr/bin/env python3
"""Generate weekly policy digest proposal (no auto-apply)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vfriday.settings import load_settings


def main() -> None:
    settings = load_settings()
    db_path = settings.db_path
    if not db_path.exists():
        print("No Viktor-Friday DB found.")
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT session_id, snapshot_json, created_at
        FROM setpoint_snapshots
        WHERE created_at >= ?
        ORDER BY created_at ASC
        """,
        (cutoff,),
    ).fetchall()
    conn.close()

    by_key: dict[str, list[float]] = {}
    for row in rows:
        snap = json.loads(row["snapshot_json"])
        setpoints = snap.get("setpoints") or {}
        for key, value in setpoints.items():
            by_key.setdefault(str(key), []).append(float(value))

    trends = {}
    for key, values in by_key.items():
        if not values:
            continue
        trends[key] = {
            "first": values[0],
            "last": values[-1],
            "delta": values[-1] - values[0],
            "avg": sum(values) / len(values),
            "n": len(values),
        }

    out_dir = settings.repo_root / "reports" / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"weekly_policy_digest_{datetime.now(timezone.utc).date().isoformat()}.md"
    out_path.write_text(
        "# Weekly Policy Digest (Proposal)\n\n"
        "No policy values are changed automatically.\n"
        "Use this digest to prepare a PR proposal.\n\n"
        "## Trends (last 7 days)\n"
        "```json\n"
        f"{json.dumps(trends, ensure_ascii=False, indent=2)}\n"
        "```\n",
        encoding="utf-8",
    )
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
