import tempfile

from fastapi.testclient import TestClient

from vfriday.app import create_app
from vfriday.schemas import SolverClaim, SolverResult


def _make_client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("VFRIDAY_DATA_DIR", tmp)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app()
    return TestClient(app), app


def _create_session(client: TestClient) -> str:
    res = client.post(
        "/v1/sessions",
        json={
            "student_alias": "Viktor",
            "topic": "physics",
            "grade_level": "8",
            "goal": "build transfer",
        },
    )
    res.raise_for_status()
    return res.json()["session_id"]


def test_happy_path_text_to_hint(monkeypatch):
    client, _app = _make_client(monkeypatch)
    sid = _create_session(client)
    res = client.post(
        f"/v1/sessions/{sid}/ingest",
        json={
            "trigger_type": "HELP_REQUEST",
            "problem_text": "Projectile motion, find y-projection.",
            "user_message": "I used cosine but not sure.",
        },
    )
    res.raise_for_status()
    body = res.json()
    assert body["status"] in {"ok", "uncertain"}
    assert body["tutor_message"]
    assert "trace_id" in body


def test_image_only_upload_fallback(monkeypatch):
    client, _app = _make_client(monkeypatch)
    sid = _create_session(client)
    res = client.post(
        f"/v1/sessions/{sid}/ingest",
        json={
            "trigger_type": "MANUAL_UPLOAD",
            "image_base64": "dGVzdA==",
            "user_message": "",
        },
    )
    res.raise_for_status()
    assert res.json()["tutor_message"]


def test_verifier_disagreement_surfaces_uncertain(monkeypatch):
    client, app = _make_client(monkeypatch)
    sid = _create_session(client)

    def fake_solver(**_kwargs):
        return SolverResult(
            status="ok",
            model="fake-solver",
            explanation="Student likely made a symbolic mistake.",
            error_found=True,
            error_type="symbolic_error",
            error_step=2,
            confidence=0.8,
            symbolic_claims=[SolverClaim(claim_type="equality", lhs="2+2", rhs="5")],
            usage={"cost": 0.0},
            latency_ms=10,
            raw={"mode": "fake"},
        )

    # Patch alias imported in pipeline module
    import vfriday.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "solver_solve", fake_solver)
    res = client.post(
        f"/v1/sessions/{sid}/ingest",
        json={
            "trigger_type": "HELP_REQUEST",
            "problem_text": "Test problem",
            "user_message": "Test work",
        },
    )
    res.raise_for_status()
    body = res.json()
    assert body["status"] == "uncertain"
    assert "verifier_disagreement" in body["flags"]


def test_budget_guard_blocks_over_cap(monkeypatch):
    client, app = _make_client(monkeypatch)
    sid = _create_session(client)
    storage = app.state.storage
    storage.add_budget_entry(
        trace_id="prefill",
        session_id=sid,
        category="tutor",
        amount_usd=999.0,
        model="test",
        metadata={},
    )
    res = client.post(
        f"/v1/sessions/{sid}/ingest",
        json={
            "trigger_type": "HELP_REQUEST",
            "problem_text": "Any",
            "user_message": "Any",
        },
    )
    res.raise_for_status()
    assert res.json()["status"] == "budget_blocked"
