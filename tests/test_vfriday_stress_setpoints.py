from vfriday.governance.setpoints import update_setpoints
from vfriday.governance.stress import compute_shared_stress


def test_compute_shared_stress_bounds():
    ai, viktor = compute_shared_stress(
        ai_factors={
            "verifier_disagreement_rate": 0.6,
            "repeated_confusion_after_hints": 0.2,
            "direct_answer_pressure_incidents": 0.1,
            "latency_over_sla": 0.0,
            "non_transfer_recurrence": 0.4,
        },
        viktor_factors={
            "idle_blocks_over_threshold": 0.7,
            "hint_to_progress_lag": 0.5,
            "repeated_error_signature": 0.2,
        },
        stress_weights_ai={
            "verifier_disagreement_rate": 0.25,
            "repeated_confusion_after_hints": 0.20,
            "direct_answer_pressure_incidents": 0.20,
            "latency_over_sla": 0.20,
            "non_transfer_recurrence": 0.15,
        },
        stress_weights_viktor={
            "idle_blocks_over_threshold": 0.35,
            "hint_to_progress_lag": 0.35,
            "repeated_error_signature": 0.30,
        },
    )
    assert 0.0 <= ai <= 1.0
    assert 0.0 <= viktor <= 1.0


def test_setpoint_drift_clamp():
    current = {
        "competency": 0.5,
        "transfer": 0.5,
    }
    observed = {
        "competency": 1.0,
        "transfer": 0.0,
    }
    new_values, drift = update_setpoints(
        current=current,
        observed=observed,
        ewma_alpha=1.0,
        max_daily_drift=0.05,
        now_iso="2026-02-22T12:00:00+00:00",
        previous_updated_at="2026-02-22T11:59:00+00:00",
    )
    assert drift["competency"] <= 0.05
    assert drift["transfer"] <= 0.05
    assert new_values["competency"] <= 0.55
    assert new_values["transfer"] >= 0.45

