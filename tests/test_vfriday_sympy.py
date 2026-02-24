from vfriday.schemas import SolverClaim
from vfriday.verifier.sympy_engine import verify_canonical_transforms, verify_solver_claims


def test_canonical_transforms():
    checks = verify_canonical_transforms()
    assert all(checks.values()), checks


def test_verify_solver_claims_mixed():
    claims = [
        SolverClaim(claim_type="equality", lhs="x**2 - y**2", rhs="(x-y)*(x+y)"),
        SolverClaim(claim_type="derivative", expr="sin(x)", var="x", equals="cos(x)"),
        SolverClaim(claim_type="integral", expr="x", var="x", equals="x**2/2"),
        SolverClaim(claim_type="equality", lhs="2+2", rhs="5"),
    ]
    result = verify_solver_claims(claims)
    assert result.checked_claims == 4
    assert result.passed_claims == 3
    assert result.failed_claims == 1
    assert 0.0 <= result.disagreement_rate <= 1.0

