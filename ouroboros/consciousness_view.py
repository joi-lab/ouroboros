"""Read-only consciousness facts from places, current roots and durable results."""

from typing import Any

from ouroboros.usage_origin import cause_label


def _clock_of(iso_value: Any) -> str:
    """``HH:MM`` in the server's local time for an ISO instant (``?`` when absent)."""
    from ouroboros.deadline_utils import parse_deadline_ts

    parsed = parse_deadline_ts(str(iso_value or ""))
    return parsed.astimezone().strftime("%H:%M") if parsed is not None else "?"


def describe_consciousness_state(snapshot: dict, requested_enabled: bool | None) -> dict:
    """Project the alarm clock's snapshot into one honest status + detail line
    (``None``: the stored control is unknown, never read as off — #1307)."""
    outcome = str(snapshot.get("last_wake_outcome") or "")
    next_at = _clock_of(snapshot.get("next_wake_at"))
    if requested_enabled is None:
        status, detail = "unknown", "The stored consciousness control is unknown: runtime state is unavailable or recovering."
    elif not requested_enabled:
        status, detail = "disabled", "Background consciousness is off."
    elif not snapshot:
        status, detail = "stopped", "Enabled in state, but the alarm clock was not constructed (supervisor init failed)."
    elif snapshot.get("live_wake_task_id"):
        status, detail = "thinking", f"Wake-up {snapshot['live_wake_task_id']} is running as a Main turn."
    elif outcome == "skipped:waiting_for_first_conversation":
        status, detail = "waiting_for_first_conversation", "No owner chat is bound yet; the first conversation binds it."
    elif outcome == "skipped:allowance_exhausted":
        spent, daily = snapshot.get("spent_24h_usd"), snapshot.get("daily_usd")
        status = "allowance_exhausted"
        at_least = "at least " if int(snapshot.get("unknown_unmetered") or 0) > 0 else ""
        degraded = "; ledger integrity degraded" if snapshot.get("integrity_degraded") else ""
        detail = (f"Daily allowance spent ({at_least}${float(spent or 0):.2f} of ${float(daily or 0):.2f} in the last 24 h{degraded}); "
                  f"next check at {_clock_of(snapshot.get('allowance_resets_at')) if snapshot.get('allowance_resets_at') else next_at}.")
    elif outcome == "skipped:allowance_unknown":
        status, detail = "allowance_unknown", f"The usage ledger could not be read ({snapshot.get('last_error') or 'unknown error'}); retry at {next_at}."
    elif outcome.startswith("rejected:"):
        status, detail = "wake_rejected", f"The last wake-up was refused ({outcome.split(':', 1)[1]}); next attempt at {next_at}."
    elif outcome in {"paused", "pausing"}:
        status = "wake_paused"
        detail = (f"The last wake-up returned while {outcome}; its task card shows the current state. "
                  f"Next wake check at {next_at}.")
    elif outcome == "unknown":
        status, detail = "wake_outcome_unknown", f"The last wake-up's outcome is unconfirmed; next check at {next_at}."
    elif outcome == "failed":
        status, detail = "wake_failed", f"The last wake-up failed ({snapshot.get('last_error') or 'runner error'}); next attempt at {next_at}, backing off."
    else:
        status, detail = "sleeping", f"Sleeping until {next_at}."
    return {**snapshot, "enabled": requested_enabled, "status": status, "detail": detail}



def describe_consciousness(clock) -> dict:
    """Read the same stored control as /api/state, including unknown and no-clock boot states."""
    from supervisor.state import control_value, load_state

    known, enabled = control_value(load_state(), "bg_consciousness_enabled")
    requested = bool(enabled) if known else None
    if clock is not None:
        return clock.details_snapshot(requested)
    return {**describe_consciousness_state({}, requested), "places": [], "recent_wakes": []}


def place_views(drive_root) -> list[dict]:
    """One row per configured place; unavailable room counts stay unknown."""
    from ouroboros import places

    views = []
    room_index = getattr(places, "place_room_index", None)
    for place in places.load_places(drive_root):
        view = dict(places.place_summary(place))
        try:
            rooms = room_index(drive_root, place.place_id) if room_index else None
        except Exception:
            rooms = None
        view["rooms"] = rooms
        if rooms is not None:
            counts = [room.get("unanswered_count") for room in rooms]
            view["unanswered"] = sum(counts) if all(isinstance(n, int) for n in counts) else None
        else:
            view.setdefault("unanswered", None)
        views.append(view)
    return views


def places_text(views) -> str:
    if views is None:
        return "unknown (place index unavailable)"
    lines = []
    for view in views:
        count = view.get("unanswered")
        addresses = [str(room.get("address") or f'chat_history(chat_id={int(room["room_chat_id"])})')
                     for room in view.get("rooms") or []]
        address = "; ".join(addresses) or ("no captured room" if view.get("rooms") == [] else "room address unknown")
        lines.append(f'{view.get("label") or view["place_id"]} [{view["place_id"]}]: {view["state"]}; '
                     f'unanswered: {count if count is not None else "unknown"}; {address}')
    return "\n  ".join(lines) if lines else "none"


def cause_views(rows, places) -> list[dict] | None:
    if rows is None:
        return None
    return [{**row, "label": cause_label(row["cause"], places or ())} for row in rows]


def root_views(roots, places) -> list[dict]:
    return [{**root, "cause": root["consciousness_cause"],
             "cause_label": cause_label(root["consciousness_cause"], places or ())} for root in roots]


def spend_text(rows) -> str:
    if rows is None:
        return "unknown"
    return "; ".join(f'{row["label"]}: {"at least " if row["unmetered_calls"] else ""}'
                     f'${row["usd"]:.2f} ({row["unmetered_calls"]} unmetered calls)' for row in rows) or "none"


def roots_text(rows) -> str:
    if rows is None:
        return "unknown"
    return "; ".join(f'{row["task_id"]}: {row["title"]} ({row["cause_label"]})' for row in rows) or "none"


def first_reply_at(drive_root, task_id: str) -> str | None:
    """Earliest confirmed receipt or automatic release, using this task's dialogue index.

    Authored mode-0 replies prove release, not provider delivery. Failed/uncertain
    receipts, drafts and task summaries prove neither. Read pages, never a global log.
    """
    from ouroboros import dialogue_store
    from ouroboros.deadline_utils import parse_deadline_ts

    first = None
    for stored in dialogue_store.iter_probes(drive_root, "chat", [("task_id", task_id)]):
        row = stored.row
        transport = row.get("transport") or {}
        delivery = transport.get("delivery") or {}
        receipt = row.get("type") == "presence_delivery" and delivery.get("state") in {"accepted", "delivered"}
        released = (row.get("direction") == "out" and str(row.get("source") or "").startswith("presence:")
                    and delivery.get("state") == "authored")
        if row.get("task_id") != task_id or not (receipt or released):
            continue
        stamp = str(delivery.get("reported_at") or row.get("ts") or "")
        instant = parse_deadline_ts(stamp)
        if instant is not None and (first is None or instant < first[0]):
            first = instant, stamp
    return first[1] if first else None


def recent_wakes(drive_root, places) -> list[dict]:
    """Wake facts among the 50 newest task rows, with explicit snapshot coverage.

    Task selection is bounded; reply rows use the dialogue task index and checks
    the five-chip task record. Missing historical evidence stays absent.
    """
    from ouroboros.consciousness_authority import wake_cause
    from ouroboros.cost_projection import cost_projection
    from ouroboros.consciousness_wake import _clip_preview
    from ouroboros.task_store import documents_by_ids, newest_ids

    ids = newest_ids(drive_root, limit=50)
    documents = documents_by_ids(drive_root, ids)
    rows = []
    for task_id in ids:
        row = documents[task_id]
        metadata = row.get("metadata") or {}
        cause = wake_cause(metadata)
        if not cause:
            continue
        focus = metadata.get("focus") or {}
        place_id = str(focus.get("place_id") or "")
        rows.append({"task_id": task_id, "cause": cause, "place_id": place_id,
                     "place_label": cause_label(f"place:{place_id}", places or ()) if place_id else "",
                     "started_at": row.get("started_at"), "first_reply_at": first_reply_at(drive_root, task_id),
                     "finished_at": row.get("finished_at"), "outcome": metadata.get("presence_outcome") or row.get("status", "unknown"),
                     "accounted_upper_bound_usd": cost_projection(row)["accounted_upper_bound_usd"],
                     "unmetered": bool(row.get("unknown_unmetered")),
                     "checks": row.get("safety_checks", []), "summary": _clip_preview(row.get("result"))})
    return rows
