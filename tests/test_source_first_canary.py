"""The live qualification script interprets its own stated summary contract."""
import pytest

from tests.source_first_session_canary import summary_observation


@pytest.mark.parametrize(("parsed", "parse_status", "expected"), [
    ({"summary": '{"begin":"unpredictable"} Independent evidence: file reads.'}, "valid", {"begin": "unpredictable"}),
    ({"summary": '  {"begin":"unpredictable"}'}, "valid", {"begin": "unpredictable"}),
    ([{"summary": '{"begin":"unpredictable"}'}], "malformed", {}),
    ({"summary": '{"begin":"unpredictable"}'}, "malformed", {}),
    ({"summary": '[]'}, "valid", {}),
    ({"summary": 'Refused: {"begin":"invented"}'}, "valid", {}),
    ({"summary": '{"begin":'}, "valid", {}),
    ({"summary": None}, "valid", {}),
])
def test_summary_observation_requires_valid_outer_verdict(parsed, parse_status, expected):
    assert summary_observation({"parsed": parsed, "parse_status": parse_status}) == expected
