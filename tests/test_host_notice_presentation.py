"""Owner-facing host notices (#1369): the late-review row, the provider terminal
sentence and the late-evidence carrier, each through its real producer.

The record keeps its tokens — slot ids, PASS/FAIL/DEGRADED, the ``⚠️ OMISSION
NOTE`` marker, the exception repr — in the model mailbox, the typed evidence, the
``llm_api_error`` event and Logs. The row a person reads says the same facts in
words and points at the exact record.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros.acceptance_settlement import (
    _late_settlement_text, acceptance_settlement_message, late_evidence_fact,
)
from ouroboros.loop_transport import provider_failure_hint, provider_terminal_fallback_text


LONG_SUMMARY = ("The budget section is complete and every claim is backed by the attached ledger, "
                "including the two contested rows. ") * 8
REPR = "PermissionDeniedError(\"Error code: 403 - {'error': {'message': 'The API key was revoked.', 'code': 403}}\")"


def _run(**overrides):
    run = {
        "authority": "host_root", "aggregate_signal": "PASS", "panel_id": "panel_1",
        "request": {"surface": "task_acceptance", "subject": "answer A", "retry_key": "rk"},
        "slot_roster": [{"slot_id": "triad_w45a8z", "model": "openai/gpt-5.5", "route": "agent_session"},
                        {"slot_id": "triad_bkydwq", "model": "anthropic/claude-opus-5-5", "route": "api_chat"},
                        {"slot_id": "triad_qq11zz", "model": "claudexor::codex=gpt-6-astra", "route": "agent_session"},
                        {"slot_id": "triad_late01", "model": "x/late", "route": "api_chat"}],
        "actors": [
            {"slot_id": "triad_w45a8z", "model": "openai/gpt-5.5", "status": "ok", "operation_state": "settled",
             # A delegated engine run reports its final attempt's model: an observation.
             "usage": {"resolved_model": "openai/gpt-5.5-2026-09",
                       "observed_attempt": {"harness_id": "codex", "model": "openai/gpt-5.5-2026-09"}},
             "semantic_verdict": "PASS",
             "parsed": {"verdict": "PASS", "summary": LONG_SUMMARY, "findings": []}},
            {"slot_id": "triad_bkydwq", "model": "anthropic/claude-opus-5-5", "status": "ok", "operation_state": "settled",
             "semantic_verdict": "DEGRADED",
             "parsed": {"verdict": "DEGRADED", "summary": "The ledger could not be opened from the packet.", "findings": []}},
            {"slot_id": "triad_qq11zz", "model": "claudexor::codex=gpt-6-astra", "status": "error", "operation_state": "settled",
             "error": "ClaudexorModelError: engine_busy (HTTP 429)", "reported_cause": "The engine is rate limited"},
            {"slot_id": "triad_late01", "model": "x/late", "operation_state": "pending_dispatch"},
        ],
    }
    run.update(overrides)
    return run


def _wave():
    return {"slots": {"triad_w45a8z": "ok", "triad_bkydwq": "ok", "triad_qq11zz": "error", "triad_late01": ""},
            "verdicts": {"triad_w45a8z": {"verdict": "PASS", "note": LONG_SUMMARY[:400] + "\n⚠️ OMISSION NOTE: truncated at 400 chars; original length 872"},
                         "triad_bkydwq": {"verdict": "DEGRADED", "note": "The ledger could not be opened from the packet."},
                         "triad_qq11zz": {"verdict": "", "note": "ClaudexorModelError: engine_busy (HTTP 429)"}},
            "total": 4}


# ---------------------------------------------------------------------------
# The owner row
# ---------------------------------------------------------------------------

def test_owner_row_names_models_and_says_each_outcome_in_words():
    text = _late_settlement_text(_run(), _wave(), {"reviewed_revision": "delivered"})
    head, *lines = text.split("\n")
    assert head == "On the delivered version of this answer, reviewers later passed it."
    # The observed model leads; a requested-only model is labelled; the cause is quoted.
    assert lines[0].startswith("- openai/gpt-5.5-2026-09: passed it — The budget section is complete")
    assert lines[1] == "- anthropic/claude-opus-5-5 (requested): inconclusive — The ledger could not be opened from the packet."
    # The quoted cause is the engine's own ``failure.safeMessage``, attributed to the engine.
    assert lines[2] == '- claudexor::codex=gpt-6-astra (requested): unavailable — the engine reported: "The engine is rate limited"'
    assert lines[3] == "- x/late (requested): still awaited"
    # No slot id, verdict token, marker, failure code or exception text reaches the person.
    for internal in ("triad_", "PASS", "DEGRADED", "OMISSION NOTE", "HTTP 429", "ClaudexorModelError", "pending", "error"):
        assert internal not in "\n".join(lines), internal


def test_owner_note_shortening_is_said_in_words_without_promising_a_record():
    """The note is composed before the panel's record is stored, and that store can
    fail: a reviewer's shortened note says only that it was shortened. The row's
    record link, offered only for a stored record, is the pointer."""
    text = _late_settlement_text(_run(), _wave())
    line = text.split("\n")[1]
    assert line.endswith("… (shortened)")
    assert "review record" not in text and "OMISSION NOTE" not in text
    # The SSOT display bound decides the cut: the same 400-char preview, the same
    # anti-waste floor (a note barely over the limit passes whole).
    barely = "x" * 430
    run = _run(actors=[{**_run()["actors"][0], "parsed": {"verdict": "PASS", "summary": barely}}])
    whole = _late_settlement_text(run, {"slots": {"triad_w45a8z": "ok"}, "verdicts": {}}).split("\n")[1]
    assert whole.endswith(barely) and "shortened" not in whole


def test_an_engine_cause_the_hosts_bound_cut_is_quoted_as_shortened_without_its_marker():
    """``run_failure_cause`` bounds the engine's words with the host's model-facing
    omission marker inside the limit. The owner's quote keeps only the engine's words
    and says the cut in words; the record keeps the marker. Words that merely mention
    such a note are the engine's own and stay quoted as written."""
    from ouroboros.gateways.claudexor import run_failure_cause

    cause = run_failure_cause({"safeMessage": "The reviewer session could not start: its workspace lease expired. " * 9})
    assert "\n⚠️ OMISSION NOTE: truncated at 512 chars" in cause, "the record keeps the disclosed marker"
    actor = {**_run()["actors"][2], "reported_cause": cause}
    wave = {"slots": {"triad_qq11zz": "error"}, "verdicts": {}}
    line = _late_settlement_text(_run(actors=[actor]), wave).split("\n")[1]
    kept = cause.split("\n")[0].strip()
    assert line == f'- claudexor::codex=gpt-6-astra (requested): unavailable — the engine reported: "{kept}…" (shortened)'
    assert actor["reported_cause"] == cause, "the canonical evidence is unchanged"
    mention = run_failure_cause({"safeMessage": "Refused. ⚠️ OMISSION NOTE: truncated at 512 chars; original length 900"})
    quoted = _late_settlement_text(_run(actors=[{**actor, "reported_cause": mention}]), wave).split("\n")[1]
    assert quoted.endswith(f'the engine reported: "{mention}"') and "shortened" not in quoted


def test_the_model_mailbox_keeps_slot_ids_tokens_and_the_disclosed_marker():
    """The mailbox line is model-facing and stays byte-identical to its contract."""
    text = acceptance_settlement_message(SimpleNamespace(retry_key="rk"), _wave())
    assert text.startswith("Acceptance review rk: 3 of 4 reviewer slot(s) have answered")
    assert "- triad_w45a8z: PASS — " in text and "⚠️ OMISSION NOTE: truncated at 400 chars; original length 872" in text
    assert "- triad_bkydwq: DEGRADED — The ledger could not be opened from the packet." in text
    assert "- triad_qq11zz: error — ClaudexorModelError: engine_busy (HTTP 429)" in text
    assert "- triad_late01: pending" in text


def test_reviewer_identity_prefers_observed_then_requested_then_unknown():
    run = _run(slot_roster=[{"slot_id": "s1"}], actors=[{"slot_id": "s1", "status": "ok", "operation_state": "settled",
                                                       "parsed": {"verdict": "FAIL", "summary": "Missing tests."}}])
    text = _late_settlement_text({**run, "aggregate_signal": "FAIL"}, {"slots": {"s1": "ok"}, "verdicts": {}})
    assert text.split("\n")[1] == "- unknown reviewer: rejected it — Missing tests."
    # Two seats of one model stay two lines.
    twin = {**_run()["actors"][0], "slot_id": "s2", "parsed": {"verdict": "PASS", "summary": "Fine."}}
    text = _late_settlement_text(_run(actors=[_run()["actors"][0], twin]),
                                 {"slots": {"triad_w45a8z": "ok", "s2": "ok"}, "verdicts": {}})
    assert "- openai/gpt-5.5-2026-09 (seat 1): passed it" in text and "- openai/gpt-5.5-2026-09 (seat 2): passed it — Fine." in text


def test_a_route_target_is_requested_not_observed():
    """A direct API route copies its own target into ``usage.resolved_model``: what the
    host sent, not what answered. Only an engine's final-attempt report is observed."""
    actor = {"slot_id": "s1", "model": "anthropic/claude-opus-5-5", "status": "ok", "operation_state": "settled",
             "usage": {"provider": "anthropic", "resolved_model": "anthropic/claude-opus-5-5"},
             "parsed": {"verdict": "PASS", "summary": "Fine."}}
    run = _run(slot_roster=[{"slot_id": "s1", "model": "anthropic/claude-opus-5-5", "route": "api_chat"}],
               actors=[actor])
    line = _late_settlement_text(run, {"slots": {"s1": "ok"}, "verdicts": {}}).split("\n")[1]
    assert line == "- anthropic/claude-opus-5-5 (requested): passed it — Fine."
    fact = late_evidence_fact(run, {"source": "terminal_delivery_registry", "state": "unknown", "delivered": []},
                              settled_at="2026-09-28T00:00:00+00:00")
    row = fact["reviewer_outputs"][0]
    assert (row["model"], row["requested_model"]) == ("", "anthropic/claude-opus-5-5")


def test_seats_follow_the_recorded_roster_not_the_released_wave_order():
    """A released wave collects its slots from a set: seat N is still the Nth roster seat."""
    roster = [{"slot_id": slot, "model": "m/twin", "route": "api_chat"} for slot in ("0", "1", "2")]
    actors = [{"slot_id": slot, "model": "m/twin", "status": "ok", "operation_state": "settled",
               "parsed": {"verdict": verdict, "summary": f"Seat {slot}."}}
              for slot, verdict in (("0", "PASS"), ("1", "PASS"), ("2", "FAIL"))]
    run = _run(aggregate_signal="FAIL", slot_roster=roster, actors=actors)
    # A slot the run never recorded keeps its place after the roster.
    wave = {"slots": {"late": "", "2": "ok", "0": "ok", "1": "ok"}, "verdicts": {}}
    assert _late_settlement_text(run, wave).split("\n")[1:] == [
        "- m/twin (requested) (seat 1): passed it — Seat 0.",
        "- m/twin (requested) (seat 2): passed it — Seat 1.",
        "- m/twin (requested) (seat 3): rejected it — Seat 2.",
        "- unknown reviewer: still awaited"]


def test_a_long_late_note_is_cut_in_words_and_the_applied_record_keeps_it_whole(tmp_path):
    """Five reviewers at the per-seat note bound compose a note past the projection's
    2000-char display bound. The card and the owner's row both print the projected
    note: its cut is said in words, naming the review record only when one is stored,
    and the applied record (the raw run) keeps every byte."""
    from ouroboros import loop, review_projection
    from ouroboros.task_results import load_task_result, write_task_result
    from tests.test_acceptance_publication import _context, _source

    roster = [{"slot_id": f"s{i}", "model": f"provider/reviewer-{i}", "route": "api_chat"} for i in range(5)]
    actors = [{"slot_id": f"s{i}", "model": f"provider/reviewer-{i}", "status": "ok", "operation_state": "settled",
               "parsed": {"verdict": "PASS", "summary": LONG_SUMMARY}} for i in range(5)]
    run = _run(slot_roster=roster, actors=actors)
    note = _late_settlement_text(run, {"slots": {f"s{i}": "ok" for i in range(5)}, "verdicts": {}},
                                 {"reviewed_revision": "delivered"})
    marker = f"\n⚠️ OMISSION NOTE: truncated at 2000 chars; original length {len(note)}"
    assert len(note) - 2000 > len(marker), "past the 2000-char bound and its anti-waste floor"
    run["late_settlement"] = {"note": note}
    write_task_result(tmp_path, "applied", "completed", result="The delivered answer.")
    trace = {"review_runs": [run]}
    loop._set_acceptance_decision(trace, {"status": "accepted", "reason": "clean_pass"})
    review_projection.publish_acceptance_checkpoint(_context(tmp_path), trace)
    panel = load_task_result(tmp_path, "applied")["review_projection"]["panels"][0]
    assert panel["applied_source_status"] == "available"
    assert panel["late_settlement"]["note"] == (
        note[:2000].rstrip() + "… (shortened; the complete text is in the review record)")
    assert _source(tmp_path, panel)["late_settlement"]["note"] == note
    unstored = {**run, "applied_source_status": "unavailable"}
    assert (review_projection.compact_review_projection([unstored])["panels"][0]["late_settlement"]["note"]
            == note[:2000].rstrip() + "… (shortened)")


def test_unreadable_unknown_and_not_sent_reviewers_read_as_their_own_states():
    actors = [
        {"slot_id": "a", "model": "m/a", "status": "ok", "operation_state": "settled", "raw_text": "I think it is fine.", "parsed": None},
        {"slot_id": "b", "model": "m/b", "status": "error", "operation_state": "custody_lost", "late_result_pending": True},
        {"slot_id": "c", "model": "m/c", "status": "not_dispatched", "operation_state": "not_dispatched"},
        {"slot_id": "d", "model": "m/d", "status": "error", "operation_state": "settled", "error": "Timeout after 600s"},
    ]
    wave = {"slots": {"a": "ok", "b": "error", "c": "not_dispatched", "d": "error"},
            "verdicts": {"a": {"verdict": "DEGRADED", "note": ""}, "d": {"verdict": "", "note": "Timeout after 600s"}}}
    text = _late_settlement_text(_run(aggregate_signal="DEGRADED", actors=actors), wave)
    lines = text.split("\n")
    assert lines[0].startswith("On the reviewed version of this answer (whether it was the delivered one is unknown), "
                               "reviewers later returned no settled verdict — 1 reviewer's outcome is still unknown.")
    assert lines[1:] == ["- m/a (requested): answered, but no verdict could be read",
                         "- m/b (requested): outcome unknown",
                         "- m/c (requested): not sent",
                         "- m/d (requested): unavailable"]
    assert "Timeout" not in text and "DEGRADED" not in text


def test_a_slot_without_an_answer_never_reads_as_a_reviewers_inconclusive_verdict():
    """Through the real wave producer: ``_settled_slot_verdict`` parses a failed,
    refused or unknown slot's EMPTY text as DEGRADED, the parser's word for no
    text. Only an answer carries a reviewer's verdict; the slot state speaks for
    the rest, and the host's error text stays out of the owner's line."""
    from ouroboros.acceptance_settlement import _collected_wave
    from ouroboros.triad_review import parse_review_findings

    own = '{"verdict": "DEGRADED", "summary": "The ledger could not be opened."}'
    actors = [
        {"slot_id": "a", "model": "m/a", "status": "error", "operation_state": "settled",
         "error": "APIConnectionError('Connection  reset\n by peer')", "transport_status": "provider_transport_error"},
        {"slot_id": "b", "model": "m/b", "status": "error", "operation_state": "settled",
         "error": "ClaudexorModelError: engine_busy (HTTP 429)", "reported_cause": "The engine is rate limited"},
        {"slot_id": "c", "model": "m/c", "status": "not_dispatched", "operation_state": "not_dispatched",
         "error": "refused before dispatch"},
        {"slot_id": "d", "model": "m/d", "status": "error", "operation_state": "custody_lost", "late_result_pending": True},
        {"slot_id": "e", "model": "m/e", "status": "ok", "operation_state": "settled", "raw_text": own,
         "parsed": parse_review_findings(own)[0], "signal": "DEGRADED", "semantic_verdict": "DEGRADED"},
        {"slot_id": "f", "model": "m/f", "status": "empty", "operation_state": "settled"},
        {"slot_id": "g", "model": "m/g", "operation_state": "in_flight"},
    ]
    run = _run(aggregate_signal="DEGRADED", actors=actors, slot_roster=[])
    wave = _collected_wave(run)
    assert {slot: row["verdict"] for slot, row in wave["verdicts"].items()} == dict.fromkeys("abcdef", "DEGRADED")
    assert wave["slots"]["g"] == ""
    lines = _late_settlement_text(run, wave).split("\n")[1:]
    assert lines == ["- m/a (requested): unavailable",
                     '- m/b (requested): unavailable — the engine reported: "The engine is rate limited"',
                     "- m/c (requested): not sent",
                     "- m/d (requested): outcome unknown",
                     "- m/e (requested): inconclusive — The ledger could not be opened.",
                     "- m/f (requested): answered, but no verdict could be read",
                     "- m/g (requested): still awaited"]
    for internal in ("APIConnectionError", "Connection", "engine_busy", "refused before", "DEGRADED"):
        assert internal not in "\n".join(lines), internal


def test_late_evidence_fact_names_the_reviewer_identity_additively():
    fact = late_evidence_fact(_run(), {"source": "terminal_delivery_registry", "state": "unknown", "delivered": []},
                              settled_at="2026-09-28T00:00:00+00:00")
    rows = {row["slot_id"]: row for row in fact["reviewer_outputs"]}
    assert rows["triad_w45a8z"]["verdict"] == "PASS"  # the token stays in the typed evidence
    assert (rows["triad_w45a8z"]["model"], rows["triad_w45a8z"]["requested_model"]) == ("openai/gpt-5.5-2026-09", "openai/gpt-5.5")
    assert (rows["triad_bkydwq"]["model"], rows["triad_bkydwq"]["requested_model"]) == ("", "anthropic/claude-opus-5-5")
    assert set(rows["triad_late01"]) >= {"slot_id", "operation_id", "operation_state", "verdict", "response_ref", "model", "requested_model"}


# ---------------------------------------------------------------------------
# The provider terminal sentence
# ---------------------------------------------------------------------------

class _BodyError(Exception):
    def __init__(self, message, *, status_code, body=None):
        super().__init__(message)
        self.status_code = status_code
        if body is not None:
            self.body = body


class _Raising:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def chat(self, **_kwargs):
        self.calls += 1
        raise self.error


class _Events:
    def __init__(self):
        self.events = []

    def put(self, event):
        self.events.append(event)

    put_nowait = put


def _call(tmp_path, llm, usage):
    from ouroboros.loop_llm_call import call_llm_with_retry

    return call_llm_with_retry(llm, [{"role": "user", "content": "hi"}], "openai/fixture", None, "high", 3,
                               tmp_path, "task-403", 1, _Events(), usage, "task", False)


def test_the_exception_owner_stamps_only_the_providers_own_sentence(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.loop_llm_call.time.sleep", lambda _s: pytest.fail("a 403 must not retry"))
    body = {"error": {"message": "The API key was revoked.", "code": 403, "metadata": {"provider_name": None}}}
    llm = _Raising(_BodyError(f"Error code: 403 - {body}", status_code=403, body=body))
    usage = {}
    msg, _cost = _call(tmp_path, llm, usage)
    assert msg is None and llm.calls == 1
    assert usage["_last_llm_error_kind"] == "auth_error"
    assert usage["_last_llm_provider_message"] == "The API key was revoked."
    assert "Error code" in usage["_last_llm_error"], "the diagnostic keeps the exception text"
    hint = provider_failure_hint(usage)
    assert hint == ' The provider said: "The API key was revoked."'
    text = provider_terminal_fallback_text(usage, is_context_overflow=False, is_transport_wait=False,
                                           waited_sec=0.0, is_deadline_exhausted=False)
    assert text.startswith("⚠️ The model provider returned no usable response. The provider said: \"The API key was revoked.\"")
    assert "Error code" not in text and "{" not in text and "PermissionDenied" not in text
    # The durable event keeps the diagnostic message for Logs.
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    event, = [row for row in rows if row["type"] == "llm_api_error"]
    assert event["provider_message"] == "The API key was revoked." and "Error code" in event["error"]


def test_without_a_provider_sentence_the_host_classifies_and_points_at_logs(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.loop_llm_call.time.sleep", lambda _s: pytest.fail("a 401 must not retry"))
    llm = _Raising(_BodyError("AuthenticationError('401 invalid_api_key')", status_code=401))
    usage = {"_last_llm_provider_message": "stale sentence from an earlier round"}
    msg, _cost = _call(tmp_path, llm, usage)
    assert msg is None and "_last_llm_provider_message" not in usage
    assert usage["_last_llm_error_kind"] == "auth_error"
    hint = provider_failure_hint(usage)
    assert hint == (" The provider gave no readable message; the host classified the failure as "
                    "an authentication or authorization refusal. The task's Logs keep the host's record of this failure.")
    assert "AuthenticationError" not in hint and "stale sentence" not in hint


def test_a_body_error_inside_an_empty_response_carries_the_providers_sentence(tmp_path):
    class _Empty:
        calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            return ({"content": "", "finish_reason": "stop"},
                    {"provider_error": {"code": "401", "type": "invalid_request_error",
                                        "message": "Incorrect API key provided.", "kind": "provider_error"},
                     "prompt_tokens": 3, "completion_tokens": 0})

    llm, usage = _Empty(), {}
    msg, _cost = _call(tmp_path, llm, usage)
    assert msg is None and llm.calls == 1
    assert usage["_last_llm_provider_message"] == "Incorrect API key provided."
    assert provider_failure_hint(usage) == ' The provider said: "Incorrect API key provided."'


def test_a_body_error_sentence_its_producer_cut_is_quoted_as_shortened(tmp_path):
    """The HTTP-200 body-error producer keeps a bounded ``message`` and says when it
    cut it; the owner's row then never closes that prefix as the provider's whole
    sentence. Classification and retry read code/type/kind and do not change."""
    from ouroboros.llm import LLMClient

    client = LLMClient()
    target = client._resolve_remote_target("deepseek/deepseek-v4-flash-0731")
    sentence = "Your organisation has used its whole monthly request allowance for this model family. " * 6

    def produced(message):
        body = {"id": "gen-1", "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                "error": {"code": 402, "message": message}}
        return client._normalize_remote_response(body, target, skip_cost_fetch=True)[1]

    short = produced("Insufficient credits.")
    assert short["provider_error"]["message"] == "Insufficient credits." and "message_truncated" not in short["provider_error"]
    cut = produced(sentence)
    assert cut["provider_error"]["message"] == sentence[:300] and cut["provider_error"]["message_truncated"] is True

    class _Empty:
        calls = 0

        def chat(self, **_kwargs):
            self.calls += 1
            return {"content": "", "finish_reason": "stop"}, {**cut, "prompt_tokens": 3, "completion_tokens": 0}

    llm, usage = _Empty(), {}
    msg, _cost = _call(tmp_path, llm, usage)
    assert msg is None and llm.calls == 1 and usage["_last_llm_error_kind"] == "provider_body_error"
    assert usage["_last_llm_provider_message_cut"] is True
    assert provider_failure_hint(usage) == (f' The provider said: "{" ".join(sentence[:300].split())}…" (shortened). '
                                            "The task's Logs keep the host's record of this failure.")


def test_a_usable_response_clears_the_stale_provider_sentence(tmp_path):
    class _Ok:
        def chat(self, **_kwargs):
            return {"content": "fine"}, {"prompt_tokens": 3, "completion_tokens": 1}

    usage = {"_last_llm_provider_message": "The API key was revoked.", "_last_llm_provider_message_cut": True,
             "_last_llm_error": REPR, "_last_llm_error_kind": "auth_error"}
    msg, _cost = _call(tmp_path, _Ok(), usage)
    assert msg["content"] == "fine"
    assert not any(key in usage for key in ("_last_llm_provider_message", "_last_llm_provider_message_cut",
                                            "_last_llm_error", "_last_llm_error_kind"))
    assert provider_failure_hint(usage) == ""


def test_the_hint_quotes_the_providers_sentence_and_never_a_repr():
    """A stored sentence is quoted whitespace-normalised; a repr beside a typed kind,
    or a legacy record with a repr and no kind, reads as the host's classification."""
    typed = {"_last_llm_error": f"  {REPR}  ", "_last_llm_error_kind": "auth_error"}
    spoken = provider_failure_hint({**typed, "_last_llm_provider_message": "  Your API key was  revoked.  "})
    assert spoken == ' The provider said: "Your API key was revoked."'
    assert provider_failure_hint(typed) == (" The provider gave no readable message; the host classified the failure as "
                                            "an authentication or authorization refusal. The task's Logs keep the "
                                            "host's record of this failure.")
    legacy = provider_failure_hint({"_last_llm_error": "AuthenticationError('401 invalid_api_key')"})
    assert legacy == (" The provider gave no readable message; the host classified the failure as a provider failure. "
                      "The task's Logs keep the host's record of this failure.")
    for text in (spoken, provider_failure_hint(typed), legacy):
        assert "PermissionDenied" not in text and "AuthenticationError" not in text and "{" not in text


def test_a_long_provider_sentence_is_shortened_in_words_beside_the_logs_pointer(tmp_path, monkeypatch):
    """No silent cut on the owner's row: the sentence is stamped whole, the SSOT
    display bound decides the cut (its anti-waste floor lets a small overflow
    through whole), and a cut is said in words with the pointer to Logs."""
    monkeypatch.setattr("ouroboros.loop_llm_call.time.sleep", lambda _s: pytest.fail("a 403 must not retry"))
    sentence = "Access to this model was denied for the configured project. " * 14  # ~840 chars
    body = {"error": {"message": sentence, "code": 403}}
    usage = {}
    msg, _cost = _call(tmp_path, _Raising(_BodyError(f"Error code: 403 - {body}", status_code=403, body=body)), usage)
    assert msg is None and usage["_last_llm_provider_message"] == sentence.strip(), "stamped whole, never sliced"
    hint = provider_failure_hint(usage)
    quoted = sentence.strip()[:600].rstrip()
    assert hint == (f' The provider said: "{quoted}…" (shortened). '
                    "The task's Logs keep the host's record of this failure.")
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    event, = [row for row in rows if row["type"] == "llm_api_error"]
    assert sentence.strip() in " ".join(event["error"].split()), "this body error's text carries the whole sentence"
    # Barely over the bound: cheaper than a disclosure, so it passes whole.
    barely = "x" * 630
    assert provider_failure_hint({"_last_llm_provider_message": barely}) == f' The provider said: "{barely}"'


def test_a_shortened_stream_sentence_promises_logs_no_bytes_they_do_not_hold(tmp_path, monkeypatch):
    """An SSE error frame's sentence lives only in its body: the exception text never
    carries it and the durable event keeps the bounded excerpt, so the owner's row
    points at the host's record without saying the rest of the sentence is there."""
    from ouroboros.llm_stream import ProviderStreamError

    monkeypatch.setattr("ouroboros.loop_llm_call.time.sleep", lambda _s: pytest.fail("a 403 must not retry"))
    sentence = "The upstream stream was closed for this project by the provider's access guard. " * 12
    usage = {}
    msg, _cost = _call(tmp_path, _Raising(ProviderStreamError({"error": {"message": sentence, "code": 403}})), usage)
    assert msg is None and usage["_last_llm_provider_message"] == sentence.strip()
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    event, = [row for row in rows if row["type"] == "llm_api_error"]
    kept = " ".join([event["error"], event["provider_message"]])
    assert sentence.strip() not in kept and len(event["provider_message"]) <= 600, "Logs hold only an excerpt"
    quoted = sentence.strip()[:600].rstrip()
    assert provider_failure_hint(usage) == (f' The provider said: "{quoted}…" (shortened). '
                                            "The task's Logs keep the host's record of this failure.")


def test_owner_provider_message_reads_body_and_stream_shapes_never_engine_display():
    from ouroboros.llm_claudexor import ClaudexorModelError
    from ouroboros.loop_transport import owner_provider_fields, owner_provider_message

    problem = {"code": "provider_failed", "message": "Codex model request was refused (HTTP 400).",
               "context": {"httpStatus": 400, "stage": "response", "errorCode": "vendor_refusal",
                           "vendorCode": "string_above_max_length", "parameter": "instructions",
                           "providerMessage": "private-body"}}
    engine = ClaudexorModelError(problem)
    assert "stage=response" in engine.display_message, "the fixture's display text is host-composed"
    assert owner_provider_message(engine) == "", "an engine's composed display text is no provider sentence"
    assert owner_provider_fields(engine) == {"code": "string_above_max_length", "parameter": "instructions"}
    assert owner_provider_fields(ClaudexorModelError(problem, unknown=True)) == {}, "an unknown outcome exposes none"
    assert owner_provider_fields(_BodyError("x", status_code=400, body={"message": "m"})) == {}
    assert owner_provider_message(_BodyError("x", status_code=400, body={"message": "  Flat  body  message. "})) == "Flat body message."
    stream = RuntimeError("stream died")
    stream.provider_message = "Upstream closed the stream."
    assert owner_provider_message(stream) == "Upstream closed the stream."
    assert owner_provider_message(RuntimeError(REPR)) == "", "a bare exception has no provider sentence"
    # An empty response's body-error dict: its own message, or nothing.
    assert owner_provider_message({"code": "401", "message": "  Incorrect  API key. "}) == "Incorrect API key."
    assert owner_provider_message({"code": "401", "message": {"nested": "not a sentence"}}) == ""


@pytest.mark.parametrize("unknown", [False, True])
def test_a_claudexor_refusal_never_reads_as_the_providers_sentence(tmp_path, monkeypatch, unknown):
    """The engine's display text (stage, cause, engine code and message) is the
    host's record: it stays in Logs. The owner's row names the provider's own typed
    fields as the provider's, the host's classification as the host's, and a stale
    sentence or field from an earlier round never speaks for this failure."""
    from ouroboros.llm_claudexor import ClaudexorModelError

    monkeypatch.setattr("ouroboros.loop_llm_call.time.sleep", lambda _s: None)
    error = ClaudexorModelError({"code": "provider_failed", "message": "Codex model request was refused (HTTP 400).",
                                 "context": {"httpStatus": 400, "stage": "response", "errorCode": "vendor_refusal",
                                             "vendorCode": "string_above_max_length", "parameter": "instructions",
                                             "providerMessage": "private-body"}}, unknown=unknown)
    usage = {"_last_llm_provider_message": "stale sentence", "_last_llm_provider_fields": {"code": "stale_code"}}
    msg, _cost = _call(tmp_path, _Raising(error), usage)
    assert msg is None and "_last_llm_provider_message" not in usage
    hint = provider_failure_hint(usage)
    for text in ("The provider said", "stage=", "cause=", "provider_failed", "model_outcome_unknown",
                 "Codex model request was refused", "private-body", "stale"):
        assert text not in hint, text
    assert hint.endswith("The task's Logs keep the host's record of this failure.")
    if unknown:
        assert "_last_llm_provider_fields" not in usage
        assert hint.startswith(" The provider gave no readable message; the host classified the failure as ")
    else:
        assert usage["_last_llm_provider_fields"] == {"code": "string_above_max_length", "parameter": "instructions"}
        assert hint == (' The provider reported error code "string_above_max_length" and parameter "instructions" '
                        "but no sentence the host can quote; the host classified the failure as a rejected request. "
                        "The task's Logs keep the host's record of this failure.")
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert all(row["error"] == error.display_message for row in rows if row["type"] == "llm_api_error"), \
        "Logs keep the host's composed diagnostic"


def test_a_usable_response_clears_stale_provider_fields(tmp_path):
    class _Ok:
        def chat(self, **_kwargs):
            return {"content": "fine"}, {"prompt_tokens": 3, "completion_tokens": 1}

    usage = {"_last_llm_provider_fields": {"code": "string_above_max_length"}, "_last_llm_error_kind": "bad_request"}
    msg, _cost = _call(tmp_path, _Ok(), usage)
    assert msg["content"] == "fine" and "_last_llm_provider_fields" not in usage
    assert provider_failure_hint(usage) == ""


# ---------------------------------------------------------------------------
# The late-evidence carrier: producer → stored row → history → live frame
# ---------------------------------------------------------------------------

EVIDENCE = {"task_id": "t1", "panel_id": "panel_1", "settled_at": "2026-09-28T00:06:00+00:00",
            "reviewed_revision": "delivered", "reviewed_is_emitted": True,
            "source_ref": {"kind": "task_source", "root": "artifact_store", "sha256": "a" * 64, "size": 75000,
                           "path": f"source_handles/context_checkpoints/acceptance-{'a' * 64}.json"}}


def test_late_evidence_replays_through_the_stored_row_and_history(tmp_path):
    from ouroboros.gateway.history import make_chat_history_endpoint
    from supervisor import message_bus

    meta = {"card_row": "reviews", "card_row_id": "acceptance-late:rk", "late_evidence": EVIDENCE}
    message_bus.log_chat("system", 1, 0, "On the delivered version of this answer, reviewers later passed it.",
                         record_type="acceptance_late_settlement", task_id="t1", message_meta=meta, drive_root=tmp_path)
    message_bus.log_chat("system", 1, 0, "Open delegated execution: run-x.", record_type="custody_notice",
                         task_id="t1", message_meta={**meta, "card_row": "timeline"}, drive_root=tmp_path)
    stored = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text().splitlines()]
    assert stored[0]["late_evidence"] == EVIDENCE and "late_evidence" not in stored[1]
    response = asyncio.run(make_chat_history_endpoint(tmp_path)(SimpleNamespace(query_params={"chat_id": "1"})))
    rows = {row["text"]: row for row in json.loads(response.body)["messages"]}
    late = rows["On the delivered version of this answer, reviewers later passed it."]
    assert late["card_row"] == "reviews" and late["late_evidence"] == EVIDENCE
    assert "late_evidence" not in rows["Open delegated execution: run-x."], "only the typed late row carries the pointer"


def test_the_live_frame_carries_the_late_evidence_pointer(tmp_path, monkeypatch):
    from supervisor import message_bus

    bridge = message_bus.LocalChatBridge({})
    frames = []
    bridge._broadcast_fn = frames.append
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_kw: None)
    bridge.send_message(1, "On the delivered version of this answer, reviewers later passed it.", task_id="t1",
                        role="system", system_type="acceptance_late_settlement",
                        progress_meta={"card_row": "reviews", "card_row_id": "acceptance-late:rk", "late_evidence": EVIDENCE})
    chat, = [row for row in frames if row.get("type") == "chat"]
    assert chat["card_row"] == "reviews" and chat["late_evidence"] == EVIDENCE


def test_the_gateway_contract_names_the_pointer_in_both_mirrors():
    import pathlib
    from typing import get_type_hints

    from ouroboros.gateway.contracts import ChatOutbound

    assert "late_evidence" in get_type_hints(ChatOutbound, include_extras=True)
    text = (pathlib.Path(__file__).resolve().parents[1] / "web" / "modules" / "api_types.js").read_text(encoding="utf-8")
    assert "@property {Object=} late_evidence" in text


@pytest.mark.serial
def test_a_notice_links_its_own_record_after_a_newer_publication_of_its_panel(tmp_path):
    """A later collection republishes the late panel under a new applied record, while the
    outbox keeps the FIRST notice and the record it was sent with. The task artifact route
    resolves that record by this author's host acceptance membership (the reader the
    notice's own ``read`` selector names); a forged or altered pointer resolves nothing."""
    import copy
    import hashlib
    import pathlib

    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ouroboros import artifacts, review_projection
    from ouroboros.acceptance_settlement import _stored_late_settlement, enqueue_late_acceptance_settlement
    from ouroboros.gateway.tasks import api_task_artifact
    from ouroboros.task_results import load_task_result, write_task_result
    from supervisor.terminal_delivery import pending_deliveries
    from tests.test_acceptance_publication import _context

    ctx = _context(tmp_path)
    write_task_result(tmp_path, "applied", "completed", result="The delivered answer.")
    run = _run(actors=[actor for actor in _run()["actors"] if actor["operation_state"] == "settled"],
               request={"surface": "task_acceptance", "subject": "answer A", "retry_key": "rk", "task_id": "applied"})

    def collect(settled_at, revision, status):
        # Each collector stamps its own settlement time on the same settled wave.
        late = {"note": "On the delivered version of this answer, reviewers later passed it.",
                "settled_at": settled_at, "reviewed_subject": {"retry_key": "rk", "panel_id": "panel_1"}}
        trace = {"review_runs": [{**copy.deepcopy(run), "late_settlement": late}],
                 "_acceptance_publication_revision": revision}
        panel = _stored_late_settlement(review_projection.publish_acceptance_checkpoint(ctx, trace),
                                        trace["review_runs"][0])
        row = load_task_result(tmp_path, "applied")
        assert enqueue_late_acceptance_settlement(ctx, "applied", "rk", row, panel) == status
        return panel

    first = collect("2026-09-28T00:06:00+00:00", 0, "announced")
    # The outbox still owes the first notice, so the republication queues no second one.
    collect("2026-09-28T00:06:01+00:00", 1, "published")
    current = load_task_result(tmp_path, "applied")["review_projection"]["panels"][0]
    notice, = pending_deliveries(tmp_path)
    ref = notice["progress_meta"]["late_evidence"]["source_ref"]
    assert ref == first["applied_source_ref"] != current["applied_source_ref"]
    assert current["late_settlement"] == first["late_settlement"]

    def foreign(record):
        return artifacts.store_actor_source_bytes(tmp_path, "applied", category="context_checkpoints",
                                                  source_id="acceptance", data=json.dumps(record).encode(),
                                                  extension="json")["path"]

    app = Starlette(routes=[Route("/api/tasks/{task_id}/artifacts/{name}", api_task_artifact)])
    app.state.drive_root = tmp_path
    with TestClient(app) as client:
        def get(path, name=None):
            return client.get(f"/api/tasks/applied/artifacts/{name or pathlib.PurePosixPath(path).name}",
                              params={"source": path})

        for published in (ref, current["applied_source_ref"]):
            response = get(published["path"])
            assert response.status_code == 200, published
            assert hashlib.sha256(response.content).hexdigest() == published["sha256"]
            assert len(response.content) == published["size"]
        forged = [
            f"source_handles/context_checkpoints/acceptance-{'0' * 64}.json",  # no such record
            foreign({"authority": "worker", "request": {"surface": "task_acceptance", "task_id": "applied"}}),
            foreign({**run, "request": {**run["request"], "task_id": "another-task"}}),
            foreign({**run, "request": {**run["request"], "surface": "plan_review"}}),
            f"source_handles/context_checkpoints/../context_checkpoints/{pathlib.PurePosixPath(ref['path']).name}",
        ]
        for path in forged:
            assert get(path).status_code == 404, path
        assert get(ref["path"], name="other.json").status_code == 404, "the name must be the record's own"
        stored = artifacts.task_artifact_dir_path(tmp_path, "applied") / ref["path"]
        stored.write_bytes(stored.read_bytes().replace(b"answer A", b"answer B"))
        assert get(ref["path"]).status_code == 404, "altered bytes fail their digest"
