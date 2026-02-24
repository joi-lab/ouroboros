"""Tutor agent adapter for Socratic hint generation."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict

from ouroboros.llm import LLMClient
from vfriday.schemas import TutorResult


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


def _heuristic_hint(error_type: str | None, verifier_disagreement: float) -> str:
    if verifier_disagreement >= 0.5:
        return (
            "Я не уверен в текущей развилке. Давай проверим шаг перед спорным местом: "
            "какую физическую величину ты выражаешь, и какие у нее единицы?"
        )
    if error_type == "trigonometry_projection":
        return "Посмотри на проекцию под углом: почему ты выбрал именно эту тригонометрическую функцию?"
    if error_type == "integration_constant":
        return "Перед финальным ответом проверь: что происходит с константой интегрирования?"
    if error_type == "sign_convention":
        return "Проверь ориентацию осей и знаки сил: где может появиться минус?"
    return "Выбери шаг, где ты наиболее не уверен, и проверь его размерности или граничный случай."


def compose_hint(
    *,
    problem_text: str,
    working_text: str,
    solver_result: Dict[str, Any],
    verifier_result: Dict[str, Any],
    setpoints: Dict[str, float],
    model: str,
    policy: Dict[str, Any],
) -> TutorResult:
    """Build tutor message from solver/verifier outputs."""
    requires_attempt = bool(policy.get("no_direct_answer_before_attempt", True))
    verifier_disagreement = float(verifier_result.get("disagreement_rate") or 0.0)
    solver_error_type = solver_result.get("error_type")

    if not os.environ.get("OPENAI_API_KEY"):
        msg = _heuristic_hint(solver_error_type, verifier_disagreement)
        flags = ["heuristic_tutor"]
        if verifier_disagreement >= 0.5:
            flags.append("uncertain_mode")
        return TutorResult(
            model=model,
            message=msg,
            confidence=0.52 if verifier_disagreement < 0.5 else 0.40,
            requires_attempt=requires_attempt,
            usage={"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
            latency_ms=5,
            flags=flags,
        )

    started = time.perf_counter()
    system_prompt = (
        "You are Tutor Agent 'Friday'. Return strict JSON with keys:\n"
        "{"
        "\"message\": str, "
        "\"confidence\": float(0..1), "
        "\"requires_attempt\": bool, "
        "\"flags\": [str]"
        "}.\n"
        "Rules:\n"
        "- Use Socratic style.\n"
        "- Do not provide full final answer unless explicitly requested after multiple attempts.\n"
        "- Keep hint concise (1-3 sentences).\n"
        "- If verifier is uncertain/disagreeing, admit uncertainty and guide validation.\n"
    )
    user_prompt = (
        f"Problem:\n{problem_text or '(missing)'}\n\n"
        f"Student work:\n{working_text or '(missing)'}\n\n"
        f"Solver result:\n{json.dumps(solver_result, ensure_ascii=False)}\n\n"
        f"Verifier result:\n{json.dumps(verifier_result, ensure_ascii=False)}\n\n"
        f"Setpoints:\n{json.dumps(setpoints, ensure_ascii=False)}\n\n"
        f"Policy:\n{json.dumps(policy, ensure_ascii=False)}\n\n"
        "Return strict JSON only."
    )
    llm = LLMClient()
    try:
        msg, usage = llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            tools=None,
            reasoning_effort="medium",
            max_tokens=700,
        )
        payload = _extract_json_object(msg.get("content") or "")
        text = str(payload.get("message") or "").strip()
        if not text:
            text = _heuristic_hint(solver_error_type, verifier_disagreement)
        flags = [str(x) for x in (payload.get("flags") or []) if str(x).strip()]
        if verifier_disagreement >= 0.5 and "uncertain_mode" not in flags:
            flags.append("uncertain_mode")
        return TutorResult(
            model=model,
            message=text,
            confidence=float(payload.get("confidence") or 0.55),
            requires_attempt=bool(payload.get("requires_attempt", requires_attempt)),
            usage=usage or {},
            latency_ms=int((time.perf_counter() - started) * 1000),
            flags=flags,
        )
    except Exception as exc:
        return TutorResult(
            model=model,
            message=_heuristic_hint(solver_error_type, verifier_disagreement),
            confidence=0.40,
            requires_attempt=requires_attempt,
            usage={"cost": 0.0},
            latency_ms=int((time.perf_counter() - started) * 1000),
            flags=["tutor_fallback_after_exception", type(exc).__name__],
        )
