from vfriday.schemas import TriggerType
from vfriday.telemetry.triggers import normalize_trigger, should_emit_pause_trigger


def test_pause_trigger_threshold():
    assert should_emit_pause_trigger(40.0) is True
    assert should_emit_pause_trigger(39.9) is False


def test_normalize_short_pause_to_context_switch():
    trig = normalize_trigger(
        requested=TriggerType.PAUSE,
        idle_seconds=10,
        user_message="thinking",
    )
    assert trig == TriggerType.CONTEXT_SWITCH


def test_help_command_forces_help_trigger():
    trig = normalize_trigger(
        requested=TriggerType.MANUAL_UPLOAD,
        idle_seconds=0,
        user_message="/help stuck on step 3",
    )
    assert trig == TriggerType.HELP_REQUEST

