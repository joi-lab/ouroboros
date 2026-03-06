"""Solver agent adapter for Viktor-Friday orchestrator."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List

from ouroboros.llm import LLMClient
from vfriday.schemas import SolverClaim, SolverResult


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _normalize_claims(items: List[Dict[str, Any]]) -> List[SolverClaim]:
    claims: List[SolverClaim] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        claims.append(
            SolverClaim(
                claim_type=str(item.get("claim_type") or "equality"),
                lhs=item.get("lhs"),
                rhs=item.get("rhs"),
                expr=item.get("expr"),
                var=item.get("var"),
                equals=item.get("equals"),
            )
        )
    return claims


def _heuristic_solver(problem_text: str, working_text: str, model: str) -> SolverResult:
    text = f"{problem_text}\n{working_text}".lower()
    error_type = None
    error_step = None
    explanation = "Need more formal steps from student to localize the exact error."

    if "cos" in text and "sin" in text and "projection" in text:
        error_type = "trigonometry_projection"
        error_step = 3
        explanation = (
            "Likely projection mismatch: check whether sine/cosine was selected "
            "for the chosen axis in step 3."
        )
    elif "integral" in text and ("dx" in text or "∫" in text):
        error_type = "integration_constant"
        error_step = 2
        explanation = "Check antiderivative and constant of integration."
    elif "newton" in text or "force" in text:
        error_type = "sign_convention"
        error_step = 2
        explanation = "Verify sign convention and axis orientation before summing forces."

    return SolverResult(
        status="ok",
        model=model,
        explanation=explanation,
        error_found=error_type is not None,
        error_type=error_type,
        error_step=error_step,
        confidence=0.52 if error_type else 0.40,
        symbolic_claims=[],
        usage={"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
        latency_ms=5,
        raw={"mode": "heuristic_fallback"},
    )


def solve(
    *,
    problem_text: str,
    working_text: str,
    model: str,
    reasoning_effort: str = "high",
) -> SolverResult:
    """Run solver model and return structured output."""
    if not os.environ.get("OPENAI_API_KEY"):
        return _heuristic_solver(problem_text, working_text, model)

    started = time.perf_counter()
    prompt = (
        "You are Solver Agent for olympiad physics/mathematics diagnostics.\n"
        "Given problem statement and student work, return JSON only with keys:\n"
        "{"
        "\"status\": \"ok|uncertain\", "
        "\"explanation\": str, "
        "\"error_found\": bool, "
        "\"error_type\": str|null, "
        "\"error_step\": int|null, "
        "\"confidence\": float(0..1), "
        "\"symbolic_claims\": ["
        "{\"claim_type\":\"equality\",\"lhs\":\"...\",\"rhs\":\"...\"}, "
        "{\"claim_type\":\"derivative\",\"expr\":\"...\",\"var\":\"x\",\"equals\":\"...\"}, "
        "{\"claim_type\":\"integral\",\"expr\":\"...\",\"var\":\"x\",\"equals\":\"...\"}"
        "]"
        "}.\n"
        "Keep symbolic claims only for statements you are reasonably sure are present."
    )
    user = (
        f"Problem:\n{problem_text or '(missing)'}\n\n"
        f"Student work / context:\n{working_text or '(missing)'}\n\n"
        "Return strict JSON."
    )
    llm = LLMClient()
    try:
        msg, usage = llm.chat(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user},
            ],
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=1200,
        )
        payload = _extract_json_object(msg.get("content") or "")
        claims = _normalize_claims(payload.get("symbolic_claims") or [])
        latency_ms = int((time.perf_counter() - started) * 1000)
        return SolverResult(
            status=str(payload.get("status") or "ok"),
            model=model,
            explanation=str(payload.get("explanation") or "Solver completed."),
            error_found=bool(payload.get("error_found")),
            error_type=(str(payload.get("error_type")) if payload.get("error_type") else None),
            error_step=(int(payload.get("error_step")) if payload.get("error_step") not in (None, "") else None),
            confidence=float(payload.get("confidence") or 0.55),
            symbolic_claims=claims,
            usage=usage or {},
            latency_ms=latency_ms,
            raw=payload if payload else {"raw_text": msg.get("content", "")[:2000]},
        )
    except Exception as exc:
        fallback = _heuristic_solver(problem_text, working_text, model)
        fallback.status = "uncertain"
        fallback.raw = {"error": repr(exc), "mode": "heuristic_after_exception"}
        return fallback
