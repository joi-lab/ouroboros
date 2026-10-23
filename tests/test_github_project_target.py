"""GitHub tools retain Project selection and explicit targets through every subcall."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros.tools import github, github_read
from ouroboros.tools.registry import ToolContext


def _api_parts(argv):
    """``(route, hostname, typed_fields)`` of one ``gh api`` argv; ``_api_method`` reads its HTTP verb."""
    route, hostname, fields, rest = "", "", {}, list(argv[2:])
    while rest:
        item = rest.pop(0)
        if item in ("--method", "--hostname", "--input"):
            value = rest.pop(0)
            hostname = value if item == "--hostname" else hostname
        elif item in ("-f", "-F"):
            key, _sep, value = rest.pop(0).partition("=")
            fields[key] = value
        elif item != "--include":
            route = item
    return route, hostname, fields


def _context(tmp_path, kind):
    system = tmp_path / "system"
    project = tmp_path / "project"
    system.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    ctx = ToolContext(repo_dir=system, drive_root=tmp_path / "data", task_id="task-fixture")
    if kind == "queued":
        ctx.workspace_root, ctx.workspace_mode, ctx.project_id = project, "external", "project-fixture"
    elif kind == "room":
        ctx.is_direct_chat, ctx.project_id = True, "project-fixture"
        ctx.task_metadata = {"_project_room_dir": str(project)}
    return ctx, project if kind != "system" else system


SHA, MOVED = "a" * 40, "b" * 40
BASE = "https://github.example/owner/selected"


def _run(run_id, name="CI", status="completed", conclusion="success", **extra):
    return {"databaseId": run_id, "workflowName": name, "event": "pull_request", "status": status,
            "conclusion": conclusion, "attempt": 1, "url": f"{BASE}/actions/runs/{run_id}", "headSha": SHA, **extra}


def _job(job_id, name, status="completed", conclusion="success", steps=(), run_id=11):
    return {"databaseId": job_id, "name": name, "status": status, "conclusion": conclusion,
            "url": f"{BASE}/actions/runs/{run_id}/job/{job_id}",
            "steps": [{"number": number, "name": step, "status": step_status, "conclusion": step_conclusion}
                      for number, step, step_status, step_conclusion in steps]}


@pytest.fixture
def gh_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(github, "github_token_from_env_or_settings", lambda: "fixture-token")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        output = ""
        if argv[1] == "api":
            route = _api_parts(argv)[0].split("?", 1)[0]
            method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
            if route == "graphql":
                output = json.dumps({"data": {"nodes": [{"id": "PR_node7", "commits": {"totalCount": 1}, "reviewDecision": "APPROVED"}]}})
            elif route.endswith(("/issues/7", "/pulls/7")):
                output = json.dumps({"number": 7, "node_id": "PR_node7", "title": "Fixture", "state": "closed" if method == "PATCH" else "open",
                                     "user": {"login": "fixture"}, "head": {"sha": SHA}, "html_url": f"{BASE}/pull/7", "body": ""})
            elif method == "POST" and route.endswith("/issues"):
                output = json.dumps({"number": 7, "title": "Title", "body": "Body", "labels": [{"name": "bug"}],
                                     "user": {"login": "fixture"}, "html_url": f"{BASE}/issues/7"})
            elif route.endswith("/comments"):
                output = json.dumps({"id": 1, "body": "text", "user": {"login": "fixture"}, "created_at": "t",
                                     "html_url": f"{BASE}/issues/7#issuecomment-1", "issue_url": f"{BASE}/issues/7"})
            elif route.endswith("/reviews"):
                output = json.dumps({"id": 1, "body": "text", "state": "COMMENTED", "user": {"login": "fixture"},
                                     "submitted_at": "t", "html_url": f"{BASE}/pull/7#pullrequestreview-1"})
            else:
                output = "[]"
        elif argv[1:3] == ["pr", "view"]:
            output = json.dumps({"number": 7, "title": "Fixture", "state": "OPEN", "author": {"login": "fixture"},
                                 "headRefOid": SHA, "url": f"{BASE}/pull/7"})
        elif argv[1:3] == ["pr", "diff"]:
            output = "diff --git a/x b/x"
        elif argv[1:3] == ["run", "list"]:
            output = json.dumps([_run(11, conclusion="failure")])
        elif argv[1:3] == ["run", "view"]:
            output = json.dumps({"jobs": [_job(55, "test", conclusion="failure")]})
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


_CALLS = [
    ("github_read", {"method": "list", "kind": "issue"}, 1), ("github_read", {"method": "get", "kind": "issue", "target": 7}, 1),
    ("github_write", {"method": "comment", "target": 7, "body": "text"}, 1),
    ("github_write", {"method": "update", "target": 7, "state": "closed"}, 1),
    ("github_write", {"method": "create_issue", "title": "Title", "body": "Body", "labels": ["bug"]}, 1),
    ("github_read", {"method": "list", "kind": "pr"}, 1),
    ("github_read", {"method": "get", "kind": "pr", "target": 7, "include": ["diff"]}, 3),
    ("github_write", {"method": "review", "target": 7, "event": "COMMENT", "body": "text"}, 1),
    ("github_read", {"method": "checks", "target": 7}, 4),
]


@pytest.mark.parametrize("kind", ["system", "queued", "room"])
@pytest.mark.parametrize("name,args,count", _CALLS)
@pytest.mark.parametrize("repo", ["", "github.example/owner/selected"])
def test_every_repository_tool_keeps_target_in_all_subcalls(tmp_path, gh_calls, monkeypatch, kind, name, args, count, repo):
    ctx, expected_cwd = _context(tmp_path, kind)
    monkeypatch.setenv("GH_REPO", "unrelated/wrong-repo")
    monkeypatch.setenv("GH_HOST", "configured.example")
    entry = next(item for item in github.get_tools() if item.name == name)

    result = entry.handler(ctx, **args, repo=repo)

    assert not result.startswith("⚠️"), result
    assert len(gh_calls) == count
    assert "repo" in entry.schema["parameters"]["properties"]
    for argv, kwargs in gh_calls:
        if argv[1] == "api":
            route, hostname, fields = _api_parts(argv)
            if route == "graphql":  # Node facts follow the PR URL returned by GitHub, like list(kind='pr').
                assert argv == ["gh", "api", "graphql", "--hostname", "github.example", "--input", "-"]
                assert kwargs["cwd"] == str(ctx.repo_dir)
                assert json.loads(kwargs["input"])["variables"] == {"ids": ["PR_node7"]}
                continue
            if route.startswith("repos/owner/selected/check-runs/"):  # The checks reader's one literal path: the URL GitHub returned.
                assert argv == ["gh", "api", "repos/owner/selected/check-runs/55/annotations?per_page=100",
                                "--hostname", "github.example"]
                continue
            assert "--repo" not in argv  # gh api has no --repo: the route or the typed fields name the repository.
            if repo:
                assert route.startswith("repos/owner/selected/") and hostname == "github.example" and "{owner}" not in route
            else:
                assert route.startswith("repos/{owner}/{repo}/") and not hostname
        elif repo:
            assert argv[-2:] == ["--repo", repo]
        else:
            assert "--repo" not in argv
        assert kwargs["cwd"] == str(expected_cwd)
        if kind != "system":
            assert "GH_REPO" not in kwargs["env"]
        assert kwargs["env"]["GH_HOST"] == "configured.example"


@pytest.mark.parametrize("failure", ["note", "missing-room", "fileless", "missing-workspace", "invalid-workspace-mode"])
def test_unusable_project_never_calls_gh_on_system_repo(tmp_path, gh_calls, failure):
    ctx, project = _context(tmp_path, "queued" if "workspace" in failure else "room")
    if failure == "note":
        ctx.task_metadata = {"_project_room_note": "registry unavailable"}
    elif failure == "fileless":
        ctx.task_metadata = {}
    elif failure == "invalid-workspace-mode":
        ctx.workspace_mode = ""
    else:
        project.rmdir()

    result = github_read.run_read(ctx, method="list", kind="issue")

    assert "GH_TARGET_" in result
    assert gh_calls == []


def test_fileless_room_accepts_explicit_repo(tmp_path, gh_calls):
    ctx, _ = _context(tmp_path, "room")
    ctx.task_metadata = {}
    assert not github_read.run_read(ctx, method="get", kind="issue", target=7, repo="owner/selected").startswith("⚠️")
    assert gh_calls[0][0] == ["gh", "api", "repos/owner/selected/issues/7"]


def test_generic_hub_transport_keeps_explicit_api_contract(tmp_path, gh_calls, monkeypatch):
    ctx, _ = _context(tmp_path, "room")
    ctx.task_metadata = {"_project_room_note": "registry unavailable"}
    monkeypatch.setenv("GH_REPO", "configured/hub")
    github._gh_cmd(["api", "/repos/owner/hub/contents/catalog.json"], ctx)
    assert gh_calls[0][0] == ["gh", "api", "/repos/owner/hub/contents/catalog.json"]
    assert gh_calls[0][1]["cwd"] == str(ctx.repo_dir)
    assert gh_calls[0][1]["env"]["GH_REPO"] == "configured/hub"


@pytest.mark.parametrize("kind", ["system", "queued", "room", "fileless", "broken"])
@pytest.mark.parametrize("name,args,_count", _CALLS)
def test_public_null_target_never_selects_internal_hub_transport(tmp_path, gh_calls, monkeypatch, kind, name, args, _count):
    from ouroboros.tools.registry import ToolRegistry

    ctx, _ = _context(tmp_path, "room" if kind in {"fileless", "broken"} else kind)
    if kind == "fileless":
        ctx.task_metadata = {}
    elif kind == "broken":
        ctx.task_metadata = {"_project_room_note": "registry unavailable"}
    monkeypatch.setenv("GH_REPO", "unrelated/wrong-repo")
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_SAFETY_MODE", "off")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    result = registry.execute_result(name, {**args, "repo": None})
    assert "GH_TARGET_INVALID" in result.text
    assert (result.status, result.code) == ("error", "TOOL_ARG_ERROR")
    assert not gh_calls


@pytest.mark.parametrize("name,args,_count", _CALLS)
def test_required_target_is_an_error_then_a_valid_call_has_no_stale_failure(tmp_path, gh_calls, monkeypatch, name, args, _count):
    from ouroboros.loop_tool_execution import _typed_execution_failure
    from ouroboros.tools.registry import ToolRegistry

    ctx, _ = _context(tmp_path, "room")
    ctx.task_metadata = {}
    monkeypatch.setenv("OUROBOROS_SAFETY_MODE", "off")
    monkeypatch.setenv("OUROBOROS_CONSCIOUSNESS_SAFETY_MODE", "off")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    refused = registry.execute_result(name, args)
    assert "GH_TARGET_REQUIRED" in refused.text
    assert (refused.status, refused.code) == ("error", "TOOL_ARG_ERROR")
    assert _typed_execution_failure(True, refused)
    assert not gh_calls
    valid = registry.execute_result(name, {**args, "repo": "owner/selected"})
    assert valid.status == "ok" and not _typed_execution_failure(True, valid)
    assert len(gh_calls) == _count


@pytest.mark.parametrize("repo", [False, 0, [], {}, 1])
def test_invalid_public_target_values_cannot_be_generic_transport(tmp_path, gh_calls, repo):
    ctx, _ = _context(tmp_path, "queued")
    assert "GH_TARGET_INVALID" in github_read.run_read(ctx, method="get", kind="issue", target=7, repo=repo)
    assert not gh_calls


def test_room_registry_failure_is_visible(tmp_path, monkeypatch):
    from ouroboros import projects_registry
    from ouroboros.workspace_admission import room_chat_lens_dir

    def fail(*args, **kwargs):
        raise OSError("fixture registry failure")

    monkeypatch.setattr(projects_registry, "get_reserved_project", fail)
    directory, note = room_chat_lens_dir(tmp_path, "project-fixture")
    assert directory == ""
    assert "registry entry is unreadable" in note
    assert "OSError" in note


@pytest.mark.parametrize("token", ["GITHUB_TOKEN", "GH_TOKEN", "settings"])
def test_cli_discovery_accepts_the_execution_token_sources(tmp_path, monkeypatch, token):
    from ouroboros import config
    from ouroboros.tools.registry_guards import _builtin_tool_availability

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(config, "load_settings", lambda: {"GITHUB_TOKEN": "fixture"} if token == "settings" else {})
    if token != "settings":
        monkeypatch.setenv(token, "fixture")
    ctx, _ = _context(tmp_path, "queued")
    assert _builtin_tool_availability("github_read", ctx)[0] is True


def test_cli_store_metadata_enables_only_cli_tools_without_probing(tmp_path, monkeypatch):
    from ouroboros.tools.registry_guards import _builtin_tool_availability

    monkeypatch.setattr(github, "github_token_from_env_or_settings", lambda: "")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    (tmp_path / "gh").mkdir()
    (tmp_path / "gh" / "hosts.yml").write_text("github.com:\n  user: fixture\n", encoding="utf-8")

    def no_probe(*args, **kwargs):
        raise AssertionError("discovery must not spawn an authentication probe")

    monkeypatch.setattr(subprocess, "run", no_probe)
    ctx, _ = _context(tmp_path, "queued")
    for name, _, _ in _CALLS:
        assert _builtin_tool_availability(name, ctx)[0] is True
    assert _builtin_tool_availability("submit_skill_to_hub", ctx) == (False, "missing_credential", "GITHUB_TOKEN")
    assert _builtin_tool_availability("generate_evolution_stats", ctx) == (True, "", "")


@pytest.mark.parametrize("bound,explicit", [(False, False), (False, True), (True, True)])
def test_presence_repository_selection_keeps_host_argument_authority(tmp_path, gh_calls, bound, explicit):
    from ouroboros.presence_authority import PresenceCapabilityCeiling, PresenceToolGrant, presence_ceiling_payload
    from tests.frozen_presence_capabilities import PresenceArgumentBinding
    from ouroboros.tools.registry import ToolRegistry

    bindings = (PresenceArgumentBinding(("repo",), "static", static_value="owner/allowed"),) if bound else ()
    ceiling = PresenceCapabilityCeiling(
        skill_name="fixture", skill_content_hash="a" * 64, profile_fingerprint="b" * 64,
        state_fingerprint="c" * 64, selection_fingerprint="d" * 64, model_slot="main",
        inline_max_rounds=10, tool_grants=(PresenceToolGrant("github_read", bindings),),
        resource_grants=(), digest="e" * 64,
    )
    ctx, _ = _context(tmp_path, "system")
    ctx.task_contract = {"capability_ceiling": presence_ceiling_payload(ceiling)}
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    args = {"method": "get", "kind": "issue", "target": 7, **({"repo": "owner/unselected"} if explicit else {})}

    result = registry.execute("github_read", args)

    if explicit and not bound:
        assert "PRESENCE_ARGUMENT_BINDING_BLOCKED" in result
        assert not gh_calls
    else:
        assert result.startswith("github_read get issue #7")
        assert len(gh_calls) == 1
        if bound:
            assert gh_calls[0][0][2] == "repos/owner/allowed/issues/7"
        else:
            assert gh_calls[0][0][2] == "repos/{owner}/{repo}/issues/7"


def test_child_does_not_gain_github_access_from_explicit_repo(tmp_path, gh_calls):
    from ouroboros.tools.registry import ToolRegistry

    ctx, _ = _context(tmp_path, "queued")
    ctx.task_metadata = {"delegation_role": "subagent"}
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    result = registry.execute("github_read", {"method": "get", "kind": "issue", "target": 7, "repo": "owner/unselected"})
    assert "BLOCKED" in result
    assert not gh_calls
