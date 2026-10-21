"""An economy target versus the physical window, at the consumers of #1727, #1728 and #1527.

Owner decisions: 2026-10-11 1A (a read the mind chose answers to the real window; the economy
rests on its own folding) and 2A (while the request fits, the shown history is not re-cut);
2026-10-13 A (an unknown window is not replaced by the target for a chosen read) and A (Nano
delivers the task text while it physically fits). Each case runs the production consumer -- the
batch delivery the loop calls, the facts line, the route rebind, the account reprepare, the cold restore,
the refusal rung and the plan builder, and the whole loop up to a fake Claudexor gateway's received requests --
on synthetic data; no provider, model or installation data is read, and no provider cache is claimed.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import queue
from types import SimpleNamespace

import pytest

from ouroboros import artifacts
from ouroboros.context_budget import RECLAIM_LOW_WATER_DIVISOR
from ouroboros.memory_inventory import VIEW_TRACE_KEY
from ouroboros.tools.tool_result import ToolResult
from tests._tool_result_delivery_shared import measured_fit
from tests._usage_store_testing import attempt_rows_in_start_order
from tests.test_llm_claudexor import MODEL, ROUTE, result
from tests.test_llm_claudexor import setup as gateway_fixture
from tests.test_model_wait import live_wait as wait_fixture
from tests.test_subscription_main_wait import main_call as main_call_fixture
from tests.test_tool_result_delivery import _ctx, _deliver, _row, _source_ref, _view_json

# The production send: the real loop, rebind, LLMClient, Claudexor serializer and physical ledger before a fake gateway.
setup, live_wait, main_call = gateway_fixture, wait_fixture, main_call_fixture
TARGET, RESERVE = 40_000, 5_000
OWNER = {"role": "user", "content": "Owner, while the batch ran: keep the old API alive. " * 60}


def _batch(tmp_path, *, window, read_args=None, read_chars=240_000):
    """A completed mixed batch through ``process_tool_results``: a page the call asked for, an unasked
    producer-wrapped build log, a tiny unasked result and owner words that arrived meanwhile."""
    ctx = _ctx(tmp_path)
    ctx._pending_owner_dialogue = [dict(OWNER)]
    page = "".join(f"line {n:06d} of the module\n" for n in range(read_chars // 25))
    log = "build step " * 8_000
    typed = ToolResult(status="ok", code="OK", text=log + "\n[host] slow build", producer_text=log,
                       host_annotations=("[host] slow build",))
    rows = [_row("read", "read_file", page, args=read_args or {"path": "m.py", "start_line": 1, "max_lines": 10_000}),
            _row("make", "run_command", typed.text, args={"cmd": ["make"]}, typed=typed),
            _row("tiny", "run_command", "ok", args={"cmd": ["true"]})]
    candidates = []
    measure = measured_fit(window=window, target=TARGET, reserve=RESERVE)

    def fit(messages, schemas):  # the round's Main measurement; records each complete candidate it saw
        candidates.append(copy.deepcopy(messages))
        return measure(messages, schemas)

    messages, trace = _deliver(ctx, rows, fit, messages=[{"role": "user", "content": "Assignment: read m.py."}])
    return ctx, page, messages, trace, candidates, measure


def test_a_chosen_page_above_the_target_inside_a_known_window_arrives_whole(tmp_path):
    ctx, page, messages, trace, calls, fit = _batch(tmp_path, window=200_000)
    receipt = trace["tool_result_delivery"][0]
    assert messages[1]["content"] == page and "result_partial" not in trace["tool_calls"][0]  # byte-identical
    assert receipt["status"] == "projected" and receipt["capacity_tokens"] == 200_000
    assert receipt["boundary_tokens"] == TARGET and receipt["target_excess_tokens"] > 0
    assert receipt["requested_room_tokens"] > receipt["free_tokens"]
    # The unasked log keeps the economy: no target room is left after the page, so it shows by address.
    log = _view_json(messages[2]["content"])
    assert log["shown_ranges"] == [] and trace["tool_calls"][1]["result_source_status"] == "ready"
    assert "[host] slow build" in messages[2]["content"] and "PRODUCER_RESULT_SOURCE_JSON=" in messages[2]["content"]
    assert messages[3]["content"] == "ok"  # anti-growth: a tiny result is never replaced by a longer envelope
    # One complete candidate: owner words and the producer footer were measured with the bodies, and the
    # published transcript (the owner words after the tool block) is a measured candidate that fits.
    assert messages[4] == OWNER and all(OWNER in candidate for candidate in calls)
    published = [{key: row[key] for key in ("role", "tool_call_id", "content")} for row in messages[1:4]]
    assert published in [candidate[1:4] for candidate in calls]
    assert fit(messages, [])["estimated_input_tokens"] + RESERVE <= 200_000
    assert artifacts.read_actor_source_bytes(tmp_path, ctx.task_id, _source_ref(messages[2]["content"])).decode()


def test_only_the_window_cuts_a_chosen_page_and_an_unfit_minimum_stays_named(tmp_path):
    ctx, page, messages, trace, calls, fit = _batch(tmp_path, window=30_000)
    receipt = trace["tool_result_delivery"][0]
    view = _view_json(messages[1]["content"])
    assert receipt["status"] == "projected" and view["shown_ranges"] and view["omitted_ranges"]
    assert trace["tool_calls"][0]["result_source_view"]["requested"] is True
    stored = artifacts.read_actor_source_bytes(tmp_path, ctx.task_id, _source_ref(messages[1]["content"])).decode()
    assert stored == page and fit(messages, [])["estimated_input_tokens"] + RESERVE <= 30_000
    (tmp_path / "tiny").mkdir()
    _ctx2, _page2, _messages2, trace2, _calls2, _fit2 = _batch(tmp_path / "tiny", window=600)
    assert trace2["tool_result_delivery"][0]["status"] == "minimum_view_unfit"


def test_an_unknown_window_keeps_the_chosen_form_and_the_unasked_economy(tmp_path):
    ctx, page, messages, trace, calls, _fit = _batch(tmp_path, window=None)
    receipt = trace["tool_result_delivery"][0]
    assert receipt["status"] == "capacity_unknown" and receipt["capacity_tokens"] is None  # never a fit or unfit claim
    assert messages[1]["content"] == page  # not pre-cut by the target
    assert _view_json(messages[2]["content"])["shown_ranges"] == []
    eighth = math.ceil(TARGET / RECLAIM_LOW_WATER_DIVISOR)
    assert receipt["unsolicited_allowance_tokens"] <= eighth and receipt["unsolicited_body_tokens"] <= eighth
    # A named head/tail view is honoured exactly, however far past the target.
    text = "H" * 150_000 + "MIDDLE" + "T" * 150_000
    rows = [_row("view", "run_command", text, args={"cmd": ["x"], "view_head_chars": 150_000, "view_tail_chars": 50_000})]
    (tmp_path / "view").mkdir()
    messages2, trace2 = _deliver(_ctx(tmp_path / "view"), rows, measured_fit(target=TARGET, reserve=RESERVE))
    assert _view_json(messages2[0]["content"])["shown_ranges"] == [[0, 150_000], [len(text) - 50_000, len(text)]]
    assert trace2["tool_result_delivery"][0]["status"] == "capacity_unknown"


PLAIN, ESCAPED = "ordinary module source line\n", '\t"\\'  # one and two serialized chars per char


def _mixed(tmp_path, page, log, *, window):
    """A chosen page beside an unasked log of another density, priced by the real request serializer."""
    rows = [_row("read", "read_file", page, args={"path": "m.py", "start_line": 1, "max_lines": 10_000}),
            _row("make", "run_command", log, args={"cmd": ["make"]})]
    fit = measured_fit(window=window, target=TARGET, reserve=RESERVE)
    messages, trace = _deliver(_ctx(tmp_path), rows, fit)
    return messages, trace["tool_result_delivery"][0], fit(messages, [])["estimated_input_tokens"] + RESERVE


@pytest.mark.parametrize("window", [65_000, None])
def test_a_plain_chosen_page_stays_whole_beside_a_heavily_escaped_unasked_log(tmp_path, window):
    """Review rl-20261013T122953: one batch-average density, raised by the escaped log, cut a page that fits
    the window whole beside the log's address (57,542 of 65,000 tokens with the reserve)."""
    page, log = PLAIN * 7_200, ESCAPED * 120_000
    messages, receipt, sent = _mixed(tmp_path, page, log, window=window)
    assert messages[0]["content"] == page  # byte-identical
    assert _view_json(messages[1]["content"])["shown_ranges"] == []  # the page left no target room for the log
    assert receipt["unsolicited_body_tokens"] == 0 and TARGET < sent <= 65_000
    assert receipt["status"] == ("projected" if window else "capacity_unknown")


def test_an_escaped_unasked_log_keeps_its_eighth_beside_a_plain_chosen_page(tmp_path):
    """At the batch average the log looked inside its eighth and arrived whole; measured beside the
    chosen page its real cost is over the eighth, so it is cut to it while the page stays whole."""
    page, log = PLAIN * 2_860, ESCAPED * 4_000
    messages, receipt, sent = _mixed(tmp_path, page, log, window=200_000)
    eighth = math.ceil(TARGET / RECLAIM_LOW_WATER_DIVISOR)
    assert messages[0]["content"] == page and sent <= TARGET
    assert receipt["whole_tokens"] - receipt["chosen_tokens"] > eighth  # the whole log
    assert _view_json(messages[1]["content"])["shown_ranges"]  # a head and tail, not the address alone
    assert 0 < receipt["unsolicited_body_tokens"] <= receipt["unsolicited_allowance_tokens"] == eighth


@pytest.mark.parametrize("page, log", [(PLAIN * 14_300, ESCAPED * 120_000), (ESCAPED * 100_000, PLAIN * 12_000)],
                         ids=["plain-page", "escaped-page"])
def test_a_chosen_page_the_window_cannot_hold_fills_it_at_its_own_density(tmp_path, page, log):
    messages, receipt, sent = _mixed(tmp_path, page, log, window=65_000)
    assert receipt["status"] == "projected" and _view_json(messages[0]["content"])["omitted_ranges"]
    assert 65_000 * 0.97 <= sent <= 65_000  # only the window cut it, not the neighbour's density
    assert _view_json(messages[1]["content"])["shown_ranges"] == []


def _facts(monkeypatch, **usage):
    from ouroboros import pricing
    from ouroboros.loop_messages import context_facts_line

    monkeypatch.setattr(pricing, "get_pricing", lambda **_kw: {})
    ctx = SimpleNamespace(accumulated_usage=usage, round_idx=3, active_context_mode="low", active_model="m",
                          messages=[{"role": "system", "content": "S"}, {"role": "user", "content": "go"}],
                          tool_schemas=[], context_fit_plan=SimpleNamespace(provider=""))
    return context_facts_line(ctx)


def test_the_facts_line_names_window_room_and_target_excess_apart(monkeypatch):
    # A measured input: this conversation's round 2 was accepted at 290,000 tokens (``context_anchor``).
    base = {"_context_prompt_estimate": 300_000, "_context_reply_allowance_tokens": 65_536,
            "_context_response_reserve_tokens": 65_536, "_context_target_total_tokens": 250_000,
            "_context_anchor_input_tokens": 290_000, "_context_anchor_round": 2}
    line = _facts(monkeypatch, **base, _context_capacity_total_tokens=500_000)
    assert "free ~134,464" in line and "over target 115,536" in line and "reply floor" not in line
    assert "round 2 accepted 290,000, +10,000 since" in line
    unknown = _facts(monkeypatch, **base, _context_capacity_total_tokens=None)
    assert "window unknown" in unknown and "free unknown" in unknown and "over target 115,536" in unknown
    nano = _facts(monkeypatch, **{**base, "_context_prompt_estimate": 60_000, "_context_response_reserve_tokens": 8_192,
                                  "_context_target_total_tokens": 85_000}, _context_capacity_total_tokens=200_000)
    assert "free ~74,464" in nano and "target room 16,808 (reply floor 8,192)" in nano
    cold = _facts(monkeypatch, **{**base, "_context_anchor_input_tokens": None}, _context_capacity_total_tokens=500_000)
    assert "free unknown" in cold and "cold chars/4 estimate" in cold  # a cold estimate states no free room


# --- #1728: a technical rebind keeps the sent view while it fits -----------------------------------

def _wire(monkeypatch, messages):
    """The OpenRouter send copy of ``messages``: its leading system items are the cacheable prefix."""
    from ouroboros.llm import LLMClient

    monkeypatch.setattr(LLMClient, "_SUPPORTED_PARAMS_FETCHED", True, raising=False)
    monkeypatch.setattr("ouroboros.pricing._fetch_live_rows", lambda *_a, **_kw: {})
    monkeypatch.setenv("OPENROUTER_API_KEY", "unused")
    client = LLMClient(api_key="unused")
    target = client._resolve_remote_target("openai/gpt-6-sol")
    payload = client._build_remote_kwargs(target, copy.deepcopy(messages), "high", 512, "auto", None, [], skip_capability_fetch=True)
    return [m for m in payload["messages"] if m["role"] == "system"]


def _rebind(monkeypatch, root, plan, messages, *, window, ratio=1.0, status="asserted", usage=None):
    import ouroboros.context
    from ouroboros import context_fit
    from ouroboros.loop_model_call import _rebind_context_fit_plan
    from ouroboros.tools.registry import ToolRegistry

    root.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry(repo_dir=root, drive_root=root)
    registry._ctx._accumulated_usage = usage
    evidence = SimpleNamespace(route_fp=f"r-{window}", status=status, stale=status != "asserted", window_tokens=window)
    monkeypatch.setattr(ouroboros.context, "_context_fit_route", lambda *_a, **_kw: ({"model": "m2", "provider": "p"}, evidence))
    monkeypatch.setattr(context_fit, "_route_calibration_ratio", lambda *_a: ratio)
    rebound, mode = _rebind_context_fit_plan(plan, registry, messages, model="m2", use_local=False, start_mode="low",
                                             tool_schemas=[])
    events = [json.loads(line) for line in (root / "logs" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    return rebound, mode, [e for e in events if e.get("checkpoint_kind") == "context_fit_route_rebound"][-1]


def _long_tail(plan):
    return plan.messages_for("low") + [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "big", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "big.py", "start_line": 1, "max_lines": 9_000})}}]},
        {"role": "tool", "tool_call_id": "big", "content": "LATER REAL MATERIAL " * 30_000}]  # ~150K: past the Low target


def test_a_rebind_keeps_the_sent_view_on_a_smaller_fitting_window_and_unfolds_nothing(tmp_path, monkeypatch):
    from tests.test_memory_view_modes import _built

    _core, plan = _built(tmp_path / "w", monkeypatch)
    low = plan(1_000_000, preferred="low", tool_schemas=[])
    messages = _long_tail(low)
    sent, wire = copy.deepcopy(messages[0]), _wire(monkeypatch, messages)
    # Another account on a smaller window with another density: the request still fits, so nothing is re-cut.
    rebound, mode, event = _rebind(monkeypatch, tmp_path / "a", low, messages, window=400_000, ratio=1.2)
    assert mode == "low" and messages[0] == sent and _wire(monkeypatch, messages) == wire and event["view_kept"] is True
    assert rebound.projection("low").memory_facts == low.projection("low").memory_facts
    assert rebound.window_tokens == 400_000 and rebound.projection("low").calibration_ratio == 1.2
    assert rebound.projection("low").estimated_tokens > low.projection("low").estimated_tokens  # the real tail, measured
    # A shorter tail on a larger window, then an unknown one: still the same bytes, no unfolding.
    messages[:] = messages[:2]
    for window, status in ((2_000_000, "asserted"), (0, "failed")):
        again, _mode, event = _rebind(monkeypatch, tmp_path / f"b{window}", rebound, messages, window=window, status=status)
        assert messages[0] == sent and event["view_kept"] is True
        assert again.projection("low").memory_facts == low.projection("low").memory_facts


def test_a_rebind_narrows_a_view_the_window_cannot_hold_from_its_own_level_by_the_window_alone(tmp_path, monkeypatch):
    from tests.test_memory_view_modes import _built

    _core, plan = _built(tmp_path / "w", monkeypatch)
    low = plan(1_000_000, preferred="low", tool_schemas=[])
    messages = _long_tail(low)
    measured = low.reproject_for_route(window_tokens=1_000_000, known_window=True, ratio=1.0, output_reserve=65_536,
                                       tool_schemas=[], start_mode="low", current_messages=messages)
    tight = measured.projection("low").estimated_tokens + low.output_reserve_tokens - 1
    old = low.projection("low").memory_facts["floor"]
    rebound, mode, event = _rebind(monkeypatch, tmp_path / "n", low, messages, window=tight)
    floor = rebound.projection("low").memory_facts["floor"]
    assert mode == "low" and event["view_kept"] is False and messages[0] == rebound.projection("low").system_message()
    assert all(floor["steps"].get(step, 0) >= count for step, count in old["steps"].items())  # nothing unfolds
    assert sum(floor["steps"].values()) > sum(old["steps"].values())
    assert floor["by_budget"] == old["by_budget"] and floor["budget_allowance_tokens"] is None  # no target refold
    assert floor["allowance_tokens"] == floor["physical_allowance_tokens"]  # no working margin reapplied
    assert messages[1:] == _long_tail(low)[1:]  # the actual tail is never touched


def test_a_rebind_never_recuts_by_a_window_this_model_already_exceeded_here(tmp_path, monkeypatch):
    """Review r1 (17.10): an account rotation re-cut memory by a catalogue window the task had already
    disproved (Codex: 872k stated, 921,840 accepted). The model's accepted floor makes that window
    unknown, so the sent view stays; without the floor the same window narrows it (the test above)."""
    from ouroboros.context_anchor import record_input_anchor
    from tests.test_memory_view_modes import _built

    _core, plan = _built(tmp_path / "w", monkeypatch)
    low = plan(1_000_000, preferred="low", tool_schemas=[])
    messages = _long_tail(low)
    measured = low.reproject_for_route(window_tokens=1_000_000, known_window=True, ratio=1.0, output_reserve=65_536,
                                       tool_schemas=[], start_mode="low", current_messages=messages)
    tight = measured.projection("low").estimated_tokens + low.output_reserve_tokens - 1
    usage = {"_last_round_cache_usage": {"prompt_tokens": tight + 50_000, "provider": "p"},
             "_context_raw_input_tokens": tight + 50_000}
    record_input_anchor(usage, SimpleNamespace(model="m2", provider="p"), round_idx=7)
    old = low.projection("low").memory_facts["floor"]
    rebound, mode, event = _rebind(monkeypatch, tmp_path / "n", low, messages, window=tight, usage=usage)
    assert mode == "low" and event["view_kept"] is True
    assert rebound.projection("low").memory_facts["floor"]["steps"] == old["steps"]  # nothing re-cut


@pytest.mark.parametrize("owner,ran,mode", [
    ("low", None, "low"), ("max", "low", "low"), ("max", "nano", "nano"), ("low", "nano", "nano"),
    ("max", "unknown", "max"), ("low", "max", "low")],
    ids=["owner-low", "max-task-local-low", "max-task-local-nano", "low-task-local-nano", "unknown-mode", "owner-lowered"])
def test_a_cold_restore_sends_the_new_capture_at_its_own_level_not_refolded_to_the_restored_tail(tmp_path, monkeypatch,
                                                                                                  owner, ran, mode):
    """The production owner-wait cold restore (the saved route restored onto the task, then its rebind). The new
    capture is sent in the mode the task ran in, never above the owner's current one: a task-local Low or Nano under
    the owner's Max gets no Max books back; a saved value that names no mode leaves the owner's."""
    import ouroboros.context
    import ouroboros.loop
    from ouroboros import context_fit
    from ouroboros.owner_wait import resume_native_loop
    from ouroboros.tools.registry import ToolRegistry
    from tests.test_memory_view_modes import _built

    _core, plan = _built(tmp_path / "w", monkeypatch)
    fresh = plan(1_000_000, preferred=owner, tool_schemas=[])  # the restarted task's new capture
    restored = [{"role": "system", "content": "THE VIEW SENT BEFORE THE RESTART"}, *_long_tail(fresh)[1:]]
    registry = ToolRegistry(repo_dir=tmp_path / "r", drive_root=tmp_path / "r")
    registry._ctx.context_fit_plan, registry._ctx.owner_wait_resume = fresh, {}
    registry._ctx.owner_wait_callback = lambda *_a: None
    evidence = SimpleNamespace(route_fp="r", status="asserted", stale=False, window_tokens=1_000_000)
    monkeypatch.setattr(ouroboros.context, "_context_fit_route", lambda *_a, **_kw: ({"model": "m", "provider": "p"}, evidence))
    monkeypatch.setattr(context_fit, "_route_calibration_ratio", lambda *_a: 1.0)
    monkeypatch.setattr(ouroboros.loop, "get_context_mode", lambda: owner)
    state = {"messages": copy.deepcopy(restored), "trace": {}, "usage": {}, "seen": [], "owner_directives": [],
             "route": {"active_model": "m", "active_effort": "high", "active_use_local": False, "active_context_mode": ran},
             "delivery": {}, "acceptance": {}, "context_model_role": "main",
             "tool_schemas": [], "round_idx": 3}
    messages = []
    *_route, active, _round, rebound = resume_native_loop(registry, state, messages, {}, {}, set())
    assert active == mode == registry._ctx.active_context_mode == rebound.initial_mode
    assert messages[1:len(restored)] == restored[1:]  # the restored tail, then the restart notice
    assert messages[0] == fresh.projection(mode).system_message()  # the new capture, never the old bytes
    assert (messages[0] == fresh.projection("max").system_message()) is (mode == "max")  # Max books only in Max
    assert rebound.projection(mode).memory_facts == fresh.projection(mode).memory_facts


def _native(main_call, monkeypatch, tmp_path, *, task_text="hi", owner_mode="low", view=None, books=0):
    """The owner's ``owner_mode`` on the fixture's real Claudexor Main call with a captured core (``view``: another
    memory snapshot in it; ``books``: Max's books grown by that many characters, as ``test_memory_view_modes._built``
    grows them): ``(ctx, gateway, tools, events, plan(window), windows)``. ``windows`` maps an account
    to the window its evidence reports (0: unknown);
    the primary pins account-a, its configured fallback account-b, and every production rebind reads it."""
    from ouroboros import context, fallback_cooldown, loop_transport
    from ouroboros.model_slots import MODEL_ACCOUNTS_KEY
    from ouroboros.tool_policy import initial_tool_schemas
    from tests._memory_view_context import world
    from tests.test_model_wait_controls import _loop_tools

    ctx, gateway, owner, events, _decide, _observations = main_call
    for key, value in {"OUROBOROS_CONTEXT_MODE": owner_mode, "OUROBOROS_TASK_REVIEW_MODE": "off",
                       "OUROBOROS_SAFETY_MODE": "off", "OUROBOROS_MAX_ROUNDS": "4", "MCP_ENABLED": "false",
                       "OUROBOROS_MODEL": MODEL, "OUROBOROS_MODEL_FALLBACKS": MODEL,
                       MODEL_ACCOUNTS_KEY: json.dumps({"main": "account-a", "fallback": ["account-b"]})}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("USE_LOCAL_MAIN", raising=False)
    monkeypatch.delenv("USE_LOCAL_FALLBACK", raising=False)
    monkeypatch.setattr(fallback_cooldown, "is_cooling_down", lambda *_a: False)
    monkeypatch.setattr(loop_transport, "upstream_transport_reachable", lambda *_a, **_kw: pytest.fail("no probe"))
    windows = {}

    def evidence(account, window):
        return SimpleNamespace(route_fp=f"capacity-{account}-{window}", status="confirmed" if window else "unprobeable",
                               stale=False, window_tokens=window, source_id="codex", source="advertised",
                               credential_profile_id=account, account_fingerprint=f"fingerprint-{account[-1]}")

    def route(task, **_kw):  # the account a production rebind asks for: explicit, the role's pin, or observed
        explicit = task.get("credential_profile_id")
        account = (explicit if explicit is not None else "account-b" if task["model_role"] == "fallback:0"
                   else "account-a") or (task.get("model_route") or {}).get("credentialProfileId") or "account-a"
        return {"model": MODEL, "provider": "claudexor"}, evidence(account, windows[account])

    monkeypatch.setattr(context, "_context_fit_route", route)
    tools = _loop_tools(ctx, owner)
    tools._ctx.task_metadata = {"model_role": "main"}
    tools._ctx.model_wait_context = owner
    env, memory, _rooms = world(tmp_path / "w")
    task = {"type": "task", "text": task_text, "id": "tmain", "chat_id": 1}
    core = context._capture_context_core(env, memory, task, None, None)
    core = core if view is None else dataclasses.replace(core, memory_view_json=view)
    core = dataclasses.replace(core, architecture_md=core.architecture_md + "\n\n" + "word " * (books // 5)) if books else core
    schemas = initial_tool_schemas(tools, context_mode=owner_mode)

    def plan(window):
        built = context._build_context_fit_plan(env, core, task, preferred_mode=owner_mode, tool_schemas=schemas,
            route_resolver=lambda *_a, **_kw: ({"model": MODEL, "provider": "claudexor"}, evidence("account-a", window)))
        return dataclasses.replace(built, model_role="main", model_route=copy.deepcopy(ROUTE))

    return ctx, gateway, tools, events, plan, windows


def _run(ctx, tools, events, plan):
    from ouroboros import loop

    ctx.context_fit_plan = tools._ctx.context_fit_plan = plan
    return loop.run_llm_loop(ctx.messages, tools, ctx.llm, ctx.drive_logs, lambda *_a, **_kw: None, queue.Queue(),
                             task_id="task-one", drive_root=ctx.drive_root, event_queue=events, initial_effort="high")


def _drained(events):
    """The task's logged events, in order, from the loop's event queue."""
    rows = []
    while not events.empty():
        rows.append(events.get_nowait().get("data") or {})
    return rows


def _answer(route, message):
    row = result(route=route)
    row["message"] = message
    return row


@pytest.mark.parametrize("primary,fallback", [("tight", "roomy"), ("roomy", "tight")], ids=["to-larger", "to-smaller"])
def test_a_fallback_and_its_return_resend_the_shown_history_byte_for_byte_on_the_wire(main_call, monkeypatch, tmp_path,
                                                                                        primary, fallback):
    """#1728 on the production send. The primary's outcome is unknown; its configured fallback answers on
    another account and window and calls switch_model back; the primary answers. While each request fits,
    every request the gateway receives repeats the previous one byte for byte -- the leading system items,
    the [SYSTEM NOTICE] with the memory rooms and their floor note, the task, every turn -- and only appends;
    the request-local clock row alone is replaced. A larger window unfolds nothing, and a smaller one that
    still holds the request re-cuts nothing. A kept floor note names the window its view was cut for; the
    round's facts line names the acting one."""
    from ouroboros.send_clock import CLOCK_NOTE_PREFIX
    from ouroboros.tool_policy import initial_tool_schemas

    ctx, gateway, tools, events, plan, windows = _native(main_call, monkeypatch, tmp_path)
    roomy = plan(900_000)
    full = roomy.projected_tokens_with_tools("low", initial_tool_schemas(tools, context_mode="low"))
    width = {"roomy": 900_000, "tight": full + roomy.output_reserve_tokens + 12_000}  # holds the request, not the margins
    assert "### Physical floor" in plan(width["tight"]).low_projection.system_content_json  # its view was narrowed
    windows.update({"account-a": width[primary], "account-b": width[fallback]})
    built = plan(width[primary])
    ctx.messages[:] = [*built.messages_for("low"), result()["message"],
                       {"role": "tool", "tool_call_id": "a", "content": "verified read A"},
                       {"role": "tool", "tool_call_id": "b", "content": "completed review B"}]
    back = {"id": "return-primary", "type": "function", "function": {
        "name": "switch_model", "arguments": json.dumps({"primary": "return"})}}
    gateway.results = [result(outcome="unknown", route=ROUTE),
                       _answer({**ROUTE, "credentialProfileId": "account-b", "accountFingerprint": "fingerprint-b"},
                               {"role": "assistant", "content": "Returning.", "tool_calls": [back]}),
                       _answer(ROUTE, {"role": "assistant", "content": "Primary answered after return."})]
    gateway.dispatch = ["unknown", "response_received", "response_received"]
    text, _usage, _trace = _run(ctx, tools, events, built)

    payloads = [payload for payload, _key in gateway.uploads]
    assert text == "Primary answered after return." and len(payloads) == len(gateway.creates) == 3
    assert [row["physical_context"]["capacity_total_tokens"] for row in attempt_rows_in_start_order(ctx.drive_root)] == [
        width[primary], width[fallback], width[primary]]
    for earlier, later in zip(payloads, payloads[1:]):
        assert earlier["messages"][-1]["content"].startswith(CLOCK_NOTE_PREFIX)
        assert later["messages"][:len(earlier["messages"]) - 1] == earlier["messages"][:-1]  # resent, then appended
        assert later["tools"] == earlier["tools"] and later["options"]["cacheKey"] == earlier["options"]["cacheKey"]
    sent = payloads[1]["messages"]
    notice = next(row["content"] for row in sent if str(row["content"]).startswith("[SYSTEM NOTICE]"))
    facts = [row["content"] for row in sent if str(row["content"]).startswith("[CONTEXT_FACTS]")]
    assert f"window {width[fallback]:,}" in facts[-1] and "target 250,000" in facts[-1]  # the acting route, fresh
    assert (f"This Low view retains memory cuts for the window ({width['tight']} tokens)." in notice) == (primary == "tight")
    rebound = [row for row in _drained(events) if row.get("checkpoint_kind") == "context_fit_route_rebound"]
    assert [(row["window_tokens"], row["view_kept"]) for row in rebound] == [(width[fallback], True), (width[primary], True)]


def _addressed(plan, mode):
    """The (step, element) pairs a projection's view shows only by address, read from its floor fact."""
    from ouroboros import memory_floor as mf
    from ouroboros.memory_view import snapshot_from_json

    level = mf.kept_level(snapshot_from_json(plan.core.memory_view_json), plan.projection(mode).memory_facts)
    return {(step, ident) for step, ids in level.addressed for ident in ids}


def _unfolded(plan, addressed, message):
    """The elements of ``addressed`` whose whole text ``message`` shows again. A retold room (F3) is never
    one text in a view (its records interleave in story order): its floor fact count answers for it."""
    from ouroboros import memory_floor as mf
    from ouroboros.memory_view import snapshot_from_json

    sent = json.dumps(message, ensure_ascii=False)
    return [(step, ident) for step, ident, whole, _short in mf.floor_elements(snapshot_from_json(plan.core.memory_view_json))
            if (step, ident) in addressed and json.dumps(whole, ensure_ascii=False)[1:-1] in sent]


def _shown_low(main_call, monkeypatch, tmp_path, cut):
    """The owner's Low view on account-a as the waiting Main call's transcript, cut by that window alone
    (``window``) or by the Low budget too (``budget``: this room's retold records about five times as long):
    ``(ctx, tools, events, windows, built)``. Account-b reports a 100,000-token window."""
    from ouroboros.memory_view import snapshot_json
    from ouroboros.tool_policy import initial_tool_schemas
    from tests import _memory_view_synthetic as syn

    actor = syn.actor("main")
    if cut == "budget":
        actor = dataclasses.replace(actor, room={**actor.room, "legacy": syn.room("1", legacy=23, legacy_chars=40_000)[
            "legacy"]})
    ctx, _gateway, tools, events, plan, windows = _native(main_call, monkeypatch, tmp_path, view=snapshot_json(actor))
    windows.update({"account-a": 150_000 if cut == "window" else 1_000_000, "account-b": 100_000})
    built = plan(windows["account-a"])
    assert built.initial_mode == "low" and (built.low_projection.memory_facts["floor"]["by_budget"] > 0) is (cut == "budget")
    ctx.tools, ctx.context_fit_plan, ctx.active_context_mode = tools, built, "low"
    ctx.tool_schemas = initial_tool_schemas(tools, context_mode="low")
    ctx.messages[:] = [*built.messages_for("low"), result()["message"],  # the shown Low view, then the round's tail
                       {"role": "tool", "tool_call_id": "a", "content": "verified read A"},
                       {"role": "tool", "tool_call_id": "b", "content": "completed review B"}]
    tools._ctx.context_fit_plan, tools._ctx.messages, tools._ctx.active_context_mode = built, ctx.messages, "low"
    tools._ctx.event_queue = events
    whole_view = plan(10_000_000).messages_for("max")[0]  # nothing addressed: the check sees all but F3
    addressed = _addressed(built, "low")
    assert addressed and not _unfolded(built, addressed, ctx.messages[0])
    assert set(_unfolded(built, addressed, whole_view)) == {pair for pair in addressed if pair[0] != "F3"}
    return ctx, tools, events, windows, built


def _reprepare_onto(ctx, account):
    """The production reprepare of the waiting Main call onto ``account`` (an owner's or a rotation's choice)."""
    from ouroboros import loop
    from ouroboros import usage_accounting as ua
    from ouroboros.loop_model_call import _reprepare_waiting_main

    physical = loop._physical_context_for_fit(loop._measure_round_main_fit(ctx, automatic_pass_used=False))
    with ua.bind_physical_attempt_context(physical):
        return _reprepare_waiting_main(ctx, {"messages": copy.deepcopy(ctx.messages), "model": MODEL, "model_role": "main",
                                             "tools": ctx.tool_schemas, "model_account_override": account})


@pytest.mark.parametrize("cut", ["window", "budget"])
def test_an_account_reprepare_that_selects_nano_narrows_the_shown_low_view_and_keeps_its_provenance(
        main_call, monkeypatch, tmp_path, cut):
    """#1728 across modes, on the production reprepare of a waiting Main call. The owner's Low view was cut for
    one account's window -- by that window alone, or by the Low budget too. The call moves to another account
    whose window cannot hold Low even with my shortest memory, so Nano is selected: it narrows the view the
    task showed by that window alone, never the Nano view prepared at startup and never sent. Every address
    stays an address, the budget that took some of them is still the Low budget, and the tail is untouched."""
    from ouroboros.context_budget import OWNER_LOW_TARGET_TOKENS

    ctx, tools, events, _windows, built = _shown_low(main_call, monkeypatch, tmp_path, cut)
    shown_floor = built.low_projection.memory_facts["floor"]
    tail, addressed = copy.deepcopy(ctx.messages[1:]), _addressed(built, "low")
    prepared = _reprepare_onto(ctx, "account-b")

    after = ctx.context_fit_plan
    # The narrower window's legitimate narrowing: Low cannot hold even my shortest memory there, Nano fits.
    assert ctx.active_context_mode == tools._ctx.active_context_mode == after.initial_mode == "nano"
    assert ctx.accumulated_usage["_context_capacity_total_tokens"] == 100_000
    assert ctx.accumulated_usage["_context_capacity_deficit_tokens"] == 0
    assert ctx.messages[0] == after.projection("nano").system_message() and ctx.messages[1:len(tail) + 1] == tail
    assert prepared.kwargs["messages"][0]["content"] == ctx.messages[0]["content"]
    # Narrowed from the shown level: every address stays one and no whole text of one comes back.
    assert addressed <= _addressed(after, "nano") and not _unfolded(after, addressed, ctx.messages[0])
    floor = after.projection("nano").memory_facts["floor"]
    assert floor["mode_switch"] == {"from": "low", "to": "nano"} and floor["budget_allowance_tokens"] is None
    # Provenance: what the Low budget took stays the Low budget's, never a Nano budget's; no budget, no target.
    assert floor["by_budget"] == shown_floor["by_budget"]
    assert floor["target_tokens"] == (OWNER_LOW_TARGET_TOKENS if cut == "budget" else None)
    sent = json.dumps(ctx.messages[0], ensure_ascii=False)
    assert ("and the Low mode budget (250000 tokens)." in sent) is (cut == "budget")
    assert "This Nano view retains memory cuts for the window (100000 tokens)" in sent and "Nano mode budget" not in sent
    assert "cannot hold Low with even the shortest view of my memory; this task runs in Nano." in sent  # began in Low
    rebound = [row for row in _drained(events) if row.get("checkpoint_kind") == "context_fit_route_rebound"]
    assert [(row["effective_mode"], row["window_tokens"], row["view_kept"]) for row in rebound] == [
        ("nano", 100_000, False)]


@pytest.mark.parametrize("second", ["tighter_account", "size_refusal"])
def test_a_second_narrowing_in_nano_still_names_what_the_low_budget_took(main_call, monkeypatch, tmp_path, second):
    """#1728 provenance across hops. After the budget case's move to Nano, Nano is the shown mode and a second
    narrowing comes: a tighter account on the production reprepare, or the provider's actual size refusal on
    the production memory rung. Nano narrows its own shown view; what the Low budget took is still counted,
    with the Low target, and named as the Low budget's -- never a Nano budget's -- and nothing unfolds."""
    from ouroboros import loop
    from ouroboros import memory_floor as mf
    from ouroboros.context_budget import NANO_MIN_HEADROOM_TOKENS, OWNER_LOW_TARGET_TOKENS
    from ouroboros.loop_round_limits import _coarsen_memory_view

    ctx, _tools, events, windows, built = _shown_low(main_call, monkeypatch, tmp_path, "budget")
    by_budget = built.low_projection.memory_facts["floor"]["by_budget"]
    _reprepare_onto(ctx, "account-b")
    first = ctx.context_fit_plan
    assert ctx.active_context_mode == "nano" and first.projection("nano").memory_facts["floor"]["by_budget"] == by_budget
    addressed = _addressed(first, "nano")
    _drained(events)
    if second == "tighter_account":  # 5,000 tokens short of the request Nano now sends
        window = windows["account-c"] = (first.projected_tokens_with_tools("nano", ctx.tool_schemas)
                                         + NANO_MIN_HEADROOM_TOKENS - 5_000)
        _reprepare_onto(ctx, "account-c")
        kinds = [(row.get("checkpoint_kind"), row.get("view_kept")) for row in _drained(events)]
        assert ("context_fit_route_rebound", False) in kinds
    else:  # the same route refuses the request by 5,000 tokens
        window, fit = 100_000, loop._measure_round_main_fit(ctx, automatic_pass_used=False)
        assert _coarsen_memory_view(ctx, fit, saving=5_000) is True  # the floor's memory step, sized by the refusal
    after = ctx.context_fit_plan
    floor = after.projection("nano").memory_facts["floor"]
    assert ctx.active_context_mode == "nano" and ctx.messages[0] == after.projection("nano").system_message()
    assert addressed < _addressed(after, "nano") and not _unfolded(after, addressed, ctx.messages[0])
    assert floor["by_budget"] == by_budget and floor["target_tokens"] == OWNER_LOW_TARGET_TOKENS
    sent = json.dumps(ctx.messages[0], ensure_ascii=False)
    assert (f"This Nano view retains memory cuts for the window ({window} tokens) and the Low mode budget (250000 tokens)."
            in sent) and "Nano mode budget" not in sent
    assert "cannot hold Low with even the shortest view of my memory; this task runs in Nano." in sent
    assert ctx.tools._ctx.memory_view_facts == mf.trace_facts(after.projection("nano").memory_facts)  # the view sent


def test_a_selected_lower_view_with_an_unsent_ones_counts_still_carries_the_shown_budget(main_call, monkeypatch, tmp_path):
    """#1728 provenance when counts coincide. The plan also holds a Nano view for account-b's window rendered
    from Nano's own level (as a rebind starting in Nano renders it) and never sent: its mode line says the
    window lowered Low, and no budget took anything in it. The shown Low view then moves to Nano and its
    narrowing lands on the same counts. The selected view is the shown one's, with its budget provenance."""
    from ouroboros.context_budget import OWNER_LOW_TARGET_TOKENS

    ctx, _tools, _events, _windows, built = _shown_low(main_call, monkeypatch, tmp_path, "budget")
    frame = dict(window_tokens=100_000, known_window=True, ratio=1.0, output_reserve=built.output_reserve_tokens,
                 tool_schemas=ctx.tool_schemas, current_messages=ctx.messages)
    unsent = built.reproject_for_route(**frame, start_mode="nano").nano_projection.memory_facts["floor"]
    assert unsent["by_budget"] == 0 and unsent["mode_switch"] == {"from": "low", "to": "nano"}
    prepared = built.reproject_for_route(**frame, start_mode="nano")
    moved = dataclasses.replace(built, nano_projection=prepared.nano_projection).reproject_for_route(
        **frame, start_mode="low")
    floor = moved.projection("nano").memory_facts["floor"]
    assert moved.initial_mode == "nano" and floor["steps"] == unsent["steps"]  # the same counts
    assert floor["by_budget"] == built.low_projection.memory_facts["floor"]["by_budget"]
    assert floor["target_tokens"] == OWNER_LOW_TARGET_TOKENS
    assert "and the Low mode budget (250000 tokens)." in moved.projection("nano").system_content_json


@pytest.mark.parametrize("fallback", ["roomy", "tight"])
def test_an_owner_max_task_in_task_local_low_rebinds_a_fallback_and_its_return_from_low(main_call, monkeypatch, tmp_path,
                                                                                       fallback):
    """#1728 on the production send with the owner's Max: the task already runs task-local Low (an earlier
    overflow lowered it). The primary's outcome is unknown; its configured fallback answers on another account
    and calls switch_model back; the primary answers. Both rebinds start from the mode the task shows. On a
    window that holds Low every request repeats the previous one byte for byte and Max's books never return;
    on one that cannot hold Low even with my shortest memory, Nano narrows the shown Low view, and the
    primary's roomier window keeps Nano: a route never raises the mode."""
    from ouroboros.memory_view import snapshot_json
    from ouroboros.send_clock import CLOCK_NOTE_PREFIX
    from tests import _memory_view_synthetic as syn

    ctx, gateway, tools, events, plan, windows = _native(main_call, monkeypatch, tmp_path, owner_mode="max",
                                                         view=snapshot_json(syn.actor("main")))
    windows.update({"account-a": 150_000, "account-b": {"roomy": 900_000, "tight": 100_000}[fallback]})
    built = dataclasses.replace(plan(150_000), initial_mode="low")  # the owner's Max plan, task-local Low shown
    low = built.messages_for("low")[0]
    assert built.preferred_mode == "max" and low != built.messages_for("max")[0]
    ctx.messages[:] = [*built.messages_for("low"), result()["message"],
                       {"role": "tool", "tool_call_id": "a", "content": "verified read A"},
                       {"role": "tool", "tool_call_id": "b", "content": "completed review B"}]
    back = {"id": "return-primary", "type": "function", "function": {
        "name": "switch_model", "arguments": json.dumps({"primary": "return"})}}
    gateway.results = [result(outcome="unknown", route=ROUTE),
                       _answer({**ROUTE, "credentialProfileId": "account-b", "accountFingerprint": "fingerprint-b"},
                               {"role": "assistant", "content": "Returning.", "tool_calls": [back]}),
                       _answer(ROUTE, {"role": "assistant", "content": "Primary answered after return."})]
    gateway.dispatch = ["unknown", "response_received", "response_received"]
    text, _usage, _trace = _run(ctx, tools, events, built)

    payloads = [payload["messages"] for payload, _key in gateway.uploads]
    assert text == "Primary answered after return." and len(payloads) == 3
    rebound = [row for row in _drained(events) if row.get("checkpoint_kind") == "context_fit_route_rebound"]
    mode = {"roomy": "low", "tight": "nano"}[fallback]
    assert [(row["preferred_mode"], row["effective_mode"], row["view_kept"]) for row in rebound] == [
        ("max", mode, fallback == "roomy"), ("max", mode, True)]
    assert tools._ctx.active_context_mode == mode and tools._ctx.context_fit_plan.preferred_mode == "max"
    assert tools._ctx.messages[0] == tools._ctx.context_fit_plan.projection(mode).system_message()
    assert ("cannot hold Max with even the shortest view of my memory; this task runs in Nano."  # kept by the return
            in json.dumps(tools._ctx.messages[0], ensure_ascii=False)) is (fallback == "tight")
    for earlier, later in zip(payloads[1 if fallback == "tight" else 0:], payloads[2 if fallback == "tight" else 1:]):
        assert earlier[-1]["content"].startswith(CLOCK_NOTE_PREFIX)
        assert later[:len(earlier) - 1] == earlier[:-1]  # resent, then appended
    if fallback == "tight":  # only the shown view changed (system items, notice); it narrows the Low one
        notice = next(i for i, row in enumerate(payloads[0]) if str(row["content"]).startswith("[SYSTEM NOTICE]"))
        assert payloads[1][notice + 1:len(payloads[0]) - 1] == payloads[0][notice + 1:-1]
        assert payloads[1][:notice + 1] != payloads[0][:notice + 1]
        addressed = _addressed(built, "low")
        assert addressed <= _addressed(tools._ctx.context_fit_plan, "nano")
        assert not _unfolded(built, addressed, tools._ctx.messages[0])


# --- the floor's memory step still narrows when the estimate fits -----------------------------------

def _refusal_ctx(tmp_path, plan):
    from ouroboros import memory_floor as mf

    shown = mf.trace_facts(plan.projection("max").memory_facts)  # as the task's start records the view it sends
    tool_ctx = SimpleNamespace(context_fit_plan=plan, messages=None, memory_view_facts=shown,
                               _execution_trace={VIEW_TRACE_KEY: dict(shown)})
    ctx = SimpleNamespace(context_fit_plan=plan, active_context_mode="max", messages=plan.messages_for("max"),
                          tool_schemas=[], tools=SimpleNamespace(_ctx=tool_ctx), task_id="t", round_idx=1,
                          event_queue=None, drive_logs=tmp_path)
    tool_ctx.messages = ctx.messages
    return ctx


def test_an_actual_refusal_narrows_memory_even_when_the_estimate_fits(tmp_path, monkeypatch):
    from ouroboros import loop
    from ouroboros import memory_floor as mf
    from ouroboros.context_fit import measure_main_fit
    from ouroboros.loop_round_limits import _coarsen_memory_view
    from ouroboros.memory_view import snapshot_from_json
    from tests.test_memory_view_modes import _built

    core, plan = _built(tmp_path / "w", monkeypatch, books=0)
    built = plan(10_000_000)  # a startup view with every element whole
    events = []
    monkeypatch.setattr(loop, "_emit_checkpoint_event", lambda *_a, **_kw: events.append(_a[-1]))
    ctx = _refusal_ctx(tmp_path, built)
    fit = measure_main_fit(built, ctx.messages, [], profile="owner_max", rendered_mode="max",
                           round_id="e:round:1")
    assert fit.measurement.capacity_deficit_tokens == 0  # the estimate says it fits; the provider refused
    assert built.projection("max").memory_facts["floor"]["steps"] == {}
    assert _coarsen_memory_view(ctx, fit) is True  # still a strict narrowing: at least one element
    first = ctx.context_fit_plan.projection("max")
    assert first.memory_facts["floor"]["steps"] and first.estimated_tokens < built.projection("max").estimated_tokens
    assert ctx.messages[0] == first.system_message() and ctx.tools._ctx.context_fit_plan is ctx.context_fit_plan
    assert [e["checkpoint_kind"] for e in events] == ["context_memory_coarsened"]
    # A measured deficit is saved in full, from the level the refused view showed: nothing it addressed comes back.
    snapshot = snapshot_from_json(core.memory_view_json)
    kept = {(step, ident) for step, ids in mf.kept_level(snapshot, first.memory_facts).addressed for ident in ids}
    left = sum(whole - short for step, ident, whole, short in mf.degradable_elements(snapshot) if (step, ident) not in kept)
    deficit = max(1, left // 2)
    assert _coarsen_memory_view(ctx, fit, saving=deficit) is True  # the measured target, from the level shown
    second = ctx.context_fit_plan.projection("max")
    assert all(second.memory_facts["floor"]["steps"].get(step, 0) >= count
               for step, count in first.memory_facts["floor"]["steps"].items())
    assert first.estimated_tokens - second.estimated_tokens >= deficit - 2  # estimator rounding of the rendered view
    assert second.memory_facts["floor"]["allowance_tokens"] == second.memory_facts["floor"]["physical_allowance_tokens"]
    # Each narrowing is the fact the task keeps (read back as a memory shortage); a rung that changes nothing keeps it.
    tool_ctx, trace = ctx.tools._ctx, ctx.tools._ctx._execution_trace
    assert tool_ctx.memory_view_facts == trace[VIEW_TRACE_KEY] == mf.trace_facts(second.memory_facts)
    everything = sum(whole for _step, _ident, whole, _short in mf.degradable_elements(snapshot))
    assert _coarsen_memory_view(ctx, fit, saving=everything) is True
    kept = tool_ctx.memory_view_facts
    assert kept == trace[VIEW_TRACE_KEY] == mf.trace_facts(ctx.context_fit_plan.projection("max").memory_facts)
    assert _coarsen_memory_view(ctx, fit, saving=everything) is False  # every element is already an address
    assert tool_ctx.memory_view_facts is kept and trace[VIEW_TRACE_KEY] == kept


def test_a_low_retry_after_memory_narrowing_keeps_every_address_the_refused_max_request_showed(
        main_call, monkeypatch, tmp_path):
    """#1728 on the production refusal passes, owner Max with rich memory. The owner's long words while the task
    ran take the request past the window; the provider refuses it by size; nothing shown is addressable, so the
    second pass narrows the shown Max view by the deficit and, memory alone not saving it, drops the books in
    the same pass. The Low request the gateway receives shows my memory as the refused Max request did -- the
    Low view prepared at startup and never sent does not come back -- and every turn is resent."""
    from ouroboros import context_fit, memory_floor as mf
    from ouroboros.memory_inventory import shortage_from_trace
    from ouroboros.memory_view import snapshot_from_json, snapshot_json
    from ouroboros.tool_policy import initial_tool_schemas
    from tests import _memory_view_synthetic as syn

    ctx, gateway, tools, events, plan, windows = _native(main_call, monkeypatch, tmp_path, owner_mode="max",
                                                         view=snapshot_json(syn.actor("main")), books=120_000)
    roomy = plan(10_000_000)
    full = roomy.projected_tokens_with_tools("max", initial_tool_schemas(tools, context_mode="max"))
    # A window that holds Max with all of my memory and both working margins; the owner's words then pass it by 20,000.
    windows["account-a"] = window = math.ceil((full + roomy.output_reserve_tokens) * 4 / 3) + 4_000
    built = plan(window)
    assert built.initial_mode == "max" and not built.projection("max").memory_facts["floor"]["steps"]  # startup: whole
    words = "Owner, while you worked: keep the old API alive. "
    owner = {"role": "user", "content": words * math.ceil((window - full - roomy.output_reserve_tokens + 20_000) * 4 / len(words))}
    ctx.messages[:] = [*built.messages_for("max"), owner]
    full_low = built.reproject_transcript(ctx.messages, "low")
    assert context_fit.estimate_context_prompt_tokens(full_low, initial_tool_schemas(tools, context_mode="low")) + \
        built.output_reserve_tokens <= window  # after dropping books the original, uncut memory would fit
    refusal = result(outcome="failed", problem={"code": "context_length_exceeded", "message": "This model's maximum "
                     f"context length is {window} tokens. However, your messages resulted in {window + 20_000} tokens."})
    gateway.results = [refusal, _answer(ROUTE, {"role": "assistant", "content": "Kept."})]
    gateway.dispatch = ["response_received"] * 2
    tools._ctx.memory_view_facts = mf.trace_facts(built.projection("max").memory_facts)  # as the start records it
    text, _usage, trace = _run(ctx, tools, events, built)

    first, low = (payload["messages"] for payload, _key in gateway.uploads)
    kinds = [row.get("checkpoint_kind") for row in _drained(events)]
    assert text == "Kept." and len(gateway.creates) == 2 and tools._ctx.active_context_mode == "low"
    assert kinds.index("context_memory_coarsened") < kinds.index("context_fit_low_retry")  # one pass: memory, then books

    def shows(sent, whole):
        return json.dumps(whole, ensure_ascii=False)[1:-1] in json.dumps(sent, ensure_ascii=False)

    elements = mf.floor_elements(snapshot_from_json(built.core.memory_view_json))
    narrowed = {(step, ident) for step, ident, whole, _short in elements if shows(first, whole) and not shows(low, whole)}
    assert {step for step, _ident in narrowed} >= {"F1", "F1b"}  # the deficit took more than one element
    # Max sends [books, handbook, story, rooms notice, turns...]; Low [book navigation, story, rooms notice, turns...].
    assert len(json.dumps(low[0])) < len(json.dumps(first[0]))
    assert low[3:-2] == first[4:-1]  # every turn resent; one facts line appended, the clock row replaced
    note = low[2]["content"]
    assert f"This Low view retains memory cuts for the window ({window} tokens)." in note
    assert "does not hold all of my memory" not in note and "cannot hold" not in note
    # The task's trace keeps the fact of the Low view sent, not the whole Max view of its start: a writer reads
    # what the request actually showed only by address.
    sent_fact = mf.trace_facts(tools._ctx.context_fit_plan.projection("low").memory_facts)
    assert trace[VIEW_TRACE_KEY] == tools._ctx.memory_view_facts == sent_fact
    assert sent_fact["floor"]["mode"] == "low" and sent_fact["floor"]["steps"].keys() >= {"F1", "F1b"}
    assert shortage_from_trace(trace).kind == "narrative"  # the old pages the Low request showed by pointer
    # A later fitting route keeps this historical note and every address without reasserting a new shortage.
    after = tools._ctx.context_fit_plan
    rebound = after.reproject_for_route(window_tokens=window * 2, known_window=True, ratio=1.0,
        output_reserve=after.output_reserve_tokens, tool_schemas=initial_tool_schemas(tools, context_mode="low"),
        start_mode="low", current_messages=ctx.messages)
    assert rebound.initial_mode == "low" and rebound.preferred_mode == "max"
    assert rebound.low_projection.system_content_json == after.low_projection.system_content_json
    assert rebound.low_projection.memory_facts == after.low_projection.memory_facts
    assert _addressed(rebound, "low") == _addressed(after, "low")


def test_a_giant_chosen_read_on_an_unknown_window_recovers_from_the_providers_size_refusal_by_its_exact_source(
        main_call, monkeypatch, tmp_path):
    """#1727 A on the production send, its named cost included. The window is unknown, so a page the mind asked
    for arrives as chosen, far past the Low target; the provider then refuses that request by its actual size
    (a synthetic refusal) and the production ladder recovers on the same route: the unconfirmed giant body
    becomes its exact checkpointed source with the read that reaches it, the owner's task text stays the same
    bytes in every send, and the read ran once -- one strictly smaller resend, no tool executed again."""
    import functools

    from ouroboros.context_budget import OWNER_LOW_TARGET_TOKENS

    task_text = "Read giant.txt whole and report its LAST line verbatim. Keep the old API alive."
    ctx, gateway, tools, events, plan, windows = _native(main_call, monkeypatch, tmp_path, task_text=task_text)
    windows["account-a"] = 0  # no fresh window evidence anywhere
    built = plan(0)
    ctx.messages[:] = built.messages_for("low")
    giant = "".join(f"row {n:07d} of the giant module: payload payload payload\n" for n in range(20_000))
    giant += "LAST LINE: keep the old API alive\n"
    (ctx.drive_root / "giant.txt").write_text(giant, encoding="utf-8")
    runs, read_file = [], tools._entries["read_file"].handler

    @functools.wraps(read_file)
    def counted(*args, **kwargs):  # the tool's effect, counted where the registry executes it
        runs.append(args)
        return read_file(*args, **kwargs)

    tools.override_handler("read_file", counted)
    read = {"id": "giant-read", "type": "function", "function": {"name": "read_file", "arguments": json.dumps(
        {"path": "giant.txt", "start_line": 1, "max_lines": 30_000})}}
    gateway.results = [
        _answer(ROUTE, {"role": "assistant", "content": "Reading it.", "tool_calls": [read]}),
        result(outcome="failed", problem={"code": "context_length_exceeded", "message": "This model's maximum context "
               "length is 272000 tokens. However, your messages resulted in 352113 tokens."}),
        _answer(ROUTE, {"role": "assistant", "content": "LAST LINE: keep the old API alive"})]
    gateway.dispatch = ["response_received"] * 3
    text, _usage, trace = _run(ctx, tools, events, built)

    first, refused, retry = (payload["messages"] for payload, _key in gateway.uploads)
    delivery = trace["tool_result_delivery"][0]
    shown = next(row["content"] for row in refused if row.get("tool_call_id") == "giant-read")
    # The chosen page went out whole although the target's room could not hold it, and the unknown was named.
    assert delivery["status"] == "capacity_unknown" and delivery["whole_tokens"] > OWNER_LOW_TARGET_TOKENS
    assert delivery["whole_tokens"] - delivery["minimum_tokens"] > delivery["free_tokens"]
    header, *rows = shown.rstrip("\n").split("\n")  # the numbered view, row by row: the file itself, unchanged
    assert "lines 1–20001 of 20001" in header and "".join(row.split("\t", 1)[1] + "\n" for row in rows) == giant
    # The pre-send measurement saw the target deficit (no window to fit) and concealed nothing for it.
    facts = [row["content"] for row in refused if str(row["content"]).startswith("[CONTEXT_FACTS]")][-1]
    assert "window unknown" in facts and "free unknown" in facts and "over target" in facts
    # The refusal was recovered on the same route by one strictly smaller resend.
    assert text == "LAST LINE: keep the old API alive" and len(gateway.creates) == 3
    assert [payload["account"] for payload, _key in gateway.uploads] == [{"mode": "pin", "profileId": "account-a"}] * 3
    assert len(json.dumps(retry)) < len(json.dumps(refused)) // 10
    assert not any(row.get("tool_call_id") == "giant-read" for row in retry)
    # The owner's operative words are the same bytes in every send.
    task = [[row for row in sent if task_text in json.dumps(row, ensure_ascii=False)] for sent in (first, refused, retry)]
    assert task[0] == task[1] == task[2] and len(task[0]) == 1
    # The body is its exact source: the rescue names the read that reaches it, and that source holds the bytes sent.
    rescue = next(row for row in retry if "Late source-only rescue" in json.dumps(row))["content"][0]["text"]
    reference = json.loads(rescue.split("Source reference: ", 1)[1].split("\n", 1)[0])["checkpoint_ref"]
    checkpoint = json.loads(artifacts.read_actor_source_bytes(ctx.drive_root, "task-one", reference))
    assert next(row["content"] for row in checkpoint["messages"] if row.get("tool_call_id") == "giant-read") == shown
    # No duplicate effect: the loop executed the read once; the rescue's own read is a new, explicit one.
    assert len(runs) == 1 and [call["tool"] for call in trace["tool_calls"]] == ["read_file"]
    page = tools.execute(reference["read"]["tool"], reference["read"]["arguments"])
    assert reference["path"] in page.split("\n", 1)[0] and "LAST LINE: keep the old API alive" in page and len(runs) == 2


# --- #1527: Nano sends the task text while the request physically fits --------------------------------

def test_a_short_nano_task_stays_text_when_the_fixed_part_alone_exceeds_the_target(tmp_path, monkeypatch):
    from ouroboros import context, context_fit
    from ouroboros.context_budget import OWNER_NANO_TARGET_TOKENS
    from tests._memory_view_context import world

    env, memory, _rooms = world(tmp_path)
    # A few paragraphs: longer than the pointer that replaced it before (#1527), tiny beside the fixed part.
    words = "Rename the helper in billing, keep every caller working and report the diff. " * 40 + "FINAL: no new files."
    task = {"type": "task", "text": words, "id": "tmain", "chat_id": 1}
    core = context._capture_context_core(env, memory, task, None, None)
    heavy = [{"type": "function", "function": {"name": "enable_tools", "description": "d" * 400_000,
                                               "parameters": {"type": "object", "properties": {}}}}]
    monkeypatch.setattr(context_fit, "_route_calibration_ratio", lambda *_a, **_kw: 1.0)
    built = context._build_context_fit_plan(env, core, task, preferred_mode="nano", tool_schemas=heavy,
        route_resolver=lambda *_a, **_kw: ({"model": "m", "provider": "p"}, SimpleNamespace(
            route_fp="r", status="asserted", stale=False, window_tokens=1_000_000)))
    first = built.messages_for(built.initial_mode)
    assert built.initial_mode == "nano" and built.nano_projection.user_content_json is None
    assert context_fit.tool_schema_tokens(heavy) > OWNER_NANO_TARGET_TOKENS  # the target is out of reach anyway
    assert "FINAL: no new files." in json.dumps(first[1]["content"]) and "[Exact task input source]" not in json.dumps(first)
    assert len(words) > 2_000  # the pointer would have been "shorter", and bought nothing
