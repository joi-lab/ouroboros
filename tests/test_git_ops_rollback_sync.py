import supervisor.git_ops as git_ops


def _fake_state():
    return {"current_sha": "oldsha", "current_branch": "ouroboros"}


def test_rollback_to_version_force_syncs_remote_when_diverged(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_collect_repo_sync_state", lambda: {"current_branch": "ouroboros"})
    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", lambda **kwargs: {})
    monkeypatch.setattr(git_ops, "preserve_local_ref_branch", lambda *args, **kwargs: (True, "rollback-keep-test"))
    monkeypatch.setattr(git_ops, "load_state", _fake_state)
    saved = {}
    monkeypatch.setattr(git_ops, "save_state", lambda st: saved.update(st))
    monkeypatch.setattr(git_ops, "update_state", lambda mutator, **_kw: mutator(saved) or saved)
    events = []
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: events.append(payload))
    monkeypatch.setattr(git_ops, "_has_remote", lambda *_args, **_kwargs: True)

    calls = []
    network_calls = []

    def fake_git_capture(cmd):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "targetsha^{commit}"]:
            return 0, "abc123def456", ""
        if cmd == ["git", "reset", "--hard", "abc123def456"]:
            return 0, "", ""
        if cmd == ["git", "rev-list", "--left-right", "--count", "HEAD...origin/ouroboros"]:
            return 0, "1 1", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)
    monkeypatch.setattr(
        git_ops,
        "_git_network_bounded",
        lambda cmd: (network_calls.append(cmd) or (0, "", "")),
    )

    ok, message = git_ops.rollback_to_version("targetsha", reason="test")

    assert ok is True
    assert "Rolled back to targetsha (abc123de)" in message
    assert "Remote not synced" not in message
    assert saved["current_sha"] == "abc123def456"
    assert network_calls == [["push", "--force-with-lease", "origin", "ouroboros"]]
    assert events and events[-1]["remote_synced"] is True


def test_rollback_to_version_returns_warning_when_remote_sync_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_collect_repo_sync_state", lambda: {"current_branch": "ouroboros"})
    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", lambda **kwargs: {})
    monkeypatch.setattr(git_ops, "preserve_local_ref_branch", lambda *args, **kwargs: (True, "rollback-keep-test"))
    monkeypatch.setattr(git_ops, "load_state", _fake_state)
    saved = {}
    monkeypatch.setattr(git_ops, "save_state", lambda st: saved.update(st))
    monkeypatch.setattr(git_ops, "update_state", lambda mutator, **_kw: mutator(saved) or saved)
    events = []
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: events.append(payload))
    monkeypatch.setattr(git_ops, "_has_remote", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        git_ops,
        "_git_network_bounded",
        lambda cmd: (1, "", "rejected"),
    )

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-parse", "--verify", "targetsha^{commit}"]:
            return 0, "abc123def456", ""
        if cmd == ["git", "reset", "--hard", "abc123def456"]:
            return 0, "", ""
        if cmd == ["git", "rev-list", "--left-right", "--count", "HEAD...origin/ouroboros"]:
            return 0, "2 0", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    ok, message = git_ops.rollback_to_version("targetsha", reason="test")

    assert ok is True
    assert "⚠️ Remote not synced: rejected" in message
    assert saved["current_sha"] == "abc123def456"
    assert events and events[-1]["remote_synced"] is False


def test_rollback_to_version_skips_remote_sync_without_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_collect_repo_sync_state", lambda: {"current_branch": "ouroboros"})
    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", lambda **kwargs: {})
    monkeypatch.setattr(git_ops, "preserve_local_ref_branch", lambda *args, **kwargs: (True, "rollback-keep-test"))
    monkeypatch.setattr(git_ops, "load_state", _fake_state)
    saved = {}
    monkeypatch.setattr(git_ops, "save_state", lambda st: saved.update(st))
    monkeypatch.setattr(git_ops, "update_state", lambda mutator, **_kw: mutator(saved) or saved)
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: None)
    monkeypatch.setattr(git_ops, "_has_remote", lambda *_args, **_kwargs: False)

    calls = []

    def fake_git_capture(cmd):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "targetsha^{commit}"]:
            return 0, "abc123def456", ""
        if cmd == ["git", "reset", "--hard", "abc123def456"]:
            return 0, "", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    ok, message = git_ops.rollback_to_version("targetsha", reason="test")

    assert ok is True
    assert "Remote not synced" not in message
    assert all(cmd[:2] != ["git", "push"] for cmd in calls)


def test_rollback_to_version_skips_remote_sync_for_unknown_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_collect_repo_sync_state", lambda: {"current_branch": "unknown"})
    monkeypatch.setattr(git_ops, "_create_rescue_snapshot", lambda **kwargs: {})
    monkeypatch.setattr(git_ops, "preserve_local_ref_branch", lambda *args, **kwargs: (True, "rollback-keep-test"))
    monkeypatch.setattr(git_ops, "load_state", _fake_state)
    monkeypatch.setattr(git_ops, "save_state", lambda st: None)
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: None)
    monkeypatch.setattr(git_ops, "_has_remote", lambda *_args, **_kwargs: True)

    calls = []

    def fake_git_capture(cmd):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "targetsha^{commit}"]:
            return 0, "abc123def456", ""
        if cmd == ["git", "reset", "--hard", "abc123def456"]:
            return 0, "", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    ok, message = git_ops.rollback_to_version("targetsha", reason="test")

    assert ok is True
    assert "Remote not synced" not in message
    assert all("origin/unknown" not in " ".join(cmd) for cmd in calls)
    assert all(cmd[:2] != ["git", "push"] for cmd in calls)
