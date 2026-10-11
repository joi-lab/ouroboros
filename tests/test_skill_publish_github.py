"""Hermetic tests for the one-shot GitHub publication transport."""

from __future__ import annotations

import base64
import json
import subprocess
import types

import pytest

from ouroboros import skill_publish_github as github
from ouroboros.tools import github as transport
from ouroboros.tools.github import GhResult

BASE_SHA = "1" * 40
COMMIT_SHA = "2" * 40
SNAPSHOT_SHA = "a" * 64
RULESET_SHA = "b" * 64


@pytest.fixture(autouse=True)
def synthetic_github_credentials(monkeypatch):
    # Load-bearing for hermeticity: ``github_repair_hint`` consults the host's real
    # gh configuration when no token is configured, so without this every failure
    # hint below would depend on whether the developer's machine has gh logged in.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_SYNTHETIC1234567890")


def _attempt():
    facts = types.SimpleNamespace(
        skill="demo",
        snapshot_hash=SNAPSHOT_SHA,
        scanner={"ruleset_sha256": RULESET_SHA},
        marks=[],
    )

    def mark(stage, **values):
        facts.marks.append((stage, values))

    facts.mark = mark
    return facts


def _pull_row(*, sha: str = COMMIT_SHA, owner: str = "alice", base: str = "main"):
    return {
        "number": 7,
        "html_url": "https://github.com/hub/project/pull/7",
        "head": {
            "sha": sha,
            "ref": "submit/demo-v1.0.0",
            "repo": {"owner": {"login": owner}},
        },
        "base": {"ref": base},
    }


def test_commit_payload_hardcodes_ouroboros_coauthor(monkeypatch):
    captured = {}

    def fake_json(_ctx, _args, **kwargs):
        captured.update(json.loads(kwargs["input_data"]))
        return {
            "data": {
                "createCommitOnBranch": {
                    "commit": {
                        "oid": COMMIT_SHA,
                        "url": "https://github.com/alice/project/commit/" + COMMIT_SHA,
                    }
                }
            }
        }

    monkeypatch.setattr(github, "_json_object", fake_json)
    sha, url = github.commit_payload(
        types.SimpleNamespace(),
        "alice",
        "project",
        "submit/demo-v1.0.0",
        BASE_SHA,
        "Add skill: demo v1.0.0",
        [{"path": "skills/demo/SKILL.md", "contents": "YQ=="}],
        [{"path": "skills/demo/removed.py"}],
    )
    message = captured["variables"]["input"]["message"]
    assert sha == COMMIT_SHA
    assert url.endswith(COMMIT_SHA)
    assert message == {
        "headline": "Add skill: demo v1.0.0",
        "body": ("Co-authored-by: Ouroboros <311266734+ouroboros-agent@users.noreply.github.com>"),
    }
    assert captured["variables"]["input"]["fileChanges"] == {
        "additions": [{"path": "skills/demo/SKILL.md", "contents": "YQ=="}],
        "deletions": [{"path": "skills/demo/removed.py"}],
    }


def test_upstream_catalog_is_read_from_the_exact_resolved_base_sha(monkeypatch):
    calls = []

    def fake_json(_ctx, args, **_kwargs):
        calls.append(args)
        if "/git/refs/heads/" in args[-1]:
            return {"object": {"sha": BASE_SHA}}
        return {
            "content": base64.b64encode(b'{"skills":[]}').decode("ascii"),
        }

    monkeypatch.setattr(github, "_json_object", fake_json)
    catalog, base_sha = github.fetch_upstream_catalog(
        types.SimpleNamespace(),
        "hub",
        "project",
        "main",
    )
    assert catalog == {"skills": []}
    assert base_sha == BASE_SHA
    assert calls[-1][-1] == f"/repos/hub/project/contents/catalog.json?ref={BASE_SHA}"


def test_owner_actor_skips_fork_and_sync(monkeypatch):
    monkeypatch.setattr(
        github,
        "_gh_run",
        lambda *_args, **_kwargs: pytest.fail("owner path must issue no fork command"),
    )
    attempt = _attempt()
    github.prepare_publish_repository(
        types.SimpleNamespace(),
        attempt,
        owner="HubOwner",
        repo="project",
        base_branch="main",
        login="hubowner",
    )
    assert attempt.marks == [("fork_ready", {"repository": "hubowner/project", "actor": "hubowner"})]


def test_non_owner_sync_failure_is_typed(monkeypatch):
    calls = []

    def fake_gh(args, _ctx, **_kwargs):
        calls.append(args)
        if args[:2] == ["repo", "view"]:
            return GhResult(True, '{"name":"project"}', 0, None, "")
        return GhResult(False, "⚠️ GH_ERROR: synthetic", 1, None, "exit")

    monkeypatch.setattr(github, "_gh_run", fake_gh)
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.prepare_publish_repository(
            types.SimpleNamespace(),
            _attempt(),
            owner="hub",
            repo="project",
            base_branch="main",
            login="alice",
        )
    assert caught.value.reason_code == "fork_sync_failed"
    assert [call[0] for call in calls] == ["repo", "api"]


def test_direct_pr_url_yields_validated_receipt_without_lookup(monkeypatch):
    calls = []

    def fake_gh(args, _ctx, **_kwargs):
        calls.append(args)
        return GhResult(True, "https://github.com/hub/project/pull/7", 0, None, "")

    monkeypatch.setattr(github, "_gh_run", fake_gh)
    monkeypatch.setattr(
        github,
        "_json_value",
        lambda *_args, **_kwargs: pytest.fail("valid direct output must not settle"),
    )
    attempt = _attempt()
    receipt = github.create_pr_receipt(
        types.SimpleNamespace(),
        attempt,
        owner="hub",
        repo="project",
        base_branch="main",
        login="alice",
        branch="submit/demo-v1.0.0",
        title="Add demo",
        body="body",
        commit_sha=COMMIT_SHA,
    )
    assert receipt and receipt["number"] == 7
    assert len(calls) == 1
    assert [stage for stage, _facts in attempt.marks] == ["pr_create_attempted"]


def test_ambiguous_create_uses_one_exact_read_only_settlement(monkeypatch):
    create_calls = []
    lookup_calls = []

    def fake_gh(args, _ctx, **_kwargs):
        create_calls.append(args)
        return GhResult(False, "⚠️ GH_TIMEOUT: synthetic", None, None, "timeout")

    def fake_json(_ctx, args, **_kwargs):
        lookup_calls.append(args)
        return [_pull_row()]

    monkeypatch.setattr(github, "_gh_run", fake_gh)
    monkeypatch.setattr(github, "_json_value", fake_json)
    receipt = github.create_pr_receipt(
        types.SimpleNamespace(),
        _attempt(),
        owner="hub",
        repo="project",
        base_branch="main",
        login="alice",
        branch="submit/demo-v1.0.0",
        title="Add demo",
        body="body",
        commit_sha=COMMIT_SHA,
    )
    assert receipt and receipt["url"].endswith("/pull/7")
    assert len(create_calls) == 1
    assert len(lookup_calls) == 1
    assert "--method" in lookup_calls[0]
    assert "GET" in lookup_calls[0]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_pull_row(), _pull_row()],
        [_pull_row(sha="3" * 40)],
        [_pull_row(owner="other")],
        [_pull_row(base="wrong")],
        [_pull_row() | {"number": 8}],
    ],
)
def test_ambiguous_settlement_never_claims_wrong_or_nonunique_pr(monkeypatch, rows):
    monkeypatch.setattr(github, "_gh_run", lambda *_args, **_kwargs: GhResult(True, "garbage", 0, None, ""))
    monkeypatch.setattr(github, "_json_value", lambda *_args, **_kwargs: rows)
    receipt = github.create_pr_receipt(
        types.SimpleNamespace(),
        _attempt(),
        owner="hub",
        repo="project",
        base_branch="main",
        login="alice",
        branch="submit/demo-v1.0.0",
        title="Add demo",
        body="body",
        commit_sha=COMMIT_SHA,
    )
    assert receipt is None


def test_existing_branch_is_never_overwritten(monkeypatch):
    monkeypatch.setattr(github, "_gh_run", lambda *_args, **_kwargs: GhResult(True, '{"ref":"exists"}', 0, None, ""))
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.ensure_branch(
            types.SimpleNamespace(),
            "alice",
            "project",
            "submit/demo-v1.0.0",
            BASE_SHA,
        )
    assert caught.value.reason_code == "submission_branch_exists"


@pytest.mark.parametrize(
    "failure,stderr,http_status,hint",
    [
        ("exit", "gh: Resource not accessible by personal access token (HTTP 403)", 403, "Settings → Secrets"),
        ("exit", "gh: Conflict (HTTP 409)", 409, "conflict (HTTP 409)"),
        ("timeout", "", None, "the outcome may be unknown"),
        ("cli_missing", "", None, "Install the GitHub CLI (gh)"),
        ("exit", "gh: ghp_SYNTHETIC1234567890 refused (HTTP 403)", 403, "Settings → Secrets"),
    ],
)
def test_sync_failure_preserves_process_evidence_and_stops(
    monkeypatch, tmp_path, failure, stderr, http_status, hint,
):
    calls = []

    def run(cmd, *, cwd, capture_output, text, timeout, input, env):
        calls.append(cmd)
        assert cwd == str(tmp_path)
        assert capture_output is True and text is True and input is None
        assert env["GH_TOKEN"] == "ghp_SYNTHETIC1234567890"
        if cmd[1:3] == ["repo", "view"]:
            return subprocess.CompletedProcess(cmd, 0, '{"name":"project"}', "")
        assert cmd == ["gh", "api", "-X", "POST", "/repos/alice/project/merge-upstream", "-f", "branch=main"]
        assert timeout == 45
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if failure == "cli_missing":
            raise FileNotFoundError("synthetic missing CLI")
        return subprocess.CompletedProcess(cmd, 1, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    attempt = _attempt()
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.prepare_publish_repository(
            types.SimpleNamespace(repo_dir=tmp_path), attempt,
            owner="hub", repo="project", base_branch="main", login="alice",
        )
    error = caught.value
    assert error.reason_code == "fork_sync_failed"
    assert error.http_status == http_status
    assert error.operation == "merge-upstream"
    assert hint in error.repair_hint
    assert [stage for stage, _ in attempt.marks] == ["fork_ready"]
    assert len(calls) == 2  # Exactly one merge-upstream; never retry a mutation.
    assert "ghp_SYNTHETIC1234567890" not in error.detail
    if "ghp_SYNTHETIC" in stderr:
        assert "***" in error.detail
    if http_status:
        assert str(http_status) in error.detail
    if failure == "cli_missing":
        assert "token" not in error.repair_hint.lower()
    if failure == "timeout":
        assert error.detail == "⚠️ GH_TIMEOUT: exceeded 45s."


def test_sync_success_marks_fork_synced(monkeypatch, tmp_path):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, '{}', "")

    monkeypatch.setattr(subprocess, "run", run)
    attempt = _attempt()
    github.prepare_publish_repository(
        types.SimpleNamespace(repo_dir=tmp_path), attempt,
        owner="hub", repo="project", base_branch="main", login="alice",
    )
    assert [stage for stage, _ in attempt.marks] == ["fork_ready", "fork_synced"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stderr,expected_status,expected_text",
    [
        # the FIRST line-terminal gh marker wins; the bounded head keeps three non-empty lines
        ("\n first\n\n second (HTTP 401)\n third\n ignored (HTTP 403)", 401,
         "⚠️ GH_ERROR: first | second (HTTP 401) | third"),
        # a marker quoted mid-sentence is prose; gh's own marker ends its line
        ("warning: quoted (HTTP 403) is not a status\ngh: Not Found (HTTP 404)", 404,
         "⚠️ GH_ERROR: warning: quoted (HTTP 403) is not a status | gh: Not Found (HTTP 404)"),
        ("a marker (HTTP 403) inside a sentence", None, "⚠️ GH_ERROR: a marker (HTTP 403) inside a sentence"),
        ("gh: Not Found (HTTP 404)\r\n", 404, "⚠️ GH_ERROR: gh: Not Found (HTTP 404)"),
        # gh api without a message; go-gh HTTPError from every other command, wrapped or bare
        ("gh: HTTP 401", 401, "⚠️ GH_ERROR: gh: HTTP 401"),
        ("failed to fork: HTTP 403: Resource not accessible by personal access token (https://api.github.com/repos/hub/project/forks)",
         403, None),
        ("HTTP 422: Validation Failed (https://api.github.com/repos/hub/project/pulls)", 422, None),
        ("note that HTTP 403 is not what happened here", None, "⚠️ GH_ERROR: note that HTTP 403 is not what happened here"),
        # a bare number is prose, never a status
        ("permission denied, status 403", None, "⚠️ GH_ERROR: permission denied, status 403"),
        # the status is read from the WHOLE redacted stderr, before the head is cut
        ("first\nsecond\nthird\nlater line (HTTP 403)", 403, "⚠️ GH_ERROR: first | second | third"),
        ("x" * 700 + " (HTTP 403)", 403, None),
        ("", None, "⚠️ GH_ERROR: "),
    ],
)
def test_transport_bounds_stderr_and_only_reads_gh_http_marker(
    monkeypatch, tmp_path, stderr, expected_status, expected_text,
):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 7, "ignored stdout", stderr),
    )
    result = transport._gh_run(["api", "/user"], types.SimpleNamespace(repo_dir=tmp_path))
    assert (result.ok, result.exit_code, result.http_status, result.failure) == (False, 7, expected_status, "exit")
    assert result.text.startswith("⚠️ GH_ERROR: ")
    assert len(result.text.removeprefix("⚠️ GH_ERROR: ")) <= 600
    assert "ignored" not in result.text
    if expected_text is not None:
        assert result.text == expected_text


@pytest.mark.parametrize("filename,failure,fragment", [
    (None, "cli_missing", "`gh` CLI not found"),
    ("/usr/local/bin/gh", "cli_missing", "`gh` CLI not found"),
    # a vanished working directory is a launch failure, not a missing CLI
    ("/vanished/project", "exception", "/vanished/project"),
])
def test_launch_failure_names_what_is_missing(monkeypatch, tmp_path, filename, failure, fragment):
    def run(cmd, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", filename)

    monkeypatch.setattr(subprocess, "run", run)
    result = transport._gh_run(["api", "/user"], types.SimpleNamespace(repo_dir=tmp_path))
    assert (result.ok, result.exit_code, result.http_status, result.failure) == (False, None, None, failure)
    assert fragment in result.text
    hint = github.github_repair_hint(result, operation="user", repository="hub/project", default="default")
    assert ("Install" in hint) == (failure == "cli_missing")


@pytest.mark.parametrize("failure", ["cli_missing", "timeout", "exit", "exception"])
def test_transport_never_publishes_a_sidecar(monkeypatch, tmp_path, failure):
    sentinel = object()
    ctx = types.SimpleNamespace(repo_dir=tmp_path, _active_builtin_tool_result=sentinel)

    def run(cmd, **kwargs):
        if failure == "cli_missing":
            raise FileNotFoundError()
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        if failure == "exception":
            raise RuntimeError("synthetic ghp_SYNTHETIC1234567890")
        return subprocess.CompletedProcess(cmd, 1, "", "synthetic")

    monkeypatch.setattr(subprocess, "run", run)
    result = transport._gh_run(["api", "/user"], ctx)
    assert result.failure == failure
    assert ctx._active_builtin_tool_result is sentinel
    assert transport._gh_cmd(["api", "/user"], ctx) == result.text
    assert ctx._active_builtin_tool_result is sentinel
    assert "ghp_SYNTHETIC1234567890" not in result.text


def test_unconfigured_credentials_hint_uses_settings(monkeypatch):
    monkeypatch.setattr(github, "github_cli_configured", lambda: False)
    result = GhResult(False, "⚠️ GH_ERROR: synthetic", 1, None, "exit")
    assert github.github_repair_hint(result, operation="repo view", repository="alice/project", default="default") == (
        "No GitHub credential is configured; add GITHUB_TOKEN in Settings → Secrets, then retry."
    )


@pytest.mark.parametrize("handler,kwargs,expected", [
    (transport._get_issue, {"number": 0}, "⚠️ TOOL_ARG_ERROR: issue number must be positive"),
    (transport._comment_on_issue, {"number": 0, "body": "comment"}, "⚠️ TOOL_ARG_ERROR: issue number must be positive"),
    (transport._close_issue, {"number": 0}, "⚠️ TOOL_ARG_ERROR: issue number must be positive"),
    (transport._get_pr, {"number": 0}, "⚠️ TOOL_ARG_ERROR: PR number must be positive."),
    (transport._comment_on_pr, {"number": 0, "body": "comment"}, "⚠️ TOOL_ARG_ERROR: PR number must be positive."),
    (transport._comment_on_pr, {"number": 7, "body": "  "}, "⚠️ TOOL_ARG_ERROR: comment body cannot be empty."),
    (transport._create_issue, {"title": " "}, "⚠️ TOOL_ARG_ERROR: issue title cannot be empty."),
])
def test_argument_refusals_publish_typed_argument_errors(handler, kwargs, expected):
    ctx = types.SimpleNamespace(_active_builtin_tool_result=None)
    text = handler(ctx, **kwargs)
    result = ctx._active_builtin_tool_result
    assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR")
    assert result.text == text == expected


def test_fork_denial_carries_the_status_from_gh_own_error_line(monkeypatch, tmp_path):
    """``repo fork`` fails through go-gh's ``HTTP NNN: …`` shape, not ``(HTTP NNN)``."""
    calls = []

    def run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["repo", "view"]:
            return subprocess.CompletedProcess(cmd, 1, "", "GraphQL: Could not resolve to a Repository")
        assert cmd[1:3] == ["repo", "fork"]
        return subprocess.CompletedProcess(
            cmd, 1, "",
            "failed to fork: HTTP 403: Resource not accessible by personal access token "
            "(https://api.github.com/repos/hub/project/forks)",
        )

    monkeypatch.setattr(subprocess, "run", run)
    attempt = _attempt()
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.prepare_publish_repository(
            types.SimpleNamespace(repo_dir=tmp_path), attempt,
            owner="hub", repo="project", base_branch="main", login="alice",
        )
    error = caught.value
    assert error.reason_code == "fork_prepare_failed"
    assert error.http_status == 403
    assert error.operation == "repo fork"
    assert "GITHUB_TOKEN in Settings → Secrets" in error.repair_hint
    assert "HTTP 403" in error.detail
    assert attempt.marks == []
    assert len(calls) == 2


@pytest.mark.parametrize("create_failed", [True, False])
def test_failed_pr_settlement_keeps_producer_evidence_without_retry(monkeypatch, tmp_path, create_failed):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "create"]:
            if create_failed:
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return subprocess.CompletedProcess(cmd, 0, "malformed URL", "")
        assert cmd[1:4] == ["api", "--method", "GET"]
        return subprocess.CompletedProcess(cmd, 1, "", "gh: read refused (HTTP 403)")

    monkeypatch.setattr(subprocess, "run", run)
    attempt = _attempt()
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.create_pr_receipt(
            types.SimpleNamespace(repo_dir=tmp_path), attempt,
            owner="hub", repo="project", base_branch="main", login="alice",
            branch="submit/demo-v1.0.0", title="Add demo", body="body", commit_sha=COMMIT_SHA,
        )
    error = caught.value
    assert error.reason_code == "pr_open_indeterminate"
    assert len(calls) == 2
    assert [stage for stage, _ in attempt.marks] == ["pr_create_attempted"]
    assert error.operation == ("pr create" if create_failed else "pulls")
    assert error.http_status == (None if create_failed else 403)
    assert ("GH_TIMEOUT" if create_failed else "403") in error.detail
    assert "inspect" in error.repair_hint.lower() and "before retrying" in error.repair_hint
    if not create_failed:
        assert error.repair_hint.startswith("gh pr create reported success")
        assert "HTTP 403 for pulls on hub/project" in error.repair_hint


@pytest.mark.parametrize("stdout", ['["wrong shape"]', 'invalid ghp_SYNTHETIC1234567890 ' + 'x' * 1000])
def test_malformed_json_keeps_bounded_redacted_detail(monkeypatch, tmp_path, stdout):
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, stdout, ""),
    )
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.fetch_upstream_catalog(types.SimpleNamespace(repo_dir=tmp_path), "hub", "project", "main")
    error = caught.value
    assert error.reason_code == "upstream_read_failed"
    assert error.http_status is None
    assert error.operation == "git/refs"
    assert error.detail
    assert len(error.detail) <= 640
    assert "ghp_SYNTHETIC1234567890" not in error.detail


@pytest.mark.parametrize("ref,content,reason_code,fragment", [
    # the parser's own cause travels with the stage code
    ({"object": {"sha": BASE_SHA}}, b"{", "upstream_catalog_invalid", "Expecting"),
    ({"object": {"sha": BASE_SHA}}, b"[]", "upstream_catalog_invalid", "list, not an object"),
    # a wrongly shaped ref answer is a read failure with the answer as its detail
    ({"object": "not a mapping"}, b"{}", "upstream_read_failed", "not a mapping"),
])
def test_malformed_upstream_answers_keep_their_cause(monkeypatch, ref, content, reason_code, fragment):
    def fake_json(_ctx, args, **_kwargs):
        if "/git/refs/heads/" in args[-1]:
            return ref
        return {"content": base64.b64encode(content).decode("ascii")}

    monkeypatch.setattr(github, "_json_object", fake_json)
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.fetch_upstream_catalog(types.SimpleNamespace(), "hub", "project", "main")
    error = caught.value
    assert error.reason_code == reason_code
    assert fragment in error.detail
    assert error.operation == ("contents" if reason_code == "upstream_catalog_invalid" else "git/refs")


@pytest.mark.parametrize("returncode,stdout,stderr", [
    (1, "", "gh: mutation refused (HTTP 403)"),
    (0, '{"errors":[{"message":"synthetic GraphQL rejection"}]}', ""),
])
def test_commit_failure_keeps_graphql_cause(monkeypatch, tmp_path, returncode, stdout, stderr):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(github.SkillPublishGitHubError) as caught:
        github.commit_payload(
            types.SimpleNamespace(repo_dir=tmp_path), "alice", "project", "submit/demo-v1.0.0",
            BASE_SHA, "Add demo", [],
        )
    error = caught.value
    assert error.reason_code == "commit_create_failed"
    assert error.operation == "graphql"
    assert error.http_status == (403 if returncode else None)
    assert ("403" if returncode else "synthetic GraphQL rejection") in error.detail
    assert len(calls) == 1
