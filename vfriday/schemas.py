"""Pydantic schemas for Viktor-Friday API and internal pipeline contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TriggerType(str, Enum):
    """Ingest trigger types used in Tutor loop."""

    PAUSE = "PAUSE"
    HELP_REQUEST = "HELP_REQUEST"
    CONTEXT_SWITCH = "CONTEXT_SWITCH"
    MANUAL_UPLOAD = "MANUAL_UPLOAD"


class SessionCreateRequest(BaseModel):
    """Create a new Viktor session."""

    student_alias: str = Field(..., min_length=1, max_length=120)
    topic: Optional[str] = Field(default=None, max_length=200)
    grade_level: Optional[str] = Field(default=None, max_length=64)
    goal: Optional[str] = Field(default=None, max_length=500)


class Session(BaseModel):
    """Session response."""

    session_id: str
    created_at: datetime
    active_setpoints: Dict[str, float]


class IngestEventRequest(BaseModel):
    """Ingest payload from Telegram gateway or API clients."""

    trigger_type: TriggerType
    problem_text: Optional[str] = None
    image_base64: Optional[str] = None
    ocr_text: Optional[str] = None
    latex_text: Optional[str] = None
    idle_seconds: Optional[float] = Field(default=None, ge=0)
    user_message: Optional[str] = None


class TutorTurnResponse(BaseModel):
    """Final response from orchestrator after one ingest turn."""

    status: str
    tutor_message: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    requires_attempt: bool
    flags: List[str]
    trace_id: str


class SessionState(BaseModel):
    """Current session state snapshot."""

    setpoints: Dict[str, float]
    stress: Dict[str, float]
    last_events: List[Dict[str, Any]]
    budget_snapshot: Dict[str, Any]


class BenchmarkRunRequest(BaseModel):
    """Run benchmark against candidate model set."""

    candidate_models: List[str] = Field(..., min_length=1)
    sample_size: int = Field(default=20, ge=1, le=500)


class BenchmarkRunResult(BaseModel):
    """Benchmark response."""

    report_id: str
    summary: Dict[str, Any]
    recommendation: str


class OCRPrepResult(BaseModel):
    """Normalized OCR+text output used by solver stage."""

    normalized_problem: str
    normalized_working: str
    source: str
    usage: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class SolverClaim(BaseModel):
    """Structured symbolic claim emitted by solver."""

    claim_type: str = Field(default="equality")
    lhs: Optional[str] = None
    rhs: Optional[str] = None
    expr: Optional[str] = None
    var: Optional[str] = None
    equals: Optional[str] = None


class SolverResult(BaseModel):
    """Solver output consumed by verifier/tutor."""

    status: str = "ok"
    model: str
    explanation: str
    error_found: bool = False
    error_type: Optional[str] = None
    error_step: Optional[int] = None
    confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    symbolic_claims: List[SolverClaim] = Field(default_factory=list)
    usage: Dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    raw: Dict[str, Any] = Field(default_factory=dict)


class VerifierResult(BaseModel):
    """Verifier summary over solver claims."""

    status: str = "ok"
    checked_claims: int = 0
    passed_claims: int = 0
    failed_claims: int = 0
    disagreement_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    details: List[Dict[str, Any]] = Field(default_factory=list)


class TutorResult(BaseModel):
    """Tutor draft after policy and guard layers."""

    model: str
    message: str
    confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    requires_attempt: bool = True
    usage: Dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0
    flags: List[str] = Field(default_factory=list)


class GoodhartScore(BaseModel):
    """Hidden evaluator output used for governance metrics."""

    hidden_score: float = Field(default=0.0, ge=0.0, le=1.0)
    leakage_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    flags: List[str] = Field(default_factory=list)
    competency_credit: float = Field(default=0.0, ge=0.0, le=1.0)
