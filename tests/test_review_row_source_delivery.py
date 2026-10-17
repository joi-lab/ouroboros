"""Per-seat frozen roots reach the real review-request handoff, not just its plan."""

import asyncio
from types import SimpleNamespace

from ouroboros.review_execution import ReviewRouteKind
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.review_multi_model import _multi_model_review_async


def test_each_retrieving_request_keeps_its_root_and_preflight_exhibit(tmp_path, monkeypatch):
    roots = [tmp_path / name for name in ("native-payload", "session-payload", "shared")]
    for index, root in enumerate(roots):
        root.mkdir()
        (root / "subject.txt").write_text(f"frozen subject {index}", encoding="utf-8")
    drive = tmp_path / "data"
    drive.mkdir()
    ctx = ToolContext(repo_dir=roots[-1], drive_root=drive)
    seen = {}
    exhibit = {"tests": "passed", "candidate_tree": "frozen-tree"}

    def capture(request, *, slots, drive_root, llm, usage_ctx=None):
        from pathlib import Path

        slot = slots[0]
        seen[slot.slot_id] = (
            request.session_root,
            (Path(request.session_root) / "subject.txt").read_text(encoding="utf-8"),
            request.evidence.get("preflight_execution"),
        )
        return SimpleNamespace(actors=[{
            "slot_id": slot.slot_id, "model": slot.model, "status": "ok",
            "raw_text": "[]", "usage": {}, "prompt_ref": {}, "response_ref": {},
        }])

    monkeypatch.setattr("ouroboros.review_substrate.run_review_request", capture)
    monkeypatch.setattr("ouroboros.tools.review.LLMClient", lambda: object())
    routes = [ReviewRouteKind.API_CHAT, ReviewRouteKind.AGENT_SESSION, ReviewRouteKind.API_CHAT]
    plan = {
        "slot_ids": ["native", "session", "fallback"], "retrieves": [True] * 3,
        "session_roots": [str(roots[0]), str(roots[1]), ""],
        "session_accesses": ["", "readonly", ""],
    }
    result = asyncio.run(_multi_model_review_async(
        "Review frozen subjects", "", ["test/native", "test/session", "test/fallback"],
        ctx, routes=routes, row_plan=plan, session_task="Read subject.txt",
        session_root=str(roots[-1]), checking_source={}, preflight_execution=exhibit,
    ))
    assert len(result["results"]) == 3
    assert seen == {
        slot: (str(roots[index]), f"frozen subject {index}", exhibit)
        for index, slot in enumerate(("native", "session", "fallback"))
    }
