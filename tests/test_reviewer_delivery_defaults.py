"""#1334 / #1116 / #1335: reviewer delivery defaults and the one-time notices.

A direct api_chat triad row carries its saved ``delivery``; a row saved before
the field keeps its packet meaning (owner 1D) and every consumer reads the one
``retrieves`` fact rather than inferring it from an actor id. The shipped panel
reads the work itself; an OpenAI-compatible-only install reviews on Main instead
of unreachable OpenRouter ids; and an upgraded install hears once, factually,
about the default panel and any finite task limit it still runs under.
"""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from ouroboros import reviewer_slot_config as rsc
from ouroboros.reviewer_slot_config import REVIEWER_SLOTS_ENV, parse_reviewer_slots

_SCOPE = [{"slot_id": "s1", "route": {"kind": "api_chat", "target_id": "m/scope"}}]


def _panel(*triad, scope=None, **extra):
    return json.dumps({"triad": list(triad), "scope": scope or _SCOPE, **extra})


def _api(slot_id, model="m/one", **fields):
    return {"slot_id": slot_id, "route": {"kind": "api_chat", "target_id": model}, **fields}


@pytest.fixture
def clean_env(monkeypatch):
    for key in (REVIEWER_SLOTS_ENV, "OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS",
                "OUROBOROS_SCOPE_REVIEW_MODEL", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_BASE_URL", "OUROBOROS_MODEL", "OUROBOROS_MODEL_LIGHT",
                "MINIMAX_API_KEY", "DEEPSEEK_API_KEY", "ZAI_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY",
                "GIGACHAT_CREDENTIALS", "USE_LOCAL_MAIN"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# --- the saved delivery field --------------------------------------------------------


def test_a_bare_row_keeps_packet_and_an_explicit_delivery_is_the_rows_own_fact():
    config = parse_reviewer_slots(_panel(_api("bare"), _api("native", delivery="native"),
                                         _api("packet", delivery="packet")))
    bare, native, packet = config.triad
    assert (bare.delivery, bare.retrieves) == ("", False)
    assert (native.delivery, native.native_retrieval, native.retrieves) == ("native", True, True)
    assert (packet.delivery, packet.retrieves) == ("packet", False)
    assert not native.subagent_id  # no synthetic roster actor carries it


@pytest.mark.parametrize(("config", "where"), [
    (_panel({"slot_id": "x", "route": {"kind": "agent_session", "target_id": "codex"}, "delivery": "native"}), "triad[0]"),
    (_panel(_api("t"), scope=[_api("s", delivery="native")]), "scope[0]"),
    (_panel(_api("t"), deep_review={"route": {"kind": "api_chat", "target_id": "m"}, "delivery": "packet"}), "deep_review"),
    (_panel(_api("t", delivery="both")), "triad[0]"),
])
def test_delivery_is_refused_wherever_it_means_nothing(config, where):
    with pytest.raises(ValueError, match=re.escape(where)):
        parse_reviewer_slots(config)


def test_the_delivery_slot_and_row_plan_carry_native_without_an_actor(clean_env):
    clean_env.setenv(REVIEWER_SLOTS_ENV, _panel(_api("n", delivery="native"), _api("p")))
    slots = rsc.triad_delivery_slots(role_hint="task acceptance")
    assert [(s.slot_id, s.native_retrieval, s.retrieves, s.subagent_id) for s in slots] == [
        ("n", True, True, ""), ("p", False, False, "")]
    plan = rsc.commit_triad_delivery()
    assert plan["retrieves"] == [True, False] and plan["legacy_skill_fingerprint"] is False
    assert [rsc.row_plan_retrieves(plan, i) for i in range(2)] == [True, False]
    # A plan without the vector falls back to route + actor (old callers, fixtures).
    legacy = {k: v for k, v in plan.items() if k != "retrieves"}
    assert [rsc.row_plan_retrieves(legacy, i) for i in range(2)] == [False, False]


def test_settings_round_trip_keeps_bare_rows_bare_and_native_rows_native(clean_env):
    from starlette.requests import Request

    from ouroboros.gateway.settings import api_reviewer_slots

    clean_env.setenv(REVIEWER_SLOTS_ENV, _panel(_api("bare"), _api("native", delivery="native")))
    request = Request({"type": "http", "method": "GET", "path": "/api/reviewer-slots",
                       "headers": [], "query_string": b""})
    body = json.loads(asyncio.run(api_reviewer_slots(request)).body)
    assert "delivery" not in body["triad"][0] and body["triad"][1]["delivery"] == "native"
    saved = json.dumps({key: body[key] for key in ("triad", "scope", "advisory")})
    assert [row.delivery for row in parse_reviewer_slots(saved).triad] == ["", "native"]


def test_the_shipped_default_triad_reads_natively_on_the_same_models(clean_env):
    clean_env.setenv("OUROBOROS_REVIEW_MODELS", "m/a,m/b,m/c")
    config = rsc.load_reviewer_slot_config()
    assert config.source == "default"
    assert [(r.target_id, r.delivery) for r in config.triad] == [("m/a", "native"), ("m/b", "native"), ("m/c", "native")]


def test_the_skill_fingerprint_names_native_delivery_only_where_a_row_reads(clean_env):
    from ouroboros.skill_review_cycles import skill_review_contract_fingerprint

    def fingerprint(panel):
        clean_env.setenv(REVIEWER_SLOTS_ENV, panel)
        delivery = rsc.commit_triad_delivery()
        return skill_review_contract_fingerprint(delivery["models"], delivery=delivery,
                                                 required_items=("a",), review_profile="p")

    bare, packet, native = (fingerprint(_panel(_api("t"))), fingerprint(_panel(_api("t", delivery="packet"))),
                            fingerprint(_panel(_api("t", delivery="native"))))
    assert bare == packet != native


def test_the_triad_multi_model_row_runs_its_native_delivery(monkeypatch, tmp_path):
    """The commit/skill triad consumer hands a native row the compact work order
    and the native episode — never the assembled packet messages."""
    import ouroboros.review_substrate as rs
    from ouroboros.review_execution import ReviewRouteKind
    from ouroboros.tools import review as review_module
    from ouroboros.tools import review_multi_model as rmm

    seen = []

    def run(request, *, slots, **_kw):
        seen.append((request, slots[0]))
        return SimpleNamespace(actors=[{"slot_id": slots[0].slot_id, "status": "ok", "raw_text": "[]"}])

    monkeypatch.setattr(rs, "run_review_request", run)
    monkeypatch.setattr(review_module, "review_drive_root", lambda ctx: tmp_path)
    plan = {"slot_ids": ["n"], "subagent_ids": [""], "retrieves": [True], "session_profiles": [""],
            "efforts": [""], "session_targets": [""], "use_local": [False]}
    asyncio.run(rmm._multi_model_review_async(
        "content", "", ["m/one"], None, routes=[ReviewRouteKind.API_CHAT], row_plan=plan,
        session_task="WORK ORDER", session_root="/repo"))
    (request, slot), = seen
    assert slot.native_retrieval and request.messages == [] and request.session_task.startswith("WORK ORDER")


# --- #1116: an OpenAI-compatible-only install ------------------------------------------


def test_a_compatible_only_install_reviews_on_main(clean_env):
    from ouroboros.review_model_routes import get_review_models, get_scope_review_models
    from ouroboros.tools.claude_advisory_review import _advisory_default_model

    clean_env.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://llm.example/v1")
    clean_env.setenv("OUROBOROS_MODEL", "openai-compatible::glm-5.3")
    assert get_review_models() == ["openai-compatible::glm-5.3"] * 3
    assert get_scope_review_models() == ["openai-compatible::glm-5.3"]
    assert _advisory_default_model() == "openai-compatible::glm-5.3"
    # The default panel shows the advisory on that route instead of a keyless OpenRouter default.
    assert rsc.load_reviewer_slot_config().advisory.target_id == "openai-compatible::glm-5.3"
    # An explicit compatible list is the owner's and is honoured exactly.
    clean_env.setenv("OUROBOROS_REVIEW_MODELS", "openai-compatible::a,openai-compatible::b")
    assert get_review_models() == ["openai-compatible::a", "openai-compatible::b"]


@pytest.mark.parametrize("other_key", ["OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL"])
def test_another_remote_route_keeps_the_existing_defaults(clean_env, other_key):
    from ouroboros.review_model_routes import get_review_models
    from ouroboros.settings_defaults import OPENROUTER_REVIEW_DEFAULTS

    clean_env.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://llm.example/v1")
    clean_env.setenv("OUROBOROS_MODEL", "openai-compatible::glm-5.3")
    clean_env.setenv(other_key, "x-key" if other_key != "OPENAI_BASE_URL" else "https://base.example")
    assert get_review_models() != ["openai-compatible::glm-5.3"] * 3
    if other_key == "OPENROUTER_API_KEY":
        assert get_review_models() == list(OPENROUTER_REVIEW_DEFAULTS["triad"])


def test_the_onboarding_preview_is_native_and_reachable_on_a_compatible_only_install():
    from ouroboros.subscription_install_presets import preview_api_reviewer_slots

    draft = json.loads(preview_api_reviewer_slots({
        "OPENAI_COMPATIBLE_BASE_URL": "https://llm.example/v1", "OUROBOROS_MODEL": "openai-compatible::glm-5.3",
    }))
    assert {row["route"]["target_id"] for row in draft["triad"] + draft["scope"]} == {"openai-compatible::glm-5.3"}
    assert all(row["delivery"] == "native" for row in draft["triad"])
    assert not any("delivery" in row for row in draft["scope"])
    assert draft["advisory"]["route"]["target_id"] == "openai-compatible::glm-5.3"
    assert all(row.retrieves for row in parse_reviewer_slots(json.dumps(draft)).triad)


# --- one-time notices (#1334, #1335) --------------------------------------------------


@pytest.mark.parametrize(("document", "settings", "expected"), [
    (None, {"OUROBOROS_MAX_ROUNDS": "unlimited", "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"}, []),  # fresh
    ({}, {"OUROBOROS_MAX_ROUNDS": 200, "OUROBOROS_TASK_ABS_CEILING_SEC": 21600},
     [("OUROBOROS_MAX_ROUNDS", 200, "document_absent_key"),
      ("OUROBOROS_TASK_ABS_CEILING_SEC", 21600, "document_absent_key")]),
    ({"OUROBOROS_MAX_ROUNDS": "unlimited", "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"}, {}, []),
    ({"OUROBOROS_MAX_ROUNDS": 150, "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"}, {},
     [("OUROBOROS_MAX_ROUNDS", 150, "saved")]),
    ({"OUROBOROS_MAX_ROUNDS": "abc", "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"}, {"OUROBOROS_MAX_ROUNDS": 200},
     [("OUROBOROS_MAX_ROUNDS", 200, "saved_invalid")]),
    ({"OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"}, {"OUROBOROS_MAX_ROUNDS": 300},
     [("OUROBOROS_MAX_ROUNDS", 300, "env")]),  # only the launch environment explains 300
    (None, {"OUROBOROS_MAX_ROUNDS": 120, "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"},
     [("OUROBOROS_MAX_ROUNDS", 120, "env")]),
])
def test_finite_bound_facts_name_only_what_the_document_and_env_show(document, settings, expected):
    from ouroboros.upgrade_notices import optional_bound_facts, optional_bounds_notice

    facts = optional_bound_facts(document, settings)
    assert [(f["key"], f["value"], f["origin"]) for f in facts] == expected
    text = optional_bounds_notice(facts)
    assert bool(text) == bool(expected)
    if expected:
        assert "nothing was changed here" in text and "chose" not in text


def test_the_classifier_ignores_the_process_environment_startup_projects_settings_into(monkeypatch):
    from ouroboros.upgrade_notices import optional_bound_facts

    # apply_settings_to_env has already exported the document default before the notice runs.
    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "200")
    facts = optional_bound_facts({}, {"OUROBOROS_MAX_ROUNDS": 200, "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"})
    assert [(f["value"], f["origin"]) for f in facts] == [(200, "document_absent_key")]


@pytest.mark.serial
@pytest.mark.parametrize(("document", "launch_env", "expected", "origin", "raw"), [
    ({"OUROBOROS_MAX_ROUNDS": None}, "77", 200, "saved_invalid", None),
    ({"OUROBOROS_MAX_ROUNDS": ""}, "77", 200, "saved_invalid", ""),
    ({"OUROBOROS_MAX_ROUNDS": "bad"}, "77", 200, "saved_invalid", "bad"),
    ({"OUROBOROS_MAX_ROUNDS": 42}, "77", 42, "saved", 42),
    ({}, None, 200, "document_absent_key", 200),
    ({}, "77", 77, "env", 77),
    (None, "77", 77, "env", 77),
    ({"OUROBOROS_MAX_ROUNDS": "unlimited"}, "77", None, None, None),
    ({"OUROBOROS_TASK_ABS_CEILING_SEC": None}, "60", 21600, "saved_invalid", None),
    ({"OUROBOROS_TASK_ABS_CEILING_SEC": ""}, "60", 21600, "saved_invalid", ""),
    ({"OUROBOROS_TASK_ABS_CEILING_SEC": 60}, None, 300, "saved", 60),
    ({}, "60", 300, "env", 60),
])
def test_startup_notice_matches_loaded_runtime_bounds_and_raw_presence(
        monkeypatch, tmp_path, document, launch_env, expected, origin, raw):
    from ouroboros import config as cfg, upgrade_notices as notices
    from ouroboros.settings_integrity import task_settings_scope, task_settings_snapshot
    from ouroboros.utils import iter_jsonl_chain_objects
    from supervisor import message_bus as bus, state as ss

    lifetime = "OUROBOROS_TASK_ABS_CEILING_SEC"
    rounds = "OUROBOROS_MAX_ROUNDS"
    key = lifetime if (document and lifetime in document) or launch_env == "60" else rounds
    other = rounds if key == lifetime else lifetime
    path = tmp_path / "settings.json"
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", path)
    for setting in cfg.SETTINGS_DEFAULTS:
        monkeypatch.delenv(setting, raising=False)
    if launch_env is not None:
        monkeypatch.setenv(key, launch_env)
    monkeypatch.setenv(other, "unlimited")
    if document is not None:
        path.write_text(json.dumps({other: "unlimited", **document}), encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    for name, value in {"DRIVE_ROOT": tmp_path, "STATE_PATH": tmp_path / "state/state.json",
                        "STATE_LAST_GOOD_PATH": tmp_path / "state/state.last_good.json",
                        "STATE_LOCK_PATH": tmp_path / "locks/state.lock"}.items():
        monkeypatch.setattr(ss, name, value)
    monkeypatch.setattr(bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bus, "get_bridge", lambda: None)
    ss.save_state({"owner_chat_id": 7, "owner_id": 1, "reviewer_default_delivery_notified": "already"})

    settings = cfg.load_settings()
    projected = {}
    cfg.apply_settings_to_env(settings, environ=projected)
    with task_settings_scope(task_settings_snapshot(settings, projected)):
        effective = cfg.get_task_abs_ceiling_sec() if key == lifetime else cfg.get_max_rounds()
        assert effective == expected
        notices.startup_upgrade_notices(settings)
        notices.startup_upgrade_notices(settings)
    assert (path.read_bytes() if path.exists() else None) == before
    facts = notices.optional_bound_facts(notices._raw_settings_document(), settings)
    rows = [r for r in iter_jsonl_chain_objects(tmp_path / "logs/chat.jsonl")
            if r.get("type") == "optional_bounds_notice"]
    if expected is None:
        assert facts == [] and rows == []
        return
    assert facts == [{"key": key, "value": effective, "origin": origin, "raw": raw}]
    assert len(rows) == 1 and rows[0]["direction"] == "system"
    text = rows[0]["text"]
    assert f"= {effective} {'seconds' if key == lifetime else 'rounds'}" in text
    assert ("key is absent" in text) == (origin == "document_absent_key")
    assert ("finite fallback" in text) == (origin == "saved_invalid")
    if key == lifetime and raw == 60:
        assert "as 60; the runtime minimum applies" in text
    assert "nothing was changed here" in text and "chose" not in text


@pytest.fixture
def notice_world(monkeypatch, tmp_path):
    import supervisor.message_bus as bus
    import supervisor.state as sstate
    from ouroboros import upgrade_notices

    state = {"owner_chat_id": 7}
    sent = []

    def update(fn):
        fn(state)

    monkeypatch.setattr(sstate, "load_state", lambda: dict(state))
    monkeypatch.setattr(sstate, "update_state", update)
    monkeypatch.setattr(bus, "send_with_budget", lambda chat, text, **kw: sent.append((chat, text, kw)))
    monkeypatch.setattr(upgrade_notices, "_raw_settings_document", lambda: {})
    monkeypatch.delenv("OUROBOROS_MAX_ROUNDS", raising=False)
    monkeypatch.delenv("OUROBOROS_TASK_ABS_CEILING_SEC", raising=False)
    return SimpleNamespace(state=state, sent=sent, notices=upgrade_notices)


def test_an_upgraded_untouched_install_hears_each_notice_once(notice_world):
    notice_world.notices.startup_upgrade_notices({})
    assert [kw["system_type"] for _chat, _text, kw in notice_world.sent] == [
        "reviewer_default_notice", "optional_bounds_notice"]
    assert all(kw["require_write"] and kw["role"] == "system" for _c, _t, kw in notice_world.sent)
    assert "200 rounds" in notice_world.sent[1][1] and "21600 seconds" in notice_world.sent[1][1]
    notice_world.notices.startup_upgrade_notices({})
    assert len(notice_world.sent) == 2  # never repeated
    assert set(notice_world.state) >= {"reviewer_default_delivery_notified", "optional_bounds_notified"}


def test_a_saved_panel_hears_nothing_about_the_default(notice_world):
    notice_world.notices.startup_upgrade_notices({"OUROBOROS_REVIEWER_SLOTS": _panel(_api("t"))})
    assert [kw["system_type"] for _c, _t, kw in notice_world.sent] == ["optional_bounds_notice"]


def test_no_owner_chat_or_a_failed_write_leaves_the_notice_owed(notice_world, monkeypatch):
    import supervisor.message_bus as bus

    notice_world.state["owner_chat_id"] = 0
    notice_world.notices.startup_upgrade_notices({})
    assert notice_world.sent == [] and "optional_bounds_notified" not in notice_world.state
    notice_world.state["owner_chat_id"] = 7
    monkeypatch.setattr(bus, "send_with_budget", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    notice_world.notices.startup_upgrade_notices({})
    assert "reviewer_default_delivery_notified" not in notice_world.state


@pytest.mark.parametrize("quality", ["unavailable", "recovered", "recovered_transient"])
def test_notice_never_uses_unconfirmed_display_owner(notice_world, quality):
    notice_world.state["_state_read"] = {"quality": quality, "source": "backup",
                                         "unconfirmed": ["owner_chat_id"]}
    notice_world.notices.startup_upgrade_notices({})
    assert not notice_world.sent
    assert "reviewer_default_delivery_notified" not in notice_world.state
    assert "optional_bounds_notified" not in notice_world.state


def test_notice_bookkeeping_preserves_recovered_control_uncertainty(monkeypatch, tmp_path):
    from ouroboros import upgrade_notices as notices
    from supervisor import message_bus as bus, state as ss

    for name, path in {"DRIVE_ROOT": tmp_path, "STATE_PATH": tmp_path / "state/state.json",
                       "STATE_LAST_GOOD_PATH": tmp_path / "state/state.last_good.json",
                       "STATE_LOCK_PATH": tmp_path / "locks/state.lock"}.items():
        monkeypatch.setattr(ss, name, path)
    monkeypatch.setattr(bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bus, "get_bridge", lambda: SimpleNamespace(send_message=lambda *a, **kw: None))
    monkeypatch.setattr(notices, "_raw_settings_document", lambda: {})
    ss.save_state({"owner_chat_id": 7, "owner_id": 1, "bg_consciousness_enabled": True})
    ss.STATE_PATH.write_bytes(b'{broken')
    before = ss.load_state()
    assert ss.control_value(before, "owner_chat_id") == (True, 7)
    assert ss.control_value(before, "bg_consciousness_enabled") == (False, None)
    notices.startup_upgrade_notices({})
    after = ss.load_state()
    assert after[notices.REVIEWER_DEFAULT_NOTICE_KEY]
    assert after[notices.OPTIONAL_BOUNDS_NOTICE_KEY]
    assert ss.control_value(after, "bg_consciousness_enabled") == (False, None)
    assert ss.control_value(after, "owner_chat_id") == (True, 7)


@pytest.mark.parametrize("failure", ["chat_append", "state_marker", "bridge"])
def test_notice_recovers_through_real_chat_and_state_writers(monkeypatch, tmp_path, failure):
    from ouroboros import upgrade_notices as notices
    from ouroboros.utils import iter_jsonl_chain_objects
    from supervisor import message_bus as bus, state as ss

    for name, path in {"DRIVE_ROOT": tmp_path, "STATE_PATH": tmp_path / "state/state.json",
                       "STATE_LAST_GOOD_PATH": tmp_path / "state/state.last_good.json",
                       "STATE_LOCK_PATH": tmp_path / "locks/state.lock"}.items():
        monkeypatch.setattr(ss, name, path)
    monkeypatch.setattr(bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(notices, "_raw_settings_document", lambda: {"OUROBOROS_MAX_ROUNDS": 150,
                        "OUROBOROS_TASK_ABS_CEILING_SEC": "unlimited"})
    ss.save_state({"owner_chat_id": 7, "owner_id": 1})
    real_update, real_append = ss.update_state, bus.append_jsonl
    delivered = []
    failed = False

    def fail_once():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected persistence/delivery failure")

    def update(fn):
        if failure == "state_marker":
            fail_once()
        return real_update(fn)

    def append(*a, **kw):
        if failure == "chat_append":
            fail_once()
        return real_append(*a, **kw)

    def send(*a, **kw):
        if failure == "bridge":
            fail_once()
        delivered.append(a)

    monkeypatch.setattr(ss, "update_state", update)
    monkeypatch.setattr(bus, "append_jsonl", append)
    monkeypatch.setattr(bus, "get_bridge", lambda: SimpleNamespace(send_message=send))
    settings = {"OUROBOROS_REVIEWER_SLOTS": _panel(_api("t"))}
    notices.startup_upgrade_notices(settings)
    assert not ss.load_state().get(notices.OPTIONAL_BOUNDS_NOTICE_KEY)
    notices.startup_upgrade_notices(settings)
    notices.startup_upgrade_notices(settings)
    assert ss.load_state()[notices.OPTIONAL_BOUNDS_NOTICE_KEY]
    rows = [r for r in iter_jsonl_chain_objects(tmp_path / "logs/chat.jsonl") if r.get("type") == "optional_bounds_notice"]
    assert len(rows) == 1 and "150 rounds" in rows[0]["text"]
    # Durable web history is the owner delivery witness. External bridge loss
    # does not erase that row or require another visible notice on restart.
    assert len(delivered) == (0 if failure == "bridge" else 1)


def test_compatible_deep_default_and_authored_choices(clean_env):
    from ouroboros.config import get_deep_self_review_model
    from ouroboros.deep_self_review import deep_review_route
    from ouroboros.settings_defaults import OPENROUTER_DEFAULTS

    clean_env.delenv("OUROBOROS_MODEL_DEEP_SELF_REVIEW", raising=False)
    clean_env.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://llm.example/v1")
    clean_env.setenv("OPENAI_COMPATIBLE_API_KEY", "test-only-key")
    clean_env.setenv("OUROBOROS_MODEL", "openai-compatible::glm-5.3")
    assert get_deep_self_review_model() == "openai-compatible::glm-5.3"
    assert deep_review_route() == ("", "openai-compatible::glm-5.3")
    clean_env.setenv("OUROBOROS_MODEL_DEEP_SELF_REVIEW", OPENROUTER_DEFAULTS["deep_self_review"])
    assert get_deep_self_review_model() == OPENROUTER_DEFAULTS["deep_self_review"]
    clean_env.delenv("OUROBOROS_MODEL_DEEP_SELF_REVIEW")
    saved = parse_reviewer_slots(_panel(_api("old")))
    assert rsc.deep_review_slot(saved).target_id == OPENROUTER_DEFAULTS["deep_self_review"]
    clean_env.setenv(REVIEWER_SLOTS_ENV, _panel(_api("old")))
    assert rsc.deep_review_slot().target_id == OPENROUTER_DEFAULTS["deep_self_review"]
    clean_env.setenv(REVIEWER_SLOTS_ENV, _panel(_api("old"), deep_review={"route": {"kind": "api_chat", "target_id": "authored/model"}, "effort": "high"}))
    assert rsc.deep_review_slot().target_id == "authored/model"
