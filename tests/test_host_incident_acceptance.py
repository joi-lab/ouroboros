"""#1572: the EMITTED terminal delivery is the historical acceptance subject.

The real skip seed, the real terminal projector, the real result store and the
real late-acceptance consumer run here. No reviewer, model or paid panel: the
historical preparation owner is replaced by a recorder, so the assertion is
whether automatic/explicit late acceptance reaches it at all.
"""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from ouroboros.acceptance_history import read_acceptance_history
from ouroboros.agent_task_pipeline import _store_task_result
from ouroboros.headless import copy_child_task_result
from ouroboros.task_results import load_task_result
from supervisor import events_chat_delivery as chat
from supervisor.terminal_delivery import delivery_id_for, project_terminal_result_event, register_pending_delivery
from tests.review_pool_rosters import set_review_pool
from tests.test_acceptance_history import _caller, _fixture, _request, _source
from tests.test_review_operation_collection import _send_ctx, fresh_sends as fresh_sends  # noqa: F401

RAW = "A genuine but unfinished model draft, preserved before the provider died."


@pytest.fixture(autouse=True)
def _review_policy(monkeypatch, fresh_sends):
    set_review_pool(monkeypatch)
    monkeypatch.setenv("OUROBOROS_TASK_REVIEW_MODE", "auto")
    monkeypatch.setattr("ouroboros.llm.LLMClient.chat",
                        lambda *_a, **_k: pytest.fail("late acceptance must not send a model request here"))


def _projected(f, raw, origin):
    """What the host actually sends: the real terminal projector over the producer's origin."""
    base = {"type": "send_message", "chat_id": 0, "task_id": f.tid, "text": raw, "log_text": raw, "format": "markdown"}
    return project_terminal_result_event(f.root, f.task, f.tid, result_text=raw, terminal_origin=origin, base_event=base)


def _store(f, raw, usage, event):
    _store_task_result(SimpleNamespace(drive_root=f.worker, repo_dir=f.tmp_path), f.task, raw, usage, f.trace,
                       review_evidence={"task_inputs": {"owner_words": "Original owner corpus"}}, final_delivery=event)
    copy_child_task_result(f.root, {"id": f.tid, "drive_root": str(f.worker)})
    return load_task_result(f.root, f.tid, strict=True)


def test_the_projector_and_the_authorship_rule_agree_on_what_was_emitted(tmp_path):
    from ouroboros.acceptance_history import (
        DELIVERY_AUTHOR_HOST, DELIVERY_AUTHOR_MODEL, emitted_delivery_authorship, host_incident_receipt,
    )

    f = _fixture(tmp_path, split=True)
    salvage = _projected(f, RAW, "host_salvage")
    assert salvage["role"] == "system" and salvage["system_type"] == "terminal_incident" and salvage["text"] != RAW
    notice = _projected(f, RAW, "host_notice")
    assert notice["role"] == "system" and notice["text"] == RAW and "system_type" not in notice
    final = _projected(f, RAW, "model_final")
    assert final["text"] == RAW and "role" not in final
    assert emitted_delivery_authorship(salvage, {"_best_effort_extracted": True}) == DELIVERY_AUTHOR_HOST
    assert emitted_delivery_authorship(notice, {"_best_effort_extracted": True}) == DELIVERY_AUTHOR_MODEL
    assert emitted_delivery_authorship(notice, {}) == DELIVERY_AUTHOR_HOST
    assert emitted_delivery_authorship(final, {}) == DELIVERY_AUTHOR_MODEL
    # Presence renders its own body: the producer's usage origin still speaks.
    assert emitted_delivery_authorship({"text": RAW}, {"terminal_origin": "host_salvage"}) == DELIVERY_AUTHOR_HOST
    # No stamped origin anywhere: unknown, never inferred from the text.
    assert emitted_delivery_authorship({"text": "⚠️ looks like an incident"}, {}) == ""
    assert host_incident_receipt({"system_type": "terminal_incident"})
    assert host_incident_receipt({"terminal_origin": "host_salvage"})
    assert not host_incident_receipt({"terminal_origin": "host_notice", "role": "system"})
    assert not host_incident_receipt({"text": "⚠️ The context exceeded the selected model window"})


@pytest.mark.parametrize("case", ["host_salvage", "host_notice_host_words", "host_notice_model_text",
                                  "model_final", "legacy_unstamped"])
def test_only_a_model_authored_emitted_delivery_leaves_an_acceptance_debt(tmp_path, case):
    f = _fixture(tmp_path, split=True)
    assert f.trace["acceptance_history_seed"]["cause"] == "acceptance_skipped_budget"
    if case == "host_salvage":
        usage, event = {"terminal_origin": "host_salvage", "_best_effort_extracted": True}, _projected(f, RAW, "host_salvage")
    elif case == "host_notice_host_words":
        words = "⚠️ Round limit reached; nothing further was produced."
        usage, event = {"terminal_origin": "host_notice"}, _projected(f, words, "host_notice")
    elif case == "host_notice_model_text":
        usage, event = {"terminal_origin": "host_notice", "_best_effort_extracted": True}, _projected(f, RAW, "host_notice")
    elif case == "model_final":
        usage, event = {"terminal_origin": "model_final"}, _projected(f, RAW, "model_final")
    else:
        usage, event = {}, {"delivery_id": "delivery-exact", "chat_id": 0, "text": RAW}
    row = _store(f, event["text"] if case == "host_notice_host_words" else RAW, usage, event)
    if case in {"host_salvage", "host_notice_host_words"}:
        assert "acceptance_debt" not in row and "acceptance_history_gap" not in row
        assert row["terminal_origin"] == usage["terminal_origin"]  # the record of why nothing is owed
        if case == "host_salvage":
            assert row["result"] == RAW  # the raw partial stays with the result, never the subject
        return
    debt = row["acceptance_debt"]
    assert debt["delivery"]["text_sha256"] == hashlib.sha256(event["text"].encode()).hexdigest()
    frozen = read_acceptance_history(f.root, f.tid, debt)
    assert frozen["answer"] == RAW


def _deliver(f, monkeypatch, event, *, legacy):
    """Register and send the exact event through the real sender and delivered-handoff.

    ``legacy``: the debt was recorded by a producer that did not look at what it
    emitted (the rule did not exist); the send receipt still retains the facts.
    """
    from ouroboros import acceptance_history, review_operation
    from supervisor import queue as task_queue

    if legacy:
        with monkeypatch.context() as before_rule:
            before_rule.setattr(acceptance_history, "emitted_delivery_authorship", lambda *_a, **_k: "")
            row = _store(f, RAW, {}, event)
    else:
        row = _store(f, RAW, {}, event)
    assert row["acceptance_debt"]["delivery"]["text_sha256"] == hashlib.sha256(event["text"].encode()).hexdigest()
    monkeypatch.setattr(task_queue, "INITIALIZED", True)
    monkeypatch.setattr(task_queue, "DRIVE_ROOT", f.root)
    monkeypatch.setattr(chat, "_bound_project_chat_id", lambda *_a: 0)
    prepared: list = []
    monkeypatch.setattr(review_operation, "prepare_historical_operation",
                        lambda **kwargs: prepared.append(kwargs) or {"status": "prepared", "recorder": True})
    assert register_pending_delivery(f.root, event)
    sends: list = []
    chat._handle_send_message(event, _send_ctx(f.root, sends))
    assert sends == [(0, event["text"])]
    return row["acceptance_debt"], prepared


def test_a_legacy_debt_over_the_host_incident_receipt_refuses_at_consumption(tmp_path, monkeypatch):
    """The incident: three reviewers read 'The context exceeded the selected model window'."""
    from ouroboros.acceptance_late import run_historical_acceptance

    f = _fixture(tmp_path, split=True)
    event = _projected(f, RAW, "host_salvage")
    debt, prepared = _deliver(f, monkeypatch, event, legacy=True)
    assert prepared == [], "automatic late acceptance must not prepare over the host's receipt"
    ctx = _caller(f)
    automatic = run_historical_acceptance(ctx, task_id=f.tid, debt_id=debt["debt_id"], automatic=True)
    assert automatic == {"status": "owed", "reason": "host_incident_receipt_not_a_delivery",
                         "dispatched": False, "task_id": f.tid, "debt_id": debt["debt_id"]}
    explicit = _request(f, ctx, _source(ctx, text="Review this historical answer."))
    assert explicit["status"] == "owed" and explicit["reason"] == "host_incident_receipt_not_a_delivery"
    assert explicit["dispatched"] is False
    assert prepared == []
    assert load_task_result(f.root, f.tid)["acceptance_debt"] == debt  # no history rewrite


@pytest.mark.parametrize("origin", ["model_final", "host_notice", ""])
def test_a_legacy_debt_over_visible_or_unknown_text_still_reaches_preparation(tmp_path, monkeypatch, origin):
    """Source-proven host receipts refuse; a model answer, a host notice (unknown
    authorship on an old row) or an unstamped legacy send are not reclassified."""
    from ouroboros.acceptance_late import run_historical_acceptance

    f = _fixture(tmp_path, split=True)
    event = (_projected(f, RAW, origin) if origin
             else {"delivery_id": delivery_id_for(f.tid, RAW), "chat_id": 0, "task_id": f.tid, "text": RAW})
    debt, prepared = _deliver(f, monkeypatch, event, legacy=True)
    assert len(prepared) == 1 and prepared[0]["purpose"]["debt_id"] == debt["debt_id"]
    assert prepared[0]["purpose"]["automatic"] is True
    ctx = _caller(f)
    repeated = run_historical_acceptance(ctx, task_id=f.tid, debt_id=debt["debt_id"], automatic=True)
    assert repeated.get("reason") != "host_incident_receipt_not_a_delivery"


@pytest.mark.parametrize("rail", ["stale_candidate_non_provider", "stale_candidate_provider", "pure_host_fallback"])
def test_the_real_forced_rails_and_projector_decide_the_debt(tmp_path, rail):
    """No hand-filled usage: the loop's own forced fallback stamps origin and the
    extraction fact, the real terminal projector decides what is emitted, and the
    real producer decides the debt. Mirrors the root counterexample: a stale
    retained model answer goes out as ``host_notice`` on a non-provider rail
    (a delivery) but as the ``host_salvage`` System receipt on a provider rail."""
    from ouroboros.acceptance_history import retain_acceptance_history
    from ouroboros.task_finalization import prepare_terminal_send_event
    from ouroboros.utils import utc_now_iso
    from tests.test_delivery_forced_finalization import _forced_test_context
    from tests.test_loop_acceptance_gate import _nominate_acceptance

    loop, registry, ctx, trace = _forced_test_context(tmp_path)
    # The rail itself stamps the typed failure before composing (the forced-bypass
    # recorder seeds history only for a rail that terminalized with a reason).
    ctx.accumulated_usage.update(execution_status="failed", reason_code="round_limit")
    answer = "A complete model-authored answer retained before newer evidence."
    _nominate_acceptance(trace, task_id="parent1", subject=answer)  # the owed panel (#1731)
    host_words = "⚠️ Round limit reached; nothing further was produced."
    if rail.startswith("stale_candidate"):
        old = loop._replace_delivery_candidate(registry, ctx, trace, answer, control="replace")
        loop._publish_delivery_candidate(registry, old, trace)
        registry._ctx._owner_directives = [{"content": "A newer constraint awaiting consideration"}]
        text, usage, returned = loop._forced_fallback_result(
            ctx, trace, answer, "round_limit", provider_terminal=rail.endswith("provider") and not rail.endswith("non_provider"))
        assert text == answer and usage["_best_effort_extracted"] is True
        assert returned["forced_finalization"]["source"].endswith("stale_evidence_resume_required")
    else:
        text, usage, _returned = loop._forced_fallback_result(ctx, trace, host_words, "round_limit")
        assert text == host_words and "_best_effort_extracted" not in usage
    assert trace["acceptance_history_seed"]["cause"] == "acceptance_bypassed_round_limit"
    task = {"id": "parent1", "chat_id": 1, "root_task_id": "parent1", "delegation_role": "root",
            "budget_drive_root": str(tmp_path), "task_contract": registry._ctx.task_contract}
    send_event = {"type": "send_message", "chat_id": 1, "task_id": "parent1", "text": text, "log_text": text,
                  "format": "markdown", "ts": utc_now_iso()}
    event = prepare_terminal_send_event(tmp_path, task, text, usage, send_event, presence=False)
    fields = retain_acceptance_history(tmp_path, task, text, trace, {"task_inputs": {"owner_words": "x"}},
                                       {}, [], event, usage=usage)
    if rail == "stale_candidate_non_provider":
        assert usage["terminal_origin"] == "host_notice" and event["role"] == "system" and event["text"] == answer
        frozen = read_acceptance_history(tmp_path, "parent1", fields["acceptance_debt"])
        assert frozen["answer"] == answer  # the visible model answer is the subject
    elif rail == "stale_candidate_provider":
        assert usage["terminal_origin"] == "host_salvage" and event["system_type"] == "terminal_incident"
        assert event["text"] != answer and fields == {}  # the receipt is not a delivery; raw text stays in `text`
        assert usage["terminal_salvage_path"]
    else:
        assert usage["terminal_origin"] == "host_notice" and event["text"] == host_words and fields == {}
