"""Benchmark runner for model scouting and safe auto-rotate proposals."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple

from vfriday.agents.solver import solve


def load_cases(path: Path) -> List[Dict[str, Any]]:
    """Load benchmark cases from JSONL file."""
    cases: List[Dict[str, Any]] = []
    if not path.exists():
        return cases
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            cases.append(obj)
    return cases


def _score_case(result: Dict[str, Any], case: Dict[str, Any]) -> Dict[str, float]:
    expected_error = str(case.get("expected_error_type") or "").strip()
    expected_keywords = [str(x).lower() for x in (case.get("expected_keywords") or []) if str(x).strip()]
    explanation = str(result.get("explanation") or "").lower()

    type_score = 0.5
    if expected_error:
        type_score = 1.0 if str(result.get("error_type") or "") == expected_error else 0.0

    keyword_score = 1.0
    if expected_keywords:
        hits = sum(1 for kw in expected_keywords if kw in explanation)
        keyword_score = hits / max(1, len(expected_keywords))

    total = 0.7 * type_score + 0.3 * keyword_score
    return {
        "type_score": round(type_score, 6),
        "keyword_score": round(keyword_score, 6),
        "total_score": round(total, 6),
    }


def run_benchmark(
    *,
    dataset_path: Path,
    candidate_models: List[str],
    sample_size: int,
) -> Tuple[str, Dict[str, Any], str]:
    """
    Run benchmark and return:
      (report_id, summary, recommendation)
    """
    all_cases = load_cases(dataset_path)
    if not all_cases:
        report_id = f"bench-{uuid.uuid4().hex[:8]}"
        summary = {"error": "benchmark_dataset_missing_or_empty", "dataset_path": str(dataset_path)}
        return report_id, summary, "No recommendation: dataset unavailable."

    eval_cases = [c for c in all_cases if not bool(c.get("holdout"))]
    holdout_cases = [c for c in all_cases if bool(c.get("holdout"))]
    eval_cases = eval_cases[: max(1, int(sample_size))]

    model_summaries: List[Dict[str, Any]] = []
    for model in candidate_models:
        rows: List[Dict[str, Any]] = []
        for case in eval_cases:
            solver = solve(
                problem_text=str(case.get("problem") or ""),
                working_text=str(case.get("student_work") or ""),
                model=model,
                reasoning_effort="medium",
            )
            score = _score_case(solver.model_dump(), case)
            rows.append(
                {
                    "case_id": str(case.get("case_id") or ""),
                    "score": score["total_score"],
                    "type_score": score["type_score"],
                    "keyword_score": score["keyword_score"],
                    "latency_ms": int(solver.latency_ms),
                    "cost": float((solver.usage or {}).get("cost") or 0.0),
                }
            )

        avg_score = sum(r["score"] for r in rows) / max(1, len(rows))
        avg_latency_ms = sum(r["latency_ms"] for r in rows) / max(1, len(rows))
        avg_cost = sum(r["cost"] for r in rows) / max(1, len(rows))
        objective = avg_score - (0.02 * avg_cost) - (avg_latency_ms / 100000.0)
        model_summaries.append(
            {
                "model": model,
                "sample_size": len(rows),
                "avg_score": round(avg_score, 6),
                "avg_latency_ms": round(avg_latency_ms, 3),
                "avg_cost_usd": round(avg_cost, 6),
                "objective": round(objective, 6),
                "rows": rows,
            }
        )

    model_summaries.sort(key=lambda x: x["objective"], reverse=True)
    best = model_summaries[0]
    report_id = f"bench-{uuid.uuid4().hex[:10]}"
    summary = {
        "report_id": report_id,
        "evaluated_models": model_summaries,
        "dataset_size_total": len(all_cases),
        "dataset_size_eval": len(eval_cases),
        "dataset_size_holdout": len(holdout_cases),
        "selection_policy": "best_objective_without_auto_merge",
    }
    recommendation = (
        f"Recommend model `{best['model']}` "
        f"(score={best['avg_score']}, latency={best['avg_latency_ms']}ms, cost=${best['avg_cost_usd']}). "
        "Generate PR proposal and require Meta-Governor approval before switch."
    )
    return report_id, summary, recommendation

