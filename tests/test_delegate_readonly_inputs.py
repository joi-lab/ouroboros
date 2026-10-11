"""Readonly session inputs preserve native read policy at the actual start seam."""
import copy
import hashlib
import json
import pathlib

from tests.test_delegated_skill_payload import _payload_ctx, _StartStub


def test_readonly_source_permissions_do_not_prevent_exact_session_input(tmp_path, monkeypatch):
    from ouroboros import claudexor_daemon, safety
    from ouroboros.tools.registry import ToolRegistry

    ctx = _payload_ctx(tmp_path, monkeypatch)
    ctx.task_metadata.update(resource_intent={"kind": "explicit_none"}, parent_task_id="parent")
    parent = tmp_path / "data/task_drives/parent"
    parent.mkdir(parents=True)
    source = parent / "notes.txt"
    token = "ghp_" + "a" * 32
    source.write_text("Parent input with " + token, encoding="utf-8")
    source.chmod(0o444)
    binary = parent / "fixture.bin"
    binary.write_bytes(b"\xff\x00" + token.encode() + b"\r\n")
    owner_secret = ctx.drive_root / "settings.json"
    owner_secret.write_text('{"owner": "control"}', encoding="utf-8")
    secret_alias = parent / "ordinary.txt"
    secret_alias.hardlink_to(owner_secret)
    protected = parent / "oracle.txt"
    protected.write_text("black box bytes", encoding="utf-8")
    ctx.task_contract = {"resource_policy": {"protected_artifacts": [{
        "id": "oracle", "role": "black_box_reference", "paths": [str(protected)],
    }]}}
    native = copy.copy(ctx)
    native.task_constraint = {"mode": "local_readonly_subagent"}
    reader = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    reader.set_context(native)
    monkeypatch.setattr(safety, "check_safety", lambda *_a, **_k: (True, ""))
    readable = reader.execute("read_file", {"root": "task_drive", "path": str(source)})
    assert "Parent input with " + token in readable
    assert "control" in reader.execute("read_file", {"root": "task_drive", "path": str(secret_alias)})
    assert "BLOCKED" in reader.execute("read_file", {"root": "task_drive", "path": str(protected)})
    seen = {}

    class Gateway(_StartStub):
        def start_run(self, request, **kwargs):
            scope = pathlib.Path(request["scope"]["root"])
            manifest_text = (scope / "inputs.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            sources = {row["source"] for row in manifest}
            assert str(secret_alias) in sources and str(protected) not in sources
            [note] = [row for row in manifest if row["source"] == str(source)]
            delivered = (scope / note["local"]).read_bytes()
            assert delivered == source.read_bytes()
            assert "masked_spans" not in note
            assert note["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
            assert note["sha256"] == hashlib.sha256(delivered).hexdigest()
            [binary_row] = [row for row in manifest if row["source"] == str(binary)]
            assert (scope / binary_row["local"]).read_bytes() == binary.read_bytes()
            assert binary_row["source_sha256"] == binary_row["sha256"]
            return super().start_run(request, **kwargs)

    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", lambda: Gateway(seen))
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    try:
        result = registry.execute_result("delegate_start", {
            "subagent_id": "payload-session", "access": "readonly", "prompt": "Read the parent notes.",
        })
        assert result.status == "ok", result.text
        assert json.loads(result.text).get("run_id") == "run-p1", result.text
        assert source.read_text(encoding="utf-8") == "Parent input with " + token
    finally:
        source.chmod(0o644)


def test_input_copy_failure_is_typed_unrun_without_pending_engine_custody(tmp_path, monkeypatch):
    from ouroboros import artifacts, claudexor_daemon, delegate_custody, safety
    from ouroboros.tools.registry import ToolRegistry

    ctx = _payload_ctx(tmp_path, monkeypatch)
    ctx.task_metadata["resource_intent"] = {"kind": "explicit_none"}
    (ctx.active_repo_dir() / "notes.txt").write_text("permitted input", encoding="utf-8")
    seen = {}
    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", lambda: _StartStub(seen))
    monkeypatch.setattr(safety, "check_safety", lambda *_a, **_k: (True, ""))

    def unavailable(*args, **kwargs):
        raise OSError("input verification failed")

    monkeypatch.setattr(artifacts, "stream_artifact_file", unavailable)
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    result = json.loads(registry.execute("delegate_start", {
        "subagent_id": "payload-session", "access": "readonly", "prompt": "Read notes.",
    }))
    assert result["reason"] == "readonly_inputs_unavailable" and result["definitely_unrun"] is True
    assert "request" not in seen and not delegate_custody.pending_invocations(ctx.drive_root)


def test_unknown_start_replays_same_exact_inputs_and_durable_invocation(tmp_path, monkeypatch):
    from ouroboros import claudexor_daemon, delegate_custody, safety
    from ouroboros.gateways.claudexor import ClaudexorUnavailable
    from ouroboros.tools.registry import ToolRegistry

    ctx = _payload_ctx(tmp_path, monkeypatch)
    ctx.task_metadata["resource_intent"] = {"kind": "explicit_none"}
    source = ctx.active_repo_dir() / "notes.txt"
    source.write_text("original input", encoding="utf-8")
    requests = []

    class Gateway(_StartStub):
        def start_run(self, request, **kwargs):
            requests.append((request, kwargs))
            if len(requests) == 1:
                raise ClaudexorUnavailable("daemon_unreachable", "start response lost")
            scope = pathlib.Path(request["scope"]["root"])
            [note] = [row for row in json.loads((scope / "inputs.json").read_text(encoding="utf-8"))
                      if row["source"] == str(source)]
            assert (scope / note["local"]).read_text(encoding="utf-8") == "original input"
            return super().start_run(request, **kwargs)

    monkeypatch.setattr(claudexor_daemon, "ensure_owned_gateway", lambda: Gateway({}))
    monkeypatch.setattr(safety, "check_safety", lambda *_a, **_k: (True, ""))
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry.set_context(ctx)
    result = json.loads(registry.execute("delegate_start", {
        "subagent_id": "payload-session", "access": "readonly", "prompt": "Read notes.",
    }))
    invocation = result["pending_invocation_id"]
    source.write_text("later parent revision", encoding="utf-8")
    result = json.loads(registry.execute("delegate_start", {"retry_of": invocation, "prompt": "Read notes."}))
    assert result["run_id"] == "run-p1" and result["idempotent_recovery"] is True
    assert requests[0] == requests[1]
    assert delegate_custody.replay(ctx.drive_root)["run-p1"].invocation_id == invocation
    assert not delegate_custody.pending_invocations(ctx.drive_root)
