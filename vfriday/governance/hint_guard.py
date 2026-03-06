"""Hint safety guard to reduce direct-answer leakage."""

from __future__ import annotations

import re
from typing import Dict, List, Tuple


_DIRECT_PATTERNS = [
    re.compile(r"\bfinal answer\b", re.IGNORECASE),
    re.compile(r"\banswer is\b", re.IGNORECASE),
    re.compile(r"\btherefore\b", re.IGNORECASE),
    re.compile(r"\bx\s*=\s*[-+*/0-9a-zA-Z().]+"),
    re.compile(r"\bv\s*=\s*[-+*/0-9a-zA-Z().]+"),
]


def detect_leakage(message: str, requires_attempt: bool, max_hint_depth: int = 2) -> Dict[str, float | bool | List[str]]:
    """Detect potential direct-answer leakage in tutor message."""
    msg = str(message or "")
    flags: List[str] = []
    hits = 0
    for pattern in _DIRECT_PATTERNS:
        if pattern.search(msg):
            hits += 1

    # Dense equations with multiple equals signs are suspicious in first hints
    if msg.count("=") >= 2:
        hits += 1
        flags.append("dense_equation_leak")

    if requires_attempt and hits > 0:
        flags.append("direct_answer_risk")

    # Normalize penalty to [0..1]
    penalty = min(1.0, hits / max(1, max_hint_depth + 1))
    return {
        "has_leak": bool(hits > 0),
        "penalty": float(penalty),
        "flags": flags,
    }


def apply_hint_guard(
    message: str,
    *,
    requires_attempt: bool,
    policy: Dict[str, object],
) -> Tuple[str, List[str], float]:
    """Apply leakage detection and sanitize if policy forbids direct answers."""
    max_hint_depth = int(
        (
            policy.get("max_hint_depth", 2)
            if isinstance(policy, dict)
            else 2
        )
        or 2
    )
    res = detect_leakage(message, requires_attempt, max_hint_depth=max_hint_depth)
    flags = [str(x) for x in (res.get("flags") or [])]
    penalty = float(res.get("penalty") or 0.0)

    no_direct = bool(policy.get("no_direct_answer_before_attempt", True))
    has_leak = bool(res.get("has_leak"))

    if no_direct and requires_attempt and has_leak:
        sanitized = (
            "Давай не прыгать к финальному ответу. Проверь ключевой переход: "
            "какая формула или проекция здесь должна применяться и почему?"
        )
        flags.append("hint_sanitized_by_guard")
        return sanitized, sorted(set(flags)), penalty

    return str(message or ""), sorted(set(flags)), penalty

