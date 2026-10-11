"""Every server stop seam stops the historical audit.

lifespan's ``finally``, Panic and the forced emergency cleanup (the restart path
that can skip lifespan's ``finally``) all latch the audit stop; the one launch
sits after supervisor readiness. Static check over the composition root, because
importing ``server`` in a unit test drags the whole runtime.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]


def _function_body(source: str, header: str) -> str:
    body = source[source.index(header):]
    nxt = re.search(r"\n(?:async )?def ", body[1:])
    return body[: nxt.start() + 1] if nxt else body


def test_every_server_stop_seam_stops_the_audit():
    server = (REPO / "server.py").read_text(encoding="utf-8")
    control = (REPO / "ouroboros" / "server_control.py").read_text(encoding="utf-8")
    for header in ("async def lifespan", "def _emergency_process_cleanup"):
        assert "_historical_audit.stop()" in _function_body(server, header), header
    assert "_historical_audit.start(DATA_DIR, REPO_DIR)" in _function_body(server, "def _run_supervisor")
    assert "audit.stop()" in _function_body(control, "def execute_panic_stop")
