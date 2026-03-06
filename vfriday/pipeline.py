"""Core orchestrator pipeline: OCR -> Solver -> Verifier -> Tutor -> Governance."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from vfriday.agents.solver import solve as solver_solve
from vfriday.agents.tutor import compose_hint
from vfriday.benchmark.runner import run_benchmark
from vfriday.governance.goodhart import (
    evaluate_hidden_score,
    model_observed_setpoint_targets,
    non_transfer_recurrence,
)
from vfriday.governance.hint_guard import apply_hint_guard
from vfriday.governance.setpoints import update_setpoints
from vfriday.governance.stress import compute_shared_stress, make_ai_factors, make_viktor_factors
from vfriday.ocr.parse import prepare_ocr_payload
from vfriday.schemas import (
    BenchmarkRunResult,
    IngestEventRequest,
    Session,
    SessionCreateRequest,
    SessionState,
    SolverResult,
    TriggerType,
    TutorTurnResponse,
)
from vfriday.settings import VFridaySettings
from vfriday.storage import Storage
from vfriday.telemetry.triggers import normalize_trigger
from vfriday.verifier.sympy_engine import verify_solver_claims


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Orchestrator:
    """Application service encapsulating MVP pipeline behavior."""

    def __init__(self, settings: VFridaySettings, storage: Storage):
        self.settings = settings
        self.storage = storage

    def create_session(self, req: SessionCreateRequest) -> Session:
        setpoints = self.settings.policy.get("setpoints", {})
        created = self.storage.create_session(
            student_alias=req.student_alias,
            topic=req.topic,
            grade_level=req.grade_level,
            goal=req.goal,
            active_setpoints={k: float(v) for k, v in setpoints.items()},
        )
        return Session(
            session_id=created["session_id"],
            created_at=datetime.fromisoformat(created["created_at"]),
            active_setpoints=created["active_setpoints"],
        )

    def _budget_blocked(self, session_id: str) -> Tuple[bool, Dict[str, Any]]:
        monthly_cap = float(self.settings.budget.get("monthly_cap_usd", 150.0))
        per_session_cap = float(self.settings.budget.get("per_session_soft_cap_usd", 8.0))
        snapshot = self.storage.budget_snapshot(monthly_cap, per_session_cap, session_id)
        blocked = (
            float(snapshot["monthly_spent_usd"]) >= monthly_cap
            or float(snapshot["session_spent_usd"]) >= per_session_cap
        )
        return blocked, snapshot

    @staticmethod
    def _coerce_solver_dict(solver_result: SolverResult) -> Dict[str, Any]:
        return solver_result.model_dump()

    def _recent_error_types(self, session_id: str, limit: int = 10) -> List[str]:
        events = self.storage.get_recent_events(session_id, limit=limit)
        out: List[str] = []
        for evt in events:
            payload = evt.get("payload") or {}
            err = payload.get("solver_error_type")
            if err:
                out.append(str(err))
        return out

    @staticmethod
    def _has_student_attempt(req: IngestEventRequest) -> bool:
        return bool((req.user_message or "").strip() or (req.ocr_text or "").strip() or (req.latex_text or "").strip())

    def ingest(self, session_id: str, req: IngestEventRequest) -> TutorTurnResponse:
        trace_id = uuid.uuid4().hex[:12]
        session = self.storage.get_session(session_id)
        if not session:
            return TutorTurnResponse(
                status="error",
                tutor_message=f"Unknown session: {session_id}",
                confidence=0.0,
                requires_attempt=True,
                flags=["session_not_found"],
                trace_id=trace_id,
            )

        blocked, budget = self._budget_blocked(session_id)
        if blocked:
            self.storage.save_event(trace_id, session_id, "budget_blocked", {"budget_snapshot": budget})
            return TutorTurnResponse(
                status="budget_blocked",
                tutor_message="Бюджетный лимит достигнут. Нужен апрув Meta-Governor перед продолжением.",
                confidence=0.0,
                requires_attempt=True,
                flags=["budget_cap_reached"],
                trace_id=trace_id,
            )

        trigger = normalize_trigger(
            requested=req.trigger_type,
            idle_seconds=req.idle_seconds,
            user_message=req.user_message,
        )
        self.storage.save_event(
            trace_id,
            session_id,
            "ingest_received",
            {
                "trigger_type": trigger.value,
                "idle_seconds": req.idle_seconds,
                "has_image": bool(req.image_base64),
                "problem_text": req.problem_text,
                "ocr_text": req.ocr_text,
                "latex_text": req.latex_text,
                "user_message": req.user_message,
            },
        )

        ocr = prepare_ocr_payload(
            problem_text=req.problem_text,
            ocr_text=req.ocr_text,
            latex_text=req.latex_text,
            user_message=req.user_message,
            image_base64=req.image_base64,
            ocr_model=str(self.settings.models.get("ocr_model", "")),
        )
        if float((ocr.usage or {}).get("cost") or 0.0) > 0:
            self.storage.add_budget_entry(
                trace_id,
                session_id,
                "ocr",
                float(ocr.usage.get("cost") or 0.0),
                str(self.settings.models.get("ocr_model")),
                {"source": ocr.source},
            )

        solver = solver_solve(
            problem_text=ocr.normalized_problem,
            working_text=ocr.normalized_working,
            model=str(self.settings.models.get("solver_model")),
            reasoning_effort="high",
        )
        self.storage.save_solver_run(
            trace_id=trace_id,
            session_id=session_id,
            model=solver.model,
            status=solver.status,
            latency_ms=solver.latency_ms,
            usage=solver.usage,
            response=solver.model_dump(),
        )
        if float((solver.usage or {}).get("cost") or 0.0) > 0:
            self.storage.add_budget_entry(
                trace_id,
                session_id,
                "solver",
                float(solver.usage.get("cost") or 0.0),
                solver.model,
                {"status": solver.status},
            )

        verifier = verify_solver_claims(solver.symbolic_claims)
        self.storage.save_verifier_run(
            trace_id=trace_id,
            session_id=session_id,
            checked_claims=verifier.checked_claims,
            passed_claims=verifier.passed_claims,
            failed_claims=verifier.failed_claims,
            disagreement_rate=verifier.disagreement_rate,
            response=verifier.model_dump(),
        )

        setpoints_current = self.storage.get_latest_setpoints(
            session_id=session_id,
            fallback={k: float(v) for k, v in (self.settings.policy.get("setpoints", {}) or {}).items()},
        )

        tutor = compose_hint(
            problem_text=ocr.normalized_problem,
            working_text=ocr.normalized_working,
            solver_result=self._coerce_solver_dict(solver),
            verifier_result=verifier.model_dump(),
            setpoints=setpoints_current,
            model=str(self.settings.models.get("tutor_model")),
            policy=self.settings.policy,
        )
        if float((tutor.usage or {}).get("cost") or 0.0) > 0:
            self.storage.add_budget_entry(
                trace_id,
                session_id,
                "tutor",
                float(tutor.usage.get("cost") or 0.0),
                tutor.model,
                {"confidence": tutor.confidence},
            )

        guarded_msg, guard_flags, leakage_penalty = apply_hint_guard(
            tutor.message,
            requires_attempt=bool(tutor.requires_attempt),
            policy=self.settings.policy,
        )

        recent_errors = self._recent_error_types(session_id, limit=12)
        non_transfer = non_transfer_recurrence(solver.error_type, recent_errors)
        repeated_confusion = 1.0 if (trigger == TriggerType.HELP_REQUEST and len(recent_errors) >= 2 and non_transfer >= 0.5) else 0.0
        post_hint_progress = self._has_student_attempt(req) and trigger != TriggerType.HELP_REQUEST

        goodhart = evaluate_hidden_score(
            leakage_penalty=leakage_penalty,
            verifier_disagreement_rate=verifier.disagreement_rate,
            repeated_confusion_after_hints=repeated_confusion,
            post_hint_progress=post_hint_progress,
            requires_attempt=bool(tutor.requires_attempt),
        )

        observed_targets = model_observed_setpoint_targets(
            goodhart=goodhart,
            verifier_disagreement_rate=verifier.disagreement_rate,
            non_transfer_rate=non_transfer,
        )
        setpoint_update_cfg = self.settings.policy.get("setpoint_update", {}) or {}
        new_setpoints, drift_map = update_setpoints(
            current=setpoints_current,
            observed=observed_targets,
            ewma_alpha=float(setpoint_update_cfg.get("ewma_alpha", 0.15)),
            max_daily_drift=float(setpoint_update_cfg.get("max_daily_drift", 0.05)),
            now_iso=_utc_now_iso(),
            previous_updated_at=str(session.get("updated_at") or ""),
        )
        self.storage.update_session_setpoints(session_id, new_setpoints)
        self.storage.save_setpoint_snapshot(
            session_id,
            {
                "setpoints": new_setpoints,
                "observed_targets": observed_targets,
                "drift_map": drift_map,
                "trace_id": trace_id,
            },
        )

        ai_factors = make_ai_factors(
            verifier_disagreement_rate=verifier.disagreement_rate,
            repeated_confusion_after_hints=repeated_confusion,
            direct_answer_pressure_incidents=1.0 if leakage_penalty > 0 else 0.0,
            latency_ms=tutor.latency_ms + solver.latency_ms,
            sla_ms=8000,
            non_transfer_recurrence=non_transfer,
        )
        viktor_factors = make_viktor_factors(
            idle_seconds=float(req.idle_seconds or 0.0),
            idle_threshold_seconds=40.0,
            hint_to_progress_lag=0.0 if post_hint_progress else 0.5,
            repeated_error_signature=non_transfer,
        )
        stress_ai, stress_viktor = compute_shared_stress(
            ai_factors=ai_factors,
            viktor_factors=viktor_factors,
            stress_weights_ai=(self.settings.policy.get("stress_weights_ai", {}) or {}),
            stress_weights_viktor=(self.settings.policy.get("stress_weights_viktor", {}) or {}),
        )
        self.storage.save_stress_snapshot(
            session_id=session_id,
            stress_ai=stress_ai,
            stress_viktor=stress_viktor,
            factors={"ai": ai_factors, "viktor": viktor_factors, "trace_id": trace_id},
        )

        flags = sorted(
            set(
                list(tutor.flags)
                + list(guard_flags)
                + list(goodhart.flags)
                + (["verifier_disagreement"] if verifier.disagreement_rate >= 0.5 else [])
            )
        )

        self.storage.save_tutor_turn(
            trace_id=trace_id,
            session_id=session_id,
            model=tutor.model,
            tutor_message=guarded_msg,
            confidence=tutor.confidence,
            requires_attempt=tutor.requires_attempt,
            flags=flags,
            hidden_score=goodhart.hidden_score,
            leakage_penalty=goodhart.leakage_penalty,
            usage=tutor.usage,
            latency_ms=tutor.latency_ms,
        )
        self.storage.save_event(
            trace_id,
            session_id,
            "pipeline_completed",
            {
                "trigger_type": trigger.value,
                "solver_error_type": solver.error_type,
                "verifier_disagreement": verifier.disagreement_rate,
                "hidden_score": goodhart.hidden_score,
                "stress_ai": stress_ai,
                "stress_viktor": stress_viktor,
                "flags": flags,
            },
        )

        status = "ok"
        if verifier.disagreement_rate >= 0.5 or tutor.confidence < 0.45:
            status = "uncertain"
        return TutorTurnResponse(
            status=status,
            tutor_message=guarded_msg,
            confidence=float(tutor.confidence),
            requires_attempt=bool(tutor.requires_attempt),
            flags=flags,
            trace_id=trace_id,
        )

    def get_state(self, session_id: str) -> SessionState:
        session = self.storage.get_session(session_id)
        if not session:
            return SessionState(
                setpoints={},
                stress={"stress_ai": 0.0, "stress_viktor": 0.0},
                last_events=[],
                budget_snapshot={},
            )
        budget_snapshot = self.storage.budget_snapshot(
            monthly_cap_usd=float(self.settings.budget.get("monthly_cap_usd", 150.0)),
            per_session_soft_cap_usd=float(self.settings.budget.get("per_session_soft_cap_usd", 8.0)),
            session_id=session_id,
        )
        return SessionState(
            setpoints=self.storage.get_latest_setpoints(
                session_id,
                fallback={k: float(v) for k, v in (self.settings.policy.get("setpoints", {}) or {}).items()},
            ),
            stress=self.storage.get_latest_stress(session_id),
            last_events=self.storage.get_recent_events(session_id, limit=10),
            budget_snapshot=budget_snapshot,
        )

    def run_benchmark(self, candidate_models: List[str], sample_size: int) -> BenchmarkRunResult:
        dataset_path = self.settings.repo_root / "benchmarks" / "viktor_cases.jsonl"
        report_id, summary, recommendation = run_benchmark(
            dataset_path=dataset_path,
            candidate_models=candidate_models,
            sample_size=sample_size,
        )
        self.storage.save_benchmark_run(
            report_id=report_id,
            candidate_models=candidate_models,
            sample_size=sample_size,
            summary=summary,
            recommendation=recommendation,
        )
        self._write_benchmark_report(report_id, summary, recommendation)
        return BenchmarkRunResult(
            report_id=report_id,
            summary=summary,
            recommendation=recommendation,
        )

    def _write_benchmark_report(self, report_id: str, summary: Dict[str, Any], recommendation: str) -> None:
        report_dir = self.settings.repo_root / "reports" / "generated"
        report_dir.mkdir(parents=True, exist_ok=True)
        out_path = report_dir / f"{report_id}.md"
        payload = {
            "report_id": report_id,
            "generated_at": _utc_now_iso(),
            "recommendation": recommendation,
            "summary": summary,
            "action": "Prepare PR proposal only. No auto-merge.",
        }
        out_path.write_text(
            "# Viktor-Friday Benchmark Report\n\n"
            f"## Report ID\n`{report_id}`\n\n"
            f"## Recommendation\n{recommendation}\n\n"
            "## Summary (JSON)\n"
            "```json\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "```\n",
            encoding="utf-8",
        )

    def run_retention(self, retention_days: int = 30) -> Dict[str, Any]:
        return self.storage.run_retention(retention_days=retention_days)

