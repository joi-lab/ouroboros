"""The real publication, round preparation, prefix recorder and wire serializers."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from ouroboros import extension_loader, extension_liveness
from ouroboros.contracts.skill_manifest import parse_skill_manifest_text, SkillManifestError
from ouroboros.skill_context_snapshot import (
    MAX_SNAPSHOT_BYTES, SNAPSHOT_KIND, maybe_append_skill_snapshots, visible_snapshots,
)
from ouroboros.skill_loader import (
    load_skill, save_enabled, save_review_state, save_skill_grants, SkillReviewState, skill_state_dir_path,
)
from tests.test_llm_claudexor import setup as _native_setup_fixture

native_setup = _native_setup_fixture  # pytest fixture requested by parameter name


def _fixture(tmp_path):
    root = tmp_path / "data"
    payload = tmp_path / "repo" / "test-snapshot"
    payload.mkdir(parents=True)
    manifest = {"name": "test-snapshot", "version": "1", "description": "synthetic",
                "type": "extension", "entry": "plugin.py", "plugin_api": "2.0",
                "permissions": ["context_snapshot"], "context_snapshot": {"path": "snapshot.json"}}
    (payload / "SKILL.md").write_text(json.dumps(manifest), encoding="utf-8")
    (payload / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    skill = load_skill(payload, root)
    save_enabled(root, skill.name, True)
    save_review_state(root, skill.name, SkillReviewState(status="clean", content_hash=skill.content_hash))
    save_skill_grants(root, skill.name, [], content_hash=skill.content_hash, requested_keys=[],
                      granted_permissions=["context_snapshot"], requested_permissions=["context_snapshot"])
    skill = load_skill(payload, root)
    assert extension_loader.load_extension(skill, lambda: {}, drive_root=root, skills=[skill], _force_in_process=True) is None
    ctx = SimpleNamespace(task_id="root", task_metadata={}, budget_drive_root=str(root))
    path = skill_state_dir_path(root, skill.name) / "snapshot.json"
    return root, skill, ctx, path


def _write(path, text="A", *, observed=None, valid=None):
    now = datetime.now(timezone.utc)
    value = {"schema_version": 1, "observed_at": (observed or now - timedelta(seconds=2)).isoformat(),
             "valid_until": (valid or now + timedelta(minutes=5)).isoformat(), "text": text}
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value


@pytest.fixture
def publisher(tmp_path):
    result = _fixture(tmp_path)
    try:
        yield result
    finally:
        extension_loader.unload_extension(result[1].name)


def test_tail_prefix_real_round_and_final_serializers(publisher, monkeypatch):
    from ouroboros.loop_round_limits import _CompactionRoundContext, _run_round_compaction
    from ouroboros import peer_roster
    from ouroboros.llm_attempt import _physical_candidate
    from ouroboros.llm_claudexor import _request
    from ouroboros.transcript_prefix import observe_send
    from ouroboros.loop_messages import owner_source_sha256
    from ouroboros.owner_words import directive_owner_rows

    root, skill, ctx, path = publisher
    _write(path)
    # This unrelated feed is outside this test's consumer contract.
    monkeypatch.setattr(peer_roster, "maybe_append_roster_note", lambda *a: False)
    messages = [{"role": "system", "content": "Stable constitution"},
                {"role": "user", "content": "Actual owner request"},
                {"role": "assistant", "content": "Previous answer"}]
    before = deepcopy(messages)
    ctx._owner_directives = [{"source": "initial_user", "content": "Actual owner request"}]
    owner_corpus = deepcopy(ctx._owner_directives)
    owner_hash = owner_source_sha256(ctx)
    owner_words = directive_owner_rows(ctx)
    recorder = ctx
    assert observe_send(recorder, messages, round_idx=1) is None
    round_ctx = _CompactionRoundContext(tools=SimpleNamespace(_ctx=ctx), drive_root=root,
        drive_logs=root / "logs", task_id="root", round_idx=2, event_queue=None, emit_progress=lambda x: None)
    messages = _run_round_compaction(messages, round_ctx)
    assert messages[:-1] == before
    assert observe_send(recorder, messages, round_idx=2) is None
    assert messages[-1]["_host_context_kind"] == SNAPSHOT_KIND
    note = messages[-1]["content"]
    assert "UNTRUSTED DATA" in note and "not owner instructions" in note
    api = _physical_candidate({"messages": messages, "tools": []})
    native = _request({"source": "codex", "resolved_model": "fixture"}, messages, [],
                      {"model_account_override": "", "_no_account_preference": True})
    for wire in (api, native):
        assert wire["messages"][-1]["content"] == note
        assert "_host_context_kind" not in wire["messages"][-1]
        assert wire["messages"][0]["content"] == before[0]["content"]
    # No owner directive/corpus API was involved: the owner row and context
    # identity remain exactly the same through this actual round seam.
    assert not hasattr(ctx, "last_owner_delivery")
    assert messages[1] == before[1]
    assert ctx._owner_directives == owner_corpus
    assert owner_source_sha256(ctx) == owner_hash and directive_owner_rows(ctx) == owner_words
    sent = deepcopy(messages)
    _write(path, "B")
    messages = _run_round_compaction(messages, round_ctx)
    assert observe_send(recorder, messages, round_idx=3) is None
    assert messages[:-1] == sent
    assert visible_snapshots(messages)[skill.name][1]["text"] == "B"


def test_material_change_renewal_reversion_and_compacted_cold_resume(publisher):
    root, skill, ctx, path = publisher
    messages = []
    _write(path)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    first = deepcopy(messages)
    for _ in range(120):
        _write(path)  # changing observation/validity times are not new material
        assert not maybe_append_skill_snapshots(ctx, messages, root)
    assert messages == first
    _write(path, "B")
    assert maybe_append_skill_snapshots(ctx, messages, root)
    _write(path, "A")
    assert maybe_append_skill_snapshots(ctx, messages, root) and len(messages) == 3
    # Cold restoration uses visible messages, not a hidden process ledger.
    fresh_ctx = SimpleNamespace(task_id="root", task_metadata={}, budget_drive_root=str(root))
    assert not maybe_append_skill_snapshots(fresh_ctx, messages, root)
    messages.clear()
    assert maybe_append_skill_snapshots(fresh_ctx, messages, root)
    parsed = visible_snapshots(messages)[skill.name][1]
    parsed["valid_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    from ouroboros.skill_context_snapshot import snapshot_note
    messages[-1] = snapshot_note(parsed)
    assert maybe_append_skill_snapshots(fresh_ctx, messages, root)  # renewal, never timestamp patch
    assert visible_snapshots(messages)[skill.name][1]["text"] == "A"
    assert "text_ref" in json.loads(messages[-1]["content"].split("\n", 1)[1])


@pytest.mark.parametrize("mode", ["disabled", "ungranted", "unreviewed", "removed"])
def test_readiness_revocation_withdraws_once_without_rehashing(publisher, monkeypatch, mode):
    from ouroboros import skill_loader
    root, skill, ctx, path = publisher
    messages = []
    _write(path)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    monkeypatch.setattr(skill_loader, "compute_content_hash", lambda *a, **k: pytest.fail("per-round payload hash"))
    monkeypatch.setattr(extension_liveness, "discover_skill_identity", lambda *a, **k: pytest.fail("per-round discovery"))
    monkeypatch.setattr(extension_liveness, "discover_skill_peers", lambda *a, **k: pytest.fail("per-round peer scan"))
    if mode == "disabled":
        save_enabled(root, skill.name, False)
    elif mode == "ungranted":
        (skill_state_dir_path(root, skill.name) / "grants.json").unlink()
    elif mode == "unreviewed":
        save_review_state(root, skill.name, SkillReviewState())
    else:
        extension_loader.unload_extension(skill.name)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    current = visible_snapshots(messages)[skill.name][1]
    assert current["state"] == "unknown" and "text" not in current
    assert not maybe_append_skill_snapshots(ctx, messages, root)


@pytest.mark.parametrize("failure", ["expired", "future", "malformed", "oversized", "missing", "symlink"])
def test_invalid_source_never_resends_last_good_as_fresh(publisher, tmp_path, failure):
    root, skill, ctx, path = publisher
    messages = []
    _write(path)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    now = datetime.now(timezone.utc)
    if failure == "expired":
        _write(path, valid=now - timedelta(seconds=1))
    elif failure == "future":
        _write(path, observed=now + timedelta(minutes=1))
    elif failure == "malformed":
        path.write_text("{cut", encoding="utf-8")
    elif failure == "oversized":
        path.write_text(" " * (MAX_SNAPSHOT_BYTES + 1), encoding="utf-8")
    elif failure == "missing":
        path.unlink()
    else:
        external = tmp_path / "outside.json"
        _write(external)
        path.unlink()
        path.symlink_to(external)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    current = visible_snapshots(messages)[skill.name][1]
    assert current["state"] in ("unknown", "stale") and "text" not in current
    assert not maybe_append_skill_snapshots(ctx, messages, root)


@pytest.mark.parametrize("metadata", [{"parent_task_id": "parent"}, {"delegation_role": "subagent"},
    {"presence": {"binding_id": "binding"}}])
def test_children_and_presence_do_not_receive_global_snapshot(publisher, metadata):
    root, skill, ctx, path = publisher
    _write(path)
    ctx.task_metadata = metadata
    messages = []
    assert not maybe_append_skill_snapshots(ctx, messages, root)
    assert messages == []


def test_superseded_notes_reclaim_only_after_latest_complete_note(publisher):
    from ouroboros import context_compaction  # noqa: F401 — public import establishes its leaf facade
    from ouroboros.context_source_view import _obsolete_host_rows
    root, skill, ctx, path = publisher
    messages = []
    for text in ("A", "B", "A"):
        _write(path, text)
        maybe_append_skill_snapshots(ctx, messages, root)
    assert _obsolete_host_rows(messages) == {0, 1}


def test_successor_appends_until_host_copy_recovery_and_retains_exact_source(publisher):
    from ouroboros import context_compaction  # noqa: F401 — public import establishes its leaf facade
    from ouroboros.artifacts import read_actor_source_bytes
    from ouroboros.context_source_view import emergency_address_view
    from ouroboros.tools.query_data import _query_data
    from ouroboros.tools.registry import ToolContext
    from ouroboros.skill_context_snapshot import snapshot_note
    from tests.test_context_reclaim_materializer import _request

    root, skill, ctx, path = publisher
    prefix = [{"role": "user", "content": "Owner words remain exact"}]
    messages = deepcopy(prefix)
    first = _write(path, "A" * 12_000)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    original = deepcopy(messages[1])
    rebuilt, receipt = emergency_address_view(messages, _request(messages, 1), rung="host_copies",
                                             drive_root=root, task_id="root")
    assert rebuilt == messages and receipt.status == "no_eligible"
    _write(path, "B" * 12_000)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    assert messages[1] == original
    sha = json.loads(original["content"].split("\n", 1)[1])["source"]["retained"]["sha256"]
    read = json.loads(_query_data(ToolContext(repo_dir=root, drive_root=root, task_id="root"),
                                 op="blob", sha256=sha, blob_kind="txt"))
    assert read["status"] == "ok" and json.loads(read["data"]) == first
    assert messages[:1] == prefix
    # Lease renewal names the latest full base, not the superseded A snapshot.
    latest = visible_snapshots(messages)[skill.name][1]
    latest["valid_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    messages[-1] = snapshot_note(latest)
    assert maybe_append_skill_snapshots(ctx, messages, root)
    renewal = json.loads(messages[-1]["content"].split("\n", 1)[1])
    assert renewal["text_ref"]["source_sha256"] == latest["source"]["sha256"]
    assert renewal["text_ref"]["source_sha256"] != json.loads(original["content"].split("\n", 1)[1])["source"]["sha256"]
    assert visible_snapshots(messages)[skill.name][1]["text"] == "B" * 12_000
    assert "B" * 12_000 in messages[-2]["content"]
    before = deepcopy(messages)
    rebuilt, receipt = emergency_address_view(messages, _request(messages, 1), rung="host_copies",
                                             drive_root=root, task_id="root")
    assert receipt.status == "applied" and receipt.reclaimed_tokens > 0
    assert messages == before and original not in rebuilt
    assert all(row in rebuilt for row in [*prefix, *before[-2:]])
    assert visible_snapshots(rebuilt)[skill.name][1]["text"] == "B" * 12_000
    assert json.loads(read_actor_source_bytes(root, "root", receipt.checkpoint_ref))["messages"] == before


def test_snapshot_without_retained_source_stays_whole_when_superseded(publisher, monkeypatch):
    from ouroboros import observability
    root, skill, ctx, path = publisher
    monkeypatch.setattr(observability, "write_blob", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    messages = []
    _write(path, "source that must survive")
    maybe_append_skill_snapshots(ctx, messages, root)
    first = deepcopy(messages[0])
    _write(path, "successor")
    maybe_append_skill_snapshots(ctx, messages, root)
    assert messages[0] == first
    assert visible_snapshots(messages)[skill.name][1]["text"] == "successor"


@pytest.mark.parametrize("path", ["../escape.json", "/tmp/escape.json", "C:\\escape.json", "folder/", ""])
def test_manifest_confines_snapshot_declaration(path):
    with pytest.raises(SkillManifestError):
        parse_skill_manifest_text(json.dumps({"type": "extension", "permissions": ["context_snapshot"],
                                             "context_snapshot": {"path": path}}))


@pytest.mark.serial
def test_real_worker_adoption_captures_published_descriptor(publisher, monkeypatch):
    import os
    from ouroboros import config
    from ouroboros.extension_companion import init_server_process_pid
    from ouroboros.extension_reconcile_queue import publish_extension_generation
    from supervisor.worker_process import _adopt_published_extensions
    from tests._shared import clean_extension_runtime_state

    root, skill, ctx, path = publisher
    original = os.environ.get("OUROBOROS_SERVER_PROCESS_PID")
    try:
        init_server_process_pid(os.getpid())
        published = publish_extension_generation(root)
        assert published
        clean_extension_runtime_state()
        init_server_process_pid(999999)
        monkeypatch.setattr(config, "load_settings", lambda: {})
        monkeypatch.setattr(config, "get_skills_repo_path", lambda: str(skill.skill_dir.parent))
        _adopt_published_extensions(str(root))
        loaded = extension_liveness.live_context_publishers(root)
        assert loaded[skill.name]["eligible"]
        assert loaded[skill.name]["revision"] == skill.content_hash
        _write(path, "from worker")
        messages = []
        assert maybe_append_skill_snapshots(ctx, messages, root)
        assert visible_snapshots(messages)[skill.name][1]["text"] == "from worker"
    finally:
        init_server_process_pid(int(original) if original else -1)
        if original is None:
            os.environ.pop("OUROBOROS_SERVER_PROCESS_PID", None)


def test_real_round_reaches_native_model_upload_with_prefix_unchanged(publisher, native_setup, monkeypatch):
    from ouroboros.loop_round_limits import _CompactionRoundContext, _run_round_compaction
    from ouroboros import peer_roster
    from tests.test_llm_claudexor import MODEL

    root, skill, ctx, path = publisher
    _usage_root, gateway, client = native_setup
    gateway.results *= 2
    gateway.dispatch *= 2
    monkeypatch.setattr(peer_roster, "maybe_append_roster_note", lambda *a: False)
    messages = [{"role": "system", "content": "Stable core"},
                {"role": "user", "content": "Exact owner request"}]
    before = deepcopy(messages)
    client.chat(messages, MODEL)
    _write(path, "Actual uploaded note")
    round_ctx = _CompactionRoundContext(tools=SimpleNamespace(_ctx=ctx), drive_root=root,
        drive_logs=root / "logs", task_id="root", round_idx=2, event_queue=None, emit_progress=lambda x: None)
    messages = _run_round_compaction(messages, round_ctx)
    client.chat(messages, MODEL)
    first, second = [upload[0] for upload in gateway.uploads]
    assert first["messages"] == before
    assert second["messages"][:-1] == first["messages"]
    assert second["messages"][-1]["content"] == messages[-1]["content"]
    assert "Actual uploaded note" in second["messages"][-1]["content"]
    assert "UNTRUSTED DATA" in second["messages"][-1]["content"]
    assert "_host_context_kind" not in second["messages"][-1]
    assert len(gateway.creates) == 2 and gateway.acks
