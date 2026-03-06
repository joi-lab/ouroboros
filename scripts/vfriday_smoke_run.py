#!/usr/bin/env python3
"""CLI smoke run for Viktor-Friday orchestrator."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from vfriday.app import create_app


def main() -> None:
    app = create_app()
    client = TestClient(app)

    created = client.post(
        "/v1/sessions",
        json={
            "student_alias": "Viktor",
            "topic": "kinematics",
            "grade_level": "8-9",
            "goal": "debug projection errors",
        },
    )
    created.raise_for_status()
    session = created.json()
    sid = session["session_id"]

    turn = client.post(
        f"/v1/sessions/{sid}/ingest",
        json={
            "trigger_type": "HELP_REQUEST",
            "problem_text": "A body moves at angle alpha. Find projection on y-axis.",
            "user_message": "I wrote v_y = v cos(alpha), not sure.",
        },
    )
    turn.raise_for_status()

    state = client.get(f"/v1/sessions/{sid}/state")
    state.raise_for_status()

    print("SMOKE OK")
    print(json.dumps({"session": session, "turn": turn.json(), "state": state.json()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
