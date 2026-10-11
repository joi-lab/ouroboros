"""Fixtures for local acceptance-preparation contracts and root-loop integration."""

import subprocess as sp

from tests.test_acceptance_delivery import _acceptance_ctx


def _ctx(tmp_path, **kwargs):
    kwargs.setdefault("max_improvement_passes", 1)
    ctx = _acceptance_ctx(tmp_path, **kwargs)
    ctx.tools._ctx._owner_directives = [{"source": "initial_user", "content": "goal"}]
    return ctx


def _fail(record, exc=None):
    from ouroboros.acceptance_preparation import record_preparation_failure

    return record_preparation_failure(record, exc or RuntimeError("builder exploded"))


def _expose(record):
    """What ``expose_acceptance_feedback`` records once the CURRENT attempt's
    carrying request came back answered."""
    record["feedback_delivered"] = True
    record["exposed_attempt"] = record["attempts"]
    return record


def _receipt_retry(ctx, record, rationale="the cause was repaired"):
    """A host-stored verification row, not an author's invented receipt hash."""
    from ouroboros.outcome_receipt_store import append_verification_receipt, read_context_verification_receipts

    tool_ctx = ctx.tools._ctx
    index = len(read_context_verification_receipts(tool_ctx, tool_ctx.task_id))
    assert append_verification_receipt(tool_ctx.drive_root, tool_ctx.task_id, {
        "check": "fixture source repair", "contract_kind": "explicit_command",
        "status": "pass", "returncode": 0, "ts": "2026-09-23T00:00:00Z",
    })
    return {"incident_id": record["incident_id"], "basis": "repair_evidence",
            "rationale": rationale, "verification_receipt_index": index}


def _repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    sp.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    sp.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    sp.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i"],
           cwd=repo, check=True, capture_output=True)
    return repo


def _raise_fingerprint(monkeypatch):
    import ouroboros.loop_delivery as delivery_mod

    def _explode(*_a, **_k):
        raise RuntimeError("the evidence fingerprint is what is broken")

    monkeypatch.setattr(delivery_mod, "delivery_evidence_fingerprint", _explode)
