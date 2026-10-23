"""Resolved Safety Supervisor settings authority and one-time upgrade persistence."""

import os


def _safety_values(settings: dict) -> dict:
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS
    from ouroboros.server_runtime import authored_model_slots, propose_unauthored_model_slots, apply_runtime_provider_defaults

    values = {**SETTINGS_DEFAULTS, **settings}
    propose_unauthored_model_slots(values, authored_model_slots(settings))
    return apply_runtime_provider_defaults(values)[0]


def safety_configuration(settings: dict) -> tuple:
    """Resolved checker identity; unrelated model/catalog changes remain ordinary writes."""
    return _resolved_safety_configuration(_safety_values(settings))


def _resolved_safety_configuration(values: dict) -> tuple:
    from ouroboros.configured_subagents import parse_configured_subagents
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS
    from ouroboros.settings_scales import normalize_safety_mode
    from ouroboros.model_slots import (model_role_option, MODEL_ACCOUNTS_KEY,
                                     MODEL_CONTEXT_WINDOWS_KEY, MODEL_PROCESSING_PREFERENCES_KEY)
    slot = "safety" if str(values.get("OUROBOROS_MODEL_SAFETY") or "").strip() else "light"
    if slot == "light" and not str(values.get("OUROBOROS_MODEL_LIGHT") or "").strip():
        slot = "main"
    model = str(values.get({"safety": "OUROBOROS_MODEL_SAFETY", "light": "OUROBOROS_MODEL_LIGHT",
                           "main": "OUROBOROS_MODEL"}[slot]) or SETTINGS_DEFAULTS["OUROBOROS_MODEL"]).strip()
    local = model.endswith(" (local)") or str(values.get(f"USE_LOCAL_{slot.upper()}") or "").lower() in {"true", "1", "yes", "on"}
    model = model.removesuffix(" (local)")
    options = tuple(model_role_option(key, "safety", settings=values)
                    for key in (MODEL_ACCOUNTS_KEY, MODEL_CONTEXT_WINDOWS_KEY, MODEL_PROCESSING_PREFERENCES_KEY))
    raw = values.get("OUROBOROS_SUBAGENTS")
    recheck = ()
    if raw:
        try:
            catalog = parse_configured_subagents(raw)
            recheck = tuple((row.subagent_id, row.route, row.effort, row.processing_preference,
                             row.enabled, catalog.enabled) for row in catalog.items if row.rechecks_flags)
        except ValueError:
            recheck = ("invalid", str(raw))
    return (model, local, options, recheck,
            normalize_safety_mode(values["OUROBOROS_SAFETY_MODE"]),
            normalize_safety_mode(values["OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"]),
            str(values.get("OUROBOROS_SAFETY_OWNER_RULES") or ""))


def safety_configuration_requires_owner(previous: dict, settings: dict, *, before: dict | None = None,
                                        after: dict | None = None) -> bool:
    """Judge a prepared write, with unauthored slots absent from both documents.

    Only new provider proposals for still-unauthored slots may explain a changed
    checker. Keep every other checker input at its previous resolved value, so
    this exception never authorizes an explicit model, route, mark or rules edit.
    The gateway supplies effective snapshots to retain operator environment input;
    the documents still own whether a slot was authored.
    """
    from ouroboros.settings_defaults import SETTINGS_DEFAULTS
    from ouroboros.server_runtime import provider_slot_proposals

    proposed = {**previous, **settings}
    resolved_after = safety_configuration(proposed if after is None else after)
    expected = _safety_values(previous if before is None else before)
    if _resolved_safety_configuration(expected) == resolved_after:
        return False
    proposals = provider_slot_proposals({**SETTINGS_DEFAULTS, **proposed})
    for key in ("OUROBOROS_MODEL", "OUROBOROS_MODEL_LIGHT"):
        if key not in previous and key not in settings:
            expected[key] = proposals[key]
    return _resolved_safety_configuration(expected) != resolved_after


def _guard_safety_configuration(settings: dict, *, owner_write: bool = False) -> None:
    from ouroboros import config as cfg
    rules = settings.get("OUROBOROS_SAFETY_OWNER_RULES", "")
    if not isinstance(rules, str) or len(rules.encode("utf-8")) > 64_000:
        raise ValueError("Safety owner rules must be text of at most 64,000 UTF-8 bytes")
    if owner_write or cfg.runtime_mode_at_least(cfg.get_runtime_mode(), "cyber_pro"):
        return
    previous = cfg._settings_integrity.read_settings_json_verified(cfg.SETTINGS_PATH) or {}
    if safety_configuration_requires_owner(previous, settings):
        raise PermissionError("The resolved Safety Supervisor is owner-controlled; use /api/owner/safety-model, safety-mode or safety-rules.")


def _migrate_consciousness_safety(raw: dict, *, lock_held: bool) -> dict:
    """Persist the upgrade once, on an existing document under its settings lock.

    An absent document is fresh; a pinned child snapshot is read-only. Neither
    is an upgrade. No effective/read-time copy follows later owner edits.
    """
    from ouroboros import config as cfg
    from ouroboros.settings_scales import normalize_safety_mode
    key = "OUROBOROS_CONSCIOUSNESS_SAFETY_MODE"
    if key in raw or not lock_held or os.environ.get("OUROBOROS_SETTINGS_SHA256"):
        return raw
    from ouroboros.utils import append_jsonl, utc_now_iso, write_text_atomic

    cfg._guard_live_settings_write()
    migrated = {**raw, key: normalize_safety_mode(raw.get("OUROBOROS_SAFETY_MODE", "full"))}
    write_text_atomic(cfg.SETTINGS_PATH, cfg.serialize_settings(migrated))
    append_jsonl(cfg.DATA_DIR / "logs" / "events.jsonl", {
        "type": "safety_settings_migration", "actor": "migration", "ts": utc_now_iso(),
        "key": key, "value": migrated[key],
    })
    return migrated


def get_safety_mode(kind: str = "owner") -> str:
    """Return captured Supervisor coverage (full | light | off).

    Ordinary modes use the owner writer; Cyber may configure subsequent work
    through the same settings owner. Coverage is separate from Cyber's advisory
    decision authority and from the facts of a saved physical request.
    """
    from ouroboros import config as cfg
    key = "OUROBOROS_CONSCIOUSNESS_SAFETY_MODE" if kind == "consciousness" else "OUROBOROS_SAFETY_MODE"
    default_val = str(cfg.SETTINGS_DEFAULTS[key])
    return cfg.normalize_safety_mode(cfg.runtime_setting(key, default_val) or default_val)
