from vfriday.governance.hint_guard import apply_hint_guard, detect_leakage


def test_detect_leakage_on_direct_answer_phrase():
    res = detect_leakage("The final answer is x = 42.", requires_attempt=True, max_hint_depth=2)
    assert res["has_leak"] is True
    assert float(res["penalty"]) > 0


def test_apply_hint_guard_sanitizes_when_policy_forbids_direct_answer():
    msg, flags, penalty = apply_hint_guard(
        "Final answer: x = 42",
        requires_attempt=True,
        policy={
            "no_direct_answer_before_attempt": True,
            "max_hint_depth": 2,
        },
    )
    assert "hint_sanitized_by_guard" in flags
    assert penalty > 0
    assert "x = 42" not in msg

