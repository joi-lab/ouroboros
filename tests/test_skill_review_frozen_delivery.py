"""A retrieving skill reviewer reads the frozen payload; the whole payload is pasted only within its hint.

Every retrieving seat — a native inspection episode or an agent session — reviews
one retained copy bound to the loader content hash. Its first send is measured the
way it is dispatched (the native opening with its manifest reference, JSON escaping
and tool schemas); the payload is pasted only below the seat's initial inline sizing
hint (never a send limit), otherwise the seat gets the index of the copy and reads the
files itself. Packet seats keep their packet chunks; a retrieving seat answers once for
the whole payload; collection and quorum never turn missing evidence into PASS. These
tests drive the real pass runner, the real native executor, the real session coverage
fold and the real collector.
"""

import copy
import json
import os
import pathlib
from types import SimpleNamespace

import pytest

from ouroboros import review_native_episode
from ouroboros.review_execution import ReviewAssignment, ReviewRouteKind
from ouroboros.review_native_episode import NativeToolRoundReviewExecutor
from ouroboros.review_substrate import ReviewRequest, ReviewSlot
from ouroboros.skill_loader import compute_content_hash
from ouroboros.skill_review_passes import (
    _reserve_skill_wave,
    run_skill_review_passes,
)
from ouroboros.tools.review_helpers import REPO_ROOT
from tests._review_substrate_shared import user_text as _user_text
from tests.test_skill_review_late_cas import late_skill  # noqa: F401 -- the real paid-wave harness

# Measuring a native seat's window probes the route's catalog; keep it off the wire.
pytestmark = pytest.mark.usefixtures("provider_catalog_offline")

_VERDICT = json.dumps([{"item": "x", "verdict": "PASS", "severity": "advisory", "reason": "ok"}])
# Quotes, backslashes and newlines: JSON escaping inflates what a send carries.
_ESCAPED = 'say "hi" \\ "there"\n' * 200


def _skill(tmp_path, files=None, name="demo"):
    skill_dir = tmp_path / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Fixture.\n---\nBody.\n", encoding="utf-8")
    for rel, data in (files or {}).items():
        (skill_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (skill_dir / rel).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    skill = SimpleNamespace(name=name, skill_dir=skill_dir, manifest=SimpleNamespace(entry="", scripts=[]))
    return skill, compute_content_hash(skill_dir)


def _build_prompt(ctx, drive_root, skill, *, manifest_dump, content_hash, file_pack, history, review_rebuttal):
    return f"STABLE::## Skill files\n{file_pack}\n## Output contract", len("STABLE::"), {}


def _native_plan(*kinds):
    routes = [ReviewRouteKind.AGENT_SESSION if kind == "session" else ReviewRouteKind.API_CHAT for kind in kinds]
    return {"models": [f"vendor/{kind}-{i}" for i, kind in enumerate(kinds)], "routes": routes,
            "retrieves": [kind != "packet" for kind in kinds],
            "efforts": ["high"] * len(kinds), "session_targets": [""] * len(kinds),
            "session_profiles": [""] * len(kinds), "session_accesses": ["readonly"] * len(kinds),
            "slot_ids": [f"{kind}-{i}" for i, kind in enumerate(kinds)], "use_local": [False] * len(kinds)}


def _pass(tmp_path, skill, content_hash, plan, *, packs=None, landing=None, monkeypatch=None, answers=None, ctx=None,
          session_root=REPO_ROOT):
    """Run the real pass runner over a fake panel transport; return its captured calls."""
    if landing is not None:
        monkeypatch.setattr(review_native_episode, "native_landing_at", lambda bound: landing)
    calls = []

    def run_review(ctx, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        seats = kwargs["row_plan"]["slot_ids"] if kwargs.get("row_plan") else ["legacy"]
        return json.dumps({"results": [
            {"model": model, "slot_id": slot, "text": (answers or {}).get(slot, _VERDICT)}
            for model, slot in zip(kwargs["models"], seats)]})

    from ouroboros.skill_review_packs import _build_skill_file_packs

    file_packs = packs or _build_skill_file_packs(skill.skill_dir, expected_content_hash=content_hash, split=False)
    result = run_skill_review_passes(
        ctx, tmp_path / "drive", skill,
        evidence={"manifest_dump": "{}", "content_hash": content_hash, "history": [],
                  "review_rebuttal": "", "required_items": ("x",)},
        file_packs=file_packs, models=list(plan["models"]), row_plan=plan, session_root=str(session_root),
        build_prompt=_build_prompt, run_review=run_review)
    return result, calls


def _seat(call, index):
    plan = call["row_plan"]
    return plan["session_tasks"][index], plan["session_policies"][index], plan["session_roots"][index]


def _views(tmp_path):
    """Every retained view container (and any staging left behind) under the drive."""
    root = tmp_path / "drive" / "state" / "review_checkouts" / "views"
    return sorted(path.name for path in root.iterdir()) if root.is_dir() else []


def _frozen_view(call, index=0):
    return pathlib.Path(_seat(call, index)[1]["skill_payload_delivery"]["frozen_view"])


def _executor(task, policy, root, *, llm=None, slot_id="native-0"):
    request = ReviewRequest(surface="skill_review", goal="g", task_id="skill-task", session_root=root,
                            session_task=task, policy=copy.deepcopy(policy), no_proxy=True)
    slot = ReviewSlot(slot_id=slot_id, model="vendor/native-0", effort="high", role_hint="multi-model review",
                      route=ReviewRouteKind.API_CHAT, native_retrieval_override=True)
    return NativeToolRoundReviewExecutor(ReviewAssignment(request=request, slot=slot, call_id="op-1"), llm=llm)


def test_the_fit_decision_is_the_dispatched_opening_and_its_exact_boundary(tmp_path, monkeypatch):
    skill, content_hash = _skill(tmp_path, {"lib.py": _ESCAPED})
    (_r, _e, _text, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                          landing=10**9, monkeypatch=monkeypatch)
    assert error == ""
    task, policy, root = _seat(calls[0], 0)
    delivery = policy["skill_payload_delivery"]
    assert delivery["form"] == "inline" and _ESCAPED in task
    # The measured first send IS the executor's own opening: manifest reference, escaping, schemas.
    opened = _executor(task, policy, root)._open_episode(root, str(tmp_path / "scratch"))[2:]
    assert delivery["first_send_chars"] == opened[1]
    assert "Required source manifest:" in _user_text(opened[0][-1]["content"])  # the cached assignment
    assert opened[1] > len(task) + _ESCAPED.count('"')  # escape inflation is counted, not raw text
    measured = delivery["first_send_chars"]
    # One char over the line is still inline; at the line the payload becomes sources.
    (_r, _e, _t, _err), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                      landing=measured + 1, monkeypatch=monkeypatch)
    assert _seat(calls[0], 0)[1]["skill_payload_delivery"]["form"] == "inline"
    (_r, _e, _t, _err), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                      landing=measured, monkeypatch=monkeypatch)
    task, policy, root = _seat(calls[0], 0)
    assert policy["skill_payload_delivery"]["form"] == "sources" and _ESCAPED not in task
    assert f"{measured:,} chars" in task and "lib.py:" in task  # the reason and the index
    payload = [row for row in policy["native_required_sources"] if row["disposition"] == "skill_payload"]
    assert {row["path"] for row in payload} == {"SKILL.md", "lib.py"}
    assert all(row["root"] == "active_workspace" and row["coverage_basis"] == "candidate_blob" for row in payload)
    # The seat's workspace is the frozen copy bound to the reviewed hash.
    assert root == policy["skill_payload_delivery"]["frozen_view"]
    assert compute_content_hash(pathlib.Path(root)) == content_hash


def test_a_payload_file_over_the_inline_hint_is_read_from_the_frozen_copy(tmp_path, monkeypatch):
    big = "".join(f"line {i:06d} of the oversized module\n" for i in range(40_000))  # ~1.4M chars
    skill, content_hash = _skill(tmp_path, {"big.py": big})
    # Exercise source delivery independently of the route's current window hint;
    # PR1810 removed the former send-size ceiling, including its implicit test bound.
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                      landing=100_000, monkeypatch=monkeypatch)
    assert error == "" and len(calls) == 1
    task, policy, root = _seat(calls[0], 0)
    assert policy["skill_payload_delivery"]["form"] == "sources" and "big.py:" in task
    # A real episode pages the file from the copy; the host observes the ranges it read.
    script = [{"tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": "big.py", "max_lines": 50})}}]},
        {"content": _VERDICT}]
    llm = SimpleNamespace(calls=[], chat=lambda **kw: (llm.calls.append(kw) or script.pop(0),
                                                       {"prompt_tokens": 1, "completion_tokens": 1}))
    result = _executor(task, policy, root, llm=llm).execute()
    assert result.raw_text == _VERDICT  # coverage never discards the received verdict
    tool_messages = [m for m in llm.calls[1]["messages"] if m.get("role") == "tool"]
    assert "line 000000 of the oversized module" in tool_messages[0]["content"]
    coverage = result.usage["native_read_coverage"]
    rows = {row["path"]: row for row in coverage["sources"]}
    assert rows["big.py"]["status"] == "incomplete" and rows["big.py"]["covered_chars"] > 0
    assert rows["BIBLE.md"]["status"] == "incomplete"  # unread governance is a visible gap
    assert result.usage["native_incomplete"] == "required_source_coverage_incomplete"


def test_an_index_that_does_not_land_either_is_one_exact_paged_source(tmp_path, monkeypatch):
    import hashlib

    files = {f"pkg/mod_{i:04d}.py": "V = 1\n" for i in range(800)}  # an index far larger than its payload
    skill, content_hash = _skill(tmp_path, files)
    ctx = SimpleNamespace(drive_root=tmp_path / "drive", task_id="skill-task", task_metadata={})
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                       landing=45_000, monkeypatch=monkeypatch, ctx=ctx)
    assert error == ""
    task, policy, root = _seat(calls[0], 0)
    delivery = policy["skill_payload_delivery"]
    assert delivery["form"] == "sources" and delivery["index"] == "paged"
    assert delivery["inline_first_send_chars"] >= 45_000 and delivery["index_first_send_chars"] >= 45_000
    assert delivery["first_send_chars"] < 45_000  # the pointer form is what this seat opens with
    stored = delivery["index_source"]
    index_file = (tmp_path / "drive").resolve() / "task_results"
    assert "pkg/mod_0001.py:" not in task and f'read_file(root="artifact_store", path="{stored["path"]}"' in task
    assert policy["native_data_root"] == str((tmp_path / "drive").resolve())
    index_row = next(row for row in policy["native_required_sources"] if row["disposition"] == "skill_payload_index")
    assert index_row["root"] == "artifact_store" and index_row["path"] == stored["path"]
    payload = [row for row in policy["native_required_sources"] if row["disposition"] == "skill_payload"]
    assert len(payload) == 801 and all(row["root"] == "active_workspace" for row in payload)  # per-file identity kept
    # The real episode opens under its line and pages the stored index from the artifact store.
    script = [{"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": json.dumps(
        {"root": "artifact_store", "path": stored["path"], "max_lines": 40})}}]}, {"content": _VERDICT}]
    llm = SimpleNamespace(calls=[], chat=lambda **kw: (llm.calls.append(kw) or script.pop(0),
                                                       {"prompt_tokens": 1, "completion_tokens": 1}))
    result = _executor(task, policy, root, llm=llm).execute()
    assert result.raw_text == _VERDICT
    read = next(m for m in llm.calls[1]["messages"] if m.get("role") == "tool")["content"]
    assert "pkg/mod_0000.py:" in read and f"content hash {content_hash}" in read
    rows = {row["path"]: row for row in result.usage["native_read_coverage"]["sources"]}
    assert rows[stored["path"]]["covered_chars"] > 0 and rows["pkg/mod_0000.py"]["status"] == "incomplete"
    assert hashlib.sha256(next(index_file.rglob("skill-payload-index-*.txt")).read_bytes()).hexdigest() == stored["sha256"]


def test_session_seat_reads_by_absolute_path_and_its_journal_folds_over_the_manifest(tmp_path, monkeypatch):
    from ouroboros.review_session_reads import session_read_facts

    skill, content_hash = _skill(tmp_path, {"lib.py": "def run():\n    return 1\n"})
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("session"), monkeypatch=monkeypatch)
    assert error == ""
    task, policy, root = _seat(calls[0], 0)
    assert root == "" and calls[0]["session_root"] == str(REPO_ROOT)  # the repository stays its root
    view = policy["skill_payload_delivery"]["frozen_view"]
    assert f"the absolute folder {view}" in task
    row = next(r for r in policy["native_required_sources"] if r["path"].endswith("lib.py"))
    assert row["file"] == str(pathlib.Path(view) / "lib.py") and row["root"] == "runtime_data"
    events = tmp_path / "run" / "attempts" / "a01"
    events.mkdir(parents=True)
    call = {"type": "tool_call", "tool": {"name": "Read", "kind": "file", "target": "Read", "use_id": "u1"},
            "payload": {"input": {"file_path": row["file"]}}}
    done = {"type": "tool_result", "tool": {**call["tool"], "status": "ok"}}
    (events / "events.jsonl").write_text(json.dumps(call) + "\n" + json.dumps(done) + "\n", encoding="utf-8")
    facts = session_read_facts(str(tmp_path / "run"), {**policy, "native_required_sources": [row]},
                               session_root=str(REPO_ROOT))
    assert facts["read_provenance"] == "harness_observed"
    assert facts["native_read_coverage"]["sources"][0]["status"] == "complete"


def test_a_session_seat_reads_an_index_that_does_not_land_from_its_ignored_project_copy(tmp_path, monkeypatch):
    import subprocess

    from ouroboros.review_session_reads import session_read_facts
    from ouroboros.tools import review_brief_coupling

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / ".gitignore").write_text("/.review-drive/\n", encoding="utf-8")
    monkeypatch.setattr(review_brief_coupling, "SESSION_INLINE_DIFF_CEILING_CHARS", 30_000)
    skill, content_hash = _skill(tmp_path, {f"pkg/mod_{i:04d}.py": "V = 1\n" for i in range(800)})
    ctx = SimpleNamespace(drive_root=tmp_path / "drive", task_id="skill-task", task_metadata={})
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("session"), monkeypatch=monkeypatch,
                                       ctx=ctx, session_root=project)
    assert error == ""
    task, policy, root = _seat(calls[0], 0)
    delivery = policy["skill_payload_delivery"]
    assert delivery["index"] == "paged" and delivery["inline_first_send_chars"] >= 30_000
    assert delivery["first_send_chars"] < 30_000 <= delivery["index_first_send_chars"]
    row = next(r for r in policy["native_required_sources"] if r["disposition"] == "skill_payload_index")
    assert row["root"] == "session_root" and row["path"].startswith(".review-drive/skill-task/")
    assert f"`{row['path']}`" in task and "pkg/mod_0001.py:" not in task and "native_data_root" not in policy
    assert f"content hash {content_hash}" in (project / row["path"]).read_text(encoding="utf-8")
    events = tmp_path / "run" / "attempts" / "a01"
    events.mkdir(parents=True)
    call = {"type": "tool_call", "tool": {"name": "Read", "kind": "file", "target": "Read", "use_id": "u1"},
            "payload": {"input": {"file_path": str(project / row["path"])}}}
    done = {"type": "tool_result", "tool": {**call["tool"], "status": "ok"}}
    (events / "events.jsonl").write_text(json.dumps(call) + "\n" + json.dumps(done) + "\n", encoding="utf-8")
    facts = session_read_facts(str(tmp_path / "run"), {**policy, "native_required_sources": [row]},
                               session_root=str(project))
    assert facts["native_read_coverage"]["sources"][0]["status"] == "complete"


def test_binary_payload_stays_a_descriptor_and_owes_no_text_read(tmp_path, monkeypatch):
    skill, content_hash = _skill(tmp_path, {"logo.png": b"\x89PNG\r\n\x1a\n\xff\x00binary"})
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                       landing=1, monkeypatch=monkeypatch)
    assert error == ""
    task, policy, _root = _seat(calls[0], 0)
    assert "logo.png: binary descriptor" in task and "mime_from_name image/png" in task
    assert "logo.png" not in {row["path"] for row in policy["native_required_sources"]}


def test_mixed_chunked_wave_reserves_retrieving_seats_in_the_first_chunk_only(tmp_path):
    skill, content_hash = _skill(tmp_path)
    plan = _native_plan("native", "packet")
    ctx = SimpleNamespace(_skill_review_wave_binding={"skill": "demo"})
    wave = _reserve_skill_wave(ctx, tmp_path, skill, ["p1", "p2"], plan["models"], plan,
                               {"review_wave_id": "wave-1"}, content_hash, "fp", "rb", packet_slots={"packet-1"})
    assert [sorted(chunk["operations"]) for chunk in wave["chunks"]] == [["native-0", "packet-1"], ["packet-1"]]
    every = _reserve_skill_wave(ctx, tmp_path, skill, ["p1", "p2"], plan["models"], plan,
                                {"review_wave_id": "wave-2"}, content_hash, "fp", "rb")
    assert all(sorted(chunk["operations"]) == ["native-0", "packet-1"] for chunk in every["chunks"])


@pytest.mark.parametrize("answers,quorum_ok", [
    ({}, True),
    ({"native-0": "not json"}, False),  # the whole-payload answer is missing for chunk 2 as well
])
def test_a_retrieving_answer_counts_toward_every_packet_chunk(tmp_path, monkeypatch, answers, quorum_ok):
    skill, content_hash = _skill(tmp_path)
    plan = _native_plan("native", "packet")
    (_r, _e, text, error), calls = _pass(tmp_path, skill, content_hash, plan, packs=["p1", "p2"],
                                         monkeypatch=monkeypatch, answers=answers)
    assert [call["models"] for call in calls][:1] == [plan["models"]]
    if quorum_ok:
        assert error == "" and [call["models"] for call in calls] == [plan["models"], ["vendor/packet-1"]]
        assert [row["slot_id"] for row in json.loads(text)["results"]] == ["native-0", "packet-1", "packet-1"]
    else:
        assert text == "" and "chunk 1/2" in error  # never a merged PASS over a missing whole review


def test_collection_keeps_a_mixed_wave_pending_until_every_chunk_has_its_quorum(tmp_path, monkeypatch):
    from ouroboros import skill_review_collect as collect
    from ouroboros.skill_review_passes import _skill_review_retry_key
    from ouroboros.tools.review_response import parse_model_response

    owner = {"skill": "demo", "content_hash": "h", "review_contract_fingerprint": "fp", "rebuttal_sha256": "rb"}

    def chunk(index, slots):
        key = _skill_review_retry_key(skill_name="demo", wave_id="w", content_hash="h", contract_fingerprint="fp",
                                      rebuttal_sha256="rb", pack=f"p{index + 1}", chunk_index=index, chunk_count=2)
        return {"retry_key": key, "operations": {slot: f"op-{index}-{slot}" for slot in slots}}

    def run(answered, later=("packet-1",)):
        def seat(root, ctx, wave, chunk_row, slot, operation):
            ok = (operation in answered)
            payload = {"slot_id": slot, "choices": [{"message": {"content": _VERDICT}}]} if ok else {
                "slot_id": slot, "error": "pending", "operation_state": "in_flight", "late_result_pending": True}
            return f"vendor/{slot}", parse_model_response(f"vendor/{slot}", payload, None)

        monkeypatch.setattr(collect, "_collect_seat", seat)
        ctx = SimpleNamespace(_skill_review_resume={"wave_id": "w", "binding": owner, "chunks": [
            chunk(0, ["native-0", "packet-1"]), chunk(1, list(later))]})
        return collect.collect_skill_review_wave(ctx, tmp_path, ["p1", "p2"], required_items=("x",))

    whole = run({"op-0-native-0", "op-0-packet-1", "op-1-packet-1"})
    assert whole["collection_pending"] is False and len(whole["results"]) == 3
    # The packet seat's second chunk is still open: the retrieving answer alone is not its quorum.
    assert run({"op-0-native-0", "op-0-packet-1"})["collection_pending"] is True
    # A later chunk naming a seat outside the first roster is not this wave.
    assert "error" in run(set(), later=("stranger",))


def test_subject_drift_after_the_packs_dispatches_nothing(tmp_path, monkeypatch):
    skill, content_hash = _skill(tmp_path, {"lib.py": "v1\n"})
    from ouroboros.skill_review_packs import _build_skill_file_packs

    packs = _build_skill_file_packs(skill.skill_dir, expected_content_hash=content_hash, split=False)
    (skill.skill_dir / "lib.py").write_text("v2\n", encoding="utf-8")  # the live payload moved on
    (_r, _e, text, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"), packs=packs,
                                         monkeypatch=monkeypatch)
    assert calls == [] and text == "" and "frozen payload unavailable" in error


def test_read_only_seats_read_the_loader_inventory_never_the_dependency_env(tmp_path, monkeypatch):
    skill, content_hash = _skill(tmp_path, {"lib.py": "def run():\n    return 1\n"})
    env = skill.skill_dir / ".ouroboros_env" / "lib"
    env.mkdir(parents=True)
    (env / "wheel.bin").write_bytes(b"\0" * (40 * 1024 * 1024))  # an installed env the hash leaves out
    assert compute_content_hash(skill.skill_dir) == content_hash
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native", "session"),
                                       monkeypatch=monkeypatch)
    assert error == ""
    view = _frozen_view(calls[0])
    assert _seat(calls[0], 0)[2] == str(view) == str(_frozen_view(calls[0], 1))  # one view for both read-only seats
    assert not (view / ".ouroboros_env").exists() and compute_content_hash(view) == content_hash
    container = view.parent.parent
    assert sum(path.stat().st_size for path in container.rglob("*") if path.is_file()) < 64 * 1024
    assert _views(tmp_path) == [container.name]  # no whole-folder copy without a writable seat
    assert calls[0].get("checking_source") is None


def test_the_same_hash_reuses_one_reader_view_through_metadata_and_cache_churn(tmp_path, monkeypatch):
    skill, content_hash = _skill(tmp_path, {"lib.py": "V = 1\n"})
    first = _pass(tmp_path, skill, content_hash, _native_plan("native"), monkeypatch=monkeypatch)[1]
    lib = skill.skill_dir / "lib.py"
    os.utime(lib, (1_000_000, 1_000_000))
    lib.chmod(0o755)
    (skill.skill_dir / "__pycache__").mkdir()
    (skill.skill_dir / "__pycache__" / "lib.cpython-312.pyc").write_bytes(b"cache")
    (skill.skill_dir / ".ouroboros_env").mkdir()
    (skill.skill_dir / ".ouroboros_env" / "installed.txt").write_text("dependency\n", encoding="utf-8")
    assert compute_content_hash(skill.skill_dir) == content_hash
    second = _pass(tmp_path, skill, content_hash, _native_plan("native"), monkeypatch=monkeypatch)[1]
    assert _frozen_view(second[0]) == _frozen_view(first[0])
    assert len(_views(tmp_path)) == 1


def test_a_hash_that_moved_before_its_view_was_kept_refuses_typed_and_recovers_at_that_hash(tmp_path, monkeypatch):
    from ouroboros.skill_review_packs import _build_skill_file_packs

    skill, content_hash = _skill(tmp_path, {"lib.py": "v1\n"})
    packs = _build_skill_file_packs(skill.skill_dir, expected_content_hash=content_hash, split=False)
    (skill.skill_dir / "lib.py").write_text("v2\n", encoding="utf-8")
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"), packs=packs,
                                       monkeypatch=monkeypatch)
    assert calls == [] and "changed after its review packs were built" in error
    assert _views(tmp_path) == []  # nothing published under the reviewed hash's name, no staging left
    (skill.skill_dir / "lib.py").write_bytes(b"v1\n")
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"), packs=packs,
                                       monkeypatch=monkeypatch)
    view = _frozen_view(calls[0])
    assert error == "" and (view / "lib.py").read_text(encoding="utf-8") == "v1\n"
    assert compute_content_hash(view) == content_hash


def test_a_published_view_that_drifted_is_replaced_and_never_served(tmp_path, monkeypatch):
    from ouroboros.skill_review_packs import _build_skill_file_packs

    skill, content_hash = _skill(tmp_path, {"lib.py": "v1\n"})
    packs = _build_skill_file_packs(skill.skill_dir, expected_content_hash=content_hash, split=False)
    view = _frozen_view(_pass(tmp_path, skill, content_hash, _native_plan("native"), monkeypatch=monkeypatch)[1][0])
    (view / "lib.py").write_text("tampered\n", encoding="utf-8")
    calls = _pass(tmp_path, skill, content_hash, _native_plan("native"), packs=packs, monkeypatch=monkeypatch)[1]
    assert _frozen_view(calls[0]) == view and (view / "lib.py").read_text(encoding="utf-8") == "v1\n"
    # Drift while the live payload is elsewhere: refused, and the drifted bytes do not stay under the name.
    (view / "lib.py").write_text("tampered\n", encoding="utf-8")
    (skill.skill_dir / "lib.py").write_text("v2\n", encoding="utf-8")
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"), packs=packs,
                                       monkeypatch=monkeypatch)
    assert calls == [] and "frozen payload unavailable" in error
    assert not view.exists() and _views(tmp_path) == []


def test_a_declared_entry_and_script_under_skipped_folders_stay_in_the_reader_view(tmp_path, monkeypatch):
    skill, _ = _skill(tmp_path, {"node_modules/run.js": "console.log(1)\n", ".tox/tool.sh": "echo hi\n",
                                 "node_modules/other.js": "undeclared\n"})
    skill.manifest = SimpleNamespace(entry="node_modules/run.js", scripts=[{"name": ".tox/tool.sh"}])
    content_hash = compute_content_hash(skill.skill_dir, manifest_entry="node_modules/run.js",
                                        manifest_scripts=[{"name": ".tox/tool.sh"}])
    assert content_hash != compute_content_hash(skill.skill_dir)  # the declared files are hashed
    from ouroboros.skill_review_packs import _build_skill_file_packs

    packs = _build_skill_file_packs(skill.skill_dir, manifest_entry="node_modules/run.js",
                                    manifest_scripts=[{"name": ".tox/tool.sh"}], expected_content_hash=content_hash,
                                    split=False)
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"), packs=packs,
                                       landing=1, monkeypatch=monkeypatch)
    assert error == ""
    view = _frozen_view(calls[0])
    assert (view / "node_modules" / "run.js").is_file() and (view / ".tox" / "tool.sh").is_file()
    assert not (view / "node_modules" / "other.js").exists()
    assert compute_content_hash(view, manifest_entry="node_modules/run.js",
                                manifest_scripts=[{"name": ".tox/tool.sh"}]) == content_hash
    rows = {row["path"] for row in _seat(calls[0], 0)[1]["native_required_sources"]
            if row["disposition"] == "skill_payload"}
    assert rows == {"SKILL.md", "node_modules/run.js", ".tox/tool.sh"}


@pytest.mark.parametrize("kind", ["native", "session"])
def test_a_link_to_a_skipped_file_keeps_its_bytes_without_copying_dependencies(tmp_path, monkeypatch, kind):
    body = "module.exports = 1\n"
    skill, _ = _skill(tmp_path, {"node_modules/real.js": body, "node_modules/unused.js": "unused\n"})
    try:  # the host's separator: Windows reads a "/" inside a relative link target as part of a name
        (skill.skill_dir / "shim.js").symlink_to(os.path.join("node_modules", "real.js"))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this host cannot create symlinks: {exc}")
    content_hash = compute_content_hash(skill.skill_dir)  # the hash reads shim.js through its link
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan(kind),
                                       monkeypatch=monkeypatch)
    assert error == "" and len(calls) == 1
    view = _frozen_view(calls[0])
    assert (view / "shim.js").read_text(encoding="utf-8") == body
    assert not (view / "shim.js").is_symlink()
    assert not (view / "node_modules").exists()
    assert compute_content_hash(view) == content_hash
    assert (skill.skill_dir / "shim.js").is_symlink()  # the author keeps its topology


def test_a_failed_copy_removes_its_own_staging_and_publishes_nothing(tmp_path, monkeypatch):
    from ouroboros import subagent_worktrees

    skill, content_hash = _skill(tmp_path, {"lib.py": "V = 1\n"})

    def broken(target, dest, **inventory):
        dest.mkdir(parents=True)
        (dest / "partial.py").write_text("half", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(subagent_worktrees, "_copy_payload_inventory", broken)
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, _native_plan("native"),
                                       monkeypatch=monkeypatch)
    assert calls == [] and "disk full" in error and _views(tmp_path) == []


def test_a_mixed_panel_binds_read_only_seats_to_the_inventory_and_a_writable_seat_to_the_whole_folder(
        tmp_path, monkeypatch):
    import hashlib

    skill, content_hash = _skill(tmp_path, {"lib.py": "def run():\n    return 1\n"})
    (skill.skill_dir / ".ouroboros_env").mkdir()
    (skill.skill_dir / ".ouroboros_env" / "dep.py").write_text("DEP = 1\n", encoding="utf-8")
    (skill.skill_dir / "empty").mkdir()
    plan = _native_plan("native", "session", "session")
    plan["session_accesses"] = ["readonly", "readonly", "full"]
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, plan, monkeypatch=monkeypatch)
    assert error == "" and len(calls) == 1
    reader, writer = _frozen_view(calls[0], 0), _frozen_view(calls[0], 2)
    assert _seat(calls[0], 0)[2] == str(reader) and _frozen_view(calls[0], 1) == reader
    assert not (reader / ".ouroboros_env").exists() and not (reader / "empty").exists()
    assert (writer / ".ouroboros_env" / "dep.py").is_file() and (writer / "empty").is_dir()
    assert compute_content_hash(reader) == compute_content_hash(writer) == content_hash
    checking = calls[0]["checking_source"]
    assert checking == {"kind": "directory", "root": str(skill.skill_dir.resolve()), "view": str(writer),
                        "content_hash": content_hash}
    from ouroboros.review_checking import prepare_seat_area

    area = pathlib.Path(prepare_seat_area(tmp_path / "drive", "skill-task", "op-writer", checking))
    assert (area / ".ouroboros_env" / "dep.py").is_file() and (area / "empty").is_dir()  # the writer's baseline
    for index, view in ((1, reader), (2, writer)):
        task, policy, _root = _seat(calls[0], index)
        assert f"the absolute folder {view}" in task
        for row in (row for row in policy["native_required_sources"] if row["disposition"] == "skill_payload"):
            file = pathlib.Path(row["file"])
            assert file.parent == view or view in file.parents
            assert hashlib.sha256(file.read_bytes()).hexdigest() == row["source_revision"]


@pytest.mark.parametrize("failed", ["whole_folder", "inventory"])
def test_a_failed_whole_folder_copy_refuses_only_the_writable_seat(tmp_path, monkeypatch, failed):
    """The writer's checking copy failing (a full disk, say) refuses that seat typed at its own
    dispatch; the native and packet seats still review. The read-only copy every retrieving seat
    reads stays a wave precondition."""
    from ouroboros import skill_review_passes
    from ouroboros.review_checking import session_checking
    from ouroboros.review_execution import ReviewRouteUnavailable

    skill, content_hash = _skill(tmp_path, {"lib.py": "def run():\n    return 1\n"})
    no_space = {"unavailable": "OSError: [Errno 28] No space left on device"}
    if failed == "whole_folder":
        monkeypatch.setattr(skill_review_passes, "skill_checking_source", lambda *a, **kw: dict(no_space))
    else:
        monkeypatch.setattr(skill_review_passes, "frozen_payload_source", lambda *a, **kw: dict(no_space))
    plan = _native_plan("native", "packet", "session")
    plan["session_accesses"] = ["readonly", "readonly", "workspace_write"]
    (_r, _e, _t, error), calls = _pass(tmp_path, skill, content_hash, plan, monkeypatch=monkeypatch)
    if failed == "inventory":
        assert "frozen payload unavailable" in error and not calls
        return
    assert error == "" and len(calls) == 1
    assert calls[0]["models"] == plan["models"] and calls[0]["checking_source"] == no_space
    # Every seat is assembled over the read-only copy; only the writer's own dispatch refuses.
    assert _frozen_view(calls[0], 2) == _frozen_view(calls[0], 0)
    with pytest.raises(ReviewRouteUnavailable) as refused:
        session_checking(None, custody_drive=tmp_path / "drive", task_id="skill-task", operation_id="op-w",
                         root=str(tmp_path), source=calls[0]["checking_source"], shape=None)
    assert refused.value.code == "review_checking_source_missing" and "No space left" in str(refused.value)


def test_retrieving_panels_lapse_replay_while_packet_panels_keep_their_identity(monkeypatch):
    import ouroboros.skill_review_passes as passes
    from ouroboros.skill_review_cycles import skill_review_contract_fingerprint

    native, packet = _native_plan("native"), _native_plan("packet")

    def fingerprint(delivery):
        return skill_review_contract_fingerprint(delivery["models"], required_items=("x",), delivery=delivery)

    before = (fingerprint(native), fingerprint(packet))
    monkeypatch.setattr(passes, "_PAYLOAD_SOURCES", passes._PAYLOAD_SOURCES + " changed")
    assert fingerprint(native) != before[0] and fingerprint(packet) == before[1]


def test_review_skill_reads_an_oversized_file_without_packet_seats_and_names_packet_seats_otherwise(
        tmp_path, monkeypatch):
    from ouroboros import skill_review, skill_review_packs
    from tests._skill_review_shared import _build_skill, _make_ctx
    from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool

    skills_root = _build_skill(tmp_path)
    (skills_root / "weather" / "scripts" / "table.py").write_text("ROWS = " + repr("x" * 4000) + "\n",
                                                                   encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    monkeypatch.setattr(skill_review_packs, "_skill_pack_token_budget", lambda: 300)  # one file exceeds a packet
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a, **k: None)
    captured = []

    def fake_review(_ctx, **kwargs):
        captured.append(kwargs)
        return json.dumps({"results": []})

    monkeypatch.setattr("ouroboros.tools.review._handle_multi_model_review", fake_review)
    ctx = _make_ctx(tmp_path)
    set_review_pool(monkeypatch, pool_roster(pool_seat("review-1", "openai/gpt-5.5", delivery="native"),
                                             pool_seat("review-2", "google/gemini-3.5-flash", delivery="native")))
    skill_review.review_skill(ctx, "weather", persist=False)
    assert len(captured) == 1  # no packet seat: no packet veto, one whole-payload pass
    tasks = [task for task in captured[0]["row_plan"]["session_tasks"] if task]
    assert len(tasks) == 2 and all("table.py" in task for task in tasks)
    captured.clear()
    set_review_pool(monkeypatch, pool_roster(pool_seat("review-1", "openai/gpt-5.5", delivery="native"),
                                             pool_seat("review-2", "google/gemini-3.5-flash")))
    outcome = skill_review.review_skill(ctx, "weather", persist=False)
    assert captured == [] and outcome.status == "pending"  # the irreducible mixed wave stays pending
    assert "table.py" in outcome.error and "review-2" in outcome.error
    assert "no reviewer was dispatched" in outcome.error


def test_the_real_wave_admission_prices_packet_seats_per_chunk_and_a_native_seat_once(tmp_path, monkeypatch):
    from ouroboros import skill_review, skill_review_packs, usage_admission
    from ouroboros import usage_accounting as ua
    from tests._skill_review_shared import _build_skill, _make_ctx
    from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool

    skills_root = _build_skill(tmp_path)
    for i in range(4):
        (skills_root / "weather" / "scripts" / f"part_{i}.py").write_text(
            f"PART_{i} = " + repr("y" * 600) + "\n", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    monkeypatch.setattr(skill_review_packs, "_skill_pack_token_budget", lambda: 300)  # several packs, none over
    admissions, real = [], usage_admission.review_wave_admission
    monkeypatch.setattr(usage_admission, "review_wave_admission",
                        lambda *a, **kw: admissions.append(kw) or real(*a, **kw))
    monkeypatch.setattr("ouroboros.tools.review._handle_multi_model_review",
                        lambda _ctx, **kw: json.dumps({"results": []}))
    set_review_pool(monkeypatch, pool_roster(pool_seat("review-1", "openai/gpt-5.5", delivery="native"),
                                             pool_seat("review-2", "google/gemini-3.5-flash")))
    ctx = _make_ctx(tmp_path)
    with ua.usage_scope(ua.UsageScope(drive_root=ctx.drive_root, task_id="t1", root_task_id="r1")):
        skill_review.review_skill(ctx, "weather", persist=False)
    assert len(admissions) == 1  # the whole wave, admitted once before the first dispatch
    models, chars = admissions[0]["models"], admissions[0]["prompt_chars"]
    packs = models.count("google/gemini-3.5-flash")
    assert packs >= 2 and models == ["google/gemini-3.5-flash"] * packs + ["openai/gpt-5.5"]
    assert len(chars) == len(models) and len(set(chars[:packs])) == 1  # one physical call per packet chunk
    assert chars[-1] > chars[0]  # the native seat: one call carrying the whole payload


def test_commit_gate_prices_a_declared_manifest_the_way_the_episode_opens(tmp_path):
    from ouroboros.tools.review_admission import commit_gate_paid_seats
    from ouroboros.tools.review_multi_model import TRIAD_ROLE_HINT
    from ouroboros.triad_review import REVIEW_TWO_PART_OBJECT_CONTRACT

    repo = tmp_path / "repo"
    repo.mkdir()
    policy = {"output_contract": REVIEW_TWO_PART_OBJECT_CONTRACT, "native_required_sources": [],
              "native_required_sources_ref": {"policy": "v2", "sha256": "a" * 64, "required_source_count": 0}}
    prepared = {"prompt": "", "stable_prefix_len": 0, "target_repo": str(repo), "models": ["api/model"],
                "routes": [ReviewRouteKind.API_CHAT],
                "row_plan": {"models": ["api/model"], "routes": [ReviewRouteKind.API_CHAT], "slot_ids": ["s1"],
                             "retrieves": [True], "parts": [("coupling",)], "session_tasks": ["BRIEF"],
                             "session_policies": [policy]}}
    seat = commit_gate_paid_seats(prepared, False)[0]
    request = ReviewRequest(surface="multi_model_review", goal="g", task_id="t", session_root=str(repo),
                            session_task="BRIEF", policy=policy)
    slot = ReviewSlot(slot_id="s1", model="api/model", effort="low", role_hint=TRIAD_ROLE_HINT,
                      route=ReviewRouteKind.API_CHAT, native_retrieval_override=True)
    executor = NativeToolRoundReviewExecutor(ReviewAssignment(request=request, slot=slot, call_id="op"), llm=None)
    assert seat["prompt_chars"] == executor._open_episode(str(repo), str(repo))[3]


def _mixed_late_wave(late_skill, monkeypatch):  # noqa: F811
    """The late-CAS harness with seat-a natively retrieving and seat-b a packet seat over two packs."""
    from ouroboros import skill_review

    h = late_skill
    h.delivery.update(retrieves=[True, False], use_local=[False, False])
    monkeypatch.setattr(skill_review, "_build_skill_file_packs", lambda *a, **kw: ["chunk a", "chunk b"])
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a, **kw: None)
    return h


def _press_review(h, monkeypatch):
    """One Skills-page Review press through the real gateway route and lifecycle."""
    import asyncio

    from ouroboros.gateway import extensions

    monkeypatch.setattr(extensions, "_request_drive_root", lambda request: h.root)
    monkeypatch.setattr(extensions, "_request_repo_dir", lambda request: h.root)
    return json.loads(asyncio.run(extensions.api_skill_review(SimpleNamespace(path_params={"skill": "late"}))).body)


def _oversized_late_wave(h, monkeypatch, recorded):
    """A payload over the packet budget, paid for and left late: two packet chunks, or one native read."""
    import threading

    from ouroboros import review_custody as custody
    from ouroboros import review_pause, skill_review, skill_review_packs

    for name, word in (("a.md", "ALPHA"), ("b.md", "BRAVO")):
        (h.skill_dir / name).write_text((word + " ") * 60, encoding="utf-8")
    monkeypatch.setattr(skill_review_packs, "_skill_pack_token_budget", lambda: 160)
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a, **kw: None)
    h.delivery.update(retrieves=[recorded == "native"] * 2, use_local=[False, False])
    entered, lock, late = [], threading.Lock(), threading.Event()

    def wait(kwargs):
        if recorded == "packet" and "BRAVO" not in str(kwargs["messages"]):
            return True  # the first chunk answers in time
        with lock:  # both late seats have sent (and are paid for) before their window ends
            entered.append(1)
            if len(entered) == 2:
                late.set()
        return h.release.wait(10)

    h.behavior.wait = wait
    h.behavior.expected_completions = 2 if recorded == "native" else 4
    now = custody.monotonic_now
    clock = lambda slot=None: now(slot) + (60 if late.is_set() else 0)  # noqa: E731
    monkeypatch.setattr(custody, "monotonic_now", clock)
    monkeypatch.setattr(review_native_episode, "monotonic_now", clock)
    monkeypatch.setattr(custody, "_logical_timeout", lambda *_: 30)
    monkeypatch.setattr(review_pause, "drain_slice", lambda _operation, remaining: min(.02, remaining))
    first = _press_review(h, monkeypatch)
    old = json.loads(h.history.read_bytes().splitlines()[-1])
    assert first["status"] == "pending" and old.get("paid") and "review_wave" in old, (first, old)
    assert len(old["review_wave"]["chunks"]) == (1 if recorded == "native" else 2)
    h.release.set()
    assert h.settled.wait(10)
    return old, len(h.calls)


@pytest.mark.parametrize("recorded", ["native", "packet"])
@pytest.mark.parametrize("today", ["unchanged", "flipped", "invalid_pool"])
def test_a_paid_wave_is_collected_in_its_recorded_layout_whatever_the_pool_says_now(
        late_skill, monkeypatch, recorded, today):  # noqa: F811
    """Delivery decides how a NEW wave splits the payload; a recorded wave keeps its own chunks."""
    from ouroboros import skill_review
    from ouroboros.skill_loader import load_review_state
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles

    h = late_skill
    old, sent = _oversized_late_wave(h, monkeypatch, recorded)
    if today == "flipped":
        h.delivery.update(retrieves=[recorded != "native"] * 2)
    if today == "invalid_pool":
        monkeypatch.setattr(skill_review, "reviewer_slot_config_error", lambda: "the pool has no usable reviewer")
    second = _press_review(h, monkeypatch)
    new = json.loads(h.history.read_bytes().splitlines()[-1])
    assert new.get("review_resume_of") == old["wave_id"], second
    assert new["review_wave"] == old["review_wave"]
    assert load_review_state(h.root, "late").status == "clean", second
    assert len(h.calls) == sent  # collection buys nothing
    assert count_paid_skill_review_cycles(h.root, "late", "manual:late", content_hash=old["content_hash"]) == 1


@pytest.mark.parametrize("change", ["edited_payload", "cancelled"])
def test_a_recorded_layout_never_lends_a_wave_to_other_bytes_or_a_cancelled_job(
        late_skill, monkeypatch, change):  # noqa: F811
    """With today's pool flipped, an edited payload or a cancelled job is not collected from the old wave."""
    from ouroboros.skill_loader import load_review_state

    h = late_skill
    old, sent = _oversized_late_wave(h, monkeypatch, "native")
    h.delivery.update(retrieves=[False, False])
    if change == "edited_payload":
        (h.skill_dir / "b.md").write_text("BRAVO changed\n", encoding="utf-8")
    else:
        row = json.loads(h.history.read_bytes().splitlines()[-1])
        h.history.write_text(json.dumps({**row, "job_status": "cancelled"}) + "\n")
    _press_review(h, monkeypatch)
    assert json.loads(h.history.read_bytes().splitlines()[-1]).get("review_resume_of") != old["wave_id"]
    assert load_review_state(h.root, "late").status != "clean" or len(h.calls) > sent


def test_cold_resume_of_a_mixed_wave_never_passes_an_unstarted_packet_chunk(late_skill, monkeypatch):  # noqa: F811
    from ouroboros import review_custody as custody
    from ouroboros import skill_review_runner as runner
    from ouroboros.skill_loader import load_review_state
    from ouroboros.tools.skill_exec import _handle_review_skill

    h = _mixed_late_wave(late_skill, monkeypatch)
    # The native seat answers within its window; chunk a's packet seat outlives its own.
    monkeypatch.setattr(custody, "_logical_timeout", lambda slot, request, meta: .05 if request.messages else 2)
    h.behavior.wait = lambda kwargs: bool(kwargs.get("tools")) or h.release.wait(10)
    rosters = []
    h.behavior.before_wait = lambda: rosters.append([sorted(c["operations"]) for c in json.loads(
        runner.review_job_state_path(h.root, "late").read_text())["review_wave"]["chunks"]])
    _handle_review_skill(h.ctx(), skill="late")
    h.release.set()
    assert h.settled.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    assert rosters and rosters[0] == [["seat-a", "seat-b"], ["seat-b"]]
    assert load_review_state(h.root, "late").status == "pending"  # chunk b's packet seat never answered
    assert len(h.calls) == 2 and sum(bool(call.get("tools")) for call in h.calls) == 1  # nothing re-sent


def test_cold_resume_of_a_mixed_wave_counts_the_whole_payload_answer_in_every_chunk(late_skill, monkeypatch):  # noqa: F811
    from ouroboros import review_custody as custody
    from ouroboros.skill_loader import load_review_state
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
    from ouroboros.tools.skill_exec import _handle_review_skill

    h = _mixed_late_wave(late_skill, monkeypatch)
    # Only chunk b tests expiry. The first seats answer immediately, but their
    # real opening/source IO must fit even under a loaded parallel test run.
    monkeypatch.setattr(custody, "_logical_timeout", lambda slot, request, meta: 30 if (
        "chunk a" in str(request.messages) or not request.messages) else .05)
    h.behavior.expected_completions = 3
    h.behavior.wait = lambda kwargs: bool(kwargs.get("tools")) or "chunk a" in str(kwargs["messages"]) or h.release.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status != "clean"
    h.release.set()
    assert h.settled.wait(10)
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "clean"
    assert len(h.calls) == 3  # native once, packet per chunk; the resume bought nothing
    assert sum(bool(call.get("tools")) for call in h.calls) == 1
    assert count_paid_skill_review_cycles(h.root, "late", "task:skill-root:late") == 1


def test_cold_collection_of_a_paged_index_seat_rejoins_its_retained_sources_and_buys_nothing(
        late_skill, monkeypatch):  # noqa: F811
    """A native seat opened with a paged index reads the stored source, outlives its
    lifecycle, and is collected cold from its original paid producer; the stored index
    and the reader view it was sent stay byte-exact across both transitions."""
    import hashlib
    import re
    import threading

    from ouroboros import review_custody as custody
    from ouroboros import review_pause, skill_review
    from ouroboros.review_dispatch import invoke_bound_api_review_paid_stamp
    from ouroboros.skill_loader import load_review_state
    from ouroboros.skill_review_cycles import count_paid_skill_review_cycles
    from ouroboros.tools.skill_exec import _handle_review_skill

    h = late_skill
    (h.skill_dir / "pkg").mkdir()
    for i in range(800):  # an index that lands neither pasted nor inline, read as one stored source
        (h.skill_dir / "pkg" / f"mod_{i:04d}.py").write_text("V = 1\n", encoding="utf-8")
    content_hash = compute_content_hash(h.skill_dir)
    h.delivery.update(retrieves=[True, True], use_local=[False, False])
    monkeypatch.setattr(review_native_episode, "native_landing_at", lambda bound: 45_000)
    monkeypatch.setattr(skill_review, "_review_wave_budget_block", lambda *a, **kw: None)
    sent, reads, lock, reading = {}, [], threading.Lock(), threading.Event()

    class PagedReader:
        def chat(self, **kwargs):
            invoke_bound_api_review_paid_stamp()
            h.calls.append(kwargs)
            tool = [m["content"] for m in kwargs["messages"] if m.get("role") == "tool"]
            if not tool:
                opening = _user_text(kwargs["messages"][-1]["content"])
                path = re.search(r'read_file\(root="artifact_store", path="([^"]+)"', opening).group(1)
                sent.update(path=path, sha256=re.search(r"sha256 `([0-9a-f]{64})`", opening).group(1))
                return {"tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file",
                        "arguments": json.dumps({"root": "artifact_store", "path": path, "max_lines": 40})}}]}, {
                    "prompt_tokens": 1, "completion_tokens": 1}
            with lock:
                reads.append(tool[0])
                if len(reads) == 2:
                    reading.set()
            assert h.release.wait(60)  # the seat outlives its lifecycle's logical window
            return {"content": h.text}, {"prompt_tokens": 2, "completion_tokens": 1}

    monkeypatch.setattr("ouroboros.tools.review.LLMClient", PagedReader)
    # Expire only after BOTH real paged reads reach their held reply. A fixed
    # two-second window races source IO and can expire before the second send.
    now = custody.monotonic_now
    def clock(slot=None):
        return now(slot) + (60 if reading.is_set() else 0)

    monkeypatch.setattr(custody, "monotonic_now", clock)
    monkeypatch.setattr(review_native_episode, "monotonic_now", clock)
    monkeypatch.setattr(custody, "_logical_timeout", lambda *_: 30)
    # A virtual clock jump must wake the queue wait to observe that expiry.
    monkeypatch.setattr(review_pause, "drain_slice", lambda _operation, remaining: min(.02, remaining))
    _handle_review_skill(h.ctx(), skill="late")
    assert load_review_state(h.root, "late").status == "pending"
    assert reading.wait(10) and all("pkg/mod_0000.py:" in read and content_hash in read for read in reads)
    stored = next((h.root / "task_results").rglob("skill-payload-index-*.txt"))
    views = h.root / "state" / "review_checkouts" / "views"
    (reader,) = [path / "skills" / "late" for path in views.iterdir()]
    h.release.set()
    assert h.settled.wait(10)
    for transition in ("settled", "collected"):
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == sent["sha256"]
        assert compute_content_hash(reader) == content_hash and not (reader / ".ouroboros_env").exists()
        if transition == "settled":
            _handle_review_skill(h.ctx(), skill="late")  # the cold resume collects; it never re-sends
    assert load_review_state(h.root, "late").status == "clean"
    assert len(h.calls) == 4 and count_paid_skill_review_cycles(h.root, "late", "task:skill-root:late") == 1
