"""Release floor: on a release tag every REQUIRED provider canary must have passed.

A canary that meets a quota, rate-limit, 5xx or timeout outcome is a pytest skip, so the
producer step stays green with required canaries never evaluated. This reader takes that
step's own JUnit and names each required canary that did not pass. Only canary ids and
states leave it: skip messages and response bodies stay in the private report.

    python -m tests.provider_release_floor --junit PATH --summary PATH [--enforce]

Without `--enforce` the result is informational and the exit is 0, so every canary run
exercises the reader before a tag depends on it. Only a missing or unparsable report is
a foreseen input; any other error propagates in both modes.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET

from tests.provider_contract_ci import provider_canary_matrix

_CLASSNAME = "tests.test_provider_integration"
_TEST = "test_full_registry_provider_contract"
_ERROR = "::error title=Required provider canary did not pass::"


def required_canary_ids() -> list[str]:
    return [canary.canary_id for canary in provider_canary_matrix() if canary.credential_required]


def canary_states(junit: Path, required: list[str]) -> dict[str, str]:
    """Give each required id one state; every testcase carrying the id must be clean to pass."""
    cases = [case for case in ET.parse(junit).getroot().iter("testcase") if case.get("classname") == _CLASSNAME]
    states = {}
    for canary_id in required:
        outcomes = [{child.tag for child in case} for case in cases if case.get("name") == f"{_TEST}[{canary_id}]"]
        if not outcomes:
            states[canary_id] = "missing"
        elif any(outcome & {"failure", "error"} for outcome in outcomes):
            states[canary_id] = "failed"
        else:
            states[canary_id] = "skipped" if any("skipped" in outcome for outcome in outcomes) else "passed"
    return states


def _report(states: dict[str, str] | None, enforce: bool) -> tuple[str, list[str]]:
    """Return the verdict sentence and the offender labels; `states` is None for an unreadable report."""
    if states is None:
        offenders, named = ["provider JUnit report missing or unreadable"], ""
        counted = "No required provider canary was evaluated: the provider JUnit report is missing or unreadable."
    else:
        offenders = [f"{canary_id} ({state})" for canary_id, state in states.items() if state != "passed"]
        counted = f"{len(states) - len(offenders)} of {len(states)} required provider canaries passed."
        named = f" by: {', '.join(offenders)}"
    if enforce:
        verdict = f"The release is blocked{named}." if offenders else "The release floor is met."
        return f"{counted} Enforced on this ref. {verdict}", offenders
    would = f"be blocked{named}" if offenders else "pass"
    return f"{counted} Not enforced on this ref; a release tag at this commit would {would}.", offenders


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report or enforce the provider-canary release floor.")
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--enforce", action="store_true")
    args = parser.parse_args(argv)
    required = required_canary_ids()
    try:
        states = canary_states(args.junit, required)
    except (OSError, ET.ParseError):
        states = None
    sentence, offenders = _report(states, args.enforce)
    table = [f"| `{canary_id}` | {state} |" for canary_id, state in (states or {}).items()]
    lines = ["", "### Release floor for required provider canaries", "", sentence]
    if table:
        lines += ["", "| Required canary | State |", "|---|---|", *table]
    with args.summary.open("a", encoding="utf-8") as handle:  # Append: never truncate what the file holds.
        handle.write("\n".join(lines) + "\n")
    print(sentence)
    if not (args.enforce and offenders):
        return 0
    for offender in offenders:
        print(_ERROR + offender)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
