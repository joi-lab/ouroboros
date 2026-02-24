"""FastAPI app for Viktor-Friday MVP orchestrator."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from vfriday.pipeline import Orchestrator
from vfriday.schemas import (
    BenchmarkRunRequest,
    BenchmarkRunResult,
    IngestEventRequest,
    Session,
    SessionCreateRequest,
    SessionState,
    TutorTurnResponse,
)
from vfriday.settings import load_settings
from vfriday.storage import Storage


def create_app() -> FastAPI:
    """Application factory."""
    settings = load_settings()
    storage = Storage(settings.db_path, settings.audit_jsonl_path)
    orchestrator = Orchestrator(settings, storage)

    app = FastAPI(title="Viktor-Friday Orchestrator", version="0.1.0")
    app.state.settings = settings
    app.state.storage = storage
    app.state.orchestrator = orchestrator

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "service": "vfriday-orchestrator"}

    @app.post("/v1/sessions", response_model=Session)
    def create_session(req: SessionCreateRequest) -> Session:
        return orchestrator.create_session(req)

    @app.post("/v1/sessions/{session_id}/ingest", response_model=TutorTurnResponse)
    def ingest(session_id: str, req: IngestEventRequest) -> TutorTurnResponse:
        if not storage.get_session(session_id):
            raise HTTPException(status_code=404, detail=f"session_not_found:{session_id}")
        return orchestrator.ingest(session_id, req)

    @app.get("/v1/sessions/{session_id}/state", response_model=SessionState)
    def state(session_id: str) -> SessionState:
        if not storage.get_session(session_id):
            raise HTTPException(status_code=404, detail=f"session_not_found:{session_id}")
        return orchestrator.get_state(session_id)

    @app.post("/v1/admin/benchmark/run", response_model=BenchmarkRunResult)
    def benchmark(req: BenchmarkRunRequest) -> BenchmarkRunResult:
        return orchestrator.run_benchmark(req.candidate_models, req.sample_size)

    @app.post("/v1/admin/retention")
    def retention(days: int = 30) -> dict:
        return orchestrator.run_retention(retention_days=days)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    cfg = load_settings()
    uvicorn.run(
        "vfriday.app:app",
        host=cfg.api_host,
        port=cfg.api_port,
        reload=False,
    )

