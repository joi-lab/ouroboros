"""Git source authentication uses synthetic credentials and never contacts a remote."""

import base64
import json
import logging
import traceback

import pytest

from ouroboros import skill_source_git as transport

TOKEN = "synthetic-github-source-token"
ENCODED = base64.b64encode(f"x-access-token:{TOKEN}".encode("utf-8")).decode("ascii")
AUTH_REFUSAL = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    import ouroboros.config as config

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Synthetic test")
    return settings


@pytest.mark.parametrize("token_source", ["env", "settings", "none"])
@pytest.mark.parametrize("url,authenticated", [
    ("https://github.com/alice/ouroboros-skills", True),
    ("https://GitHub.com/alice/skills.git", True),
    ("http://github.com/alice/skills", False),
    ("https://github.com.example.invalid/alice/skills", False),
    ("https://git.example.invalid/alice/skills", False),
    ("ssh://git@github.com/alice/skills", False),
    ("git@github.com:alice/skills.git", False),
    ("file:///tmp/synthetic-skills.git", False),
])
def test_fetch_credentials_are_scoped_to_github_https(credentials, tmp_path, monkeypatch, token_source,
                                                     url, authenticated):
    if token_source == "env":
        monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    elif token_source == "settings":
        credentials.write_text(json.dumps({"GITHUB_TOKEN": TOKEN}), encoding="utf-8")
    calls = []

    def run(argv, *, cwd, env, **kwargs):
        calls.append((argv, env))
        if argv[1] == "init":
            (cwd / ".git").mkdir()
        if argv[1] == "fetch" and authenticated and token_source != "none" and env["GIT_CONFIG_COUNT"] == "1":
            return 128, "", AUTH_REFUSAL
        return 0, "a" * 40, ""

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    target = tmp_path / "clone"
    assert transport.fetch_tree(target, url, "main") == "a" * 40
    assert transport.fetch_tree(target, url, "main") == "a" * 40  # refresh an existing clone too
    transport.fetch_signals(target, url=url)
    fetches = [env for argv, env in calls if argv[1] == "fetch"]
    retry = authenticated and token_source != "none"
    assert len(fetches) == (6 if retry else 3)
    for i, env in enumerate(fetches):
        assert env["GIT_CONFIG_KEY_0"] == "user.name" and env["GIT_CONFIG_VALUE_0"] == "Synthetic test"
        config = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
                  for i in range(int(env["GIT_CONFIG_COUNT"]))}
        headers = {key: value for key, value in config.items() if key.lower().endswith(".extraheader")}
        assert headers == ({"http.https://github.com/.extraheader": f"AUTHORIZATION: basic {ENCODED}"}
                           if retry and i % 2 else {})
        assert config.get("credential.helper") == ("" if retry and i % 2 else None)
        assert env["GIT_TERMINAL_PROMPT"] == "0"
    for argv, _env in calls:
        assert TOKEN not in repr(argv) and ENCODED not in repr(argv)
    assert not any(path.is_file() for path in target.rglob("*")), "credentials must not be persisted"


def test_stale_token_does_not_break_public_reads(credentials, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    fetches = []

    def run(argv, *, cwd, env, **kwargs):
        if argv[1] == "init":
            (cwd / ".git").mkdir()
        if argv[1] == "fetch":
            fetches.append(env)
            if env["GIT_CONFIG_COUNT"] != "1":
                return 128, "", AUTH_REFUSAL  # a stale token breaks an otherwise public read
        return 0, "a" * 40, ""

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    row = {"id": "public", "url": "https://github.com/example/public"}
    for _ in range(2):  # clone and refresh
        assert transport.update_clone(tmp_path, row)["error"] == ""
    assert transport.checkout_at_commit(tmp_path, row["url"], "a" * 40).is_dir()
    assert transport.fetch_signals(transport.clone_dir(tmp_path, row), url=row["url"]) == b"a" * 40
    assert len(fetches) == 4 and all(env["GIT_CONFIG_COUNT"] == "1" for env in fetches)


@pytest.mark.parametrize("token", ["", TOKEN])
@pytest.mark.parametrize("failure,authentication", [
    (AUTH_REFUSAL, True),
    ("fatal: Authentication failed for 'https://github.com/example/private/'", True),
    ("fatal: unable to access repository: The requested URL returned error: 401", True),
    ("remote: Repository not found.", True),
    ("fatal: Could not resolve host: github.com", False),
    ("fatal: couldn't find remote ref signals", False),
    ("error: unable to write file: No space left on device", False),
])
def test_only_authentication_refusals_retry_once_with_a_configured_token(
        credentials, tmp_path, monkeypatch, token, failure, authentication):
    monkeypatch.setenv("GITHUB_TOKEN", token)
    attempts = []

    def run(argv, *, env, **kwargs):
        if argv[1] == "fetch":
            attempts.append(env["GIT_CONFIG_COUNT"])
            return 128, "", failure if len(attempts) == 1 else "remote: Invalid username or token."
        return 0, "", ""

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    with pytest.raises(transport.SkillSourceFetchError) as exc:
        transport.fetch_tree(tmp_path / "clone", "https://github.com/example/private", "main")
    retry = bool(token) and authentication
    assert attempts == (["1", "3"] if retry else ["1"])
    assert ("Retrying with the configured GitHub token failed too" in str(exc.value)) == retry
    assert failure in str(exc.value)  # the read without the token, a mistyped or deleted repository included
    assert ("Invalid username or token" in str(exc.value)) == retry


def test_token_retry_never_asks_this_computers_credential_helpers(credentials, tmp_path, monkeypatch):
    """A refused token must not make git erase a login a credential helper stored; the
    anonymous read still uses the helpers (private repositories this computer can read)."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    calls = tmp_path / "helper-calls.txt"
    helper = tmp_path / "helper.sh"
    # LF bytes and forward slashes: Windows text mode would end the script's lines with CR for its
    # sh, and a git config value reads a backslash as an escape.
    helper.write_bytes(f'#!/bin/sh\necho "$1" >> "{calls.as_posix()}"\n'
                       'if [ "$1" = get ]; then echo username=stored; echo password=stored-login; fi\n'.encode())
    helper.chmod(0o755)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_bytes(f'[credential]\n\thelper = {helper.as_posix()}\n'
                          f'[credential "https://github.com"]\n\thelper = {helper.as_posix()}\n'.encode())
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))  # never this computer's real helpers
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_ASKPASS", "SSH_ASKPASS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    envs = []

    def run(argv, *, env, **kwargs):
        if argv[1] == "fetch":
            envs.append(env)
            return 128, "", AUTH_REFUSAL
        return 0, "", ""

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    with pytest.raises(transport.SkillSourceFetchError):
        transport.fetch_tree(tmp_path / "clone", "https://github.com/example/private", "main")
    anonymous, retry = envs
    request = "protocol=https\nhost=github.com\npath=example/private\n"

    def credential(action, env, extra=""):
        return subprocess.run([git, "credential", action], input=request + extra + "\n", env=env,
                              cwd=tmp_path, capture_output=True, text=True, timeout=30)

    assert "username=stored" in credential("fill", anonymous).stdout
    credential("reject", anonymous, "username=stored\npassword=stored-login\n")
    assert set(calls.read_text(encoding="utf-8").split()) == {"get", "erase"}
    calls.unlink()
    assert credential("fill", retry).returncode != 0  # nothing to fill, and no prompt
    credential("reject", retry, "username=stored\npassword=stored-login\n")
    assert not calls.exists()


@pytest.mark.parametrize("phase", ["anonymous", "authenticated"])
@pytest.mark.parametrize("failure", ["stderr", "stdout", "exception"])
def test_fetch_errors_redact_credentials_from_messages_logs_and_cache(
        credentials, tmp_path, monkeypatch, caplog, failure, phase):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    message = f"synthetic failure: {TOKEN} AUTHORIZATION: basic {ENCODED}"

    def run(argv, **kwargs):
        if argv[1] != "fetch":
            return 0, "", ""
        if phase == "authenticated" and kwargs["env"]["GIT_CONFIG_COUNT"] == "1":
            return 128, "", AUTH_REFUSAL
        if failure == "exception":
            raise RuntimeError(message)
        return (1, message, "") if failure == "stdout" else (1, "", message)

    monkeypatch.setattr("supervisor.git_ops._run_git_process_bounded", run)
    row = {"id": "mine", "url": "https://github.com/alice/ouroboros-skills"}
    with pytest.raises(transport.SkillSourceFetchError):
        try:
            transport.fetch_tree(tmp_path / "direct", row["url"], "")
        except Exception:
            trace = traceback.format_exc()
            assert TOKEN not in trace and ENCODED not in trace
            raise
    with caplog.at_level(logging.INFO, logger=transport.__name__):
        meta = transport.update_clone(tmp_path / "data", row)
    assert "synthetic failure" in meta["error"]
    assert ("Retrying with the configured GitHub token failed too" in meta["error"]) == (phase == "authenticated")
    rendered = json.dumps(meta) + caplog.text + (transport.repo_cache_dir(tmp_path / "data", row) / "meta.json").read_text(encoding="utf-8")
    assert TOKEN not in rendered and ENCODED not in rendered
