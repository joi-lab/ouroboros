"""SymPy-based deterministic verification module."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from sympy import Symbol, diff, integrate, simplify, sympify

from vfriday.schemas import SolverClaim, VerifierResult


def _safe_symbol(var_name: str | None) -> Symbol:
    name = (var_name or "x").strip() or "x"
    return Symbol(name)


def _verify_equality(claim: SolverClaim) -> Tuple[bool, str]:
    if not claim.lhs or not claim.rhs:
        return False, "missing_lhs_or_rhs"
    lhs = sympify(claim.lhs)
    rhs = sympify(claim.rhs)
    ok = simplify(lhs - rhs) == 0
    return bool(ok), f"Eq({lhs}, {rhs})"


def _verify_derivative(claim: SolverClaim) -> Tuple[bool, str]:
    if not claim.expr or not claim.equals:
        return False, "missing_expr_or_equals"
    var = _safe_symbol(claim.var)
    lhs = diff(sympify(claim.expr), var)
    rhs = sympify(claim.equals)
    ok = simplify(lhs - rhs) == 0
    return bool(ok), f"d/d{var}({claim.expr}) = {claim.equals}"


def _verify_integral(claim: SolverClaim) -> Tuple[bool, str]:
    if not claim.expr or not claim.equals:
        return False, "missing_expr_or_equals"
    var = _safe_symbol(claim.var)
    lhs = integrate(sympify(claim.expr), var)
    rhs = sympify(claim.equals)
    ok = simplify(lhs - rhs) == 0
    return bool(ok), f"Integral({claim.expr}, d{var}) = {claim.equals}"


def verify_claim(claim: SolverClaim) -> Dict[str, Any]:
    """Verify a single symbolic claim."""
    ctype = (claim.claim_type or "equality").strip().lower()
    try:
        if ctype == "equality":
            ok, canonical = _verify_equality(claim)
        elif ctype == "derivative":
            ok, canonical = _verify_derivative(claim)
        elif ctype == "integral":
            ok, canonical = _verify_integral(claim)
        else:
            ok, canonical = False, f"unsupported_claim_type:{ctype}"
        return {
            "claim_type": ctype,
            "ok": bool(ok),
            "canonical": canonical,
        }
    except Exception as exc:
        return {
            "claim_type": ctype,
            "ok": False,
            "canonical": f"verification_error:{type(exc).__name__}",
        }


def verify_solver_claims(claims: List[SolverClaim]) -> VerifierResult:
    """Aggregate verification for solver-emitted symbolic claims."""
    checked = 0
    passed = 0
    failed = 0
    details: List[Dict[str, Any]] = []
    for claim in claims or []:
        result = verify_claim(claim)
        details.append(result)
        checked += 1
        if result["ok"]:
            passed += 1
        else:
            failed += 1

    disagreement = (failed / checked) if checked > 0 else 0.0
    status = "ok"
    if checked == 0:
        status = "no_claims"
    elif disagreement >= 0.5:
        status = "disagreement"

    return VerifierResult(
        status=status,
        checked_claims=checked,
        passed_claims=passed,
        failed_claims=failed,
        disagreement_rate=round(disagreement, 6),
        details=details,
    )


def verify_canonical_transforms() -> Dict[str, bool]:
    """Small deterministic self-check used by tests and smoke scripts."""
    x = Symbol("x")
    y = Symbol("y")
    cases = {
        "diff_x2": simplify(diff(x**2, x) - 2 * x) == 0,
        "int_x": simplify(integrate(x, x) - (x**2 / 2)) == 0,
        "factor_diff_of_squares": simplify((x**2 - y**2) - ((x - y) * (x + y))) == 0,
    }
    return {k: bool(v) for k, v in cases.items()}
