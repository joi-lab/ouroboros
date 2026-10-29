"""The post-merge check: base choice, one retry, bisection to the culprit merge and the hand-over.

Deterministic: GitHub is a fake, test sessions are fakes that write tests/ci_evidence.py's files,
and the only git is a throwaway local repository without remotes.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error

import pytest

from scripts import post_merge_check as pmc

REPO = Path(__file__).resolve().parents[1]
HEAD = "f" * 40


def _row(nodeid, outcome="passed", phase="call"):
    return {"nodeid": nodeid, "phase": phase, "outcome": outcome, "is_subtest": False}


def _results(directory, reports, *, exit_code=None, collection=()):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    failed = any(row["outcome"] == "failed" for row in reports) or collection
    (directory / "results.json").write_text(json.dumps({
        "format": 1, "session_exit_code": (1 if failed else 0) if exit_code is None else exit_code,
        "reports": reports, "collection_failures": [{"nodeid": path, "outcome": "failed"} for path in collection],
    }), encoding="utf-8")


def _selection(evidence, *, parallel="success", serial="success", node="success", inventory=None):
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "selection.json").write_text(json.dumps({
        "tier": "selected", "file_count": 3, "available": 40, "base": "b" * 40, "node": node,
        "outcomes": {"parallel": parallel, "serial": serial}, "inventory": inventory or {},
    }), encoding="utf-8")


# --- base ------------------------------------------------------------------------------------

def _run(sha, conclusion="success", status="completed", number=1):
    return {"id": number, "head_sha": sha, "status": status, "conclusion": conclusion}


def test_the_base_is_the_head_of_the_newest_completed_check():
    ancestors = {"a" * 40, "c" * 40}
    runs = [_run(HEAD, number=9), _run("d" * 40, status="in_progress", conclusion=None, number=8),
            _run("e" * 40, conclusion="cancelled", number=7), _run("x" * 40, number=6),
            _run("c" * 40, conclusion="failure", number=5), _run("a" * 40, number=4)]
    base, why = pmc.choose_base(runs, "a" * 40, HEAD, ancestor=lambda sha, head: sha in ancestors,
                                parent=lambda head: pytest.fail("no fallback"))
    # The run on HEAD itself, the unfinished, the cancelled and the non-ancestor runs are skipped.
    assert base == "c" * 40 and "run 5, failure" in why


def test_the_base_falls_back_to_the_previous_tip_and_then_to_the_first_parent():
    parent = lambda head: "p" * 40  # noqa: E731
    assert pmc.choose_base([], "a" * 40, HEAD, ancestor=lambda sha, head: True, parent=parent) == (
        "a" * 40, "the branch tip before this push")
    for before in ("", pmc.ZERO_SHA, "n" * 40):  # missing, a new branch, not an ancestor
        base, why = pmc.choose_base([_run("n" * 40)], before, HEAD,
                                    ancestor=lambda sha, head: sha != "n" * 40, parent=parent)
        assert (base, why) == ("p" * 40, "the first parent of this push")


def test_the_base_command_survives_an_api_error_and_publishes_its_choice(tmp_path, monkeypatch, capsys):
    def broken(self, path):
        raise urllib.error.HTTPError(path, 503, "Service Unavailable", None, None)

    monkeypatch.setattr(pmc.GitHub, "get", broken)
    monkeypatch.setattr(pmc, "is_ancestor", lambda sha, head: sha == "a" * 40)
    output = tmp_path / "github-output"
    environ ={"GITHUB_SHA": HEAD, "BEFORE": "a" * 40, "GITHUB_OUTPUT": str(output), "GITHUB_REPOSITORY": "o/r"}
    parsed = type("Args", (), {"head": None, "evidence_dir": tmp_path / "evidence"})
    assert pmc.command_base(parsed, environ) == 0
    assert output.read_text(encoding="utf-8") == f"base={'a' * 40}\n"
    assert json.loads((tmp_path / "evidence" / "base.json").read_text(encoding="utf-8"))["base"] == "a" * 40
    assert "HTTP 503" in capsys.readouterr().out


# --- sessions --------------------------------------------------------------------------------

def test_a_session_without_its_final_export_is_read_from_its_journal(tmp_path):
    events = [{"event": "start", "nodeid": "tests/test_a.py::test_hang"},
              {"event": "start", "nodeid": "tests/test_a.py::test_red"},
              {"event": "report", "nodeid": "tests/test_a.py::test_red", "phase": "call", "outcome": "failed"},
              {"event": "finish", "nodeid": "tests/test_a.py::test_red"},
              {"event": "start", "nodeid": "tests/test_b.py::test_crash"},
              {"event": "report", "nodeid": "tests/test_b.py::test_crash", "phase": "crash", "outcome": "failed"}]
    (tmp_path / "events.jsonl").write_text("\n".join(map(json.dumps, events)) + '\n{"event": "sta', encoding="utf-8")
    session = pmc.read_session(tmp_path)
    assert session["final"] is False and session["exit"] is None
    assert pmc.candidates(session) == {"tests/test_a.py::test_red": "failed in call",
                                       "tests/test_b.py::test_crash": "its worker crashed",
                                       "tests/test_a.py::test_hang": "was running when the session ended"}
    # Without a completed session nothing is known to be absent.
    assert pmc.outcomes(session, ["tests/test_a.py::test_hang", "tests/test_c.py::test_x"]) == {
        "tests/test_a.py::test_hang": "failed", "tests/test_c.py::test_x": "unknown"}


def test_outcomes_name_each_id_and_a_module_stands_for_its_tests(tmp_path):
    _results(tmp_path, [_row("tests/t.py::test_ok"), _row("tests/t.py::test_ok", phase="teardown"),
                        _row("tests/t.py::test_red", "failed"), _row("tests/t.py::test_skip", "skipped", "setup"),
                        _row("tests/u.py::test_late", "failed", "teardown"), _row("tests/v.py::test_ok")],
             collection=["tests/w.py"])
    session = pmc.read_session(tmp_path)
    assert pmc.outcomes(session, ["tests/t.py::test_ok", "tests/t.py::test_red", "tests/t.py::test_skip",
                                  "tests/u.py::test_late", "tests/t.py::test_gone", "tests/w.py::test_any",
                                  "tests/v.py", "tests/u.py", "tests/w.py"]) == {
        "tests/t.py::test_ok": "passed", "tests/t.py::test_red": "failed", "tests/t.py::test_skip": "skipped",
        "tests/u.py::test_late": "failed", "tests/t.py::test_gone": "absent", "tests/w.py::test_any": "failed",
        "tests/v.py": "passed", "tests/u.py": "failed", "tests/w.py": "failed"}


def test_container_args_never_name_a_node_the_tree_lacks():
    sources = {
        "tests/test_a.py": "def test_plain(): pass\n\ndef test_param(x): pass\n\nclass TestK:\n"
                           "    def test_m(self): pass\n\nclass TestInherited(Base):\n    pass\n",
        "tests/test_b.py": "def test_one(): pass\n",
        "tests/test_broken.py": "def broken(:\n",
    }
    ids = ["tests/test_a.py::test_plain", "tests/test_a.py::test_param[case-1]", "tests/test_a.py::test_param[2]",
           "tests/test_a.py::TestK::test_m", "tests/test_a.py::TestInherited::test_m", "tests/test_a.py::test_new",
           "tests/test_a.py::TestGone::test_m", "tests/test_b.py::test_one", "tests/test_gone.py::test_x",
           "tests/test_broken.py::test_y"]
    args, absent = pmc.container_args(ids, sources.get)
    # A missing function or class falls back to the module, which then covers every node in it.
    assert args == ["tests/test_a.py", "tests/test_b.py::test_one", "tests/test_broken.py"]
    assert absent == ["tests/test_gone.py::test_x"]
    args, _ = pmc.container_args(ids[:5], sources.get)
    assert args == ["tests/test_a.py::TestInherited", "tests/test_a.py::TestK::test_m",
                    "tests/test_a.py::test_param", "tests/test_a.py::test_plain"]


# --- classify --------------------------------------------------------------------------------

def test_one_retry_separates_failed_from_flaky(tmp_path):
    _selection(tmp_path, parallel="failure", serial="failure")
    _results(tmp_path / "parallel", [_row("tests/t.py::test_flaky", "failed"), _row("tests/t.py::test_broken", "failed"),
                                     _row("tests/t.py::test_fine")])
    _results(tmp_path / "serial", [_row("tests/s.py::test_skips_now", "failed", "setup")])
    seen = []

    def retry(args, directory):
        seen.append((list(args), Path(directory)))
        _results(directory, [_row("tests/t.py::test_flaky"), _row("tests/t.py::test_broken", "failed"),
                             _row("tests/s.py::test_skips_now", "skipped", "setup")])

    provisional = []
    record = pmc.classify(tmp_path, run=retry, node=lambda: pytest.fail("the Node lane was green"),
                          checkpoint=lambda early: provisional.append((len(seen), early)))
    # Before the retry starts, every red is on record as FAILED: what a killed step leaves behind.
    [(retries_so_far, early)] = provisional
    assert retries_so_far == 0 and early["complete"] is False and early["flaky"] == []
    assert [(row["id"], row["retry"]) for row in early["failed"]] == [
        ("tests/s.py::test_skips_now", "not run"), ("tests/t.py::test_broken", "not run"),
        ("tests/t.py::test_flaky", "not run")]
    assert record["complete"] is True
    # Exactly the failures, once, in one session.
    assert seen == [(["tests/s.py::test_skips_now", "tests/t.py::test_broken", "tests/t.py::test_flaky"],
                     tmp_path / "retry")]
    assert [row["id"] for row in record["flaky"]] == ["tests/t.py::test_flaky"]
    # Only a pass is a pass: a failure skipped on its retry stays FAILED.
    assert [(row["id"], row["retry"]) for row in record["failed"]] == [
        ("tests/s.py::test_skips_now", "skipped"), ("tests/t.py::test_broken", "failed")]
    assert record["failed"][0]["pass"] == "serial" and record["problems"] == []


LINTED = "ruff F811 tests/t.py fake_route"


def test_lint_findings_name_code_file_and_name_and_say_when_ruff_could_not_run(tmp_path, monkeypatch):
    """The release's lint gate on ruff's JSON rows, so no ruff is needed here; a line number is not
    part of the id, it moves with edits. A ruff that cannot run or answers anything but a list is None."""
    asked = []

    def ruff(answer, code=0, stderr=""):
        def run(argv, **kwargs):
            asked.append((argv, kwargs["cwd"]))
            if isinstance(answer, BaseException):
                raise answer
            return subprocess.CompletedProcess(argv, code, answer, stderr)
        monkeypatch.setattr(pmc.subprocess, "run", run)

    module = str(tmp_path / "pkg" / "m.py")
    for line in (5, 9):  # the same two findings before and after an edit above them
        ruff(json.dumps([{"code": "F401", "filename": module, "location": {"row": 1, "column": 8},
                          "message": "`os` imported but unused"},
                         {"code": "F811", "filename": module, "location": {"row": line, "column": 5},
                          "message": f"Redefinition of unused `loads` from line {line - 3}: `loads` redefined here"}]))
        found = pmc.lint_findings(tmp_path)
        assert sorted(found) == ["ruff F401 pkg/m.py os", "ruff F811 pkg/m.py loads"]
        assert f"from line {line - 3}" in found["ruff F811 pkg/m.py loads"]
    assert asked[-1] == ([sys.executable, "-m", "ruff", *pmc.LINT_ARGS, "."], tmp_path.resolve())
    ruff("[]")
    assert pmc.lint_findings(tmp_path, ["pkg/clean.py"]) == {}
    assert asked[-1][0][-1] == "pkg/clean.py"

    for answer, code, stderr in (("", 1, f"{sys.executable}: No module named ruff\n"),  # the packaged runtime
                                 (OSError("no ruff"), 0, ""), (subprocess.TimeoutExpired("ruff", 600), 0, ""),
                                 ("not json", 0, ""), ('{"code": "F401"}', 0, ""), ("", 0, ""),
                                 ("[]", 2, "ruff failed: invalid configuration\n")):
        ruff(answer, code, stderr)
        assert pmc.lint_findings(tmp_path) is None, (answer, code)


def test_lint_findings_read_the_installed_ruff(tmp_path):
    """The same ids from a real ruff run; only an interpreter without ruff (the packaged runtime) skips."""
    pytest.importorskip("ruff", reason="ruff is a development dependency; the packaged runtime has none")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "m.py").write_text("import os\nfrom json import loads\n\n\ndef loads():\n    return 1\n",
                                           encoding="utf-8")
    (tmp_path / "pkg" / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert sorted(pmc.lint_findings(tmp_path)) == ["ruff F401 pkg/m.py os", "ruff F811 pkg/m.py loads"]
    assert pmc.lint_findings(tmp_path, ["pkg/clean.py"]) == {}


def test_a_lint_finding_is_failed_without_a_retry_and_ruff_that_cannot_run_is_a_problem(tmp_path):
    _selection(tmp_path)
    no_retry = dict(run=lambda *a: pytest.fail("a lint finding is deterministic: no retry"), node=lambda: 0)
    record = pmc.classify(tmp_path, lint=lambda: {LINTED: "Redefinition of unused `fake_route` from line 3"},
                          **no_retry)
    assert record["failed"] == [{"id": LINTED, "pass": "lint", "retry": "failed",
                                 "why": "Redefinition of unused `fake_route` from line 3"}]
    assert record["lint_checked"] is True and record["problems"] == []
    record = pmc.classify(tmp_path, lint=lambda: None, **no_retry)
    assert record["failed"] == [] and record["lint_checked"] is False
    assert record["problems"] == ["ruff could not run, so the lint gate went unchecked"]
    assert pmc.classify(tmp_path, **no_retry)["lint_checked"] is False  # a caller that does not lint


def test_a_lint_finding_is_probed_on_its_file_as_the_commit_has_it(tmp_path):
    tree = tmp_path / "tree"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "t.py").write_text("x = 1\n", encoding="utf-8")
    absent = "ruff F401 tests/gone.py os"
    asked = []

    def lint(cwd, paths):
        asked.append((Path(cwd), list(paths)))
        return {LINTED: "Redefinition"}

    assert pmc.lint_answers(tree, [LINTED, absent], lint) == {LINTED: "fail", absent: "pass"}
    assert asked == [(tree, ["tests/t.py"])]  # only the files the commit has
    assert pmc.lint_answers(tree, [LINTED], lambda cwd, paths: {}) == {LINTED: "pass"}
    assert pmc.lint_answers(tree, [LINTED], lambda cwd, paths: None) == {LINTED: "error"}
    assert pmc.lint_answers(tree, [], lambda cwd, paths: pytest.fail("no finding, no ruff")) == {}


def test_a_red_node_lane_is_retried_as_one_target(tmp_path):
    for code, verdict in ((0, "flaky"), (1, "failed")):
        evidence = tmp_path / str(code)
        _selection(evidence, node="failure")
        _results(evidence / "parallel", [_row("tests/t.py::test_ok")])
        _results(evidence / "serial", [])
        record = pmc.classify(evidence, run=lambda *a: pytest.fail("no pytest red to retry"), node=lambda: code)
        assert [row["id"] for row in record[verdict]] == [pmc.NODE_LANE]


def test_a_retry_pytest_refuses_runs_the_nodes_the_tree_defines(tmp_path):
    _selection(tmp_path, parallel="failure")
    _results(tmp_path / "parallel", [_row("tests/t.py::test_p[redacted-value]", "failed")])
    _results(tmp_path / "serial", [])
    sessions = []

    def run(args, directory):
        sessions.append(list(args))
        if len(sessions) == 1:  # "ERROR: not found": nothing ran
            _results(directory, [], exit_code=4)
        else:
            _results(directory, [_row("tests/t.py::test_p[redacted-value]")])

    record = pmc.classify(tmp_path, run=run, node=lambda: 0, read_source={"tests/t.py": "def test_p(v): pass\n"}.get)
    assert sessions == [["tests/t.py::test_p[redacted-value]"], ["tests/t.py::test_p"]]
    assert [row["id"] for row in record["flaky"]] == ["tests/t.py::test_p[redacted-value]"]


def test_a_check_that_could_not_see_its_failures_says_so(tmp_path):
    assert pmc.classify(tmp_path)["problems"] == [
        "the selected run left no selection.json (its selection failed or it died early)"]
    _selection(tmp_path, parallel="failure", serial="skipped",
               inventory={"parallel": {"expected": ["tests/t.py::a", "tests/t.py::b"], "executed": ["tests/t.py::a"]}})
    _results(tmp_path / "parallel", [_row("tests/t.py::a")])
    record = pmc.classify(tmp_path, run=lambda *a: pytest.fail("nothing to retry"), node=lambda: 0)
    assert record["problems"] == ["the parallel pass was red without a failing test (read its log)",
                                  "the serial pass did not run"]
    assert record["not_run"] == {"parallel": ["tests/t.py::b"]} and record["failed"] == []
    # A pass killed before it recorded its outcome still left its journal.
    (tmp_path / "serial").mkdir()
    (tmp_path / "serial" / "events.jsonl").write_text(json.dumps({"event": "start", "nodeid": "tests/s.py::test_slow"}),
                                                      encoding="utf-8")
    record = pmc.classify(tmp_path, run=lambda args, directory: _results(directory, [_row("tests/s.py::test_slow")]),
                          node=lambda: 0)
    assert record["problems"][1] == "the serial pass did not finish"
    assert [(row["id"], row["why"]) for row in record["flaky"]] == [
        ("tests/s.py::test_slow", "was running when the session ended")]


# --- attribute -------------------------------------------------------------------------------

CHAIN = ["base"] + [f"m{index}" for index in range(1, 9)]  # base, then eight merges; m8 is the head


def _prober(first_bad, *, errors=()):
    """A fake probe: an id fails from its first bad commit on (None: already at the base)."""
    calls = []

    def probe(sha, ids):
        calls.append((sha, sorted(ids)))
        index = CHAIN.index(sha)
        return {nodeid: "error" if sha in errors else
                "fail" if first_bad[nodeid] is None or index >= CHAIN.index(first_bad[nodeid]) else "pass"
                for nodeid in ids}
    return probe, calls


def test_ids_broken_by_one_merge_share_one_bisection():
    probe, calls = _prober({"a": "m3", "b": "m3", "c": "m3"})
    verdicts = pmc.bisect(CHAIN, ["c", "a", "b"], probe)
    assert {row["id"]: (row["verdict"], row["culprit"], row["last_good"]) for row in verdicts} == {
        nodeid: ("culprit", "m3", "m2") for nodeid in "abc"}
    assert calls[0] == ("base", ["a", "b", "c"]) and len(calls) == 4  # the base, then log2(8) probes
    assert all(ids == ["a", "b", "c"] for _, ids in calls)


def test_two_culprits_and_a_preexisting_red_are_told_apart():
    probe, calls = _prober({"early": "m1", "late": "m8", "old": None})
    verdicts = {row["id"]: row for row in pmc.bisect(CHAIN, ["early", "late", "old"], probe)}
    assert (verdicts["early"]["culprit"], verdicts["early"]["last_good"]) == ("m1", "base")
    assert (verdicts["late"]["culprit"], verdicts["late"]["last_good"]) == ("m8", "m7")
    assert verdicts["old"]["verdict"] == "pre-existing" and "culprit" not in verdicts["old"]
    assert all("old" not in ids for _, ids in calls[1:])  # a pre-existing red is never bisected


def test_the_probe_budget_leaves_the_rest_unattributed():
    probe, calls = _prober({"a": "m5"})
    verdicts = pmc.bisect(CHAIN, ["a"], probe, max_probes=2)
    assert len(calls) == 2 and verdicts[0]["verdict"] == "unattributed"
    assert "probe budget (2)" in verdicts[0]["why"]


def test_a_probe_that_could_not_run_attributes_nothing():
    probe, _ = _prober({"a": "m6"}, errors={"m4"})
    assert pmc.bisect(CHAIN, ["a"], probe)[0]["verdict"] == "unattributed"
    assert pmc.bisect(["base"], ["a"], probe)[0]["verdict"] == "pre-existing"  # nothing landed since the base


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.mark.skipif(os.name == "nt", reason="the probe runner is POSIX CI only")
def test_attribute_probes_detached_worktrees_of_the_commits_and_removes_them(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "ouroboros")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    shas = []
    for value in ("1", "1", "2", "2"):  # the third commit changes the value the test checks
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / "test_v.py").write_text("def test_value(): pass\n", encoding="utf-8")
        (repo / "VALUE").write_text(value + str(len(shas)), encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"commit {len(shas)}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(pmc, "REPO_ROOT", repo)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "classification.json").write_text(json.dumps(
        {"failed": [{"id": "tests/test_v.py::test_value"}, {"id": pmc.NODE_LANE}]}), encoding="utf-8")
    probed = []

    def run(args, directory, *, cwd):
        probed.append(Path(cwd))
        red = (Path(cwd) / "VALUE").read_text(encoding="utf-8").startswith("2")
        _results(directory, [_row("tests/test_v.py::test_value", "failed" if red else "passed")])

    monkeypatch.setattr(pmc, "run_pytest", run)
    monkeypatch.setattr(pmc, "run_node_lane", lambda cwd: int((Path(cwd) / "VALUE").read_text(encoding="utf-8").startswith("2")))
    args = type("Args", (), {"evidence_dir": evidence, "base": shas[0], "head": shas[-1], "max_probes": 16,
                             "work_dir": tmp_path})
    assert pmc.command_attribute(args) == 0
    record = json.loads((evidence / "attribution.json").read_text(encoding="utf-8"))
    assert {row["id"]: (row["verdict"], row["culprit"], row["last_good"]) for row in record["verdicts"]} == {
        nodeid: ("culprit", shas[2], shas[1]) for nodeid in ("tests/test_v.py::test_value", pmc.NODE_LANE)}
    # The base, then the two commits between it and the head: each probe in its own worktree, all removed.
    assert [probe["sha"] for probe in record["probes"]] == shas[:3] and len(set(probed)) == 3
    assert record["commits"] == shas[1:] and not any(path.exists() for path in probed)
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert record["notes"] == [] and not any(probe["dependencies_differ"] for probe in record["probes"])


# --- route -----------------------------------------------------------------------------------

def test_the_machine_block_round_trips_through_an_issue_body():
    block = {"format": 1, "kind": "culprit", "pr": 2067, "culprit": "c" * 40, "last_good": "b" * 40,
             "ids": ["tests/t.py::test_x[a-->b]", "tests/t.py::test_y[<x>]"]}
    body = pmc.issue_body(block, head=HEAD, run_url="https://example.invalid/run/1", author="dev")
    assert "-->" not in pmc.machine_block(block)[:-3]
    assert pmc.parse_machine_block(body) == block
    assert pmc.parse_machine_block("no block here") is None
    assert pmc.parse_machine_block("<!-- post-merge-red {not json} -->") is None


class FakeGitHub:
    """Issues, pull requests and labels in memory, with failures on demand."""

    def __init__(self, *, issues=(), prs=None, label=False, fail=()):
        self.issues = {issue["number"]: dict(issue, state="open") for issue in issues}
        self.prs, self.label, self.fail = prs or {}, label, set(fail)
        self.comments, self.posts = [], []

    def _check(self, method, path):
        if any((method, path.split("?")[0]) == entry or (method, None) == entry for entry in self.fail):
            raise urllib.error.HTTPError(path, 502, "Bad Gateway", None, None)

    def get(self, path):
        self._check("GET", path)
        if path == f"labels/{pmc.LABEL}" and not self.label:
            raise urllib.error.HTTPError(path, 404, "Not Found", None, None)
        return {}

    def pages(self, path):
        self._check("GET", path)
        if path.startswith("issues?"):
            return [issue for issue in self.issues.values() if issue["state"] == "open"]
        return self.prs.get(path.split("/")[1], [])

    def post(self, path, body):
        self._check("POST", path)
        self.posts.append((path, body))
        if path == "labels":
            self.label = True
        elif path == "issues":
            number = 100 + len(self.issues)
            self.issues[number] = {"number": number, "body": body["body"], "title": body["title"], "state": "open"}
            return {"number": number}
        elif path.endswith("/comments"):
            self.comments.append((int(path.split("/")[1]), body["body"]))
        return {}

    def patch(self, path, body):
        self._check("PATCH", path)
        self.issues[int(path.split("/")[1])].update(body)
        return {}


def _issue(number, block):
    return {"number": number, "body": "text\n" + pmc.machine_block({"format": 1, **block})}


def _pr(number, sha, login="dev"):
    return {"number": number, "merge_commit_sha": sha, "merged_at": "2026-10-21T00:00:00Z",
            "base": {"ref": "ouroboros"}, "user": {"login": login}, "title": f"Change {number}"}


def _route(github, failed, verdicts, *, recheck=None, flaky=(), node="success"):
    return pmc.route(github, classification={"failed": [{"id": nodeid} for nodeid in failed],
                                             "flaky": [{"id": nodeid} for nodeid in flaky],
                                             "selection": {"node": node}},
                     attribution={"verdicts": verdicts}, head=HEAD, run_url="https://example.invalid/run/7",
                     recheck=recheck or (lambda ids: pytest.fail(f"nothing to recheck: {ids}")))


def test_a_culprit_gets_one_pr_comment_and_one_new_issue():
    culprit = "c" * 40
    github = FakeGitHub(prs={culprit: [_pr(2066, "d" * 40), _pr(2067, culprit, login="author")]})
    verdicts = [{"id": nodeid, "verdict": "culprit", "culprit": culprit, "last_good": "b" * 40}
                for nodeid in ("tests/t.py::test_a", "tests/t.py::test_b")]
    record = _route(github, ["tests/t.py::test_a", "tests/t.py::test_b"], verdicts)
    assert record["created"] == [100] and record["comments"] == [2067] and record["warnings"] == []
    assert record["handed"] == [{"ids": ["tests/t.py::test_a", "tests/t.py::test_b"], "pr": 2067, "issue": 100}]
    assert github.posts[0] == ("labels", {"name": pmc.LABEL, "color": "b60205",
                                          "description": "A merge into ouroboros broke tests (post-merge check)"})
    issue = github.issues[100]
    assert issue["title"] == "Post-merge red: #2067 Change 2067"
    assert pmc.parse_machine_block(issue["body"]) == {
        "format": 1, "kind": "culprit", "pr": 2067, "culprit": culprit, "last_good": "b" * 40,
        "ids": ["tests/t.py::test_a", "tests/t.py::test_b"]}
    [(number, text)] = github.comments
    assert number == 2067 and text.startswith("@author ")
    for fact in ("`tests/t.py::test_a`", "`tests/t.py::test_b`", f"First bad commit: `{culprit}`",
                 f"last good commit: `{'b' * 40}`", "https://example.invalid/run/7", "Tracking issue: #100.",
                 "blocks nothing and reverts nothing", "fix goes forward in a new pull request"):
        assert fact in text


def test_an_open_issue_of_the_same_pull_request_is_extended_not_duplicated():
    culprit = "c" * 40
    github = FakeGitHub(label=True, prs={culprit: [_pr(2067, culprit)]}, issues=[
        _issue(5, {"kind": "culprit", "pr": 2067, "culprit": culprit, "last_good": "b" * 40,
                   "ids": ["tests/t.py::test_a"]})])
    verdicts = [{"id": "tests/t.py::test_new", "verdict": "culprit", "culprit": culprit, "last_good": "b" * 40}]
    record = _route(github, ["tests/t.py::test_new", "tests/t.py::test_a"], verdicts)
    assert record["created"] == [] and record["updated"] == [5] and record["closed"] == []
    assert pmc.parse_machine_block(github.issues[5]["body"])["ids"] == ["tests/t.py::test_a", "tests/t.py::test_new"]
    assert [number for number, _ in github.comments] == [5, 2067]
    assert "labels" not in [path for path, _ in github.posts]


def test_a_red_that_stays_red_does_not_comment_on_its_pull_request_again():
    culprit = "c" * 40
    github = FakeGitHub(prs={culprit: [_pr(2067, culprit, login="author")]})
    verdicts = [{"id": "tests/t.py::test_a", "verdict": "culprit", "culprit": culprit, "last_good": "b" * 40}]
    first = _route(github, ["tests/t.py::test_a"], verdicts)
    again = _route(github, ["tests/t.py::test_a"], verdicts)
    assert first["comments"] == [2067] and first["created"] == [100]
    assert again["comments"] == [] and again["created"] == again["updated"] == []
    assert again["handed"] == [{"ids": ["tests/t.py::test_a"], "pr": 2067, "issue": 100}]
    assert [number for number, _ in github.comments] == [2067]


def test_issues_whose_tests_pass_at_the_head_are_closed_and_others_stay_open():
    github = FakeGitHub(label=True, issues=[
        _issue(5, {"kind": "culprit", "pr": 1, "culprit": "c" * 40, "ids": ["tests/t.py::test_fixed",
                                                                            "tests/t.py::test_deleted"]}),
        _issue(6, {"kind": "pre-existing", "ids": ["tests/t.py::test_still_red"]}),
        _issue(7, {"kind": "unattributed", "ids": ["tests/t.py::test_red_this_run"]}),
        _issue(8, {"kind": "culprit", "pr": 2, "culprit": "d" * 40, "ids": [pmc.NODE_LANE]}),
    ])
    asked = []

    def recheck(ids):
        asked.append(ids)
        return {"tests/t.py::test_fixed": "passed", "tests/t.py::test_deleted": "absent",
                "tests/t.py::test_still_red": "failed"}

    record = _route(github, ["tests/t.py::test_red_this_run"],
                    [{"id": "tests/t.py::test_red_this_run", "verdict": "pre-existing"}], recheck=recheck)
    # This run's own failures are not run again; the Node lane's result is this run's.
    assert asked == [["tests/t.py::test_deleted", "tests/t.py::test_fixed", "tests/t.py::test_still_red"]]
    assert record["closed"] == [5, 8] and record["created"] == [] and record["comments"] == []
    assert github.issues[5]["state"] == "closed" and github.issues[5]["state_reason"] == "completed"
    assert github.issues[6]["state"] == github.issues[7]["state"] == "open"
    assert [number for number, _ in github.comments] == [5, 8]
    assert github.comments[0][1].startswith(f"Green at `{HEAD}`")
    record = _route(FakeGitHub(issues=[_issue(8, {"kind": "culprit", "pr": 2, "ids": [pmc.NODE_LANE]})]),
                    [], [], node="failure")
    assert record["closed"] == []  # a red Node lane in this run is not green


def test_a_lint_issue_closes_only_once_a_linted_head_no_longer_has_its_finding():
    def run(lint_checked, failed=()):
        github = FakeGitHub(label=True, issues=[_issue(5, {"kind": "culprit", "pr": 1, "culprit": "c" * 40,
                                                           "ids": [LINTED]})])
        record = pmc.route(github, classification={"failed": [{"id": nodeid} for nodeid in failed], "flaky": [],
                                                   "selection": {"node": "success"}, "lint_checked": lint_checked},
                           attribution={"verdicts": [{"id": nodeid, "verdict": "unattributed"} for nodeid in failed]},
                           head=HEAD, run_url="https://example.invalid/run/9",
                           recheck=lambda ids: pytest.fail(f"a finding is not a test to rerun: {ids}") if ids else {})
        return record["closed"]

    assert run(True) == [5]
    assert run(True, failed=[LINTED]) == []  # still found at the head
    assert run(False) == []  # ruff did not run: nothing proves the finding gone


def test_reds_without_blame_get_one_issue_unless_already_tracked():
    github = FakeGitHub(label=True, issues=[_issue(5, {"kind": "pre-existing", "ids": ["tests/t.py::test_known"]})])
    recheck = lambda ids: {nodeid: "failed" for nodeid in ids}  # noqa: E731
    verdicts = [{"id": "tests/t.py::test_known", "verdict": "pre-existing"},
                {"id": "tests/t.py::test_new_old", "verdict": "pre-existing"},
                {"id": "tests/t.py::test_lost", "verdict": "unattributed", "why": "probe budget"}]
    record = _route(github, [row["id"] for row in verdicts], verdicts, recheck=recheck)
    assert record["updated"] == [5] and record["created"] == [101] and record["comments"] == []
    assert pmc.parse_machine_block(github.issues[5]["body"])["ids"] == ["tests/t.py::test_known",
                                                                        "tests/t.py::test_new_old"]
    assert pmc.parse_machine_block(github.issues[101]["body"]) == {"format": 1, "kind": "unattributed",
                                                                   "ids": ["tests/t.py::test_lost"]}
    assert "@" not in github.issues[101]["body"]


def test_api_errors_become_warnings_and_the_hand_over_goes_on():
    culprit = "c" * 40
    github = FakeGitHub(prs={culprit: [_pr(2067, culprit)]}, fail={("POST", "labels"), ("POST", "issues")})
    verdicts = [{"id": "tests/t.py::test_a", "verdict": "culprit", "culprit": culprit, "last_good": "b" * 40}]
    record = _route(github, ["tests/t.py::test_a"], verdicts)
    assert record["created"] == [] and record["comments"] == [2067]
    assert record["warnings"] == ["create the post-merge-red label: HTTP 502 Bad Gateway",
                                  "create an issue: HTTP 502 Bad Gateway"]
    assert "Tracking issue" not in github.comments[0][1]
    # Unreadable open issues: comments still go out, but no issue that could duplicate one.
    github = FakeGitHub(prs={culprit: [_pr(2067, culprit)]}, fail={("GET", "issues")})
    record = _route(github, ["tests/t.py::test_a"], verdicts)
    assert record["created"] == [] and record["comments"] == [2067] and len(record["warnings"]) == 1


def test_only_the_merge_commit_of_a_merged_pull_request_is_blamed():
    culprit = "c" * 40
    open_pr = dict(_pr(1, culprit), merged_at=None)
    other_base = dict(_pr(2, culprit), base={"ref": "main"})
    github = FakeGitHub(prs={culprit: [open_pr, other_base, _pr(3, "d" * 40)]})
    assert pmc.find_merged_pr(github, culprit) is None
    github.prs[culprit].append(_pr(4, culprit))
    assert pmc.find_merged_pr(github, culprit)["number"] == 4
    verdicts = [{"id": "tests/t.py::test_a", "verdict": "culprit", "culprit": "e" * 40, "last_good": "b" * 40}]
    record = _route(FakeGitHub(), ["tests/t.py::test_a"], verdicts)  # a direct push: no pull request
    assert record["comments"] == [] and record["created"] == [100]


# --- summary ---------------------------------------------------------------------------------

def test_the_summary_is_red_only_for_failures_left_after_their_retry(tmp_path):
    text, red = pmc.render_summary(tmp_path)
    assert red and "did not finish" in text
    (tmp_path / "base.json").write_text(json.dumps({"base": "b" * 40, "head": HEAD, "why": "the branch tip"}), encoding="utf-8")
    (tmp_path / "classification.json").write_text(json.dumps({
        "failed": [], "flaky": [{"id": "tests/t.py::test_f", "why": "failed in call", "pass": "parallel"}],
        "problems": [], "selection": {"tier": "selected", "file_count": 3, "available": 40, "node": "success"}}), encoding="utf-8")
    text, red = pmc.render_summary(tmp_path, [("1" * 40, "Merge pull request #2146 from x"), ("2" * 40, "direct")])
    assert not red and "GREEN" in text and "2 commit(s) (#2146)" in text and "`tests/t.py::test_f`" in text
    (tmp_path / "classification.json").write_text(json.dumps({
        "failed": [{"id": "tests/t.py::test_a", "why": "failed in call", "pass": "serial"}], "flaky": [],
        "problems": [], "selection": {}}), encoding="utf-8")
    (tmp_path / "attribution.json").write_text(json.dumps({"verdicts": [
        {"id": "tests/t.py::test_a", "verdict": "culprit", "culprit": "c" * 40, "last_good": "b" * 40}]}), encoding="utf-8")
    (tmp_path / "routing.json").write_text(json.dumps({
        "comments": [2067], "created": [100], "updated": [], "closed": [5], "warnings": ["comment on #9: HTTP 403"],
        "handed": [{"ids": ["tests/t.py::test_a"], "pr": 2067, "issue": 100}]}), encoding="utf-8")
    text, red = pmc.render_summary(tmp_path)
    assert red and ("| `tests/t.py::test_a` | failed in call | culprit | `cccccccccccc` | `bbbbbbbbbbbb` "
                    "| PR #2067, issue #100 |") in text
    assert "closed as green: #5" in text and "**Warning:** comment on #9: HTTP 403" in text
    web = "https://github.com/o/r"
    text, _ = pmc.render_summary(tmp_path, [("1" * 40, "Merge pull request #2146 from x")], web)
    for link in (f"[`cccccccccccc`]({web}/commit/{'c' * 40})", f"PR [#2067]({web}/pull/2067)",
                 f"issue [#100]({web}/issues/100)", f"([#2146]({web}/pull/2146))", f"[#5]({web}/issues/5)"):
        assert link in text
    # A retry cut short by its timeout left only its provisional record: red, and said so.
    (tmp_path / "classification.json").write_text(json.dumps({"complete": False, "failed": [], "flaky": [],
                                                              "problems": []}), encoding="utf-8")
    text, red = pmc.render_summary(tmp_path)
    assert red and "the retry did not finish" in text


# --- run_tests.py --keep-going ---------------------------------------------------------------

@pytest.fixture
def run_tests():
    spec = importlib.util.spec_from_file_location("run_tests_keep_going", REPO / "scripts" / "run_tests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("keep_going", [False, True])
def test_keep_going_runs_every_lane_after_a_red_one(run_tests, monkeypatch, tmp_path, keep_going):
    from ouroboros import preflight_inventory, test_selection

    chosen = test_selection.TestSelection("selected", "base", ("tests/test_floor.py",), (), (), "policy", 3)
    monkeypatch.setattr(test_selection, "select_tests", lambda *a, **k: chosen)
    monkeypatch.setattr(preflight_inventory, "read_inventory", lambda root, label: ({}, ""))
    monkeypatch.setattr(preflight_inventory, "inventory_problem", lambda selection, inventory: "")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("OUROBOROS_PREFLIGHT_SERIAL", raising=False)  # two passes, not the legacy single one
    for node, parallel in ((1, 0), (0, 1)):  # a red Node lane; a red parallel pass
        ran = []
        monkeypatch.setattr(run_tests, "_node_lane", lambda: node)
        monkeypatch.setattr(run_tests, "_run", lambda label, argv, cwd: ran.append(label) or
                            (parallel if label == "parallel" else 0))
        evidence = tmp_path / f"{keep_going}-{node}"
        code = run_tests.main(["--selected", "--evidence-dir", str(evidence), *(["--keep-going"] * keep_going)])
        record = json.loads((evidence / "selection.json").read_text(encoding="utf-8"))
        assert code == 1 and record["outcome"] == "failure"
        assert record["node"] == ("failure" if node else "success")
        if keep_going:
            assert ran == ["parallel", "serial"]
        else:  # the unchanged default stops at the first red lane
            assert ran == ([] if node else ["parallel"])
    with pytest.raises(SystemExit):
        run_tests.main(["--keep-going", "tests/test_x.py"])
