"""#1333: ``pr_merge`` keeps reviewed, current and merged identities apart.

Drives the real tool handler over a stateful fake ``gh`` (no network): the intent
row lands before the merge effect, a missing review is loud but never a lock,
queued/auto-merge is not "merged", an unknown outcome is only read back, a failed
publication never repeats the merge, and the public PR-body block carries no
private identifiers while every unrelated byte of the body survives.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ouroboros import merge_receipts
from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, load_task_result, write_task_result
from ouroboros.tools import github

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
TREE = "d" * 40
URL = "https://github.com/octo/demo/pull/7"
CHECKLIST = json.dumps([{"item": item, "verdict": "PASS", "severity": "advisory",
                         "reason": f"Checked {item} against the touched module and its tests."}
                        for item in sorted(__import__("ouroboros.tools.scope_review_contract",
                                                      fromlist=["x"]).SCOPE_REQUIRED_ITEMS)])


class FakeGh:
    """A PR that merges (or not) the way GitHub answers; records every call."""

    def __init__(self, drive_root, *, merge="ok", body="Summary of the change.\n\nOwner notes stay here."):
        self.drive_root, self.merge, self.calls = drive_root, merge, []
        self.pr = {"number": 7, "state": "OPEN", "url": URL, "title": "demo", "body": body, "comments": [],
                   "headRefOid": HEAD, "baseRefName": "main", "baseRefOid": BASE, "mergeStateStatus": "CLEAN",
                   "isDraft": False, "autoMergeRequest": None, "mergeCommit": None, "mergedAt": None}
        self.fail_view_after_merge = False
        self.fail_edit = False
        self.intent_seen_at_merge = None

    def __call__(self, args, ctx, timeout=30, input_data=None, *, repo=github._GENERIC_TRANSPORT):
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            if self.fail_view_after_merge and any(c[:2] == ["pr", "merge"] for c in self.calls):
                return github.GhResult(False, "⚠️ GH_ERROR: boom", 1, 502, "exit")
            return github.GhResult(True, json.dumps(self.pr), 0, None, "")
        if args[:2] == ["pr", "merge"]:
            row = load_task_result(self.drive_root, "merge-task") or {}
            self.intent_seen_at_merge = [r.get("state") for r in row.get("merge_receipts") or []]
            if self.merge == "ok":
                self.pr.update(state="MERGED", mergeCommit={"oid": MERGE}, mergedAt="2026-09-26T00:00:00Z")
                return github.GhResult(True, "merged", 0, None, "")
            if self.merge == "queued":
                self.pr.update(autoMergeRequest={"enabledAt": "now"})
                return github.GhResult(True, "queued", 0, None, "")
            if self.merge == "accepted_open":  # a merge queue took it; the PR stays open
                return github.GhResult(True, "added to merge queue", 0, None, "")
            if self.merge == "timeout_merged":
                self.pr.update(state="MERGED", mergeCommit={"oid": MERGE})
                return github.GhResult(False, "⚠️ GH_TIMEOUT: exceeded 120s.", None, None, "timeout")
            if self.merge == "timeout_open":
                return github.GhResult(False, "⚠️ GH_TIMEOUT: exceeded 120s.", None, None, "timeout")
            return github.GhResult(False, "⚠️ GH_ERROR: pre-effect refusal", 1, None, "pre_effect")
        if args[:2] == ["pr", "edit"]:
            return github.GhResult(False, "GraphQL: The 'login' field requires 'read:org' scope", 1, None, "exit")
        if args[:1] == ["api"] and "PATCH" in args:
            if self.fail_edit:
                return github.GhResult(False, "⚠️ GH_ERROR: forbidden", 1, 403, "exit")
            self.pr["body"] = json.loads(input_data)["body"]
            return github.GhResult(True, "", 0, None, "")
        if args[:1] == ["api"]:
            return github.GhResult(True, json.dumps({"commit": {"tree": {"sha": TREE}},
                                                     "parents": [{"sha": BASE}, {"sha": HEAD}]}), 0, None, "")
        raise AssertionError(f"unexpected gh call {args}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    write_task_result(tmp_path, "merge-task", STATUS_RUNNING, root_task_id="merge-task", delegation_role="root")
    write_task_result(tmp_path, "review-1", STATUS_COMPLETED, root_task_id="merge-task", result="PASS")
    fake = FakeGh(tmp_path)
    monkeypatch.setattr(github, "_gh_run", fake)
    rows = []
    ctx = SimpleNamespace(task_id="merge-task", drive_root=tmp_path, task_metadata={}, repo_dir=tmp_path,
                          current_chat_id=7, pending_events=[],
                          emit_progress_fn=lambda text, **kw: rows.append((text, kw)))
    return SimpleNamespace(ctx=ctx, gh=fake, rows=rows, root=tmp_path)


def _merge(world, **kw):
    args = {"number": 7, "expected_head_sha": HEAD, "method": "squash", **kw}
    return github._pr_merge(world.ctx, **args)


def _receipts(world):
    return (load_task_result(world.root, "merge-task") or {}).get("merge_receipts") or []


def test_a_reviewed_merge_records_intent_first_and_publishes_one_receipt(world):
    out = _merge(world, review_task_ids=["review-1"], reviewed_head_sha=HEAD, reviewed_base_sha=BASE,
                 review_verdict="PASS")
    assert world.gh.intent_seen_at_merge == ["intent_recorded"]  # written before the effect
    assert ["pr", "merge", "7", "--squash", "--match-head-commit", HEAD] in world.gh.calls
    assert not any("--auto" in c or "--admin" in c for c in world.gh.calls)
    (receipt,) = _receipts(world)
    assert receipt["outcome"] == {**receipt["outcome"], "status": "merged", "merge_sha": MERGE, "merge_tree": TREE,
                                  "merge_parents": [BASE, HEAD], "attribution": "this_call"}
    assert receipt["coverage"]["status"] == "covers_head" and receipt["coverage"]["gaps"] == []
    assert receipt["review"]["host_observed"][0]["status"] == "completed"
    assert receipt["publication"]["body"] == {"status": "published"}
    assert receipt["publication"]["card"]["status"] == "owed"
    assert world.ctx.pending_events[0]["progress_meta"]["card_row_id"] == f"merge-receipt:{receipt['receipt_id']}"
    assert world.rows == []  # callback return has no delivery authority
    assert out.startswith("✅ PR #7 merge: merged")
    body = world.gh.pr["body"]
    assert body.startswith("Summary of the change.\n\nOwner notes stay here.")  # unrelated text kept
    assert "review-1" not in body and str(world.root) not in body and "merge-task" not in body


def test_an_unreviewed_merge_is_loud_and_still_merges(world):
    out = _merge(world)
    (receipt,) = _receipts(world)
    assert receipt["outcome"]["status"] == "merged" and receipt["coverage"]["status"] == "not_declared"
    assert out.startswith("⚠️") and "no_review_declared" in out
    assert "No review was declared" in world.gh.pr["body"]


@pytest.mark.parametrize(("kw", "status"), [
    ({"reviewed_head_sha": "e" * 40}, "changes_after_review"),
    ({"reviewed_head_sha": HEAD, "review_scope": "delta"}, "delta_only"),
    ({"review_task_ids": ["missing-review"]}, "unknown"),
])
def test_coverage_never_claims_more_than_was_declared(world, kw, status):
    _merge(world, **kw)
    assert _receipts(world)[0]["coverage"]["status"] == status


def test_a_moved_head_or_closed_pr_merges_nothing_and_writes_nothing(world):
    world.gh.pr["headRefOid"] = "f" * 40
    assert "head_moved" in _merge(world)
    world.gh.pr.update(headRefOid=HEAD, state="CLOSED")
    assert "pr_not_open" in _merge(world)
    assert not any(c[:2] == ["pr", "merge"] for c in world.gh.calls) and _receipts(world) == []


@pytest.mark.parametrize(("merge", "reason"), [("queued", "auto_merge_pending"), ("accepted_open", "accepted_still_open")])
def test_queued_is_not_merged(world, merge, reason):
    world.gh.merge = merge
    _merge(world, reviewed_head_sha=HEAD)
    outcome = _receipts(world)[0]["outcome"]
    assert outcome == {"status": "queued", "reason": reason}


def test_an_unknown_outcome_is_always_read_back_without_a_retry_escape(world):
    world.gh.merge = "timeout_open"
    assert "PR_MERGE_UNKNOWN" in _merge(world)
    merges = lambda: sum(1 for c in world.gh.calls if c[:2] == ["pr", "merge"])  # noqa: E731
    assert merges() == 1
    out = _merge(world)
    assert "sent no new merge request" in out and merges() == 1
    world.gh.merge = "ok"
    _merge(world)
    assert merges() == 1 and len(_receipts(world)) == 1
    schema = next(e.schema for e in github.get_tools() if e.name == "pr_merge")
    assert "retry_after_unknown" not in schema["parameters"]["properties"]


def test_a_merge_someone_finished_while_the_answer_was_lost_is_unattributed(world):
    world.gh.merge = "timeout_merged"
    _merge(world, reviewed_head_sha=HEAD)
    outcome = _receipts(world)[0]["outcome"]
    assert outcome["status"] == "merged" and outcome["attribution"] == "unproven"
    _merge(world, reviewed_head_sha=HEAD)
    assert _receipts(world)[0]["outcome"]["attribution"] == "unproven"


def test_a_failed_readback_is_unknown(world):
    world.gh.fail_view_after_merge = True
    assert "PR_MERGE_UNKNOWN" in _merge(world)
    assert _receipts(world)[0]["outcome"] == {"status": "unknown", "reason": "readback_failed"}


def test_an_unwritable_intent_refuses_before_any_effect(world, monkeypatch):
    monkeypatch.setattr(merge_receipts, "write_receipt", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("lock")))
    assert "merge_intent_unwritable" in _merge(world)
    assert not any(c[:2] == ["pr", "merge"] for c in world.gh.calls)


def test_a_failed_publication_is_a_gap_and_a_retry_never_merges_again(world):
    world.gh.fail_edit = True
    out = _merge(world, reviewed_head_sha=HEAD)
    receipt = _receipts(world)[0]
    assert receipt["outcome"]["status"] == "merged" and receipt["publication"]["body"]["status"] == "gap"
    assert receipt["outcome"]["attribution"] == "this_call"
    assert "not confirmed" in out
    world.gh.fail_edit = False
    out = _merge(world, reviewed_head_sha=HEAD)
    assert "sent no new merge request" in out
    assert sum(1 for c in world.gh.calls if c[:2] == ["pr", "merge"]) == 1
    assert _receipts(world)[0]["publication"]["body"]["status"] == "published"
    assert _receipts(world)[0]["receipt_id"] == receipt["receipt_id"]
    assert _receipts(world)[0]["outcome"] == receipt["outcome"]


@pytest.mark.parametrize("failure", ["readback_unavailable", "missing_block"])
def test_successful_body_patch_needs_readback_and_recovers_without_second_merge(world, monkeypatch, failure):
    original = world.gh
    description = world.gh.pr["body"]
    patch_seen = False

    def gh(args, *a, **kw):
        nonlocal patch_seen
        if patch_seen and args[:2] == ["pr", "view"] and failure == "readback_unavailable":
            return github.GhResult(False, "readback unavailable", 1, 502, "exit")
        result = original(args, *a, **kw)
        if args[:1] == ["api"] and "PATCH" in args:
            patch_seen = True
            if failure == "missing_block":
                world.gh.pr["body"] = description
        return result

    monkeypatch.setattr(github, "_gh_run", gh)
    assert "not confirmed" in _merge(world)
    (before,) = _receipts(world)
    assert before["outcome"]["status"] == "merged"
    assert before["publication"]["body"] == {"status": "gap", "reason": "readback_missing_block"}
    assert before["publication"]["card"]["status"] == "owed"
    monkeypatch.setattr(github, "_gh_run", original)
    assert "sent no new merge request" in _merge(world)
    (after,) = _receipts(world)
    assert after["publication"]["body"] == {"status": "published"}
    assert all(after[key] == before[key] for key in ("receipt_id", "revision", "outcome", "effect"))
    assert after["publication"]["card"] == before["publication"]["card"]
    assert world.gh.pr["body"] == description + "\n\n" + merge_receipts.public_block(after) + "\n"
    assert sum(c[:2] == ["pr", "merge"] for c in original.calls) == 1
    assert sum(c[:1] == ["api"] and "PATCH" in c for c in original.calls) == (2 if failure == "missing_block" else 1)
    assert not any(c[:2] == ["pr", "edit"] for c in original.calls)


@pytest.mark.parametrize("url", ["", "unknown", "https://github.com/octo/demo", URL + "junk",
                                 "https://github.com/octo/demo/pull/8"])
def test_unknown_body_target_is_an_explicit_gap_without_default_repo_write(world, url):
    world.gh.pr["url"] = url
    description = world.gh.pr["body"]
    assert "not confirmed" in _merge(world)
    (receipt,) = _receipts(world)
    assert receipt["outcome"]["status"] == "merged"
    assert receipt["publication"]["body"] == {"status": "gap", "reason": "target_unavailable"}
    assert receipt["publication"]["card"]["status"] == "owed"
    assert world.gh.pr["body"] == description
    assert not any(c[:2] == ["pr", "edit"] or "PATCH" in c for c in world.gh.calls)


def test_the_body_block_replaces_an_older_receipt_and_the_checklist_is_recognized(world):
    world.gh.pr["body"] = f"Intro.\n\n```json\n{CHECKLIST}\n```\n\nFooter."
    _merge(world, reviewed_head_sha=HEAD)
    assert _receipts(world)[0]["review"]["contributor_evidence"] == "present_validated"
    first_body = world.gh.pr["body"]
    updated = merge_receipts.upsert_body(first_body, "<!-- ouroboros:merge-receipt ab -->\nnew\n<!-- /ouroboros:merge-receipt -->")
    assert updated.count("<!-- ouroboros:merge-receipt ") == 1 and "new" in updated and "Footer." in updated and "Intro." in updated
    assert merge_receipts.contributor_evidence("```json\n[{\"item\": \"x\"}]\n```", []) == "present_invalid"
    assert merge_receipts.contributor_evidence("no checklist", []) == "absent"


def test_pr_merge_is_registered_with_an_explicit_safety_policy():
    from ouroboros.safety import POLICY_CHECK, TOOL_POLICY

    names = {entry.name for entry in github.get_tools()}
    assert "pr_merge" in names and TOOL_POLICY["pr_merge"] == POLICY_CHECK
    schema = next(e.schema for e in github.get_tools() if e.name == "pr_merge")
    assert set(schema["parameters"]["required"]) == {"number", "expected_head_sha", "method"}
    assert schema["parameters"]["properties"]["method"]["enum"] == ["merge", "squash", "rebase"]


def test_queue_recovery_observes_the_same_operation_and_recomputes_changed_subject(world):
    world.gh.merge = "accepted_open"
    _merge(world, review_task_ids=["review-1"], reviewed_head_sha=HEAD, reviewed_base_sha=BASE)
    rid = _receipts(world)[0]["receipt_id"]
    _merge(world)
    assert _receipts(world)[0]["outcome"]["status"] == "queued"
    world.gh.pr.update(state="MERGED", headRefOid="e" * 40, mergeCommit={"oid": MERGE})
    _merge(world)
    (receipt,) = _receipts(world)
    assert receipt["receipt_id"] == rid
    assert receipt["outcome"]["attribution"] == "unproven"
    assert receipt["coverage"]["status"] == "changes_after_review"
    assert "merged_head_differs" in receipt["coverage"]["gaps"]
    assert sum(c[:2] == ["pr", "merge"] for c in world.gh.calls) == 1
    assert merge_receipts.card_row_text(receipt).startswith("⚠️")


def test_base_and_tree_gaps_prevent_green_without_vetoing_merge(world, monkeypatch):
    original = world.gh

    def gh(args, *a, **kw):
        if args[:1] == ["api"] and "/commits/" in args[1]:
            is_merge = args[1].endswith(MERGE)
            return github.GhResult(True, json.dumps({"commit": {"tree": {"sha": TREE if is_merge else "f" * 40}},
                                                      "parents": [{"sha": "e" * 40}]}), 0, None, "")
        return original(args, *a, **kw)

    monkeypatch.setattr(github, "_gh_run", gh)
    _merge(world, review_task_ids=["review-1"], reviewed_head_sha=HEAD, reviewed_base_sha=BASE)
    r = _receipts(world)[0]
    assert r["outcome"]["status"] == "merged"
    assert r["coverage"]["status"] == "unknown"
    assert set(r["coverage"]["gaps"]) == {"base_changed_since_review", "merged_tree_differs_from_reviewed_head"}


def test_parallel_callers_share_one_atomic_intent(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock

    barrier, lock = Barrier(2), Lock()
    original, views = world.gh, 0

    def gh(args, *a, **kw):
        nonlocal views
        result = original(args, *a, **kw)
        if args[:2] == ["pr", "view"]:
            with lock:
                views += 1
                initial = views <= 2
            if initial:
                barrier.wait(timeout=5)  # both see OPEN before either claims
        return result

    monkeypatch.setattr(github, "_gh_run", gh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: _merge(world), range(2)))
    assert sum(c[:2] == ["pr", "merge"] for c in world.gh.calls) == 1
    assert len(_receipts(world)) == 1


@pytest.mark.serial
@pytest.mark.parametrize(("mode", "status"), [("ok", "merged"), ("accepted_open", "queued"), ("refused", "refused")])
def test_losing_claim_cannot_erase_the_winners_effect_or_settled_outcome(world, monkeypatch, mode, status):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event, local

    world.gh.merge = mode
    initial_reads, loser_claimed, winner_done = Barrier(2), Event(), Event()
    caller = local()
    read, write, gh = merge_receipts.task_receipts, merge_receipts.write_receipt, world.gh
    winner = {}

    def read_together(*args):
        rows = read(*args)
        initial_reads.wait(timeout=5)  # both decide to claim before either writes
        return rows

    def claim_then_delay(*args, **kwargs):
        selected = write(*args, **kwargs)
        if kwargs.get("claim"):
            caller.won = selected["receipt_id"] == args[2]["receipt_id"]
            if not caller.won:
                loser_claimed.set()  # retain the intent snapshot until the winner finishes
                assert winner_done.wait(timeout=5)
        return selected

    def effect(*args, **kwargs):
        if args[0][:2] == ["pr", "merge"]:
            assert loser_claimed.wait(timeout=5)
        return gh(*args, **kwargs)

    def run():
        try:
            result = _merge(world, review_task_ids=["review-1"], reviewed_head_sha=HEAD, reviewed_base_sha=BASE)
            if caller.won:
                winner.update(_receipts(world)[0])
            return result
        finally:
            if getattr(caller, "won", False):
                winner_done.set()

    monkeypatch.setattr(merge_receipts, "task_receipts", read_together)
    monkeypatch.setattr(merge_receipts, "write_receipt", claim_then_delay)
    monkeypatch.setattr(github, "_gh_run", effect)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(lambda _: run(), range(2)))
    (stored,) = _receipts(world)
    assert stored["effect"] == winner["effect"]
    assert stored["outcome"] == winner["outcome"]
    assert stored["coverage"] == winner["coverage"]
    assert stored["outcome"]["status"] == status and stored["state"] == "settled"
    assert all(f"merge: {status}" in text for text in outputs)
    assert sum(c[:2] == ["pr", "merge"] for c in world.gh.calls) == 1
    if status != "refused":
        assert f"Outcome: **{status}**" in world.gh.pr["body"]
        assert all(f"merge: {status}" in event["text"] for event in world.ctx.pending_events)


@pytest.mark.serial
def test_delayed_queue_publication_catches_up_to_the_persisted_merge(world, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    queued_edit, merged_published = Event(), Event()
    gh = world.gh
    world.gh.merge = "accepted_open"

    def delayed_edit(args, *a, **kw):
        if args[:1] == ["api"] and "PATCH" in args and "Outcome: **queued**" in kw["input_data"]:
            queued_edit.set()
            assert merged_published.wait(timeout=5)
        return gh(args, *a, **kw)

    monkeypatch.setattr(github, "_gh_run", delayed_edit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(_merge, world)
        try:
            assert queued_edit.wait(timeout=5)
            world.gh.pr.update(state="MERGED", mergeCommit={"oid": MERGE})
            assert "merge: merged" in _merge(world)
            settled = _receipts(world)[0]
        finally:
            merged_published.set()
        assert "merge: merged" in first.result(timeout=5)
    (stored,) = _receipts(world)
    assert stored["effect"] == settled["effect"] and stored["outcome"] == settled["outcome"]
    assert stored["outcome"]["attribution"] == "unproven"
    assert merge_receipts.public_block(stored) in world.gh.pr["body"]
    assert stored["publication"]["body"]["status"] == "published"
    assert "merge: merged" in world.ctx.pending_events[-1]["text"]
    assert sum(c[:2] == ["pr", "merge"] for c in world.gh.calls) == 1


def test_retry_enriches_merge_readback_without_erasing_attribution(world, monkeypatch):
    gh = world.gh

    def unavailable_commit(args, *a, **kw):
        if args[:1] == ["api"] and "/commits/" in args[1]:
            return github.GhResult(False, "unavailable", 1, 502, "exit")
        return gh(args, *a, **kw)

    monkeypatch.setattr(github, "_gh_run", unavailable_commit)
    _merge(world, review_task_ids=["review-1"], reviewed_head_sha=HEAD, reviewed_base_sha=BASE)
    initial = _receipts(world)[0]
    assert initial["coverage"]["status"] == "unknown"
    monkeypatch.setattr(github, "_gh_run", gh)
    assert _merge(world).startswith("✅")
    (stored,) = _receipts(world)
    assert stored["receipt_id"] == initial["receipt_id"]
    assert stored["outcome"]["attribution"] == "this_call"
    assert stored["outcome"]["merge_tree"] == TREE
    assert stored["coverage"]["status"] == "covers_head"
    assert stored["coverage"]["gaps"] == []
    assert merge_receipts.public_block(stored) in world.gh.pr["body"]
    assert sum(c[:2] == ["pr", "merge"] for c in world.gh.calls) == 1


def test_unsettled_and_merged_receipts_survive_disposable_history_eviction(world):
    world.gh.merge = "accepted_open"
    _merge(world)
    rid = _receipts(world)[0]["receipt_id"]
    for i in range(merge_receipts._RECEIPTS_CAP + 2):
        merge_receipts.write_receipt(world.root, "merge-task", {
            "receipt_id": f"other-{i}", "repo": {"url": f"other-{i}"}, "state": "settled",
            "outcome": {"status": "refused"}})
    assert any(r["receipt_id"] == rid for r in _receipts(world))
    _merge(world)
    assert sum(c[:2] == ["pr", "merge"] for c in world.gh.calls) == 1


def test_card_outbox_survives_worker_exit_and_confirms_delivery_separately(world, monkeypatch):
    from queue import Queue
    from supervisor import terminal_delivery as td

    _merge(world)
    receipt = _receipts(world)[0]
    did = receipt["publication"]["card"]["delivery_id"]
    world.ctx.pending_events.clear()  # the worker died before draining its buffer
    monkeypatch.setattr(td, "_REPLAY_MIN_AGE_SEC", 0)
    queue = Queue()
    assert td.replay_pending_deliveries(world.root, event_queue=queue) == [did]
    event = queue.get_nowait()
    assert event["progress_meta"]["card_row_id"] == f"merge-receipt:{receipt['receipt_id']}"
    td.register_delivery(world.root, did)  # supervisor's post-send seam
    _merge(world)
    assert _receipts(world)[0]["publication"]["card"]["status"] == "delivered"
    assert td.pending_deliveries(world.root) == []


def test_body_uses_fresh_publication_view_and_confirms_a_lost_edit_reply(world, monkeypatch):
    original = world.gh
    concurrent_note = "\n\nNote added while merge was running.  \n"

    def gh(args, *a, **kw):
        result = original(args, *a, **kw)
        if args[:2] == ["pr", "merge"]:
            world.gh.pr["body"] += concurrent_note
        if args[:1] == ["api"] and "PATCH" in args:
            return github.GhResult(False, "lost reply", None, None, "timeout")
        return result

    monkeypatch.setattr(github, "_gh_run", gh)
    _merge(world)
    assert concurrent_note in world.gh.pr["body"]
    assert _receipts(world)[0]["publication"]["body"]["status"] == "published"
    _merge(world)
    assert sum(c[:1] == ["api"] and "PATCH" in c for c in world.gh.calls) == 1
