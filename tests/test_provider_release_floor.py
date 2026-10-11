"""The release floor names every required provider canary that did not pass, and only those."""
from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from tests import provider_release_floor
from tests.provider_contract_ci import provider_canary_matrix
from tests.provider_release_floor import main

REQUIRED = [canary.canary_id for canary in provider_canary_matrix() if canary.credential_required]
OPTIONAL = [canary.canary_id for canary in provider_canary_matrix() if not canary.credential_required]
VICTIM = REQUIRED[len(REQUIRED) // 2]
SECRET = "SECRET-sk-live-0123456789"
ERROR = "::error title=Required provider canary did not pass::"


def _junit(tmp_path, cases, foreign=()):
    """Write a pytest-shaped report from (canary id, child tag or None for a pass) pairs.

    `foreign` ids become passed testcases of the same test name in another module.
    """
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="pytest")
    rows = [("tests.test_provider_integration", *case) for case in cases]
    for classname, canary_id, tag in [*rows, *(("tests.test_other", canary_id, None) for canary_id in foreign)]:
        case = ET.SubElement(suite, "testcase", classname=classname,
                             name=f"test_full_registry_provider_contract[{canary_id}]")
        if tag:
            ET.SubElement(case, tag, message=f"{SECRET} [{canary_id}] inconclusive provider alarm").text = SECRET
    path = tmp_path / "results.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _green(victim_tag=None, drop_victim=False):
    """Every required canary passed and every optional one skipped, except what the case changes."""
    required = [(canary_id, victim_tag if canary_id == VICTIM else None) for canary_id in REQUIRED
                if not (drop_victim and canary_id == VICTIM)]
    return [*required, *((canary_id, "skipped") for canary_id in OPTIONAL)]


def _run(tmp_path, capsys, junit, *, enforce):
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n", encoding="utf-8")
    code = main(["--junit", str(junit), "--summary", str(summary), *(["--enforce"] if enforce else [])])
    out = capsys.readouterr().out
    written = summary.read_text(encoding="utf-8")
    # What the summary file held survives, and no skip message or body leaves the private report.
    assert written.startswith("earlier step\n") and "Release floor for required provider canaries" in written
    assert SECRET not in out and SECRET not in written
    return code, out, written


def test_the_synthetic_reports_cover_the_live_matrix():
    assert REQUIRED and OPTIONAL and VICTIM != REQUIRED[0]
    assert provider_release_floor.required_canary_ids() == REQUIRED


@pytest.mark.parametrize("enforce", [True, False])
def test_every_required_canary_passed_meets_the_floor_whatever_the_optional_ones_did(tmp_path, capsys, enforce):
    code, out, written = _run(tmp_path, capsys, _junit(tmp_path, _green()), enforce=enforce)
    assert code == 0 and ERROR not in out
    assert f"{len(REQUIRED)} of {len(REQUIRED)} required provider canaries passed." in written
    assert ("The release floor is met." if enforce else "would pass.") in written
    assert all(f"| `{canary_id}` | passed |" in written for canary_id in REQUIRED)
    assert not any(canary_id in written or canary_id in out for canary_id in OPTIONAL)


@pytest.mark.parametrize("cases,state", [
    (_green("skipped"), "skipped"),
    (_green(drop_victim=True), "missing"),
    (_green("failure"), "failed"),
    (_green("error"), "failed"),
    # Every testcase carrying the id must be clean: a second clean one does not vouch for a skipped one.
    ([*_green("skipped"), (VICTIM, None)], "skipped"),
    ([*_green(), (VICTIM, "skipped")], "skipped"),
    ([*_green("skipped"), (VICTIM, "failure")], "failed"),
], ids=["skipped", "missing", "failure", "error", "skipped-then-passed", "passed-then-skipped", "skipped-and-failed"])
def test_one_required_canary_that_did_not_pass_blocks_a_tag_and_is_the_only_one_named(tmp_path, capsys, cases, state):
    junit = _junit(tmp_path, cases)
    code, out, written = _run(tmp_path, capsys, junit, enforce=True)
    assert code == 1
    assert [line for line in out.splitlines() if line.startswith("::")] == [f"{ERROR}{VICTIM} ({state})"]
    assert not any(f"{other} (" in out for other in [*REQUIRED, *OPTIONAL] if other != VICTIM)
    assert f"{len(REQUIRED) - 1} of {len(REQUIRED)} required provider canaries passed." in written
    assert f"The release is blocked by: {VICTIM} ({state})." in written and f"| `{VICTIM}` | {state} |" in written
    # The same report on a ref that is not a release tag: reported, never red.
    code, out, written = _run(tmp_path, capsys, junit, enforce=False)
    assert code == 0 and "::" not in out
    assert f"a release tag at this commit would be blocked by: {VICTIM} ({state})." in written


def test_only_the_exact_canary_testcase_counts(tmp_path, capsys):
    """A passed testcase of another module, or of an id that merely starts the same, is not the canary."""
    junit = _junit(tmp_path, [*_green(drop_victim=True), (f"{VICTIM}_extra", None)], foreign=[VICTIM])
    code, out, _written = _run(tmp_path, capsys, junit, enforce=True)
    assert code == 1 and f"{ERROR}{VICTIM} (missing)" in out.splitlines()


@pytest.mark.parametrize("content", [None, "<testsuites><testsuite", ""], ids=["absent", "truncated", "empty"])
def test_a_missing_or_unparsable_report_blocks_a_tag_and_only_reports_elsewhere(tmp_path, capsys, content):
    junit = tmp_path / "results.xml"
    if content is not None:
        junit.write_text(content, encoding="utf-8")
    code, out, written = _run(tmp_path, capsys, junit, enforce=True)
    assert code == 1 and "missing or unreadable" in written
    assert [line for line in out.splitlines() if line.startswith("::")] == [
        f"{ERROR}provider JUnit report missing or unreadable"]
    code, out, written = _run(tmp_path, capsys, junit, enforce=False)
    assert code == 0 and "::" not in out and "a release tag at this commit would be blocked." in written


@pytest.mark.parametrize("enforce", [True, False])
def test_a_broken_reader_is_never_a_quiet_pass(tmp_path, capsys, monkeypatch, enforce):
    """Informational mode forgives a missing report only: any other error must surface on a branch run."""
    def broken(junit, required):
        raise RuntimeError("reader bug")

    monkeypatch.setattr(provider_release_floor, "canary_states", broken)
    with pytest.raises(RuntimeError, match="reader bug"):
        _run(tmp_path, capsys, _junit(tmp_path, _green()), enforce=enforce)
